"""Always-on monitor: watch streamers, record, detect, auto-clip.

Demo mode simulates a live stream (loudness, chat, transcript) so the whole
dashboard is testable without any network or credentials. Turning demo off
switches the same loop to real Twitch/YouTube checks.
"""
from __future__ import annotations

import os
import random
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

import ai
import detector
from chat import chat_hub
from clipper import create_live_clip, demo_clip
from config import DIRS, settings
from recorder import recorder_manager
from state import get_state
from transcriber import live_transcriber
from utils import LOG, load_json, now_iso, run_cmd, safe_name, save_json, which

DEMO_LINES = [
    "chat what do you mean it's not that hard",
    "bro look at this angle, watch, watch",
    "okay okay one more try and then we rotate",
    "no way, no way he actually hit that",
    "I'm telling you this lobby is different",
    "if this works I'm ending the stream",
    "that was the craziest thing I've ever seen",
    "chat is lying to me, there is no way",
    "oh my god, oh my god, okay we're fine",
    "clip that, clip that right now",
]
DEMO_HYPE = [
    "NO WAY HE ACTUALLY HIT THAT",
    "WAIT WAIT WAIT WHAT WAS THAT",
    "THAT IS THE CRAZIEST SHOT I HAVE EVER SEEN",
    "CHAT CHAT CHAT DID YOU SEE THAT",
]


class Monitor:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.started_at: str = ""
        self._twitch_token: tuple[str, float] = ("", 0.0)
        self._last_check: dict[str, float] = {}
        self._last_detect: dict[str, float] = {}
        self._last_clip: dict[str, float] = {}
        self._recent_spikes: list[dict[str, Any]] = []
        self._clip_threads: list[threading.Thread] = []
        self._demo_next_spike: dict[str, float] = {}
        self._demo_started: set[str] = set()
        self._seeded = False
        self._offline_misses: dict[str, int] = {}
        self._errors: dict[str, str] = {}

    # ---------------------------------------------------------------- control
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(self) -> None:
        with self.lock:
            if self.running():
                return
            self.stop_event.clear()
            self.started_at = now_iso()
            self.thread = threading.Thread(target=self._loop, name="monitor", daemon=True)
            self.thread.start()
        state = get_state()
        state.add_event("info", "Monitor started in demo mode" if state.demo else "Monitor started")

    def stop(self) -> None:
        self.stop_event.set()
        with self.lock:
            thread = self.thread
        if thread and thread.is_alive():
            thread.join(timeout=5)
        self.thread = None

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running(),
            "started_at": self.started_at,
            "demo": get_state().demo,
            "errors": dict(self._errors),
        }

    # ------------------------------------------------------------------- loop
    def _loop(self) -> None:
        state = get_state()
        try:
            self._seed_demo_clip()
        except Exception:  # noqa: BLE001
            LOG.exception("Demo seed failed")
        while not self.stop_event.is_set():
            started = time.time()
            try:
                if state.demo:
                    self._demo_tick()
                else:
                    self._real_tick()
                self._detect_all()
            except Exception:  # noqa: BLE001 - a monitor bug must not kill recording
                LOG.exception("Monitor tick failed")
                state.add_warning("Monitor tick failed (see log)", "error")
            elapsed = time.time() - started
            target = 2.0 if state.demo else float(max(5, settings.poll_seconds))
            for _ in range(int(max(1, target - elapsed) * 2)):
                if self.stop_event.is_set():
                    break
                time.sleep(0.5)
        state.add_event("info", "Monitor stopped")

    # ------------------------------------------------------------- demo mode
    def _demo_tick(self) -> None:
        state = get_state()
        now = time.time()
        rows = [s for s in state.streamers if s.get("enabled")] or [{
            "id": "demo-runner", "name": "Demo Runner", "platform": "twitch",
            "username": "demo_runner", "enabled": True,
        }]
        for streamer in rows:
            sid = streamer["id"]
            if sid not in self._demo_started:
                state.set_active(sid, {
                    "status": "recording", "demo": True, "started_at": now_iso(),
                    "name": streamer["name"], "platform": streamer["platform"],
                    "username": streamer.get("username", ""),
                    "preview_url": "",
                })
                self._demo_started.add(sid)
                self._demo_next_spike[sid] = now + random.uniform(8, 20)
            active = state.active.get(sid) or {}
            if active.get("status") == "stopped":
                continue

            spike_on = now >= self._demo_next_spike.get(sid, 0)
            db = -38 + 3.2 * random.random()
            if spike_on:
                db = -12 + 5 * random.random()
            state.add_loudness(sid, db, now)
            state.add_chat_rate(sid, round(random.uniform(0.2, 0.8) + (5.5 if spike_on else 0), 2), now)
            if spike_on and random.random() < 0.55:
                for _ in range(random.randint(2, 5)):
                    state.add_chat(sid, f"viewer{random.randint(2, 64)}",
                                   random.choice(["LETS GO", "NO WAY", "W", "clip it", "OMG", "XD"]),
                                   now - random.random() * 2, log=False)
            if spike_on and now >= self._demo_next_spike.get(sid, 0) + 7:
                state.add_transcript(sid, random.choice(DEMO_HYPE), ts=now - 3, source="demo")
                self._demo_next_spike[sid] = now + random.uniform(55, 95)
            elif random.random() < 0.06:
                state.add_transcript(sid, random.choice(DEMO_LINES), ts=now, source="demo")
            state.set_active(sid, {"chat_last_10s_hint": 0})

    # ------------------------------------------------------------- real mode
    def _real_tick(self) -> None:
        state = get_state()
        streamers = [s for s in state.streamers if s.get("enabled")]
        for streamer in streamers:
            sid = streamer["id"]
            if time.time() - self._last_check.get(sid, 0) < max(15, settings.poll_seconds):
                if sid in state.active:
                    self._feed_recorder(sid)
                continue
            self._last_check[sid] = time.time()
            live, video_id, title = self._check_live(streamer)
            rec = recorder_manager.get(sid)
            if live:
                self._offline_misses[sid] = 0
                url = self._stream_url(streamer)
                if not rec or rec.status in {"stopped", "error"}:
                    if state.recordings_paused:
                        state.set_active(sid, {"status": "paused", "name": streamer["name"]})
                        continue
                    try:
                        rec = recorder_manager.start(streamer, url)
                        state.set_active(sid, {**rec.snapshot(), "title": title})
                        chat_hub.start_live(streamer, video_id)
                        live_transcriber.start(sid)
                        state.add_event("success", f"LIVE: {streamer['name']} — recording started")
                    except Exception as exc:  # noqa: BLE001
                        self._errors[sid] = str(exc)[:200]
                        state.add_warning(f"Could not start {streamer['name']}: {str(exc)[:120]}", "error")
                else:
                    state.set_active(sid, {"status": rec.status, "title": title})
                    self._feed_recorder(sid)
            else:
                misses = self._offline_misses.get(sid, 0) + 1
                self._offline_misses[sid] = misses
                if rec and misses >= 2:
                    self._stop_streamer(sid, "Stream ended")
                elif sid in state.active:
                    state.set_active(sid, {"status": "offline"})

    def _stop_streamer(self, sid: str, reason: str) -> None:
        state = get_state()
        try:
            chat_hub.stop(sid)
        except Exception:  # noqa: BLE001
            pass
        try:
            live_transcriber.stop(sid)
        except Exception:  # noqa: BLE001
            pass
        rec_obj = recorder_manager.get(sid)
        rec = recorder_manager.stop(sid, reason)
        state.remove_active(sid)
        if rec_obj:
            def finalize() -> None:
                try:
                    rec_obj.finalize_chunks(force=True)
                    rec_obj.build_full(reason)
                except Exception as exc:  # noqa: BLE001
                    LOG.info("Finalising recording failed: %s", str(exc)[:180])
            threading.Thread(target=finalize, daemon=True).start()
        state.add_event("info", f"Recording stopped: {reason}")

    def _feed_recorder(self, sid: str) -> None:
        state = get_state()
        rec = recorder_manager.get(sid)
        if not rec:
            return
        if rec.status in {"stopped", "error"}:
            return
        try:
            points = rec.analyze_new_segments()
            for ts, db in points:
                state.add_loudness(sid, db, ts)
        except Exception as exc:  # noqa: BLE001
            LOG.info("Loudness analysis failed for %s: %s", sid, str(exc)[:160])
        state.add_chat_rate(sid, round(state.chat_rate(sid, 5), 2))
        try:
            rec.finalize_chunks()
        except Exception as exc:  # noqa: BLE001
            LOG.info("Chunk finalize failed: %s", str(exc)[:160])
        if settings.max_recording_gb > 0:
            try:
                used = sum(f.stat().st_size for f in rec.session_dir.rglob("*.ts"))
                if used > settings.max_recording_gb * 1024 ** 3:
                    state.add_warning(f"{rec.streamer['name']}: recording cap reached", "warn")
                    self._stop_streamer(sid, "Size cap reached")
            except OSError:
                pass
        state.set_active(sid, {"status": rec.status, **{
            k: v for k, v in rec.snapshot().items() if k in {"elapsed", "segments", "chunks", "error"}
        }})

    # ------------------------------------------------------------ live check
    def _twitch_creds(self) -> tuple[str, str]:
        return settings.twitch_client_id, settings.twitch_client_secret or settings.twitch_token

    def _twitch_app_token(self) -> str:
        token, expires = self._twitch_token
        if token and time.time() < expires:
            return token
        client_id, secret = self._twitch_creds()
        if not (client_id and secret):
            return ""
        try:
            resp = requests.post(
                "https://id.twitch.tv/oauth2/token",
                params={"client_id": client_id, "client_secret": secret,
                        "grant_type": "client_credentials"},
                timeout=15,
            )
            data = resp.json()
            token = str(data.get("access_token") or "")
            if token:
                self._twitch_token = (token, time.time() + max(60, int(data.get("expires_in", 3600)) - 120))
        except requests.RequestException as exc:
            LOG.info("Twitch token failed: %s", str(exc)[:160])
        return token

    def _check_live(self, streamer: dict[str, Any]) -> tuple[bool, str, str]:
        platform = streamer.get("platform")
        username = (streamer.get("username") or streamer.get("name") or "").strip()
        if platform == "twitch":
            client_id, _ = self._twitch_creds()
            token = self._twitch_app_token()
            if client_id and token:
                try:
                    resp = requests.get(
                        "https://api.twitch.tv/helix/streams",
                        params={"user_login": username.lower()},
                        headers={"Client-Id": client_id, "Authorization": f"Bearer {token}"},
                        timeout=15,
                    )
                    data = resp.json().get("data") or []
                    if data:
                        return True, "", str(data[0].get("title") or "")
                    return False, "", ""
                except (requests.RequestException, ValueError) as exc:
                    self._errors[streamer["id"]] = f"Twitch API: {str(exc)[:160]}"
        return self._ytdlp_live(streamer)

    def _ytdlp_live(self, streamer: dict[str, Any]) -> tuple[bool, str, str]:
        if not which(settings.ytdlp):
            self._errors[streamer["id"]] = "yt-dlp is not installed"
            return False, "", ""
        url = self._stream_url(streamer)
        cmd = [settings.ytdlp, "--no-warnings", "--simulate", "--no-playlist",
               "--print", "%(is_live)s", "--print", "%(id)s", "--print", "%(title)s", url]
        res = run_cmd(cmd, timeout=45)
        if res.returncode != 0:
            self._errors[streamer["id"]] = (res.stderr or "yt-dlp check failed")[-180:]
            return False, "", ""
        lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
        live = bool(lines and lines[0].lower() in {"true", "yes", "1"})
        video_id = lines[1] if len(lines) > 1 else ""
        title = lines[2] if len(lines) > 2 else ""
        return live, video_id, title

    def _stream_url(self, streamer: dict[str, Any]) -> str:
        if streamer.get("url"):
            return str(streamer["url"])
        username = (streamer.get("username") or streamer.get("name") or "").strip()
        if streamer.get("platform") == "twitch":
            return f"https://www.twitch.tv/{username}"
        handle = username.lstrip("@")
        if streamer.get("channel_id"):
            return f"https://www.youtube.com/channel/{streamer['channel_id']}/live"
        return f"https://www.youtube.com/@{handle}/live"

    # --------------------------------------------------------------- detect
    def _detect_all(self) -> None:
        state = get_state()
        if state.demo:
            sids = [s["id"] for s in state.streamers if s.get("enabled")] or list(state.active)
        else:
            sids = [sid for sid, row in state.active.items() if row.get("status") == "recording"]
        now = time.time()
        for sid in sids:
            if now - self._last_detect.get(sid, 0) < 20:
                continue
            self._last_detect[sid] = now
            try:
                spikes = detector.detect_live(sid, window=45, now=now,
                                              use_ai=False if state.demo else None)
            except Exception as exc:  # noqa: BLE001
                LOG.info("detect_live failed for %s: %s", sid, str(exc)[:160])
                continue
            for spike in spikes:
                if self._is_duplicate(spike):
                    continue
                spike["demo"] = bool(state.demo)
                state.add_spike(spike)
                self._recent_spikes.append(spike)
                self._recent_spikes = self._recent_spikes[-60:]
                state.add_event(
                    "success" if spike.get("score", 0) >= 70 else "info",
                    f"Spike {spike.get('score', 0)}/100 on {spike.get('streamer')}: "
                    f"{str(spike.get('title') or spike.get('reason'))[:70]}",
                )
                if not state.demo and settings.auto_clip:
                    self._maybe_clip(sid, spike)

    def _is_duplicate(self, spike: dict[str, Any]) -> bool:
        now = time.time()
        for old in self._recent_spikes:
            if old.get("streamer_id") != spike.get("streamer_id"):
                continue
            if now - float(old.get("at", 0)) > 240:
                continue
            if detector._overlap(float(old.get("window_start", 0)), float(old.get("window_end", 0)),
                                 float(spike.get("window_start", 0)), float(spike.get("window_end", 0))) > 4:
                return True
        return False

    def _maybe_clip(self, sid: str, spike: dict[str, Any]) -> None:
        state = get_state()
        threshold = float(os.environ.get("AUTO_CLIP_MIN_SCORE", "70"))
        if float(spike.get("score", 0)) < threshold:
            return
        now = time.time()
        if now - self._last_clip.get(sid, 0) < max(10, settings.auto_clip_cooldown):
            return
        limit = settings.max_auto_clips_per_stream
        if limit > 0:
            made = sum(1 for c in state.clips if c.get("auto") and c.get("streamer_id") == sid)
            if made >= limit:
                return
        self._last_clip[sid] = now
        seconds = int(min(60, max(20, float(spike.get("duration", 20)) + 14)))

        def work() -> None:
            try:
                clip = create_live_clip(seconds, sid, spike=spike, auto=True)
                state.update_clip(clip["id"], {"streamer_id": sid, "detection": spike.get("source", "")})
                state.add_event("success", f"Auto-clip ready: {clip['title']}")
            except Exception as exc:  # noqa: BLE001
                state.add_warning(f"Auto-clip failed: {str(exc)[:140]}", "error")
                LOG.info("Auto-clip failed: %s", exc)

        thread = threading.Thread(target=work, name=f"clip-{sid}", daemon=True)
        thread.start()
        self._clip_threads = [t for t in self._clip_threads if t.is_alive()] + [thread]

    # ------------------------------------------------------------ demo seed
    def _seed_demo_clip(self) -> None:
        state = get_state()
        if not state.demo or self._seeded:
            return
        self._seeded = True
        if not bool(os.environ.get("DEMO_SEED_CLIP", "1")):
            return
        flag = DIRS["data"] / ".demo_clip_seeded"
        if flag.exists() or state.clips:
            return
        seed = os.environ.get("DEMO_STREAMER_NAME", "Demo Runner")

        def work() -> None:
            time.sleep(1.5)
            try:
                clip = demo_clip(duration=int(os.environ.get("DEMO_CLIP_SECONDS", "30")),
                                 streamer=safe_name(seed, 30))
                state.update_clip(clip["id"], {"demo": True})
                flag.write_text(now_iso(), encoding="utf-8")
                state.add_event("success", "Demo clip rendered — the pipeline works")
            except Exception as exc:  # noqa: BLE001
                LOG.warning("Demo clip failed: %s", exc)
                state.add_warning(f"Demo clip failed: {str(exc)[:140]}", "warn")

        threading.Thread(target=work, name="demo-clip", daemon=True).start()


monitor = Monitor()
