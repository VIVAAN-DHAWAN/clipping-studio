"""Live chat capture (Twitch IRC, YouTube) + VOD chat replay."""
from __future__ import annotations

import json
import random
import re
import socket
import ssl
import threading
import time
from datetime import datetime
from typing import Any, Callable, Iterable

import requests

from config import settings
from state import get_state
from utils import LOG, clamp

TWITCH_WEB_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"

_IRC_RE = re.compile(
    r"^(?:@(?P<tags>[^ ]+) )?: (?P<user>[^!]+)![^ ]+ PRIVMSG #(?P<channel>[^ ]+) :(?P<msg>.*)$"
)


def twitch_channel_from_url(url: str) -> str:
    try:
        from urllib.parse import urlparse

        path = urlparse(url).path.strip("/")
        return path.split("/")[0].lower()
    except Exception:
        return ""


def video_id_from_url(url: str) -> str | None:
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/live/)([A-Za-z0-9_-]{6,})", url)
    if m:
        return m.group(1)
    m = re.search(r"twitch\.tv/videos/(\d+)", url)
    if m:
        return m.group(1)
    if url.strip().isdigit():
        return url.strip()
    return None


class ChatHub:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.threads: dict[str, threading.Thread] = {}
        self.stops: dict[str, threading.Event] = {}
        self.errors: dict[str, str] = {}

    def _key(self, sid: str) -> str:
        return f"live:{sid}"

    def start_live(self, streamer: dict[str, Any], video_id: str | None = None) -> None:
        sid = streamer["id"]
        key = self._key(sid)
        with self.lock:
            if key in self.threads and self.threads[key].is_alive():
                return
            stop = threading.Event()
            self.stops[key] = stop
            if streamer["platform"] == "twitch":
                channel = (streamer.get("username") or streamer.get("name") or "").lower()
                target = self._twitch_irc_loop
                args = (sid, channel, stop)
            else:
                url = f"https://www.youtube.com/watch?v={video_id}" if video_id else (
                    streamer.get("url") or f"https://www.youtube.com/@{streamer.get('username')}/live"
                )
                target = self._chat_downloader_loop
                args = (sid, url, stop, True)
            thread = threading.Thread(target=target, args=args, name=f"chat-{sid}", daemon=True)
            self.threads[key] = thread
            thread.start()

    def stop(self, sid: str) -> None:
        key = self._key(sid)
        with self.lock:
            stop = self.stops.pop(key, None)
            thread = self.threads.pop(key, None)
        if stop:
            stop.set()
        if thread and thread.is_alive():
            thread.join(timeout=3)

    def stop_all(self) -> None:
        with self.lock:
            sids = [k.split(":", 1)[1] for k in self.threads]
        for sid in sids:
            self.stop(sid)

    def is_running(self, sid: str) -> bool:
        with self.lock:
            t = self.threads.get(self._key(sid))
            return bool(t and t.is_alive())

    # ------------------------------------------------------------- twitch IRC
    def _twitch_irc_loop(self, sid: str, channel: str, stop: threading.Event) -> None:
        state = get_state()
        if not channel:
            state.add_warning(f"Twitch chat: no username for {sid}")
            return
        attempt = 0
        nick = f"justinfan{random.randint(10000, 99999)}"
        while not stop.is_set():
            sock = None
            try:
                context = ssl.create_default_context()
                try:
                    sock = socket.create_connection(("irc.chat.twitch.tv", 6697), timeout=12)
                    sock = context.wrap_socket(sock, server_hostname="irc.chat.twitch.tv")
                except (OSError, ssl.SSLError):
                    if sock:
                        sock.close()
                    sock = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=12)
                sock.settimeout(1.0)
                sock.sendall(
                    f"PASS oauth:justinfan{random.randint(1000, 9999)}\r\n"
                    f"NICK {nick}\r\n"
                    "CAP REQ :twitch.tv/tags twitch.tv/commands\r\n"
                    f"JOIN #{channel}\r\n".encode()
                )
                state.add_event("info", f"Chat connected: #{channel}")
                self.errors.pop(sid, None)
                buffer = b""
                attempt = 0
                while not stop.is_set():
                    try:
                        data = sock.recv(8192)
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    if not data:
                        break
                    buffer += data
                    while b"\r\n" in buffer:
                        raw, buffer = buffer.split(b"\r\n", 1)
                        line = raw.decode("utf-8", "replace")
                        if line.startswith("PING"):
                            try:
                                sock.sendall(b"PONG :tmi.twitch.tv\r\n")
                            except OSError:
                                break
                            continue
                        parsed = self._parse_irc(line)
                        if parsed:
                            user, msg = parsed
                            state.add_chat(sid, user, msg)
            except Exception as exc:
                self.errors[sid] = str(exc)[:200]
                state.add_event("warn", f"Twitch chat reconnecting: {exc}")
            finally:
                if sock:
                    try:
                        sock.close()
                    except OSError:
                        pass
            if stop.is_set():
                break
            attempt += 1
            wait = min(60, 3 * attempt)
            for _ in range(int(wait * 2)):
                if stop.is_set():
                    break
                time.sleep(0.5)
        state.add_event("info", f"Chat stopped: #{channel}")

    @staticmethod
    def _parse_irc(line: str) -> tuple[str, str] | None:
        m = _IRC_RE.match(line)
        if not m:
            return None
        user = m.group("user")
        tags = m.group("tags") or ""
        for pair in tags.split(";"):
            if pair.startswith("display-name=") and pair.split("=", 1)[1]:
                user = pair.split("=", 1)[1]
                break
        msg = m.group("msg").strip()
        return (user, msg) if msg else None

    # -------------------------------------------------- chat-downloader (YT)
    def _chat_downloader_loop(self, sid: str, url: str, stop: threading.Event,
                              live: bool) -> None:
        state = get_state()
        try:
            from chat_downloader import ChatDownloader
        except ImportError:
            state.add_warning("YouTube chat needs `chat-downloader` (pip install chat-downloader)")
            return
        attempt = 0
        while not stop.is_set():
            try:
                downloader = ChatDownloader()
                chat = downloader.get_chat(
                    url, chat_type="live" if live else "replay",
                    timeout=30, inactivity_timeout=90 if live else 30,
                    message_groups=["messages"],
                )
                state.add_event("info", f"YouTube chat connected: {sid}")
                for item in chat:
                    if stop.is_set():
                        break
                    self._handle_cd_item(sid, item)
                attempt = 0
            except Exception as exc:
                self.errors[sid] = str(exc)[:250]
                if attempt == 0:
                    state.add_event("warn", f"YouTube chat: {str(exc)[:120]}")
            if stop.is_set():
                break
            attempt += 1
            wait = min(60, 5 * attempt)
            for _ in range(int(wait * 2)):
                if stop.is_set():
                    break
                time.sleep(0.5)

    def _handle_cd_item(self, sid: str, item: dict[str, Any]) -> None:
        state = get_state()
        if not isinstance(item, dict):
            return
        msg = item.get("message")
        if not msg:
            return
        author = item.get("author") or {}
        user = author.get("name") if isinstance(author, dict) else str(author)
        ts = item.get("timestamp")
        if isinstance(ts, (int, float)) and ts > 1e11:
            ts = ts / 1000.0
        offset = item.get("time_in_seconds")
        state.add_chat(
            sid, user or "viewer", str(msg),
            ts=float(ts) if isinstance(ts, (int, float)) and ts > 1e9 else None,
            offset=float(offset) if isinstance(offset, (int, float)) else None,
        )


# ------------------------------------------------------------- VOD chat replay
def download_twitch_vod_chat(video_id: str, *,
                             on_message: Callable[[float, str, str], None] | None = None,
                             stop: threading.Event | None = None,
                             max_messages: int = 120000) -> list[dict[str, Any]]:
    """Fetch Twitch VOD comments (v5 API, GraphQL fallback)."""
    rows: list[dict[str, Any]] = []
    cursor = None
    pages = 0
    session = requests.Session()
    headers = {"Client-ID": TWITCH_WEB_CLIENT_ID, "Accept": "application/json"}
    while pages < 800:
        if stop and stop.is_set():
            break
        pages += 1
        params = {"client_id": TWITCH_WEB_CLIENT_ID}
        if cursor:
            params["cursor"] = cursor
        try:
            res = session.get(
                f"https://api.twitch.tv/v5/videos/{video_id}/comments",
                params=params, headers=headers, timeout=20,
            )
        except requests.RequestException:
            break
        if res.status_code == 404:
            break
        if res.status_code >= 400:
            break
        try:
            data = res.json()
        except ValueError:
            break
        comments = data.get("comments") or []
        for c in comments:
            try:
                offset = float(c.get("content_offset_seconds", 0) or 0)
            except (TypeError, ValueError):
                offset = 0.0
            commenter = c.get("commenter") or {}
            user = str(commenter.get("display_name") or commenter.get("name") or "viewer")
            body = (c.get("message") or {}).get("body") or ""
            if not body:
                continue
            row = {"offset": offset, "user": user, "message": body}
            rows.append(row)
            if on_message:
                on_message(offset, user, body)
            if len(rows) >= max_messages:
                return rows
        cursor = data.get("_next")
        if not cursor:
            break
        time.sleep(0.12)
    if rows:
        return rows
    # GraphQL fallback
    query = """query($id: ID!, $cursor: Cursor) {
      video(id: $id) { comments(first: 100, after: $cursor) {
        edges { node { contentOffsetSeconds commenter { displayName } message { body } } }
        pageInfo { hasNextPage endCursor }
      } }
    }"""
    cursor = None
    while pages < 900:
        if stop and stop.is_set():
            break
        pages += 1
        try:
            res = session.post(
                "https://gql.twitch.tv/gql", headers=headers,
                json={"query": query, "variables": {"id": video_id, "cursor": cursor}},
                timeout=20,
            )
            data = res.json()
        except (requests.RequestException, ValueError):
            break
        video = (data.get("data") or {}).get("video") or {}
        comments = video.get("comments") or {}
        for edge in comments.get("edges") or []:
            node = edge.get("node") or {}
            body = (node.get("message") or {}).get("body") or ""
            if not body:
                continue
            user = ((node.get("commenter") or {}).get("displayName") or "viewer")
            offset = float(node.get("contentOffsetSeconds") or 0)
            rows.append({"offset": offset, "user": user, "message": body})
            if on_message:
                on_message(offset, user, body)
        info = comments.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        cursor = info.get("endCursor")
        time.sleep(0.15)
    return rows


def download_youtube_vod_chat(video_id_or_url: str, *,
                              on_message: Callable[[float, str, str], None] | None = None,
                              stop: threading.Event | None = None,
                              max_messages: int = 120000) -> list[dict[str, Any]]:
    try:
        from chat_downloader import ChatDownloader
    except ImportError:
        return []
    url = video_id_or_url if video_id_or_url.startswith("http") else (
        f"https://www.youtube.com/watch?v={video_id_or_url}"
    )
    rows: list[dict[str, Any]] = []
    try:
        downloader = ChatDownloader()
        chat = downloader.get_chat(url, chat_type="replay", timeout=30,
                                   inactivity_timeout=30, message_groups=["messages"])
        for item in chat:
            if stop and stop.is_set():
                break
            if not isinstance(item, dict) or not item.get("message"):
                continue
            author = item.get("author") or {}
            user = author.get("name") if isinstance(author, dict) else "viewer"
            offset = item.get("time_in_seconds")
            if not isinstance(offset, (int, float)):
                ts = item.get("timestamp")
                if isinstance(ts, (int, float)) and ts > 1e11:
                    offset = 0.0
                else:
                    offset = 0.0
            row = {"offset": float(offset or 0), "user": str(user), "message": str(item["message"])}
            rows.append(row)
            if on_message:
                on_message(row["offset"], row["user"], row["message"])
            if len(rows) >= max_messages:
                break
    except Exception as exc:
        LOG.info("YouTube chat replay unavailable: %s", str(exc)[:200])
    return rows


def download_chat_replay(url: str, *, on_message: Callable[[float, str, str], None] | None = None,
                         stop: threading.Event | None = None) -> tuple[str, list[dict[str, Any]]]:
    """Return (kind, rows) for a Twitch or YouTube VOD URL."""
    if "twitch.tv/videos/" in url or re.fullmatch(r"\d+", url.strip()):
        vid = video_id_from_url(url) or url.strip()
        rows = download_twitch_vod_chat(vid, on_message=on_message, stop=stop)
        return "twitch", rows
    if "youtube.com" in url or "youtu.be" in url:
        vid = video_id_from_url(url) or url
        rows = download_youtube_vod_chat(vid, on_message=on_message, stop=stop)
        return "youtube", rows
    return "unknown", []


def chat_rate_from_rows(rows: list[dict[str, Any]], bucket: float = 1.0) -> list[tuple[float, float]]:
    """Convert offset-based chat rows into (offset, msgs_per_second) points."""
    if not rows:
        return []
    counts: dict[int, int] = {}
    for row in rows:
        off = int(float(row.get("offset") or 0) / bucket)
        counts[off] = counts.get(off, 0) + 1
    return [(k * bucket, v / bucket) for k, v in sorted(counts.items())]


chat_hub = ChatHub()
