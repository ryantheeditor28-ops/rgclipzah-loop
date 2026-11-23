"""RGClipzah repost loop - headless entry (GitHub Actions or any server).

Runs ONE repost cycle (discover -> download -> EDIT -> package -> auto-post)
and prints the summary. Unlike grand6-loop, editing happens here too: the
bundled agency.editclip re-renders every grab as a branded 1080x1920 edit,
so what gets posted is our cut, not a raw rip.

All credentials arrive as environment variables (GitHub Secrets); state lives
in repost_state.json, which the workflow commits back after every run so
dedupe + daily caps + the anti-spam throttle persist.

Usage:
    python run_loop.py              # one full cycle over every enabled source
    python run_loop.py --discover   # list what each source sees, post nothing
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from agency import repost  # noqa: E402
from agency.logutil import log  # noqa: E402


def main() -> int:
    cookies = ROOT / "cookies.txt"
    if cookies.exists():
        os.environ["YTDL_COOKIES_FILE"] = str(cookies)
    elif os.environ.get("RGCLIP_LOOP") == "1":
        # headless with no cookie file: anonymous requests instead of the
        # firefox-profile fallback (there is no browser here)
        repost.ytdlp_cookie_args = lambda: []  # type: ignore[assignment]

    if "--discover" in sys.argv:
        cfg = repost.normalize_config(repost.load_state()["config"])
        for src in cfg["sources"]:
            if not src.get("enabled", True):
                print(f"  [disabled] {src['kind']} {src['ident']}")
                continue
            try:
                found = repost.discover(src, cfg)
                print(f"  {src['kind']} {src['ident']}: {len(found)} fresh-listing item(s)")
                for it in found[:3]:
                    print(f"     - {it['title'][:60]}")
            except Exception as exc:  # noqa: BLE001
                print(f"  {src['kind']} {src['ident']}: LIST FAILED - {exc}")
        return 0

    # safety switch: the dashboard/watcher toggle also gates the cloud loop
    if not repost.load_state()["config"].get("enabled"):
        log("loop: disabled in dashboard state - skipping cycle (enable it to go live)")
        return 0

    res = repost.run_cycle()
    log(f"loop: {res.get('summary', res)}")
    if res.get("failures"):
        for f in res["failures"]:
            log(f"loop failure: {f}", level="error")
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
