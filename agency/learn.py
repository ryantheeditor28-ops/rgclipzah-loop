"""Learning loop: scoreboard.csv -> learning.md + logs/plan.json.

review() reads the scoreboard (tolerating missing/empty numeric cells) and
computes the daily picture: best/worst post by latest view bucket, median
views by pillar / platform / daypart, posts per pillar over 7 days, plus
exactly ONE test suggestion per day, rotating deterministically by date
through: hook style -> length -> posting time -> layout -> pillar mix.

run() persists two artifacts:
    learning.md   one "## <date>" section appended under the Wins / Losses /
                  Current Rules headings (file created with those three
                  headings if missing); rules are short imperative lines.
    logs/plan.json  schema (kept deliberately small):
                  {"date": "2026-11-19",
                   "best_slot": "evening",
                   "pillar_medians": {"clip": 1234.0},
                   "platform_medians": {"youtube": 2000.0},
                   "test": {"name": "evening-slot",
                            "variable": "posting time",
                            "rationale": "..."},
                   "tomorrow": {"vertical_posts": 2,
                                "suggested_hooks_style": "question"}}

CLI:  python -m agency.learn [--weekly]
      --weekly adds the 7-day top3/bottom3 and a rule to cut the weakest pillar.
An empty scoreboard prints a friendly bootstrap message instead of crashing.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import json
import statistics
import sys
from pathlib import Path

from .config import ROOT, ensure_dirs, load_config
from .logutil import log
from .monitor import SCOREBOARD_PATH

LEARNING_MD = ROOT / "learning.md"
PLAN_PATH = ROOT / "logs" / "plan.json"

LEARNING_HEADINGS: tuple[str, ...] = ("Wins", "Losses", "Current Rules")

# One test per day; the variable rotates deterministically by date.
TEST_ROTATION: tuple[str, ...] = ("hook style", "length", "posting time", "layout", "pillar mix")


# ---- scoreboard reading -----------------------------------------------------
def load_rows() -> list[dict]:
    if not SCOREBOARD_PATH.exists():
        return []
    try:
        with SCOREBOARD_PATH.open("r", encoding="utf-8", newline="") as fh:
            return [dict(row) for row in csv.DictReader(fh)]
    except OSError as exc:
        log(f"cannot read {SCOREBOARD_PATH.name}: {exc}", level="error")
        return []


def num(row: dict, column: str) -> float | None:
    """Numeric cell value; missing/empty/broken cells read as None."""
    text = str(row.get(column) or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def latest_views(row: dict) -> float | None:
    """Most recent view count on record: 72h beats 24h beats 12h."""
    for column in ("views_72h", "views_24h", "views_12h"):
        value = num(row, column)
        if value is not None:
            return value
    return None


def daypart(time_text: str) -> str:
    """morning <12, afternoon 12-17, evening 17-22, night otherwise."""
    try:
        hour = int(str(time_text).strip().split(":")[0])
    except (ValueError, IndexError):
        return "night"
    if hour < 12:
        return "morning"
    if hour < 17:
        return "afternoon"
    if hour < 22:
        return "evening"
    return "night"


def brief(row: dict) -> dict:
    return {
        "post_id": row.get("post_id", ""),
        "platform": row.get("platform", ""),
        "pillar": row.get("pillar", ""),
        "views": latest_views(row),
        "date": row.get("date", ""),
    }


def fmt_views(value) -> str:
    return "-" if value is None else f"{value:.0f}"


# ---- aggregates -------------------------------------------------------------
def medians_by(rows: list[dict], key_of) -> dict[str, float]:
    """Median latest views per group; rows without any views are skipped."""
    groups: dict[str, list[float]] = {}
    for row in rows:
        views = latest_views(row)
        if views is None:
            continue
        groups.setdefault(key_of(row), []).append(views)
    return {key: statistics.median(values) for key, values in sorted(groups.items()) if values}


def _parse_date(text: str) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(text or "").strip())
    except ValueError:
        return None


def rows_since(rows: list[dict], today: datetime.date, days: int) -> list[dict]:
    cutoff = today - datetime.timedelta(days=days)
    return [row for row in rows if (d := _parse_date(row.get("date"))) and d >= cutoff]


def pillar_counts(rows: list[dict], today: datetime.date) -> dict[str, int]:
    """Posts per pillar over the last 7 days (by scoreboard date)."""
    counts: dict[str, int] = {}
    for row in rows_since(rows, today, days=7):
        pillar = str(row.get("pillar") or "").strip() or "unknown"
        counts[pillar] = counts.get(pillar, 0) + 1
    return counts


def scored_rows(rows: list[dict]) -> list[tuple[float, dict]]:
    return [(views, row) for row in rows if (views := latest_views(row)) is not None]


def best_worst(rows: list[dict]) -> tuple[dict | None, dict | None]:
    scored = sorted(scored_rows(rows), key=lambda pair: pair[0])
    if not scored:
        return None, None
    return scored[-1][1], scored[0][1]


# ---- test selection ---------------------------------------------------------
def hook_style(hook: str) -> str:
    """Crude but deterministic style label for a hook line."""
    text = (hook or "").strip().lower()
    if not text:
        return "bold claim"
    if "how to" in text or text.startswith("how "):
        return "how-to"
    if text.endswith("?") or text.split()[0] in ("what", "why", "did", "who", "which"):
        return "question"
    if text[:1].isdigit():
        return "number"
    if any(word in text for word in ("nobody", "secret", "wrong", "stop", "never")):
        return "contrarian"
    return "bold claim"


def _pct_lift(a: float, b: float) -> str:
    if b == 0:
        return "n/a"
    return f"{(a / b - 1) * 100:+.0f}%"


def _test_hook(rows: list[dict]) -> tuple[str, str]:
    best, _worst = best_worst(rows)
    if best is None:
        return "hook-style", "no reads yet - run the monitor after the 12h mark, then copy the winning hook style"
    style = hook_style(str(best.get("hook", "")))
    snippet = str(best.get("hook", ""))[:60]
    return f"{style}-hook", (
        f"top post used a {style} hook ({snippet!r}) - write tomorrow's hooks in that style"
    )


def _test_length(rows: list[dict]) -> tuple[str, str]:
    short: list[float] = []
    long: list[float] = []
    for row in rows:
        views = latest_views(row)
        length = num(row, "length_sec")
        if views is None or length is None:
            continue
        (short if length <= 15 else long).append(views)
    if short and long:
        med_short, med_long = statistics.median(short), statistics.median(long)
        if med_short > med_long * 1.3:
            return "short-cut", (
                f"sub-15s posts median {med_short:.0f} views vs {med_long:.0f} for longer - "
                "cut tomorrow's edits to under 15 seconds"
            )
        if med_long > med_short * 1.3:
            return "long-play", (
                f"posts over 15s median {med_long:.0f} views vs {med_short:.0f} for short - "
                "test one 20-30s edit"
            )
        return "length-pair", (
            f"short {med_short:.0f} vs long {med_long:.0f} median views - post a 12s and a "
            "25s version of the same moment"
        )
    return "length", "no length data yet - log length_sec on every post and compare at 24h"


def _test_posting_time(rows: list[dict]) -> tuple[str, str]:
    by_slot = medians_by(rows, lambda r: daypart(r.get("time", "")))
    evening, morning = by_slot.get("evening"), by_slot.get("morning")
    if evening and morning:
        if evening > morning * 1.3:
            return "evening-slot", (
                f"evening median {evening:.0f} views vs morning {morning:.0f} "
                f"({_pct_lift(evening, morning)}) - move a post to the evening slot"
            )
        if morning > evening * 1.3:
            return "morning-slot", (
                f"morning median {morning:.0f} views vs evening {evening:.0f} "
                f"({_pct_lift(morning, evening)}) - move a post to the morning slot"
            )
        return "slot-head-to-head", (
            f"morning {morning:.0f} vs evening {evening:.0f} median views - "
            "post one at each and compare the 24h reads"
        )
    return "posting-time", (
        "not enough slot data yet - post one extra at a different hour and log its 12h read"
    )


def _test_layout(rows: list[dict]) -> tuple[str, str]:
    by_format = medians_by(rows, lambda r: str(r.get("format") or "").strip() or "unknown")
    if len(by_format) > 1:
        winner = max(by_format, key=by_format.get)
        return f"{winner}-layout", (
            f"'{winner}' layout posts median {by_format[winner]:.0f} views - "
            "run every post through it this week"
        )
    return "layout", "log the layout column per post to compare blur vs full-frame"


def _test_pillar(rows: list[dict]) -> tuple[str, str]:
    by_pillar = medians_by(rows, lambda r: str(r.get("pillar") or "").strip() or "unknown")
    if len(by_pillar) > 1:
        winner = max(by_pillar, key=by_pillar.get)
        return f"{winner}-mix", (
            f"{winner} posts median {by_pillar[winner]:.0f} views - "
            f"shift tomorrow's mix toward {winner}"
        )
    return "pillar-mix", "no pillar data yet - tag every post with its pillar and read at 24h"


_TEST_DETAIL = {
    "hook style": _test_hook,
    "length": _test_length,
    "posting time": _test_posting_time,
    "layout": _test_layout,
    "pillar mix": _test_pillar,
}


def pick_test(rows: list[dict], today: datetime.date) -> dict:
    """Exactly one test per day; the variable rotates deterministically by date."""
    variable = TEST_ROTATION[today.toordinal() % len(TEST_ROTATION)]
    name, rationale = _TEST_DETAIL[variable](rows)
    return {"name": name, "variable": variable, "rationale": rationale}


# ---- review -----------------------------------------------------------------
def daily_rules(report: dict, weekly: bool) -> list[str]:
    """Short imperative rules for the dated learning.md section."""
    rules: list[str] = []
    pillar_medians = report.get("pillar_medians") or {}
    if pillar_medians:
        top = max(pillar_medians, key=pillar_medians.get)
        rules.append(f"Make more {top} posts - highest median views.")
    slots = report.get("daypart_medians") or {}
    if slots.get("morning") and slots.get("evening") and slots["evening"] > slots["morning"] * 1.3:
        rules.append("Hold the evening slot for the strongest post of the day.")
    if weekly and pillar_medians:
        weak = min(pillar_medians, key=pillar_medians.get)
        rules.append(f"Cut or rework {weak} posts for a week.")
    return rules


def weekly_summary(rows: list[dict], today: datetime.date) -> dict:
    recent = [row for row in rows_since(rows, today, days=7) if latest_views(row) is not None]
    recent.sort(key=latest_views, reverse=True)
    return {
        "top3": [brief(row) for row in recent[:3]],
        "bottom3": [brief(row) for row in reversed(recent[-3:])],
    }


def review(config: dict | None = None, weekly: bool = False) -> dict:
    """Analyze scoreboard.csv and return the report dict (no files written)."""
    ensure_dirs()
    _config = config or load_config()
    today = datetime.date.today()
    rows = load_rows()
    if not rows:
        return {"date": today.isoformat(), "posts": 0, "empty": True}

    best, worst = best_worst(rows)
    report: dict = {
        "date": today.isoformat(),
        "posts": len(rows),
        "best": brief(best) if best else None,
        "worst": brief(worst) if worst else None,
        "pillar_medians": medians_by(rows, lambda r: str(r.get("pillar") or "").strip() or "unknown"),
        "platform_medians": medians_by(rows, lambda r: str(r.get("platform") or "").strip() or "unknown"),
        "daypart_medians": medians_by(rows, lambda r: daypart(r.get("time", ""))),
        "pillar_counts_7d": pillar_counts(rows, today),
        "test": pick_test(rows, today),
    }
    daypart_medians = report["daypart_medians"]
    report["best_slot"] = max(daypart_medians, key=daypart_medians.get) if daypart_medians else ""
    report["suggested_hooks_style"] = (
        hook_style(str(best.get("hook", ""))) if best else "bold claim"
    )
    report["rules"] = daily_rules(report, weekly)
    if weekly:
        report["weekly"] = weekly_summary(rows, today)
    return report


# ---- outputs ----------------------------------------------------------------
def ensure_learning_md() -> Path:
    if not LEARNING_MD.exists():
        LEARNING_MD.write_text(
            "# Learning log\n\n" + "".join(f"## {h}\n\n" for h in LEARNING_HEADINGS),
            encoding="utf-8",
        )
    return LEARNING_MD


def append_section(report: dict) -> None:
    """Append one "## <date>" section to learning.md."""
    ensure_learning_md()
    lines = ["", f"## {report['date']}"]
    if report.get("empty"):
        lines.append("- No scoreboard rows yet. Post first, then check back after the 12h read.")
    else:
        best, worst = report.get("best"), report.get("worst")
        if best:
            lines.append(
                f"- Win: {best['post_id']} ({best['platform']}) at {fmt_views(best['views'])} views."
            )
        if worst:
            lines.append(
                f"- Loss: {worst['post_id']} ({worst['platform']}) at only {fmt_views(worst['views'])} views."
            )
        counts = report.get("pillar_counts_7d") or {}
        if counts:
            mix = ", ".join(f"{pillar}:{count}" for pillar, count in sorted(counts.items()))
            lines.append(f"- Mix (7d): {mix}.")
        weekly = report.get("weekly") or {}
        for label in ("top3", "bottom3"):
            entries = weekly.get(label) or []
            if entries:
                shown = "; ".join(f"{e['post_id']} {fmt_views(e['views'])}" for e in entries)
                lines.append(f"- Weekly {label.replace('3', ' 3')}: {shown}.")
        for rule in report.get("rules", []):
            lines.append(f"- Rule: {rule}")
        test = report.get("test") or {}
        if test:
            lines.append(
                f"- Test: {test.get('name', '')} ({test.get('variable', '')}) - {test.get('rationale', '')}"
            )
    with LEARNING_MD.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def write_plan(report: dict, config: dict | None = None) -> Path:
    """Write logs/plan.json (schema documented in the module docstring)."""
    cfg = config or load_config()
    try:
        vertical = int(cfg.get("schedule", {}).get("vertical_posts_per_day") or 2)
    except (TypeError, ValueError):
        vertical = 2
    plan = {
        "date": report.get("date", ""),
        "best_slot": report.get("best_slot", ""),
        "pillar_medians": report.get("pillar_medians", {}),
        "platform_medians": report.get("platform_medians", {}),
        "test": report.get("test", {}),
        "tomorrow": {
            "vertical_posts": vertical,
            "suggested_hooks_style": report.get("suggested_hooks_style", "bold claim"),
        },
    }
    PLAN_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLAN_PATH.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    return PLAN_PATH


# ---- CLI --------------------------------------------------------------------
def run(weekly: bool = False) -> dict:
    """review() + write learning.md and logs/plan.json; returns the report."""
    report = review(weekly=weekly)
    if report.get("empty"):
        return report
    append_section(report)
    write_plan(report)
    return report


def print_report(report: dict) -> None:
    if report.get("empty"):
        print("No posts in scoreboard.csv yet - post a clip first.")
        print("The monitor fills the first view read 10-14h after posting; check back after 12h.")
        return
    best, worst = report.get("best"), report.get("worst")
    if best:
        print(f"Best  : {best['post_id']}  {best['platform']}  {fmt_views(best['views'])} views")
    if worst:
        print(f"Worst : {worst['post_id']}  {worst['platform']}  {fmt_views(worst['views'])} views")
    for label, medians in (
        ("pillar", report.get("pillar_medians", {})),
        ("platform", report.get("platform_medians", {})),
        ("daypart", report.get("daypart_medians", {})),
    ):
        if medians:
            pretty = ", ".join(f"{key}={value:.0f}" for key, value in medians.items())
            print(f"Median views by {label}: {pretty}")
    counts = report.get("pillar_counts_7d") or {}
    if counts:
        print("Posts per pillar (7d): " + ", ".join(f"{k}:{v}" for k, v in sorted(counts.items())))
    test = report.get("test") or {}
    print(f"Today's test: {test.get('name', '')} [{test.get('variable', '')}] - {test.get('rationale', '')}")
    weekly = report.get("weekly") or {}
    for label in ("top3", "bottom3"):
        entries = weekly.get(label) or []
        if entries:
            shown = "; ".join(f"{e['post_id']} {fmt_views(e['views'])}" for e in entries)
            print(f"Weekly {label.replace('3', ' 3')}: {shown}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Daily/weekly learning review over scoreboard.csv")
    parser.add_argument("--weekly", action="store_true", help="add 7-day top3/bottom3 and a weakest-pillar cut rule")
    args = parser.parse_args(argv)
    try:
        report = run(weekly=args.weekly)
    except Exception as exc:
        log(f"learn run failed: {exc}", level="error")
        return 1
    print_report(report)
    if not report.get("empty"):
        print(f"learning.md updated; plan at {PLAN_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
