"""VOD mode: paste a link (or pick a file), get ranked vertical clips.

Pipeline: download -> subtitle/transcript -> dual-judge detection -> render.
Runs on a background thread and reports through state.set_vod().
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import ai
from chat import download_chat_replay
from clipper import create_vod_clip
from config import DIRS, settings
from detector import detect_vod
from state import get_state
from transcriber import find_subtitle_file, parse_subtitles, save_transcript, transcribe_media
from utils import (
    LOG, ffprobe_duration, now_iso, read_jsonl, run_cmd, safe_name, which,
)

Progress = Callable[[str, float, str], None]

_vod_lock = threading.Lock()
_vod_thread: threading.Thread | None = None


# ------------------------------------------------------------------ helpers
def platform_from_url(url: str) -> str:
    low = (url or "").lower()
    if "twitch.tv" in low:
        return "twitch"
    if "youtube.com" in low or "youtu.be" in low:
        return "youtube"
    if "kick.com" in low:
        return "kick"
    return ""


def _progress(status: str, pct: float, message: str) -> None:
    get_state().set_vod({
        **get_state().vod_job,
        "status": status,
        "progress": round(max(0.0, min(100.0, pct)), 1),
        "message": message,
        "updated_at": now_iso(),
    })


def _download(url: str, out_dir: Path, *, max_minutes: int = 0,
              progress: Progress | None = None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out_tpl = str(out_dir / "source.%(ext)s")
    cmd = [
        settings.ytdlp, "--no-warnings", "--newline", "--no-playlist",
        "--merge-output-format", "mp4",
        "-f", "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]/b",
        "--write-subs", "--write-auto-subs", "--sub-langs", "en.*,en,hi.*",
        "--convert-subs", "srt", "-o", out_tpl,
    ]
    if max_minutes > 0:
        cmd += ["--match-filter", f"duration <= {max_minutes * 60}"]
    cmd.append(url)
    if not which(settings.ytdlp):
        raise RuntimeError("yt-dlp is not installed. Install it with: pip install yt-dlp")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    last_pct = 0.0
    assert proc.stdout is not None
    for line in proc.stdout:
        match = re.search(r"\[download\]\s+(\d+(?:\.\d+)?)%", line)
        if match:
            pct = float(match.group(1))
            if pct - last_pct >= 2 or pct >= 99.5:
                last_pct = pct
                if progress:
                    progress("downloading", 2 + pct * 0.28, f"Downloading video {pct:.0f}%")
        elif "[info] Downloading" in line or "Destination:" in line:
            if progress:
                progress("downloading", 4, "Starting download")
    code = proc.wait()
    if code != 0:
        raise RuntimeError("yt-dlp could not download that link")
    candidates = sorted(out_dir.glob("source.*"), key=lambda p: p.stat().st_size, reverse=True)
    video = next((p for p in candidates if p.suffix.lower() in {".mp4", ".mkv", ".webm"}),
                 None)
    if not video or video.stat().st_size < 100_000:
        raise RuntimeError("Download finished but no video file was produced")
    return video


def _audio_points(source: Path, progress: Progress | None = None) -> list[tuple[float, float]]:
    """One pass of ffmpeg astats -> (second, dBFS) for reaction signals."""
    from recorder import analyze_audio_file

    if progress:
        progress("analysing", 34, "Measuring loudness peaks")
    try:
        return analyze_audio_file(source, window_seconds=1.0)
    except Exception as exc:  # noqa: BLE001 - signals are optional, never fatal
        LOG.warning("VOD loudness analysis failed: %s", exc)
        return []


def _chat_rows(url: str, progress: Progress | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def on_message(offset: float, user: str, message: str) -> None:
        rows.append({"ts": float(offset), "user": user, "message": message})

    if progress:
        progress("chat", 30, "Pulling chat replay")
    try:
        download_chat_replay(url, on_message=on_message)
    except Exception as exc:  # noqa: BLE001 - chat replay is optional
        LOG.info("VOD chat replay unavailable: %s", str(exc)[:200])
    return rows


# --------------------------------------------------------------------- jobs
def vod_running() -> bool:
    return bool(_vod_thread and _vod_thread.is_alive())


def start_vod(url: str, *, streamer: str = "", platform: str = "",
              max_clips: int = 6, min_len: int = 15, max_len: int = 60,
              max_minutes: int | None = None, with_chat: bool = True) -> dict[str, Any]:
    global _vod_thread
    state = get_state()
    url = (url or "").strip()
    if not url:
        raise ValueError("Paste a video link first")
    if vod_running():
        raise RuntimeError("A VOD job is already running")
    job_id = uuid.uuid4().hex[:10]
    max_minutes = settings.max_vod_minutes if max_minutes is None else max_minutes
    state.set_vod({
        "id": job_id,
        "status": "queued",
        "progress": 0,
        "message": "Queued",
        "url": url,
        "streamer": streamer or platform_from_url(url) or "vod",
        "platform": platform or platform_from_url(url),
        "started_at": now_iso(),
        "clips": [],
        "error": "",
        "options": {"max_clips": max_clips, "min_len": min_len, "max_len": max_len},
    })
    _vod_thread = threading.Thread(
        target=_run_job, args=(job_id, url, streamer, platform, max_clips,
                               min_len, max_len, max_minutes, with_chat),
        name="vod-job", daemon=True,
    )
    _vod_thread.start()
    return dict(state.vod_job)


def start_vod_file(path: str, *, streamer: str = "", platform: str = "",
                   max_clips: int = 6, min_len: int = 15, max_len: int = 60) -> dict[str, Any]:
    global _vod_thread
    state = get_state()
    src = Path(path).expanduser()
    if not src.is_file():
        raise FileNotFoundError(str(src))
    if vod_running():
        raise RuntimeError("A VOD job is already running")
    job_id = uuid.uuid4().hex[:10]
    state.set_vod({
        "id": job_id, "status": "queued", "progress": 0, "message": "Queued",
        "url": str(src), "streamer": streamer or src.stem, "platform": platform,
        "started_at": now_iso(), "clips": [], "error": "",
        "options": {"max_clips": max_clips, "min_len": min_len, "max_len": max_len},
    })
    _vod_thread = threading.Thread(
        target=_run_local_job, args=(job_id, src, streamer, max_clips, min_len, max_len),
        name="vod-job", daemon=True,
    )
    _vod_thread.start()
    return dict(state.vod_job)


def cancel_vod() -> dict[str, Any]:
    state = get_state()
    job = dict(state.vod_job)
    if job.get("status") in {"queued", "downloading", "transcribing", "detecting", "rendering"}:
        job.update({"status": "cancelled", "message": "Cancelled by user", "updated_at": now_iso()})
        state.set_vod(job)
    return job


def _run_job(job_id: str, url: str, streamer: str, platform: str,
             max_clips: int, min_len: int, max_len: int,
             max_minutes: int, with_chat: bool) -> None:
    if not _vod_lock.acquire(blocking=False):
        _progress("error", 0, "Another VOD job is running")
        return
    state = get_state()
    try:
        platform = platform or platform_from_url(url)
        streamer = streamer or f"{platform or 'vod'} clip"
        work = DIRS["recordings"] / "vod" / job_id
        _progress("downloading", 2, "Downloading video")
        source = _download(url, work, max_minutes=max_minutes,
                           progress=lambda s, p, m: _progress(s, min(p * 0.9, 30), m))
        _transcribe_and_clip(job_id=job_id, source=source, url=url, platform=platform,
                             streamer=streamer, max_clips=max_clips, min_len=min_len,
                             max_len=max_len, with_chat=with_chat, work=work)
    except Exception as exc:  # noqa: BLE001 - report, never crash the server
        LOG.exception("VOD job failed")
        state.add_event("error", f"VOD job failed: {str(exc)[:180]}")
        job = dict(state.vod_job)
        job.update({"status": "error", "error": str(exc)[:400],
                    "message": str(exc)[:200], "updated_at": now_iso()})
        state.set_vod(job)
    finally:
        _vod_lock.release()


def _run_local_job(job_id: str, source: Path, streamer: str,
                   max_clips: int, min_len: int, max_len: int) -> None:
    if not _vod_lock.acquire(blocking=False):
        _progress("error", 0, "Another VOD job is running")
        return
    state = get_state()
    try:
        _transcribe_and_clip(job_id=job_id, source=source, url="", platform="",
                             streamer=streamer or source.stem, max_clips=max_clips,
                             min_len=min_len, max_len=max_len, with_chat=False,
                             work=source.parent)
    except Exception as exc:  # noqa: BLE001
        LOG.exception("VOD file job failed")
        state.add_event("error", f"VOD job failed: {str(exc)[:180]}")
        job = dict(state.vod_job)
        job.update({"status": "error", "error": str(exc)[:400],
                    "message": str(exc)[:200], "updated_at": now_iso()})
        state.set_vod(job)
    finally:
        _vod_lock.release()


def _transcribe_and_clip(*, job_id: str, source: Path, url: str, platform: str,
                         streamer: str, max_clips: int, min_len: int,
                         max_len: int, with_chat: bool, work: Path) -> None:
    state = get_state()
    duration = ffprobe_duration(source) or 0.0
    if duration <= 1:
        raise RuntimeError("Could not read the video duration")

    words: list[dict[str, Any]] = []
    transcript_text = ""
    language = ""

    sub_file = find_subtitle_file(work)
    if sub_file:
        _progress("transcribing", 30, f"Reading subtitles ({sub_file.name})")
        try:
            sub = parse_subtitles(sub_file)
            words = sub.get("words") or []
            transcript_text = sub.get("text") or ""
            language = sub.get("language") or ""
        except Exception as exc:  # noqa: BLE001
            LOG.info("Subtitle parse failed: %s", exc)

    if not words and ai.enabled():
        def on_progress(status: str, pct: float, message: str) -> None:
            _progress("transcribing", 30 + pct * 0.35, message)

        try:
            result = transcribe_media(source, duration, progress=on_progress)
            words = result.get("words") or []
            transcript_text = result.get("text") or ""
            language = result.get("language") or ""
            save_transcript(job_id, result)
        except ai.AIError as exc:
            LOG.info("VOD transcription unavailable: %s", str(exc)[:200])
            state.add_event("warn", "Transcribing skipped: AI endpoint not configured")

    if not words and not transcript_text:
        state.add_event("warn", "No transcript: clips will be chosen by loudness/chat only")

    chat_rows: list[dict[str, Any]] = []
    if with_chat and url:
        chat_rows = _chat_rows(url, progress=lambda s, p, m: _progress(s, p, m))
        if chat_rows:
            try:
                (work / "chat.jsonl").write_text(
                    "\n".join(json.dumps(r, ensure_ascii=False) for r in chat_rows),
                    encoding="utf-8",
                )
            except OSError:
                pass

    _progress("detecting", 66, "Finding clips (dual judge)")
    raw_audio = _audio_points(source, progress=lambda s, p, m: _progress(s, p, m))
    try:
        audio_points = [(float(t), float(db)) for t, db in raw_audio]
    except (TypeError, ValueError):
        audio_points = [(float(i), float(db)) for i, db in enumerate(raw_audio)]
    clips = detect_vod(
        words=words, duration=duration, streamer=streamer, platform=platform,
        audio_points=audio_points, chat_events=chat_rows or None,
        max_clips=max_clips, min_len=min_len, max_len=max_len,
        progress=lambda s, p, m: _progress("detecting", p, m),
    )
    if not clips:
        raise RuntimeError("No clip-worthy moments found in this video")

    rendered: list[str] = []
    for idx, clip in enumerate(clips):
        base = 70 + (idx / max(1, len(clips))) * 28
        _progress("rendering", base, f"Rendering clip {idx + 1}/{len(clips)}")
        try:
            result = create_vod_clip(
                source, start=clip["start"],
                duration=max(5.0, clip["end"] - clip["start"]),
                streamer=streamer, transcript=clip.get("transcript", ""),
                words=words or None,
                spike={**clip, "title": clip.get("title", ""), "hook": clip.get("hook", "")},
                auto=True, source_url=url, platform=platform,
                progress=lambda s, p, m, _b=base: _progress("rendering", _b + min(4, p / 25), m),
            )
            extra = {"vod_job": job_id}
            if clip.get("source"):
                extra["detection"] = clip["source"]
            if clip.get("verdict"):
                extra["verdict"] = clip["verdict"]
            state.update_clip(result["id"], extra)
            rendered.append(result["id"])
        except Exception as exc:  # noqa: BLE001 - one bad clip must not kill the batch
            LOG.warning("Clip render failed (%.0f-%.0f): %s", clip["start"], clip["end"], exc)
    if not rendered:
        raise RuntimeError("Every clip render failed; check the server log")
    job = dict(state.vod_job)
    job.update({
        "status": "done", "progress": 100,
        "message": f"{len(rendered)} clip(s) ready", "clips": rendered,
        "finished_at": now_iso(), "duration": round(duration, 1),
        "language": language, "chat_messages": len(chat_rows),
    })
    state.set_vod(job)
    state.add_event("success", f"VOD job finished: {len(rendered)} clips")


def vod_recent() -> list[dict[str, Any]]:
    return read_jsonl(DIRS["data"] / "vod_jobs.jsonl", limit=20)
