"""Cross-platform subprocess bits.

Windows: CREATE_NO_WINDOW keeps ffmpeg/yt-dlp consoles from flashing.
POSIX: nonzero creationflags raise ValueError, so pass 0 (the flag is a no-op
there - systemd already runs us without a console).
"""
from __future__ import annotations

import sys

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def ytdlp_cmd() -> list[str]:
    """How to invoke yt-dlp.

    Always `python -m yt_dlp`, never the `yt-dlp` executable: the standalone
    binary cannot load pip-installed plugins (the bgutil PO-token provider
    lives in site-packages/yt_dlp_plugins), so it silently sees no POT
    provider and every YouTube download fails with "Requested format is not
    available".
    """
    return [sys.executable, "-m", "yt_dlp"]


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


def ytdlp_pot_args() -> list[str]:
    """Extra yt-dlp args needed to get a proof-of-origin token.

    YouTube puts logged-in accounts on SABR-only streaming: without a PO
    token the extractor only sees storyboard images, so downloads fail with
    "Requested format is not available". The bgutil POT provider fixes that.

    RGCLIP_POT_URL is set by the workflow (local: http://127.0.0.1:4416).
    RGCLIP_EJS=1 turns on the JS challenge solver (node + yt-dlp-ejs), which
    the web client needs for the "n" signature parameter.
    """
    from .config import env

    args: list[str] = []
    pot_url = env("RGCLIP_POT_URL")
    if pot_url:
        args += ["--extractor-args", f"youtubepot-bgutilhttp:base_url={pot_url}"]
    if env("RGCLIP_EJS") == "1":
        args += ["--js-runtimes", "node", "--remote-components", "ejs:github"]
    return args
