"""Logging: console + logs/agency.log + human-readable log.md append."""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

from .config import ROOT

LOG_FILE = ROOT / "logs" / "agency.log"
HUMAN_LOG = ROOT / "log.md"


def log(msg: str, *, level: str = "info") -> None:
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] [{level}] {msg}"
    print(line, file=sys.stderr if level == "error" else sys.stdout)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def human_log(msg: str) -> None:
    """Append one line to log.md (the page's activity journal)."""
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        with HUMAN_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"- {stamp} — {msg}\n")
    except OSError:
        pass
