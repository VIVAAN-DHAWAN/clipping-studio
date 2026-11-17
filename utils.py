"""Small shared helpers: files, ffmpeg, timing, logging, clipboard."""
from __future__ import annotations

import json
import logging
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from config import BASE_DIR, DIRS, settings

_LOCK = threading.RLock()
_FILE_LOCKS: dict[str, threading.Lock] = {}


def _file_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _LOCK:
        if key not in _FILE_LOCKS:
            _FILE_LOCKS[key] = threading.Lock()
        return _FILE_LOCKS[key]


def setup_logging() -> None:
    logger = logging.getLogger("clipping_studio")
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    try:
        fh = logging.FileHandler(DIRS["logs"] / "app.log", encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        pass
    logger.propagate = False


LOG = logging.getLogger("clipping_studio")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def now_ts() -> float:
    return time.time()


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def clock_str(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time()).strftime("%H:%M:%S")


def hms(seconds: float | int | None) -> str:
    seconds = max(0, int(seconds or 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def human_ago(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        then = datetime.fromisoformat(iso)
        delta = (datetime.now(timezone.utc).astimezone() - then).total_seconds()
    except Exception:
        return ""
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def slugify(text: str, max_len: int = 60, fallback: str = "clip") -> str:
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    text = _SLUG_RE.sub("-", text).strip("-._").lower()
    text = re.sub(r"-{2,}", "-", text)
    return (text[:max_len].strip("-") or fallback)


def safe_name(text: str, max_len: int = 80) -> str:
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    text = _SLUG_RE.sub("_", text).strip("._-")
    return text[:max_len] or "item"


def load_json(path: str | Path, default: Any) -> Any:
    p = Path(path)
    if not p.is_file():
        return default
    try:
        with p.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        LOG.warning("Could not read %s: %s", p, exc)
        return default


def save_json(path: str | Path, data: Any, indent: int = 2) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lock = _file_lock(p)
    with lock:
        fd, tmp = tempfile.mkstemp(prefix=p.name + ".", dir=str(p.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=indent, ensure_ascii=False, default=str)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, p)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


def append_jsonl(path: str | Path, row: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lock = _file_lock(p)
    with lock:
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def read_jsonl(path: str | Path, limit: int = 0) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with p.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return rows[-limit:] if limit else rows


def run_cmd(cmd: list[str], timeout: float | None = 30, cwd: str | None = None,
            env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
        env=env,
        errors="replace",
    )


def which(name: str) -> str | None:
    return shutil.which(name)


def ffprobe_duration(path: str | Path) -> float | None:
    try:
        res = run_cmd([
            settings.ffprobe, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ], timeout=20)
        if res.returncode == 0:
            return float(res.stdout.strip())
    except (subprocess.SubprocessError, ValueError, OSError):
        pass
    return None


def ffprobe_info(path: str | Path) -> dict[str, Any]:
    try:
        res = run_cmd([
            settings.ffprobe, "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ], timeout=25)
        if res.returncode == 0:
            import json as _json

            return _json.loads(res.stdout)
    except Exception:
        pass
    return {}


def extract_audio(src: str | Path, dst: str | Path, start: float | None = None,
                  duration: float | None = None, rate: int = 16000) -> bool:
    cmd = [settings.ffmpeg, "-v", "error", "-y"]
    if start is not None:
        cmd += ["-ss", f"{max(0, start):.3f}"]
    cmd += ["-i", str(src)]
    if duration is not None:
        cmd += ["-t", f"{max(0.2, duration):.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(rate), "-c:a", "pcm_s16le", str(dst)]
    try:
        res = run_cmd(cmd, timeout=180)
        return res.returncode == 0 and Path(dst).is_file() and Path(dst).stat().st_size > 44
    except (subprocess.SubprocessError, OSError):
        return False


def ffmpeg_available() -> bool:
    return bool(which(settings.ffmpeg)) and bool(which(settings.ffprobe))


def ytdlp_available() -> bool:
    return bool(which(settings.ytdlp))


def open_in_file_manager(path: str | Path) -> tuple[bool, str]:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    system = platform.system()
    cmd = settings.open_folder_cmd or None
    try:
        if cmd:
            subprocess.Popen(cmd.split() + [str(p)])
        elif system == "Darwin":
            subprocess.Popen(["open", str(p)])
        elif system == "Windows":
            subprocess.Popen(["explorer", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
        return True, f"Opened {p}"
    except OSError as exc:
        return False, f"Could not open folder: {exc}"


def copy_to_clipboard(text: str) -> tuple[bool, str]:
    system = platform.system()
    try:
        if system == "Darwin":
            proc = subprocess.run(["pbcopy"], input=text.encode(), timeout=5)
        elif system == "Windows":
            proc = subprocess.run(["clip"], input=text.encode("utf-16le"), timeout=5)
        else:
            if shutil.which("wl-copy"):
                proc = subprocess.run(["wl-copy"], input=text.encode(), timeout=5)
            elif shutil.which("xclip"):
                proc = subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode(), timeout=5)
            else:
                return False, "No clipboard tool found"
        return (proc.returncode == 0), "Copied to clipboard"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Clipboard error: {exc}"


def safe_join(base: str | Path, relative: str) -> Path | None:
    base = Path(base).resolve()
    try:
        target = (base / relative).resolve()
        target.relative_to(base)
        return target
    except (ValueError, OSError):
        return None


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def db_to_percent(db: float) -> float:
    """Map -60..0 dBFS to 0..100 for the UI graph."""
    if db is None or not math.isfinite(db):
        return 0.0
    return clamp((db + 60.0) / 60.0 * 100.0, 0.0, 100.0)


def parse_percent(text: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)%", text)
    if m:
        return clamp(float(m.group(1)), 0, 100)
    return None


def fmt_bytes(n: int | float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:.1f} TB"


def flatten_words(words: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for w in words:
        text = str(w.get("word") or w.get("text") or "").strip()
        if not text:
            continue
        try:
            start = float(w.get("start", 0))
            end = float(w.get("end", start + 0.2))
        except (TypeError, ValueError):
            continue
        out.append({"word": text, "start": start, "end": max(end, start + 0.05)})
    return out


def ensure_inside(path: str | Path, root: str | Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False
