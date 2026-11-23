"""Live watch: record a stream, listen to the TOPIC, cut pre-rolled clips.

Stage 1 of the live-watcher. Pipeline per watch session:

    Recorder (gtavi live.py)  60s .ts segments -> jobs/live-<name>/live/
        |
        | every interval: concat the freshest completed segments -> 16k wav
        v
    transcribe_wav (whisper.cpp + silero VAD) -> rolling word stream
        |
        v
    topics.score_window  ->  verdict {score, topic_type, hook, moment}
        |                       topic is the PRIMARY signal; loudness a bonus
        v
    raw cut [moment - preroll, moment + tail]  (-c copy from the buffer)
        |  -> jobs/<live-job>/  candidates.json + transcript.json
        v
    cut.py --layout blur --hook  ->  rendered 1080x1920 clip
        |
    check.py (rule-check)  ->  PASS only
        v
    queue.new_package + captions  ->  outbox/ (pending_approval gate)

Nothing posts from here — packages wait for the operator like every other.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from .config import ROOT, ensure_dirs, load_config, repo_dir
from .logutil import log
from .research import _run_repo
from .transcribe import transcribe_wav
from . import queue, seo, topics


def _gtavi_scripts() -> Path:
    return repo_dir() / "scripts"


def _import_recorder():
    s = str(_gtavi_scripts())
    if s not in sys.path:
        sys.path.insert(0, s)
    import live as gtavi_live  # gtavi-claudecode/scripts/live.py

    return gtavi_live


# ---------------------------------------------------------------- helpers

def _ytdlp_meta(url: str) -> dict:
    """Best-effort stream metadata (title/channel) for package credits."""
    try:
        res = subprocess.run(
            ["yt-dlp", "--skip-download", "--no-warnings",
             "--print", "%(webpage_url)s|%(title)s|%(channel)s|%(uploader)s", url],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120,
        )
        parts = (res.stdout or "").strip().split("|")
        if len(parts) >= 3:
            return {
                "webpage_url": parts[0] or url,
                "title": parts[1],
                "channel": parts[2] or (parts[3] if len(parts) > 3 else ""),
            }
    except Exception:  # noqa: BLE001 - metadata is best-effort only
        pass
    return {"webpage_url": url, "title": "", "channel": ""}


def _ffmpeg(*args: str, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(["ffmpeg", "-y", "-v", "error", *args],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def _pick_segments(comp: list[tuple], t0: float, t1: float) -> list[tuple]:
    return [s for s in comp if s[2] > t0 and s[1] < t1]


def _concat_list(pick: list[tuple], lst_path: Path) -> None:
    lst_path.write_text(
        "".join(f"file '{s[0].resolve().as_posix()}'\n" for s in pick),
        encoding="utf-8",
    )


def _window_wav(comp: list[tuple], t0: float, t1: float, out_wav: Path) -> Path | None:
    pick = _pick_segments(comp, t0, t1)
    if not pick:
        return None
    lst = out_wav.with_suffix(".txt")
    _concat_list(pick, lst)
    res = _ffmpeg("-f", "concat", "-safe", "0", "-i", str(lst),
                  "-ss", f"{max(0.0, t0 - pick[0][1]):.2f}",
                  "-t", f"{t1 - t0:.2f}",
                  "-vn", "-ac", "1", "-ar", "16000", str(out_wav))
    lst.unlink(missing_ok=True)
    if res.returncode != 0 or not out_wav.exists() or out_wav.stat().st_size < 44:
        return None
    return out_wav


def _loudness_bonus(wav: Path) -> float:
    """0..1.5 bonus from the window's peak volume (shouting adds punch)."""
    try:
        res = subprocess.run(
            ["ffmpeg", "-i", str(wav), "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        m = re.search(r"max_volume:\s*(-?[\d.]+)\s*dB", res.stderr or "")
        if m:
            db = float(m.group(1))
            return max(0.0, min(1.5, (db + 12) / 10))
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def _cut_raw(comp: list[tuple], t0: float, t1: float, out_mp4: Path) -> Path | None:
    pick = _pick_segments(comp, t0, t1)
    if not pick:
        return None
    lst = out_mp4.with_suffix(".txt")
    _concat_list(pick, lst)
    res = _ffmpeg("-f", "concat", "-safe", "0", "-i", str(lst),
                  "-ss", f"{max(0.0, t0 - pick[0][1]):.2f}",
                  "-t", f"{t1 - t0:.2f}",
                  "-c", "copy", "-avoid_negative_ts", "make_zero",
                  "-movflags", "+faststart", str(out_mp4))
    lst.unlink(missing_ok=True)
    if res.returncode != 0 or not out_mp4.exists() or out_mp4.stat().st_size < 10_000:
        return None
    return out_mp4


def _prune(comp: list[tuple], keep_after: float) -> None:
    for f, _s, e, _mt, done in comp:
        if done and e < keep_after:
            f.unlink(missing_ok=True)


# ---------------------------------------------------------------- watch

DEFAULTS = {
    "preroll_s": 20,          # cause before the moment (operator spec: ~20s)
    "tail_s": 22,             # payoff after the moment
    "max_clip_s": 58,         # rule-check ceiling (15-60 with headroom)
    "buffer_keep_min": 12,    # segments on disk
    "window_min": 3,          # transcript window length
    "interval_s": 75,         # judge cadence
    "overlap_s": 10,          # re-transcribe overlap between cycles
    "score_threshold": 3.0,   # calibrated on the operator's references
    "cooldown_s": 420,        # min seconds between clips
    "max_clips_per_watch": 3,
    "layout": "blur",
}


def watch(
    url: str,
    *,
    minutes: float = 30,
    max_clips: int | None = None,
    preroll_s: float | None = None,
    tail_s: float | None = None,
    interval_s: float | None = None,
    score_threshold: float | None = None,
    dry: bool = False,
    name: str = "",
    cfg: dict | None = None,
) -> list[dict]:
    """Watch one live stream and land topic-verified clips in the outbox."""
    cfg = cfg or load_config()
    lc = {**DEFAULTS, **(cfg.get("live") or {})}
    preroll_s = lc["preroll_s"] if preroll_s is None else preroll_s
    tail_s = lc["tail_s"] if tail_s is None else tail_s
    interval_s = lc["interval_s"] if interval_s is None else interval_s
    threshold = lc["score_threshold"] if score_threshold is None else score_threshold
    max_clips = lc["max_clips_per_watch"] if max_clips is None else max_clips

    ensure_dirs()
    slug = re.sub(r"[^a-z0-9]+", "-", (name or url.split("/")[-1] or "live").lower()).strip("-")[:24]
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    job = ROOT / "jobs" / f"live-{slug}-{stamp}"
    ld = job / "live"
    ld.mkdir(parents=True, exist_ok=True)

    log(f"live: metadata for {url}")
    meta = _ytdlp_meta(url)
    channel = meta.get("channel") or slug
    log(f"live: watching {channel} ({meta.get('title', '')[:70]}) for {minutes:g} min")

    gtavi = _import_recorder()
    try:
        rec = gtavi_live.Recorder(ld, url, minutes)
    except SystemExit as exc:  # live.py sys.exit()s when the URL is not live
        log(f"live: recorder refused to start: {exc}", level="error")
        return []

    log(f"live: recording -> {ld}")
    rolling: list[dict] = []          # word dicts in stream time
    covered_until = 0.0               # stream time already transcribed
    seen_fps: list[tuple] = []
    last_clip_at = 0.0
    made: list[dict] = []
    window = lc["window_min"] * 60
    end_at = time.time() + minutes * 60 + 60

    try:
        while time.time() < end_at and rec.alive() and len(made) < max_clips:
            time.sleep(min(interval_s, 10))
            if time.time() >= end_at or len(made) >= max_clips:
                break
            # sleep the remainder of the interval in small steps
            for _ in range(int(interval_s // 10)):
                if time.time() >= end_at or not rec.alive() or len(made) >= max_clips:
                    break
                time.sleep(10)

            segs = rec.segments()
            comp = [s for s in segs if s[4]]
            if not comp:
                log("live: no completed segments yet")
                continue
            win_end = comp[-1][2]
            win_start = max(0.0, covered_until - lc["overlap_s"])
            if win_end - win_start < window * 0.5:
                continue  # not enough fresh audio to judge

            wav = _window_wav(comp, win_start, win_end, job / f"win_{int(win_end)}.wav")
            if wav is None:
                log("live: window wav extraction failed")
                continue
            bonus = _loudness_bonus(wav)
            try:
                words = transcribe_wav(wav)
            except Exception as exc:  # noqa: BLE001
                log(f"live: transcription failed: {exc}", level="error")
                wav.unlink(missing_ok=True)
                continue
            wav.unlink(missing_ok=True)
            new_words = [
                {**w, "s": round(w["s"] + win_start, 2), "e": round(w["e"] + win_start, 2)}
                for w in words if w["s"] + win_start >= covered_until - lc["overlap_s"]
            ]
            rolling.extend(new_words)
            covered_until = win_end
            log(f"live: transcript now {len(rolling)} words (stream t={win_end:.0f}s)")

            # judge the freshest full window
            judge_from = max(0.0, win_end - window)
            window_words = [w for w in rolling if w["s"] >= judge_from]
            verdict = topics.score_window(window_words, loudness_bonus=bonus)
            if verdict is None:
                log("live: no clippable topic in window")
            else:
                fp = topics.dedup_fingerprint(window_words)
                dup = any(topics.similar(fp, old) >= 0.6 for old in seen_fps)
                cooled = time.time() - last_clip_at >= lc["cooldown_s"]
                log(f"live: verdict {verdict['topic_type']} score={verdict['score']} "
                    f"threshold={threshold} dup={dup} cooled={cooled} "
                    f"hook={verdict['hook']!r}")
                if verdict["score"] >= threshold and not dup and cooled:
                    seen_fps.append(fp)
                    last_clip_at = time.time()
                    if dry:
                        log("live: DRY - would cut here")
                        continue
                    pkg = _make_package(
                        job, comp, verdict, rolling, url=url, channel=channel,
                        meta=meta, cfg=cfg, lc=lc, preroll_s=preroll_s, tail_s=tail_s,
                    )
                    if pkg:
                        made.append(pkg)
                        log(f"live: package {pkg['id']} ready ({pkg['duration_sec']}s, "
                            f"{verdict['topic_type']})")
            _prune(comp, win_end - lc["buffer_keep_min"] * 60)
    finally:
        rec.stop()
        (job / "live_session.json").write_text(json.dumps({
            "url": url, "channel": channel, "title": meta.get("title", ""),
            "clips": len(made), "ended": datetime.now().isoformat(timespec="seconds"),
        }, indent=1), encoding="utf-8")
    log(f"live: session done, {len(made)} clip(s)")
    return made


def _make_package(job, comp, verdict, rolling, *, url, channel, meta, cfg, lc,
                  preroll_s, tail_s) -> dict | None:
    """Cut the pre-rolled window, render it, rule-check it, package it."""
    t_moment = verdict["moment"]
    t0 = max(0.0, t_moment - preroll_s)
    win_end = comp[-1][2]
    t1 = min(win_end - 0.5, t_moment + tail_s, t0 + lc["max_clip_s"])
    if t1 - t0 < 12:
        log("live: window too short to cut", level="error")
        return None

    raw = _cut_raw(comp, t0, t1, job / f"raw_{int(t_moment)}.mp4")
    if raw is None:
        log("live: raw cut failed", level="error")
        return None
    duration = t1 - t0

    clip_job = job / f"clip_{int(t_moment)}"
    (clip_job / "edits").mkdir(parents=True, exist_ok=True)
    src = clip_job / "source.mp4"
    raw.replace(src)
    (clip_job / "candidates.json").write_text(json.dumps({
        "source": str(src),
        "info": {"webpage_url": url, "title": meta.get("title", ""),
                 "channel": channel, "uploader": channel},
        "candidates": [{"n": 1, "start": 0.0, "end": round(duration, 2),
                        "score": verdict["score"], "peak": None,
                        "why": [verdict["quote"]]}],
    }, indent=1), encoding="utf-8")
    clip_words = [
        {"w": w["w"], "s": round(w["s"] - t0, 2), "e": round(w["e"] - t0, 2)}
        for w in rolling if t0 <= w["s"] < t1
    ]
    (clip_job / "transcript.json").write_text(json.dumps(clip_words), encoding="utf-8")

    res = _run_repo("cut.py", [f"jobs/{clip_job.name}", "--pick", "1",
                               "--layout", lc["layout"], "--hook", verdict["hook"]],
                    timeout=3600)
    clip = clip_job / "edits" / "clip_1.mp4"
    if res.returncode != 0 or not clip.exists():
        log(f"live: cut.py failed: {(res.stderr or '')[-300:]}", level="error")
        return None

    chk = _run_repo("check.py", [str(clip), "--campaign", "campaigns.md"], timeout=600)
    report = (chk.stdout or "") + (chk.stderr or "")
    if chk.returncode != 0 or "FAIL" in report:
        log(f"live: rule-check FAIL: {report[-300:]}", level="error")
        return None

    packaged = queue.new_package(
        clip,
        hook=verdict["hook"],
        source_url=meta.get("webpage_url", url),
        source_channel=channel,
        source_title=meta.get("title", ""),
        credit=f"@{channel}" if channel else "",
        pillar="clip",
        layout=lc["layout"],
        rule_check="PASS",
        platforms=cfg["platforms"],
        notes=f"live topic clip ({verdict['topic_type']}, score {verdict['score']})",
    )
    captions = seo.fill_captions(packaged, cfg=cfg)
    for p, cap in captions.items():
        queue.set_caption(packaged, p, cap)
    return queue.load_meta(packaged)
