"""Facebook Page Reels poster (Graph API, three-phase video_reels flow).

Posts vertical clips as **Facebook Reels on a Page** (personal profiles
cannot be posted to via API and cannot be monetized - a Page is required
for both). Uses a Page access token; no public video host is needed -
unlike Instagram, this endpoint accepts raw bytes via rupload.

Flow (Meta "Publish Reels" docs):
    1. POST /{FB_PAGE_ID}/video_reels?upload_phase=start
       -> {video_id, upload_url}
    2. POST {upload_url}  (rupload.facebook.com) with headers
       Authorization: OAuth <page token>, offset: "0", file_size: <bytes>
       and the mp4 as the request body
    3. POST /{FB_PAGE_ID}/video_reels?upload_phase=finish
       &video_id=...&video_state=PUBLISHED&description=<caption>

Reels specs: 9:16 (ours: 1080x1920), 3 s - 90 s (ours: 7-30 s).
Permissions: pages_manage_posts + pages_read_engagement (own page works
without App Review in Development mode).

Metrics: GET /{video_id}/video_insights (total_video_views / reactions /
comments / shares); falls back to the video object's summary counts.
Unavailable -> None (base metrics contract).
"""
from __future__ import annotations

import datetime

from ..config import env
from . import oauth
from .base import PosterBase, PosterError

# v21.0 is valid until Jan 21, 2027 (Meta changelog); override via FB_GRAPH_VERSION.
GRAPH_VERSION = env("FB_GRAPH_VERSION") or "v21.0"

DESCRIPTION_MAX = 2200
INSIGHT_METRICS = "total_video_views,total_video_reactions,total_video_comments,total_video_shares"


def _result(post_id: str, url: str) -> dict:
    return {
        "ok": True,
        "post_id": post_id,
        "url": url,
        "posted_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def _failed(error: str, retry: bool = False) -> dict:
    return {"ok": False, "error": error, "retry": retry}


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class FacebookPoster(PosterBase):
    name = "facebook"
    required_env = ("FB_PAGE_ID", "FB_PAGE_ACCESS_TOKEN")

    # ---- endpoints -------------------------------------------------------
    @staticmethod
    def _base() -> str:
        return f"https://graph.facebook.com/{GRAPH_VERSION}"

    def _page_id(self) -> str:
        return env("FB_PAGE_ID") or ""

    def _token(self) -> str:
        return env("FB_PAGE_ACCESS_TOKEN") or ""

    # ---- captions --------------------------------------------------------
    def caption(self, package: dict) -> dict:
        """Injected captions first, captions/<platform>.json as fallback."""
        cap = package.get("_captions", {}).get(self.name) or {}
        if not cap:
            from ..queue import get_caption

            cap = get_caption(package["id"], self.name)
        return cap or {}

    def _description(self, package: dict) -> str:
        cap = self.caption(package)
        text = str(cap.get("caption") or cap.get("title") or package.get("hook") or "").strip()
        desc = str(cap.get("description") or "").strip()
        if desc and desc not in text:
            text = f"{text}\n\n{desc}".strip()
        tags = cap.get("hashtags") or self.cfg["seo"]["hashtags"].get("facebook") or []
        tag_str = " ".join(str(t) if str(t).startswith("#") else f"#{t}" for t in tags).strip()
        if tag_str and tag_str.lower() not in text.lower():
            text = f"{text}\n\n{tag_str}".strip()
        return text[:DESCRIPTION_MAX]

    # ---- upload ----------------------------------------------------------
    def upload(self, package: dict) -> dict:
        if not self.is_configured():
            return _failed(f"facebook not configured, missing env: {self.missing_config()}")
        problems = self.validate(package)
        if problems:
            return _failed("validation failed: " + "; ".join(problems))
        try:
            if self.package_is_image(package):
                return self._upload_photo(package)
            return self._upload(package)
        except oauth.RetryableError as exc:
            return _failed(str(exc), retry=True)
        except PosterError as exc:
            return _failed(str(exc))

    # ---- image posts -------------------------------------------------------
    def _upload_photo(self, package: dict) -> dict:
        """Photo post via /{page-id}/photos — raw bytes, no public host needed."""
        media = self.video_path(package)
        caption = self._description(package)
        with media.open("rb") as fh:
            resp = oauth.request_with_retry(
                "POST",
                f"{self._base()}/{self._page_id()}/photos",
                params={"access_token": self._token()},
                data={"caption": caption[:DESCRIPTION_MAX]},
                files={"source": (media.name, fh, "image/jpeg")},
                timeout=oauth.TIMEOUT_UPLOAD,
            )
        if resp.status_code >= 400:
            raise PosterError(
                f"facebook photo post failed: HTTP {resp.status_code}: "
                f"{oauth.body_snippet(resp)}"
            )
        data = resp.json() or {}
        post_id = str(data.get("post_id") or data.get("id") or "")
        if not post_id:
            raise PosterError(
                f"facebook photo post returned no id: {oauth.body_snippet(resp)}"
            )
        fbid = str(data.get("id") or post_id.split("_")[-1])
        return _result(post_id, f"https://www.facebook.com/photo/?fbid={fbid}")

    def _start(self) -> tuple[str, str]:
        resp = oauth.request_with_retry(
            "POST",
            f"{self._base()}/{self._page_id()}/video_reels",
            params={
                "upload_phase": "start",
                "access_token": self._token(),
            },
        )
        if resp.status_code >= 400:
            raise PosterError(
                f"facebook reels start failed: HTTP {resp.status_code}: "
                f"{oauth.body_snippet(resp)}"
            )
        data = resp.json() or {}
        video_id = str(data.get("video_id") or "")
        upload_url = str(data.get("upload_url") or "")
        if not video_id or not upload_url:
            raise PosterError(
                f"facebook reels start returned no video_id/upload_url: "
                f"{oauth.body_snippet(resp)}"
            )
        return video_id, upload_url

    def _transfer(self, upload_url: str, video) -> None:
        size = video.stat().st_size
        with video.open("rb") as fh:
            resp = oauth.request_with_retry(
                "POST",
                upload_url,
                data=fh.read(),
                headers={
                    "Authorization": f"OAuth {self._token()}",
                    "offset": "0",
                    "file_size": str(size),
                    "Content-Type": "application/octet-stream",
                },
                timeout=oauth.TIMEOUT_UPLOAD,
            )
        if resp.status_code >= 400:
            raise PosterError(
                f"facebook reels transfer failed: HTTP {resp.status_code}: "
                f"{oauth.body_snippet(resp)}"
            )
        if not ((resp.json() or {}).get("success") or resp.status_code == 200):
            raise PosterError(
                f"facebook reels transfer not confirmed: {oauth.body_snippet(resp)}"
            )

    def _finish(self, video_id: str, package: dict) -> None:
        resp = oauth.request_with_retry(
            "POST",
            f"{self._base()}/{self._page_id()}/video_reels",
            params={
                "upload_phase": "finish",
                "video_id": video_id,
                "video_state": "PUBLISHED",
                "description": self._description(package),
                "access_token": self._token(),
            },
        )
        if resp.status_code >= 400:
            raise PosterError(
                f"facebook reels finish failed: HTTP {resp.status_code}: "
                f"{oauth.body_snippet(resp)}"
            )

    def _upload(self, package: dict) -> dict:
        video = self.video_path(package)
        video_id, upload_url = self._start()
        self._transfer(upload_url, video)
        self._finish(video_id, package)
        return _result(video_id, f"https://www.facebook.com/reel/{video_id}")

    # ---- metrics ---------------------------------------------------------
    def metrics(self, post: dict) -> dict:
        video_id = str(post.get("post_id") or "")
        if not video_id:
            raise PosterError("facebook metrics: post_id missing")
        values = self._insights(video_id)
        if not values:
            values = self._video_fields(video_id)
        return {
            "views": values.get("views"),
            "likes": values.get("likes"),
            "comments": values.get("comments"),
            "shares": values.get("shares"),
            "saves": None,  # Facebook exposes no save count on video insights
            "followers": None,
        }

    def _insights(self, video_id: str) -> dict:
        resp = oauth.request_with_retry(
            "GET",
            f"{self._base()}/{video_id}/video_insights",
            params={"metric": INSIGHT_METRICS, "access_token": self._token()},
        )
        if resp.status_code >= 400:
            return {}
        out: dict = {}
        for row in resp.json().get("data") or []:
            vals = row.get("values") or []
            if not vals:
                continue
            name = str(row.get("name") or "")
            raw = vals[0].get("value")
            if name == "total_video_views":
                out["views"] = _as_int(raw)
            elif name == "total_video_reactions":
                out["likes"] = _as_int(raw)
            elif name == "total_video_comments":
                out["comments"] = _as_int(raw)
            elif name == "total_video_shares":
                out["shares"] = _as_int(raw)
        return out

    def _video_fields(self, video_id: str) -> dict:
        resp = oauth.request_with_retry(
            "GET",
            f"{self._base()}/{video_id}",
            params={
                "fields": "views,likes.summary(true),comments.summary(true),shares",
                "access_token": self._token(),
            },
        )
        if resp.status_code >= 400:
            return {}
        data = resp.json() or {}
        likes = ((data.get("likes") or {}).get("summary") or {}).get("total_count")
        comments = ((data.get("comments") or {}).get("summary") or {}).get("total_count")
        shares = (data.get("shares") or {}).get("count")
        return {
            "views": _as_int(data.get("views")),
            "likes": _as_int(likes),
            "comments": _as_int(comments),
            "shares": _as_int(shares),
        }
