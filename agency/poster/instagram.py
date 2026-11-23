"""Instagram Reels poster (Graph API, container -> publish flow).

IG-specific requirement: the Graph API cannot accept raw bytes for video -
the container must reference a PUBLICLY REACHABLE video_url. Set
IG_PUBLIC_URL_BASE and we PUT the mp4 to {IG_PUBLIC_URL_BASE}/{pkg_id}.mp4,
then hand that URL to the container. Two hosting modes:

    - plain: the base accepts unauthenticated PUTs (rare)
    - S3-compatible (recommended): also set IG_UPLOAD_ENDPOINT (base URL
      INCLUDING the bucket), IG_UPLOAD_ACCESS_KEY, IG_UPLOAD_SECRET_KEY and
      optionally IG_UPLOAD_REGION (default "auto") - the PUT is then signed
      with AWS SigV4 (works with Cloudflare R2, Backblaze B2's S3 API,
      AWS S3, Minio)

Without IG_PUBLIC_URL_BASE, upload() refuses with a clear message (dry_run
shows it as a note, not a crash).

Flow:
    1. PUT mp4 to the public URL
    2. POST /{IG_USER_ID}/media  {media_type: REELS, video_url, caption, share_to_feed}
       -> container id
    3. poll GET /{container_id}?fields=status_code until FINISHED
    4. POST /{IG_USER_ID}/media_publish {creation_id} -> media id -> permalink

Metrics: GET /{media_id}/insights with the current REELS metric set
(views, reach, likes, comments, saved, shares - `plays` was replaced by
`views` for media created after the 2024 metric migration; docs as of 2026
list `views` for REELS). Missing values fall back to media fields
(like_count / comments_count). Unavailable -> None.
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import time
import urllib.parse

from ..config import env
from . import oauth
from .base import PosterBase, PosterError

# v21.0 is valid until Jan 21, 2027 (Meta changelog); override via IG_GRAPH_VERSION.
GRAPH_VERSION = env("IG_GRAPH_VERSION") or "v21.0"

CAPTION_MAX = 2200
POLL_INTERVAL_SECS = 5
POLL_MAX_SECS = 300

INSIGHT_METRICS = "views,reach,likes,comments,saved,shares"
INSIGHT_METRICS_CORE = "views,likes,comments,saved,shares"


def _result(post_id: str, url: str) -> dict:
    return {
        "ok": True,
        "post_id": post_id,
        "url": url,
        "posted_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def _failed(error: str, retry: bool = False) -> dict:
    return {"ok": False, "error": error, "retry": retry}


def _sigv4_put_headers(url: str, payload_sha256: str, access_key: str,
                       secret_key: str, region: str, *, service: str = "s3") -> dict:
    """Minimal AWS SigV4 header signing (stdlib only) for S3-compatible PUTs.

    Works with Cloudflare R2 (region "auto"), Backblaze B2's S3 API
    (region e.g. "us-west-004"), AWS S3 and Minio. Signs the payload hash so
    the raw bytes PUT authenticates without any presigned-URL quirks.
    """
    parsed = urllib.parse.urlsplit(url)
    host = parsed.netloc
    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")

    canonical_headers = (
        f"host:{host}\n"
        f"x-amz-content-sha256:{payload_sha256}\n"
        f"x-amz-date:{amz_date}\n"
    )
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = "\n".join([
        "PUT", parsed.path or "/", "", canonical_headers, signed_headers, payload_sha256,
    ])
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])

    def _hmac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    k_date = _hmac(("AWS4" + secret_key).encode("utf-8"), datestamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    k_signing = _hmac(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
        "x-amz-content-sha256": payload_sha256,
        "x-amz-date": amz_date,
    }


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class InstagramPoster(PosterBase):
    name = "instagram"
    # IG_PUBLIC_URL_BASE is required too: without a public video host the
    # Graph API cannot accept the mp4, so the platform is not postable.
    required_env = ("IG_ACCESS_TOKEN", "IG_USER_ID", "IG_PUBLIC_URL_BASE")

    # ---- endpoints -------------------------------------------------------
    @staticmethod
    def _base() -> str:
        return f"https://graph.facebook.com/{GRAPH_VERSION}"

    def _user_id(self) -> str:
        return env("IG_USER_ID") or ""

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
        text = str(cap.get("caption") or cap.get("title") or package.get("hook") or "").strip()
        desc = str(cap.get("description") or "").strip()
        if desc and desc not in text:
            text = f"{text}\n\n{desc}".strip()
        tags = cap.get("hashtags") or self.cfg["seo"]["hashtags"].get("instagram") or []
        tag_str = " ".join(str(t) if str(t).startswith("#") else f"#{t}" for t in tags).strip()
        if tag_str and tag_str.lower() not in text.lower():
            text = f"{text}\n\n{tag_str}".strip()
        return text[:CAPTION_MAX]

    # ---- dry run ---------------------------------------------------------
    def dry_run(self, package: dict) -> dict:
        result = super().dry_run(package)
        public_base = env("IG_PUBLIC_URL_BASE")
        if public_base:
            result["notes"] = [
                f"video will be PUT to {public_base}/{package['id']}.mp4 "
                "(must be publicly reachable)"
            ]
        else:
            result["notes"] = [
                "IG_PUBLIC_URL_BASE not set: Instagram needs a publicly "
                "reachable video_url; upload() will refuse until configured "
                "(see SETUP-CREDENTIALS.md)"
            ]
        return result

    # ---- upload ----------------------------------------------------------
    def upload(self, package: dict) -> dict:
        if not self.is_configured():
            return _failed(f"instagram not configured, missing env: {self.missing_config()}")
        public_base = env("IG_PUBLIC_URL_BASE")
        if not public_base:
            return _failed(
                "instagram requires IG_PUBLIC_URL_BASE: the Graph API cannot "
                "accept raw video bytes; the finished mp4 must be PUT to a "
                "publicly reachable URL (S3/R2/Backblaze static hosting) and "
                "that URL passed to the media container. Set IG_PUBLIC_URL_BASE "
                "and make sure PUT <base>/{pkg_id}.mp4 is publicly readable."
            )
        problems = self.validate(package)
        if problems:
            return _failed("validation failed: " + "; ".join(problems))
        try:
            if self.package_is_image(package):
                return self._upload_image(package, public_base)
            return self._upload(package, public_base)
        except oauth.RetryableError as exc:
            return _failed(str(exc), retry=True)
        except PosterError as exc:
            return _failed(str(exc))

    # ---- image posts -------------------------------------------------------
    def _upload_image(self, package: dict, public_base: str) -> dict:
        """IMAGE container: PUT the still to the public host, then publish."""
        media = self.video_path(package)
        ext = (media.suffix.lower().lstrip(".") or "jpg")
        if ext == "jpeg":
            ext = "jpg"
        ctype = "image/png" if ext == "png" else "image/jpeg"
        image_url = self._put_video(public_base, package, media, ext=ext, content_type=ctype)
        caption = self._caption_text(package)
        resp = oauth.request_with_retry(
            "POST",
            f"{self._base()}/{self._user_id()}/media",
            params={  # mirrors _create_container (REELS path), which works
                "media_type": "IMAGE",
                "image_url": image_url,
                "caption": caption[:CAPTION_MAX],
                "access_token": env("IG_ACCESS_TOKEN"),
            },
            timeout=oauth.TIMEOUT_UPLOAD,
        )
        if resp.status_code >= 400:
            raise PosterError(
                f"instagram image container failed: HTTP {resp.status_code}: "
                f"{oauth.body_snippet(resp)}"
            )
        creation_id = str((resp.json() or {}).get("id") or "")
        if not creation_id:
            raise PosterError(
                f"instagram image container returned no id: {oauth.body_snippet(resp)}"
            )
        media_id = self._publish(creation_id)
        return _result(media_id, self._permalink(media_id))

    def _put_video(self, public_base: str, package: dict, video_path,
                   ext: str = "mp4", content_type: str = "video/mp4") -> str:
        video_url = f"{public_base.rstrip('/')}/{package['id']}.{ext}"
        with video_path.open("rb") as fh:
            body = fh.read()
        headers = {"Content-Type": content_type}
        put_url = video_url
        endpoint = env("IG_UPLOAD_ENDPOINT")
        access_key = env("IG_UPLOAD_ACCESS_KEY")
        secret_key = env("IG_UPLOAD_SECRET_KEY")
        if endpoint and access_key and secret_key:
            # S3-compatible signed upload: PUT to the private endpoint with
            # SigV4 auth; IG fetches the public URL afterwards.
            put_url = f"{endpoint.rstrip('/')}/{package['id']}.{ext}"
            payload_hash = hashlib.sha256(body).hexdigest()
            headers.update(_sigv4_put_headers(
                put_url, payload_hash, access_key, secret_key,
                env("IG_UPLOAD_REGION") or "auto",
            ))
        resp = oauth.request_with_retry(
            "PUT", put_url, data=body, headers=headers, timeout=oauth.TIMEOUT_UPLOAD
        )
        if resp.status_code in (200, 201):
            return video_url
        if resp.status_code in (401, 403):
            raise PosterError(
                f"instagram video host PUT unauthorized (HTTP {resp.status_code}): "
                "check IG_UPLOAD_ACCESS_KEY / IG_UPLOAD_SECRET_KEY / bucket path"
            )
        if resp.status_code >= 500:
            raise oauth.RetryableError(
                f"instagram video host PUT failed: HTTP {resp.status_code}: "
                f"{oauth.body_snippet(resp)}"
            )
        raise PosterError(
            f"instagram video host PUT rejected: HTTP {resp.status_code}: "
            f"{oauth.body_snippet(resp)}"
        )

    def _create_container(self, video_url: str, package: dict) -> str:
        resp = oauth.request_with_retry(
            "POST",
            f"{self._base()}/{self._user_id()}/media",
            params={
                "media_type": "REELS",
                "video_url": video_url,
                "caption": self._caption_text(package),
                "share_to_feed": "true",
                "access_token": env("IG_ACCESS_TOKEN"),
            },
        )
        if resp.status_code >= 400:
            raise PosterError(
                f"instagram container failed: HTTP {resp.status_code}: "
                f"{oauth.body_snippet(resp)}"
            )
        container_id = str((resp.json() or {}).get("id") or "")
        if not container_id:
            raise PosterError("instagram container returned no id")
        return container_id

    def _poll_container(self, container_id: str) -> None:
        deadline = time.monotonic() + POLL_MAX_SECS
        while time.monotonic() < deadline:
            resp = oauth.request_with_retry(
                "GET",
                f"{self._base()}/{container_id}",
                params={"fields": "status_code", "access_token": env("IG_ACCESS_TOKEN")},
            )
            if resp.status_code >= 400:
                raise PosterError(
                    f"instagram container status failed: HTTP {resp.status_code}: "
                    f"{oauth.body_snippet(resp)}"
                )
            status = str((resp.json() or {}).get("status_code") or "")
            if status == "FINISHED":
                return
            if status in ("ERROR", "EXPIRED"):
                raise PosterError(f"instagram container {status.lower()}")
            time.sleep(POLL_INTERVAL_SECS)
        raise oauth.RetryableError(
            f"instagram container not FINISHED after {POLL_MAX_SECS}s "
            "(processing can take several minutes for Reels)"
        )

    def _publish(self, container_id: str) -> str:
        resp = oauth.request_with_retry(
            "POST",
            f"{self._base()}/{self._user_id()}/media_publish",
            params={
                "creation_id": container_id,
                "access_token": env("IG_ACCESS_TOKEN"),
            },
        )
        if resp.status_code >= 400:
            raise PosterError(
                f"instagram publish failed: HTTP {resp.status_code}: "
                f"{oauth.body_snippet(resp)}"
            )
        media_id = str((resp.json() or {}).get("id") or "")
        if not media_id:
            raise PosterError("instagram publish returned no media id")
        return media_id

    def _permalink(self, media_id: str) -> str:
        resp = oauth.request_with_retry(
            "GET",
            f"{self._base()}/{media_id}",
            params={"fields": "permalink,shortcode", "access_token": env("IG_ACCESS_TOKEN")},
        )
        if resp.status_code >= 400:
            return media_id  # metrics still work with the bare id
        data = resp.json() or {}
        if data.get("permalink"):
            return str(data["permalink"])
        if data.get("shortcode"):
            return f"https://www.instagram.com/reel/{data['shortcode']}/"
        return media_id

    def _upload(self, package: dict, public_base: str) -> dict:
        video = self.video_path(package)
        video_url = self._put_video(public_base, package, video)
        container_id = self._create_container(video_url, package)
        self._poll_container(container_id)
        media_id = self._publish(container_id)
        return _result(media_id, self._permalink(media_id))

    # ---- metrics ---------------------------------------------------------
    def metrics(self, post: dict) -> dict:
        media_id = str(post.get("post_id") or "")
        if not media_id:
            raise PosterError("instagram metrics: post_id missing")
        values = self._insights(media_id)
        if not values:
            values = self._media_fields(media_id)
        return {
            "views": values.get("views"),
            "likes": values.get("likes"),
            "comments": values.get("comments"),
            "shares": values.get("shares"),
            "saves": values.get("saved"),
            "followers": None,
        }

    def _insights(self, media_id: str) -> dict:
        token = {"access_token": env("IG_ACCESS_TOKEN")}
        for metrics in (INSIGHT_METRICS, INSIGHT_METRICS_CORE):
            resp = oauth.request_with_retry(
                "GET",
                f"{self._base()}/{media_id}/insights",
                params={**token, "metric": metrics},
            )
            if resp.status_code >= 400:
                continue  # try the reduced set, then media-field fallback
            out: dict = {}
            for row in resp.json().get("data") or []:
                vals = row.get("values") or []
                if vals:
                    out[str(row.get("name"))] = _as_int(vals[0].get("value"))
            if out:
                return out
        return {}

    def _media_fields(self, media_id: str) -> dict:
        resp = oauth.request_with_retry(
            "GET",
            f"{self._base()}/{media_id}",
            params={
                "fields": "like_count,comments_count",
                "access_token": env("IG_ACCESS_TOKEN"),
            },
        )
        if resp.status_code >= 400:
            return {}
        data = resp.json() or {}
        return {
            "likes": _as_int(data.get("like_count")),
            "comments": _as_int(data.get("comments_count")),
        }
