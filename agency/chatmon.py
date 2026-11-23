"""Per-creator live chat volume monitors (messages/sec histogram).

Follows whatever platform the creator is recording:

    Twitch   anonymous IRC over TLS (irc.chat.twitch.tv:6697) - stdlib socket
             + ssl, counts PRIVMSG lines, answers PING keepalives.
    Kick     Pusher websocket (wss://ws-us2.pusher.com) - a minimal RFC6455
             client, subscribing to chatrooms.<id>.v2 and counting
             App\\Events\\ChatMessage. Chatroom id resolved from the public
             kick.com API.
    YouTube  Data API v3 liveChatMessages.list polled with the uploader's
             OAuth token (same YT_* credentials the poster uses). Messages
             are spread across the poll interval so the histogram stays
             honest between batches. ~5 quota units per poll, every 45 s.

Everything is stdlib except the requests calls (already a dependency).
Series shape: 1-second buckets for the last WINDOW_SEC, plus totals.
"""
from __future__ import annotations

from .portable import CREATE_NO_WINDOW

import base64
import collections
import json
import os
import re
import secrets
import socket
import ssl
import threading
import time

import requests

from .poster import oauth

WINDOW_SEC = 120  # histogram covers the last 2 minutes, 1-second buckets
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


class _BaseChat(threading.Thread):
    """Common: message timestamps deque + status notes."""

    def __init__(self, label: str):
        super().__init__(daemon=True, name=f"chat-{label}")
        self._stop_evt = threading.Event()
        self._hits: collections.deque[float] = collections.deque()
        self._lock = threading.Lock()
        self.platform = label
        self.status = "connecting"
        self.note = ""
        self.total = 0

    def hit(self) -> None:
        with self._lock:
            self._hits.append(time.time())
            self.total += 1

    def buckets(self) -> list[int]:
        """1-second counts for the last WINDOW_SEC, oldest first."""
        now = int(time.time())
        counts = [0] * WINDOW_SEC
        with self._lock:
            while self._hits and self._hits[0] < now - (WINDOW_SEC - 1):
                self._hits.popleft()
            for t in self._hits:
                counts[WINDOW_SEC - 1 - (now - int(t))] += 1
        return counts

    def snapshot(self) -> dict:
        return {
            "platform": self.platform,
            "connected": self.status == "connected",
            "status": self.status,
            "note": self.note,
            "total": self.total,
            "bins": self.buckets(),
            "window_sec": WINDOW_SEC,
        }

    def stop(self) -> None:
        self._stop_evt.set()


# ---- Twitch (IRC over TLS) -------------------------------------------------
class TwitchChat(_BaseChat):
    HOST, PORT = "irc.chat.twitch.tv", 6697

    def __init__(self, channel: str):
        super().__init__("twitch")
        self._channel = re.sub(r"[^a-z0-9_]", "", channel.lower())

    def run(self) -> None:
        try:
            raw = socket.create_connection((self.HOST, self.PORT), timeout=15)
            sock = ssl.create_default_context().wrap_socket(
                raw, server_hostname=self.HOST)
            sock.settimeout(25)
            f = sock.makefile("r", encoding="utf-8", errors="replace")

            def send(line: str) -> None:
                sock.sendall((line + "\r\n").encode("utf-8"))

            send("PASS SCHMOOPIIE")
            send(f"NICK justinfan{secrets.randbelow(80000) + 10000}")
            send(f"JOIN #{self._channel}")
            last_alive = time.time()
            while not self._stop_evt.is_set():
                try:
                    line = f.readline()
                except (socket.timeout, TimeoutError):
                    if time.time() - last_alive > 60:
                        self.status, self.note = "stalled", "no data for 60s"
                        break
                    send("PING :tmi.twitch.tv")  # keepalive
                    continue
                if not line:
                    self.status, self.note = "disconnected", "socket closed"
                    break
                line = line.strip()
                last_alive = time.time()
                if line.startswith("PING"):
                    send("PONG :tmi.twitch.tv")
                    continue
                if "Welcome, GLHF!" in line:
                    self.status, self.note = "connected", f"#{self._channel}"
                    continue
                if " PRIVMSG " in line:
                    self.hit()
            sock.close()
        except Exception as exc:  # noqa: BLE001
            self.status, self.note = "error", repr(exc)[:160]


# ---- minimal websocket client (for Kick/Pusher) ----------------------------
class _MiniWS:
    """Smallest usable RFC6455 client: one text ping/pong, masked sends."""

    def __init__(self, host: str, path: str, port: int = 443):
        self._host, self._port, self._path = host, port, path
        self._sock: socket.socket | None = None
        self._buf = b""

    def connect(self) -> None:
        raw = socket.create_connection((self._host, self._port), timeout=15)
        self._sock = ssl.create_default_context().wrap_socket(
            raw, server_hostname=self._host)
        self._sock.settimeout(30)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        req = (f"GET {self._path} HTTP/1.1\r\nHost: {self._host}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
               f"User-Agent: {UA}\r\n\r\n")
        self._sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("handshake: socket closed")
            head += chunk
        pre, _, rest = head.partition(b"\r\n\r\n")
        if b" 101 " not in pre.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"handshake refused: {pre[:80]!r}")
        self._buf = rest  # may already contain frames

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("socket closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        mask = secrets.token_bytes(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        header = bytearray([0x81])  # FIN + text
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += n.to_bytes(2, "big")
        else:
            header.append(0x80 | 127)
            header += n.to_bytes(8, "big")
        self._sock.sendall(bytes(header) + mask + masked)

    def recv_text(self) -> str:
        """Next text message; answers pings and skips binary/control frames."""
        while True:
            b1, b2 = self._recv_exact(2)
            opcode = b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = int.from_bytes(self._recv_exact(2), "big")
            elif n == 127:
                n = int.from_bytes(self._recv_exact(8), "big")
            payload = self._recv_exact(n) if n else b""
            if opcode == 0x9:  # ping -> pong
                mask = secrets.token_bytes(4)
                masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                hdr = bytes([0x8A, 0x80 | len(payload)]) + mask
                self._sock.sendall(hdr + masked)
                continue
            if opcode == 0x8:
                raise ConnectionError("server sent close")
            if opcode in (0x1, 0x2, 0x0):
                return payload.decode("utf-8", errors="replace")


# ---- Kick (Pusher websocket) ------------------------------------------------
class KickChat(_BaseChat):
    PUSHER_APP = "32cbd69e4b950bf97679"  # kick's public pusher app key
    PUSHER_CLUSTER = "ws-us2.pusher.com"

    def __init__(self, slug: str):
        super().__init__("kick")
        self._slug = re.sub(r"[^a-zA-Z0-9_-]", "", slug)
        self._room = 0

    def _resolve_room(self) -> None:
        r = requests.get(f"https://kick.com/api/v2/channels/{self._slug}",
                         headers={"User-Agent": UA, "Accept": "application/json"},
                         timeout=20)
        if r.status_code != 200:
            raise ConnectionError(f"kick api HTTP {r.status_code} for /{self._slug}")
        self._room = int(((r.json() or {}).get("chatroom") or {}).get("id") or 0)
        if not self._room:
            raise ConnectionError(f"kick: no chatroom id for /{self._slug}")

    def run(self) -> None:
        try:
            self._resolve_room()
            ws = _MiniWS(self.PUSHER_CLUSTER,
                         f"/app/{self.PUSHER_APP}?protocol=7&client=js"
                         "&version=8.4.0&flash=false")
            ws.connect()
            hello = json.loads(ws.recv_text())
            if hello.get("event") != "pusher:connection_established":
                raise ConnectionError(f"pusher handshake: {str(hello)[:120]}")
            ws.send_text(json.dumps({"event": "pusher:subscribe",
                                     "data": {"auth": "",
                                              "channel": f"chatrooms.{self._room}.v2"}}))
            self.status, self.note = "connected", f"/{self._slug}"
            last_alive = time.time()
            while not self._stop_evt.is_set():
                try:
                    msg = json.loads(ws.recv_text())
                except (socket.timeout, TimeoutError):
                    ws.send_text(json.dumps({"event": "pusher:ping", "data": {}}))
                    if time.time() - last_alive > 75:
                        self.status, self.note = "stalled", "no data for 75s"
                        break
                    continue
                last_alive = time.time()
                if msg.get("event") == "App\\Events\\ChatMessage":
                    self.hit()
                elif msg.get("event") == "pusher:error":
                    self.note = str(msg.get("data", ""))[:160]
        except Exception as exc:  # noqa: BLE001
            self.status, self.note = "error", repr(exc)[:160]


# ---- YouTube (Data API, liveChatMessages.list) ------------------------------
class YTChat(_BaseChat):
    POLL_SEC = 45  # 5 quota units per poll -> ~400 units/hour, safe

    def __init__(self, url: str):
        super().__init__("youtube")
        self._url = url
        self._video = self._video_id(url)

    @staticmethod
    def _video_id(url: str) -> str:
        m = re.search(r"(?:v=|youtu\.be/|/live/|/shorts/|/embed/)([\w-]{11})", url)
        if m:
            return m.group(1)
        # channel /@handle/live style pages: let yt-dlp resolve it
        try:
            import shutil
            import subprocess
            ytdlp = shutil.which("yt-dlp") or r"C:\ytdlp\yt-dlp.exe"
            r = subprocess.run([ytdlp, "--no-warnings", "--no-playlist",
                                "--print", "id", url],
                               capture_output=True, text=True, timeout=90,
                               creationflags=CREATE_NO_WINDOW)
            if r.returncode == 0:
                out = (r.stdout or "").strip().splitlines()
                if out and re.fullmatch(r"[\w-]{11}", out[0]):
                    return out[0]
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _token(self) -> str:
        cached = oauth.cached_token("youtube-chat")
        if cached:
            return cached
        data = oauth.refresh_google_token(
            os.environ.get("YT_CLIENT_ID", ""),
            os.environ.get("YT_CLIENT_SECRET", ""),
            os.environ.get("YT_REFRESH_TOKEN", ""))
        oauth.cache_token("youtube-chat", data["access_token"],
                          int(data.get("expires_in", 3600)))
        return str(data["access_token"])

    def _api(self, path: str, **params) -> dict:
        r = requests.get(
            f"https://www.googleapis.com/youtube/v3/{path}",
            params=params, headers={"Authorization": f"Bearer {self._token()}"},
            timeout=30)
        if r.status_code >= 400:
            raise ConnectionError(f"YT api HTTP {r.status_code}: "
                                  f"{oauth.body_snippet(r, 160)}")
        return r.json() or {}

    def run(self) -> None:
        if not self._video:
            self.status, self.note = "error", "could not resolve a video id from the url"
            return
        try:
            info = self._api("videos", part="liveStreamingDetails", id=self._video)
            items = info.get("items") or []
            chat_id = ((items[0].get("liveStreamingDetails") or {})
                       .get("activeLiveChatId") if items else "")
            if not chat_id:
                self.status, self.note = "offline", "stream is not live (no chat id)"
                return
            self.status, self.note = "connected", f"video {self._video}"
            while not self._stop_evt.is_set():
                t0 = time.time()
                data = self._api("liveChatMessages", liveChatId=chat_id,
                                 part="snippet", maxResults=200)
                msgs = data.get("items") or []
                # spread the batch across the poll window so the histogram
                # reflects activity between polls instead of one spike
                span = min(self.POLL_SEC, max(1.0, len(msgs) * 0.5))
                for i, _ in enumerate(msgs):
                    with self._lock:
                        self._hits.append(t0 - span + span * (i + 1) / max(1, len(msgs)))
                    self.total += 1
                if self.status != "connected":
                    self.status = "connected"
                nxt = ((data.get("paginationInfo") or {}).get("nextPollTimeMs")
                       or self.POLL_SEC * 1000)
                wait = max(15.0, min(60.0, (nxt - time.time() * 1000) / 1000))
                wait = max(wait, self.POLL_SEC) if len(msgs) == 0 else max(wait, 20.0)
                self._stop_evt.wait(wait)
        except Exception as exc:  # noqa: BLE001
            self.status, self.note = "error", repr(exc)[:160]


# ---- hub (one monitor per creator) ------------------------------------------
def detect(url: str) -> tuple[str, str]:
    """(platform, handle/url) for a stream url, ('none', '') otherwise."""
    u = (url or "").strip()
    if not u or u == "testsrc":
        return "none", ""
    m = re.search(r"twitch\.tv/([A-Za-z0-9_]+)", u)
    if m:
        return "twitch", m.group(1)
    m = re.search(r"kick\.com/([A-Za-z0-9_-]+)", u)
    if m:
        return "kick", m.group(1)
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube", u
    return "none", ""


class ChatHub:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._threads: dict[str, _BaseChat] = {}

    def start(self, cid: str, url: str) -> dict:
        platform, handle = detect(url)
        self.stop(cid)
        if platform == "none":
            return {"platform": "none", "note": "chat follows twitch/kick/youtube urls"}
        cls = {"twitch": TwitchChat, "kick": KickChat, "youtube": YTChat}[platform]
        thread = cls(handle) if platform != "youtube" else cls(url)
        with self._lock:
            self._threads[cid] = thread
        thread.start()
        return thread.snapshot()

    def stop(self, cid: str) -> None:
        with self._lock:
            thread = self._threads.pop(cid, None)
        if thread:
            thread.stop()

    def stop_all(self) -> None:
        with self._lock:
            threads = list(self._threads.values())
            self._threads.clear()
        for t in threads:
            t.stop()

    def status(self, cid: str) -> dict:
        with self._lock:
            thread = self._threads.get(cid)
        if not thread or not thread.is_alive():
            if thread and not thread.is_alive() and thread.status == "connected":
                thread.status = "disconnected"
            if not thread:
                return {"platform": "none", "connected": False, "status": "idle",
                        "note": "", "total": 0, "bins": [0] * WINDOW_SEC,
                        "window_sec": WINDOW_SEC}
        snap = thread.snapshot()
        per_min = sum(snap["bins"][-60:])
        snap["per_min"] = per_min
        return snap


chatmon = ChatHub()
