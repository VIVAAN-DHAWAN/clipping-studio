"""Live + VOD transcription via the configured Whisper-compatible endpoint."""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from config import DIRS, settings
from state import get_state
from utils import (
    LOG, append_jsonl, extract_audio, ffprobe_duration, now_iso, read_jsonl, run_cmd,
)
import ai

ProgressFn = Callable[[str, float, str], None]

LIVE_PROMPT = (
    "GTA 6 livestream commentary. Gaming slang, streamer names, hype moments, "
    "laughs, rage, chat reactions. Transcribe verbatim."
)


class LiveTranscriber:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.threads: dict[str, threading.Thread] = {}
        self.stops: dict[str, threading.Event] = {}
        self.notified_disabled: bool = False

    def start(self, sid: str) -> None:
        with self.lock:
            if sid in self.threads and self.threads[sid].is_alive():
                return
            stop = threading.Event()
            self.stops[sid] = stop
            thread = threading.Thread(target=self._loop, args=(sid, stop),
                                      name=f"tr-{sid}", daemon=True)
            self.threads[sid] = thread
            thread.start()

    def stop(self, sid: str) -> None:
        with self.lock:
            stop = self.stops.pop(sid, None)
            thread = self.threads.pop(sid, None)
        if stop:
            stop.set()
        if thread and thread.is_alive():
            thread.join(timeout=3)

    def stop_all(self) -> None:
        with self.lock:
            sids = list(self.threads)
        for sid in sids:
            self.stop(sid)

    def _loop(self, sid: str, stop: threading.Event) -> None:
        from recorder import recorder_manager

        state = get_state()
        if not ai.enabled():
            if not self.notified_disabled:
                state.add_event("warn", "Live transcription is idle: add AI keys to enable Whisper")
                self.notified_disabled = True
            return
        while not stop.is_set():
            rec = recorder_manager.get(sid)
            if not rec:
                break
            if rec.status not in {"recording", "starting", "reconnecting"}:
                if rec.status in {"stopped", "error"}:
                    break
            window = settings.live_transcribe_window
            audio = rec.extract_recent_audio(window)
            if audio:
                try:
                    result = ai.transcribe(audio, prompt=LIVE_PROMPT, word_timestamps=False)
                    text = result.get("text", "").strip()
                    if text:
                        state.add_transcript(sid, text, source="whisper")
                        row = {
                            "time": now_iso(),
                            "text": text,
                            "language": result.get("language", ""),
                        }
                        try:
                            append_jsonl(rec.session_dir / "transcript.jsonl", row)
                        except OSError:
                            pass
                    state.count_ai_check(True)
                except ai.AIError as exc:
                    state.count_ai_check(False, str(exc))
                    LOG.info("Live transcription: %s", str(exc)[:200])
                finally:
                    try:
                        Path(audio).unlink()
                    except OSError:
                        pass
            wait = max(8, settings.live_transcribe_interval)
            for _ in range(wait * 2):
                if stop.is_set():
                    break
                time.sleep(0.5)
        state.add_event("info", "Live transcription stopped")


def transcribe_media(media_path: str | Path, duration: float | None = None, *,
                     progress: ProgressFn | None = None,
                     stop: threading.Event | None = None,
                     language: str | None = None,
                     chunk_seconds: int = 540) -> dict[str, Any]:
    """Transcribe a long media file in <24MB audio chunks. Returns merged result."""
    path = Path(media_path)
    if not path.is_file():
        raise FileNotFoundError(path)
    if not ai.enabled():
        raise ai.AIError("Transcription requires AI configuration")
    duration = duration or ffprobe_duration(path) or 0
    if duration <= 0:
        raise ValueError("Could not read media duration")

    chunks = max(1, int((duration + chunk_seconds - 1) // chunk_seconds))
    all_words: list[dict[str, Any]] = []
    all_segments: list[dict[str, Any]] = []
    texts: list[str] = []
    language_detected = language or ""
    work = DIRS["cache"] / "vod_audio"
    work.mkdir(parents=True, exist_ok=True)

    for idx in range(chunks):
        if stop and stop.is_set():
            break
        start = idx * chunk_seconds
        length = min(chunk_seconds, max(1.0, duration - start))
        wav = work / f"{path.stem}_{idx:03d}.wav"
        if progress:
            pct = 5 + (idx / chunks) * 58
            progress("transcribing", pct, f"Transcribing chunk {idx + 1}/{chunks}")
        if not extract_audio(path, wav, start=start, duration=length):
            LOG.warning("Could not extract audio chunk %s", wav)
            continue
        try:
            result = ai.transcribe(wav, language=language, prompt=LIVE_PROMPT,
                                   word_timestamps=True)
        except ai.AIError as exc:
            LOG.warning("Whisper chunk failed: %s", str(exc)[:250])
            if idx == 0:
                raise
            continue
        finally:
            pass
        text = (result.get("text") or "").strip()
        if text:
            texts.append(text)
        language_detected = language_detected or result.get("language", "")
        for w in result.get("words") or []:
            all_words.append({
                "word": w["word"], "start": float(w["start"]) + start,
                "end": float(w["end"]) + start,
            })
        for seg in result.get("segments") or []:
            all_segments.append({
                "start": float(seg.get("start", 0)) + start,
                "end": float(seg.get("end", 0)) + start,
                "text": seg.get("text", ""),
            })
        try:
            wav.unlink()
        except OSError:
            pass

    if progress:
        progress("transcribing", 65, "Transcription complete")
    full_text = " ".join(texts).strip()
    return {
        "text": full_text,
        "words": sorted(all_words, key=lambda w: w["start"]),
        "segments": sorted(all_segments, key=lambda s: s["start"]),
        "language": language_detected,
        "duration": duration,
        "source": "whisper",
    }


def transcribe_clip(audio_path: str | Path, duration: float) -> dict[str, Any]:
    result = ai.transcribe(audio_path, prompt=LIVE_PROMPT, word_timestamps=True)
    result.setdefault("duration", duration)
    return result


def save_transcript(video_id: str, result: dict[str, Any]) -> Path:
    out = DIRS["transcripts"] / f"{video_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    txt = DIRS["transcripts"] / f"{video_id}.txt"
    txt.write_text(result.get("text", ""), encoding="utf-8")
    return out


# ------------------------------------------------------------------ subtitles
_TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})|(\d{1,2}):(\d{2})[.,](\d{1,3})"
)


def _parse_time(token: str) -> float:
    token = token.strip().replace(",", ".")
    parts = token.split(":")
    try:
        parts = [float(p) for p in parts]
    except ValueError:
        return 0.0
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0] if parts else 0.0


def _clean_sub_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\{\\[^}]*\}", "", text)
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&#39;", "'", text)
    text = re.sub(r"&quot;", '"', text)
    return " ".join(text.split())


def parse_subtitles(path: str | Path) -> dict[str, Any]:
    from captioner import words_from_segments

    text = Path(path).read_text(encoding="utf-8", errors="replace")
    segments: list[dict[str, Any]] = []
    if "-->" in text:
        blocks = re.split(r"\n\s*\n", text)
        for block in blocks:
            lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
            timing = next((ln for ln in lines if "-->" in ln), None)
            if not timing:
                continue
            try:
                left, right = timing.split("-->", 1)
                start = _parse_time(left.strip().split()[0])
                end = _parse_time(right.strip().split()[0])
            except (ValueError, IndexError):
                continue
            body = " ".join(_clean_sub_text(ln) for ln in lines if "-->" not in ln)
            if body:
                segments.append({"start": start, "end": max(end, start + 0.2), "text": body})
    words = words_from_segments(segments)
    full_text = " ".join(s["text"] for s in segments)
    return {
        "text": full_text,
        "words": words,
        "segments": segments,
        "source": "subtitles",
        "language": "en",
        "duration": segments[-1]["end"] if segments else 0,
    }


def find_subtitle_file(folder: str | Path) -> Path | None:
    folder = Path(folder)
    for pattern in ("*.en.vtt", "*.en-US.vtt", "*.vtt", "*.srt"):
        matches = sorted(folder.rglob(pattern))
        if matches:
            return matches[0]
    return None


def load_live_transcript_log(rec_dir: Path) -> list[dict[str, Any]]:
    return read_jsonl(rec_dir / "transcript.jsonl")


live_transcriber = LiveTranscriber()
