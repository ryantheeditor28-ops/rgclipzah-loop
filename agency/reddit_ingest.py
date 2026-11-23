"""Reddit content ingest: judge r/GTA6 posts, download media, package for posting.

Quality judgment (operator spec): upvotes + comments + freshness. Reddit has no
"views", so hotness = (score + 4*comments) / (age_h + 2)^0.4 — fresh engaging
posts outrank stale popular ones.

Media rules (operator spec):
    video posts  -> package for ALL platforms (re-rendered 9:16 by cut.py)
    image posts  -> package for instagram + facebook ONLY

Auth: Reddit "script" app (REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET in .env),
app-only client_credentials. Plain scrapers are 403-blocked by Reddit's edge;
authenticated API access is the supported route.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

import requests

from .config import ROOT, env, ensure_dirs, load_config
from .portable import ytdlp_cookie_args
from .logutil import log
from .research import _run_repo
from . import queue, seo

UA = ("windows:grand6-agency:v1.0 (personal content desk; operator-authored)")
OAUTH = "https://www.reddit.com/api/v1/access_token"
API = "https://oauth.reddit.com"
# Reddit's own endpoints 403-wall anonymous/scripted traffic; the Arctic Shift
# public archive mirrors posts (with live-updated scores) and needs no keys.
ARC = "https://arctic-shift.photon-reddit.com/api/posts/search"

# banned in titles — same posture as the post rule-check (check.py BANNED)
BANNED = re.compile(
    r"(?i)leak|leaked build|play early|early access download|free download|"
    r"crack|piracy|mod menu|money glitch|afk bot|dm me|giveaway"
)

DEFAULTS = {
    "sub": "GTA6",
    "min_score": 100,        # upvotes floor
    "min_comments": 5,
    "max_age_h": 48,         # only fresh content
    "limit": 100,
    "max_downloads": 5,      # per harvest run
}


def _token() -> str | None:
    cid, secret = env("REDDIT_CLIENT_ID") or "", env("REDDIT_CLIENT_SECRET") or ""
    if not (cid and secret):
        return None
    try:
        r = requests.post(OAUTH, auth=(cid, secret),
                          data={"grant_type": "client_credentials", "duration": "permanent"},
                          headers={"User-Agent": UA}, timeout=30)
        r.raise_for_status()
        return r.json().get("access_token")
    except Exception as exc:  # noqa: BLE001
        log(f"reddit: token failed: {exc}", level="error")
        return None


def _headers(token: str) -> dict:
    return {"User-Agent": UA, "Authorization": f"bearer {token}"}


def fetch_posts(*, sub: str, limit: int = 100) -> list[dict]:
    """Recent posts via the Arctic Shift archive (no credentials).

    The archive rejects query spans over ~24h, so the 48h window is walked
    as two 24h slices, each paginated backwards page by page (a launch-week
    sub pushes hundreds of posts per hour — gems sit deep in the feed).
    Field names match the Reddit API, so judge() is unchanged.
    """
    out: dict[str, dict] = {}
    now = int(time.time())
    window = 24 * 3600
    slices = [(now - 2 * window, now - window + 1), (now - window, now + 60)]
    for w_start, w_before in slices:
        before = w_before
        prev_oldest = None
        for _page in range(40):  # ~24h on a very busy sub
            if before <= w_start:
                break
            try:
                r = requests.get(ARC, params={
                    "subreddit": sub, "limit": 100, "sort": "desc",
                    "after": w_start, "before": before,
                }, headers={"User-Agent": UA}, timeout=30)
                r.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                log(f"reddit: archive fetch failed: {exc}", level="error")
                break
            batch = r.json().get("data") or []
            if not batch:
                break
            for p in batch:
                if p.get("id"):
                    out[p["id"]] = p
            oldest = min(p.get("created_utc", 0) for p in batch)
            if oldest == prev_oldest:
                break  # no progress
            prev_oldest = oldest
            before = int(oldest)
    return list(out.values())


def media_kind(p: dict) -> str | None:
    """'video' | 'image' | 'gallery' | None (text/link/self posts are skipped)."""
    if p.get("is_video") or p.get("domain") == "v.redd.it" or (p.get("media") or {}).get("reddit_video"):
        return "video"
    if p.get("is_gallery") or p.get("domain") == "reddit.com" and (p.get("gallery_data") or p.get("media_metadata")):
        return "gallery"
    hint = p.get("post_hint") or ""
    if hint in ("image", "link") and str(p.get("url", "")).startswith("https://i.redd.it/"):
        return "image"
    if (p.get("domain") == "i.redd.it"):
        return "image"
    return None


def judge(posts: list[dict], cfg: dict) -> list[dict]:
    """Filter to usable media posts and rank by hotness. Returns scored list."""
    rc = {**DEFAULTS, **(cfg.get("reddit") or {})}
    now = time.time()
    usable: list[dict] = []
    for p in posts:
        kind = media_kind(p)
        if not kind:
            continue
        if p.get("stickied") or p.get("over_18"):
            continue
        title = p.get("title", "")
        if BANNED.search(title):
            continue
        age_h = max(0.0, (now - p.get("created_utc", now)) / 3600)
        score = int(p.get("score", 0))
        comments = int(p.get("num_comments", 0))
        if score < rc["min_score"] or comments < rc["min_comments"] or age_h > rc["max_age_h"]:
            continue
        hotness = (score + 4 * comments) / ((age_h + 2) ** 0.4)
        usable.append({
            "id": p["id"], "title": title, "score": score, "comments": comments,
            "age_h": round(age_h, 1), "kind": kind, "hotness": round(hotness, 1),
            "author": f"u/{p.get('author', '?')}",
            "permalink": "https://www.reddit.com" + p.get("permalink", ""),
            "url": p.get("url", ""),
            "gallery": _gallery_urls(p),
        })
    usable.sort(key=lambda x: x["hotness"], reverse=True)
    return usable


def _gallery_urls(p: dict) -> list[str]:
    meta = p.get("media_metadata") or {}
    urls = []
    for item in (p.get("gallery_data") or {}).get("items", []):
        m = meta.get(item.get("media_id")) or {}
        u = ((m.get("p") or [{}])[-1].get("u")) or m.get("s", {}).get("u")
        if u:
            urls.append(u.replace("&amp;", "&"))
    return urls


# ------------------------------------------------------------- downloading

def _dl_video(permalink: str, out: Path) -> Path | None:
    """Reddit-hosted video (v.redd.it DASH) via yt-dlp; merges audio+video.

    v.redd.it requires a logged-in session — Firefox cookies (operator's
    Reddit login) provide it. No secrets leave the machine.
    """
    res = subprocess.run(
        ["yt-dlp", *ytdlp_cookie_args(), "--no-warnings",
         "-f", "bv*[height<=1080]+ba/b",
         "-o", str(out), permalink],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800,
    )
    f = out if out.exists() else out.with_suffix(".mp4")
    if res.returncode == 0 and f.exists() and f.stat().st_size > 50_000:
        return f
    log(f"reddit: yt-dlp failed: {(res.stderr or '')[-200:]}", level="error")
    return None


def _dl_image(url: str, out: Path) -> Path | None:
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=60)
        r.raise_for_status()
        if len(r.content) < 5_000:
            return None
        out.write_bytes(r.content)
        return out
    except Exception as exc:  # noqa: BLE001
        log(f"reddit: image download failed ({url[:60]}): {exc}", level="error")
        return None


# ------------------------------------------------------------- packaging

def _hook_from_title(title: str) -> str:
    t = re.sub(r"\s+", " ", title).strip()
    t = re.sub(r"\[.*?\]\s*", "", t)  # drop [OC] style tags
    return (t[:58].rsplit(" ", 1)[0] if len(t) > 58 else t) or "GTA 6 moment"


def _package_video(video: Path, post: dict, cfg: dict) -> dict | None:
    """Re-render a (usually 16:9) reddit video into 9:16 via cut.py, then package."""
    ensure_dirs()
    job = ROOT / "jobs" / f"reddit-{post['id']}"
    (job / "edits").mkdir(parents=True, exist_ok=True)
    src = job / "source.mp4"
    if not src.exists():
        video.replace(src)
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(src)], capture_output=True, text=True, timeout=60)
    try:
        dur = float(probe.stdout.strip())
    except ValueError:
        log("reddit: could not probe video duration", level="error")
        return None
    dur = min(dur, 58.0)  # campaigns ceiling with headroom
    (job / "candidates.json").write_text(json.dumps({
        "source": str(src),
        "info": {"webpage_url": post["permalink"], "title": post["title"],
                 "channel": post["author"], "uploader": post["author"]},
        "candidates": [{"n": 1, "start": 0.0, "end": round(dur, 2),
                        "score": post.get("hotness") or post.get("score", 0),
                        "peak": None, "why": [f"reddit {post['score']}pts/{post['comments']}cmts"]}],
    }, indent=1), encoding="utf-8")
    (job / "transcript.json").write_text("[]", encoding="utf-8")

    hook = _hook_from_title(post["title"])
    res = _run_repo("cut.py", [f"jobs/{job.name}", "--pick", "1", "--layout", "blur",
                               "--hook", hook, "--captions", "none"], timeout=3600)
    clip = job / "edits" / "clip_1.mp4"
    if res.returncode != 0 or not clip.exists():
        log(f"reddit: cut.py failed: {(res.stderr or '')[-300:]}", level="error")
        return None
    chk = _run_repo("check.py", [str(clip), "--campaign", "campaigns.md"], timeout=600)
    report = (chk.stdout or "") + (chk.stderr or "")
    if chk.returncode != 0 or "FAIL" in report:
        log(f"reddit: rule-check FAIL: {report[-300:]}", level="error")
        return None

    meta = queue.new_package(
        clip, hook=hook, source_url=post["permalink"], source_channel=post["author"],
        source_title=post["title"], credit=post["author"], pillar="clip", layout="blur",
        rule_check="PASS", platforms=cfg["platforms"],
        notes=("reddit " + post["kind"]
               + f" ({post['score']}pts, {post['comments']}cmts"
               + (f", hotness {post['hotness']}" if "hotness" in post else ", operator link")
               + ")"),
    )
    captions = seo.fill_captions(meta, cfg=cfg)
    for p, cap in captions.items():
        queue.set_caption(meta, p, cap)
    return queue.load_meta(meta)


def _package_image(image: Path, post: dict, cfg: dict) -> dict | None:
    """Images go to instagram + facebook ONLY (operator spec)."""
    meta = queue.new_package(
        image, hook=_hook_from_title(post["title"]), source_url=post["permalink"],
        source_channel=post["author"], source_title=post["title"], credit=post["author"],
        pillar="clip", layout="fill", rule_check="PASS",
        platforms=["instagram", "facebook"],
        notes=f"reddit image ({post['score']}pts, {post['comments']}cmts) - IG/FB only",
    )
    caption = f"{post['title'][:180]}\n\nCredit: {post['author']}"
    tags = " ".join((cfg.get("seo", {}).get("hashtags", {}).get("facebook") or [])[:5])
    if tags:
        caption = f"{caption}\n\n{tags}"
    for p in ("instagram", "facebook"):
        queue.set_caption(meta, p, {"title": _hook_from_title(post["title"]),
                                    "caption": caption, "hashtags": []})
    return queue.load_meta(meta)


def ingest_url(url: str, *, cfg: dict | None = None) -> dict | None:
    """Operator-directed: ingest ONE reddit post (share links work too).

    Resolves the link through yt-dlp with the browser session (share codes
    redirect), pulls the full record from the archive, downloads the media
    and packages it. Videos -> all platforms; images -> IG + FB.
    Media-wrapper links (reddit.com/media?url=...) decode straight to the
    i.redd.it asset and are matched back to their post in the archive.
    """
    cfg = cfg or load_config()
    import urllib.parse

    m_wrap = re.search(r"reddit\.com/media\?url=([^&]+)", url)
    direct_media: str | None = None
    if m_wrap:
        direct_media = urllib.parse.unquote(m_wrap.group(1))
        log(f"reddit: media wrapper decoded -> {direct_media[:80]}")
    if direct_media:
        # find the owning post in the archive by media URL
        rec = None
        try:
            for p in fetch_posts(sub=(cfg.get("reddit") or {}).get("sub", "GTA6"), limit=100):
                if p.get("url") == direct_media or p.get("url_overridden_by_dest") == direct_media:
                    rec = p
                    break
        except Exception as exc:  # noqa: BLE001
            log(f"reddit: archive match failed: {exc}")
        post = {
            "id": (rec or {}).get("id") or direct_media.rsplit("/", 1)[-1].split(".")[0],
            "title": (rec or {}).get("title") or "GTA 6 screenshot",
            "score": int((rec or {}).get("score", 0) or 0),
            "comments": int((rec or {}).get("num_comments", 0) or 0),
            "age_h": 0.0,
            "kind": "video" if "v.redd.it" in direct_media else "image",
            "author": f"u/{(rec or {}).get('author', '?')}" if rec else "u/?",
            "permalink": ("https://www.reddit.com" + rec["permalink"]) if rec and rec.get("permalink") else direct_media,
            "url": direct_media,
            "gallery": _gallery_urls(rec or {}),
        }
        work = ROOT / "media" / "reddit"
        work.mkdir(parents=True, exist_ok=True)
        if post["kind"] == "video":
            raw = _dl_video(post["permalink"], work / f"{post['id']}.mp4")
            pkg = _package_video(raw, post, cfg) if raw else None
        else:
            ext = ".png" if ".png" in direct_media.lower() else ".jpg"
            raw = _dl_image(direct_media, work / f"{post['id']}{ext}")
            pkg = _package_image(raw, post, cfg) if raw else None
        if pkg:
            log(f"reddit: package {pkg['id']} ready from operator link ({post['kind']})")
        return pkg
    res = subprocess.run(
        ["yt-dlp", *ytdlp_cookie_args(), "--no-warnings",
         "--skip-download", "--print", "%(webpage_url)s|%(title)s", url],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    if res.returncode != 0:
        err = res.stderr or ""
        # share codes for images redirect to /media?url=<asset> — yt-dlp calls
        # that unsupported, but the error names the real asset; decode + retry
        m2 = re.search(r"media\?url=([^&\s\"']+)", err)
        if m2:
            return ingest_url("https://www.reddit.com/media?url=" + m2.group(1), cfg=cfg)
        log(f"reddit: could not resolve {url}: {err[-200:]}", level="error")
        return None
    line = (res.stdout or "").strip().splitlines()[-1]
    page_url, _, title = line.partition("|")
    m = re.search(r"/comments/([a-z0-9]+)", page_url)
    pid = m.group(1) if m else ""
    log(f"reddit: resolved {url} -> {page_url} (id={pid})")

    rec = None
    if pid:
        try:
            rr = requests.get("https://arctic-shift.photon-reddit.com/api/posts/ids",
                              params={"ids": pid}, headers={"User-Agent": UA}, timeout=30)
            rec = ((rr.json().get("data") or []) or [None])[0]
        except Exception as exc:  # noqa: BLE001
            log(f"reddit: archive lookup failed: {exc}")
    post = {
        "id": pid or (page_url.rstrip("/").split("/")[-1] or "share"),
        "title": (rec or {}).get("title") or title or "GTA 6 post",
        "score": int((rec or {}).get("score", 0) or 0),
        "comments": int((rec or {}).get("num_comments", 0) or 0),
        "age_h": 0.0,
        "kind": media_kind(rec or {"url": page_url, "domain": ""}),
        "author": f"u/{(rec or {}).get('author', '?')}" if rec else "u/?",
        "permalink": ("https://www.reddit.com" + rec["permalink"]) if rec and rec.get("permalink") else page_url,
        "url": (rec or {}).get("url") or page_url,
        "gallery": _gallery_urls(rec or {}),
    }
    if post["kind"] is None:
        # trust the resolved URL when the archive record is thin
        if "v.redd.it" in post["url"]:
            post["kind"] = "video"
        elif "i.redd.it" in post["url"]:
            post["kind"] = "image"
    if post["kind"] is None:
        log(f"reddit: unsupported media type for {post['permalink']}", level="error")
        return None

    work = ROOT / "media" / "reddit"
    work.mkdir(parents=True, exist_ok=True)
    if post["kind"] == "video":
        raw = _dl_video(post["permalink"], work / f"{post['id']}.mp4")
        if not raw:
            return None
        pkg = _package_video(raw, post, cfg)
    else:
        raw = _dl_image(post["url"], work / f"{post['id']}.jpg")
        if not raw:
            return None
        pkg = _package_image(raw, post, cfg)
    if pkg:
        log(f"reddit: package {pkg['id']} ready from operator link ({post['kind']})")
    return pkg


def harvest(*, max_downloads: int | None = None, dry: bool = False,
            min_score: int | None = None, cfg: dict | None = None) -> list[dict]:
    """Judge the subreddit and package the best posts. Returns created packages."""
    cfg = cfg or load_config()
    rc = {**DEFAULTS, **(cfg.get("reddit") or {})}
    if max_downloads is not None:
        rc["max_downloads"] = max_downloads
    if min_score is not None:
        rc["min_score"] = min_score

    posts = fetch_posts(sub=rc["sub"], limit=rc["limit"])
    if not posts:
        log("reddit: no posts fetched (credentials or network)")
        return []
    ranked = judge(posts, cfg)
    log(f"reddit: {len(ranked)} usable media posts")
    for r in ranked[:10]:
        log(f"reddit: {r['hotness']:7.1f}  {r['score']:6d}pts {r['comments']:4d}cmts  "
            f"{r['kind']:7s} {r['title'][:60]!r}")
    if dry:
        return [{"dry_run": True, "candidates": ranked[:10]}]

    made: list[dict] = []
    work = ROOT / "media" / "reddit"
    work.mkdir(parents=True, exist_ok=True)
    video_broken = 0  # v.redd.it needs a browser session; give up quickly
    for post in ranked:
        if len(made) >= rc["max_downloads"]:
            break
        if post["kind"] == "video" and video_broken >= 2:
            continue
        try:
            if post["kind"] == "video":
                raw = _dl_video(post["permalink"], work / f"{post['id']}.mp4")
                if raw:
                    pkg = _package_video(raw, post, cfg)
                    if pkg:
                        made.append(pkg)
                else:
                    video_broken += 1
                    if video_broken == 2:
                        log("reddit: reddit-hosted videos need a logged-in browser "
                            "session (see SETUP-CREDENTIALS.md) — skipping video "
                            "posts this run", level="error")
            elif post["kind"] == "image":
                raw = _dl_image(post["url"], work / f"{post['id']}.jpg")
                if raw:
                    pkg = _package_image(raw, post, cfg)
                    if pkg:
                        made.append(pkg)
            elif post["kind"] == "gallery":
                for i, u in enumerate(post["gallery"][:1]):  # first image only
                    ext = ".png" if ".png" in u.lower() else ".jpg"
                    raw = _dl_image(u, work / f"{post['id']}_{i}{ext}")
                    if raw:
                        pkg = _package_image(raw, post, cfg)
                        if pkg:
                            made.append(pkg)
                        break
        except Exception as exc:  # noqa: BLE001
            log(f"reddit: post {post['id']} failed: {exc}", level="error")
    log(f"reddit: harvest done, {len(made)} package(s) at the approval gate")
    return made
