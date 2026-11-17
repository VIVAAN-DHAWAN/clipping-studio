"""Hormozi-style word-by-word captions: ASS burn-in + SRT sidecar."""
from __future__ import annotations

import os
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from config import settings
from utils import LOG, run_cmd

EMPHASIS = {
    "W", "WOW", "NO", "WAY", "CLIP", "CLIPIT", "LOL", "LMAO", "OMG", "WHAT",
    "WTF", "INSANE", "CRAZY", "CLUTCH", "RAGE", "FAIL", "BRO", "HOLY", "POG",
    "GOAT", "STOP", "WAIT", "LOOK", "RUN", "HELP", "DEAD", "SHEESH", "RIOT",
}
_FONT_CANDIDATES = [
    os.environ.get("CAPTION_FONT", ""),
    "Arial Black",
    "Arial-Bold",
    "Impact",
    "DejaVu Sans",
    "Liberation Sans",
]
_PUNCT_RE = re.compile(r"^[^\w']+|[^\w']+$", re.UNICODE)


def caption_font() -> str:
    return next((f for f in _FONT_CANDIDATES if f), "Arial Black")


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    if cs == 100:
        cs = 99
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _srt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms == 1000:
        ms = 999
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _display_word(word: str) -> str:
    word = _PUNCT_RE.sub("", word.strip())
    word = word.replace("_", " ")
    return word.upper()


def _ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", "\\N")


def _style_word(word: str) -> str:
    raw = _display_word(word)
    if not raw:
        return ""
    if raw.strip("!?.,") in EMPHASIS:
        return r"{\c&H0000FFFF&}" + _ass_escape(raw) + r"{\c&H00FFFFFF&}"
    return _ass_escape(raw)


def words_from_segments(segments: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Spread words across a segment when word-level timing isn't available."""
    out: list[dict[str, Any]] = []
    for seg in segments:
        text = str(seg.get("text") or "").strip()
        try:
            start = float(seg.get("start", 0) or 0)
            end = float(seg.get("end", start + 2) or (start + 2))
        except (TypeError, ValueError):
            start, end = 0.0, 2.0
        tokens = [t for t in re.split(r"\s+", text) if t]
        if not tokens:
            continue
        span = max(0.35, end - start)
        per = span / len(tokens)
        for i, token in enumerate(tokens):
            out.append({
                "word": token,
                "start": start + i * per,
                "end": start + (i + 1) * per,
            })
    return out


def words_from_text(text: str, duration: float) -> list[dict[str, Any]]:
    tokens = [t for t in re.split(r"\s+", (text or "").strip()) if t]
    if not tokens or duration <= 0:
        return []
    weights = [max(1, len(t)) for t in tokens]
    total = sum(weights)
    t = 0.0
    out = []
    for token, weight in zip(tokens, weights):
        span = duration * (weight / total)
        out.append({"word": token, "start": t, "end": t + span})
        t += span
    return out


def build_cues(words: list[dict[str, Any]], *, max_words: int = 2,
               max_chars: int = 18, max_gap: float = 0.55) -> list[dict[str, Any]]:
    cues: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []

    def flush() -> None:
        if current:
            cues.append({
                "start": current[0]["start"],
                "end": max(current[-1]["end"], current[0]["start"] + 0.18),
                "words": list(current),
            })
            current.clear()

    for word in words:
        if not _display_word(word["word"]):
            continue
        if current:
            gap = word["start"] - current[-1]["end"]
            chars = sum(len(_display_word(w["word"])) for w in current) + len(_display_word(word["word"]))
            if len(current) >= max_words or chars > max_chars or gap > max_gap:
                flush()
        current.append(word)
    flush()
    for i in range(len(cues) - 1):
        cues[i]["end"] = min(cues[i]["end"], cues[i + 1]["start"] - 0.02)
    return [c for c in cues if c["end"] > c["start"]]


def _hook_lines(hook: str) -> str:
    hook = " ".join((hook or "").split()).upper()
    if not hook:
        return ""
    words = hook.split()
    lines, line = [], []
    for w in words:
        if sum(len(x) + 1 for x in line + [w]) > 20 and line:
            lines.append(" ".join(line))
            line = [w]
        else:
            line.append(w)
    if line:
        lines.append(" ".join(line))
    return "\\N".join(lines[:3])


def build_ass(words: list[dict[str, Any]], *, duration: float, hook: str = "",
              hook_seconds: float = 5.0) -> str:
    font = caption_font()
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hormozi,{font},116,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,1,0,1,9,3,5,24,24,0,1
Style: Hook,{font},80,&H0000FFFF,&H0000FFFF,&H00000000,&H78000000,-1,0,0,0,100,100,1,0,1,10,3,8,50,50,240,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines: list[str] = []
    if hook:
        hook_text = _hook_lines(hook)
        if hook_text:
            end = max(1.5, min(hook_seconds, duration if duration else hook_seconds))
            lines.append(
                f"Dialogue: 0,{_ass_time(0)},{_ass_time(end)},Hook,,0,0,0,,"
                r"{\pos(540,285)\fad(120,120)}" + hook_text
            )
    cues = build_cues(words)
    for cue in cues:
        if duration and cue["start"] > duration + 0.5:
            continue
        text = " ".join(_style_word(w["word"]) for w in cue["words"]).strip()
        text = " ".join(t for t in text.split(" ") if t)
        if not text:
            continue
        end = min(cue["end"], duration) if duration else cue["end"]
        if end <= cue["start"]:
            continue
        payload = (
            r"{\an5\pos(540,1435)\fad(30,20)\fscx112\fscy112"
            r"\t(0,90,\fscx100\fscy100)}" + text
        )
        lines.append(
            f"Dialogue: 0,{_ass_time(cue['start'])},{_ass_time(end)},Hormozi,,0,0,0,,{payload}"
        )
    return header + "\n".join(lines) + "\n"


def build_srt(words: list[dict[str, Any]], *, duration: float = 0,
              max_words: int = 6) -> str:
    if not words:
        return ""
    cues = build_cues(words, max_words=max_words, max_chars=48, max_gap=0.9)
    out = []
    for i, cue in enumerate(cues, 1):
        text = " ".join(_display_word(w["word"]) for w in cue["words"])
        end = min(cue["end"], duration) if duration else cue["end"]
        out.append(f"{i}\n{_srt_time(cue['start'])} --> {_srt_time(end)}\n{text}\n")
    return "\n".join(out)


def write_ass(path: str | Path, words: list[dict[str, Any]], **kwargs: Any) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(build_ass(words, **kwargs), encoding="utf-8")
    return p


def write_srt(path: str | Path, words: list[dict[str, Any]], **kwargs: Any) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(build_srt(words, **kwargs), encoding="utf-8")
    return p


def burn_captions(video: str | Path, ass_path: str | Path, out: str | Path,
                  fonts_dir: str | None = None) -> bool:
    """Burn an ASS file into a video. Re-encodes video, copies audio."""
    ass_arg = str(Path(ass_path).resolve()).replace("\\", "/").replace(":", r"\:")
    ass_arg = ass_arg.replace("'", r"\'")
    filt = f"subtitles='{ass_arg}'"
    if fonts_dir:
        fdir = str(Path(fonts_dir).resolve()).replace("\\", "/").replace(":", r"\:")
        filt += f":fontsdir='{fdir}'"
    cmd = [
        settings.ffmpeg, "-v", "error", "-y", "-i", str(video),
        "-vf", filt, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", str(out),
    ]
    try:
        res = run_cmd(cmd, timeout=600)
        if res.returncode != 0:
            LOG.warning("Caption burn failed: %s", res.stderr[-800:])
        return res.returncode == 0 and Path(out).is_file() and Path(out).stat().st_size > 1000
    except (subprocess.SubprocessError, OSError) as exc:
        LOG.warning("Caption burn error: %s", exc)
        return False


def hook_from_chat(messages: list[dict[str, Any]]) -> str:
    """Pick the most repeated (or most hype) chat message as the top hook."""
    if not messages:
        return ""
    cleaned = []
    for row in messages:
        msg = " ".join(str(row.get("message") or "").split())
        if not msg or msg.startswith("!"):
            continue
        if re.fullmatch(r"[\W_]+", msg):
            continue
        if len(msg) > 60:
            continue
        cleaned.append(msg.strip())
    if not cleaned:
        return ""
    hype_words = ("W", "CLIP", "LMAO", "INSANE", "NO WAY", "OMG", "POG", "GOAT", "LOL")
    counts = Counter(m.upper() for m in cleaned)
    best, count = counts.most_common(1)[0]
    if count > 1:
        return " ".join(best.split()[:9])
    hype = [m for m in cleaned if any(h in m.upper() for h in hype_words)]
    pick = max(hype or cleaned, key=len)
    return " ".join(pick.split()[:9])


def merge_words_from_sources(*sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best = []
    for src in sources:
        if src and len(src) > len(best):
            best = src
    return best
