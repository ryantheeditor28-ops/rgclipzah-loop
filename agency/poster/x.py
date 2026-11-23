"""X (Twitter) poster (API v2, OAuth 2.0 user context, chunked media upload).

2026 v2 media flow (the old command=INIT/APPEND/FINALIZE protocol on
POST /2/media/upload is deprecated):
    1. INIT     POST /2/media/upload/initialize  JSON {media_type, total_bytes,
                media_category:"tweet_video"} -> data.id
    2. APPEND   POST /2/media/upload/{id}/append multipart (segment_index + media),
                chunks <= 5 MB (we use 4 MB)
    3. FINALIZE POST /2/media/upload/{id}/finalize -> may return processing_info
    4. STATUS   GET /2/media/upload?command=STATUS&media_id={id} until succeeded
    5. POST /2/tweets {text, media:{media_ids:[...]}} -> data.id

Auth: refresh-token grant (scope offline.access tweet.write media.write).
X ROTATES refresh tokens on use - when a new one comes back we log a masked
reminder to update X_REFRESH_TOKEN in .env (never logged in full).

Metrics: GET /2/tweets/{id}?tweet.fields=public_metrics,non_public_metrics
(impression_count, like_count, reply_count, retweet_count, bookmark_count;
non_public_metrics only exists for your own tweets via user context).
"""
from __future__ import annotations

import datetime
import time

from ..config import env
from ..logutil import log
from . import oauth
from .base import PosterBase, PosterError

INIT_URL = "https://api.x.com/2/media/upload/initialize"
APPEND_URL = "https://api.x.com/2/media/upload/{id}/append"
FINALIZE_URL = "https://api.x.com/2/media/upload/{id}/finalize"
STATUS_URL = "https://api.x.com/2/media/upload"
TWEETS_URL = "https://api.x.com/2/tweets"
TWEET_URL = "https://api.x.com/2/tweets/{id}"

MEDIA_CATEGORY = "tweet_video"
CHUNK_BYTES = 4 * 1024 * 1024  # server max 8 MB; 4 MB is the documented safe size
TEXT_MAX = 280
STATUS_POLL_MAX = 60


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


class XPoster(PosterBase):
    name = "x"
    required_env = ("X_CLIENT_ID", "X_CLIENT_SECRET", "X_REFRESH_TOKEN")

    # ---- captions --------------------------------------------------------
    def caption(self, package: dict) -> dict:
        """Injected captions first, captions/<platform>.json as fallback."""
        cap = package.get("_captions", {}).get(self.name) or {}
        if not cap:
            from ..queue import get_caption

            cap = get_caption(package["id"], self.name)
        return cap or {}

    def _text(self, package: dict) -> str:
        cap = self.caption(package)
        text = str(cap.get("text") or cap.get("title") or package.get("hook") or "").strip()
        tags = cap.get("hashtags") or self.cfg["seo"]["hashtags"].get("x") or []
        tag_str = " ".join(str(t) if str(t).startswith("#") else f"#{t}" for t in tags).strip()
        if tag_str and tag_str.lower() not in text.lower():
            text = f"{text} {tag_str}".strip()
        # NB: X counts URLs as 23 chars and most emoji as 2, so this is a
        # best-effort cap; composed texts here are short.
        return text[:TEXT_MAX]

    # ---- auth ------------------------------------------------------------
    def _token(self, force: bool = False) -> str:
        if not force:
            cached = oauth.cached_token(self.name)
            if cached:
                return cached
        data = oauth.refresh_x_token(
            env("X_CLIENT_ID") or "",
            env("X_CLIENT_SECRET") or "",
            env("X_REFRESH_TOKEN") or "",
        )
        oauth.cache_token(self.name, data["access_token"], int(data.get("expires_in", 7200)))
        rotated = data.get("refresh_token")
        if rotated:
            log(
                "x rotated its refresh token (new token starts with "
                f"{oauth.mask(rotated)}); update X_REFRESH_TOKEN in .env"
            )
        return str(data["access_token"])

    def _request(self, method: str, url: str, *, retry_on_401: bool = True, **kwargs):
        """Authorized request; one 401 -> force refresh -> retry once."""
        headers = {**kwargs.pop("headers", {}), "Authorization": f"Bearer {self._token()}"}
        resp = oauth.request_with_retry(method, url, headers=headers, **kwargs)
        if resp.status_code == 401 and retry_on_401:
            headers["Authorization"] = f"Bearer {self._token(force=True)}"
            resp = oauth.request_with_retry(method, url, headers=headers, **kwargs)
        return resp

    def _check(self, resp, what: str) -> dict:
        if resp.status_code >= 500:
            raise oauth.RetryableError(
                f"x {what}: HTTP {resp.status_code}: {oauth.body_snippet(resp)}"
            )
        if resp.status_code >= 400:
            raise PosterError(
                f"x {what}: HTTP {resp.status_code}: {oauth.body_snippet(resp)}"
            )
        try:
            return resp.json() or {}
        except ValueError as exc:
            raise PosterError(f"x {what}: non-JSON response") from exc

    # ---- media upload ----------------------------------------------------
    def _init_media(self, size: int) -> str:
        data = self._check(
            self._request(
                "POST", INIT_URL, json={"media_type": "video/mp4",
                                        "total_bytes": size,
                                        "media_category": MEDIA_CATEGORY}
            ),
            "media init",
        )
        media_id = str((data.get("data") or {}).get("id") or "")
        if not media_id:
            raise PosterError("x media init returned no media id")
        return media_id

    def _append_media(self, media_id: str, video_path, size: int) -> None:
        with video_path.open("rb") as fh:
            index = 0
            while True:
                chunk = fh.read(CHUNK_BYTES)
                if not chunk:
                    break
                resp = self._request(
                    "POST",
                    APPEND_URL.format(id=media_id),
                    data={"segment_index": str(index)},
                    files={"media": (f"chunk{index}.mp4", chunk, "video/mp4")},
                    timeout=oauth.TIMEOUT_UPLOAD,
                )
                if resp.status_code >= 400:
                    self._check(resp, f"media append (segment {index})")
                index += 1
        if size == 0:
            raise PosterError("x media append: video file is empty")

    def _finalize_media(self, media_id: str) -> None:
        data = self._check(
            self._request("POST", FINALIZE_URL.format(id=media_id)), "media finalize"
        )
        info = (data.get("data") or {}).get("processing_info") or {}
        if not info:
            return  # ready to use
        self._poll_media(media_id, info)

    def _poll_media(self, media_id: str, info: dict) -> None:
        for _ in range(STATUS_POLL_MAX):
            state = str(info.get("state") or "")
            if state == "succeeded":
                return
            if state == "failed":
                raise PosterError(
                    f"x media processing failed: {info.get('error') or 'unknown'}"
                )
            time.sleep(int(info.get("check_after_secs") or 2))
            data = self._check(
                self._request(
                    "GET", STATUS_URL,
                    params={"command": "STATUS", "media_id": media_id},
                ),
                "media status",
            )
            info = (data.get("data") or {}).get("processing_info") or {}
            if not info:
                return
        raise oauth.RetryableError(
            f"x media still processing after {STATUS_POLL_MAX} polls"
        )

    # ---- upload ----------------------------------------------------------
    def upload(self, package: dict) -> dict:
        if not self.is_configured():
            return _failed(f"x not configured, missing env: {self.missing_config()}")
        problems = self.validate(package)
        if problems:
            return _failed("validation failed: " + "; ".join(problems))
        try:
            return self._upload(package)
        except oauth.RetryableError as exc:
            return _failed(str(exc), retry=True)
        except PosterError as exc:
            return _failed(str(exc))

    def _upload(self, package: dict) -> dict:
        video = self.video_path(package)
        size = video.stat().st_size
        media_id = self._init_media(size)
        self._append_media(media_id, video, size)
        self._finalize_media(media_id)
        data = self._check(
            self._request(
                "POST", TWEETS_URL,
                json={"text": self._text(package), "media": {"media_ids": [media_id]}},
            ),
            "tweet create",
        )
        post_id = str((data.get("data") or {}).get("id") or "")
        if not post_id:
            raise PosterError("x tweet create returned no id")
        username = env("X_USERNAME") or ""  # optional, for a clean URL
        url = (
            f"https://x.com/{username}/status/{post_id}"
            if username else f"https://x.com/i/web/status/{post_id}"
        )
        return _result(post_id, url)

    # ---- metrics ---------------------------------------------------------
    def metrics(self, post: dict) -> dict:
        post_id = str(post.get("post_id") or "")
        if not post_id:
            raise PosterError("x metrics: post_id missing")
        data = self._check(
            self._request(
                "GET", TWEET_URL.format(id=post_id),
                params={"tweet.fields": "public_metrics,non_public_metrics"},
            ),
            "tweet lookup",
        )
        tweet = data.get("data") or {}
        public = tweet.get("public_metrics") or {}
        non_public = tweet.get("non_public_metrics") or {}
        views = _as_int(public.get("impression_count"))
        if views is None:
            views = _as_int(non_public.get("impression_count"))
        return {
            "views": views,
            "likes": _as_int(public.get("like_count")),
            "comments": _as_int(public.get("reply_count")),
            "shares": _as_int(public.get("retweet_count")),
            "saves": _as_int(public.get("bookmark_count")),
            "followers": None,
        }
