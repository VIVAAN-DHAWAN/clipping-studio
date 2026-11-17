"""OpenAI-compatible AI client used for every AI feature.

Works with OpenAI, Groq, OpenRouter, DeepSeek, NVIDIA NIM, Together, Fireworks,
Cerebras, xAI, Mistral, Ollama, LM Studio, or any custom /v1 endpoint.

Only short text snippets (and short audio windows for Whisper) are sent to the
configured endpoint. Full videos never leave the machine unless you click Post.
"""
from __future__ import annotations

import json
import mimetypes
import re
import time
from pathlib import Path
from typing import Any

import requests

from config import settings
from utils import LOG


class AIError(RuntimeError):
    pass


def _join(base: str, path: str) -> str:
    base = (base or "").rstrip("/")
    if not base:
        raise AIError("No AI endpoint configured. Set MY_AI_ENDPOINT_URL in .env")
    for suffix in ("/chat/completions", "/audio/transcriptions", "/models"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return base + path


def _headers(key: str | None = None) -> dict[str, str]:
    key = settings.api_key if key is None else key
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def enabled() -> bool:
    return bool(settings.ai_enabled)


def require() -> None:
    if not enabled():
        raise AIError(
            "AI is not configured. Add MY_AI_ENDPOINT_URL + MY_AI_API_KEY (or OPENAI_API_KEY) "
            "to clipping-studio/.env and restart."
        )


def _post(url: str, *, json_body: dict | None = None, data: dict | None = None,
          files: dict | None = None, headers: dict | None = None,
          timeout: float = 90, retries: int = 2) -> dict[str, Any]:
    last_error = ""
    for attempt in range(retries + 1):
        try:
            res = requests.post(
                url,
                json=json_body,
                data=data,
                files=files,
                headers=headers or _headers(),
                timeout=timeout,
            )
        except requests.RequestException as exc:
            last_error = f"Network error: {exc}"
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise AIError(last_error) from exc

        if res.status_code < 400:
            try:
                return res.json()
            except ValueError:
                return {"text": res.text}

        body = res.text[:700]
        last_error = f"AI endpoint {res.status_code}: {body}"
        if res.status_code in (408, 409, 425, 429, 500, 502, 503, 504) and attempt < retries:
            time.sleep(2.0 * (attempt + 1))
            continue
        raise AIError(last_error)
    raise AIError(last_error or "AI request failed")


# --------------------------------------------------------------------- chat
def chat(messages: list[dict[str, str]], *, model: str | None = None,
         temperature: float = 0.7, max_tokens: int = 1400,
         json_mode: bool = False, timeout: float | None = None,
         retries: int = 1) -> str:
    require()
    url = _join(settings.base_url, "/chat/completions")
    model = model or settings.ai_model
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    try:
        data = _post(url, json_body=payload, timeout=timeout or settings.ai_timeout,
                     retries=retries)
    except AIError as exc:
        if json_mode and "response_format" in str(exc).lower():
            payload.pop("response_format", None)
            data = _post(url, json_body=payload, timeout=timeout or settings.ai_timeout,
                         retries=retries)
        else:
            raise
    choices = data.get("choices") or []
    if not choices:
        raise AIError(f"AI response had no choices: {str(data)[:300]}")
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    if not content and msg.get("reasoning_content"):
        content = msg["reasoning_content"]
    if not content:
        raise AIError("AI returned an empty message")
    return str(content).strip()


def ask(system: str, user: str, *, json_mode: bool = False, model: str | None = None,
        temperature: float = 0.6, max_tokens: int = 1600) -> str:
    return chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        json_mode=json_mode, model=model, temperature=temperature, max_tokens=max_tokens,
    )


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


def parse_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    match = _FENCE_RE.search(text)
    if match:
        text = match.group(1).strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {"value": obj}
    except ValueError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(text[start:end + 1])
            return obj if isinstance(obj, dict) else {"value": obj}
        except ValueError:
            pass
    raise AIError("AI did not return valid JSON")


def chat_json(system: str, user: str, *, model: str | None = None,
              max_tokens: int = 1200, temperature: float = 0.4) -> dict[str, Any]:
    text = chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        json_mode=True, model=model, temperature=temperature, max_tokens=max_tokens,
    )
    return parse_json(text)


# -------------------------------------------------------------- transcription
def _transcribe_once(path: Path, payload: dict[str, Any], timeout: float,
                     key: str | None = None) -> dict[str, Any]:
    url = _join(settings.whisper_base_url, "/audio/transcriptions")
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    with path.open("rb") as fh:
        files = {"file": (path.name, fh, mime)}
        return _post(
            url, data=payload, files=files,
            headers=_headers(settings.whisper_api_key if key is None else key),
            timeout=timeout, retries=0,
        )


def transcribe(audio_path: str | Path, *, language: str | None = None,
               word_timestamps: bool = True, model: str | None = None,
               prompt: str | None = None) -> dict[str, Any]:
    """Return {text, words, segments, language, provider} using Whisper."""
    require()
    path = Path(audio_path)
    if not path.is_file() or path.stat().st_size < 100:
        raise AIError("Audio file is missing or empty")
    if path.stat().st_size > 24 * 1024 * 1024:
        raise AIError("Audio is larger than 24 MB; split it before transcribing")

    base_payload: dict[str, Any] = {"model": model or settings.whisper_model}
    if language:
        base_payload["language"] = language
    if prompt:
        base_payload["prompt"] = prompt[:800]

    variants: list[dict[str, Any]] = [
        {**base_payload, "response_format": "verbose_json",
         "timestamp_granularities[]": "word"},
        {**base_payload, "response_format": "verbose_json"},
        {**base_payload, "response_format": "json"},
    ]
    last_error = ""
    for payload in variants:
        try:
            data = _transcribe_once(path, payload, settings.ai_transcribe_timeout)
            text = str(data.get("text") or "").strip()
            words = data.get("words") or []
            segments = data.get("segments") or []
            if not words and segments and word_timestamps:
                for seg in segments:
                    seg_words = seg.get("words") or []
                    words.extend(seg_words)
            if not text and segments:
                text = " ".join(str(s.get("text", "")).strip() for s in segments).strip()
            if not text and not segments:
                last_error = "Whisper returned no text"
                continue
            clean_words = []
            for w in words:
                if not isinstance(w, dict):
                    continue
                label = str(w.get("word") or w.get("text") or "").strip()
                if not label:
                    continue
                try:
                    start = float(w.get("start", 0) or 0)
                    end = float(w.get("end", start + 0.25) or (start + 0.25))
                except (TypeError, ValueError):
                    start, end = 0.0, 0.25
                clean_words.append({"word": label, "start": start, "end": max(end, start + 0.05)})
            clean_segments = []
            for seg in segments:
                if not isinstance(seg, dict):
                    continue
                clean_segments.append({
                    "start": float(seg.get("start", 0) or 0),
                    "end": float(seg.get("end", 0) or 0),
                    "text": str(seg.get("text", "")).strip(),
                })
            return {
                "text": text,
                "words": clean_words,
                "segments": clean_segments,
                "language": data.get("language") or language or "",
                "provider": settings.whisper_model,
            }
        except AIError as exc:
            last_error = str(exc)
            msg = last_error.lower()
            # Only downgrade on capability/validation errors, not auth/network.
            if any(token in msg for token in ("400", "422", "415", "unsupported", "invalid")):
                continue
            raise
    raise AIError(last_error or "Transcription failed")


# ------------------------------------------------------------------- scoring
VIRAL_SYSTEM = """You are the viral clip analyst inside Clipping Studio.
You judge short-form clips of livestreams for TikTok / Reels / YouTube Shorts.
Be brutally honest, calibrated, and specific. You never invent events that are
not supported by the transcript or chat. Return ONLY valid JSON."""

VIRAL_PROMPT = """Analyze this livestream moment and decide whether it should be clipped.

STREAMER: {streamer}
PLATFORM: {platform}
WINDOW: {window_seconds:.0f} seconds
LOUDNESS: peak {loudness_peak:.1f} dBFS, rolling average {loudness_avg:.1f} dBFS ({loudness_ratio:.1f}x linear)
CHAT: {chat_mps:.2f} msg/s in the window, baseline {chat_avg:.2f} msg/s ({chat_ratio:.1f}x)
HYPE CHAT SAMPLE:
{chat_sample}
TRANSCRIPT:
{transcript}

Scoring rubric (0-100):
- 85-100 = chaotic/insane moment, immediate scroll-stopper, strong context
- 70-84 = clearly funny, clutch, rage, or shocking with a clean payoff
- 50-69 = decent but needs a better hook or more context
- 0-49 = quiet, inside joke, low context, or nothing happens

Return JSON exactly:
{{
  "score": 0-100 integer,
  "verdict": "LIKELY VIRAL" | "PROMISING" | "MAYBE" | "SKIP",
  "reason": "one punchy sentence naming the strongest signal",
  "title": "max 6 words, in the streamer's voice, no quotes, no hashtags",
  "hook": "max 8 words, text overlay hook",
  "caption": "one-line social caption with 1 emoji (optional)",
  "hashtags": ["5", "relevant", "hashtags", "without", "hash"],
  "moment": "what literally happened, 1 sentence"
}}"""


def score_viral(*, streamer: str, platform: str, window_seconds: float,
                loudness_peak: float, loudness_avg: float, chat_mps: float,
                chat_avg: float, chat_sample: str, transcript: str) -> dict[str, Any]:
    loud_ratio = 10 ** ((loudness_peak - loudness_avg) / 20.0) if loudness_avg else 1.0
    chat_ratio = chat_mps / chat_avg if chat_avg > 0.01 else (chat_mps / 0.3 if chat_mps else 1.0)
    prompt = VIRAL_PROMPT.format(
        streamer=streamer or "streamer",
        platform=platform or "unknown",
        window_seconds=window_seconds,
        loudness_peak=loudness_peak,
        loudness_avg=loudness_avg,
        loudness_ratio=loud_ratio,
        chat_mps=chat_mps,
        chat_avg=chat_avg,
        chat_ratio=chat_ratio,
        chat_sample=chat_sample[:900] or "(no chat available)",
        transcript=(transcript or "(no speech transcribed)")[-2400:],
    )
    data = chat_json(VIRAL_SYSTEM, prompt, max_tokens=700, temperature=0.35)
    try:
        score = int(round(float(data.get("score", 0))))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(100, score))
    verdict = str(data.get("verdict") or "").strip().upper()
    if verdict not in {"LIKELY VIRAL", "PROMISING", "MAYBE", "SKIP"}:
        verdict = "LIKELY VIRAL" if score >= 80 else "PROMISING" if score >= 65 else "MAYBE" if score >= 45 else "SKIP"
    hashtags = data.get("hashtags") or []
    if isinstance(hashtags, str):
        hashtags = [h.strip() for h in re.split(r"[, ]+", hashtags) if h.strip()]
    hashtags = [h.lstrip("#") for h in hashtags if isinstance(h, str)][:6]
    return {
        "score": score,
        "verdict": verdict,
        "reason": str(data.get("reason") or "")[:240],
        "title": str(data.get("title") or "").strip().strip('"')[:80],
        "hook": str(data.get("hook") or "").strip().strip('"')[:90],
        "caption": str(data.get("caption") or "").strip()[:280],
        "hashtags": hashtags,
        "moment": str(data.get("moment") or "")[:300],
    }


# --------------------------------------------------------------- copy helpers
SOCIAL_SYSTEM = """You are a short-form growth editor for streamer clips.
You write in the creator's natural voice, never corporate, never cringe.
Return ONLY valid JSON."""

CAPTION_PROMPT = """Write a {platform} caption for this clip.

STREAMER: {streamer}
CLIP TITLE: {title}
CONTEXT: {transcript}
PLATFORM RULES: {rules}

Return JSON:
{{"caption": "max 150 chars, hook-first, 1 emoji max", "hashtags": ["5-7", "tags", "without", "hash"]}}"""


def generate_caption(*, streamer: str, title: str, transcript: str,
                     platform: str = "tiktok") -> dict[str, Any]:
    rules = {
        "tiktok": "casual, lowercase energy, curiosity gap, no links",
        "reels": "punchy, relatable, one emoji, no hashtag wall",
        "shorts": "searchable phrasing, clean, family-safe",
    }.get(platform, "casual, punchy")
    data = chat_json(
        SOCIAL_SYSTEM,
        CAPTION_PROMPT.format(streamer=streamer, title=title,
                              transcript=(transcript or "")[-1600:],
                              platform=platform, rules=rules),
        max_tokens=400, temperature=0.8,
    )
    caption = str(data.get("caption") or title or "").strip()[:220]
    tags = data.get("hashtags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in re.split(r"[, ]+", tags) if t.strip()]
    tags = [t.lstrip("#") for t in tags if isinstance(t, str)][:7]
    if not tags:
        tags = ["gaming", "streamer", "clips", "fyp", "viral"]
    return {"caption": caption, "hashtags": tags}


TITLE_SYSTEM = """You write titles for viral gaming clips. Titles must be
specific to what was said or happened. No generic 'insane moment' filler.
Return ONLY valid JSON."""

TITLE_PROMPT = """Generate 5 titles for this clip.

STREAMER: {streamer}
TRANSCRIPT: {transcript}
CONTEXT: {context}

Return JSON:
{{"titles": [
  {{"text": "...", "type": "clickbait"}},
  {{"text": "...", "type": "clickbait"}},
  {{"text": "...", "type": "seo"}},
  {{"text": "...", "type": "seo"}},
  {{"text": "...", "type": "funny"}}
]}}
Each max 55 chars. No quotes around titles. No hashtags."""


def generate_titles(*, streamer: str, transcript: str, context: str = "") -> list[dict[str, str]]:
    data = chat_json(
        TITLE_SYSTEM,
        TITLE_PROMPT.format(streamer=streamer, transcript=(transcript or "")[-1600:],
                            context=context[:600]),
        max_tokens=600, temperature=0.85,
    )
    rows = data.get("titles") or []
    out = []
    for row in rows[:5]:
        if isinstance(row, str):
            out.append({"text": row.strip()[:80], "type": "title"})
        elif isinstance(row, dict):
            out.append({
                "text": str(row.get("text") or "").strip().strip('"')[:80],
                "type": str(row.get("type") or "title")[:20],
            })
    return [r for r in out if r["text"]]


DESCRIPTION_SYSTEM = """You are an SEO editor for gaming YouTube Shorts.
Return ONLY valid JSON."""


def generate_description(*, streamer: str, title: str, transcript: str) -> dict[str, Any]:
    data = chat_json(
        DESCRIPTION_SYSTEM,
        f"""Write a YouTube Shorts description and tags for:
STREAMER: {streamer}
TITLE: {title}
TRANSCRIPT: {(transcript or '')[-1800:]}

Return JSON:
{{"description": "2 short paragraphs, hook + context + CTA, no hashtags inside",
  "hashtags": ["10", "search", "tags"],
  "timestamps": [{{"time": "0:00", "label": "hook"}}]}}""",
        max_tokens=800, temperature=0.6,
    )
    return data


AGENT_SYSTEM = """You are an elite short-form content strategist and YouTube
operator for a GTA 6 / gaming channel. You are direct, specific, and practical.
Never output generic advice. Use concrete numbers, titles, and next actions.
Format with short markdown sections."""


def agent_run(system: str, prompt: str, *, max_tokens: int = 2600,
              temperature: float = 0.6) -> str:
    return chat(
        [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        max_tokens=max_tokens, temperature=temperature,
    )


def ping() -> dict[str, Any]:
    """Cheap round-trip used by the Settings tab. Not called automatically."""
    require()
    started = time.time()
    text = chat(
        [{"role": "system", "content": "Reply with exactly: OK"},
         {"role": "user", "content": "ping"}],
        max_tokens=8, temperature=0, retries=0, timeout=20,
    )
    return {"ok": True, "latency_ms": int((time.time() - started) * 1000), "reply": text[:80]}
