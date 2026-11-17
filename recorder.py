"""Stream recorder: yt-dlp -> ffmpeg into 10s MPEG-TS parts.

The parts are the rolling buffer (last N minutes), get grouped into 5-minute
.mp4 chunks, and are concatenated into full.mp4 when the stream ends. This
keeps RAM flat while still allowing instant retroactive clipping.
"""
from __future__ import annotations

import math
import os
import shutil
import signal
import subprocess
import threading
import time
from array import array
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from config import DIRS, settings
from utils import (
    LOG, ffprobe_duration, hms, now_iso, run_cmd, safe_name, save_json, slugify, which,
)

SegmentRow = dict[str, Any]


def _encoder_available(name: str) -> bool:
    try:
        res = run_cmd([settings.ffmpeg, "-hide_banner", "-encoders"], timeout=15)
        return name in res.stdout
    except Exception:
        return False


_HW_ENCODER: bool | None = None


def video_encoder() -> str:
    global _HW_ENCODER
    if _HW_ENCODER is None:
        _HW_ENCODER = _encoder_available("h264_videotoolbox")
    return "h264_videotoolbox" if _HW_ENCODER else "libx264"


def _parse_ffmpeg_progress(line: str) -> None:
    pass


class StreamRecorder:
    def __init__(self, streamer: dict[str, Any], url: str,
                 notify: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        self.streamer = dict(streamer)
        self.sid = streamer["id"]
        self.url = url
        self.notify = notify
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.yt_process: subprocess.Popen | None = None
        self.ff_process: subprocess.Popen | None = None
        self.status = "starting"
        self.error = ""
        self.started_at = now_iso()
        self.started_ts = time.time()
        self.stopped_at: str | None = None
        day = datetime.now().strftime("%Y-%m-%d")
        self.session_dir = DIRS["recordings"] / safe_name(self.streamer["name"], 50) / day
        self.segment_root = self.session_dir / "segments"
        self.chunk_dir = self.session_dir / "chunks"
        self.segment_root.mkdir(parents=True, exist_ok=True)
        self.chunk_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = datetime.now().strftime("%H%M%S")
        self.run_dir = self.segment_root / f"run_{self.run_id}"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._analyzed: set[str] = set()
        self._duration_cache: dict[tuple[str, int], float] = {}
        self._chunks_built: set[str] = set()
        self._restarts = 0
        self._last_scan = 0.0
        self._segments_cache: list[SegmentRow] = []
        self._segments_cache_at = 0.0
        self._log_path = DIRS["logs"] / f"recorder_{safe_name(self.streamer['name'], 30)}.log"
        self.session_chat_log = self.session_dir / "chat.jsonl"

    # ------------------------------------------------------------------ status
    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            segs = self._segment_rows()
            return {
                "id": self.sid,
                "name": self.streamer["name"],
                "platform": self.streamer["platform"],
                "username": self.streamer.get("username"),
                "status": self.status,
                "error": self.error,
                "started_at": self.started_at,
                "stopped_at": self.stopped_at,
                "elapsed": int(time.time() - self.started_ts),
                "segments": len(segs),
                "chunks": len(list(self.chunk_dir.glob("chunk_*.mp4"))),
                "session_dir": str(self.session_dir),
                "url": self.url,
                "restarts": self._restarts,
                "log": str(self._log_path),
            }

    def _patch(self, **kwargs: Any) -> None:
        if self.notify:
            try:
                self.notify(self.sid, kwargs)
            except Exception as exc:  # never kill recording because a callback broke
                LOG.debug("notify failed: %s", exc)

    # ------------------------------------------------------------------- start
    def start(self) -> None:
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.stop_event.clear()
            self.status = "starting"
            self.started_at = now_iso()
            self.started_ts = time.time()
            self._patch(status="recording", started_at=self.started_at,
                        preview_url=self.streamer.get("preview_url", ""))
            self.thread = threading.Thread(target=self._run_loop, name=f"rec-{self.sid}", daemon=True)
            self.thread.start()

    def _run_loop(self) -> None:
        while not self.stop_event.is_set():
            self._run_pipeline(transcode=False)
            if self.stop_event.is_set():
                break
            if self.status == "error":
                break
            self._restarts += 1
            if self._restarts > 6:
                self.status = "error"
                self.error = "Stream ended or yt-dlp failed repeatedly"
                self._patch(status="error", error=self.error)
                break
            self._patch(status="reconnecting", error=self.error)
            for _ in range(10):
                if self.stop_event.is_set():
                    break
                time.sleep(0.5)

    def _run_pipeline(self, transcode: bool) -> None:
        fmt = os.environ.get(
            "RECORDING_FORMAT",
            "b[height<=1080][vcodec^=avc1]/b[height<=1080]/b",
        )
        yt_cmd = [
            settings.ytdlp, "--no-warnings", "--quiet", "--no-part",
            "--no-playlist", "--live-from-start" if os.environ.get("RECORD_FROM_START") == "1" else "--no-live-from-start",
            "-f", fmt, "-o", "-", self.url,
        ]
        seg_pattern = str(self.run_dir / "seg_%05d.ts")
        ff_cmd = [
            settings.ffmpeg, "-hide_banner", "-loglevel", "warning", "-y",
            "-i", "pipe:0",
            "-map", "0:v:0?", "-map", "0:a:0?",
        ]
        if transcode or os.environ.get("FORCE_TRANSCODE") == "1":
            enc = video_encoder()
            ff_cmd += ["-c:v", enc]
            if enc == "h264_videotoolbox":
                ff_cmd += ["-b:v", "4500k", "-maxrate", "6000k", "-bufsize", "12M"]
            else:
                ff_cmd += ["-preset", "veryfast", "-crf", "24", "-maxrate", "5M", "-bufsize", "10M"]
            ff_cmd += ["-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
        else:
            ff_cmd += ["-c", "copy"]
        ff_cmd += [
            "-f", "segment", "-segment_time", str(max(2, settings.segment_seconds)),
            "-reset_timestamps", "1", "-segment_format", "mpegts", seg_pattern,
        ]

        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        log = self._log_path.open("ab")
        started_ts = time.time()
        try:
            log.write(f"\n--- {now_iso()} transcode={transcode or os.environ.get('FORCE_TRANSCODE') == '1'} ---\n".encode())
            log.flush()
            self.yt_process = subprocess.Popen(
                yt_cmd, stdout=subprocess.PIPE, stderr=log, stdin=subprocess.DEVNULL
            )
            assert self.yt_process.stdout is not None
            self.ff_process = subprocess.Popen(
                ff_cmd, stdin=self.yt_process.stdout, stdout=subprocess.DEVNULL, stderr=log
            )
            try:
                self.yt_process.stdout.close()
            except OSError:
                pass
            self.status = "recording"
            self.error = ""
            self._patch(status="recording", error="")
            self._wait_for_processes(transcode)
        except FileNotFoundError as exc:
            self.status = "error"
            self.error = f"Missing binary: {exc}"
            LOG.error("Recorder %s: %s", self.sid, self.error)
            self._patch(status="error", error=self.error)
        finally:
            self._terminate_processes()
            log.close()
            retry_transcode = (
                not self.stop_event.is_set()
                and time.time() - started_ts < 12
                and not transcode
                and not list(self.run_dir.glob("*.ts"))
            )
        if retry_transcode:
            # Pipeline died almost immediately (usually incompatible codecs).
            self._patch(status="starting", error="Retrying with transcode")
            self.run_id = datetime.now().strftime("%H%M%S")
            self.run_dir = self.segment_root / f"run_{self.run_id}"
            self.run_dir.mkdir(parents=True, exist_ok=True)
            self._run_pipeline(transcode=True)
            return
        if self.stop_event.is_set():
            self.status = "stopped"
        elif not self.error:
            self.error = "Stream ended"
            self.status = "stopped"

    def _wait_for_processes(self, transcode: bool) -> None:
        deadline_check = time.time() + 6
        while not self.stop_event.is_set():
            ff_alive = self.ff_process and self.ff_process.poll() is None
            if not ff_alive:
                break
            if time.time() < deadline_check:
                if self.ff_process and self.ff_process.poll() is not None:
                    break
            time.sleep(0.5)
        if self.stop_event.is_set():
            return
        # ffmpeg exited by itself: capture reason from yt-dlp.
        try:
            if self.yt_process and self.yt_process.poll() is None:
                self.yt_process.terminate()
        except OSError:
            pass
        if self.ff_process:
            try:
                code = self.ff_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.ff_process.kill()
                code = -9
            if code not in (0, None) and not self.stop_event.is_set():
                self.error = f"ffmpeg exited with code {code}"
                self.status = "stopped"

    def _terminate_processes(self) -> None:
        for proc in (self.yt_process, self.ff_process):
            if not proc:
                continue
            try:
                if proc.poll() is None:
                    proc.send_signal(signal.SIGINT)
            except OSError:
                pass
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
        self.yt_process = None
        self.ff_process = None

    # -------------------------------------------------------------------- stop
    def stop(self, reason: str = "stopped") -> dict[str, Any]:
        self.stop_event.set()
        self._patch(status="finalizing", error="")
        self._terminate_processes()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=15)
        self.status = "stopped"
        self.stopped_at = now_iso()
        try:
            self.finalize_chunks(force=True)
            self.build_full(reason)
        except Exception as exc:
            LOG.warning("Finalize failed for %s: %s", self.sid, exc)
            self.error = f"Finalize failed: {exc}"
        self._patch(status="stopped", error=self.error, stopped_at=self.stopped_at)
        return self.snapshot()

    # --------------------------------------------------------------- segments
    def _segment_rows(self, refresh: bool = False) -> list[SegmentRow]:
        now = time.time()
        with self.lock:
            if not refresh and self._segments_cache and now - self._segments_cache_at < 1.5:
                return self._segments_cache
            files = sorted(self.segment_root.glob("run_*/seg_*.ts"))
            rows: list[SegmentRow] = []
            for path in files:
                st = path.stat()
                key = (str(path), st.st_size)
                if key in self._duration_cache:
                    dur = self._duration_cache[key]
                else:
                    try:
                        dur = ffprobe_duration(path) or 0.0
                    except Exception:
                        dur = 0.0
                    # Only cache closed-ish files; open segments grow.
                    if now - st.st_mtime > 2:
                        self._duration_cache[key] = dur
                if dur <= 0.15:
                    continue
                rows.append({
                    "path": path,
                    "duration": dur,
                    "mtime": st.st_mtime,
                    "size": st.st_size,
                    "index": int(path.stem.split("_")[-1]),
                    "run": path.parent.name,
                })
            # Anchor end times backwards from the newest part.
            end = now
            for row in reversed(rows):
                row["end"] = end
                row["start"] = max(0.0, end - row["duration"])
                end = row["start"]
            self._segments_cache = rows
            self._segments_cache_at = now
            return rows

    def _closed_segments(self, force: bool = False) -> list[SegmentRow]:
        rows = self._segment_rows(refresh=True)
        if not rows:
            return []
        if force:
            return rows
        now = time.time()
        closed = []
        for i, row in enumerate(rows):
            is_last = i == len(rows) - 1
            if not is_last:
                closed.append(row)
            elif now - row["mtime"] > max(6, settings.segment_seconds + 3):
                closed.append(row)
        return closed

    def clip_window(self, seconds: float, end_offset: float = 0.0) -> dict[str, Any] | None:
        rows = self._segment_rows(refresh=True)
        if not rows:
            return None
        now = time.time() - max(0.0, end_offset)
        target_start = now - max(1.0, seconds)
        selected = [r for r in rows if r["end"] > target_start + 0.05 and r["start"] < now]
        if not selected:
            return None
        lines = []
        for row in selected:
            inpoint = max(0.0, target_start - row["start"])
            outpoint = min(row["duration"], now - row["start"])
            if outpoint - inpoint < 0.15:
                continue
            lines.append(f"file '{row['path'].as_posix()}'")
            lines.append(f"inpoint {inpoint:.3f}")
            lines.append(f"outpoint {outpoint:.3f}")
        if not lines:
            return None
        cache = DIRS["cache"] / "windows"
        cache.mkdir(parents=True, exist_ok=True)
        stamp = f"{int(time.time() * 1000)}"
        list_path = cache / f"{self.sid}_{stamp}.txt"
        out_path = cache / f"{self.sid}_{stamp}.ts"
        list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        cmd = [
            settings.ffmpeg, "-v", "error", "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_path), "-c", "copy", "-fflags", "+genpts", str(out_path),
        ]
        try:
            res = run_cmd(cmd, timeout=120)
            if res.returncode != 0 or not out_path.is_file() or out_path.stat().st_size < 1000:
                LOG.warning("clip_window concat failed: %s", res.stderr[-500:])
                # Fallback: cut from the last segment directly.
                last = rows[-1]
                cmd2 = [
                    settings.ffmpeg, "-v", "error", "-y",
                    "-sseof", f"-{min(seconds, last['duration']):.2f}",
                    "-i", str(last["path"]), "-c", "copy", str(out_path),
                ]
                res2 = run_cmd(cmd2, timeout=120)
                if res2.returncode != 0:
                    return None
            dur = ffprobe_duration(out_path) or seconds
            return {
                "path": out_path,
                "duration": dur,
                "window_start": target_start,
                "window_end": now,
                "cleanup": [out_path, list_path],
            }
        except Exception as exc:
            LOG.warning("clip_window error: %s", exc)
            return None
        finally:
            if list_path.exists():
                try:
                    list_path.unlink()
                except OSError:
                    pass

    def extract_recent_audio(self, seconds: float = 20) -> Path | None:
        window = self.clip_window(seconds, end_offset=0.5)
        if not window:
            return None
        wav = DIRS["cache"] / f"live_{self.sid}_{int(time.time())}.wav"
        cmd = [
            settings.ffmpeg, "-v", "error", "-y", "-i", str(window["path"]),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav),
        ]
        try:
            res = run_cmd(cmd, timeout=120)
            for path in window.get("cleanup", []):
                try:
                    Path(path).unlink()
                except OSError:
                    pass
            if res.returncode == 0 and wav.is_file() and wav.stat().st_size > 1000:
                return wav
        except Exception as exc:
            LOG.warning("extract_recent_audio: %s", exc)
        return None

    # --------------------------------------------------------------- loudness
    def analyze_new_segments(self) -> list[tuple[float, float]]:
        """Return (timestamp, dBFS) points for newly completed parts."""
        points: list[tuple[float, float]] = []
        closed = self._closed_segments()
        for row in closed:
            key = str(row["path"])
            if key in self._analyzed:
                continue
            self._analyzed.add(key)
            dbs = analyze_audio_file(row["path"])
            if not dbs:
                continue
            step = row["duration"] / max(1, len(dbs))
            start = row["end"] - row["duration"]
            for i, db in enumerate(dbs):
                points.append((start + i * step, db))
        return points

    # ----------------------------------------------------------------- chunks
    def finalize_chunks(self, force: bool = False) -> int:
        rows = self._closed_segments(force=force)
        if not rows:
            return 0
        per_chunk = max(1, int(math.ceil(settings.chunk_seconds / max(2, settings.segment_seconds))))
        built = 0
        for g in range(0, len(rows), per_chunk):
            group = rows[g:g + per_chunk]
            if not group:
                continue
            if len(group) < per_chunk and not force:
                continue
            idx = g // per_chunk
            out = self.chunk_dir / f"chunk_{idx:03d}.mp4"
            if out.is_file() and out.stat().st_size > 1000:
                self._chunks_built.add(str(out))
                continue
            if self._concat(group, out):
                self._chunks_built.add(str(out))
                built += 1
        return built

    def build_full(self, reason: str = "") -> Path | None:
        chunks = sorted(self.chunk_dir.glob("chunk_*.mp4"))
        full = self.session_dir / "full.mp4"
        rows = self._closed_segments(force=True)
        if not rows and not chunks:
            return None
        # Build any leftover group first.
        self.finalize_chunks(force=True)
        chunks = sorted(self.chunk_dir.glob("chunk_*.mp4"))
        if chunks:
            sources = [{"path": c, "duration": 0} for c in chunks]
        else:
            sources = rows
        tmp = DIRS["cache"] / f"full_{slugify(self.streamer['name'])}_{int(time.time())}.mp4"
        if not self._concat(sources, tmp):
            return None
        try:
            if full.exists():
                full.unlink()
            shutil.move(str(tmp), str(full))
        except OSError as exc:
            LOG.warning("full.mp4 move failed: %s", exc)
            return None
        save_json(self.session_dir / "session.json", {
            "streamer": self.streamer,
            "url": self.url,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at or now_iso(),
            "reason": reason,
            "full": str(full),
            "chunks": len(chunks),
            "segments": len(rows),
            "elapsed_seconds": int(time.time() - self.started_ts),
        })
        self._patch(full_path=str(full), status="stopped")
        return full

    def _concat(self, rows: list[dict[str, Any]], out: Path) -> bool:
        if not rows:
            return False
        out.parent.mkdir(parents=True, exist_ok=True)
        list_path = DIRS["cache"] / f"concat_{out.stem}_{int(time.time() * 1000)}.txt"
        list_path.write_text(
            "\n".join(f"file '{Path(r['path']).as_posix()}'" for r in rows) + "\n",
            encoding="utf-8",
        )
        cmd = [
            settings.ffmpeg, "-v", "error", "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_path), "-c", "copy", "-movflags", "+faststart", str(out),
        ]
        try:
            res = run_cmd(cmd, timeout=900)
            if res.returncode != 0 or not out.is_file() or out.stat().st_size < 1000:
                LOG.warning("concat copy failed (%s), retrying re-encode: %s",
                            out.name, res.stderr[-400:])
                cmd = [
                    settings.ffmpeg, "-v", "error", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(list_path), "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                    "-movflags", "+faststart", str(out),
                ]
                res = run_cmd(cmd, timeout=3600)
            return res.returncode == 0 and out.is_file() and out.stat().st_size > 1000
        except Exception as exc:
            LOG.warning("concat error: %s", exc)
            return False
        finally:
            try:
                list_path.unlink()
            except OSError:
                pass


def analyze_audio_file(path: str | Path, window_seconds: float = 0.5) -> list[float]:
    """Compute per-window RMS dBFS by streaming raw PCM through ffmpeg."""
    rate = 8000
    cmd = [
        settings.ffmpeg, "-v", "error", "-i", str(path), "-vn",
        "-ac", "1", "-ar", str(rate), "-f", "s16le", "pipe:1",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=90)
        if res.returncode != 0 or not res.stdout:
            return []
        samples = array("h")
        samples.frombytes(res.stdout[: len(res.stdout) // 2 * 2])
        if os.sys.byteorder != "little":
            samples.byteswap()
        size = max(400, int(rate * window_seconds))
        out: list[float] = []
        for i in range(0, len(samples), size):
            chunk = samples[i:i + size]
            if len(chunk) < size // 2:
                break
            total = 0
            for s in chunk:
                total += s * s
            rms = math.sqrt(total / len(chunk)) if chunk else 0.0
            if rms <= 1:
                out.append(-60.0)
            else:
                out.append(max(-60.0, min(0.0, 20.0 * math.log10(rms / 32768.0))))
        return out
    except Exception as exc:
        LOG.debug("audio analysis failed for %s: %s", path, exc)
        return []


class RecorderManager:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.recorders: dict[str, StreamRecorder] = {}
        self.notify: Callable[[str, dict[str, Any]], None] | None = None

    def set_notify(self, callback: Callable[[str, dict[str, Any]], None]) -> None:
        self.notify = callback

    def start(self, streamer: dict[str, Any], url: str) -> StreamRecorder:
        sid = streamer["id"]
        with self.lock:
            existing = self.recorders.get(sid)
            if existing and existing.status in {"recording", "starting", "reconnecting"}:
                return existing
            rec = StreamRecorder(streamer, url, self._notify)
            self.recorders[sid] = rec
            rec.start()
            return rec

    def _notify(self, sid: str, patch: dict[str, Any]) -> None:
        if self.notify:
            self.notify(sid, patch)

    def get(self, sid: str) -> StreamRecorder | None:
        with self.lock:
            return self.recorders.get(sid)

    def statuses(self) -> list[dict[str, Any]]:
        with self.lock:
            return [r.snapshot() for r in self.recorders.values()]

    def any_recording(self) -> bool:
        with self.lock:
            return any(r.status == "recording" for r in self.recorders.values())

    def stop(self, sid: str, reason: str = "stopped") -> dict[str, Any] | None:
        with self.lock:
            rec = self.recorders.get(sid)
        if not rec:
            return None
        return rec.stop(reason)

    def stop_all(self, reason: str = "stopped") -> list[dict[str, Any]]:
        with self.lock:
            recs = list(self.recorders.values())
        return [r.stop(reason) for r in recs]

    def cleanup(self) -> None:
        for rec in list(self.recorders.values()):
            if rec.status in {"stopped", "error"}:
                # keep history for UI but stop threads; objects are small
                pass


recorder_manager = RecorderManager()
