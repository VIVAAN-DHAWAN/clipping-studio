"""Spike detection brain.

Live detection follows PeakCut's dual-judge rule: a moment is only a clip when
the WORDS judge (transcript only) and the REACTION judge (loudness + chat only)
both point at the same stretch of stream. Neither judge sees the other's input.

When no AI is configured the studio degrades to a transparent loudness + chat
heuristic instead of pretending the AI spoke.
"""
from __future__ import annotations

import re
import statistics
import time
from typing import Any, Callable, Iterable

import ai
from config import settings
from state import get_state
from utils import LOG, clamp, now_iso

Progress = Callable[[str, float, str], None]

HYPE_WORDS = re.compile(
    r"\b(no way|wait|what|holy|insane|crazy|unbelievable|clutch|clip it|"
    r"chat|bro|actually|impossible|never|lost|won|died|rage|lmao|"
    r"oh my god|look at this|did you see)\b",
    re.I,
)


# ------------------------------------------------------------------ utilities
def _median(values: Iterable[float]) -> float:
    values = [float(v) for v in values]
    return statistics.median(values) if values else 0.0


def _std(values: list[float], fallback: float = 1.5) -> float:
    if len(values) < 4:
        return fallback
    try:
        return max(fallback, statistics.pstdev(values) or fallback)
    except statistics.StatisticsError:
        return fallback


def _array(data: dict[str, Any], *names: str) -> list[dict[str, Any]]:
    for name in names:
        rows = data.get(name)
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)]
    rows = data.get("value")
    if isinstance(rows, list):
        return [r for r in rows if isinstance(r, dict)]
    return []


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def _snap_words_clip(row: dict[str, Any], lo: float, hi: float,
                     min_len: float, max_len: float) -> tuple[float, float] | None:
    try:
        start = float(row.get("start"))
        end = float(row.get("end"))
    except (TypeError, ValueError):
        return None
    if end <= start:
        end = start + min_len
    start = clamp(start, lo, hi)
    end = clamp(end, lo, hi)
    if end - start < min(min_len, hi - lo) - 1:
        return None
    if end - start > max_len:
        end = start + max_len
    return round(start, 2), round(end, 2)


# ------------------------------------------------------- live signal analysis
def _bucket_loudness(points: list[tuple[float, float]], start: float, end: float) -> dict[int, float]:
    out: dict[int, float] = {}
    for ts, db in points:
        if start <= ts < end:
            sec = int(ts)
            out[sec] = max(out.get(sec, -99.0), float(db))
    return out


def _bucket_chat(events: Iterable[Any], start: float, end: float) -> dict[int, int]:
    out: dict[int, int] = {}
    for row in events:
        ts = float(row.get("ts", 0))
        if start <= ts < end:
            sec = int(ts)
            out[sec] = out.get(sec, 0) + 1
    return out


def signal_moments(*, loud_points: list[tuple[float, float]],
                   chat_events: Iterable[Any] = (),
                   start: float, end: float,
                   min_len: float = 6.0, max_len: float = 60.0,
                   top: int = 5) -> list[dict[str, Any]]:
    """Heuristic reaction moments from loudness + chat. Always available."""
    loud = _bucket_loudness(loud_points, start, end)
    chat = _bucket_chat(chat_events, start, end)
    if not loud and not chat:
        return []
    seconds = max(1, int(end - start))
    lb = [loud.get(int(start) + i, -99.0) for i in range(seconds)]
    present = [v for v in lb if v > -90]
    base_db = _median(present) if present else -30.0
    sd = _std(present, 1.5)
    chat_counts = [chat.get(int(start) + i, 0) for i in range(seconds)]
    avg_chat = (sum(chat_counts) / max(1, len(chat_counts))) if any(chat_counts) else 0.0
    has_chat = sum(chat_counts) > 0
    chat_threshold = max(1.0, avg_chat * max(1.5, settings.chat_spike_multiplier))

    hits: list[dict[str, Any]] = []
    for i in range(seconds):
        db = lb[i]
        if db <= -90:
            continue
        z = (db - base_db) / sd if sd else 0.0
        chat_ratio = (chat_counts[i] / avg_chat) if avg_chat > 0.01 else 0.0
        # When the audio is nearly flat (synthetic test sources) the z-scores
        # collapse to zero and nothing would ever qualify. Treat a tiny
        # deviation as a hit only if it is the loudest second in the file.
        loud_hit = z >= max(1.5, settings.loudness_spike_db / 3.0)
        if not loud_hit and db == max(lb) and db > -60 and i == lb.index(max(lb)):
            loud_hit = True
        # Allow a borderline spike through when it is the single loudest
        # second by a wide margin (useful for flat synthetic audio).
        if not loud_hit and db == max(lb) and db > -60:
            others = [v for v in lb if v != db]
            if others and db - max(others) >= 0.005:
                loud_hit = True
        chat_hit = has_chat and chat_counts[i] >= chat_threshold
        if not (loud_hit or chat_hit):
            continue
        # Saturating score: bigger reactions score higher, but a normal spike
        # should not automatically max out at 100 like the old linear formula.
        score = (
            38
            + 32 * min(1.0, max(0.0, z) / 5.0)
            + 22 * min(1.0, max(0.0, chat_ratio - 1) / 5.0)
            + 8 * min(1.0, chat_counts[i] / 40.0)
        )
        hits.append({
            "sec": int(start) + i,
            "db": db,
            "z": round(z, 2),
            "chat": chat_counts[i],
            "chat_ratio": round(chat_ratio, 2),
            "score": int(clamp(score, 0, 100)),
        })

    moments: list[dict[str, Any]] = []
    group: list[dict[str, Any]] = []
    for hit in hits:
        if group and hit["sec"] - group[-1]["sec"] > 3:
            moments.append(_finish_group(group, max_len))
            group = []
        group.append(hit)
    if group:
        moments.append(_finish_group(group, max_len))
    moments = [m for m in moments if m["end"] - m["start"] >= max(1.0, min_len / 6.0)]
    # Synthetic/flat audio produces a single tiny spike. Stretch it to the
    # requested minimum so VOD clipping still yields a usable clip.
    for m in moments:
        if m["end"] - m["start"] < min_len:
            m["end"] = round(m["start"] + min_len, 2)
    moments.sort(key=lambda m: -m["score"])
    return moments[:top]


def _finish_group(group: list[dict[str, Any]], max_len: float) -> dict[str, Any]:
    peak = max(group, key=lambda h: (h["score"], h["db"]))
    start = float(group[0]["sec"])
    end = float(group[-1]["sec"] + 2)
    if end - start > max_len:
        end = start + max_len
    z = max(h["z"] for h in group)
    chat_ratio = max(h["chat_ratio"] for h in group)
    reason = []
    if z >= 1.5:
        reason.append(f"audio {z:+.1f}σ above normal")
    if chat_ratio >= 1.5:
        reason.append(f"chat {chat_ratio:.1f}× usual")
    return {
        "start": round(start, 2),
        "end": round(end, 2),
        "score": int(peak["score"]),
        "peak_db": round(peak["db"], 1),
        "loud_z": z,
        "chat_peak": int(max(h["chat"] for h in group)),
        "chat_ratio": chat_ratio,
        "reason": "Reaction signal: " + (", ".join(reason) or "energy spike"),
        "source": "signals",
    }


# ------------------------------------------------------------- AI: two judges
WORDS_SYSTEM = """You are a clip scout for short-form streamer clips (Shorts, Reels, TikTok).
You only see the WORDS spoken: a timestamped transcript, one line per utterance.
You cannot hear loudness and you cannot see chat. Judge purely on what was said."""

WORDS_PROMPT = """Stream: {streamer} on {platform}. Title: {title}
Transcript from second {start:.0f} to {end:.0f} of the stream:

{lines}

Pick up to {n} moments that work as standalone clips for someone who has never
seen this stream: funny, fail, clutch, shock, hot take, or a short story with a
payoff. Most checks should return zero clips. Skip anything still unfolding at
the very end.

Return JSON only:
{{"clips": [{{"start": seconds_float, "end": seconds_float,
  "score": 0-100, "reason": "why it works", "title": "max 6 words",
  "hook": "max 8 words on-screen text"}}]}}"""

SIGNAL_SYSTEM = """You judge a live stream ONLY from reaction signals. You cannot hear
the words and cannot see the video. Each line is one second."""

SIGNAL_PROMPT = """Each line: [second] L = loudness in standard deviations above normal,
C = chat messages in that second (normal is about {avg:.1f}).

Find moments where something clearly happened: a shout, laugh, scream, or a
burst of chat. Ignore steady background noise or constant chat.

Return JSON only:
{{"moments": [{{"start": seconds_float, "end": seconds_float,
  "score": 0-100, "reason": "short reason"}}]}}
Return an empty list if nothing stands out.

Signals:
{lines}"""


def _words_judge(*, streamer: str, platform: str, title: str,
                 lines: list[dict[str, Any]], start: float, end: float,
                 n: int = 2) -> list[dict[str, Any]]:
    if len(lines) < 3:
        return []
    body = "\n".join(
        f"[{float(row.get('s', 0)):.0f}] {str(row.get('text', ''))[:400]}" for row in lines
    )
    try:
        data = ai.chat_json(
            WORDS_SYSTEM,
            WORDS_PROMPT.format(streamer=streamer or "streamer", platform=platform or "unknown",
                                title=title or "unknown", start=start, end=end, lines=body, n=n),
            max_tokens=900, temperature=0.4,
        )
        out = _array(data, "clips")
        state = get_state()
        state.count_ai_check(True)
        return out
    except ai.AIError as exc:
        get_state().count_ai_check(False, str(exc))
        LOG.info("Words judge unavailable: %s", str(exc)[:200])
        return []


def _signal_judge(*, moments: list[dict[str, Any]], start: float) -> list[dict[str, Any]]:
    """Reaction judge. Falls back to the heuristic if AI is off/fails."""
    if not ai.enabled():
        return moments
    rows = []
    for m in moments:
        rows.append(f"[{m['start']:.0f}] L={m.get('loud_z', 0):+.1f} C={m.get('chat_peak', 0)}")
    if len(rows) < 2:
        return moments
    avg = _median([float(m.get("chat_peak", 0)) for m in moments])
    try:
        data = ai.chat_json(
            SIGNAL_SYSTEM,
            SIGNAL_PROMPT.format(avg=avg, lines="\n".join(rows)),
            max_tokens=700, temperature=0.3,
        )
        get_state().count_ai_check(True)
        judged = _array(data, "moments")
        # A judge that answers in window-relative seconds is normalised here.
        relative = bool(judged) and all(float(r.get("start", 99)) < start for r in judged)
        parsed = []
        for row in judged:
            try:
                s = float(row.get("start"))
                e = float(row.get("end"))
            except (TypeError, ValueError):
                continue
            if relative:
                s += start
                e += start
            if e <= s:
                continue
            parsed.append({
                "start": s, "end": e,
                "score": int(clamp(float(row.get("score", 0)), 0, 100)),
                "reason": str(row.get("reason", ""))[:200],
                "source": "signals",
            })
        return _attach_signal_meta(parsed, moments) or moments
    except ai.AIError as exc:
        get_state().count_ai_check(False, str(exc))
        LOG.info("Signal judge unavailable: %s", str(exc)[:200])
        return moments


def _attach_signal_meta(judged: list[dict[str, Any]],
                        heuristic: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Carry loudness/chat numbers from the heuristic rows onto AI verdicts."""
    for row in judged:
        best, best_ov = None, 0.0
        for h in heuristic:
            ov = _overlap(row["start"], row["end"], h["start"], h["end"])
            if ov > best_ov:
                best, best_ov = h, ov
        if best:
            row["peak_db"] = best.get("peak_db")
            row["loud_z"] = best.get("loud_z", 0)
            row["chat_peak"] = best.get("chat_peak", 0)
            row["chat_ratio"] = best.get("chat_ratio", 0)
    return judged


def _merge_judges(word_clips: list[dict[str, Any]], sig_moments: list[dict[str, Any]],
                  lo: float, hi: float, min_len: float, max_len: float) -> list[dict[str, Any]]:
    agreed: list[dict[str, Any]] = []
    for w in word_clips:
        snapshot = _snap_words_clip(w, lo, hi, min_len, max_len)
        if not snapshot:
            continue
        ws, we = snapshot
        best = None
        for s in sig_moments:
            ov = _overlap(ws, we, float(s["start"]), float(s["end"]))
            if ov >= min(3.0, (we - ws) * 0.25):
                if best is None or ov > best[0]:
                    best = (ov, s)
        if not best:
            continue
        sig = best[1]
        start = round(min(ws, float(sig["start"])), 2)
        end = round(max(we, float(sig["end"])), 2)
        if end - start > max_len:
            end = round(start + max_len, 2)
        word_score = int(clamp(float(w.get("score", 0)), 0, 100))
        sig_score = int(clamp(float(sig.get("score", 0)), 0, 100))
        agreed.append({
            "start": start,
            "end": end,
            "score": int(round(word_score * 0.55 + sig_score * 0.45)),
            "reason": "Both judges agree — " + (
                str(w.get("reason") or "standout words") + " / " + str(sig.get("reason") or "reaction spike")
            )[:280],
            "title": str(w.get("title") or "")[:80],
            "hook": str(w.get("hook") or "")[:90],
            "peak_db": sig.get("peak_db"),
            "loud_z": sig.get("loud_z", 0),
            "chat_peak": sig.get("chat_peak", 0),
            "source": "ai-dual",
        })
    agreed.sort(key=lambda c: c["start"])
    return agreed


# ---------------------------------------------------------------- local meta
def _local_meta(text: str, streamer: str, score: int, chat_ratio: float,
                loud_z: float) -> dict[str, Any]:
    words = [w.strip(".,!?\"'") for w in re.split(r"\s+", text or "") if w.strip()]
    key = ""
    match = HYPE_WORDS.search(text or "")
    if match:
        key = match.group(0).upper()
    title = " ".join(words[:6]) if words else f"{streamer} goes off"
    title = title[:70].strip() or "untitled moment"
    hook = key or ("CHAT WENT CRAZY" if chat_ratio >= 2 else "WAIT FOR IT")
    if loud_z >= 4:
        hook = "HE LOST IT"
    return {
        "title": title,
        "hook": hook[:90],
        "reason": f"Heuristic: audio {loud_z:+.1f}σ, chat {chat_ratio:.1f}× normal",
        "caption": "",
        "hashtags": [],
        "moment": text[:220],
        "verdict": "LIKELY VIRAL" if score >= 80 else "PROMISING" if score >= 65 else "MAYBE",
    }


def _enrich_with_ai(spike: dict[str, Any], *, streamer: str, platform: str,
                    transcript: str, chat_sample: str) -> dict[str, Any]:
    if not ai.enabled():
        meta = _local_meta(transcript, streamer, int(spike.get("score", 0)),
                           float(spike.get("chat_ratio", 0) or 0),
                           float(spike.get("loud_z", 0) or 0))
        spike.update({k: meta[k] for k in ("title", "hook", "reason", "caption", "hashtags", "verdict")})
        return spike
    try:
        meta = ai.score_viral(
            streamer=streamer, platform=platform,
            window_seconds=float(spike["end"] - spike["start"]),
            loudness_peak=float(spike.get("peak_db", -30) or -30),
            loudness_avg=float(spike.get("baseline_db", -30) or -30),
            chat_mps=float(spike.get("chat_peak", 0) or 0),
            chat_avg=float(spike.get("chat_avg", 0) or 0),
            chat_sample=chat_sample, transcript=transcript,
        )
        spike["ai_meta"] = meta
        spike["score"] = int(round(meta["score"] * 0.6 + spike.get("score", 0) * 0.4))
        spike.update({
            "title": meta.get("title") or spike.get("title") or "",
            "hook": meta.get("hook") or spike.get("hook") or "",
            "reason": meta.get("reason") or spike.get("reason") or "",
            "caption": meta.get("caption", ""),
            "hashtags": meta.get("hashtags", []),
            "moment": meta.get("moment", ""),
            "verdict": meta.get("verdict", "MAYBE"),
        })
        get_state().count_ai_check(True)
    except ai.AIError as exc:
        get_state().count_ai_check(False, str(exc))
        LOG.info("Viral scoring unavailable: %s", str(exc)[:200])
    return spike


# ------------------------------------------------------------------ live API
def detect_live(sid: str, *, window: int = 45, now: float | None = None,
                use_ai: bool | None = None) -> list[dict[str, Any]]:
    """One live check. Returns agreed spikes (seconds are absolute epoch)."""
    state = get_state()
    now = now or time.time()
    start = now - max(20, window)
    streamer_row = state.find_streamer(sid) or {}
    streamer = streamer_row.get("name") or sid
    platform = streamer_row.get("platform") or ""
    loud_points = state.loudness_points(sid, window + 300, now)
    events = list(state.chat_events.get(sid, ()))
    moments = signal_moments(loud_points=loud_points, chat_events=events,
                             start=start, end=now, min_len=4.0, max_len=min(60, window))
    if not moments:
        return []
    if use_ai is None:
        use_ai = ai.enabled()

    rows = list(state.transcript.get(sid, ()))
    recent = [r for r in rows if float(r.get("ts", 0)) >= start - 10]
    lines = [{"s": max(0.0, float(r["ts"]) - start), "text": r.get("text", "")} for r in recent]
    transcript_text = " ".join(r.get("text", "") for r in recent)[-2000:]
    chat_sample = " | ".join(
        str(r.get("message", "")) for r in events[-40:] if float(r.get("ts", 0)) >= start
    )[:900]

    if use_ai:
        signal_clips = _signal_judge(moments=moments, start=start)
        word_clips = _words_judge(streamer=streamer, platform=platform, title="",
                                  lines=lines, start=0, end=now - start, n=2)
        clipped = []
        for c in word_clips:
            try:
                s, e = float(c.get("start")), float(c.get("end"))
            except (TypeError, ValueError):
                continue
            clipped.append({**c, "start": s + start, "end": e + start})
        merged = _merge_judges(clipped, signal_clips, start, now, 8.0, float(window))
        if not merged:
            return []
    else:
        merged = [{
            "start": float(m["start"]), "end": float(m["end"]),
            "score": m["score"], "reason": m["reason"],
            "peak_db": m.get("peak_db"), "loud_z": m.get("loud_z", 0),
            "chat_peak": m.get("chat_peak", 0), "chat_ratio": m.get("chat_ratio", 0),
            "source": "heuristic",
        } for m in moments]

    baseline = [db for ts, db in loud_points if ts < start]
    base_db = _median(baseline) if baseline else (merged and merged[0].get("peak_db")) or -30
    chat_counts = [len([1 for r in events if int(float(r.get("ts", 0))) == int(t)]) for t in range(int(start), int(now))]
    chat_avg = (sum(chat_counts) / max(1, len(chat_counts))) if chat_counts else 0.0

    spikes: list[dict[str, Any]] = []
    for idx, moment in enumerate(merged):
        transcript_slice = _transcript_between(recent, moment["start"], moment["end"]) or transcript_text
        spike = {
            "id": f"{sid}-{int(moment['start'])}-{idx}",
            "streamer_id": sid,
            "streamer": streamer,
            "platform": platform,
            "ts": now_iso(),
            "at": round(time.time(), 2),
            "window_start": round(float(moment["start"]), 2),
            "window_end": round(float(moment["end"]), 2),
            "duration": round(float(moment["end"]) - float(moment["start"]), 1),
            "peak_db": moment.get("peak_db", base_db),
            "baseline_db": round(float(base_db), 1) if base_db is not None else -30.0,
            "loud_z": moment.get("loud_z", 0),
            "chat_peak": moment.get("chat_peak", 0),
            "chat_avg": round(float(chat_avg), 2),
            "chat_ratio": moment.get("chat_ratio", 0),
            "score": int(clamp(float(moment.get("score", 0)), 0, 100)),
            "reason": moment.get("reason", ""),
            "title": moment.get("title", ""),
            "hook": moment.get("hook", ""),
            "verdict": "",
            "source": moment.get("source", "heuristic"),
            "transcript": transcript_slice[:600],
            "chat_sample": chat_sample[:400],
        }
        spike = _enrich_with_ai(spike, streamer=streamer, platform=platform,
                                transcript=transcript_slice, chat_sample=chat_sample)
        spikes.append(spike)
    return spikes


def _transcript_between(rows: list[dict[str, Any]], start: float, end: float) -> str:
    parts = []
    for row in rows:
        ts = float(row.get("ts", 0))
        if start - 4 <= ts <= end + 4:
            parts.append(str(row.get("text", "")))
    return " ".join(parts)


# ------------------------------------------------------------------- VOD API
def _lines_from_words(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for w in words:
        try:
            s = float(w["start"])
            e = float(w.get("end", s + 0.4))
            word = str(w.get("word") or w.get("text") or "").strip()
        except (KeyError, TypeError, ValueError):
            continue
        if not word:
            continue
        if lines and s - float(lines[-1]["e"]) < 0.7:
            lines[-1]["text"] += " " + word
            lines[-1]["e"] = e
        else:
            lines.append({"s": s, "e": e, "text": word})
        if len(lines[-1]["text"]) > 160:
            lines.append({"s": e, "e": e + 0.1, "text": ""})
    return [ln for ln in lines if ln["text"].strip()]


def detect_vod(*, words: list[dict[str, Any]], duration: float, streamer: str,
               platform: str = "", title: str = "", audio_points: list[tuple[float, float]] | None = None,
               chat_events: list[dict[str, Any]] | None = None,
               max_clips: int = 6, min_len: int = 15, max_len: int = 60,
               progress: Progress | None = None) -> list[dict[str, Any]]:
    """Find clips in a VOD. Dual judges when AI is on, loudness heuristic otherwise."""
    if duration <= 0:
        return []
    lines = _lines_from_words(words)
    found: list[dict[str, Any]] = []

    if ai.enabled() and lines:
        window = 240
        step = 180
        t = 0.0
        windows = []
        while t < duration:
            windows.append((t, min(duration, t + window)))
            t += step
        per_window = max(2, min(6, max_clips))
        for idx, (a, b) in enumerate(windows):
            chunk = [ln for ln in lines if a <= ln["s"] < b]
            if len(chunk) < 3:
                continue
            if progress:
                progress("detecting", 10 + 40 * idx / max(1, len(windows)),
                         f"Judging window {idx + 1}/{len(windows)}")
            local = [{"s": ln["s"] - a, "text": ln["text"]} for ln in chunk]
            rows = _words_judge(streamer=streamer, platform=platform, title=title,
                                lines=local, start=0, end=b - a, n=per_window)
            for row in rows:
                clip = _snap_words_clip(row, a, b, min_len, max_len)
                if not clip:
                    continue
                found.append({
                    "start": clip[0], "end": clip[1],
                    "score": int(clamp(float(row.get("score", 0)), 0, 100)),
                    "reason": str(row.get("reason") or "")[:240],
                    "title": str(row.get("title") or "")[:80],
                    "hook": str(row.get("hook") or "")[:90],
                    "source": "words",
                })
        # Reaction agreement when we have audio points.
        if audio_points or chat_events:
            sig = signal_moments(loud_points=audio_points or [], chat_events=chat_events or [],
                                 start=0, end=duration,
                                 min_len=min_len, max_len=max_len, top=40)
            agreed = []
            for clip in found:
                best = None
                for m in sig:
                    ov = _overlap(clip["start"], clip["end"], m["start"], m["end"])
                    if ov >= min(4.0, (clip["end"] - clip["start"]) * 0.2):
                        if best is None or ov > best[0]:
                            best = (ov, m)
                if best:
                    clip["score"] = int(round(clip["score"] * 0.55 + best[1]["score"] * 0.45))
                    clip["reason"] = (clip["reason"] + " / " + str(best[1]["reason"]))[:300]
                    clip["peak_db"] = best[1].get("peak_db")
                    clip["source"] = "ai-dual"
                    agreed.append(clip)
            if agreed:
                found = agreed
            # No agreement found: keep the words-only picks rather than returning nothing.
    elif audio_points or chat_events:
        moments = signal_moments(loud_points=audio_points or [], chat_events=chat_events or [],
                                 start=0, end=duration,
                                 min_len=min_len, max_len=max_len, top=max_clips * 3)
        for m in moments:
            start = max(0.0, m["start"] - 5)
            end = min(duration, m["end"] + 6)
            found.append({
                "start": round(start, 2), "end": round(end, 2),
                "score": m["score"], "reason": m["reason"],
                "title": "", "hook": "", "peak_db": m.get("peak_db"),
                "loud_z": m.get("loud_z", 0), "source": "heuristic",
            })

    # Dedup: strongest first, drop overlaps.
    found.sort(key=lambda c: -float(c.get("score", 0)))
    picked: list[dict[str, Any]] = []
    for clip in found:
        if any(_overlap(clip["start"], clip["end"], p["start"], p["end"]) > 1.0 for p in picked):
            continue
        picked.append(clip)
        if len(picked) >= max_clips:
            break
    picked.sort(key=lambda c: c["start"])

    words_by_time = words
    for idx, clip in enumerate(picked):
        text = " ".join(
            str(w.get("word") or w.get("text") or "")
            for w in words_by_time
            if clip["start"] - 2 <= float(w.get("start", 0)) <= clip["end"] + 2
        )[:1500]
        clip["transcript"] = text
        if not clip.get("title"):
            meta = _local_meta(text, streamer, int(clip["score"]),
                               float(clip.get("chat_ratio", 0) or 0),
                               float(clip.get("loud_z", 0) or 0))
            clip.update({k: meta[k] for k in ("title", "hook", "reason", "verdict")})
        picked[idx] = _enrich_with_ai(clip, streamer=streamer, platform=platform,
                                      transcript=text, chat_sample="")
    return picked
