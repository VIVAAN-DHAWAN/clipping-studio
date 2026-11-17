"""Central configuration for Clipping Studio.

Everything is configurable through environment variables / a local .env file.
Keys are never logged or returned to the browser.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is a hard requirement, but stay usable
    load_dotenv = None

BASE_DIR = Path(__file__).resolve().parent


def _load_env_files() -> None:
    if load_dotenv is None:
        return
    candidates = []
    extra = os.environ.get("CLIPPING_STUDIO_ENV")
    if extra:
        candidates.append(Path(extra).expanduser())
    candidates.append(BASE_DIR / ".env")
    candidates.append(Path.home() / ".clipping-studio.env")
    for path in candidates:
        try:
            if path.is_file():
                load_dotenv(path, override=False)
        except OSError:
            continue


_load_env_files()


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _int(name: str, default: int) -> int:
    try:
        raw = os.environ.get(name)
        return int(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name)
        return float(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _clean_url(url: str) -> str:
    return url.strip().rstrip("/")


def _has_filter(binary: str, name: str) -> bool:
    """True when this ffmpeg build exposes a filter we need for captions."""
    try:
        res = subprocess.run(
            [binary, "-hide_banner", "-h", f"filter={name}"],
            capture_output=True, text=True, timeout=6,
        )
        out = (res.stdout or "") + (res.stderr or "")
        return res.returncode == 0 and "Unknown filter" not in out and bool(out.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def _find_ffmpeg() -> str:
    """Prefer a full ffmpeg build that can burn ASS captions.

    Homebrew's regular ffmpeg bottle ships without libass/freetype on macOS,
    while the static build bundled with imageio-ffmpeg includes both. If the
    system build already has the subtitle filters we keep using it.
    """
    explicit = os.environ.get("FFMPEG_BIN", "").strip()
    if explicit:
        return explicit
    system = shutil.which("ffmpeg")
    candidates: list[str] = [system] if system else []
    try:
        import imageio_ffmpeg  # type: ignore

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled and bundled not in candidates:
            candidates.append(bundled)
    except Exception:
        pass
    for candidate in candidates:
        if _has_filter(candidate, "ass") or _has_filter(candidate, "subtitles"):
            return candidate
    return candidates[0] if candidates else "ffmpeg"


PROVIDER_DEFAULTS: dict[str, tuple[str, str]] = {
    "OPENAI_API_KEY": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "GROQ_API_KEY": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    "OPENROUTER_API_KEY": ("https://openrouter.ai/api/v1", "openai/gpt-4o-mini"),
    "DEEPSEEK_API_KEY": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "NVIDIA_API_KEY": ("https://integrate.api.nvidia.com/v1", "meta/llama-3.1-70b-instruct"),
    "TOGETHER_API_KEY": ("https://api.together.xyz/v1", "meta-llama/Llama-3.3-70B-Instruct-Turbo"),
    "FIREWORKS_API_KEY": ("https://api.fireworks.ai/inference/v1", "accounts/fireworks/models/llama-v3p3-70b-instruct"),
    "MISTRAL_API_KEY": ("https://api.mistral.ai/v1", "mistral-large-latest"),
    "XAI_API_KEY": ("https://api.x.ai/v1", "grok-2-latest"),
    "CEREBRAS_API_KEY": ("https://api.cerebras.ai/v1", "llama-3.3-70b"),
}

STREAMER_SCHEMA_FIELDS = {
    "id", "name", "platform", "username", "channel_id", "handle",
    "enabled", "allowed_post", "avatar", "notes",
}


def _discover_ai() -> tuple[str, str, str, str]:
    """Return (base_url, api_key, provider, default_model)."""
    explicit_base = _clean_url(os.environ.get("MY_AI_ENDPOINT_URL", ""))
    explicit_model = os.environ.get("MY_AI_MODEL") or os.environ.get("AI_MODEL") or ""
    key = (
        os.environ.get("MY_AI_API_KEY")
        or os.environ.get("AI_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ""
    )
    provider = "custom" if explicit_base else ""
    default_model = explicit_model or "gpt-4o-mini"

    if key and not provider:
        for env_name, (base, model) in PROVIDER_DEFAULTS.items():
            if os.environ.get(env_name):
                key = os.environ[env_name]
                provider = env_name.replace("_API_KEY", "").lower()
                return base, key, provider, explicit_model or model

    if explicit_base:
        if "11434" in explicit_base or "ollama" in explicit_base:
            provider = "ollama"
        return explicit_base, key, provider, default_model

    # No explicit endpoint. Allow a local Ollama instance automatically.
    if _bool("USE_OLLAMA", False):
        return "http://127.0.0.1:11434/v1", key or "ollama", "ollama", explicit_model or "llama3.2"

    return "", key, provider, default_model


class Settings:
    def __init__(self) -> None:
        self.reload()

    def reload(self) -> None:
        base, key, provider, model = _discover_ai()
        self.base_url = _clean_url(os.environ.get("MY_AI_ENDPOINT_URL", "") or base)
        self.api_key = os.environ.get("MY_AI_API_KEY") or os.environ.get("AI_API_KEY") or key
        self.ai_provider = provider
        self.ai_model = os.environ.get("MY_AI_MODEL") or os.environ.get("AI_MODEL") or model
        self.ai_fast_model = os.environ.get("AI_FAST_MODEL") or self.ai_model
        self.whisper_model = os.environ.get("WHISPER_MODEL", "whisper-1")
        self.whisper_base_url = _clean_url(
            os.environ.get("WHISPER_BASE_URL", "") or self.base_url
        )
        self.whisper_api_key = os.environ.get("WHISPER_API_KEY", "") or self.api_key
        self.ai_enabled = bool(self.base_url) and (
            bool(self.api_key)
            or "127.0.0.1" in self.base_url
            or "localhost" in self.base_url
        )
        self.ai_timeout = _float("AI_TIMEOUT_SECONDS", 90.0)
        self.ai_transcribe_timeout = _float("AI_TRANSCRIBE_TIMEOUT_SECONDS", 300.0)

        self.host = os.environ.get("HOST", "127.0.0.1")
        self.port = _int("PORT", 8765)
        self.debug = _bool("FLASK_DEBUG", False)

        self.demo_mode = _bool("DEMO_MODE", True)
        self.buffer_seconds = _int("BUFFER_SECONDS", 360)
        self.segment_seconds = _int("SEGMENT_SECONDS", 10)
        self.chunk_seconds = _int("CHUNK_SECONDS", 300)
        self.spike_pre = _int("SPIKE_PRE_SECONDS", 30)
        self.spike_post = _int("SPIKE_POST_SECONDS", 15)
        self.auto_clip = _bool("AUTO_CLIP_ENABLED", True)
        self.auto_clip_cooldown = _int("AUTO_CLIP_COOLDOWN_SECONDS", 60)
        self.max_auto_clips_per_stream = _int("MAX_AUTO_CLIPS_PER_STREAM", 0)
        self.loudness_spike_db = _float("LOUDNESS_SPIKE_DB", 6.0)
        self.chat_spike_multiplier = _float("CHAT_SPIKE_MULTIPLIER", 3.0)
        self.live_transcribe_interval = _int("LIVE_TRANSCRIBE_INTERVAL", 20)
        self.live_transcribe_window = _int("LIVE_TRANSCRIBE_WINDOW", 20)
        self.poll_seconds = _int("MONITOR_POLL_SECONDS", 30)
        self.max_recording_gb = _float("MAX_RECORDING_GB", 0)
        self.max_vod_minutes = _int("MAX_VOD_MINUTES", 0)

        self.twitch_client_id = os.environ.get("TWITCH_CLIENT_ID", "")
        self.twitch_client_secret = os.environ.get("TWITCH_CLIENT_SECRET", "")
        self.twitch_token = os.environ.get("TWITCH_TOKEN", "")
        self.youtube_api_key = os.environ.get("YOUTUBE_API_KEY", "")

        self.youtube_client_id = os.environ.get("YOUTUBE_CLIENT_ID", "")
        self.youtube_client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET", "")
        self.youtube_refresh_token = os.environ.get("YOUTUBE_REFRESH_TOKEN", "")

        self.tiktok_access_token = os.environ.get("TIKTOK_ACCESS_TOKEN", "") or os.environ.get("TIKTOK_KEY", "")
        self.tiktok_open_id = os.environ.get("TIKTOK_OPEN_ID", "")
        self.instagram_access_token = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "")
        self.instagram_user_id = os.environ.get("INSTAGRAM_USER_ID", "")
        self.public_base_url = _clean_url(os.environ.get("PUBLIC_BASE_URL", ""))

        self.allowed_posters = {
            s.strip().lower()
            for s in os.environ.get("ALLOWED_STREAMERS", "").split(",")
            if s.strip()
        }
        self.ffmpeg = _find_ffmpeg()
        self.ffprobe = os.environ.get("FFPROBE_BIN", "ffprobe")
        self.ytdlp = os.environ.get("YTDLP_BIN", "yt-dlp")
        self.open_folder_cmd = os.environ.get("OPEN_FOLDER_CMD", "")

    @property
    def env_status(self) -> dict[str, bool]:
        """Presence map only -- never expose values."""
        return {
            "AI": bool(self.base_url and self.api_key) or (
                bool(self.base_url) and ("127.0.0.1" in self.base_url or "localhost" in self.base_url)
            ),
            "Twitch API": bool(self.twitch_client_id and (self.twitch_client_secret or self.twitch_token)),
            "YouTube API": bool(self.youtube_api_key),
            "YouTube upload": bool(
                self.youtube_client_id and self.youtube_client_secret and self.youtube_refresh_token
            ),
            "TikTok": bool(self.tiktok_access_token),
            "Instagram": bool(self.instagram_access_token and self.instagram_user_id),
        }

    def ai_public(self) -> dict[str, Any]:
        return {
            "enabled": self.ai_enabled,
            "provider": self.ai_provider or "none",
            "model": self.ai_model if self.ai_enabled else "",
            "fast_model": self.ai_fast_model if self.ai_enabled else "",
            "whisper_model": self.whisper_model if self.ai_enabled else "",
            "endpoint_host": _host_only(self.base_url),
            "env": self.env_status,
        }


def _host_only(url: str) -> str:
    if not url:
        return ""
    try:
        from urllib.parse import urlparse

        return urlparse(url).netloc or url[:40]
    except Exception:
        return ""


settings = Settings()


def reload_settings() -> Settings:
    settings.reload()
    return settings


# ---- directories -----------------------------------------------------------
DIRS = {
    "data": BASE_DIR / "data",
    "recordings": BASE_DIR / "recordings",
    "clips": BASE_DIR / "clips",
    "transcripts": BASE_DIR / "transcripts",
    "agent_outputs": BASE_DIR / "agent_outputs",
    "cache": BASE_DIR / "cache",
    "logs": BASE_DIR / "logs",
    "post_packages": BASE_DIR / "data" / "post_packages",
}

for _d in DIRS.values():
    _d.mkdir(parents=True, exist_ok=True)


def dir_path(name: str) -> Path:
    return DIRS[name]
