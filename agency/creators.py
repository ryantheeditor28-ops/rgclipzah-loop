"""Creators: the streamers the agency clips from (PeakCut-style).

One JSON file, creators.json, holds every creator:

    {
      "id": "kaicenat",
      "name": "KaiCenat",
      "color": "#ff8a2a",            # sidebar avatar + accent
      "twitch": "twitch.tv/kaicenat",
      "kick": "",
      "youtube": "",
      "auto_record": false,          # record-when-live watcher (roadmap)
      "ai_check_min": 5,             # how often the AI would review the stream
      "clip_min_sec": 30,
      "clip_max_sec": 60,
      "platforms": ["youtube", "tiktok", "instagram"],
      "credit_line": "Clipped from @kaicenat",
      "created": "2026-11-21T05:00:00+08:00"
    }

Clips cut from a creator's recording are stamped
source.channel = "creator:<id>" so All videos / Activity can filter by
creator with no extra state.
"""
from __future__ import annotations

import datetime
import json
import re
import threading
from pathlib import Path

from .config import ROOT

_PATH = ROOT / "creators.json"
_LOCK = threading.RLock()

PALETTE = [
    "#ff8a2a", "#ff2d78", "#a03cff", "#4493f8", "#3fd06c",
    "#e3b341", "#ff5c5c", "#2ad4b4", "#c4f52a", "#f52ad0",
]

FIELDS = ("name", "color", "twitch", "kick", "youtube", "auto_record",
          "ai_check_min", "clip_min_sec", "clip_max_sec", "platforms",
          "credit_line")

DEFAULTS: dict = {
    "color": "",
    "twitch": "", "kick": "", "youtube": "",
    "auto_record": False,
    "ai_check_min": 5,
    "clip_min_sec": 30,
    "clip_max_sec": 60,
    "platforms": [],
    "credit_line": "",
}


def _path() -> Path:
    return _PATH


def load() -> list[dict]:
    with _LOCK:
        if not _PATH.exists():
            return []
        try:
            data = json.loads(_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []


def save(items: list[dict]) -> None:
    with _LOCK:
        _PATH.write_text(json.dumps(items, indent=2, ensure_ascii=False),
                         encoding="utf-8")


def get(cid: str) -> dict | None:
    cid = (cid or "").strip()
    return next((c for c in load() if c.get("id") == cid), None)


def _slug(name: str, existing: list[dict]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (name or "creator").lower()).strip("-") or "creator"
    cid, n = base, 2
    while any(c.get("id") == cid for c in existing):
        cid, n = f"{base}-{n}", n + 1
    return cid


def new(name: str, **fields) -> dict:
    name = (name or "").strip() or "Unnamed"
    with _LOCK:
        items = load()
        cid = _slug(name, items)
        used = {c.get("color") for c in items}
        color = fields.pop("color", "") or next(
            (p for p in PALETTE if p not in used), PALETTE[len(items) % len(PALETTE)])
        creator = {
            "id": cid,
            "name": name,
            "color": color,
            **{k: fields.get(k, DEFAULTS.get(k)) for k in FIELDS if k not in ("name", "color")},
            "created": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        items.append(creator)
        save(items)
        return creator


def update(cid: str, fields: dict) -> dict:
    with _LOCK:
        items = load()
        for c in items:
            if c.get("id") == cid:
                for k in FIELDS:
                    if k in fields:
                        c[k] = fields[k]
                save(items)
                return c
        raise KeyError(f"no creator {cid}")


def delete(cid: str) -> None:
    with _LOCK:
        save([c for c in load() if c.get("id") != cid])
