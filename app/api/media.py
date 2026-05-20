"""Media upload + analyze endpoints.

  POST /api/media/video      — upload видео, извлечь фреймы + транскрибировать
                                + сохранить в MinIO + L1 events. Admin only.
  POST /api/media/image      — upload картинки, нормализовать, сохранить. Admin only.
  GET  /api/media/list       — список последних загрузок. Admin only.
  GET  /api/media/frame/{key} — отдать фрейм из MinIO. Anonymous (public CDN-like).

Storage:
  • MinIO bucket `media-frames` (создаётся автоматически)
  • Метаданные в L1 raw_events (domain=media_analysis)
  • Я смогу читать через cognitive_recall domain:media_analysis
"""
from __future__ import annotations

import io
import json
import logging
import os
import shutil
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from app.config import settings
from app.db.postgres import get_pool
from app.db.s3 import get_s3
from app.security.middleware import require_admin
from app.services.media_analyzer import (
    analyze_audio,
    analyze_image,
    analyze_video,
    sanitize_filename,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/media", tags=["media"])

# Constants
MEDIA_BUCKET = "media-frames"
MAX_UPLOAD_SIZE_MB = 200  # nginx body_size должен быть >= этого
MAX_AUDIO_SIZE_MB = 50    # аудио — обычно небольшие файлы
ALLOWED_VIDEO_EXT = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v"}
ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
ALLOWED_AUDIO_EXT = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".opus"}


# ─────────────────────────────────────────────────────────────────────────
# Auth helper: admin-cookie ИЛИ X-Owner-Key header
# ─────────────────────────────────────────────────────────────────────────
class _OwnerCtx:
    """Маркер «owner-key auth» когда нет реальной session/user."""
    user_id = "owner-key"
    email = "owner-key@cognitive-core"
    is_admin = True


async def _check_admin_or_owner(request: Request):
    """Авторизация: либо валидная admin-session, либо корректный X-Owner-Key.

    Возвращает объект с .email / .user_id / .is_admin = True.
    Бросает 401/403 если ни то ни другое.

    Owner-key path нужен для cogmedia (Bash curl с лэптопа без сессии).
    """
    owner_key_header = request.headers.get("X-Owner-Key", "").strip()
    expected = (settings.owner_api_key or "").strip()
    if owner_key_header and expected and owner_key_header == expected:
        return _OwnerCtx()

    # Fallback на стандартную admin-сессию
    return await require_admin(request)


def _ensure_bucket():
    """Создать MEDIA_BUCKET если не существует."""
    s3 = get_s3()
    if not s3.bucket_exists(MEDIA_BUCKET):
        s3.make_bucket(MEDIA_BUCKET)
        logger.info("media bucket created: %s", MEDIA_BUCKET)


def _upload_frame_to_s3(local_path: Path, key: str) -> str:
    """Залить файл в MinIO. Вернёт URL вида /api/media/frame/<key>."""
    s3 = get_s3()
    s3.fput_object(MEDIA_BUCKET, key, str(local_path), content_type="image/jpeg")
    return f"/api/media/frame/{key}"


async def _save_to_l1(payload: dict[str, Any]) -> str:
    """Записать метаданные в L1 raw_events. Returns event id."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO l1_raw_events (source_agent, domain, raw_payload)
            VALUES ($1, $2, $3::jsonb)
            RETURNING id::text AS id
            """,
            "media_uploader",
            "media_analysis",
            json.dumps(payload, ensure_ascii=False, default=str),
        )
    return row["id"]


# ─────────────────────────────────────────────────────────────────────────
# POST /api/media/video
# ─────────────────────────────────────────────────────────────────────────
@router.post("/video")
async def upload_video(request: Request, file: UploadFile = File(...)):
    user = await _check_admin_or_owner(request)

    # Валидация
    if not file.filename:
        raise HTTPException(status_code=400, detail="filename отсутствует")
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_VIDEO_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"тип файла не поддерживается. Допустимые: {', '.join(ALLOWED_VIDEO_EXT)}",
        )

    media_id = uuid.uuid4().hex
    safe_name = sanitize_filename(file.filename)
    logger.info("video upload start user=%s file=%s media_id=%s", user.email, safe_name, media_id)

    # Save to temp file
    tmp_dir = tempfile.mkdtemp(prefix=f"upload_{media_id}_")
    tmp_path = os.path.join(tmp_dir, safe_name)
    bytes_written = 0
    try:
        with open(tmp_path, "wb") as f:
            while chunk := await file.read(1024 * 1024):  # 1MB chunks
                bytes_written += len(chunk)
                if bytes_written > MAX_UPLOAD_SIZE_MB * 1024 * 1024:
                    raise HTTPException(
                        status_code=413,
                        detail=f"видео > {MAX_UPLOAD_SIZE_MB}MB",
                    )
                f.write(chunk)
        await file.close()

        logger.info("video uploaded %d bytes → %s", bytes_written, tmp_path)

        # Запустить анализ (фреймы + транскрипция)
        try:
            analysis = await analyze_video(tmp_path)
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            logger.warning("analyze_video failed: %s\n%s", e, tb)
            # Если это ValueError из MAX_VIDEO_DURATION_SEC — 400, иначе 500
            status = 400 if isinstance(e, ValueError) and "слишком длинн" in str(e) else 500
            # Возвращаем последние 600 chars traceback чтобы было видно
            tb_tail = tb[-600:] if len(tb) > 600 else tb
            raise HTTPException(
                status_code=status,
                detail=f"{type(e).__name__}: {str(e)[:200]}\n\nTRACE:\n{tb_tail}",
            )

        # Загрузить фреймы в MinIO
        _ensure_bucket()
        frame_records: list[dict] = []
        for fr in analysis.get("frames", []):
            local = Path(fr["local_path"])
            key = f"video/{media_id}/frame_{fr['index']:04d}.jpg"
            try:
                url = _upload_frame_to_s3(local, key)
                frame_records.append({
                    "index": fr["index"],
                    "ts": fr["ts"],
                    "key": key,
                    "url": url,
                    "size_bytes": fr["size_bytes"],
                })
            except Exception as e:
                logger.warning("frame upload failed key=%s err=%s", key, e)

        # Payload для L1 и ответа
        result = {
            "media_id": media_id,
            "kind": "video",
            "filename": safe_name,
            "uploaded_by": user.email,
            "user_id": user.user_id,
            "uploaded_at": datetime.utcnow().isoformat(),
            "size_bytes": bytes_written,
            "duration_sec": analysis.get("duration"),
            "has_audio": analysis.get("has_audio"),
            "transcript": analysis.get("transcript"),
            "language": analysis.get("language"),
            "transcript_duration_ms": analysis.get("transcript_duration_ms"),
            "frames": frame_records,
            "frames_count": len(frame_records),
        }

        # Запись в L1
        event_id = await _save_to_l1(result)
        result["event_id"] = event_id

        logger.info(
            "video analyzed media_id=%s duration=%.1fs frames=%d transcript_chars=%d",
            media_id,
            analysis.get("duration") or 0,
            len(frame_records),
            len(analysis.get("transcript") or ""),
        )
        return result

    finally:
        # Cleanup временных файлов
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────
# POST /api/media/image
# ─────────────────────────────────────────────────────────────────────────
@router.post("/image")
async def upload_image(request: Request, file: UploadFile = File(...)):
    user = await _check_admin_or_owner(request)
    if not file.filename:
        raise HTTPException(status_code=400, detail="filename отсутствует")
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_IMAGE_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"тип файла не поддерживается. Допустимые: {', '.join(ALLOWED_IMAGE_EXT)}",
        )

    media_id = uuid.uuid4().hex
    safe_name = sanitize_filename(file.filename)
    tmp_dir = tempfile.mkdtemp(prefix=f"upload_img_{media_id}_")
    tmp_path = os.path.join(tmp_dir, safe_name)
    bytes_written = 0
    try:
        with open(tmp_path, "wb") as f:
            while chunk := await file.read(1024 * 1024):
                bytes_written += len(chunk)
                if bytes_written > 25 * 1024 * 1024:  # 25MB cap для картинок
                    raise HTTPException(413, detail="изображение > 25MB")
                f.write(chunk)
        await file.close()

        try:
            analysis = await analyze_image(tmp_path)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"картинка повреждена: {e}")

        _ensure_bucket()
        norm_path = Path(analysis["normalized_path"])
        key = f"image/{media_id}/{norm_path.name}"
        s3 = get_s3()
        s3.fput_object(MEDIA_BUCKET, key, str(norm_path),
                       content_type=f"image/{(analysis.get('format') or 'jpeg').lower()}")
        url = f"/api/media/frame/{key}"

        result = {
            "media_id": media_id,
            "kind": "image",
            "filename": safe_name,
            "uploaded_by": user.email,
            "user_id": user.user_id,
            "uploaded_at": datetime.utcnow().isoformat(),
            "size_bytes": bytes_written,
            "width": analysis["width"],
            "height": analysis["height"],
            "format": analysis["format"],
            "key": key,
            "url": url,
        }
        result["event_id"] = await _save_to_l1(result)
        return result
    finally:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────
# POST /api/media/audio — только транскрипция (Whisper, без фреймов)
# ─────────────────────────────────────────────────────────────────────────
@router.post("/audio")
async def upload_audio(request: Request, file: UploadFile = File(...)):
    user = await _check_admin_or_owner(request)

    if not file.filename:
        raise HTTPException(status_code=400, detail="filename отсутствует")
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_AUDIO_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"тип файла не поддерживается. Допустимые: {', '.join(ALLOWED_AUDIO_EXT)}",
        )

    media_id = uuid.uuid4().hex
    safe_name = sanitize_filename(file.filename)
    logger.info("audio upload start user=%s file=%s media_id=%s",
                user.email, safe_name, media_id)

    tmp_dir = tempfile.mkdtemp(prefix=f"upload_audio_{media_id}_")
    tmp_path = os.path.join(tmp_dir, safe_name)
    bytes_written = 0
    try:
        with open(tmp_path, "wb") as f:
            while chunk := await file.read(1024 * 1024):
                bytes_written += len(chunk)
                if bytes_written > MAX_AUDIO_SIZE_MB * 1024 * 1024:
                    raise HTTPException(
                        status_code=413,
                        detail=f"аудио > {MAX_AUDIO_SIZE_MB}MB",
                    )
                f.write(chunk)
        await file.close()

        try:
            analysis = await analyze_audio(tmp_path)
        except Exception as e:
            logger.exception("analyze_audio failed")
            raise HTTPException(status_code=500,
                                detail=f"анализ упал: {type(e).__name__}: {str(e)[:200]}")

        result = {
            "media_id": media_id,
            "kind": "audio",
            "filename": safe_name,
            "uploaded_by": user.email,
            "user_id": user.user_id,
            "uploaded_at": datetime.utcnow().isoformat(),
            "size_bytes": bytes_written,
            "duration_sec": analysis.get("duration_sec"),
            "transcript": analysis.get("transcript"),
            "language": analysis.get("language"),
            "transcript_duration_ms": analysis.get("transcript_duration_ms"),
        }
        result["event_id"] = await _save_to_l1(result)
        logger.info(
            "audio analyzed media_id=%s duration=%.1fs lang=%s chars=%d",
            media_id,
            result.get("duration_sec") or 0,
            result.get("language") or "?",
            len(result.get("transcript") or ""),
        )
        return result
    finally:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────
# GET /api/media/frame/{key} — отдать фрейм из MinIO
# ─────────────────────────────────────────────────────────────────────────
@router.get("/frame/{key:path}")
async def get_frame(key: str):
    """Отдать фрейм/картинку из MinIO. Anonymous — публичный CDN-like.

    Security: key должен быть в нашем формате (video/<media_id>/frame_NN.jpg
    или image/<media_id>/<name>). Никаких .. / абсолютных путей.
    """
    if ".." in key or key.startswith("/") or len(key) > 200:
        raise HTTPException(status_code=400, detail="плохой ключ")

    s3 = get_s3()
    try:
        obj = s3.get_object(MEDIA_BUCKET, key)
    except Exception as e:
        logger.info("frame fetch failed key=%s err=%s", key, e)
        raise HTTPException(status_code=404, detail="фрейм не найден")

    # Определить content-type из расширения
    ext = key.rsplit(".", 1)[-1].lower() if "." in key else "jpg"
    ctype = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png", "webp": "image/webp", "gif": "image/gif",
    }.get(ext, "application/octet-stream")

    def _stream():
        try:
            while True:
                chunk = obj.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            obj.close()
            obj.release_conn()

    return StreamingResponse(
        _stream(),
        media_type=ctype,
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ─────────────────────────────────────────────────────────────────────────
# GET /api/media/info/{media_id} — публичный info для Claude (без auth)
# ─────────────────────────────────────────────────────────────────────────
@router.get("/info/{media_id}")
async def get_media_info(media_id: str):
    """Публичный endpoint — отдаёт метаданные + URL'ы кадров для media_id.

    Не требует auth — это специальная точка для того чтобы пользователь мог
    дать прямую ссылку Claude'у. Sensitive поля (user_email, IP) убираются.
    """
    if not media_id or not media_id.replace("-", "").isalnum() or len(media_id) > 64:
        raise HTTPException(400, "bad media_id")

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT timestamp, raw_payload
              FROM l1_raw_events
             WHERE domain = 'media_analysis'
               AND raw_payload->>'media_id' = $1
             ORDER BY timestamp DESC LIMIT 1
            """,
            media_id,
        )
    if not row:
        raise HTTPException(404, "не найдено")

    p = dict(row["raw_payload"]) if isinstance(row["raw_payload"], dict) else json.loads(row["raw_payload"])
    # Срезаем sensitive поля (IP, email)
    for k in ("user_id", "user_email", "uploaded_by", "ip"):
        p.pop(k, None)
    return {
        "media_id": p.get("media_id"),
        "kind": p.get("kind"),
        "filename": p.get("filename"),
        "uploaded_at": p.get("uploaded_at"),
        "duration_sec": p.get("duration_sec"),
        "size_bytes": p.get("size_bytes"),
        "has_audio": p.get("has_audio"),
        "language": p.get("language"),
        "transcript": p.get("transcript"),
        "frames_count": p.get("frames_count"),
        "frames": [
            {"index": f.get("index"), "ts": f.get("ts"),
             "url": f.get("url")}
            for f in (p.get("frames") or [])
        ],
        "width": p.get("width"),
        "height": p.get("height"),
        "format": p.get("format"),
        "url": p.get("url"),  # для image
    }


# ─────────────────────────────────────────────────────────────────────────
# GET /api/media/list — последние загрузки (admin)
# ─────────────────────────────────────────────────────────────────────────
@router.get("/list")
async def list_media(request: Request, limit: int = 50):
    user = await require_admin(request)
    _ = user
    if limit < 1 or limit > 200:
        raise HTTPException(400, "limit 1..200")

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id::text AS id, timestamp, raw_payload
              FROM l1_raw_events
             WHERE domain = 'media_analysis'
             ORDER BY timestamp DESC
             LIMIT $1
            """,
            limit,
        )
    items = []
    for r in rows:
        d = dict(r)
        if d.get("timestamp"):
            d["timestamp"] = d["timestamp"].isoformat()
        items.append(d)
    return {"count": len(items), "items": items}
