"""Thread-safe in-memory studio state + small JSON persistence layer."""
from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from config import DIRS, settings
from utils import (
    LOG, append_jsonl, db_to_percent, hms, load_json, now_iso, save_json, slugify, today_str,
)


class StudioState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.started_at = now_iso()
        self.streamers: list[dict[str, Any]] = self._normalize_streamers(
            load_json(DIRS["data"] / "streamers.json", [])
        )
        self.active: dict[str, dict[str, Any]] = {}
        self.loudness: dict[str, deque] = {}
        self.chat_series: dict[str, deque] = {}
        self.chat_events: dict[str, deque] = {}
        self.transcript: dict[str, deque] = {}
        self.chat_logs: dict[str, Path] = {}
        self.spikes: deque = deque(maxlen=400)
        self.clips: list[dict[str, Any]] = load_json(DIRS["data"] / "clips.json", [])
        self.campaigns: list[dict[str, Any]] = load_json(DIRS["data"] / "campaigns.json", [])
        self.earnings: list[dict[str, Any]] = load_json(DIRS["data"] / "earnings.json", [])
        self.agent_outputs: list[dict[str, Any]] = load_json(DIRS["data"] / "agent_outputs.json", [])
        self.events: deque = deque(maxlen=200)
        self.warnings: deque = deque(maxlen=50)
        self.vod_job: dict[str, Any] = {"status": "idle"}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.stats: dict[str, Any] = {
            "ai_checks": 0,
            "auto_clips": 0,
            "spikes": 0,
            "recorded_seconds_accum": 0.0,
            "started_at": self.started_at,
        }
        ui = load_json(DIRS["data"] / "ui_state.json", {})
        self.demo = bool(ui.get("demo", settings.demo_mode))
        self.ai_error: str = ""
        self.ai_last_ok: str = ""
        self.recordings_paused = False
        self._load_spikes()
        self.add_event("info", "Clipping Studio started")

    # ------------------------------------------------------------------ setup
    def _load_spikes(self) -> None:
        rows = load_json(DIRS["data"] / "spikes.json", [])
        if isinstance(rows, list):
            for row in rows[-400:]:
                self.spikes.append(row)
            self.stats["spikes"] = len(rows)

    def _normalize_streamers(self, rows: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        if not isinstance(rows, list):
            return out
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or row.get("username") or "streamer").strip()
            platform = str(row.get("platform") or "twitch").strip().lower()
            if platform not in {"twitch", "youtube"}:
                platform = "twitch"
            base = str(row.get("id") or slugify(f"{platform}-{name}"))
            sid, n = base, 2
            while sid in seen:
                sid = f"{base}-{n}"
                n += 1
            seen.add(sid)
            out.append({
                "id": sid,
                "name": name,
                "platform": platform,
                "username": str(row.get("username") or row.get("handle") or "").strip().lstrip("@"),
                "channel_id": str(row.get("channel_id") or "").strip(),
                "url": str(row.get("url") or "").strip(),
                "handle": str(row.get("handle") or "").strip(),
                "enabled": bool(row.get("enabled", True)),
                "allowed_post": bool(row.get("allowed_post", False)),
                "avatar": str(row.get("avatar") or ""),
                "notes": str(row.get("notes") or ""),
            })
        return out

    def save_streamers(self) -> None:
        with self.lock:
            save_json(DIRS["data"] / "streamers.json", self.streamers)

    def save_ui(self) -> None:
        with self.lock:
            save_json(DIRS["data"] / "ui_state.json", {"demo": self.demo})

    def upsert_streamer(self, row: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            rows = self._normalize_streamers(self.streamers + [row])
            # Match by id, else by platform+username/name.
            target = None
            wanted_id = str(row.get("id") or "")
            for item in rows:
                if wanted_id and item["id"] == wanted_id:
                    target = item
                    break
            if target is None:
                key = (str(row.get("platform", "")).lower(),
                       str(row.get("username") or row.get("name") or "").lower().lstrip("@"))
                for item in self.streamers:
                    if (item["platform"], (item["username"] or item["name"]).lower()) == key:
                        target = item
                        break
            if target is None:
                target = rows[-1]
            else:
                updates = self._normalize_streamers([{**target, **row}])[0]
                updates["id"] = target["id"]
                target = updates
            merged = [s for s in self.streamers if s["id"] != target["id"]] + [target]
            self.streamers = self._normalize_streamers(merged)
            self.save_streamers()
            return target

    def delete_streamer(self, sid: str) -> bool:
        with self.lock:
            before = len(self.streamers)
            self.streamers = [s for s in self.streamers if s["id"] != sid]
            self.save_streamers()
            return len(self.streamers) < before

    def find_streamer(self, sid: str) -> dict[str, Any] | None:
        with self.lock:
            for s in self.streamers:
                if s["id"] == sid:
                    return dict(s)
            for s in self.active.values():
                if s.get("id") == sid:
                    return dict(s)
        return None

    # ----------------------------------------------------------------- events
    def add_event(self, level: str, message: str) -> None:
        with self.lock:
            self.events.appendleft({
                "ts": time.time(),
                "time": datetime.now().strftime("%H:%M:%S"),
                "level": level,
                "message": message,
            })

    def add_warning(self, message: str, level: str = "warn") -> None:
        with self.lock:
            self.warnings.appendleft({"ts": time.time(), "level": level, "message": message})
        self.add_event(level, message)

    def clear_warnings(self) -> None:
        with self.lock:
            self.warnings.clear()

    # -------------------------------------------------------------- recording
    def set_active(self, sid: str, data: dict[str, Any]) -> None:
        with self.lock:
            current = self.active.get(sid, {})
            current.update(data)
            current["id"] = sid
            self.active[sid] = current
            if not data.get("started_at") and data.get("status") == "recording":
                current.setdefault("started_at", now_iso())

    def remove_active(self, sid: str, keep: bool = False) -> None:
        with self.lock:
            row = self.active.pop(sid, None)
            if row and row.get("started_at"):
                try:
                    started = datetime.fromisoformat(row["started_at"]).timestamp()
                    self.stats["recorded_seconds_accum"] += max(0, time.time() - started)
                except ValueError:
                    pass
            if not keep:
                self.loudness.pop(sid, None)
                self.chat_series.pop(sid, None)
                self.chat_events.pop(sid, None)

    def active_seconds(self) -> float:
        with self.lock:
            now = time.time()
            total = float(self.stats.get("recorded_seconds_accum", 0))
            for row in self.active.values():
                if row.get("status") != "recording" or not row.get("started_at"):
                    continue
                try:
                    total += max(0, now - datetime.fromisoformat(row["started_at"]).timestamp())
                except ValueError:
                    pass
            return total

    # ------------------------------------------------------------------ chats
    def log_chat(self, sid: str, row: dict[str, Any]) -> None:
        path = self.chat_logs.get(sid)
        if path:
            append_jsonl(path, row)

    def add_chat(self, sid: str, user: str, message: str, ts: float | None = None,
                 offset: float | None = None, log: bool = True) -> None:
        ts = ts or time.time()
        row = {"ts": ts, "user": user or "anonymous", "message": (message or "")[:500]}
        if offset is not None:
            row["offset"] = offset
        with self.lock:
            dq = self.chat_events.setdefault(sid, deque(maxlen=4000))
            dq.append(row)
            if log:
                path = self.chat_logs.get(sid)
                if path:
                    append_jsonl(path, {"time": now_iso(), "user": row["user"],
                                        "message": row["message"], "offset": offset})

    def recent_chat(self, sid: str, seconds: float = 15, limit: int = 80) -> list[dict[str, Any]]:
        cutoff = time.time() - seconds
        with self.lock:
            rows = [r for r in self.chat_events.get(sid, ()) if r["ts"] >= cutoff]
        return rows[-limit:]

    def chat_rate(self, sid: str, window: float = 10, now: float | None = None) -> float:
        now = now or time.time()
        cutoff = now - window
        with self.lock:
            events = self.chat_events.get(sid, ())
            count = sum(1 for r in events if r["ts"] >= cutoff)
        return count / max(1.0, window)

    def chat_count(self, sid: str, start: float, end: float) -> int:
        with self.lock:
            return sum(1 for r in self.chat_events.get(sid, ()) if start <= r["ts"] < end)

    def add_chat_rate(self, sid: str, mps: float, ts: float | None = None) -> None:
        with self.lock:
            self.chat_series.setdefault(sid, deque(maxlen=7200)).append((ts or time.time(), mps))

    # --------------------------------------------------------------- loudness
    def add_loudness(self, sid: str, db: float, ts: float | None = None) -> None:
        with self.lock:
            self.loudness.setdefault(sid, deque(maxlen=15000)).append((ts or time.time(), db))

    def loudness_points(self, sid: str, seconds: float, now: float | None = None) -> list[tuple[float, float]]:
        now = now or time.time()
        cutoff = now - seconds
        with self.lock:
            return [(t, db) for t, db in self.loudness.get(sid, ()) if t >= cutoff]

    # ------------------------------------------------------------- transcript
    def add_transcript(self, sid: str, text: str, start: float | None = None,
                       end: float | None = None, source: str = "ai",
                       ts: float | None = None) -> None:
        text = " ".join((text or "").split())
        if not text:
            return
        with self.lock:
            dq = self.transcript.setdefault(sid, deque(maxlen=250))
            if dq and text == dq[-1].get("text"):
                return
            # Trim overlapping Whisper window repetitions.
            if dq:
                prev = dq[-1].get("text", "")
                words_new = text.split()
                words_prev = prev.split()
                for overlap in range(min(12, len(words_new), len(words_prev)), 1, -1):
                    if words_new[:overlap] == words_prev[-overlap:]:
                        text = " ".join(words_new[overlap:])
                        break
            if text:
                dq.append({
                    "ts": ts or time.time(),
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "text": text[:600],
                    "start": start,
                    "end": end,
                    "source": source,
                })

    def transcript_text(self, sid: str, since_ts: float | None = None) -> str:
        with self.lock:
            rows = list(self.transcript.get(sid, ()))
        if since_ts:
            rows = [r for r in rows if r["ts"] >= since_ts]
        return " ".join(r["text"] for r in rows)[-2000:]

    def recent_transcript(self, sid: str, limit: int = 5) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.transcript.get(sid, ()))[-limit:]

    # ----------------------------------------------------------------- spikes
    def add_spike(self, spike: dict[str, Any]) -> None:
        with self.lock:
            self.spikes.append(spike)
            self.stats["spikes"] = self.stats.get("spikes", 0) + 1
            save_json(DIRS["data"] / "spikes.json", list(self.spikes)[-400:])
        append_jsonl(DIRS["data"] / "spikes.jsonl", spike)

    def count_ai_check(self, ok: bool = True, error: str = "") -> None:
        with self.lock:
            self.stats["ai_checks"] = self.stats.get("ai_checks", 0) + 1
            if ok:
                self.ai_last_ok = now_iso()
                self.ai_error = ""
            else:
                self.ai_error = error[:300]

    # ------------------------------------------------------------------ clips
    def add_clip(self, clip: dict[str, Any]) -> None:
        with self.lock:
            self.clips.append(clip)
            self.clips = self.clips[-800:]
            save_json(DIRS["data"] / "clips.json", self.clips)
            self.stats["clips_count"] = len(self.clips)
            if clip.get("auto"):
                self.stats["auto_clips"] = self.stats.get("auto_clips", 0) + 1

    def update_clip(self, clip_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        with self.lock:
            for clip in self.clips:
                if clip.get("id") == clip_id:
                    clip.update(updates)
                    save_json(DIRS["data"] / "clips.json", self.clips)
                    return dict(clip)
        return None

    def get_clip(self, clip_id: str) -> dict[str, Any] | None:
        with self.lock:
            for clip in self.clips:
                if clip.get("id") == clip_id:
                    return dict(clip)
        return None

    def clips_today(self) -> int:
        today = today_str()
        with self.lock:
            return sum(1 for c in self.clips if str(c.get("date")) == today)

    # --------------------------------------------------------------- campaigns
    def save_campaigns(self) -> None:
        with self.lock:
            save_json(DIRS["data"] / "campaigns.json", self.campaigns)

    def save_earnings(self) -> None:
        with self.lock:
            save_json(DIRS["data"] / "earnings.json", self.earnings)

    def save_agent_outputs(self) -> None:
        with self.lock:
            self.agent_outputs = self.agent_outputs[-300:]
            save_json(DIRS["data"] / "agent_outputs.json", self.agent_outputs)

    # -------------------------------------------------------------------- vod
    def set_vod(self, data: dict[str, Any]) -> None:
        with self.lock:
            self.vod_job = data

    def set_job(self, job_id: str, data: dict[str, Any]) -> None:
        with self.lock:
            self.jobs[job_id] = data
            if len(self.jobs) > 50:
                for key in list(self.jobs)[:-50]:
                    self.jobs.pop(key, None)

    # ---------------------------------------------------------------- snapshot
    def _series(self, sid: str, seconds: int, now: float) -> dict[str, list[list[float]]]:
        loud: dict[int, float] = {}
        chat: dict[int, float] = {}
        for t, db in self.loudness.get(sid, ()):
            if t >= now - seconds:
                sec = int(t)
                loud[sec] = max(loud.get(sec, 0), db_to_percent(db))
        for t, mps in self.chat_series.get(sid, ()):
            if t >= now - seconds:
                sec = int(t)
                chat[sec] = max(chat.get(sec, 0), mps)
        start = int(now) - seconds
        loud_series = [[start + i, round(loud.get(start + i, 0), 1)] for i in range(seconds + 1)]
        chat_series = [[start + i, round(chat.get(start + i, 0), 2)] for i in range(seconds + 1)]
        return {"loudness": loud_series, "chat": chat_series}

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        with self.lock:
            active_list = []
            for row in self.active.values():
                item = dict(row)
                if item.get("started_at") and item.get("status") == "recording":
                    try:
                        item["elapsed"] = int(now - datetime.fromisoformat(item["started_at"]).timestamp())
                        item["elapsed_text"] = hms(item["elapsed"])
                    except ValueError:
                        item["elapsed"] = 0
                item["chat_last_10s"] = round(self.chat_rate(item["id"], 10, now), 2)
                active_list.append(item)
            active_list.sort(key=lambda r: (r.get("status") != "recording", r.get("name", "")))
            focus = next((r["id"] for r in active_list if r.get("status") == "recording"), None)
            if not focus and active_list:
                focus = active_list[0]["id"]
            series = {r["id"]: self._series(r["id"], 90, now) for r in active_list}
            transcript = {r["id"]: self.recent_transcript(r["id"], 8) for r in active_list}
            recent_chat = {r["id"]: list(self.chat_events.get(r["id"], ()))[-12:] for r in active_list}
            clips = [dict(c) for c in self.clips[-60:]][::-1]
            for clip in clips:
                clip["thumb_url"] = f"/media/clips/{clip['thumb']}" if clip.get("thumb") else ""
                clip["url"] = f"/media/clips/{clip['file']}" if clip.get("file") else ""
                clip["clean_url"] = f"/media/clips/{clip['clean_file']}" if clip.get("clean_file") else ""
                clip["srt_url"] = f"/media/clips/{clip['srt_file']}" if clip.get("srt_file") else ""
            spikes = list(self.spikes)[-80:]
            today = today_str()
            recorded = self.active_seconds()
            rec_today = sum(
                (c.get("duration") or 0) for c in self.clips if c.get("date") == today
            )
            campaigns = [dict(c) for c in self.campaigns]
            earnings = [dict(e) for e in self.earnings]
            total_earnings = sum(
                (float(e.get("views", 0) or 0) / 1000.0) * float(e.get("rate", 0) or 0)
                for e in earnings
            )
            return {
                "now": now,
                "started_at": self.started_at,
                "demo": self.demo,
                "paused": self.recordings_paused,
                "ai": {**settings.ai_public(), "error": self.ai_error, "last_ok": self.ai_last_ok},
                "stats": {
                    "recorded_seconds": round(recorded, 1),
                    "recorded_text": hms(recorded),
                    "buffer_seconds": min(settings.buffer_seconds, int(recorded)) if active_list else 0,
                    "buffer_text": hms(min(settings.buffer_seconds, int(recorded)) if active_list else 0),
                    "ai_checks": self.stats.get("ai_checks", 0),
                    "clips_total": len(self.clips),
                    "clips_today": self.clips_today(),
                    "clips_recorded_seconds_today": round(rec_today, 1),
                    "spikes": self.stats.get("spikes", 0),
                    "auto_clips": self.stats.get("auto_clips", 0),
                },
                "active": active_list,
                "streamers": [dict(s) for s in self.streamers],
                "focus": focus,
                "series": series,
                "transcript": transcript,
                "recent_chat": recent_chat,
                "clips": clips,
                "spikes": spikes,
                "events": list(self.events)[:40],
                "warnings": list(self.warnings)[:10],
                "vod": dict(self.vod_job),
                "jobs": list(self.jobs.values())[-20:],
                "campaigns": campaigns,
                "earnings": earnings,
                "earnings_total": round(total_earnings, 2),
                "agent_outputs": list(self.agent_outputs)[-20:][::-1],
                "settings": {
                    "buffer_seconds": settings.buffer_seconds,
                    "segment_seconds": settings.segment_seconds,
                    "spike_pre": settings.spike_pre,
                    "spike_post": settings.spike_post,
                    "auto_clip": settings.auto_clip,
                    "live_transcribe": settings.live_transcribe_interval,
                    "env": settings.env_status,
                    "paths": {
                        "recordings": str(DIRS["recordings"]),
                        "clips": str(DIRS["clips"]),
                        "transcripts": str(DIRS["transcripts"]),
                        "agent_outputs": str(DIRS["agent_outputs"]),
                    },
                },
            }


state: StudioState | None = None


def get_state() -> StudioState:
    global state
    if state is None:
        state = StudioState()
    return state
