"""Clipping Studio web server.

Run: python app.py  (then open http://127.0.0.1:8765)
The server binds to localhost only. No request ever receives an API key.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request, send_file

import ai
import campaigns as campaign_lib
import detector
import publisher
from agents import delete_output, get_agent, list_agents, run_agent
from chat import chat_hub
from clipper import create_live_clip, demo_clip
from config import DIRS, reload_settings, settings
from monitor import monitor
from recorder import recorder_manager
from state import get_state
from transcriber import live_transcriber
from utils import (
    LOG, ffmpeg_available, hms, load_json, now_iso, open_in_file_manager,
    safe_join, save_json, today_str, ytdlp_available,
)
from vod_mode import cancel_vod, platform_from_url, start_vod, start_vod_file

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["JSON_SORT_KEYS"] = False
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0


# ------------------------------------------------------------------ helpers
def _error(message: str, code: int = 400, **extra: Any):
    return jsonify({"ok": False, "error": message, **extra}), code


def _body() -> dict[str, Any]:
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data
    return {k: v for k, v in request.form.items()}


def _sid(name: str) -> str:
    return str(name or "").strip()


def _clip_from_disk(clip_id: str) -> dict[str, Any] | None:
    return get_state().get_clip(clip_id)


def _spike_by_id(spike_id: str) -> dict[str, Any] | None:
    if not spike_id:
        return None
    for row in get_state().spikes:
        if row.get("id") == spike_id:
            return dict(row)
    return None


def _delete_clip(clip_id: str) -> bool:
    state = get_state()
    with state.lock:
        clip = next((c for c in state.clips if c.get("id") == clip_id), None)
        if not clip:
            return False
        for key in ("file", "clean_file", "srt_file", "ass_file", "thumb"):
            rel = clip.get(key)
            if not rel:
                continue
            path = safe_join(DIRS["clips"], rel)
            if path and path.is_file():
                try:
                    path.unlink()
                except OSError:
                    pass
        state.clips = [c for c in state.clips if c.get("id") != clip_id]
        save_json(DIRS["data"] / "clips.json", state.clips)
    return True


@app.errorhandler(Exception)
def _on_error(exc: Exception):  # noqa: ANN001 - Flask signature
    if isinstance(exc, (FileNotFoundError, KeyError, ValueError, RuntimeError)):
        return _error(str(exc)[:400] if str(exc) else exc.__class__.__name__, 400)
    LOG.exception("Unhandled request error")
    return _error("Server error — check the terminal log", 500)


@app.after_request
def _headers(resp):  # noqa: ANN001
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


# -------------------------------------------------------------------- pages
@app.get("/")
def index():
    return render_template("index.html")


@app.get("/favicon.ico")
def favicon():
    return ("", 204)


@app.get("/api/health")
def health():
    state = get_state()
    return jsonify({
        "ok": True,
        "demo": state.demo,
        "monitor": monitor.status(),
        "ffmpeg": ffmpeg_available(),
        "ytdlp": ytdlp_available(),
        "ai": settings.ai_public(),
        "clips": len(state.clips),
        "time": now_iso(),
    })


# -------------------------------------------------------------------- state
@app.get("/api/state")
def api_state():
    state = get_state()
    snap = state.snapshot()
    snap["monitor"] = monitor.status()
    snap["earnings_summary"] = campaign_lib.summary()
    snap["agents"] = list_agents()
    snap["platforms"] = publisher.PLATFORMS
    snap["version"] = "1.0.0"
    return jsonify(snap)


@app.post("/api/settings/reload")
def api_reload():
    reload_settings()
    get_state().add_event("info", "Settings reloaded from .env")
    return jsonify({"ok": True, "ai": settings.ai_public()})


@app.post("/api/ai/ping")
def api_ping():
    try:
        result = ai.ping()
        get_state().count_ai_check(True)
        return jsonify({"ok": True, **result})
    except ai.AIError as exc:
        get_state().count_ai_check(False, str(exc))
        return _error(str(exc), 400)


@app.post("/api/demo/toggle")
def api_demo_toggle():
    state = get_state()
    data = _body()
    state.demo = bool(data.get("demo", not state.demo))
    state.save_ui()
    state.add_event("info", f"Demo mode {'on' if state.demo else 'off'}")
    return jsonify({"ok": True, "demo": state.demo})


@app.post("/api/pause")
def api_pause():
    state = get_state()
    data = _body()
    state.recordings_paused = bool(data.get("paused", not state.recordings_paused))
    state.add_event("info", "Recording paused" if state.recordings_paused else "Recording resumed")
    return jsonify({"ok": True, "paused": state.recordings_paused})


# ---------------------------------------------------------------- streamers
@app.post("/api/streamers")
def api_streamer_upsert():
    data = _body()
    if not str(data.get("name") or "").strip():
        return _error("Streamer name is required")
    row = get_state().upsert_streamer(data)
    get_state().add_event("info", f"Streamer saved: {row['name']}")
    return jsonify({"ok": True, "streamer": row})


@app.delete("/api/streamers/<sid>")
def api_streamer_delete(sid: str):
    state = get_state()
    if recorder_manager.get(sid):
        recorder_manager.stop(sid, "streamer removed")
        chat_hub.stop(sid)
        live_transcriber.stop(sid)
        state.remove_active(sid)
    ok = state.delete_streamer(sid)
    return jsonify({"ok": ok})


@app.post("/api/record/start")
def api_record_start():
    state = get_state()
    data = _body()
    sid = _sid(data.get("sid") or data.get("id"))
    streamer = state.find_streamer(sid)
    if not streamer:
        return _error("Pick a streamer first")
    if state.demo:
        state.set_active(sid, {
            "status": "recording", "demo": True, "started_at": now_iso(),
            "name": streamer["name"], "platform": streamer["platform"],
        })
        return jsonify({"ok": True, "demo": True,
                        "message": "Demo mode: simulated stream started"})
    url = str(data.get("url") or "").strip() or monitor._stream_url(streamer)
    if recorder_manager.get(sid):
        return jsonify({"ok": True, "already": True, "message": "Already recording"})
    rec = recorder_manager.start(streamer, url)
    state.set_active(sid, rec.snapshot())
    chat_hub.start_live(streamer, str(data.get("video_id") or "") or None)
    live_transcriber.start(sid)
    return jsonify({"ok": True, "recorder": rec.snapshot()})


@app.post("/api/record/stop")
def api_record_stop():
    state = get_state()
    data = _body()
    sid = _sid(data.get("sid") or data.get("id"))
    if data.get("all"):
        stopped = recorder_manager.stop_all("stopped by user")
        chat_hub.stop_all()
        live_transcriber.stop_all()
        for item in stopped:
            state.remove_active(str(item.get("id") or ""))
        return jsonify({"ok": True, "stopped": stopped})
    if not sid:
        return _error("Missing streamer id")
    was_demo = bool((state.active.get(sid) or {}).get("demo"))
    rec_obj = recorder_manager.get(sid)
    result = recorder_manager.stop(sid, "stopped by user")
    chat_hub.stop(sid)
    live_transcriber.stop(sid)
    state.remove_active(sid)
    if rec_obj:
        threading.Thread(target=lambda: (rec_obj.finalize_chunks(force=True),
                                         rec_obj.build_full("stopped")),
                         daemon=True).start()
    if was_demo and not result:
        return jsonify({"ok": True, "demo": True, "message": "Demo stream stopped"})
    return jsonify({"ok": bool(result), "recorder": result})


# -------------------------------------------------------------------- clips
@app.post("/api/clip/live")
def api_clip_live():
    state = get_state()
    data = _body()
    sid = _sid(data.get("sid") or data.get("id"))
    seconds = max(5, min(120, int(data.get("seconds") or 40)))
    if state.demo:
        try:
            clip = demo_clip(duration=min(60, seconds), streamer=str(
                (state.find_streamer(sid) or {}).get("name") or "Demo Runner"))
            state.update_clip(clip["id"], {"demo": True})
            return jsonify({"ok": True, "clip": clip, "demo": True})
        except Exception as exc:  # noqa: BLE001
            return _error(f"Demo clip failed: {exc}", 500)
    if not sid:
        return _error("Missing streamer id")
    spike = _spike_by_id(str(data.get("spike_id") or ""))
    try:
        clip = create_live_clip(seconds, sid, spike=spike, auto=False,
                                end_offset=float(data.get("end_offset") or 0))
        return jsonify({"ok": True, "clip": clip})
    except (RuntimeError, FileNotFoundError, ValueError) as exc:
        return _error(str(exc), 400)


@app.post("/api/clip/demo")
def api_clip_demo():
    data = _body()
    try:
        clip = demo_clip(
            duration=max(10, min(90, int(data.get("duration") or 30))),
            title=str(data.get("title") or "no way he actually hit that")[:80],
            streamer=str(data.get("streamer") or "Demo Runner")[:40],
            score=int(data.get("score") or 92),
        )
        get_state().update_clip(clip["id"], {"demo": True})
        return jsonify({"ok": True, "clip": clip})
    except Exception as exc:  # noqa: BLE001
        return _error(f"Demo render failed: {exc}", 500)


@app.post("/api/detect")
def api_detect():
    state = get_state()
    data = _body()
    sid = _sid(data.get("sid") or data.get("id"))
    if not sid:
        return _error("Missing streamer id")
    spikes = detector.detect_live(sid, window=max(20, min(180, int(data.get("window") or 45))))
    for spike in spikes:
        if not monitor._is_duplicate(spike):
            state.add_spike(spike)
            monitor._recent_spikes.append(spike)
    return jsonify({"ok": True, "spikes": spikes, "ai": ai.enabled()})


@app.delete("/api/clips/<clip_id>")
def api_clip_delete(clip_id: str):
    return jsonify({"ok": _delete_clip(clip_id)})


@app.post("/api/clip/publish")
def api_publish():
    data = _body()
    clip_id = _sid(data.get("clip_id") or data.get("id"))
    if not clip_id:
        return _error("Missing clip id")
    platforms = data.get("platforms")
    if isinstance(platforms, str):
        platforms = [p.strip() for p in platforms.split(",") if p.strip()]
    result = publisher.publish(clip_id, platforms if isinstance(platforms, list) else None)
    return jsonify(result)


@app.post("/api/clip/copy")
def api_copy():
    data = _body()
    clip_id = _sid(data.get("clip_id") or data.get("id"))
    platform = str(data.get("platform") or "tiktok")
    ok, message = publisher.copy_caption(clip_id, platform)
    return jsonify({"ok": ok, "message": message})


@app.post("/api/clip/package")
def api_package():
    data = _body()
    clip_id = _sid(data.get("clip_id") or data.get("id"))
    platforms = data.get("platforms")
    if isinstance(platforms, str):
        platforms = [p.strip() for p in platforms.split(",") if p.strip()]
    try:
        package = publisher.build_package(clip_id, platforms if isinstance(platforms, list) else None)
    except (KeyError, FileNotFoundError) as exc:
        return _error(str(exc), 404)
    return jsonify({"ok": True, "package": package})


@app.post("/api/clip/meta")
def api_clip_meta():
    """Re-run AI title/hook/caption for a clip (manual edits stay manual)."""
    data = _body()
    clip_id = _sid(data.get("clip_id") or data.get("id"))
    clip = _clip_from_disk(clip_id)
    if not clip:
        return _error("Clip not found", 404)
    if not ai.enabled():
        return _error("AI endpoint not configured")
    try:
        meta = ai.score_viral(
            streamer=clip.get("streamer", ""), platform=clip.get("platform", ""),
            window_seconds=float(clip.get("duration") or 30),
            loudness_peak=-10, loudness_avg=-30, chat_mps=0, chat_avg=0,
            chat_sample="", transcript=clip.get("transcript", ""),
        )
    except ai.AIError as exc:
        return _error(str(exc), 400)
    updates = {k: meta[k] for k in ("score", "verdict", "reason", "title", "hook", "caption", "hashtags")
               if k in meta}
    updates["viral_score"] = meta.get("score", clip.get("viral_score"))
    updated = get_state().update_clip(clip_id, updates)
    return jsonify({"ok": True, "clip": updated, "meta": meta})


# ---------------------------------------------------------------------- VOD
@app.post("/api/vod")
def api_vod():
    data = _body()
    url = str(data.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return _error("Paste a full video URL")
    try:
        job = start_vod(
            url,
            streamer=str(data.get("streamer") or "")[:60],
            platform=str(data.get("platform") or platform_from_url(url)),
            max_clips=max(1, min(12, int(data.get("max_clips") or 6))),
            min_len=max(5, min(120, int(data.get("min_len") or 15))),
            max_len=max(10, min(180, int(data.get("max_len") or 60))),
            max_minutes=int(data.get("max_minutes") or 0) if data.get("max_minutes") else None,
            with_chat=bool(data.get("with_chat", True)),
        )
        return jsonify({"ok": True, "job": job})
    except (ValueError, RuntimeError) as exc:
        return _error(str(exc), 400)


@app.post("/api/vod/file")
def api_vod_file():
    data = _body()
    path = str(data.get("path") or "").strip()
    if not path:
        return _error("Give an absolute file path")
    try:
        job = start_vod_file(
            path,
            streamer=str(data.get("streamer") or "")[:60],
            max_clips=max(1, min(12, int(data.get("max_clips") or 6))),
            min_len=max(5, min(120, int(data.get("min_len") or 15))),
            max_len=max(10, min(180, int(data.get("max_len") or 60))),
        )
        return jsonify({"ok": True, "job": job})
    except (FileNotFoundError, RuntimeError) as exc:
        return _error(str(exc), 400)


@app.post("/api/vod/cancel")
def api_vod_cancel():
    return jsonify({"ok": True, "job": cancel_vod()})


# ---------------------------------------------------------------- campaigns
@app.post("/api/campaigns")
def api_campaign_upsert():
    data = _body()
    if not str(data.get("name") or "").strip():
        return _error("Campaign name is required")
    try:
        row = campaign_lib.upsert_campaign(data)
    except (TypeError, ValueError) as exc:
        return _error(f"Bad campaign numbers: {exc}")
    return jsonify({"ok": True, "campaign": row})


@app.delete("/api/campaigns/<campaign_id>")
def api_campaign_delete(campaign_id: str):
    return jsonify({"ok": campaign_lib.delete_campaign(campaign_id)})


@app.post("/api/earnings")
def api_earning_add():
    data = _body()
    try:
        row = campaign_lib.add_earning(data)
    except (TypeError, ValueError) as exc:
        return _error(f"Bad earnings row: {exc}")
    return jsonify({"ok": True, "earning": row, "summary": campaign_lib.summary()})


@app.delete("/api/earnings/<earning_id>")
def api_earning_delete(earning_id: str):
    return jsonify({"ok": campaign_lib.delete_earning(earning_id)})


@app.post("/api/earnings/import")
def api_earning_import():
    data = _body()
    text = str(data.get("csv") or "")
    if not text:
        raw = request.files.get("file")
        if raw:
            text = raw.read().decode("utf-8", errors="replace")
    if not text:
        return _error("Upload a CSV or paste its contents")
    try:
        result = campaign_lib.import_csv(text, campaign_id=str(data.get("campaign_id") or ""))
    except ValueError as exc:
        return _error(f"CSV import failed: {exc}")
    return jsonify({"ok": True, **result, "summary": campaign_lib.summary()})


@app.get("/api/earnings")
def api_earnings():
    return jsonify({"ok": True, **campaign_lib.summary()})


# ------------------------------------------------------------------- agents
@app.get("/api/agents")
def api_agents():
    return jsonify({"ok": True, "agents": list_agents(), "ai": ai.enabled()})


@app.post("/api/agents/run")
def api_agent_run():
    data = _body()
    agent_id = _sid(data.get("agent_id") or data.get("id"))
    if not get_agent(agent_id):
        return _error("Unknown agent")
    context: dict[str, Any] = {
        "streamer": data.get("streamer") or "",
        "platform": data.get("platform") or "",
        "title": data.get("title") or "",
        "transcript": data.get("transcript") or "",
        "extra": data.get("extra") or "",
        "date": data.get("date") or today_str(),
    }
    clip_id = _sid(data.get("clip_id"))
    if clip_id:
        clip = _clip_from_disk(clip_id)
        if clip:
            context.update({
                "streamer": context["streamer"] or clip.get("streamer", ""),
                "platform": context["platform"] or clip.get("platform", ""),
                "title": context["title"] or clip.get("title", ""),
                "transcript": context["transcript"] or clip.get("transcript", ""),
            })
    try:
        record = run_agent(agent_id, context)
    except ai.AIError as exc:
        return _error(str(exc), 400)
    return jsonify({"ok": True, "output": record})


@app.delete("/api/agents/output/<output_id>")
def api_agent_output_delete(output_id: str):
    return jsonify({"ok": delete_output(output_id)})


@app.get("/api/agents/output/<path:filename>")
def api_agent_output_file(filename: str):
    path = safe_join(DIRS["agent_outputs"], filename)
    if not path or not path.is_file():
        return _error("Output not found", 404)
    return send_file(path, mimetype="text/markdown")


# ------------------------------------------------------------------- misc
@app.post("/api/open")
def api_open():
    data = _body()
    target = str(data.get("target") or "clips")
    allowed = {"clips": DIRS["clips"], "recordings": DIRS["recordings"],
               "agents": DIRS["agent_outputs"], "packages": DIRS["post_packages"]}
    folder = allowed.get(target)
    if not folder:
        return _error("Unknown folder")
    ok, message = open_in_file_manager(folder)
    return jsonify({"ok": ok, "message": message})


@app.get("/media/clips/<path:rel>")
def media_clip(rel: str):
    path = safe_join(DIRS["clips"], rel)
    if not path or not path.is_file():
        return ("", 404)
    return send_file(path, conditional=True)


# --------------------------------------------------------------- lifecycle
def _startup() -> None:
    state = get_state()
    settings.reload()
    if not state.streamers:
        state.streamers = state._normalize_streamers([{
            "name": "Demo Runner", "platform": "twitch", "username": "demo_runner",
            "enabled": True, "notes": "Ships with the demo. Replace with your streamer.",
        }])
        state.save_streamers()
    monitor.start()
    state.add_event("info", "Dashboard ready")


if __name__ == "__main__":
    _startup()
    host = settings.host
    port = settings.port
    print(f"\n  Clipping Studio -> http://{host}:{port}\n")
    app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)
