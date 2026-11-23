"""YouTube Shorts poster (Data API v3, resumable upload).

Flow:
    1. refresh-grant an access token (cached in memory for the process)
    2. POST resumable session (snippet+status metadata) -> Location URL
    3. PUT the mp4 bytes to the Location URL
    4. video id -> https://www.youtube.com/shorts/{id}

Metrics: GET /videos?part=statistics -> views/likes/comments
(shares and saves are not exposed -> None).
"""
from __future__ import annotations

import datetime
from pathlib import Path

from ..config import env
from . import oauth
from .base import PosterBase, PosterError

UPLOAD_INIT_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"

TITLE_MAX = 95
DESCRIPTION_MAX = 4900
CATEGORY_GAMING = "20"
_PRIVACY_STATUS = "public"


def _clip(text: str, limit: int) -> str:
    return text[:limit].strip()


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _result(post_id: str, url: str) -> dict:
    return {
        "ok": True,
        "post_id": post_id,
        "url": url,
        "posted_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def _failed(error: str, retry: bool = False) -> dict:
    return {"ok": False, "error": error, "retry": retry}


class YouTubePoster(PosterBase):
    name = "youtube"
    required_env = ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN")

    # ---- captions --------------------------------------------------------
    def caption(self, package: dict) -> dict:
        """Injected captions first, captions/<platform>.json as fallback."""
        cap = package.get("_captions", {}).get(self.name) or {}
        if not cap:
            from ..queue import get_caption

            cap = get_caption(package["id"], self.name)
        return cap or {}

    def _title(self, package: dict) -> str:
        cap = self.caption(package)
        title = str(cap.get("title") or package.get("hook") or "clip").strip()
        return _clip(title, TITLE_MAX)

    def _description(self, package: dict) -> str:
        cap = self.caption(package)
        desc = str(cap.get("description") or "").strip()
        if not desc:
            parts = [str(package.get("hook") or "")]
            if package.get("credit"):
                parts.append(f"Credit: {package['credit']}")
            if package.get("source", {}).get("url"):
                parts.append(f"Source: {package['source']['url']}")
            desc = "\n".join(p for p in parts if p)
        return _clip(desc, DESCRIPTION_MAX)

    def _tags(self, package: dict) -> list[str]:
        cap = self.caption(package)
        tags = cap.get("tags") or self.cfg["seo"]["hashtags"].get("youtube") or []
        return [str(t).lstrip("#").strip() for t in tags if str(t).strip()]

    # ---- auth ------------------------------------------------------------
    def _token(self) -> str:
        cached = oauth.cached_token(self.name)
        if cached:
            return cached
        client_id = env("YT_CLIENT_ID") or ""
        client_secret = env("YT_CLIENT_SECRET") or ""
        refresh_token = env("YT_REFRESH_TOKEN") or ""
        data = oauth.refresh_google_token(client_id, client_secret, refresh_token)
        oauth.cache_token(self.name, data["access_token"], int(data.get("expires_in", 3600)))
        return str(data["access_token"])

    def _auth_header(self) -> dict:
        return {"Authorization": f"Bearer {self._token()}"}

    # ---- upload ----------------------------------------------------------
    def upload(self, package: dict) -> dict:
        if not self.is_configured():
            return _failed(f"youtube not configured, missing env: {self.missing_config()}")
        problems = self.validate(package)
        if problems:
            return _failed("validation failed: " + "; ".join(problems))
        try:
            return self._upload(package)
        except oauth.RetryableError as exc:
            return _failed(str(exc), retry=True)
        except PosterError as exc:
            return _failed(str(exc))

    def _metadata(self, package: dict) -> dict:
        return {
            "snippet": {
                "title": self._title(package),
                "description": self._description(package),
                "tags": self._tags(package),
                "categoryId": CATEGORY_GAMING,
            },
            "status": {
                "selfDeclaredMadeForKids": False,
                "privacyStatus": _PRIVACY_STATUS,
            },
        }

    def _open_session(self, size: int, metadata: dict) -> str:
        """Step 1: resumable session -> Location URL."""
        resp = oauth.request_with_retry(
            "POST",
            UPLOAD_INIT_URL,
            params={"uploadType": "resumable", "part": "snippet,status"},
            json=metadata,
            headers={
                **self._auth_header(),
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(size),
                "X-Upload-Content-Type": "video/mp4",
            },
        )
        if resp.status_code >= 400:
            raise PosterError(
                f"youtube session init failed: HTTP {resp.status_code}: "
                f"{oauth.body_snippet(resp)}"
            )
        location = resp.headers.get("Location")
        if not location:
            raise PosterError("youtube session init returned no Location URL")
        return location

    def _put_video(self, location: str, video: Path, size: int) -> str:
        """Step 2: PUT the whole mp4 to the session URL; returns video id."""
        with video.open("rb") as fh:
            resp = oauth.request_with_retry(
                "PUT",
                location,
                data=fh.read(),
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(size),
                },
                timeout=oauth.TIMEOUT_UPLOAD,
            )
        if resp.status_code in (200, 201):
            video_id = str(resp.json().get("id") or "")
            if video_id:
                return video_id
            raise PosterError("youtube upload finished but response had no video id")
        if resp.status_code >= 500 or resp.status_code == 308:
            raise oauth.RetryableError(
                f"youtube upload incomplete: HTTP {resp.status_code}: "
                f"{oauth.body_snippet(resp)}"
            )
        raise PosterError(
            f"youtube upload rejected: HTTP {resp.status_code}: {oauth.body_snippet(resp)}"
        )

    def _upload(self, package: dict) -> dict:
        video = self.video_path(package)
        size = video.stat().st_size
        location = self._open_session(size, self._metadata(package))
        video_id = self._put_video(location, video, size)
        return _result(video_id, f"https://www.youtube.com/shorts/{video_id}")

    # ---- metrics ---------------------------------------------------------
    def metrics(self, post: dict) -> dict:
        post_id = str(post.get("post_id") or "")
        if not post_id:
            raise PosterError("youtube metrics: post_id missing")
        resp = oauth.request_with_retry(
            "GET",
            VIDEOS_URL,
            params={"part": "statistics", "id": post_id},
            headers=self._auth_header(),
        )
        if resp.status_code >= 400:
            raise PosterError(
                f"youtube metrics failed: HTTP {resp.status_code}: "
                f"{oauth.body_snippet(resp)}"
            )
        items = resp.json().get("items") or []
        stats = items[0].get("statistics", {}) if items else {}
        return {
            "views": _as_int(stats.get("viewCount")),
            "likes": _as_int(stats.get("likeCount")),
            "comments": _as_int(stats.get("commentCount")),
            "shares": None,  # not exposed by the Data API
            "saves": None,   # not exposed by the Data API
            "followers": None,
        }
