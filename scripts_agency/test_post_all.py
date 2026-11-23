"""One-shot pipeline test: post ONE branded sample clip to FB + IG + YT.

Builds a minimal outbox package from media/test/out_vert.mp4 (the branded
editclip render), then calls each poster's upload() directly - bypassing the
loop's caps/state, so this test does NOT affect the automation's counters.

Usage (from repo root):
    python scripts_agency\\test_post_all.py            # all platforms
    python scripts_agency\\test_post_all.py youtube    # one platform only
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agency.poster.facebook import FacebookPoster  # noqa: E402
from agency.poster.instagram import InstagramPoster  # noqa: E402
from agency.poster.youtube import YouTubePoster  # noqa: E402

PKG_ID = "test_pipeline_001"
VIDEO_SRC = ROOT / "media" / "test" / "out_vert.mp4"

CAPTIONS = {
    "youtube": {
        "title": "RGCIPZAH pipeline test - safe to ignore",
        "description": "Tech check of the automated upload chain. Real edits incoming.\n\nFan page - clips credited to their editors. DM for credit or removal.",
        "tags": ["rgcipzah", "shorts", "test"],
    },
    "facebook": {
        "caption": "Pipeline test - RGCIPZAH is warming up. Real edits coming soon.",
        "hashtags": ["#rgcipzah", "#movieclips", "#shorts"],
    },
    "instagram": {
        "caption": "Pipeline test - RGCIPZAH is warming up (tech check, real edits coming)",
        "hashtags": ["#rgcipzah", "#movieclips", "#reels"],
    },
}


def main() -> int:
    pkg_dir = ROOT / "outbox" / PKG_ID
    pkg_dir.mkdir(parents=True, exist_ok=True)
    video = pkg_dir / "video.mp4"
    if not video.exists():
        shutil.copyfile(VIDEO_SRC, video)
        print(f"staged: {video}")

    package = {
        "id": PKG_ID,
        "video": "video.mp4",
        "rule_check": "PASS",
        "status": "approved",
        "hook": "RGCIPZAH pipeline test",
        "_captions": CAPTIONS,
    }

    only = {a.lower() for a in sys.argv[1:]}
    all_posters = [FacebookPoster(), YouTubePoster(), InstagramPoster()]
    posters = [p for p in all_posters if not only or p.name in only]
    skipped = [p.name for p in all_posters if only and p.name not in only]
    if skipped:
        print(f"(skipping per args: {', '.join(skipped)})")

    results = {}
    for p in posters:
        dry = p.dry_run(package)
        print(f"\n=== {p.name}: dry_run ===")
        print(f"  configured={dry['configured']} would_upload={dry['would_upload']}")
        if dry["missing_env"]:
            print(f"  missing: {dry['missing_env']}")
        if dry["problems"]:
            print(f"  problems: {dry['problems']}")
        if not dry["would_upload"]:
            results[p.name] = {"ok": False, "error": "dry_run blocked"}
            continue
        print(f"  uploading ...")
        res = p.upload(package)
        results[p.name] = res
        if res.get("ok"):
            print(f"  OK  post_id={res.get('post_id')}")
            print(f"  URL {res.get('url')}")
        else:
            print(f"  FAILED: {res.get('error')} (retry={res.get('retry')})")

    print("\n================ SUMMARY ================")
    for name, res in results.items():
        status = "OK" if res.get("ok") else "FAILED"
        extra = res.get("url") or res.get("error", "")
        print(f"{name:10s} {status:7s} {extra}")
    return 0 if all(r.get("ok") for r in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
