"""One-click posting plus a no-API fallback.

Real uploads need platform credentials in .env. When a credential is missing the
publisher builds a ready-to-post package (video path, caption, hashtags, title)
and copies the caption to the clipboard instead of failing silently.
"""
from __future__ import annotations

import json
import mimetypes
import time
from pathlib import Path
from typing import Any

import requests

import ai
from config import DIRS, settings
from state import get_state
from utils import LOG, copy_to_clipboard, now_iso, safe_name, slugify

PLATFORMS = ("tiktok", "instagram", "youtube")


def _clip_paths(clip: dict[str, Any]) -> tuple[Path | None, Path | None]:
    clips_root = DIRS["clips"]
    video = clips_root / str(clip.get("file") or "") if clip.get("file") else None
    clean = clips_root / str(clip.get("clean_file") or "") if clip.get("clean_file") else None
    if video and not video.is_file():
        video = None
    if clean and not clean.is_file():
        clean = None
    return video, clean


def _public_url(clip: dict[str, Any]) -> str:
    if not settings.public_base_url:
        return ""
    rel = clip.get("file") or ""
    return f"{settings.public_base_url}/media/clips/{rel}" if rel else ""


def _copy_for(clip: dict[str, Any], platform: str) -> dict[str, Any]:
    """AI caption + hashtags when available, deterministic fallback otherwise."""
    title = str(clip.get("title") or "untitled clip")[:90]
    transcript = str(clip.get("transcript") or "")
    caption = str(clip.get("caption") or "").strip()
    tags = list(clip.get("hashtags") or [])
    if ai.enabled():
        try:
            result = ai.generate_caption(
                streamer=str(clip.get("streamer") or "streamer"),
                title=title, transcript=transcript, platform=platform,
            )
            caption = result.get("caption") or caption
            tags = result.get("hashtags") or tags
        except ai.AIError as exc:
            LOG.info("Caption generation unavailable: %s", str(exc)[:160])
    if not caption:
        hook = str(clip.get("hook") or "").strip()
        caption = f"{hook.lower().capitalize()} 😭" if hook else title
    if not tags:
        base = slugify(str(clip.get("streamer") or "gaming"), 20, "gaming").replace("-", "")
        tags = [base, "gaming", "streamer", "clips", "fyp"]
    tags = [str(t).lstrip("#").strip() for t in tags if str(t).strip()][:7]
    hashtag_line = " ".join(f"#{t}" for t in tags)
    full = f"{caption}\n\n{hashtag_line}".strip()
    return {
        "platform": platform,
        "title": title,
        "caption": caption,
        "hashtags": tags,
        "caption_with_tags": full,
        "clip_id": clip.get("id", ""),
    }


def build_package(clip_id: str, platforms: list[str] | None = None) -> dict[str, Any]:
    state = get_state()
    clip = state.get_clip(clip_id)
    if not clip:
        raise KeyError("Clip not found")
    platforms = [p for p in (platforms or list(PLATFORMS)) if p in PLATFORMS]
    video, clean = _clip_paths(clip)
    if not video and not clean:
        raise FileNotFoundError("Clip file is missing on disk")
    out_dir = DIRS["post_packages"]
    out_dir.mkdir(parents=True, exist_ok=True)
    package = {
        "clip_id": clip_id,
        "created_at": now_iso(),
        "title": clip.get("title", ""),
        "streamer": clip.get("streamer", ""),
        "video": str(video or clean),
        "clean_video": str(clean) if clean else "",
        "duration": clip.get("duration", 0),
        "viral_score": clip.get("viral_score", 0),
        "public_url": _public_url(clip),
        "per_platform": {p: _copy_for(clip, p) for p in platforms},
    }
    path = out_dir / f"{safe_name(clip.get('title') or clip_id, 50)}_{clip_id}.json"
    path.write_text(json.dumps(package, indent=2, ensure_ascii=False), encoding="utf-8")
    copied = copy_to_clipboard(package["per_platform"][platforms[0]]["caption_with_tags"]) if platforms else ""
    package["package_path"] = str(path)
    package["clipboard"] = copied
    return package


# ------------------------------------------------------------------- YouTube
def _youtube_access_token() -> str:
    if not (settings.youtube_client_id and settings.youtube_client_secret
            and settings.youtube_refresh_token):
        raise RuntimeError("YouTube upload is not configured (client id/secret/refresh token)")
    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": settings.youtube_client_id,
            "client_secret": settings.youtube_client_secret,
            "refresh_token": settings.youtube_refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    data = resp.json() if resp.content else {}
    if resp.status_code >= 400 or not data.get("access_token"):
        raise RuntimeError(f"YouTube token refresh failed: {data.get('error_description') or resp.status_code}")
    return str(data["access_token"])


def publish_youtube(clip: dict[str, Any], copy: dict[str, Any]) -> dict[str, Any]:
    video, _ = _clip_paths(clip)
    if not video:
        raise FileNotFoundError("YouTube needs the burned-caption MP4")
    token = _youtube_access_token()
    body = {
        "snippet": {
            "title": copy["title"][:100],
            "description": f"{copy['caption_with_tags']}\n\n{settings.public_base_url}".strip(),
            "tags": copy["hashtags"],
            "categoryId": "20",
        },
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }
    init = requests.post(
        "https://www.googleapis.com/upload/youtube/v3/videos",
        params={"uploadType": "resumable", "part": "snippet,status"},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "video/mp4",
            "X-Upload-Content-Length": str(video.stat().st_size),
        },
        json=body, timeout=60,
    )
    if init.status_code >= 400:
        raise RuntimeError(f"YouTube upload init failed: {init.text[:200]}")
    upload_url = init.headers.get("Location")
    if not upload_url:
        raise RuntimeError("YouTube did not return an upload URL")
    mime = mimetypes.guess_type(video.name)[0] or "video/mp4"
    with video.open("rb") as fh:
        upload = requests.put(upload_url, data=fh, headers={"Content-Type": mime}, timeout=900)
    data = upload.json() if upload.content else {}
    if upload.status_code >= 400 or not data.get("id"):
        raise RuntimeError(f"YouTube upload failed: {str(data)[:200]}")
    return {"ok": True, "id": data["id"], "url": f"https://youtu.be/{data['id']}"}


# -------------------------------------------------------------------- TikTok
def publish_tiktok(clip: dict[str, Any], copy: dict[str, Any]) -> dict[str, Any]:
    token = settings.tiktok_access_token
    if not token:
        raise RuntimeError("TikTok is not configured (TIKTOK_ACCESS_TOKEN missing)")
    video, _ = _clip_paths(clip)
    if not video:
        raise FileNotFoundError("TikTok needs the burned-caption MP4")
    size = video.stat().st_size
    chunk = min(size, 10 * 1024 * 1024)
    chunks = max(1, (size + chunk - 1) // chunk)
    init = requests.post(
        "https://open.tiktokapis.com/v2/post/publish/video/init/",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "post_info": {
                "title": copy["caption_with_tags"][:150],
                "privacy_level": "SELF_ONLY",
                "disable_duet": False,
                "disable_comment": False,
                "disable_stitch": False,
            },
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": size,
                "chunk_size": chunk,
                "total_chunk_count": chunks,
            },
        },
        timeout=60,
    )
    data = init.json() if init.content else {}
    publish_id = (data.get("data") or {}).get("publish_id")
    upload_url = (data.get("data") or {}).get("upload_url")
    if init.status_code >= 400 or not publish_id or not upload_url:
        raise RuntimeError(f"TikTok init failed: {str(data)[:240]}")
    with video.open("rb") as fh:
        for idx in range(chunks):
            part = fh.read(chunk)
            start = idx * chunk
            end = start + len(part) - 1
            resp = requests.put(
                upload_url, data=part,
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(part)),
                    "Content-Range": f"bytes {start}-{end}/{size}",
                },
                timeout=300,
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"TikTok chunk {idx + 1} failed: {resp.text[:180]}")
    status = "PROCESSING_UPLOAD"
    for _ in range(30):
        time.sleep(4)
        poll = requests.post(
            "https://open.tiktokapis.com/v2/post/publish/status/fetch/",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"publish_id": publish_id}, timeout=30,
        ).json()
        status = (poll.get("data") or {}).get("status", status)
        if status in {"PUBLISH_COMPLETE", "FAILED"}:
            break
    return {"ok": status == "PUBLISH_COMPLETE", "publish_id": publish_id, "status": status}


# ----------------------------------------------------------------- Instagram
def publish_instagram(clip: dict[str, Any], copy: dict[str, Any]) -> dict[str, Any]:
    token = settings.instagram_access_token
    user_id = settings.instagram_user_id
    if not (token and user_id):
        raise RuntimeError("Instagram is not configured (INSTAGRAM_ACCESS_TOKEN / INSTAGRAM_USER_ID)")
    public = _public_url(clip)
    if not public:
        raise RuntimeError(
            "Instagram needs a public video URL. Set PUBLIC_BASE_URL or use the manual package."
        )
    create = requests.post(
        f"https://graph.facebook.com/v21.0/{user_id}/media",
        data={
            "media_type": "REELS", "video_url": public,
            "caption": copy["caption_with_tags"], "access_token": token,
        },
        timeout=60,
    )
    data = create.json() if create.content else {}
    container = data.get("id")
    if create.status_code >= 400 or not container:
        raise RuntimeError(f"Instagram container failed: {str(data)[:240]}")
    ready = False
    for _ in range(30):
        time.sleep(5)
        status = requests.get(
            f"https://graph.facebook.com/v21.0/{container}",
            params={"fields": "status_code", "access_token": token}, timeout=30,
        ).json()
        if status.get("status_code") == "FINISHED":
            ready = True
            break
        if status.get("status_code") == "ERROR":
            break
    if not ready:
        raise RuntimeError("Instagram could not process the video in time")
    pub = requests.post(
        f"https://graph.facebook.com/v21.0/{user_id}/media_publish",
        data={"creation_id": container, "access_token": token}, timeout=60,
    )
    result = pub.json() if pub.content else {}
    if pub.status_code >= 400:
        raise RuntimeError(f"Instagram publish failed: {str(result)[:220]}")
    return {"ok": True, "id": result.get("id", "")}


POSTERS = {
    "youtube": publish_youtube,
    "tiktok": publish_tiktok,
    "instagram": publish_instagram,
}


# --------------------------------------------------------------------- entry
def publish(clip_id: str, platforms: list[str] | None = None) -> dict[str, Any]:
    state = get_state()
    clip = state.get_clip(clip_id)
    if not clip:
        return {"ok": False, "error": "Clip not found", "results": {}}
    platforms = [p for p in (platforms or list(PLATFORMS)) if p in PLATFORMS]
    package = build_package(clip_id, platforms)
    results: dict[str, Any] = {}
    posted: dict[str, Any] = dict(clip.get("posted") or {})
    for platform in platforms:
        copy = package["per_platform"][platform]
        try:
            result = POSTERS[platform](clip, copy)
            result["posted_at"] = now_iso()
        except (RuntimeError, FileNotFoundError, requests.RequestException) as exc:
            result = {"ok": False, "error": str(exc)[:400], "manual": True}
        results[platform] = result
        if result.get("ok"):
            posted[platform] = {
                "url": result.get("url", ""), "id": result.get("id") or result.get("publish_id", ""),
                "at": now_iso(),
            }
        else:
            state.add_warning(f"{platform.title()} upload not sent: {str(result.get('error'))[:140]}")
    state.update_clip(clip_id, {"posted": posted, "caption": package["per_platform"][platforms[0]]["caption"] if platforms else clip.get("caption", "")})
    ok = any(r.get("ok") for r in results.values()) if results else False
    return {
        "ok": ok,
        "results": results,
        "package": package,
        "manual_note": "" if ok else "Package saved and caption copied. Post it manually or add platform keys.",
    }


def copy_caption(clip_id: str, platform: str = "tiktok") -> tuple[bool, str]:
    clip = get_state().get_clip(clip_id)
    if not clip:
        return False, "Clip not found"
    copy = _copy_for(clip, platform if platform in PLATFORMS else "tiktok")
    return copy_to_clipboard(copy["caption_with_tags"])
