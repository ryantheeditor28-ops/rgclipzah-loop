"""Cross-platform subprocess bits.

Windows: CREATE_NO_WINDOW keeps ffmpeg/yt-dlp consoles from flashing.
POSIX: nonzero creationflags raise ValueError, so pass 0 (the flag is a no-op
there - systemd already runs us without a console).
"""
from __future__ import annotations

import sys

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def ytdlp_cookie_args() -> list[str]:
    """yt-dlp auth for platforms that need a session (reddit videos, x).

    Headless servers have no browser profile: set YTDL_COOKIES_FILE to a
    cookies.txt exported from your desktop browser and this is used instead of
    reading the local Firefox profile.
    """
    from .config import env

    cookie_file = env("YTDL_COOKIES_FILE")
    if cookie_file:
        return ["--cookies", cookie_file]
    return ["--cookies-from-browser=firefox"]
