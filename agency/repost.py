"""Repost engine: watch other people's posts (Reddit, TikTok, X) and repost them.

Operator spec:
    image posts -> instagram + facebook
    video posts -> instagram + facebook + youtube + x + tiktok (configured set)

Sources are profiles/subreddits pasted into the Automation tab. Each cycle the
engine lists a source's recent posts, skips everything already seen, downloads
the new media (yt-dlp with the operator's browser session; direct GET for
images) and packages it through the normal outbox queue:

    mode "gate"  package waits at the Approval Gate (default - nothing posts
                 without the operator)
    mode "auto"  package is approved and posted to its platforms right away

State lives in repost_state.json next to config.json: seen item keys (dedupe),
the last runs and per-item platform results for the UI.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
import subprocess
import threading
import time
from pathlib import Path

from . import queue, reddit_ingest, seo
from .config import ROOT, load_config
from .portable import ytdlp_cmd, ytdlp_cookie_args, ytdlp_pot_args
from .logutil import log

STATE_PATH = ROOT / "repost_state.json"
WORK = ROOT / "media" / "repost"
UA = reddit_ingest.UA

PLATFORMS = ("youtube", "tiktok", "instagram", "facebook", "x")

_DEFAULTS = {
    "sources": [],
    "mode": "gate",
    "video_platforms": ["instagram", "facebook", "youtube", "x", "tiktok"],
    "image_platforms": ["instagram", "facebook"],
    "interval_minutes": 60,
    "enabled": False,
    "daily_cap": 12,
    "daily_image_cap": 8,
    "daily_video_cap": 8,
    "max_age_h": 48,
    "min_post_gap_h": 0,
    "post_gap_slack_min": 15,
}

_lock = threading.Lock()          # one download/post cycle at a time
_running = False                  # mirrored into state for the UI


# ----------------------------------------------------------------- state

def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def load_state() -> dict:
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - missing/corrupt state starts fresh
        state = {}
    state.setdefault("config", dict(_DEFAULTS))
    state["config"] = normalize_config(state["config"])
    state.setdefault("seen", {})
    state.setdefault("runs", [])
    state.setdefault("items", [])
    state.setdefault("last_run", "")
    return state


def _save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(STATE_PATH)


def normalize_config(raw: dict) -> dict:
    cfg = {**_DEFAULTS, **(raw or {})}
    cfg["mode"] = "auto" if cfg.get("mode") == "auto" else "gate"
    for key in ("video_platforms", "image_platforms"):
        want = cfg.get(key) or _DEFAULTS[key]
        cfg[key] = [p for p in dict.fromkeys(want) if p in PLATFORMS]
    if not cfg["video_platforms"]:
        cfg["video_platforms"] = list(_DEFAULTS["video_platforms"])
    if not cfg["image_platforms"]:
        cfg["image_platforms"] = list(_DEFAULTS["image_platforms"])
    for key, lo, hi in (("interval_minutes", 15, 10080), ("daily_cap", 1, 999),
                        ("daily_image_cap", 1, 999), ("daily_video_cap", 1, 999), ("max_age_h", 1, 720),
                        ("min_post_gap_h", 0, 168),
                        ("post_gap_slack_min", 0, 120)):
        try:
            cfg[key] = int(cfg.get(key, _DEFAULTS[key]))
        except (TypeError, ValueError):
            cfg[key] = _DEFAULTS[key]
        cfg[key] = max(lo, min(hi, cfg[key]))
    cfg["enabled"] = bool(cfg.get("enabled"))
    sources = []
    for s in cfg.get("sources") or []:
        if not isinstance(s, dict) or not s.get("url"):
            continue
        kind, ident = parse_source(str(s["url"]))
        if not kind:
            continue
        try:
            per_run = max(1, min(10, int(s.get("max_per_run", 3))))
        except (TypeError, ValueError):
            per_run = 3
        try:
            min_score = max(0, int(s.get("min_score", 100)))
        except (TypeError, ValueError):
            min_score = 100
        sources.append({
            "id": s.get("id") or hashlib.sha1(f"{kind}:{ident}".encode()).hexdigest()[:8],
            "kind": kind, "url": s["url"], "ident": ident,
            "enabled": s.get("enabled", True), "max_per_run": per_run, "min_score": min_score,
            "edit": bool(s.get("edit", True)),
        })
    cfg["sources"] = sources
    return cfg


_DIM_FAIL = re.compile(r"not 9:16|aspect ratio is not supported|no video stream", re.I)


def _safe_name(key: str) -> str:
    return re.sub(r"[^\w-]", "", key)[:60] or hashlib.sha1(key.encode()).hexdigest()[:12]


def _raw_media_path(key: str) -> Path | None:
    """The downloaded media for a grab, if it is still on disk (raw, cached or edit-source)."""
    safe = _safe_name(key)
    for candidate in (WORK / f"{safe}.mp4", WORK / f"{safe}.jpg", WORK / f"{safe}.png",
                      ROOT / "jobs" / f"repost-{safe}" / "source.mp4"):
        if candidate.exists() and candidate.stat().st_size > 5_000:
            return candidate
    return None


def public_state() -> dict:
    """Everything the Automation tab needs, logs trimmed."""
    state = load_state()
    items = state["items"][-40:]
    for it in items:
        blob = it.get("detail", "") + " " + " ".join(str(r.get("error", "")) for r in it.get("results", []))
        it["reeditable"] = (it.get("status") == "failed" and bool(_DIM_FAIL.search(blob))
                            and _raw_media_path(it.get("key", "")) is not None)
    state["items"] = items
    state["runs"] = state["runs"][-15:]
    state["running"] = _running
    state["next_due"] = _next_due(state)
    return state


def _next_due(state: dict) -> str:
    if not state["config"].get("enabled") or not state.get("last_run"):
        return ""
    try:
        base = datetime.datetime.fromisoformat(state["last_run"])
    except ValueError:
        return ""
    return (base + datetime.timedelta(minutes=state["config"]["interval_minutes"])).isoformat(timespec="seconds")


def save_config(raw: dict) -> dict:
    with _lock:
        state = load_state()
        state["config"] = normalize_config(raw)
        _save_state(state)
    log(f"repost: config saved ({len(state['config']['sources'])} source(s), mode={state['config']['mode']})")
    return public_state()


def set_watcher(enabled: bool) -> dict:
    with _lock:
        state = load_state()
        state["config"]["enabled"] = bool(enabled)
        _save_state(state)
    log(f"repost: watcher {'enabled' if enabled else 'paused'}")
    return public_state()


def watcher_due() -> bool:
    """True when the background watcher in the web app should fire a cycle."""
    if _running:
        return False
    state = load_state()
    if not state["config"].get("enabled"):
        return False
    due = _next_due(state)
    return bool(due) and datetime.datetime.now().astimezone() >= datetime.datetime.fromisoformat(due)


# ----------------------------------------------------------------- sources

def parse_source(url: str) -> tuple[str, str] | tuple[None, None]:
    """('reddit'|'tiktok'|'x'|'youtube'|'facebook', identifier) from a profile/subreddit URL."""
    u = url.strip()
    m = re.search(r"reddit\.com/(?:r/)?([A-Za-z0-9_]{2,21})", u)
    if m and "/comments/" not in u and "/media" not in u:
        return "reddit", m.group(1)
    m = re.search(r"tiktok\.com/@([A-Za-z0-9_.]{1,30})", u)
    if m:
        return "tiktok", m.group(1)
    m = re.search(r"(?:x|twitter)\.com/@?([A-Za-z0-9_]{1,15})(?:[/?#]|$)", u)
    if m:
        return "x", m.group(1)
    # youtube channel: /@handle, /channel/UC..., /c/name, /user/name
    m = re.search(r"youtube\.com/(?:@([A-Za-z0-9_.-]{3,40})|channel/([A-Za-z0-9_-]{10,})"
                  r"|(?:c|user)/([A-Za-z0-9_.-]{2,40}))", u)
    if m and ("watch?v=" not in u and not re.search(r"/shorts/[A-Za-z0-9_-]{6,}", u)):
        return "youtube", next(g for g in m.groups() if g)
    # facebook page (profile or videos/reels tab)
    m = re.search(r"facebook\.com/(?:profile\.php\?id=([0-9]{5,})|([A-Za-z0-9.\-]{3,60}?))(?:/videos|/reels)?(?:[/?#]|$)", u)
    if m and "watch" not in u and "/share/" not in u and "/reel/" not in u:
        return "facebook", m.group(1) or m.group(2)
    return None, None


def _run_ytdlp(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*ytdlp_cmd(), *ytdlp_cookie_args(), *ytdlp_pot_args(), "--no-warnings", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )


_SEP = " §§ "  # titles can contain pipes; this separator survives them


def _listing_url(kind: str, url: str, ident: str) -> str:
    """The URL yt-dlp should flat-list for a source profile."""
    if kind == "youtube":
        tab = "shorts" if url.rstrip("/").endswith("/shorts") else "videos"
        if ident.startswith("UC"):
            return f"https://www.youtube.com/channel/{ident}/{tab}"
        if "/@" in url or f"/@{ident}" in url:
            return f"https://www.youtube.com/@{ident}/{tab}"
        return url  # /c/ and /user/ pages list fine as given
    if kind == "facebook" and not re.search(r"/(videos|reels)(?:[/?#]|$)", url):
        return f"https://www.facebook.com/{ident}/videos"
    return url


def _discover_profile(kind: str, url: str, max_age_h: int) -> list[dict]:
    """Recent posts of a TikTok / X / YouTube / Facebook profile via yt-dlp.

    --dump-single-json (not --print templates): yt-dlp pipes template output in
    the ANSI codepage on Windows (a § arrives as byte 0xA7, which UTF-8 decoding
    turns into U+FFFD and silently breaks line parsing), while JSON is
    ASCII-escaped and survives every codepage.
    """
    ident0 = (parse_source(url) or (None, None))[1] or ""
    res = _run_ytdlp([
        "--flat-playlist", "--playlist-end", "30", "--no-playlist-reverse", "--dump-single-json",
        _listing_url(kind, url, ident0),
    ], timeout=300)
    entries: list[dict] = []
    if (res.stdout or "").strip():
        try:
            entries = (json.loads(res.stdout).get("entries") or []) if res.stdout.lstrip().startswith("{") \
                else json.loads(res.stdout)
        except ValueError:
            entries = []
    if not entries and res.returncode != 0:
        why = (res.stderr or "").strip().splitlines()[-1][:180] if (res.stderr or "").strip() else "no output"
        hint = " — paste one of their post links into 'Grab & repost' instead" \
            if "Unsupported URL" in why or "blocked" in why.lower() else ""
        raise RuntimeError(f"could not list {url}: {why}{hint}")
    cutoff = time.time() - max_age_h * 3600
    ident = (parse_source(url) or (None, None))[1] or ""
    items, seen_ids = [], set()
    for e in entries:
        if not isinstance(e, dict):
            continue
        item_id = str(e.get("id") or "").strip()
        if not item_id or item_id in ("NA", "None"):
            continue
        try:
            stamp = float(e.get("timestamp") or 0.0)
        except (TypeError, ValueError):
            stamp = 0.0
        if stamp and stamp < cutoff:
            continue  # older than the freshness window
        page = str(e.get("webpage_url") or "")
        if not page.startswith("http"):
            # flat listings sometimes carry a bare id - rebuild the post URL
            page = (f"https://www.tiktok.com/@{ident}/video/{item_id}" if kind == "tiktok"
                    else f"https://www.youtube.com/watch?v={item_id}" if kind == "youtube"
                    else f"https://www.facebook.com/{ident}/videos/{item_id}" if kind == "facebook"
                    else f"https://x.com/{ident}/status/{item_id}")
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        try:
            views_n = int(float(e.get("view_count") or 0))
        except (TypeError, ValueError):
            views_n = 0
        items.append({
            "key": f"{kind}:{item_id}",
            "title": (str(e.get("title") or "") or f"{kind} post {item_id}")[:120],
            "url": page, "author": f"@{ident}",
            "kind": "video", "views": views_n, "when": stamp,
        })
    items.sort(key=lambda x: (x["views"], x["when"]), reverse=True)
    return items


def discover(source: dict, cfg: dict) -> list[dict]:
    """Fresh, unseen candidates for one source, best first."""
    if source["kind"] == "reddit":
        posts = reddit_ingest.fetch_posts(sub=source["ident"], limit=100)
        rc = dict(reddit_ingest.DEFAULTS)
        rc.update(min_score=source.get("min_score", 100), max_age_h=cfg["max_age_h"])
        ranked = reddit_ingest.judge(posts, {"reddit": rc})
        return [{
            "key": f"reddit:{r['id']}", "title": r["title"], "url": r["url"],
            "permalink": r["permalink"], "author": r["author"], "kind": r["kind"],
            "gallery": r["gallery"], "views": r["score"], "when": 0.0,
        } for r in ranked]
    return _discover_profile(source["kind"], source["url"], cfg["max_age_h"])


# ----------------------------------------------------------------- download

def _download_video(page_url: str, out: Path) -> tuple[Path | None, str]:
    """Fetch one post's video (+ its metadata json for the caption engine).
    Returns (file, failure reason)."""
    if out.exists() and out.stat().st_size > 50_000:
        return out, ""  # already on disk from an earlier attempt
    res = _run_ytdlp(["-f", "bv*[height<=1080]+ba/b", "--merge-output-format", "mp4",
                      "--write-info-json", "--no-write-playlist-metafiles",
                      "-o", str(out), page_url],
                     timeout=1800)
    f = out if out.exists() else out.with_suffix(".mp4")
    if res.returncode == 0 and f.exists() and f.stat().st_size > 50_000:
        return f, ""
    why = (res.stderr or "").strip().splitlines()[-1][:200] if (res.stderr or "").strip() else "yt-dlp returned nothing"
    log(f"repost: yt-dlp download failed: {why}", level="error")
    return None, why


def _enrich_from_info(item: dict, out: Path) -> None:
    """Fold the real title/uploader/description from yt-dlp's info.json into the
    item — flat playlist listings often carry hashtag-only titles ('#movie')."""
    try:
        info_p = out.with_suffix(".info.json")
        if not info_p.exists():
            return
        info = json.loads(info_p.read_text(encoding="utf-8", errors="replace"))
        if str(info.get("title") or "").strip():
            item["title"] = str(info["title"])[:120]
        if str(info.get("description") or "").strip():
            item["description"] = str(info["description"])[:2000]
        up = str(info.get("uploader") or info.get("channel") or "").strip()
        if up:
            item["author"] = up if up.startswith("@") else f"@{up}"
    except Exception as exc:  # noqa: BLE001 - metadata is a bonus, never fatal
        log(f"repost: info.json parse failed for {item.get('key')}: {exc!r}")


def _download(item: dict) -> tuple[Path | None, str]:
    """Download the item's media. Returns (file, failure reason)."""
    WORK.mkdir(parents=True, exist_ok=True)
    safe = _safe_name(item["key"])
    if item["kind"] == "video":
        out = WORK / f"{safe}.mp4"
        f, why = _download_video(item.get("permalink") or item["url"] if item["key"].startswith("reddit:")
                                 else item["url"], out)
        if f:
            _enrich_from_info(item, out)
        return f, why
    url = (item.get("gallery") or [item["url"]])[0]
    ext = ".png" if ".png" in url.lower() else ".jpg"
    f = reddit_ingest._dl_image(url, WORK / f"{safe}{ext}")
    return (f, "" if f else "image host unreachable or post removed")


def _is_image(file: Path) -> bool:
    try:
        from .poster.base import probe_is_image

        return probe_is_image(file)
    except Exception:  # noqa: BLE001
        return False


# ----------------------------------------------------------------- packaging

def _hook(title: str) -> str:
    return reddit_ingest._hook_from_title(title)


def _edit_video(file: Path, item: dict) -> Path | None:
    """Re-render a downloaded video with the bundled editclip transformer:
    branded 1080x1920 edit (punch-in or blur layout + hook + brand bug +
    loudnorm). Self-contained: runs on this PC and on Actions runners alike.
    Returns the edited clip, or None (reason in the logs)."""
    from .config import ensure_dirs
    from . import editclip

    ensure_dirs()
    safe = _safe_name(item["key"])
    job = ROOT / "jobs" / f"repost-{safe}"
    (job / "edits").mkdir(parents=True, exist_ok=True)
    src = job / "source.mp4"
    if not src.exists():
        file.replace(src)
    try:
        dur = float(subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(src)], capture_output=True, text=True,
            timeout=60).stdout.strip() or 0)
    except ValueError:
        dur = 0.0
    (job / "candidates.json").write_text(json.dumps({
        "source": str(src),
        "info": {"webpage_url": item.get("permalink") or item["url"], "title": item["title"],
                 "channel": item.get("author", ""), "uploader": item.get("author", ""),
                 "description": item.get("description", "")},
        "candidates": [{"n": 1, "start": 0.0, "end": round(min(dur or 58.0, 58.0), 2),
                        "score": int(item.get("views") or 0), "peak": None,
                        "why": [f"rgclipzah edit of {item['key'].split(':', 1)[0]}"]}],
    }, indent=1), encoding="utf-8")
    return editclip.render(src, job / "edits" / "clip_1.mp4", hook=_hook(item["title"]))


def _ig_accepts(file: Path) -> bool:
    """Instagram image containers take ~4:5 through 1.91:1; ultra-wide shots fail."""
    try:
        from .media_probe import probe_streams

        v = [s for s in probe_streams(str(file)).get("streams", []) if s.get("codec_type") == "video"]
        if not v or not v[0].get("width") or not v[0].get("height"):
            return True  # cannot tell - let the API decide
        ratio = v[0]["width"] / v[0]["height"]
        return 0.5 <= ratio <= 1.91
    except Exception:  # noqa: BLE001
        return True


def _package(item: dict, file: Path, rcfg: dict, agency: dict, *, edited: bool = False) -> dict:
    image = _is_image(file)
    platforms = rcfg["image_platforms"] if image else rcfg["video_platforms"]
    wide = image and not _ig_accepts(file)
    if wide:
        platforms = [p for p in platforms if p != "instagram"]
    meta = queue.new_package(
        file, hook=_hook(item["title"]), source_url=item.get("permalink") or item["url"],
        source_channel=item.get("author", ""), source_title=item["title"],
        source_description=item.get("description", ""),
        credit=item.get("author", ""), pillar="clip",
        layout="fill" if image else "blur", rule_check="PASS", platforms=platforms,
        notes=(f"repost {item['kind']} via {item['key'].split(':', 1)[0]} "
               f"({'grand6 edit' if edited else 'raw'})"
               + (", ultra-wide image so facebook only" if wide else "")),
    )
    # every post gets the strategist captions (hook, original description,
    # credit, CTA, platform-tuned hashtags) - images and videos alike
    for p, cap in seo.fill_captions(meta, cfg=agency).items():
        if p in platforms:
            queue.set_caption(meta, p, cap)
    return meta


def _post_now(meta: dict, cfg: dict) -> list[dict]:
    from . import orchestrator

    queue.approve(meta["id"])
    return orchestrator.post_one(meta["id"], cfg)


# ----------------------------------------------------------------- cycles

def _qkind(kind: str) -> str:
    """Quota bucket: images and galleries share the image cap."""
    return "image" if kind in ("image", "gallery") else "video"


def _posted_today(state: dict, qkind: str | None = None) -> int:
    today = datetime.date.today().isoformat()
    return sum(1 for it in state["items"]
               if it.get("at", "").startswith(today) and it.get("status") in ("posted", "packaged")
               and (qkind is None or _qkind(it.get("kind", "")) == qkind))


def _last_post_at(state: dict) -> datetime.datetime | None:
    """When the engine last produced a post (auto sends or gate approvals).

    Items are stamped with the local offset of whatever machine posted them
    (the Actions runner writes UTC, this PC writes +08:00), so the newest post
    is the newest *instant* - not the biggest ISO string. Comparing strings
    used to mis-read a UTC post from the runner as older than a local one and
    let the throttle fire an hour early.
    """
    best: datetime.datetime | None = None
    for it in state["items"]:
        if it.get("status") != "posted":
            continue
        raw = it.get("at") or ""
        if not raw:
            continue
        try:
            when = datetime.datetime.fromisoformat(raw)
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.astimezone()
        if best is None or when > best:
            best = when
    return best


def _effective_gap_h(cfg: dict) -> float:
    """The gap we actually enforce: nominal gap minus a tolerance band.

    The external trigger (cron-job.org) fires on a fixed grid - every 2
    hours. A post is not stamped at the instant the trigger fires: discovery,
    download, render and upload take a few minutes, so the recorded
    timestamp lands a minute or two AFTER the slot. Comparing that stamp
    against the next slot with a strict `>= 2h` rejects the next slot and
    pushes the post to the one after it - a 4-hour gap, which is exactly
    the "3 hours, then 12 hours" pattern this loop used to show.

    The tolerance band lets the engine post on the next grid slot instead
    of skipping it, while still refusing to post twice inside one slot.
    """
    try:
        gap = float(cfg.get("min_post_gap_h", 0) or 0)
    except (TypeError, ValueError):
        gap = 0.0
    try:
        slack = float(cfg.get("post_gap_slack_min", 0) or 0) / 60.0
    except (TypeError, ValueError):
        slack = 0.0
    return max(0.0, gap - slack)


def _mark_seen(state: dict, source_id: str, key: str) -> None:
    seen = state["seen"].setdefault(source_id, [])
    if key not in seen:
        seen.append(key)
    state["seen"][source_id] = seen[-400:]


def _attempts(state: dict, key: str) -> int:
    """How many times this item already failed - retried items stop at 3."""
    return sum(1 for it in state["items"] if it.get("key") == key)


def _caption_key(hook: str) -> str:
    """Caption identity: lowercased, every non-word char stripped."""
    return re.sub(r"[^\w]+", "", hook.lower())


def _dup_caption(state: dict, item: dict) -> bool:
    """True when this video's caption would repeat a recently posted one.

    Some source creators title every upload the same, so video-ID dedupe
    alone let one caption ship on post after post. Compare captions instead:
    a fresh video whose caption matches the recent history is skipped."""
    if item.get("kind") != "video":
        return False
    key = _caption_key(_hook(item["title"]))
    return bool(key) and key in state.get("hook_history", [])


def _remember_caption(state: dict, item: dict) -> None:
    """Record a shipped caption so the same text does not repeat soon."""
    key = _caption_key(_hook(item["title"]))
    if not key:
        return
    hist = [k for k in state.get("hook_history", []) if k != key]
    hist.append(key)
    state["hook_history"] = hist[-40:]


def run_cycle(only_source: str | None = None, max_items: int | None = None) -> dict:
    """One harvest+repost pass over every enabled source. Thread-safe."""
    global _running
    with _lock:
        if _running:
            return {"ok": False, "error": "A repost run is already in progress"}
        _running = True
    try:
        return _run_cycle_inner(only_source, max_items)
    finally:
        _running = False


def _run_cycle_inner(only_source: str | None, max_items: int | None) -> dict:
    cfg = load_config()
    rcfg = normalize_config({**load_state()["config"]})
    state = load_state()
    ran_at = _now_iso()
    made, failures = [], []
    made_kinds = {"image": 0, "video": 0}
    skipped = {"image": 0, "video": 0}
    quota_skipped: list[str] = []
    cap_left = rcfg["daily_cap"] - _posted_today(state)
    img_left = rcfg["daily_image_cap"] - _posted_today(state, "image")
    vid_left = rcfg["daily_video_cap"] - _posted_today(state, "video")
    # cap for the WHOLE cycle (owner rule: one post per cycle). It applies to
    # every source AND within a source's item loop - a single source with
    # max_per_run > 1 used to post several videos in one cycle (double-post).
    cycle_limit = cap_left if max_items is None else min(cap_left, max_items)
    if cap_left <= 0 or (img_left <= 0 and vid_left <= 0):
        summary = (f"daily caps reached ({rcfg['daily_cap']}/day"
                   f", {rcfg['daily_image_cap']} image, {rcfg['daily_video_cap']} video) - nothing downloaded")
        state["runs"] = [*state["runs"][-29:], {"at": ran_at, "ok": True, "summary": summary}]
        state["last_run"] = ran_at
        _save_state(state)
        return {"ok": True, "summary": summary, "items": []}

    # anti-spam spacing: in auto mode, keep a minimum gap between consecutive
    # posts so platforms don't flag the account for burst-posting. Unseen items
    # stay fresh - the next cycle after the gap picks them up.
    gap_h = rcfg.get("min_post_gap_h", 0)
    eff_gap_h = _effective_gap_h(rcfg)
    if rcfg["mode"] == "auto" and gap_h > 0:
        last_post = _last_post_at(state)
        if last_post:
            waited = datetime.datetime.now().astimezone() - last_post
            if waited < datetime.timedelta(hours=eff_gap_h):
                summary = (f"post throttle: last post {int(waited.total_seconds() // 60)} min ago, "
                           f"min gap {gap_h}h - waiting before the next send (anti-spam spacing)")
                state["runs"] = [*state["runs"][-29:], {"at": ran_at, "ok": True, "summary": summary}]
                state["last_run"] = ran_at
                _save_state(state)
                return {"ok": True, "summary": summary, "items": []}

    for source in rcfg["sources"]:
        if only_source and source["id"] != only_source:
            continue
        if not source.get("enabled", True):
            continue
        if len(made) >= cycle_limit:
            break
        try:
            found = discover(source, rcfg)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{source['kind']} {source['ident']}: {exc}")
            state["runs"] = [*state["runs"][-29:], {"at": _now_iso(), "ok": False,
                                                    "summary": f"could not list {source['kind']} {source['ident']}",
                                                    "error": str(exc)[:300]}]
            _save_state(state)
            continue
        fresh = [i for i in found if i["key"] not in state["seen"].get(source["id"], [])
                 and _attempts(state, i["key"]) < 3]
        dupes = sum(1 for i in fresh if _dup_caption(state, i))
        fresh = [i for i in fresh if not _dup_caption(state, i)]
        log(f"repost: {source['kind']} {source['ident']}: {len(fresh)} new of {len(found)}"
            + (f", {dupes} skipped (caption repeats a recent post)" if dupes else ""))
        for item in fresh[:source["max_per_run"]]:
            if len(made) >= cycle_limit:
                break
            qk = _qkind(item["kind"])
            if (qk == "image" and made_kinds["image"] >= img_left) or (qk == "video" and made_kinds["video"] >= vid_left):
                skipped[qk] += 1  # this kind's daily quota is used up
                continue
            entry = {"key": item["key"], "source": f"{source['kind']} {source['ident']}",
                     "title": item["title"], "kind": item["kind"], "url": item.get("permalink") or item["url"],
                     "at": _now_iso(), "status": "failed", "package": "", "results": [], "detail": ""}
            try:
                file, why = _download(item)
                if not file:
                    entry["detail"] = why or "download failed"
                else:
                    edited = False
                    if source.get("edit", True) and not _is_image(file):
                        clip = _edit_video(file, item)
                        if clip:
                            file, edited = clip, True
                        else:
                            entry["detail"] = "Grand6 edit failed - see agency logs"
                            file = None
                    if file:
                        meta = _package(item, file, rcfg, cfg, edited=edited)
                        entry["package"] = meta["id"]
                        if rcfg["mode"] == "auto":
                            results = _post_now(meta, cfg)
                            entry["results"] = [{"platform": r.get("platform", ""), "ok": bool(r.get("ok")),
                                                 "url": r.get("url", ""), "error": str(r.get("error", ""))[:160],
                                                 "quota": bool(r.get("quota"))}
                                                for r in results]
                            entry["status"] = "posted" if any(r["ok"] for r in entry["results"]) else "failed"
                            entry["detail"] = "; ".join(f"{r['platform']}: {r['error'] or 'posted'}"
                                                        for r in entry["results"])
                            for r in entry["results"]:
                                if r.get("quota"):
                                    quota_skipped.append(r["platform"])
                                    log(f"repost: {r['platform']} daily quota reached - "
                                        f"skipping {r['platform']} this cycle")
                        else:
                            entry["status"] = "packaged"
                            entry["detail"] = ("edited with Grand6, " if edited else "") + "waiting at the Approval Gate"
            except Exception as exc:  # noqa: BLE001
                entry["detail"] = repr(exc)[:300]
                log(f"repost: item {item['key']} failed: {exc!r}", level="error")
            state["items"] = [*state["items"][-99:], entry]
            if entry["status"] in ("posted", "packaged"):
                _mark_seen(state, source["id"], item["key"])  # failures stay fresh and retry (max 3)
                _remember_caption(state, item)
                made.append(entry)
                made_kinds[qk] += 1
            else:
                failures.append(f"{entry['title'][:50]}: {entry['detail'][:80]}")
            _save_state(state)
    skipped_note = ""
    if skipped["image"] or skipped["video"]:
        skipped_note = (f", {skipped['image'] + skipped['video']} skipped by caps "
                        f"({skipped['image']} image, {skipped['video']} video)")
    if quota_skipped:
        skipped_note += (", " + ", ".join(sorted(set(quota_skipped)))
                         + " skipped (daily quota reached)")
    state["runs"] = [*state["runs"][-29:], {"at": ran_at, "ok": not failures or bool(made),
                                            "summary": f"{len(made)} new post(s) processed "
                                                       f"({made_kinds['image']} image, {made_kinds['video']} video)"
                                                       + (f", {len(failures)} failed" if failures else "")
                                                       + skipped_note}]
    state["last_run"] = ran_at
    _save_state(state)
    return {"ok": True, "summary": f"{len(made)} processed, {len(failures)} failed",
            "items": made, "failures": failures}


def reedit(key: str) -> dict:
    """Give a failed grab another life: videos go through the Grand6 editor
    (9:16) and re-post; images re-package with the current platform routing."""
    global _running
    key = (key or "").strip()
    if not key:
        return {"ok": False, "error": "Missing grab key"}
    with _lock:
        if _running:
            return {"ok": False, "error": "A repost run is already in progress"}
        _running = True
    try:
        state = load_state()
        rcfg = normalize_config({**state["config"]})
        cfg = load_config()
        entry = next((it for it in reversed(state["items"]) if it.get("key") == key), None)
        if not entry:
            return {"ok": False, "error": "That grab is no longer in the list"}
        raw = _raw_media_path(key)
        if not raw:
            return {"ok": False, "error": "The downloaded file is gone — grab it again with 'Grab & repost'"}
        item = {"key": key, "title": entry.get("title", ""), "url": entry.get("url", ""),
                "author": "", "kind": entry.get("kind", "video"), "views": 0, "when": 0.0}
        edited = False
        if not _is_image(raw):
            clip = _edit_video(raw, item)
            if not clip:
                return {"ok": False, "error": "Grand6 edit failed — see agency logs"}
            raw, edited = clip, True
        meta = _package(item, raw, rcfg, cfg, edited=edited)
        new_entry = {**entry, "at": _now_iso(), "status": "packaged", "package": meta["id"],
                     "results": [], "detail": ("re-edited with Grand6, " if edited else "re-packaged, ")
                                             + "waiting at the Approval Gate"}
        if rcfg["mode"] == "auto":
            results = _post_now(meta, cfg)
            new_entry["results"] = [{"platform": r.get("platform", ""), "ok": bool(r.get("ok")),
                                     "url": r.get("url", ""), "error": str(r.get("error", ""))[:160]}
                                    for r in results]
            new_entry["status"] = "posted" if any(r["ok"] for r in new_entry["results"]) else "failed"
            new_entry["detail"] = ("re-edited with Grand6 · " if edited else "") + "; ".join(
                f"{r['platform']}: {r['error'] or 'posted'}" for r in new_entry["results"])
        state["items"] = [*state["items"][-99:], new_entry]
        try:  # remember the source item so the watcher does not grab it again
            kind = key.split(":", 1)[0]
            ident = (entry.get("source", "").split(" ", 1) + [""])[1]
            source = next((s for s in rcfg["sources"] if s["kind"] == kind and s["ident"] == ident), None)
            if source:
                _mark_seen(state, source["id"], key)
        except Exception:  # noqa: BLE001
            pass
        _save_state(state)
        log(f"repost: re-edit {key} -> {new_entry['status']} ({meta['id']})")
        return {"ok": True, "status": new_entry["status"], "package": meta["id"]}
    except Exception as exc:  # noqa: BLE001
        log(f"repost: re-edit {key} failed: {exc!r}", level="error")
        return {"ok": False, "error": repr(exc)[:300]}
    finally:
        _running = False


def ingest_single(url: str, *, edit: bool | None = None, force_auto: bool = False) -> dict:
    """Grab ONE post link (reddit / tiktok / x / youtube ...) and package it now.

    edit=None (Automation card): reddit links take the proven reddit flow, everything
    else is packaged raw and only auto-posts when the repost mode says so.
    edit=True/False (Home toggle): the operator picked the pipeline - AI edit first or
    raw - and force_auto posts the result the moment it is ready.
    """
    global _running
    url = (url or "").strip()
    if not url:
        return {"ok": False, "error": "Paste a post link first"}
    with _lock:
        if _running:
            return {"ok": False, "error": "A repost run is already in progress"}
        _running = True
    try:
        cfg = load_config()
        rcfg = normalize_config({**load_state()["config"]})
        auto = force_auto or rcfg["mode"] == "auto"
        key = f"single:{hashlib.sha1(url.encode()).hexdigest()[:12]}"
        state = load_state()
        if edit is None and (parse_source(url)[0] == "reddit" or "reddit.com" in url):
            # the proven reddit path; hand it our platform routing for videos
            single_cfg = json.loads(json.dumps(cfg))
            single_cfg["platforms"] = list(dict.fromkeys(rcfg["video_platforms"]))
            pkg = reddit_ingest.ingest_url(url, cfg=single_cfg)
            if not pkg:
                return {"ok": False, "error": "Reddit ingest failed (see logs) - image posts route to IG + FB"}
            entry = {"key": key, "source": "operator link",
                     "title": pkg.get("hook", "reddit post"), "kind": "image" if probe_image(pkg) else "video",
                     "url": url, "at": _now_iso(), "status": "packaged", "package": pkg["id"],
                     "results": [], "detail": "waiting at the Approval Gate"}
            state["items"] = [*state["items"][-99:], entry]
            _save_state(state)
            return {"ok": True, "package": pkg["id"], "status": entry["status"]}
        # anything yt-dlp understands: download, optionally edit, classify, package, post
        item = {"key": key, "title": url.rsplit("/", 1)[-1] or url,
                "url": url, "author": "", "kind": "video", "views": 0, "when": 0.0}
        res = _run_ytdlp(["--skip-download", "--dump-single-json", url], timeout=300)
        meta_ok = False
        if res.returncode == 0 and res.stdout.strip():
            try:
                meta_j = json.loads(res.stdout)
                title, uploader = str(meta_j.get("title") or ""), str(meta_j.get("uploader") or "")
                meta_ok = True
            except ValueError:
                title, uploader = "", ""
        if meta_ok:
            if title:
                item["title"] = title[:120]
            if uploader:
                item["author"] = f"@{uploader}" if not str(uploader).startswith("@") else str(uploader)
            if str(meta_j.get("description") or "").strip():
                item["description"] = str(meta_j["description"])[:2000]
        file, why = _download(item)
        if not file:
            return {"ok": False, "error": f"Download failed - {why or 'yt-dlp could not fetch this post'}"}
        edited = False
        if edit and not _is_image(file):
            clip = _edit_video(file, item)
            if not clip:
                return {"ok": False, "error": "Grand6 edit failed - see agency logs"}
            file, edited = clip, True
        meta = _package(item, file, rcfg, cfg, edited=edited)
        entry = {"key": key, "source": "home ingest" if force_auto else "operator link",
                 "title": item["title"], "kind": "image" if probe_image(meta) else "video",
                 "url": url, "at": _now_iso(), "status": "packaged", "package": meta["id"],
                 "results": [],
                 "detail": (("edited with Grand6, " if edited else "") + "waiting at the Approval Gate")}
        if auto:
            results = _post_now(meta, cfg)
            entry["results"] = [{"platform": r.get("platform", ""), "ok": bool(r.get("ok")),
                                 "url": r.get("url", ""), "error": str(r.get("error", ""))[:160]} for r in results]
            entry["status"] = "posted" if any(r["ok"] for r in entry["results"]) else "failed"
            entry["detail"] = ("edited with Grand6 · " if edited else "") + "; ".join(
                f"{r['platform']}: {r['error'] or 'posted'}" for r in entry["results"])
        state["items"] = [*state["items"][-99:], entry]
        _save_state(state)
        log(f"repost: single ingest {url[:60]} -> {entry['status']} ({meta['id']})")
        return {"ok": True, "package": meta["id"], "status": entry["status"]}
    except Exception as exc:  # noqa: BLE001
        log(f"repost: single ingest failed: {exc!r}", level="error")
        return {"ok": False, "error": repr(exc)[:300]}
    finally:
        _running = False


def probe_image(meta_or_id: dict | str) -> bool:
    """True when the packaged media is a still image."""
    try:
        from .poster.base import probe_is_image

        meta = meta_or_id if isinstance(meta_or_id, dict) else queue.load_meta(meta_or_id)
        return probe_is_image(queue.pkg_dir(meta) / meta.get("video", "video.mp4"))
    except Exception:  # noqa: BLE001
        return False
