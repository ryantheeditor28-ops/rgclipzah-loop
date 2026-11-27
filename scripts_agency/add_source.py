"""Add a source channel to the repost source list.

Usage:
    python scripts_agency/add_source.py <profile-url> [--disable] [--per-run N]

The source id is derived the same way agency/repost.py derives it:
sha1("<kind>:<ident>")[:8]. Re-running with a URL that is already in the
list updates that entry instead of creating a duplicate.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agency.repost import parse_source  # noqa: E402

STATE = ROOT / "repost_state.json"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    url = sys.argv[1].strip()
    enabled = "--disable" not in sys.argv
    per_run = 2
    if "--per-run" in sys.argv:
        per_run = int(sys.argv[sys.argv.index("--per-run") + 1])

    kind, ident = parse_source(url)
    if not kind:
        print(f"could not work out a source type from {url!r}")
        return 1

    sid = hashlib.sha1(f"{kind}:{ident}".encode()).hexdigest()[:8]
    state = json.loads(STATE.read_text(encoding="utf-8"))
    sources = state.setdefault("config", {}).setdefault("sources", [])

    for existing in sources:
        if existing.get("id") == sid or existing.get("url") == url:
            existing.update({
                "kind": kind, "url": url, "ident": ident,
                "enabled": enabled, "max_per_run": per_run,
                "min_score": 100, "edit": True,
            })
            print(f"updated existing source {existing.get('ident')!r} (id {sid})")
            break
    else:
        sources.append({
            "id": sid, "kind": kind, "url": url, "ident": ident,
            "enabled": enabled, "max_per_run": per_run,
            "min_score": 100, "edit": True,
        })
        print(f"added source {ident!r} (kind {kind}, id {sid})")

    STATE.write_text(json.dumps(state, indent=1), encoding="utf-8")
    print(f"total sources now: {len(sources)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
