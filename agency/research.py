"""Research stage: trend radar + live streams + Reddit questions -> daily brief.

Wraps the gtavi-claudecode scripts (keyless) and parses their markdown output
into structured picks the production stage can consume.
"""
from __future__ import annotations

import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

from .config import ROOT, load_config, repo_dir, ensure_dirs
from .logutil import log

RESEARCH_DIR = ROOT / "research"


def _run_repo(script: str, args: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    """Run a gtavi-claudecode script with CLIP_DESK_HOME pointed at the agency.

    timeout: light scripts (trends/streams/questions) need ~minutes; heavy
    ones (moments.py downloads + CPU transcription) need hours.
    """
    import os

    from .transcribe import is_available as whisper_cpp_available

    repo = repo_dir()
    env_vars = {
        "CLIP_DESK_HOME": str(ROOT),
        "PYTHONIOENCODING": "utf-8",
    }
    if whisper_cpp_available():
        # pass the user's explicit transcriber choice through to the repo hook
        env_vars["CLIP_AGENCY_DIR"] = str(ROOT)
        if os.environ.get("CLIP_TRANSCRIBER") == "whisper_cpp":
            env_vars["CLIP_TRANSCRIBER"] = "whisper_cpp"
    cmd = [sys.executable, str(repo / "scripts" / script), *args]
    ensure_dirs()
    return subprocess.run(
        cmd, cwd=str(ROOT), env={**dict(os.environ), **env_vars},
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )


# ---------------------------------------------------------------- parsers
_TREND_ROW = re.compile(
    r"^\|\s*(\d+)\s*\|\s*([\d,]*)\s*\|\s*([\d,]*)\s*\|\s*([\d.]+)\s*\|\s*(\d+)\s*"
    r"\|\s*(.*?)\s*\|\s*\[(.*?)\]\((\S+?)\)\s*\|\s*(yes?)?\s*\|\s*$"
)
_STREAM_ROW = re.compile(
    r"^\|\s*\d+\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*([\d,]*)\s*\|\s*(.*?)\s*\|\s*(https?://\S+?)\s*\|\s*$"
)


def run_trends(query: str, hours: float, limit: int) -> list[dict]:
    out = RESEARCH_DIR / f"trends-{datetime.date.today():%Y%m%d}.md"
    RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
    res = _run_repo("trends.py", ["--query", query, "--hours", str(hours),
                                  "--limit", str(limit), "--out", str(out)])
    if res.returncode != 0:
        log(f"trends.py failed: {res.stderr[-300:]}", level="error")
        return []
    rows = []
    for line in out.read_text(encoding="utf-8").splitlines():
        m = _TREND_ROW.match(line)
        if not m:
            continue
        n, vph, views, age, minutes, channel, title, url, heat = m.groups()
        rows.append({
            "rank": int(n),
            "views_per_hour": int(vph.replace(",", "") or 0),
            "views": int(views.replace(",", "") or 0),
            "age_hours": float(age),
            "minutes": int(minutes or 0),
            "channel": channel,
            "title": title,
            "url": url,
            "has_heatmap": heat == "yes",
        })
    return rows


def run_streams(query: str, limit: int) -> list[dict]:
    out = RESEARCH_DIR / f"streams-{datetime.date.today():%Y%m%d}.md"
    RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
    res = _run_repo("streams.py", ["--query", query, "--limit", str(limit), "--out", str(out)])
    if res.returncode != 0:
        log(f"streams.py failed: {res.stderr[-300:]}", level="error")
        return []
    rows, platform = [], ""
    for line in out.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            platform = line[3:].strip()
            continue
        m = _STREAM_ROW.match(line)
        if m:
            channel, title, viewers, live_for, url = m.groups()
            rows.append({
                "platform": platform,
                "channel": channel,
                "title": title,
                "viewers": int(viewers.replace(",", "") or 0),
                "live_for": live_for.strip(),
                "url": url,
            })
    return sorted(rows, key=lambda r: r["viewers"], reverse=True)


def run_questions(subs: str, days: int) -> int:
    """Mine repeated Reddit questions into idea-bank; returns # of questions found."""
    res = _run_repo("questions.py", ["--subs", subs, "--days", str(days),
                                     "--facts", str(ROOT / "facts.md"),
                                     "--ideas", str(ROOT / "idea-bank.md"),
                                     "--out", str(RESEARCH_DIR / f"questions-{datetime.date.today():%Y%m%d}.md")])
    if res.returncode != 0:
        log(f"questions.py failed: {res.stderr[-300:]}", level="error")
        return 0
    qfile = RESEARCH_DIR / f"questions-{datetime.date.today():%Y%m%d}.md"
    if qfile.exists():
        return sum(1 for ln in qfile.read_text(encoding="utf-8").splitlines()
                   if ln.startswith("| ") and "count" not in ln and "---" not in ln)
    return 0


# ---------------------------------------------------------------- brief
def build_brief(cfg: dict) -> dict:
    """Run all research sources and write research/brief-<date>.{json,md}."""
    r = cfg["research"]
    log(f"research: trends query={r['query']!r} hours={r['trend_hours']}")
    trends = run_trends(r["query"], r["trend_hours"], r["trend_limit"])
    streams = run_streams(r["query"], 10)
    n_questions = 0
    try:
        n_questions = run_questions(r["subreddits"], 3)
    except Exception as exc:  # never abort the brief
        log(f"questions mining skipped: {exc}", level="error")

    # picks: top 3 trending videos by views/hour (prefer heatmap when close)
    pool = sorted(trends, key=lambda t: (t["views_per_hour"] + (500 if t["has_heatmap"] else 0)),
                  reverse=True)
    clip_targets = pool[:3]
    live_targets = streams[:2]

    brief = {
        "date": datetime.date.today().isoformat(),
        "generated": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "query": r["query"],
        "trend_count": len(trends),
        "stream_count": len(streams),
        "question_ideas": n_questions,
        "clip_targets": clip_targets,
        "live_targets": live_targets,
    }

    RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
    (RESEARCH_DIR / f"brief-{datetime.date.today():%Y%m%d}.json").write_text(
        json.dumps(brief, indent=2, ensure_ascii=False), encoding="utf-8")

    md = [f"# Agency brief — {brief['date']}", "",
          f"Trends scanned: {len(trends)} · Live streams: {len(streams)} · Reddit question ideas: {n_questions}",
          "", "## Clip targets (by views/hour)", ""]
    for t in clip_targets:
        heat = " · most-replayed heatmap" if t["has_heatmap"] else ""
        md.append(f"- **{t['title']}** — {t['channel']} · {t['views_per_hour']:,} views/h · "
                  f"{t['age_hours']:.1f}h old{heat}\n  {t['url']}")
    md += ["", "## Live targets (by viewers)", ""]
    for s in live_targets:
        md.append(f"- **{s['channel']}** ({s['platform']}) — {s['viewers']:,} viewers · {s['title']}\n  {s['url']}")
    (RESEARCH_DIR / f"brief-{datetime.date.today():%Y%m%d}.md").write_text(
        "\n".join(md) + "\n", encoding="utf-8")

    log(f"research: brief written ({len(clip_targets)} clip targets, {len(live_targets)} live targets)")
    return brief


def latest_brief(max_age_days: int = 2) -> dict | None:
    """Most recent brief within max_age_days, else None."""
    RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today()
    for back in range(max_age_days + 1):
        p = RESEARCH_DIR / f"brief-{(today - datetime.timedelta(days=back)):%Y%m%d}.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    return None
