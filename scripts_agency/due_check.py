"""Cheap "is a post due?" gate for the GitHub Actions loop.

GitHub's cron scheduler skips slots under load, so the workflow knocks every
30 minutes instead of every 2 hours (see .github/workflows/loop.yml). This
script answers the only question worth asking before we burn runner minutes
on apt-get, the POT provider and a video download:

    has it been at least the configured post gap since the last post?

Stdlib only, on purpose: it has to run before `pip install`.

Outputs (to $GITHUB_OUTPUT when running under Actions):
    due=true|false
    reason=<short human explanation>

Usage:
    python scripts_agency/due_check.py
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "repost_state.json"


def last_post(state: dict) -> datetime.datetime | None:
    """When the engine last produced a post.

    Items are stamped with the local offset of whatever machine posted them
    (the Actions runner writes UTC, this PC writes +08:00), so the newest post
    is the newest *instant* - not the biggest ISO string.
    """
    best: datetime.datetime | None = None
    for item in state.get("items", []):
        if item.get("status") != "posted":
            continue
        raw = item.get("at") or ""
        if not raw:
            continue
        try:
            when = datetime.datetime.fromisoformat(raw)
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.astimezone()
        if best is None or when > best:
            best = when
    return best


def _emit(due: bool, reason: str) -> None:
    print(f"due={str(due).lower()}  reason={reason}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"due={str(due).lower()}\n")
            fh.write(f"reason={reason}\n")


def main() -> int:
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - no state yet means nothing was posted
        state = {}
    cfg = state.get("config") or {}

    if not cfg.get("enabled", True):
        _emit(False, "engine disabled in repost_state.json")
        return 0

    try:
        gap_h = float(cfg.get("min_post_gap_h", 2) or 0)
    except (TypeError, ValueError):
        gap_h = 2.0
    if gap_h <= 0:
        _emit(True, "no post gap configured - always due")
        return 0

    last = last_post(state)
    if last is None:
        _emit(True, "nothing posted yet - due")
        return 0

    now = datetime.datetime.now(datetime.timezone.utc)
    waited_h = (now - last).total_seconds() / 3600.0
    if waited_h >= gap_h:
        _emit(True, f"last post {waited_h:.2f}h ago, gap {gap_h:g}h - due")
        return 0

    remaining = gap_h - waited_h
    _emit(False, f"last post {waited_h:.2f}h ago, gap {gap_h:g}h - "
                 f"{remaining * 60:.0f} min left")
    return 0


if __name__ == "__main__":
    sys.exit(main())
