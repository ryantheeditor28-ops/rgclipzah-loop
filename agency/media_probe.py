"""Tiny ffprobe helpers shared by queue/produce/monitor."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


def _ffprobe() -> str:
    found = shutil.which("ffprobe")
    if not found:
        raise RuntimeError("ffprobe not found on PATH")
    return found


def probe_duration(video: Path) -> float:
    out = subprocess.run(
        [_ffprobe(), "-v", "error", "-show_entries", "format=duration", "-of", "json", str(video)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    try:
        return float(json.loads(out.stdout)["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError):
        return 0.0


def probe_streams(video: Path) -> dict:
    out = subprocess.run(
        [_ffprobe(), "-v", "error", "-show_entries",
         "stream=codec_name,codec_type,width,height,r_frame_rate", "-of", "json", str(video)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return {}
