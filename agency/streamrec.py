"""Rolling live-stream recorder with instant clip-to-gate (Peakcut style).

Records a live stream (Twitch / YouTube / Kick / direct .m3u8 or .mp4 URL)
into a rolling buffer of 10-second MPEG-TS segments. The Streams tab cuts
the last 30 or 60 seconds into a clip at any moment and drops it straight
into the approval gate as a package.

    from agency.streamrec import recorder
    recorder.start("twitch.tv/somechannel", quality="720")
    recorder.clip(30, hook="clutch play!", vertical=True)
    recorder.stop()

Design notes:
- Segmented TS (not one growing mp4) is what makes "last N seconds" cheap:
  the newest file is still being written, everything older is final.
- ffmpeg runs with `-c copy` where possible, so recording costs no CPU.
- A watchdog thread re-resolves the URL (live manifests expire) and restarts
  ffmpeg on dropouts, continuing the segment numbering in the same session.
- Only the newest CLIP_STREAM_RETAIN_SEGS segments (default 240 = ~40 min)
  are kept; older ones are pruned automatically to bound disk usage.
- url == "testsrc" records a built-in test pattern (no network) so the whole
  path - record, peek, clip, package - can be exercised offline.
- PeakCut's ingest shape: one ffmpeg with two outputs - TS segments plus
  16 kHz mono s16le on stdout. Python drains the pipe (that drain is what
  keeps the second output alive) and computes one RMS dB value per second
  for the live loudness graph.
- start() is instant: the url resolves in a background thread ("connecting…"),
  and a spawn that never produced audio fails fast with the reason.
- One recorder per creator: get(creator_id) hands out instances; each records
  its own stream in parallel. Clips are stamped source.channel="creator:<id>".
"""
from __future__ import annotations

import datetime
import math
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import queue
from .portable import CREATE_NO_WINDOW
from .config import ROOT, load_config

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"
YTDLP = shutil.which("yt-dlp") or r"C:\ytdlp\yt-dlp.exe"

SEG_SECONDS = 10
SR = 16000                 # tap sample rate (PeakCut: 16 kHz mono s16le on stdout)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_QUALITY_FMT = {
    "best": "b/bv*+ba/b",
    "720": "bv*[height<=720]+ba/b[height<=720]/b",
    "480": "bv*[height<=480]+ba/b[height<=480]/b",
}

# 9:16 blur-pad: background = scaled+cropped+blurred copy, foreground = fit width
VF_VERTICAL = ("split[a][b];"
               "[a]scale=1080:1920:force_original_aspect_ratio=increase,"
               "crop=1080:1920,boxblur=24:4[bg];"
               "[b]scale=1080:-2[fg];"
               "[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p")


class RecorderError(RuntimeError):
    pass


def _probe_duration(path: Path) -> float | None:
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
            creationflags=CREATE_NO_WINDOW)
        return float(r.stdout.strip())
    except Exception:
        return None


class StreamRecorder:
    """One recording session at a time, guarded by an RLock.

    Each creator gets their own instance (see get()); the legacy "" instance
    is the quick/no-creator recorder.
    """

    def __init__(self, creator_id: str = "") -> None:
        self._lock = threading.RLock()
        self._creator = (creator_id or "").strip()
        self._proc: subprocess.Popen | None = None
        self._errfile = None
        self._want = False
        self._watchdog: threading.Thread | None = None
        self._url = ""
        self._quality = "best"
        self._title = ""
        self._session: Path | None = None
        self._started = 0.0
        self._clips: list[dict] = []
        self._last_error = ""
        self._restarts = 0
        self._spawn_t = 0.0
        self._tap_note = ""                          # levels availability note
        self._db: list[float] = []                   # loudness per second (PipeCut-style)
        self._got_audio = False

    # ------------------------------------------------------------------
    def _sessions_root(self) -> Path:
        d = ROOT / "media" / "streams"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _segments(self) -> list[Path]:
        if not self._session:
            return []
        return sorted(self._session.glob("seg_*.ts"))

    def _is_live(self) -> bool:
        return bool(self._want and self._proc and self._proc.poll() is None)

    # -- url resolution -------------------------------------------------
    def _resolve(self, url: str) -> list[str]:
        """Page URL -> direct media URL(s) via yt-dlp. Direct URLs pass through."""
        url = url.strip()
        if url == "testsrc":
            return ["testsrc"]
        if re.search(r"\.(m3u8|mp4|flv)(\?|$)", url):
            return [url]
        if "://" not in url:
            url = "https://" + url
        fmt = _QUALITY_FMT.get(self._quality, _QUALITY_FMT["best"])
        try:
            res = subprocess.run(
                [YTDLP, "-g", "-f", fmt, "--no-warnings", "--no-playlist", url],
                capture_output=True, text=True, timeout=120,
                creationflags=CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired as exc:
            raise RecorderError(f"yt-dlp timed out resolving {url}") from exc
        lines = [l.strip() for l in (res.stdout or "").splitlines() if l.strip()]
        if res.returncode != 0 or not lines:
            err = [l for l in (res.stderr or "").strip().splitlines() if l.strip()]
            raise RecorderError(f"yt-dlp could not resolve stream: {err[-1][:200] if err else 'no media url (is it live?)'}")
        return lines[:2]  # 1 = muxed; 2 = DASH video+audio

    def _fetch_title(self, url: str) -> str:
        """Best-effort stream title (shown on the creator card)."""
        if url == "testsrc":
            return "Test pattern"
        if re.search(r"\.(m3u8|mp4|flv)(\?|$)", url):
            return ""
        try:
            res = subprocess.run(
                [YTDLP, "--no-warnings", "--no-playlist", "--skip-download",
                 "--print", "title", url],
                capture_output=True, text=True, timeout=60,
                creationflags=CREATE_NO_WINDOW)
            if res.returncode == 0:
                return (res.stdout or "").strip().splitlines()[0][:120]
        except Exception:  # noqa: BLE001 - title is cosmetic
            pass
        return ""

    # -- ffmpeg ----------------------------------------------------------
    def _spawn(self, resolved: list[str], session: Path) -> None:
        """PeakCut's proven shape: one ffmpeg, two outputs - TS segments plus
        16 kHz mono s16le on stdout, read by Python for the loudness graph.
        (Second outputs starve unless the pipe is actively drained.)"""
        seg_pattern = session / "seg_%05d.ts"
        cmd = [FFMPEG, "-v", "error"]
        a_in = "0:a:0"
        if resolved == ["testsrc"]:
            a_in = "1:a:0"
            # lavfi inputs: pace to realtime, and encode (raw frames can't be
            # stream-copied into mpegts). No -reconnect (it's a network option).
            cmd += ["-re", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
                    "-re", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-c:a", "aac", "-b:a", "96k", "-g", "60", "-keyint_min", "60"]
        elif len(resolved) == 1:
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "10", "-user_agent", UA, "-i", resolved[0]]
        else:  # separate video + audio urls (yt-dlp DASH)
            a_in = "1:a:0"
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "10", "-user_agent", UA, "-i", resolved[0],
                    "-reconnect", "1", "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "10", "-user_agent", UA, "-i", resolved[1]]
        # -c copy streams the real stream's own codecs into the segments
        # (PeakCut); testsrc was given its encoders above.
        if resolved != ["testsrc"]:
            cmd += ["-c", "copy"]
        cmd += ["-fflags", "+genpts", "-f", "segment",
                "-segment_time", str(SEG_SECONDS), "-segment_format", "mpegts",
                "-reset_timestamps", "1",
                "-segment_start_number", str(len(self._segments())),
                str(seg_pattern),
                "-map", a_in, "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "pipe:1"]
        if self._errfile:
            self._errfile.close()
        self._errfile = open(session / "ffmpeg_log.txt", "ab")
        self._spawn_t = time.time()
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=self._errfile,
            cwd=session, creationflags=CREATE_NO_WINDOW)
        threading.Thread(target=self._read_levels, daemon=True,
                         name=f"levels-{self._creator}").start()

    def _read_levels(self) -> None:
        """Drain the s16le pipe: one second of audio per read -> one dB value.
        This active drain is what keeps the second output alive on ffmpeg 9."""
        proc = self._proc
        try:
            while True:
                data = proc.stdout.read(SR * 2)
                if not data:
                    break
                data = data[: len(data) // 2 * 2]
                self._got_audio = True
                n = len(data) // 2
                if n:
                    acc = 0.0
                    step = 8  # every 4th sample is plenty for a VU graph
                    cnt = 0
                    for i in range(0, len(data) - 1, step):
                        s = int.from_bytes(data[i:i + 2], "little", signed=True) / 32768.0
                        acc += s * s
                        cnt += 1
                    level = 20 * math.log10(math.sqrt(acc / cnt) + 1e-10)
                    with self._lock:
                        self._db.append(round(max(-100.0, min(0.0, level)), 1))
                        if len(self._db) > 300:   # 5 minutes is more than the graph shows
                            del self._db[:-300]
        except Exception:  # noqa: BLE001 - pipe closed on restart/stop
            pass

    def _watch(self) -> None:
        """Restart ffmpeg on dropouts (live manifests expire) - but only if
        audio ever flowed; a spawn that never produced anything is an error,
        not a retry (PeakCut's rule). Also refreshes preview.jpg every tick
        (~5 s) so the UI's peek is a plain file serve."""
        while True:
            time.sleep(5)
            with self._lock:
                if not self._want:
                    return
                if self._proc and self._proc.poll() is not None:
                    if not self._got_audio:
                        self._want = False
                        self._last_error = (self._last_error
                                            or "recording never started (no audio/video received)")
                        return
                    self._restarts += 1
                    if self._restarts > 12:
                        self._want = False
                        self._last_error = "stream ended (restart limit reached)"
                        return
                    try:
                        self._spawn(self._resolve(self._url), self._session)
                        self._last_error = ""
                    except Exception as exc:  # keep trying next tick
                        self._last_error = str(exc)
                    continue
                if self._is_live():
                    try:
                        self._update_preview()
                    except Exception:  # noqa: BLE001 - preview is cosmetic
                        pass

    def _update_preview(self) -> None:
        """One frame from near the end of the newest segment -> preview.jpg."""
        if not self._session:
            return
        segs = self._segments()
        if not segs:
            return
        out = self._session / "preview.jpg"
        tmp = self._session / "preview_tmp.jpg"
        subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
             "-sseof", "-2", "-i", str(segs[-1]), "-frames:v", "1",
             "-q:v", "3", str(tmp)],
            capture_output=True, timeout=20,
            creationflags=CREATE_NO_WINDOW)
        if tmp.exists():
            tmp.replace(out)

    # -- public api -------------------------------------------------------
    def start(self, url: str, quality: str = "best") -> dict:
        """Instant like PeakCut: returns at once, the url resolves in the
        background ('connecting…'), errors land in status().last_error."""
        with self._lock:
            if self._want:
                raise RecorderError("already recording - stop first")
            if not url.strip():
                raise RecorderError("stream url required")
            self._url = url.strip()
            self._quality = quality if quality in _QUALITY_FMT else "best"
            self._last_error, self._restarts, self._clips = "", 0, []
            self._db, self._got_audio = [], False
            self._session = self._sessions_root() / datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            (self._session / "clips").mkdir(parents=True)
            self._started = time.time()
            self._want = True
            self._watchdog = threading.Thread(target=self._watch, daemon=True)
            self._connect_thr = threading.Thread(target=self._connect, daemon=True)
            self._watchdog.start()
            self._connect_thr.start()
            return self.status()

    def _connect(self) -> None:
        """Resolve the url and spawn ffmpeg once. Never got audio -> fail
        fast with the reason (PeakCut's 'Could not start recording')."""
        try:
            resolved = self._resolve(self._url)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                if self._want and not self._got_audio:
                    self._want = False
                    self._last_error = f"could not start recording: {str(exc)[-160:]}"
            return
        with self._lock:
            if not self._want:
                return
            if not self._title:
                self._title = self._fetch_title(self._url)
            self._spawn(resolved, self._session)
            try:  # first frame right away; the watchdog keeps it fresh
                self._update_preview()
            except Exception:  # noqa: BLE001
                pass

    def stop(self) -> dict:
        with self._lock:
            self._want = False
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            if self._errfile:
                self._errfile.close()
                self._errfile = None
            # back to a clean idle (PeakCut-style): the page shows the
            # creator's player again; the last frame stays on disk for
            # last_preview(), the buffer session itself is not kept "live"
            self._session = None
            self._url, self._title, self._started = "", "", 0.0
            self._clips = []
            self._db = []
            return self.status()

    def status(self) -> dict:
        with self._lock:
            self._prune()
            segs = self._segments()
            rec = self._is_live()
            return {
                "recording": rec,
                "starting": bool(self._want and not rec),
                "stopped": not self._want and bool(self._session),
                "url": self._url,
                "creator": self._creator,
                "title": self._title,
                "session": self._session.name if self._session else "",
                "elapsed_sec": round(time.time() - self._started) if self._started else 0,
                "segments": len(segs),
                "buffer_min": round(len(segs) * SEG_SECONDS / 60, 1),
                "disk_mb": round(sum(s.stat().st_size for s in segs) / 1e6, 1),
                "clips": len(self._clips),
                "restarts": self._restarts,
                "last_error": self._last_error,
                "tap_note": self._tap_note,
            }

    def clip(self, seconds: int, hook: str = "", vertical: bool = True) -> dict:
        seconds = 30 if seconds <= 30 else 60  # Peakcut: two fixed lengths
        with self._lock:
            if not self._is_live():
                raise RecorderError("not recording")
            segs = self._segments()
            if not segs:
                raise RecorderError("buffer is empty - give it a few seconds")
            # walk newest -> oldest until covered; last segment may be partial
            picked: list[Path] = []
            total = 0.0
            for seg in reversed(segs):
                d = _probe_duration(seg) or SEG_SECONDS
                picked.insert(0, seg)
                total += d
                if total >= seconds:
                    break
            start_off = max(0.0, total - seconds)
            out = self._session / "clips" / f"clip_{datetime.datetime.now():%H%M%S}_{seconds}s.mp4"
            lst = self._session / "concat.txt"
            lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in picked), encoding="ascii")
            cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                   "-ss", f"{start_off:.3f}", "-f", "concat", "-safe", "0",
                   "-i", str(lst), "-t", str(seconds)]
            if vertical:
                cmd += ["-vf", VF_VERTICAL, "-c:v", "libx264", "-preset", "veryfast",
                        "-crf", "21", "-c:a", "aac", "-b:a", "128k"]
            else:
                cmd += ["-c", "copy", "-video_track_timescale", "90000"]
            cmd += ["-movflags", "+faststart", str(out)]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                                 creationflags=CREATE_NO_WINDOW)
            if res.returncode != 0 or not out.exists():
                raise RecorderError(f"ffmpeg clip failed: {(res.stderr or '')[-250:]}")
            dur = _probe_duration(out) or 0.0
            cfg = load_config()
            credit = ""
            platforms = cfg["platforms"]
            if self._creator:
                from . import creators as creators_mod
                c = creators_mod.get(self._creator)
                if c:
                    credit = c.get("credit_line", "")
                    if c.get("platforms"):
                        platforms = c["platforms"]
            meta = queue.new_package(
                out,
                hook=(hook.strip() or f"Live clip {round(dur or seconds)}s - {self._url}")[:120],
                source_url=self._url,
                source_channel=f"creator:{self._creator}" if self._creator else "live stream",
                source_title=self._title or f"live {self._quality} - {self._session.name}",
                credit=credit,
                pillar="clip",
                layout="vertical" if vertical else "raw",
                duration_sec=dur, rule_check="PASS",
                platforms=platforms,
                notes=f"streamrec last {seconds}s session={self._session.name}",
            )
            entry = {
                "pid": meta["id"], "seconds": seconds, "duration": dur,
                "vertical": vertical, "hook": meta["hook"],
                "media_url": f"/api/media/{meta['id']}",
                "created": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                "warn": "" if total >= seconds - 1 else f"only {total:.0f}s was buffered",
            }
            self._clips.insert(0, entry)
            return entry

    def clips(self) -> list[dict]:
        with self._lock:
            return list(self._clips)

    # -- live audio levels (loudness graph) --------------------------------
    # PeakCut's way: the s16le pipe is drained by _read_levels() (one dB per
    # second of audio, computed in Python); this just reads the ring.
    def audio_levels(self, seconds: int = 62) -> dict:
        """Loudness history for the graph: one dB value per second, oldest first."""
        with self._lock:
            rms = self._db[-max(5, min(seconds, 300)):]
            return {
                "recording": self._is_live(),
                "rms": rms,
                "peak": rms,
                "tap_note": self._tap_note,
            }

    def _prune(self) -> None:
        retain = int(os.environ.get("CLIP_STREAM_RETAIN_SEGS", "240"))
        segs = self._segments()
        for old in segs[:-retain]:
            try:
                old.unlink()
            except OSError:
                pass

    def peek(self) -> Path | None:
        """The cached preview frame (background-refreshed ~every 5 s while
        recording; the last frame stays available after stopping)."""
        with self._lock:
            if not self._session:
                return None
            out = self._session / "preview.jpg"
            return out if out.exists() else None


def creator_url(c: dict) -> str:
    """The url the recorder would use for this creator (twitch > kick > youtube)."""
    for key in ("twitch", "kick", "youtube"):
        v = (c.get(key) or "").strip()
        if v:
            return v if "://" in v else "https://" + v
    return ""


def last_preview() -> Path | None:
    """Most recent preview frame across all sessions - what the UI shows
    when nothing is recording (PeakCut-style: a preview even when idle)."""
    root = ROOT / "media" / "streams"
    if not root.exists():
        return None
    newest: tuple[float, Path] | None = None
    for f in root.glob("*/preview.jpg"):
        try:
            m = f.stat().st_mtime
        except OSError:
            continue
        if newest is None or m > newest[0]:
            newest = (m, f)
    return newest[1] if newest else None


recorder = StreamRecorder()

# ---- per-creator pool ----------------------------------------------------
# One recorder per creator id ("" = the legacy quick recorder). Server
# endpoints address them by ?cid= / {"creator": id}.
_pool: dict[str, StreamRecorder] = {}
_pool_lock = threading.Lock()


def get(creator_id: str = "") -> StreamRecorder:
    with _pool_lock:
        key = (creator_id or "").strip()
        if key not in _pool:
            _pool[key] = StreamRecorder(key)
        return _pool[key]


def all_recorders() -> dict[str, StreamRecorder]:
    return dict(_pool)
