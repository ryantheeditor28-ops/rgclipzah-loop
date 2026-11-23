"""Optional fast transcription backend: whisper.cpp (BLAS build) on CPU.

TRADEOFF (measured on this machine, see PATCHES.md):
  - whisper.cpp text accuracy is excellent.
  - Token timestamps from whisper.cpp (without DTW models, which are no
    longer published) are NOT reliable, so word times here are evenly spread
    within silero-VAD speech regions, weighted by word length.
    Measured error vs faster-whisper: mean 0.6 s, worst ~3.6 s on continuous
    synthetic speech. Real stream audio (natural pauses -> many VAD splits)
    lands closer to 0.2-0.3 s.

Because the clip pipeline burns word-by-word karaoke captions from
transcript.json, the DEFAULT remains faster-whisper (frame-accurate word
timestamps). Enable this fast path explicitly by setting
CLIP_TRANSCRIBER=whisper_cpp in the environment — worth it for
text-priority jobs (secrets scanning, question mining, research) or when
transcription time matters more than caption sync.

The gtavi-claudecode moments.py honors these env vars (see PATCHES.md):
  CLIP_TRANSCRIBER=whisper_cpp
  CLIP_AGENCY_DIR=<clip-agency path>
Fallback to faster-whisper is automatic on any failure.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from .config import ROOT
from .logutil import log

BIN = ROOT / "bin" / "Release" / "whisper-cli.exe"
MODEL = ROOT / "models" / "ggml-small-q5_1.bin"
VAD = ROOT / "models" / "ggml-silero-v5.1.2.bin"


def is_available() -> bool:
    return BIN.exists() and MODEL.exists()


def _words_from_vad_segments(data: dict) -> list[dict]:
    """Spread words evenly inside each VAD speech region, length-weighted."""
    words: list[dict] = []
    for seg in data.get("transcription", []):
        t0 = seg.get("offsets", {}).get("from", 0) / 1000
        t1 = seg.get("offsets", {}).get("to", 0) / 1000
        tokens = (seg.get("text") or "").strip().split()
        if not tokens or t1 <= t0:
            continue
        weights = [max(1, len(t.strip(".,!?;:"))) for t in tokens]
        total = float(sum(weights))
        cur = t0
        for tok, w in zip(tokens, weights):
            dur = (t1 - t0) * (w / total)
            words.append({"w": tok, "s": round(cur, 2), "e": round(cur + dur, 2)})
            cur += dur
    return words


def transcribe_wav(wav: Path, *, timeout: int = 7200) -> list[dict]:
    """Transcribe a 16 kHz mono WAV. Accurate text, approximate word timing."""
    if not is_available():
        raise FileNotFoundError(
            "whisper.cpp not installed (need bin/Release/whisper-cli.exe and "
            "models/ggml-small-q5_1.bin)")
    wav = Path(wav)
    tmp = Path(tempfile.mkdtemp(prefix="wsp_"))
    prefix = tmp / "tr"
    cmd = [str(BIN), "-m", str(MODEL), "-f", str(wav), "-oj", "-of", str(prefix), "-nt"]
    if VAD.exists():
        cmd += ["--vad", "-vm", str(VAD), "-vt", "0.5"]
    res = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=timeout)
    jf = prefix.with_suffix(".json")
    if not jf.exists():
        raise RuntimeError(f"whisper.cpp produced no JSON ({res.stderr[-200:]})")
    data = json.loads(jf.read_text(encoding="utf-8"))
    words = _words_from_vad_segments(data)
    log(f"transcribe: whisper.cpp -> {len(words)} words from {wav.name} (approximate timing)")
    return words
