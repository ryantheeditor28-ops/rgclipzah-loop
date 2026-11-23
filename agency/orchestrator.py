"""Agency orchestrator: the full daily cycle.

    research -> produce -> seo -> post -> monitor -> learn -> report

Approval gate: 'post' only uploads packages whose status == 'approved'
(approve via scripts_agency/approve.py). Everything else is automated.

Usage:
    python -m agency.orchestrator                    # run all stages
    python -m agency.orchestrator --stage produce    # one stage
    python -m agency.orchestrator --source <url>     # produce from one video
"""
from __future__ import annotations

import argparse
import datetime
import traceback

from . import queue, research, seo
from .config import ensure_dirs, load_config
from .logutil import human_log, log


def stage_research(cfg: dict) -> dict:
    return research.build_brief(cfg)


def stage_produce(cfg: dict, *, source: str | None = None, limit: int | None = None) -> list[dict]:
    from . import produce as prod_mod

    limit = limit if limit is not None else cfg["schedule"]["vertical_posts_per_day"]
    if source:
        return prod_mod.produce_from_source(source, max_packages=limit, cfg=cfg)
    return prod_mod.produce_from_brief(cfg, limit=limit)


def stage_seo(cfg: dict) -> list[str]:
    """Fill missing captions on pending packages."""
    touched = []
    for meta in queue.list_packages(status="pending_approval"):
        missing = [p for p in meta["posts"] if not queue.get_caption(meta, p)]
        if not missing:
            continue
        captions = seo.fill_captions(meta, cfg=cfg)
        for p in missing:
            if p in captions:
                queue.set_caption(meta, p, captions[p])
        touched.append(meta["id"])
    if touched:
        log(f"seo: captions filled for {len(touched)} package(s)")
    return touched


def post_one(package_id: str, cfg: dict | None = None) -> list[dict]:
    """Upload ONE package to its still-queued platforms (web app / CLI helper)."""
    cfg = cfg or load_config()
    try:
        from .poster import get_posters
    except ImportError as exc:
        log(f"post: poster module unavailable ({exc})", level="error")
        return []
    meta = queue.load_meta(package_id)
    if meta["status"] != "approved":
        return [{"package": package_id, "ok": False,
                 "error": f"status={meta['status']!r} (must be approved)"}]
    posters = get_posters(cfg["platforms"])  # already {name: poster instance}
    meta["_captions"] = {p: queue.get_caption(meta, p) for p in meta["posts"]}
    results = []
    for pname, entry in meta["posts"].items():
        if entry["status"] in ("posted", "skipped"):
            continue
        poster = posters.get(pname)
        if poster is None:
            queue.set_platform_status(meta, pname, "skipped", error="no adapter")
            continue
        if not poster.is_configured():
            queue.set_platform_status(
                meta, pname, "skipped",
                error="not configured: missing " + ", ".join(poster.missing_config()))
            continue
        problems = poster.validate(queue.load_meta(meta) | {"_captions": meta["_captions"]})
        if problems:
            queue.set_platform_status(meta, pname, "failed", error="; ".join(problems))
            results.append({"package": meta["id"], "platform": pname, "ok": False,
                            "error": "; ".join(problems)})
            continue
        log(f"post: uploading {meta['id']} -> {pname}")
        try:
            res = poster.upload(queue.load_meta(meta) | {"_captions": meta["_captions"]})
        except Exception as exc:
            res = {"ok": False, "error": f"{exc!r}", "retry": True}
        if res.get("ok"):
            queue.set_platform_status(
                meta, pname, "posted",
                post_id=res.get("post_id", ""), url=res.get("url", ""),
                posted_at=datetime.datetime.now().astimezone().isoformat(timespec="seconds"))
            human_log(f"POSTED {meta['id']} to {pname}: {res.get('url', '')}")
            log(f"post: OK {meta['id']} -> {pname} {res.get('url', '')}")
        else:
            queue.set_platform_status(meta, pname, "failed", error=res.get("error", "unknown"))
            log(f"post: FAIL {meta['id']} -> {pname}: {res.get('error', '')[:200]}", level="error")
        results.append({"package": meta["id"], "platform": pname, **res})
    return results


def stage_post(cfg: dict) -> list[dict]:
    """Upload approved packages to configured platforms (never pending ones)."""
    try:
        from .poster import get_posters
    except ImportError as exc:
        log(f"post: poster module unavailable ({exc})", level="error")
        return []

    results = []
    posters = get_posters(cfg["platforms"])  # already {name: poster instance}
    for meta in queue.list_packages(status=None):
        if meta["status"] != "approved":
            continue  # approval gate — human decides
        # inject captions for validate/dry_run/preview
        meta["_captions"] = {p: queue.get_caption(meta, p) for p in meta["posts"]}
        for pname, entry in meta["posts"].items():
            if entry["status"] in ("posted", "skipped"):
                continue
            poster = posters.get(pname)
            if poster is None:
                queue.set_platform_status(meta, pname, "skipped", error="no adapter")
                continue
            if not poster.is_configured():
                queue.set_platform_status(
                    meta, pname, "skipped",
                    error="not configured: missing " + ", ".join(poster.missing_config()))
                continue
            problems = poster.validate(queue.load_meta(meta) | {"_captions": meta["_captions"]})
            if problems:
                queue.set_platform_status(meta, pname, "failed", error="; ".join(problems))
                results.append({"package": meta["id"], "platform": pname, "ok": False,
                                "error": "; ".join(problems)})
                continue
            log(f"post: uploading {meta['id']} -> {pname}")
            try:
                res = poster.upload(queue.load_meta(meta) | {"_captions": meta["_captions"]})
            except Exception as exc:
                res = {"ok": False, "error": f"{exc!r}", "retry": True}
            if res.get("ok"):
                queue.set_platform_status(
                    meta, pname, "posted",
                    post_id=res.get("post_id", ""), url=res.get("url", ""),
                    posted_at=datetime.datetime.now().astimezone().isoformat(timespec="seconds"))
                human_log(f"POSTED {meta['id']} to {pname}: {res.get('url', '')}")
                log(f"post: OK {meta['id']} -> {pname} {res.get('url', '')}")
            else:
                queue.set_platform_status(meta, pname, "failed", error=res.get("error", "unknown"))
                log(f"post: FAIL {meta['id']} -> {pname}: {res.get('error', '')[:200]}", level="error")
            results.append({"package": meta["id"], "platform": pname, **res})
    return results


def stage_monitor(cfg: dict) -> dict:
    try:
        from . import monitor
    except ImportError as exc:
        log(f"monitor: module unavailable ({exc})", level="error")
        return {}
    return monitor.collect(cfg)


def stage_learn(cfg: dict) -> dict:
    try:
        from . import learn
    except ImportError as exc:
        log(f"learn: module unavailable ({exc})", level="error")
        return {}
    return learn.review(cfg)


def run_all(source: str | None = None, limit: int | None = None) -> dict:
    ensure_dirs()
    cfg = load_config()
    report: dict = {"started": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                    "stages": {}}
    stages: list[tuple[str, callable]] = [
        ("research", lambda: stage_research(cfg)),
        ("produce", lambda: stage_produce(cfg, source=source, limit=limit)),
        ("seo", lambda: stage_seo(cfg)),
        ("post", lambda: stage_post(cfg)),
        ("monitor", lambda: stage_monitor(cfg)),
        ("learn", lambda: stage_learn(cfg)),
    ]
    for name, fn in stages:
        t0 = datetime.datetime.now()
        try:
            report["stages"][name] = fn()
            status = "ok"
        except Exception as exc:
            report["stages"][name] = {"error": repr(exc)}
            log(f"stage {name} crashed: {exc}\n{traceback.format_exc(limit=3)}", level="error")
            status = "crashed"
        dt = (datetime.datetime.now() - t0).total_seconds()
        log(f"stage {name}: {status} ({dt:.1f}s)")

    # final summary
    pending = queue.list_packages(status="pending_approval")
    approved = queue.list_packages(status="approved")
    report["summary"] = {
        "pending_approval": len(pending),
        "approved": len(approved),
        "next_action": (
            f"approve with: python scripts_agency/approve.py approve <id>" if pending
            else "no packages waiting" if not approved else "approved packages will post on next run"),
    }
    log(f"run complete: {report['summary']}")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="clip-agency orchestrator")
    ap.add_argument("--stage", default="all",
                    choices=["all", "research", "produce", "seo", "post", "monitor", "learn"])
    ap.add_argument("--source", default=None, help="produce from this video/stream URL")
    ap.add_argument("--limit", type=int, default=None, help="max packages this run")
    args = ap.parse_args()

    if args.stage == "all":
        report = run_all(source=args.source, limit=args.limit)
        import json

        print(json.dumps(report["summary"], indent=2))
        return
    cfg = load_config()
    ensure_dirs()
    fn = {
        "research": lambda: stage_research(cfg),
        "produce": lambda: stage_produce(cfg, source=args.source, limit=args.limit),
        "seo": lambda: stage_seo(cfg),
        "post": lambda: stage_post(cfg),
        "monitor": lambda: stage_monitor(cfg),
        "learn": lambda: stage_learn(cfg),
    }[args.stage]
    import json

    print(json.dumps(fn(), indent=2, ensure_ascii=False, default=str)[:4000])


if __name__ == "__main__":
    main()
