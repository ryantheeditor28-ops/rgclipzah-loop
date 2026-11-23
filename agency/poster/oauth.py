"""Shared transport + OAuth2 helpers for platform posters.

Contains the small HTTP layer every adapter uses (timeout, 3 retries with
exponential backoff on 5xx/timeouts, fatal-on-4xx with a truncated body
snippet) and OAuth2 refresh-token grants for Google / X / TikTok.

SECURITY RULES (enforced here, relied on everywhere):
    - secrets are NEVER printed or logged; use mask() if a token must be
      referenced in a message (first 6 chars only)
    - URLs are never included in error text: TikTok upload URLs embed an
      upload_token query parameter
    - request errors surface the response BODY snippet (<=300 chars) only
"""
from __future__ import annotations

import time

import requests

from .base import PosterError


class RetryableError(PosterError):
    """Transient failure (network / 5xx) - safe to retry the whole upload."""


# Shared endpoints (refresh-token grant targets).
GOOGLE_TOKEN_URL = "https://www.googleapis.com/oauth2/v4/token"
X_TOKEN_URL = "https://api.x.com/2/oauth2/token"
TIKTOK_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"

TIMEOUT_DEFAULT = 60.0
TIMEOUT_UPLOAD = 600.0
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 1.0  # seconds; doubles each attempt (1, 2, 4)

_SNIPPET_LIMIT = 300

# In-memory access-token cache: provider -> (token, expires_at unix secs).
# Process-local by design: nothing token-like is ever written to disk.
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_CACHE_SAFETY_SECS = 60


# ---- secret hygiene ------------------------------------------------------
def mask(secret: str | None) -> str:
    """First 6 chars + ellipsis; safe for log lines."""
    if not secret:
        return "<missing>"
    return secret[:6] + "..." if len(secret) > 6 else "***"


def body_snippet(response: requests.Response, limit: int = _SNIPPET_LIMIT) -> str:
    """Response body flattened to one line, truncated. Never contains headers."""
    try:
        text = (response.text or "").strip().replace("\n", " ").replace("\r", " ")
    except Exception:  # noqa: BLE001 - defensive: body read should never crash us
        return "<unreadable body>"
    return text[:limit]


# ---- shared HTTP ---------------------------------------------------------
def request_with_retry(
    method: str,
    url: str,
    *,
    timeout: float = TIMEOUT_DEFAULT,
    max_retries: int = MAX_RETRIES,
    **kwargs,
) -> requests.Response:
    """requests.request() with timeout + exponential backoff.

    Retries (max `max_retries`) on 5xx and on timeout/connection errors.
    4xx is returned untouched: callers treat it as fatal and embed a body
    snippet in the error. Raises PosterError only when retries are
    exhausted on a network-level failure. Never logs URLs (they can carry
    upload tokens) or Authorization data.
    """
    resp: requests.Response | None = None
    last_note = "unknown error"
    for attempt in range(max_retries + 1):  # 1 try + max_retries
        try:
            resp = requests.request(method, url, timeout=timeout, **kwargs)
            if resp.status_code < 500:
                return resp
            last_note = f"HTTP {resp.status_code}"
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_note = type(exc).__name__
        if attempt >= max_retries:
            break
        time.sleep(RETRY_BACKOFF_BASE * (2**attempt))
    if resp is not None:
        return resp  # final 5xx: caller decides retry vs fatal
    raise RetryableError(
        f"{method} request failed after {max_retries} retries ({last_note})"
    )


# ---- in-memory token cache ----------------------------------------------
def cache_token(provider: str, token: str, expires_in: int) -> None:
    """Remember a fresh access token for this process run."""
    _TOKEN_CACHE[provider] = (token, time.time() + max(int(expires_in), 0))


def cached_token(provider: str) -> str | None:
    """Return the cached token if present and not near expiry."""
    entry = _TOKEN_CACHE.get(provider)
    if not entry:
        return None
    token, expires_at = entry
    if time.time() >= expires_at - _CACHE_SAFETY_SECS:
        _TOKEN_CACHE.pop(provider, None)
        return None
    return token


def invalidate_token(provider: str) -> None:
    _TOKEN_CACHE.pop(provider, None)


# ---- refresh-token grants ------------------------------------------------
def _grant_request(
    provider: str, url: str, payload: dict, *, auth: tuple | None = None
) -> dict:
    """POST an OAuth token grant; fatal on 4xx with body snippet."""
    resp = request_with_retry(
        "POST", url, data=payload, auth=auth, timeout=TIMEOUT_DEFAULT
    )
    if resp.status_code >= 400:
        raise PosterError(
            f"{provider} token refresh failed: HTTP {resp.status_code}: "
            f"{body_snippet(resp)}"
        )
    data = resp.json()
    if "access_token" not in data:
        raise PosterError(
            f"{provider} token refresh returned no access_token: "
            f"{body_snippet(resp)}"
        )
    return data


def refresh_google_token(client_id: str, client_secret: str, refresh_token: str) -> dict:
    """Google OAuth2 refresh grant -> {access_token, expires_in, ...}."""
    return _grant_request(
        "youtube",
        GOOGLE_TOKEN_URL,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
    )


def refresh_x_token(client_id: str, client_secret: str, refresh_token: str) -> dict:
    """X OAuth2 refresh grant. NOTE: X rotates refresh tokens - the response
    may carry a NEW refresh_token; callers must keep using the newest one."""
    return _grant_request(
        "x",
        X_TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
        auth=(client_id, client_secret),
    )


def refresh_tiktok_token(
    client_key: str, client_secret: str, refresh_token: str
) -> dict:
    """TikTok refresh grant (used only when TT_REFRESH_TOKEN is configured;
    plain user access tokens live ~24h)."""
    return _grant_request(
        "tiktok",
        TIKTOK_TOKEN_URL,
        {
            "client_key": client_key,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )
