"""Discovery dry-run for one source: list what it would see, post nothing.

Usage:
    python scripts_agency/check_source.py <ident-or-url>

Runs the same discovery the live loop uses, but only reports. It does not
download, edit or post.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agency import repost  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    needle = sys.argv[1].strip()

    cfg = repost.normalize_config(repost.load_state()["config"])
    matches = [s for s in cfg["sources"]
               if needle.lower() in s["ident"].lower() or needle.lower() in s["url"].lower()]
    if not matches:
        print(f"no source matches {needle!r}")
        return 1

    for src in matches:
        print(f"=== {src['kind']} {src['ident']}  ({src['url']}) ===")
        try:
            found = repost.discover(src, cfg)
        except Exception as exc:  # noqa: BLE001
            print(f"  discovery FAILED: {exc!r}")
            continue
        print(f"  found {len(found)} item(s)")
        for item in found[:8]:
            print(f"    - {item.get('key')}  score={item.get('score')}  "
                  f"{item.get('title', '')[:70]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
