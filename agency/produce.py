"""Production stage: source video -> moments -> cut -> rule-check -> outbox.

Chains the gtavi-claudecode scripts:
    moments.py  <url|file>  ->  jobs/<id>/{info,transcript,candidates}.json + source.mp4
    cut.py      jobs/<id> --pick N --layout L --hook "..."  ->  edits/clip_N.mp4
    check.py    edits/clip_N.mp4 --campaign campaigns.md    ->  PASS/FAIL (exit 1)
Only PASS clips become outbox packages.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from .config import ROOT, load_config, repo_dir, ensure_dirs
from .logutil import log
from .research import _run_repo
from . import queue, seo


def _jobs_dir() -> Path:
    ensure_dirs()
    return ROOT / "jobs"


def ensure_candidates(source: str, *, top: int, max_minutes: int) -> Path | None:
    """Run moments.py if this source has no job yet. Returns candidates.json path."""
    jobs_before = {p.name for p in _jobs_dir().iterdir() if p.is_dir()}
    # downloads + CPU transcription are slow: 4h ceiling (resumable job cache)
    res = _run_repo("moments.py", [source, "--top", str(top), "--max-minutes", str(max_minutes)],
                    timeout=4 * 3600)
    if res.returncode != 0:
        log(f"moments.py failed for {source}: {res.stderr[-400:]}", level="error")
        return None
    jobs_after = {p.name for p in _jobs_dir().iterdir() if p.is_dir()}
    new = jobs_after - jobs_before
    if new:
        job_id = sorted(new)[0]
    else:
        # source already had a job: match by slug-8 suffix is unreliable; take
        # the job whose info/candidates reference this source (URL or video id)
        job_id = ""
        for name in jobs_after:
            for probe in (_jobs_dir() / name / "info.json",
                          _jobs_dir() / name / "candidates.json"):
                if not probe.exists():
                    continue
                try:
                    data = json.loads(probe.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                info = data.get("info") or {}
                haystack = " ".join(str(x) for x in (
                    info.get("webpage_url", ""), info.get("id", ""),
                    data.get("source", ""),
                ))
                if source in haystack:
                    job_id = name
                    break
            if job_id:
                break
    if not job_id:
        log(f"could not locate job dir for {source}", level="error")
        return None
    cj = _jobs_dir() / job_id / "candidates.json"
    if not cj.exists():
        log(f"job {job_id} has no candidates.json", level="error")
        return None
    log(f"moments: job={job_id} ({source})")
    return cj


def _cut(job_id: str, pick: int, layout: str, hook: str, captions: str, allow_profanity: bool) -> Path | None:
    args = [f"jobs/{job_id}", "--pick", str(pick),
            "--layout", layout, "--hook", hook, "--captions", captions]
    if allow_profanity:
        args.append("--allow-profanity")
    res = _run_repo("cut.py", args, timeout=3600)
    if res.returncode != 0:
        log(f"cut.py failed (job={job_id} pick={pick}): {res.stderr[-400:]}", level="error")
        return None
    clip = _jobs_dir() / job_id / "edits" / f"clip_{pick}.mp4"
    if not clip.exists():
        log(f"cut.py reported success but {clip} missing", level="error")
        return None
    return clip


def _check(clip: Path) -> tuple[bool, str]:
    res = _run_repo("check.py", [str(clip), "--campaign", "campaigns.md"])
    out = (res.stdout or "") + (res.stderr or "")
    ok = res.returncode == 0 and "FAIL" not in out
    return ok, out[-500:]


def produce_from_source(
    source: str,
    *,
    picks: list[int] | None = None,
    hooks: dict[int, str] | None = None,
    max_packages: int = 2,
    cfg: dict | None = None,
) -> list[dict]:
    """Full production line for one source. Returns created package metas."""
    cfg = cfg or load_config()
    prod = cfg["production"]
    hooks = hooks or {}
    created: list[dict] = []

    cj = ensure_candidates(source, top=cfg["research"]["max_moments"],
                           max_minutes=prod.get("max_source_minutes", 90) or 90)
    if cj is None:
        return created
    data = json.loads(cj.read_text(encoding="utf-8"))
    info = data.get("info", {}) or {}
    candidates = data.get("candidates", []) or []
    if not candidates:
        log(f"no candidates for {source}")
        return created

    source_url = info.get("webpage_url", source)
    channel = info.get("channel") or info.get("uploader") or ""
    title = info.get("title", "")

    if picks:
        chosen = [c for c in candidates if c.get("n") in picks]
    else:
        chosen = sorted(candidates, key=lambda c: c.get("score", 0), reverse=True)
    chosen = chosen[: max(1, max_packages)]

    for cand in chosen:
        pick_n = cand.get("n")
        if pick_n is None:
            continue
        hook = hooks.get(pick_n) or seo.generate_hook(cand, info)
        log(f"produce: job cut pick={pick_n} hook={hook!r}")
        clip = _cut(Path(cj).parent.name, pick_n, prod["layout"], hook,
                    prod["captions"], prod.get("allow_profanity", False))
        if clip is None:
            continue
        ok, report = _check(clip)
        if not ok:
            log(f"rule-check FAIL for {clip.name}: {report}", level="error")
            continue
        meta = queue.new_package(
            clip,
            hook=hook,
            source_url=source_url,
            source_channel=channel,
            source_title=title,
            credit=f"@{channel}" if channel else "",
            pillar="clip",
            layout=prod["layout"],
            rule_check="PASS",
            platforms=cfg["platforms"],
        )
        captions = seo.fill_captions(meta, cfg=cfg)
        for p, cap in captions.items():
            queue.set_caption(meta, p, cap)
        created.append(queue.load_meta(meta))
        log(f"produce: package {meta['id']} ready ({meta['duration_sec']}s)")
    return created


def produce_from_brief(cfg: dict | None = None, *, limit: int = 2) -> list[dict]:
    """Produce from the latest research brief's clip targets."""
    cfg = cfg or load_config()
    from .research import latest_brief

    brief = latest_brief()
    if not brief:
        log("produce: no research brief found; run the research stage first", level="error")
        return []
    created: list[dict] = []
    for target in brief.get("clip_targets", [])[:limit]:
        made = produce_from_source(target["url"], max_packages=1, cfg=cfg)
        created.extend(made)
        if len(created) >= limit:
            break
    return created
