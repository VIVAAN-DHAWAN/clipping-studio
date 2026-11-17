"""Clip factory: retro cut -> 9:16 vertical -> word captions -> SRT + clean."""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from captioner import (
    build_ass, burn_captions, hook_from_chat, words_from_segments, words_from_text,
    write_ass, write_srt,
)
from config import DIRS, settings
from state import get_state
from utils import (
    LOG, ffprobe_duration, ffprobe_info, run_cmd, safe_name, slugify,
)
import ai
from recorder import recorder_manager

Progress = Callable[[str, float, str], None]


def _font_dir() -> str | None:
    candidates = [
        "/System/Library/Fonts/Supplemental",
        "/Library/Fonts",
        "/usr/share/fonts/truetype/dejavu",
        "/usr/share/fonts/truetype/liberation",
        "C:/Windows/Fonts",
    ]
    for path in candidates:
        if Path(path).is_dir():
            return path
    return None


def _has_video(path: Path) -> bool:
    info = ffprobe_info(path)
    return any(s.get("codec_type") == "video" for s in info.get("streams", []))


def verticalize(source: str | Path, out: str | Path, *, start: float = 0.0,
                duration: float | None = None) -> bool:
    filt = (
        "[0:v]split=2[bg][fg];"
        "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,boxblur=22:2,eq=brightness=-0.13:saturation=1.08[bgv];"
        "[fg]scale=1080:1920:force_original_aspect_ratio=decrease[fgv];"
        "[bgv][fgv]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=30,format=yuv420p[v]"
    )
    cmd = [settings.ffmpeg, "-v", "error", "-y", "-ss", f"{max(0, start):.3f}",
           "-i", str(source)]
    if duration:
        cmd += ["-t", f"{max(0.5, duration):.3f}"]
    cmd += [
        "-filter_complex", filt, "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", str(out),
    ]
    try:
        res = run_cmd(cmd, timeout=1200)
        if res.returncode != 0:
            LOG.warning("verticalize failed: %s", res.stderr[-700:])
        return res.returncode == 0 and Path(out).is_file() and Path(out).stat().st_size > 5000
    except Exception as exc:
        LOG.warning("verticalize error: %s", exc)
        return False


def make_thumbnail(video: Path, out: Path, at: float = 1.0) -> bool:
    cmd = [
        settings.ffmpeg, "-v", "error", "-y", "-ss", f"{max(0, at):.2f}",
        "-i", str(video), "-frames:v", "1", "-vf", "scale=540:-2",
        "-q:v", "3", str(out),
    ]
    try:
        res = run_cmd(cmd, timeout=60)
        return res.returncode == 0 and out.is_file() and out.stat().st_size > 1000
    except Exception:
        return False


def slice_words(words: list[dict[str, Any]], start: float, duration: float) -> list[dict[str, Any]]:
    out = []
    end = start + duration
    for w in words or []:
        try:
            ws, we = float(w["start"]), float(w["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if we <= start or ws >= end:
            continue
        out.append({
            "word": w.get("word", ""),
            "start": max(0.0, ws - start),
            "end": max(0.0, min(end, we) - start),
        })
    return out


def _transcribe_source(source: Path, start: float, duration: float) -> dict[str, Any] | None:
    if not ai.enabled():
        return None
    wav = DIRS["cache"] / f"clip_audio_{int(time.time() * 1000)}.wav"
    from utils import extract_audio

    if not extract_audio(source, wav, start=start, duration=duration):
        return None
    try:
        return ai.transcribe(wav, prompt="GTA 6 livestream clip, gaming slang, verbatim.",
                             word_timestamps=True)
    finally:
        try:
            wav.unlink()
        except OSError:
            pass


def _pick_title(*, spike: dict[str, Any] | None, transcript: str, streamer: str,
                duration: float) -> tuple[str, str, list[str]]:
    if spike and spike.get("title"):
        return str(spike["title"])[:70], str(spike.get("hook") or "")[:80], []
    if ai.enabled() and transcript.strip():
        try:
            titles = ai.generate_titles(streamer=streamer, transcript=transcript,
                                        context="manual clip")
            if titles:
                return titles[0]["text"], "", []
        except ai.AIError as exc:
            LOG.info("title generation: %s", str(exc)[:150])
    snippet = " ".join((transcript or "").split())[:48]
    title = snippet.title() if snippet else f"{streamer} clip {int(duration)}s"
    return title[:70], "", []


def _hook(*, spike: dict[str, Any] | None, streamer_id: str, transcript: str,
          window_start: float, window_end: float) -> str:
    if spike and spike.get("hook"):
        return str(spike["hook"])[:90]
    state = get_state()
    chats = state.recent_chat(streamer_id, seconds=30, limit=120)
    hook = hook_from_chat(chats)
    if hook:
        return hook
    if transcript:
        return " ".join(transcript.split()[:5]).upper()
    return ""


def create_clip_from_source(
    source: str | Path,
    *,
    start: float,
    duration: float,
    streamer: str,
    transcript: str = "",
    words: list[dict[str, Any]] | None = None,
    spike: dict[str, Any] | None = None,
    title: str | None = None,
    hook: str | None = None,
    score: int | None = None,
    verdict: str = "",
    reason: str = "",
    auto: bool = False,
    source_url: str = "",
    platform: str = "",
    progress: Progress | None = None,
) -> dict[str, Any]:
    state = get_state()
    source = Path(source)
    duration = max(1.0, float(duration))
    if progress:
        progress("preparing", 5, "Reading source")
    if not source.is_file():
        raise FileNotFoundError(source)
    if not _has_video(source):
        raise ValueError("Source has no video stream")

    streamer_slug = safe_name(streamer, 40)
    out_dir = DIRS["clips"] / streamer_slug
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    clip_id = f"{int(time.time() * 1000)}-{os.getpid() % 1000:03d}"

    transcript_text = (transcript or "").strip()
    clip_words = words or []
    if not clip_words:
        result = None
        if progress:
            progress("transcribing", 15, "Transcribing clip for word-level captions")
        try:
            result = _transcribe_source(source, start, duration)
        except ai.AIError as exc:
            LOG.info("clip transcription skipped: %s", str(exc)[:180])
        except Exception as exc:
            LOG.warning("clip transcription failed: %s", exc)
        if result:
            clip_words = slice_words(result.get("words") or [], start, duration)
            transcript_text = transcript_text or result.get("text", "")
            segments = result.get("segments") or []
            if clip_words:
                pass
            elif segments:
                clip_words = slice_words(words_from_segments(segments), start, duration)
            state.count_ai_check(True)
    if not transcript_text:
        transcript_text = " ".join(
            str(r.get("text", "")) for r in state.recent_transcript(streamer, 12)
        )
    if not clip_words and transcript_text:
        clip_words = words_from_text(transcript_text, duration)

    final_title = (title or "").strip().strip('"')[:70]
    final_hook = (hook or "").strip()[:90]
    if not final_title:
        final_title, ai_hook, _ = _pick_title(
            spike=spike, transcript=transcript_text, streamer=streamer, duration=duration
        )
        final_hook = final_hook or ai_hook
    if not final_hook:
        final_hook = _hook(spike=spike, streamer_id="", transcript=transcript_text,
                           window_start=0, window_end=duration)
    safe_title = slugify(final_title, 48, "clip")

    base = f"{stamp}_{safe_title}"
    final_path = out_dir / f"{base}.mp4"
    clean_path = out_dir / f"{base}_clean.mp4"
    ass_path = out_dir / f"{base}.ass"
    srt_path = out_dir / f"{base}.srt"
    thumb_path = out_dir / f"{base}.jpg"
    tmp_vertical = DIRS["cache"] / f"{clip_id}_vertical.mp4"

    if progress:
        progress("rendering", 25, "Cutting 9:16 vertical with blurred background")
    if not verticalize(source, tmp_vertical, start=start, duration=duration):
        raise RuntimeError("ffmpeg vertical render failed")
    try:
        shutil.copyfile(tmp_vertical, clean_path)
    except OSError:
        clean_path = tmp_vertical

    burned = False
    if clip_words:
        if progress:
            progress("captioning", 62, f"Burning {len(clip_words)} words")
        write_ass(ass_path, clip_words, duration=duration, hook=final_hook,
                  hook_seconds=min(5.0, max(2.5, duration * 0.2)))
        write_srt(srt_path, clip_words, duration=duration)
        burned = burn_captions(tmp_vertical, ass_path, final_path, fonts_dir=_font_dir())
    if burned:
        try:
            tmp_vertical.unlink()
        except OSError:
            pass
    else:
        try:
            shutil.copyfile(tmp_vertical, final_path)
            tmp_vertical.unlink()
        except OSError:
            final_path = clean_path

    if progress:
        progress("finalizing", 88, "Thumbnail + metadata")
    make_thumbnail(Path(final_path), thumb_path, at=min(2.0, duration / 2))

    final_duration = ffprobe_duration(final_path) or duration
    score_value = int(score if score is not None else (
        spike.get("score") if spike and spike.get("score") is not None else 0
    ) or 0)
    clip = {
        "id": clip_id,
        "title": final_title or "Untitled clip",
        "streamer": streamer,
        "platform": platform,
        "file": f"{streamer_slug}/{final_path.name}",
        "clean_file": f"{streamer_slug}/{clean_path.name}",
        "srt_file": f"{streamer_slug}/{srt_path.name}" if srt_path.is_file() else "",
        "ass_file": f"{streamer_slug}/{ass_path.name}" if ass_path.is_file() else "",
        "thumb": f"{streamer_slug}/{thumb_path.name}" if thumb_path.is_file() else "",
        "date": datetime.now().strftime("%Y-%m-%d"),
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "created_ts": time.time(),
        "duration": round(final_duration, 2),
        "viral_score": score_value,
        "verdict": verdict or (spike or {}).get("verdict", ""),
        "reason": reason or (spike or {}).get("reason", ""),
        "transcript": transcript_text[:700],
        "hook": final_hook,
        "words": len(clip_words),
        "captions": burned,
        "auto": bool(auto),
        "spike_id": (spike or {}).get("id", ""),
        "source_url": source_url,
        "posted": {},
        "earnings_id": "",
    }
    state.add_clip(clip)
    if progress:
        progress("done", 100, "Clip ready")
    return clip


def create_live_clip(seconds: int, streamer_id: str, *, spike: dict[str, Any] | None = None,
                     end_offset: float = 0.0, auto: bool = False,
                     progress: Progress | None = None) -> dict[str, Any]:
    state = get_state()
    streamer = state.find_streamer(streamer_id)
    rec = recorder_manager.get(streamer_id)
    if not rec or rec.status not in {"recording", "starting", "reconnecting"}:
        raise RuntimeError("No active recording for that streamer")
    if progress:
        progress("cutting", 8, f"Cutting last {seconds}s from rolling buffer")
    window = rec.clip_window(seconds, end_offset=end_offset)
    if not window:
        raise RuntimeError("Rolling buffer is empty — wait a few seconds")
    transcript = state.transcript_text(streamer_id, since_ts=window["window_start"] - 5)
    words = []
    if ai.enabled() and transcript:
        words = words_from_text(transcript, window["duration"])
    title = None
    hook = None
    score = None
    verdict = ""
    reason = ""
    if spike:
        title = spike.get("title")
        hook = spike.get("hook")
        score = spike.get("score")
        verdict = spike.get("verdict", "")
        reason = spike.get("reason", "")
    try:
        clip = create_clip_from_source(
            window["path"],
            start=0.0,
            duration=window["duration"],
            streamer=(streamer or {}).get("name", streamer_id),
            transcript=transcript,
            words=words if not ai.enabled() else None,
            spike=spike,
            title=title,
            hook=hook or _hook(spike=spike, streamer_id=streamer_id,
                               transcript=transcript, window_start=0, window_end=window["duration"]),
            score=score,
            verdict=verdict,
            reason=reason,
            auto=auto,
            source_url=(streamer or {}).get("url", ""),
            platform=(streamer or {}).get("platform", ""),
            progress=progress,
        )
    finally:
        for path in window.get("cleanup", []):
            try:
                Path(path).unlink()
            except OSError:
                pass
    return clip


def create_vod_clip(source: str | Path, *, start: float, duration: float, streamer: str,
                    transcript: str, words: list[dict[str, Any]] | None = None,
                    spike: dict[str, Any] | None = None, auto: bool = True,
                    source_url: str = "", platform: str = "",
                    progress: Progress | None = None) -> dict[str, Any]:
    rel_words = slice_words(words or [], start, duration)
    return create_clip_from_source(
        source, start=start, duration=duration, streamer=streamer,
        transcript=transcript, words=rel_words, spike=spike, auto=auto,
        source_url=source_url, platform=platform, progress=progress,
    )


DEMO_SCRIPT = (
    "BRO WAIT WAIT NO WAY HE ACTUALLY HIT THAT "
    "INSANE CLIP THAT RIGHT NOW W W W "
    "I CANNOT BELIEVE THIS I'M NOT EVEN JOKING "
    "THAT WAS THE CRAZIEST THING I'VE EVER SEEN"
)


def demo_clip(*, duration: int = 45, title: str = "no way he actually hit that",
              score: int = 92, streamer: str = "mooda") -> dict[str, Any]:
    stamp = int(time.time() * 1000)
    src = DIRS["cache"] / f"demo_src_{stamp}.mp4"
    font = "/System/Library/Fonts/Supplemental/Arial Black.ttf"
    vf = f"testsrc2=size=1280x720:rate=30:duration={duration}"
    if Path(font).is_file():
        vf += (
            f",drawtext=fontfile='{font}':text='GTA 6 LIVE':fontcolor=white:fontsize=54:"
            "x=(w-text_w)/2:y=60:box=1:boxcolor=black@0.45:boxborderw=18"
        )
    cmd = [
        settings.ffmpeg, "-v", "error", "-y",
        "-f", "lavfi", "-i", vf,
        "-f", "lavfi", "-i", f"sine=frequency=220:duration={duration}",
        "-shortest", "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", str(src),
    ]
    res = run_cmd(cmd, timeout=180)
    if res.returncode != 0:
        raise RuntimeError(f"Demo source failed: {res.stderr[-300:]}")
    words = words_from_text(DEMO_SCRIPT, duration)
    try:
        clip = create_clip_from_source(
            src, start=0, duration=duration, streamer=streamer,
            transcript=DEMO_SCRIPT, words=words,
            spike={
                "id": f"demo-{stamp}",
                "title": title,
                "hook": "CHAT WENT CRAZY",
                "score": score,
                "verdict": "LIKELY VIRAL",
                "reason": "High energy + chat explosion + insane reaction (demo data)",
            },
            title=title, hook="CHAT WENT CRAZY", score=score,
            verdict="LIKELY VIRAL",
            reason="High energy + chat explosion + insane reaction (demo data)",
            auto=True, platform="twitch",
        )
        clip["demo"] = True
        return clip
    finally:
        try:
            src.unlink()
        except OSError:
            pass
