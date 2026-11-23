"""Performance monitoring: platform metrics -> scoreboard.csv.

Reads every outbox package with a platform entry at status "posted", asks the
matching poster adapter (agency.poster.<platform>) for metrics, and merges the
numbers into scoreboard.csv at the agency root. The csv follows the
gtavi-claudecode scoreboard contract exactly: one row per platform post, row
identity "<package_id>:<platform>", the exact header below.

Merge rules (fresh counts beat stale ones):
    - views_12h / views_24h / views_72h only fill inside their age window
      (10-14h, 22-26h, 70-74h since posted_at)
    - followers_at_read fills on the same read windows (latest read wins)
    - likes / shares / comments / saves refresh on EVERY read, any age
    - a None never blanks an existing number

Set CLIP_AGENCY_TEST_STUB=1 to run fully offline: every platform returns
deterministic fake metrics {views: age*10, likes: 2, comments: 1, shares: 0,
saves: 0, followers: None}.

CLI:  python -m agency.monitor [--platform youtube]
"""
from __future__ import annotations

import argparse
import csv
import datetime
import os
import sys
from zoneinfo import ZoneInfo

from .config import ROOT, ensure_dirs, load_config
from .logutil import log

SCOREBOARD_PATH = ROOT / "scoreboard.csv"

# Exact gtavi-claudecode scoreboard contract; do not reorder or rename.
HEADER: list[str] = [
    "post_id", "date", "time", "platform", "pillar", "format", "hook",
    "length_sec", "credit", "views_12h", "views_24h", "views_72h", "likes",
    "reposts_or_shares", "replies_or_comments", "saves_or_bookmarks",
    "followers_at_read", "test_name", "notes",
]

# (scoreboard column, min age hours, max age hours)
VIEW_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("views_12h", 10.0, 14.0),
    ("views_24h", 22.0, 26.0),
    ("views_72h", 70.0, 74.0),
)

# Engagement columns refreshed on every read, regardless of age window.
LIVE_COLUMNS: tuple[str, ...] = (
    "likes", "reposts_or_shares", "replies_or_comments", "saves_or_bookmarks",
)

# poster metrics() key -> scoreboard column
METRIC_COLUMNS: dict[str, str] = {
    "likes": "likes",
    "shares": "reposts_or_shares",
    "comments": "replies_or_comments",
    "saves": "saves_or_bookmarks",
    "followers": "followers_at_read",
}


# ---- time helpers -----------------------------------------------------------
def local_tz(config: dict):
    """ZoneInfo from config's page.timezone; 'local' falls back to the system."""
    name = str((config.get("page") or {}).get("timezone") or "local")
    if name and name != "local":
        try:
            return ZoneInfo(name)
        except Exception:
            log(f"unknown timezone {name!r}; using system local time")
    return datetime.datetime.now().astimezone().tzinfo


def parse_ts(value: str, tz) -> datetime.datetime | None:
    """ISO timestamp -> aware datetime (naive input assumed local); None if bad."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


# ---- metrics sources --------------------------------------------------------
def stub_enabled() -> bool:
    return os.environ.get("CLIP_AGENCY_TEST_STUB", "").strip() == "1"


def stub_metrics(age_h: float) -> dict:
    """Deterministic offline metrics: views scale with the post's age."""
    return {
        "views": int(age_h * 10),
        "likes": 2,
        "comments": 1,
        "shares": 0,
        "saves": 0,
        "followers": None,
    }


def poster_for(platform: str):
    """Instantiate the platform adapter via the agency.poster registry."""
    try:
        from .poster import get_poster

        return get_poster(platform)
    except Exception as exc:  # missing/broken adapter must never abort the run
        log(f"poster adapter for {platform!r} unavailable: {exc}")
        return None


def fetch_metrics(platform: str, post_entry: dict, age_h: float) -> dict | None:
    """Metrics for one platform post; None when no data source is available."""
    if stub_enabled():
        return stub_metrics(age_h)
    poster = poster_for(platform)
    if poster is None:
        return None
    return poster.metrics(post_entry)


# ---- scoreboard csv ---------------------------------------------------------
def load_rows() -> list[dict]:
    """Existing scoreboard rows; a missing or garbled file reads as empty."""
    if not SCOREBOARD_PATH.exists():
        return []
    try:
        with SCOREBOARD_PATH.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames != HEADER:
                return []
            return [dict(row) for row in reader]
    except OSError as exc:
        log(f"cannot read {SCOREBOARD_PATH.name}: {exc}", level="error")
        return []


def save_rows(rows: list[dict]) -> None:
    with SCOREBOARD_PATH.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=HEADER, restval="", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def set_metric(row: dict, column: str, value) -> bool:
    """Write one metric cell; never overwrite a number with None/blank."""
    if value is None:
        return False
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return False
    row[column] = str(number)
    return True


def merge_metrics(row: dict, metrics: dict, age_h: float) -> list[str]:
    """Merge one read into the row; returns the cells that changed."""
    filled: list[str] = []
    in_read_window = any(low_h <= age_h <= high_h for _c, low_h, high_h in VIEW_BUCKETS)
    if in_read_window:
        if set_metric(row, "followers_at_read", metrics.get("followers")):
            filled.append("followers_at_read")
    for column, low_h, high_h in VIEW_BUCKETS:
        if low_h <= age_h <= high_h and set_metric(row, column, metrics.get("views")):
            filled.append(column)
    for key, column in METRIC_COLUMNS.items():
        if column in LIVE_COLUMNS and set_metric(row, column, metrics.get(key)):
            filled.append(column)
    return filled


def fmt_number(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def seed_row(row: dict, meta: dict, platform: str, posted_at: datetime.datetime) -> None:
    """Fill the descriptive columns of a fresh scoreboard row."""
    local = posted_at.astimezone()
    source = meta.get("source") or {}
    notes = " ".join(part for part in (source.get("channel"), source.get("url")) if part)
    row.update({
        "post_id": f"{meta.get('id', '')}:{platform}",
        "date": local.strftime("%Y-%m-%d"),
        "time": local.strftime("%H:%M"),
        "platform": platform,
        "pillar": meta.get("pillar", ""),
        "format": meta.get("layout", ""),
        "hook": str(meta.get("hook", ""))[:80],
        "length_sec": fmt_number(meta.get("duration_sec")),
        "credit": meta.get("credit", ""),
        "test_name": meta.get("test_name", ""),
        "notes": notes,
    })


def last_views(entry: dict) -> int | None:
    """Views recorded on the previous monitor read (meta last_metrics)."""
    last = entry.get("last_metrics") or {}
    value = (last.get("metrics") or {}).get("views")
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def delta_views(entry: dict, metrics: dict) -> int | None:
    current, previous = metrics.get("views"), last_views(entry)
    if current is None or previous is None:
        return None
    return int(current) - previous


# ---- collection -------------------------------------------------------------
def collect(config: dict | None = None, platform_filter: str | None = None) -> dict:
    """Collect metrics for every posted platform entry into scoreboard.csv.

    A failing platform or package is logged and skipped, never fatal. Returns
    {"ran_at", "stub", "posts_seen", "updated": [...], "errors": [...]}.
    """
    ensure_dirs()
    cfg = config or load_config()
    tz = local_tz(cfg)
    now = datetime.datetime.now().astimezone()

    from . import queue  # deferred: keeps importing monitor cheap for learn.py

    rows = load_rows()
    by_id = {row.get("post_id", ""): row for row in rows}
    summary: dict = {
        "ran_at": now.isoformat(timespec="seconds"),
        "stub": stub_enabled(),
        "posts_seen": 0,
        "updated": [],
        "errors": [],
    }

    for meta in queue.list_packages():
        for platform, entry in (meta.get("posts") or {}).items():
            if platform_filter and platform != platform_filter:
                continue
            if entry.get("status") != "posted":
                continue
            post_id = f"{meta.get('id', '?')}:{platform}"
            summary["posts_seen"] += 1
            try:
                posted_at = parse_ts(entry.get("posted_at") or meta.get("created", ""), tz)
                if posted_at is None:
                    raise ValueError("no readable posted_at/created timestamp")
                age_h = (now - posted_at).total_seconds() / 3600.0
                try:
                    metrics = fetch_metrics(platform, entry, age_h)
                except Exception as exc:
                    summary["errors"].append(f"{post_id}: metrics failed: {exc}")
                    log(f"metrics failed for {post_id}: {exc}", level="error")
                    continue
                row = by_id.get(post_id)
                if row is None:
                    row = {column: "" for column in HEADER}
                    seed_row(row, meta, platform, posted_at)
                    rows.append(row)
                    by_id[post_id] = row
                filled = merge_metrics(row, metrics or {}, age_h) if metrics else []
                delta = delta_views(entry, metrics or {}) if metrics else None
                if metrics:
                    entry["last_metrics"] = {
                        "ts": now.isoformat(timespec="seconds"),
                        "metrics": metrics,
                    }
                    queue.save_meta(meta)
                summary["updated"].append({
                    "post_id": post_id,
                    "platform": platform,
                    "age_h": round(age_h, 1),
                    "views": (metrics or {}).get("views"),
                    "delta_views": delta,
                    "filled": filled,
                })
            except Exception as exc:  # one bad package must not stop the rest
                summary["errors"].append(f"{post_id}: {exc}")
                log(f"monitor skipped {post_id}: {exc}", level="error")

    save_rows(rows)
    return summary


# ---- CLI --------------------------------------------------------------------
def print_table(summary: dict) -> None:
    mode = " [STUB]" if summary.get("stub") else ""
    print(f"metrics run {summary.get('ran_at', '')}{mode}")
    header = f"{'package:platform':<34} {'age_h':>7} {'views':>8} {'delta':>7}  filled"
    print(header)
    print("-" * len(header))
    for item in summary.get("updated", []):
        views = "-" if item["views"] is None else f"{item['views']}"
        delta = "-" if item["delta_views"] is None else f"{item['delta_views']:+d}"
        filled = ",".join(item["filled"]) or "-"
        print(f"{item['post_id']:<34} {item['age_h']:>7.1f} {views:>8} {delta:>7}  {filled}")
    for error in summary.get("errors", []):
        print(f"! {error}")
    if not summary.get("updated") and not summary.get("errors"):
        print("nothing posted yet - nothing to collect")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect platform metrics into scoreboard.csv")
    parser.add_argument("--platform", help="only collect metrics for this platform")
    args = parser.parse_args(argv)
    try:
        summary = collect(platform_filter=args.platform)
    except Exception as exc:  # catastrophic only; per-post failures never get here
        log(f"monitor run failed: {exc}", level="error")
        return 1
    print_table(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
