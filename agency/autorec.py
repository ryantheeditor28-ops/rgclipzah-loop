"""Auto-record watcher: records a creator automatically when they go live.

Every AUTO_SEC the watcher looks at creators with auto_record on, asks the
platform whether their channel is live (Kick: public JSON api; Twitch and
YouTube: yt-dlp), and starts the recorder for the ones that are. Nothing is
recorded while a channel is offline; a recording it starts is exactly the
same as a manual one (same buffer, clips, chat graphs).
"""
from __future__ import annotations

import os
import subprocess
import time

import requests

from . import chatmon, creators, streamrec
from .logutil import log

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
from .portable import CREATE_NO_WINDOW


def _check_live(url: str) -> tuple[bool, str]:
    """(is_live, how) - cheap checks first, yt-dlp for twitch/youtube."""
    if "kick.com/" in url:
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        try:
            r = requests.get(f"https://kick.com/api/v2/channels/{slug}",
                             headers={"User-Agent": UA, "Accept": "application/json"},
                             timeout=20)
            if r.status_code == 200:
                return bool((r.json() or {}).get("livestream")), "kick api"
        except Exception:  # noqa: BLE001
            pass
        return False, "kick check failed"
    # twitch + youtube: yt-dlp only yields an id when the channel is live
    try:
        res = subprocess.run(
            [streamrec.YTDLP, "--no-warnings", "--no-playlist", "--skip-download",
             "--print", "id", url],
            capture_output=True, text=True, timeout=90,
            creationflags=CREATE_NO_WINDOW)
        out = (res.stdout or "").strip().splitlines()
        if res.returncode == 0 and out and out[0].strip():
            return True, "yt-dlp"
    except Exception:  # noqa: BLE001
        pass
    return False, "not live"


def tick() -> list[str]:
    """One watch pass; returns the ids of creators it started recording."""
    started: list[str] = []
    for c in creators.load():
        if not c.get("auto_record"):
            continue
        st = streamrec.get(c["id"]).status()
        if st["recording"] or st["starting"]:
            continue
        url = streamrec.creator_url(c)
        if not url:
            continue
        live, how = _check_live(url)
        if not live:
            continue
        try:
            streamrec.get(c["id"]).start(url)
            chatmon.chatmon.start(c["id"], url)
            log(f"auto-record: {c['name']} is live ({how}) — recording started")
            started.append(c["id"])
        except Exception as exc:  # noqa: BLE001
            log(f"auto-record: could not start {c['name']}: {exc}")
    return started


def loop(interval: float | None = None) -> None:
    interval = float(interval or os.environ.get("CLIP_AUTOREC_SEC", "180"))
    log(f"auto-record watcher on — checking every {int(interval)}s")
    while True:
        time.sleep(interval)
        try:
            tick()
        except Exception as exc:  # noqa: BLE001
            log(f"auto-record watcher error: {exc}")
