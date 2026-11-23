"""Self-contained clip transformer for RGCIPZAH (no gtavi-claudecode repo needed).

Takes any downloaded video and re-renders it as a branded 1080x1920 edit:
  - vertical source  -> subtle 3.5% punch-in reframe (changes the framing)
  - landscape source -> 9:16 blur-background layout, centered
  - no on-video text (hook text overlay was removed by owner request)
  - RGCIPZAH wordmark bug (text only) centered at ~70% frame height
  - loudness normalized to -14 LUFS, H.264/AAC, <=58.5s (Shorts-eligible)

Only needs ffmpeg/ffprobe on PATH, so it runs identically on this PC and on
GitHub Actions runners.

Note on branding: this module adds the RGCIPZAH mark to clips WE re-render.
It is not a watermark-covering tool and must not be used to obscure the
original creator's watermark or credit.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .config import ROOT
from .logutil import log

MAX_OUT_SEC = 58.5
OUT_W, OUT_H = 1080, 1920
BUG_WIDTH = 340        # on-screen width of the wordmark bug
BUG_CENTER_Y = 0.70    # bug's vertical center as a fraction of frame height


def _probe(path: Path) -> dict:
    res = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=120)
    try:
        info = json.loads(res.stdout or "{}")
    except ValueError:
        return {}
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    try:
        dur = float((info.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    return {
        "w": int(v.get("width") or 0) if v else 0,
        "h": int(v.get("height") or 0) if v else 0,
        "dur": dur,
        "audio": bool(a),
    }


def render(src: Path, dst: Path, hook: str = "") -> Path | None:
    """Re-render src as a branded vertical clip. Returns dst or None.

    `hook` is accepted for call compatibility but no text is drawn on the
    video anymore (owner request: no on-video text at all)."""
    src = Path(src).resolve()   # ffmpeg runs with cwd=job dir; inputs must be absolute
    dst = Path(dst).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    info = _probe(src)
    if not info.get("w") or not info.get("h"):
        log("editclip: could not probe source video", level="error")
        return None

    bug = ROOT / "brand" / "wordmark-bug.png"
    if not bug.exists():
        bug = None  # edit still works, just unbranded

    # filter chain part 1: reframe to 1080x1920
    ratio = info["w"] / max(1, info["h"])
    if 0.45 <= ratio <= 0.75:  # already vertical (9:16-ish): punch-in reframe
        zw, zh = 1037, 1800  # ~3.5% crop-in, changes the framing
        base = (f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase:flags=bicubic,"
                f"crop={OUT_W}:{OUT_H},"
                f"crop={zw}:{zh}:(iw-{zw})/2:(ih-{zh})/2,"
                f"scale={OUT_W}:{OUT_H}:flags=bicubic")
    else:  # landscape/square: blur-background layout
        base = (f"split=2[bg][fg];"
                f"[bg]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase:flags=bicubic,"
                f"crop={OUT_W}:{OUT_H},boxblur=luma_radius=24:luma_power=2,eq=brightness=-0.06[bgb];"
                f"[fg]scale={OUT_W}:-2:flags=bicubic[fgs];"
                f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2")
    chain = f"[0:v]{base},fps=30[b0]"

    # part 2: brand bug overlay (input 1) - centered, ~70% frame height
    inputs = ["-i", str(src)]
    parts = [chain]
    last = "b0"
    if bug:
        inputs += ["-i", str(bug)]
        parts.append(f"[1:v]scale={BUG_WIDTH}:-1[bugm]")
        parts.append(f"[{last}][bugm]overlay=x=(W-w)/2:y=H*{BUG_CENTER_Y}-h/2[b1]")
        last = "b1"

    parts.append(f"[{last}]format=yuv420p[vout]")

    # audio: normalize when present, silence track when not (posters require audio)
    silent = "-f lavfi -i anullsrc=channel_layout=stereo:sample_rate=44100"
    if info.get("audio"):
        amap = ["-map", "0:a:0", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11"]
    else:
        inputs += silent.split()
        amap = ["-map", "2:a:0", "-shortest"]

    cmd = (["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *inputs,
            "-filter_complex", ";".join(parts), "-map", "[vout]", *amap,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
            "-movflags", "+faststart", "-t", str(MAX_OUT_SEC), str(dst)])
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=1800,
                             cwd=str(dst.parent), encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        log("editclip: ffmpeg timed out", level="error")
        return None
    if res.returncode != 0 or not dst.exists() or dst.stat().st_size < 20_000:
        tail = (res.stderr or "").strip().splitlines()[-1][:220] if (res.stderr or "").strip() else "no output"
        log(f"editclip: render failed: {tail}", level="error")
        return None
    out = _probe(dst)
    if not out.get("w") or not out.get("h"):
        log("editclip: rendered file does not probe", level="error")
        return None
    log(f"editclip: {dst.name} {out['w']}x{out['h']} {out['dur']:.1f}s "
        f"({dst.stat().st_size // 1024} KB)")
    return dst
