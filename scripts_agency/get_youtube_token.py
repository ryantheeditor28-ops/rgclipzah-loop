"""One-time YouTube OAuth consent -> refresh token for .env.

Uses the installed-app (Desktop client) flow with RAW requests only - no
Google SDK. Opens the browser, serves a temporary redirect capture on
http://localhost:8765, exchanges the code at Google's token endpoint and
prints the refresh token plus the exact lines to paste into .env.

Prerequisites (see SETUP-CREDENTIALS.md):
    - Google Cloud project with YouTube Data API v3 enabled
    - OAuth client of type "Desktop app" -> YT_CLIENT_ID / YT_CLIENT_SECRET
      (present in .env or the process environment before running this)

Usage:
    python scripts_agency\\get_youtube_token.py

The refresh token does not expire while the app is in "Production"
publishing status; in "Testing" status Google expires refresh tokens after
7 days. Run from the repo root: C:\\Users\\ryang\\clip-agency.
"""
from __future__ import annotations

import sys
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agency.config import env  # noqa: E402  (after sys.path bootstrap)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://www.googleapis.com/oauth2/v4/token"
# Google does not validate the port of loopback redirect URIs for desktop
# (installed-app) clients, so we can grab any free port at runtime.
DEFAULT_PORT = 8765
SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload "
    "https://www.googleapis.com/auth/youtube.readonly"
)
AUTH_WAIT_SECS = 600
_MaskLimit = 6


def _mask(secret: str) -> str:
    return secret[:_MaskLimit] + "..." if len(secret) > _MaskLimit else "***"


def _free_port(preferred: int = DEFAULT_PORT) -> int:
    """First free loopback port at or after `preferred`."""
    import socket

    for candidate in range(preferred, preferred + 25):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", candidate))
            except OSError:
                continue
        return candidate
    raise SystemExit(f"no free port in {preferred}..{preferred + 24}")


def _auth_url(client_id: str, redirect_uri: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def _capture_code(redirect_uri: str) -> str:
    """Serve the loopback redirect once and return the authorization code."""
    holder: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (stdlib API)
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" in query:
                holder["code"] = query["code"][0]
            if "error" in query:
                holder["error"] = query["error"][0]
            body = b"<html><body><h2>Token captured.</h2>You can close this window.</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:  # silence request logging
            pass

    port = urllib.parse.urlparse(redirect_uri).port or DEFAULT_PORT
    server = HTTPServer(("localhost", port), Handler)  # port pre-checked as free
    server.timeout = 1
    print(f"waiting for consent redirect on {redirect_uri} ...")
    deadline = time.monotonic() + AUTH_WAIT_SECS
    while not holder and time.monotonic() < deadline:
        server.handle_request()
    server.server_close()
    if "error" in holder:
        raise SystemExit(f"consent failed: {holder['error']}")
    if "code" not in holder:
        raise SystemExit(f"no code captured within {AUTH_WAIT_SECS}s - try again")
    return holder["code"]


def _exchange(client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    resp = requests.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=60,
    )
    data = resp.json()
    if resp.status_code != 200 or "refresh_token" not in data:
        raise SystemExit(
            f"token exchange failed: HTTP {resp.status_code}: "
            f"{str(data)[:300]} (hint: consent must show 'prompt=consent' "
            "and access_type=offline - re-run this script)"
        )
    return data


def main() -> None:
    client_id = env("YT_CLIENT_ID") or ""
    client_secret = env("YT_CLIENT_SECRET") or ""
    if not client_id or not client_secret:
        raise SystemExit(
            "YT_CLIENT_ID / YT_CLIENT_SECRET missing: put them in .env first "
            "(Google Cloud Console -> APIs & Services -> Credentials -> "
            "OAuth client ID -> Desktop app)"
        )
    print(f"client id {_mask(client_id)} ok")
    redirect_uri = f"http://localhost:{_free_port()}"
    print(f"using redirect {redirect_uri}")
    webbrowser.open(_auth_url(client_id, redirect_uri))
    code = _capture_code(redirect_uri)
    data = _exchange(client_id, client_secret, code, redirect_uri)

    refresh_token = data["refresh_token"]
    granted = data.get("scope", SCOPES)
    print("\n=== YouTube OAuth consent complete ===")
    print(f"granted scopes: {granted}")
    print(f"refresh token (starts with {_mask(refresh_token)}):")
    print(f"\n{refresh_token}\n")
    print("Paste these lines into .env (replace existing values):")
    print("-" * 60)
    print(f"YT_CLIENT_ID={client_id}")
    print(f"YT_CLIENT_SECRET={client_secret}")
    print(f"YT_REFRESH_TOKEN={refresh_token}")
    print("-" * 60)
    print("Never commit .env or share the refresh token.")


if __name__ == "__main__":
    main()
