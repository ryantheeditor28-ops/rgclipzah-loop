"""TikTok poster (Content Posting API, direct post, FILE_UPLOAD).

Flow (per docs updated Aug-Oct 2026):
    1. POST /v2/post/publish/video/init/  (post_info + source_info) -> publish_id + upload_url
    2. PUT the mp4 bytes to upload_url (Content-Range, single chunk for small files)
    3. poll POST /v2/post/publish/status/fetch/ until PUBLISH_COMPLETE / FAILED
    4. item_id from data.publicaly_available_post_id (yes, TikTok's field name
       is misspelled) -> https://www.tiktok.com/@user/video/{item_id}

Notes:
    - privacy_level is PUBLIC_TO_EVERYONE; unaudited API clients are blocked
      from public posting by TikTok (403 unaudited_client_can_only_post_to_private_accounts)
      until the app passes audit.
    - init is rate limited to 6 req/min per user token; status fetch 30 req/min.
    - metrics: POST /v2/video/query/?fields=view_count,like_count,comment_count,share_count
"""
from __future__ import annotations

import datetime
import math
import time

import requests

from ..config import env
from . import oauth
from .base import PosterBase, PosterError

INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
QUERY_URL = "https://open.tiktokapis.com/v2/video/query/"

PRIVACY_LEVEL = "PUBLIC_TO_EVERYONE"
CAPTION_MAX_UTF16 = 2200
CHUNK_MAX_BYTES = 64 * 1024 * 1024  # cap chunk ~64MB; our clips are 1 chunk
POLL_INTERVAL_SECS = 5
POLL_MAX_SECS = 240  # moderation is usually <1 min, can take longer


def _utf16_len(text: str) -> int:
    """TikTok counts caption length in UTF-16 runes."""
    return len(text.encode("utf-16-le")) // 2


def _result(post_id: str, url: str) -> dict:
    return {
        "ok": True,
        "post_id": post_id,
        "url": url,
        "posted_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def _failed(error: str, retry: bool = False) -> dict:
    return {"ok": False, "error": error, "retry": retry}


def _check(resp: requests.Response, what: str) -> dict:
    """Parse a TikTok JSON response; fatal on 4xx, retryable on 5xx."""
    if resp.status_code >= 500:
        raise oauth.RetryableError(
            f"tiktok {what}: HTTP {resp.status_code}: {oauth.body_snippet(resp)}"
        )
    if resp.status_code >= 400:
        raise PosterError(
            f"tiktok {what}: HTTP {resp.status_code}: {oauth.body_snippet(resp)}"
        )
    try:
        data = resp.json()
    except ValueError as exc:
        raise PosterError(f"tiktok {what}: non-JSON response") from exc
    err = data.get("error") or {}
    if err.get("code") not in (None, "", "ok"):
        raise PosterError(f"tiktok {what}: {err.get('code')}: {err.get('message')}")
    return data.get("data") or {}


class TikTokPoster(PosterBase):
    name = "tiktok"
    required_env = ("TT_CLIENT_KEY", "TT_CLIENT_SECRET", "TT_ACCESS_TOKEN")

    # ---- captions --------------------------------------------------------
    def caption(self, package: dict) -> dict:
        """Injected captions first, captions/<platform>.json as fallback."""
        cap = package.get("_captions", {}).get(self.name) or {}
        if not cap:
            from ..queue import get_caption

            cap = get_caption(package["id"], self.name)
        return cap or {}

    def _caption_text(self, package: dict) -> str:
        cap = self.caption(package)
        text = str(cap.get("title") or cap.get("caption") or package.get("hook") or "").strip()
        tags = cap.get("hashtags") or self.cfg["seo"]["hashtags"].get("tiktok") or []
        tag_str = " ".join(str(t) if str(t).startswith("#") else f"#{t}" for t in tags).strip()
        if tag_str and tag_str.lower() not in text.lower():
            text = f"{text} {tag_str}".strip()
        if _utf16_len(text) > CAPTION_MAX_UTF16 and tag_str:
            text = text[: -len(tag_str) - 1].strip()  # drop tags first
        while text and _utf16_len(text) > CAPTION_MAX_UTF16:
            text = text[:-1].rstrip()
        return text

    # ---- auth ------------------------------------------------------------
    def _token(self) -> str:
        cached = oauth.cached_token(self.name)
        if cached:
            return cached
        token = env("TT_ACCESS_TOKEN") or ""
        oauth.cache_token(self.name, token, 3600)
        return token

    def _refresh_if_possible(self) -> str:
        """TikTok user tokens live ~24h; refresh only if TT_REFRESH_TOKEN set."""
        refresh_token = env("TT_REFRESH_TOKEN")
        if not refresh_token:
            raise PosterError(
                "tiktok access token rejected and no TT_REFRESH_TOKEN configured "
                "(tokens expire ~24h; see SETUP-CREDENTIALS.md)"
            )
        data = oauth.refresh_tiktok_token(
            env("TT_CLIENT_KEY") or "", env("TT_CLIENT_SECRET") or "", refresh_token
        )
        oauth.cache_token(
            self.name, data["access_token"], int(data.get("expires_in", 86400))
        )
        return str(data["access_token"])

    # ---- upload ----------------------------------------------------------
    def upload(self, package: dict) -> dict:
        if not self.is_configured():
            return _failed(f"tiktok not configured, missing env: {self.missing_config()}")
        problems = self.validate(package)
        if problems:
            return _failed("validation failed: " + "; ".join(problems))
        try:
            return self._upload(package)
        except oauth.RetryableError as exc:
            return _failed(str(exc), retry=True)
        except PosterError as exc:
            return _failed(str(exc))

    def _init(self, package: dict, size: int) -> tuple[str, str]:
        chunk_size = min(size, CHUNK_MAX_BYTES)
        body = {
            "post_info": {
                "title": self._caption_text(package),
                "privacy_level": PRIVACY_LEVEL,
                "disable_duet": False,
                "disable_comment": False,
                "disable_stitch": False,
                "brand_content_toggle": False,
                "brand_organic_toggle": False,
                "is_aigc": False,
            },
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": size,
                "chunk_size": chunk_size,
                "total_chunk_count": max(1, math.ceil(size / chunk_size)),
            },
        }
        resp = oauth.request_with_retry(
            "POST",
            INIT_URL,
            json=body,
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Content-Type": "application/json; charset=UTF-8",
            },
        )
        if resp.status_code == 401:
            # token expired -> one refresh attempt, then re-init
            token = self._refresh_if_possible()
            resp = oauth.request_with_retry(
                "POST",
                INIT_URL,
                json=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=UTF-8",
                },
            )
        data = _check(resp, "video init")
        upload_url = str(data.get("upload_url") or "")
        publish_id = str(data.get("publish_id") or "")
        if not upload_url or not publish_id:
            raise PosterError("tiktok video init returned no upload_url/publish_id")
        return upload_url, publish_id

    def _put_chunks(self, upload_url: str, video_path, size: int) -> None:
        """PUT bytes to upload_url in <=64MB chunks (1 chunk for our clips)."""
        chunk_size = min(size, CHUNK_MAX_BYTES)
        offset = 0
        with video_path.open("rb") as fh:
            while offset < size:
                chunk = fh.read(chunk_size)
                end = offset + len(chunk) - 1
                resp = oauth.request_with_retry(
                    "PUT",
                    upload_url,
                    data=chunk,
                    headers={
                        "Content-Type": "video/mp4",
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {offset}-{end}/{size}",
                    },
                    timeout=oauth.TIMEOUT_UPLOAD,
                )
                if resp.status_code not in (200, 201, 206):
                    if resp.status_code >= 500:
                        raise oauth.RetryableError(
                            f"tiktok upload: HTTP {resp.status_code}: "
                            f"{oauth.body_snippet(resp)}"
                        )
                    raise PosterError(
                        f"tiktok upload rejected: HTTP {resp.status_code}: "
                        f"{oauth.body_snippet(resp)}"
                    )
                offset += len(chunk)

    def _poll(self, publish_id: str) -> dict:
        """Poll status/fetch until PUBLISH_COMPLETE or FAILED. Never logs the
        publish_id's URLs; publish_id itself is safe to surface."""
        deadline = time.monotonic() + POLL_MAX_SECS
        token_header = {"Authorization": f"Bearer {self._token()}",
                        "Content-Type": "application/json; charset=UTF-8"}
        while time.monotonic() < deadline:
            data = _check(
                oauth.request_with_retry(
                    "POST", STATUS_URL, json={"publish_id": publish_id},
                    headers=token_header,
                ),
                "status fetch",
            )
            status = str(data.get("status") or "")
            if status == "PUBLISH_COMPLETE":
                return data
            if status == "FAILED":
                raise PosterError(
                    f"tiktok publish failed: {data.get('fail_reason') or 'unknown'}"
                )
            time.sleep(POLL_INTERVAL_SECS)
        raise PosterError(
            f"tiktok still processing after {POLL_MAX_SECS}s "
            f"(publish_id={publish_id}); the post may appear later - "
            "re-check TikTok before retrying to avoid a double post"
        )

    def _upload(self, package: dict) -> dict:
        video = self.video_path(package)
        size = video.stat().st_size
        upload_url, publish_id = self._init(package, size)
        self._put_chunks(upload_url, video, size)
        data = self._poll(publish_id)
        ids = data.get("publicaly_available_post_id") or []
        item_id = str(ids[0]) if ids else ""
        username = env("TT_USERNAME") or ""  # optional, for a clean URL
        if item_id:
            post_id = item_id
            url = (
                f"https://www.tiktok.com/@{username}/video/{item_id}"
                if username else item_id
            )
        else:
            # private/unaudited posts get no public id; keep the publish handle
            post_id = publish_id
            url = publish_id
        return _result(post_id, url)

    # ---- metrics ---------------------------------------------------------
    def metrics(self, post: dict) -> dict:
        post_id = str(post.get("post_id") or "")
        if not post_id:
            raise PosterError("tiktok metrics: post_id missing")
        resp = oauth.request_with_retry(
            "POST",
            QUERY_URL,
            params={"fields": "view_count,like_count,comment_count,share_count"},
            json={"filters": {"video_ids": [post_id]}},
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Content-Type": "application/json",
            },
        )
        data = _check(resp, "video query")
        videos = data.get("videos") or []
        stats = videos[0] if videos else {}
        return {
            "views": _as_int(stats.get("view_count")),
            "likes": _as_int(stats.get("like_count")),
            "comments": _as_int(stats.get("comment_count")),
            "shares": _as_int(stats.get("share_count")),
            "saves": None,      # not exposed by /video/query/
            "followers": None,
        }


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
