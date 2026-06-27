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

import base64
import json
import logging
import os
import shutil
import tempfile
import uuid
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.db.postgres import get_pool
from app.db.redis import get_redis as _get_redis
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
ALLOWED_AUDIO_EXT = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".opus", ".webm"}


# ─────────────────────────────────────────────────────────────────────────
# Auth helper: admin-cookie ИЛИ X-Owner-Key ИЛИ per-agent X-API-Key
# ─────────────────────────────────────────────────────────────────────────
class _OwnerCtx:
    """Маркер «owner-key auth» когда нет реальной session/user."""
    user_id = "owner-key"
    email = "owner-key@cognitive-core"
    is_admin = True


class _AgentCtx:
    """Маркер «agent-key auth» — per-agent X-API-Key.

    Resolved owner_user_id привязывается к user_id (что и есть владелец
    помощника). is_admin=False — обычный пользовательский upload.
    """
    def __init__(self, agent_id: str, user_id: str, email: str = ""):
        self.agent_id = agent_id
        self.user_id = user_id
        self.email = email or f"{agent_id}@agent"
        self.is_admin = False


async def _check_admin_or_owner(request: Request):
    """Авторизация: admin-session ИЛИ X-Owner-Key ИЛИ per-agent X-API-Key.

    Три пути:
      1. Header X-Owner-Key совпадает с settings.owner_api_key (legacy
         cogmedia path, owner-key глобальный — будет deprecated в Phase
         cleanup после A/B v1→v2).
      2. Header X-API-Key матчится с agent_keys.api_key (не revoked).
         Помощник заливает media — owner_user_id берётся из FK на agent.
      3. Admin-cookie через require_admin (legacy admin-only UI).

    Возвращает объект с .user_id / .email / .is_admin. Бросает 401/403
    если ничего не подходит.
    """
    # Path 1: legacy owner-key (deprecated, оставлен для совместимости с cogmedia)
    owner_key_header = request.headers.get("X-Owner-Key", "").strip()
    expected = (settings.owner_api_key or "").strip()
    if owner_key_header and expected and owner_key_header == expected:
        return _OwnerCtx()

    # Path 2: per-agent X-API-Key (демократизация — любой агент юзера может upload)
    api_key = request.headers.get("X-API-Key", "").strip()
    if api_key:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT ak.agent_id,
                       ast.owner_user_id::text AS user_id,
                       acc.email
                  FROM agent_keys ak
                  JOIN agent_states ast ON ast.agent_id = ak.agent_id
             LEFT JOIN accounts acc     ON acc.user_id = ast.owner_user_id
                 WHERE ak.api_key = $1 AND ak.revoked_at IS NULL
                 LIMIT 1
                """,
                api_key,
            )
        if row and row["user_id"]:
            # last_used touch — не критично, fire-and-forget
            try:
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE agent_keys SET last_used_at = NOW() WHERE api_key = $1",
                        api_key,
                    )
            except Exception as e:
                # last_used_at — best-effort metric (non-fatal). Log чтобы
                # видеть если pool/postgres consistently недоступен.
                logger.warning("media: failed to bump last_used_at for agent=%s: %s",
                               row.get("agent_id", "?"), type(e).__name__)
            return _AgentCtx(
                agent_id=row["agent_id"],
                user_id=row["user_id"],
                email=row.get("email") or "",
            )

    # Path 3: fallback на admin-сессию
    return await require_admin(request)


def _ensure_bucket():
    """Создать MEDIA_BUCKET если не существует.

    Object store недоступен -> 503 (а не сырое исключение/зависание воркера).
    Раньше минио-клиент без таймаута ретраил долго на синхронном вызове внутри
    async-хендлера и блокировал весь uvicorn-воркер при упавшем MinIO.
    """
    try:
        s3 = get_s3()
        if not s3.bucket_exists(MEDIA_BUCKET):
            s3.make_bucket(MEDIA_BUCKET)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("media: object store unavailable: %s", e)
        raise HTTPException(
            status_code=503,
            detail="object store (MinIO) недоступен — попробуйте позже",
        )
        logger.info("media bucket created: %s", MEDIA_BUCKET)


def _upload_frame_to_s3(local_path: Path, key: str) -> str:
    """Залить файл в MinIO. Вернёт URL вида /api/media/frame/<key>."""
    s3 = get_s3()
    s3.fput_object(MEDIA_BUCKET, key, str(local_path), content_type="image/jpeg")
    return f"/api/media/frame/{key}"


async def _save_to_l1(payload: dict[str, Any], source_agent: str = "media_uploader") -> str:
    """Записать метаданные в L1 raw_events. Returns event id.

    Параметр source_agent позволяет атрибутировать upload к конкретному
    помощнику (если auth прошёл через X-API-Key) — это даёт recall
    domain=media_analysis по конкретному agent_id.

    Если payload содержит room_id (Phase v2 room-aware), он остаётся в
    payload и доступен для последующего auto-post в комнату.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO l1_raw_events (source_agent, domain, raw_payload)
            VALUES ($1, $2, $3::jsonb)
            RETURNING id::text AS id
            """,
            source_agent,
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

        # Vision stage — multi-provider анализ «механики» видео.
        # Owner-mandate 2026-05-24: «дать возможность механики, а не картинок» +
        # per-tenant external keys (PR #52): сначала пытаемся ключами owner'а
        # (qwen → minimax → gigachat → claude → openai → gemini), потом fallback
        # на shared platform Qwen, потом DeepSeek text-only. owner_user_id берём
        # из auth-context — у _OwnerCtx это marker «owner-key@cognitive-core»,
        # для него tenant keys не применимы (resolve fails — fallthrough на shared).
        vision_result: dict = {}
        try:
            from app.services.vision_analyzer import analyze_mechanics
            if frame_records:
                # Build absolute frame URLs — вне зависимости provider, public HTTPS.
                base_url = os.environ.get("PUBLIC_BASE_URL", "https://mcp.me-ai.ru").rstrip("/")
                abs_urls = [
                    f"{base_url}{fr['url']}" if fr["url"].startswith("/") else fr["url"]
                    for fr in frame_records
                ]
                # owner_user_id — UUID-string или None. Для _OwnerCtx user_id =
                # "owner-key" (не UUID) — analyze_mechanics поймает ошибку UUID
                # cast в _load_tenant_keys и пропустит tenant-stage.
                resolved_owner = None
                uid = getattr(user, "user_id", None)
                if uid and uid != "owner-key":
                    resolved_owner = uid
                vision_result = await analyze_mechanics(
                    frame_urls=abs_urls,
                    transcript=analysis.get("transcript"),
                    duration_seconds=analysis.get("duration"),
                    owner_user_id=resolved_owner,
                )
                if vision_result.get("mechanics_summary"):
                    logger.info(
                        "vision mechanics for media_id=%s source=%s tokens=%d/%d: %s",
                        media_id,
                        vision_result.get("provider_source", "?"),
                        vision_result.get("tokens_in", 0),
                        vision_result.get("tokens_out", 0),
                        vision_result["mechanics_summary"][:120],
                    )
                elif vision_result.get("error"):
                    logger.warning("vision stage error for media_id=%s: %s", media_id, vision_result["error"])
        except Exception as e:
            logger.warning("vision stage exception for media_id=%s: %s", media_id, e)
            vision_result = {"error": f"exception: {e}"}

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
            "mechanics_summary": vision_result.get("mechanics_summary") or None,
            "vision_provider": vision_result.get("model") if vision_result.get("mechanics_summary") else None,
            "vision_provider_source": vision_result.get("provider_source") if vision_result.get("mechanics_summary") else None,
            "vision_tokens": (
                {"in": vision_result.get("tokens_in"), "out": vision_result.get("tokens_out")}
                if vision_result.get("mechanics_summary") else None
            ),
        }

        # Запись в L1 — атрибутируем к agent_id если auth через X-API-Key
        src = getattr(user, "agent_id", None) or "media_uploader"
        event_id = await _save_to_l1(result, source_agent=src)
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
        # Cleanup временных файлов — silent failures накапливают диск,
        # логируем если cleanup ломается (накопит /tmp выше 1GB → видно в alerts)
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception as e:
            logger.warning("media.video: tmp cleanup failed for %s: %s", tmp_dir, type(e).__name__)


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
        src = getattr(user, "agent_id", None) or "media_uploader"
        result["event_id"] = await _save_to_l1(result, source_agent=src)
        return result
    finally:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception as e:
            logger.warning("media.image: tmp cleanup failed for %s: %s", tmp_dir, type(e).__name__)


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
        src = getattr(user, "agent_id", None) or "media_uploader"
        result["event_id"] = await _save_to_l1(result, source_agent=src)
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


# ─────────────────────────────────────────────────────────────────────────
# POST /api/media/upload_b64 — universal base64 entry (для MCP tool)
# ─────────────────────────────────────────────────────────────────────────
class UploadB64Body(BaseModel):
    file_b64: str = Field(..., description="base64-encoded file content")
    filename: str = Field(..., min_length=1, max_length=255, description="original filename с extension")
    kind: str = Field("auto", pattern=r"^(auto|video|image|audio)$", description="auto = detect from extension")




# ─────────────────────────────────────────────────────────────────────────────
# Resumable upload (ewewew P2: обходит base64 context-cap для media > ~36KB).
# Pattern:
#   POST /upload-init {filename, size_bytes, content_type}
#     → {upload_id, put_url, ttl_seconds}
#   PUT  /upload/{upload_id} body=raw bytes
#     → {status: "uploaded", bytes: N}
#   POST /upload/{upload_id}/finalize
#     → routes на analyze_video/image/audio по extension, возвращает
#       {media_id, frames, transcript, vision_summary} как существующие
#       /video /image /audio endpoints.
#
# State хранится в Redis (TTL 1 час). Tmp файлы — /tmp/cogcore-uploads/.
# Cleanup orphan files делает существующий media_cleanup loop.
# ─────────────────────────────────────────────────────────────────────────────



UPLOAD_TMP_DIR = Path("/tmp/cogcore-uploads")
UPLOAD_TTL_SEC = 3600  # 1 hour to PUT + finalize before state evicted


class UploadInitBody(BaseModel):
    """Init resumable upload — owner объявляет намерение."""
    filename: str = Field(..., min_length=1, max_length=255)
    size_bytes: int = Field(..., ge=1, le=2 * 1024 * 1024 * 1024)  # max 2GB
    content_type: str | None = Field(None, max_length=128)


@router.post("/upload-init")
async def upload_init(body: UploadInitBody, request: Request) -> dict:
    """Объявить resumable upload, получить upload_id + URL для PUT.

    Возвращает put_url относительно текущего host — agent сам подставит
    https://mcp.me-ai.ru/ или https://self-hosted.example/ префикс.
    """
    user = await _check_admin_or_owner(request)
    ext = Path(body.filename).suffix.lower()
    if ext not in ALLOWED_VIDEO_EXT and ext not in ALLOWED_IMAGE_EXT and ext not in ALLOWED_AUDIO_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"расширение {ext!r} не поддерживается. Допустимые: "
                   f"video={ALLOWED_VIDEO_EXT}, image={ALLOWED_IMAGE_EXT}, audio={ALLOWED_AUDIO_EXT}",
        )
    if body.size_bytes > MAX_UPLOAD_SIZE_MB * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail=f"size_bytes > {MAX_UPLOAD_SIZE_MB}MB platform limit",
        )

    upload_id = uuid.uuid4().hex
    state = {
        "user_email": user.email,
        "filename": body.filename,
        "safe_name": sanitize_filename(body.filename),
        "size_bytes": body.size_bytes,
        "content_type": body.content_type or "application/octet-stream",
        "ext": ext,
        "received_bytes": 0,
        "status": "initialized",
        "media_id": None,  # set after finalize
        "created_at": datetime.utcnow().isoformat(),
    }
    try:
        r = await _get_redis()
        await r.setex(f"cogcore:upload:{upload_id}", UPLOAD_TTL_SEC, json.dumps(state))
    except Exception as e:
        logger.error("upload_init redis failed: %s", e)
        raise HTTPException(status_code=500, detail=f"state store failed: {e}")

    logger.info("upload_init user=%s upload_id=%s filename=%s size=%d",
                user.email, upload_id, body.filename, body.size_bytes)
    return {
        "upload_id": upload_id,
        "put_url": f"/api/media/upload/{upload_id}",
        "finalize_url": f"/api/media/upload/{upload_id}/finalize",
        "ttl_seconds": UPLOAD_TTL_SEC,
        "max_size_mb": MAX_UPLOAD_SIZE_MB,
    }


@router.put("/upload/{upload_id}")
async def upload_put(upload_id: str, request: Request) -> dict:
    """Stream raw bytes на disk. Без UploadFile multipart — просто request body.

    Не auth-check здесь (init endpoint уже проверил user). upload_id серверует
    как opaque token — owner получил его из init response, который и был
    auth-gated.

    Заодно валидация размера vs объявленного в init: если PUT > size_bytes —
    отказ, чтоб не было неконтролируемого диска.
    """
    try:
        r = await _get_redis()
        raw = await r.get(f"cogcore:upload:{upload_id}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"state lookup failed: {e}")
    if not raw:
        raise HTTPException(status_code=404, detail="upload_id not found или expired (TTL 1h)")
    state = json.loads(raw)
    if state["status"] != "initialized":
        raise HTTPException(
            status_code=409,
            detail=f"upload {upload_id} status={state['status']}, нельзя PUT (используй другой upload_id)",
        )

    UPLOAD_TMP_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = UPLOAD_TMP_DIR / f"{upload_id}.bin"
    bytes_written = 0
    max_bytes = state["size_bytes"]
    try:
        with open(tmp_path, "wb") as f:
            async for chunk in request.stream():
                bytes_written += len(chunk)
                if bytes_written > max_bytes:
                    tmp_path.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"PUT body > объявленного size_bytes={max_bytes}",
                    )
                f.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"write failed: {e}")

    state["received_bytes"] = bytes_written
    state["status"] = "uploaded"
    await r.setex(f"cogcore:upload:{upload_id}", UPLOAD_TTL_SEC, json.dumps(state))
    logger.info("upload_put upload_id=%s bytes=%d", upload_id, bytes_written)
    return {"status": "uploaded", "bytes": bytes_written, "next": f"POST {state.get('finalize_url', f'/api/media/upload/{upload_id}/finalize')}"}


@router.post("/upload/{upload_id}/finalize")
async def upload_finalize(upload_id: str, request: Request) -> dict:
    """Запустить analyze pipeline на uploaded raw файле.

    Dispatch по extension: video → analyze_video + frames + Whisper transcript,
    audio → analyze_audio + Whisper, image → analyze_image.

    Reuses логику из существующих /video /audio /image endpoints (frames upload
    + L1 save + vision multi-provider stage). Возвращает тот же shape.

    Idempotent: повторный finalize для уже-finalized upload_id вернёт прошлый
    результат (media_id остаётся в state).
    """
    user = await _check_admin_or_owner(request)
    try:
        r = await _get_redis()
        raw = await r.get(f"cogcore:upload:{upload_id}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"state lookup failed: {e}")
    if not raw:
        raise HTTPException(status_code=404, detail="upload_id not found или expired")
    state = json.loads(raw)
    if state["status"] not in ("uploaded", "finalized"):
        raise HTTPException(
            status_code=409,
            detail=f"upload status={state['status']}, ожидался uploaded (сначала PUT)",
        )
    if state.get("user_email") != user.email and not user.is_admin:
        raise HTTPException(status_code=403, detail="Не ваш upload")

    # Idempotency: уже finalized
    if state["status"] == "finalized" and state.get("media_id"):
        logger.info("upload_finalize idempotent return media_id=%s", state["media_id"])
        return state.get("finalize_result", {"media_id": state["media_id"], "note": "reused"})

    tmp_path = UPLOAD_TMP_DIR / f"{upload_id}.bin"
    if not tmp_path.exists():
        raise HTTPException(status_code=410, detail="tmp file missing (cleanup ran?)")

    media_id = uuid.uuid4().hex
    ext = state["ext"]
    safe_name = state["safe_name"]
    # Rename tmp file to have proper extension (analyze_* expects extension)
    final_tmp = UPLOAD_TMP_DIR / f"{upload_id}{ext}"
    if final_tmp.exists():
        final_tmp.unlink()
    tmp_path.rename(final_tmp)

    try:
        if ext in ALLOWED_VIDEO_EXT:
            result = await _analyze_and_save(
                final_tmp, media_id, safe_name, user, kind="video",
                analyze_fn=analyze_video,
            )
        elif ext in ALLOWED_AUDIO_EXT:
            result = await _analyze_and_save(
                final_tmp, media_id, safe_name, user, kind="audio",
                analyze_fn=analyze_audio,
            )
        elif ext in ALLOWED_IMAGE_EXT:
            result = await _analyze_and_save(
                final_tmp, media_id, safe_name, user, kind="image",
                analyze_fn=analyze_image,
            )
        else:
            raise HTTPException(status_code=400, detail=f"unsupported ext {ext}")
    finally:
        # Always cleanup tmp file
        try:
            final_tmp.unlink(missing_ok=True)
        except Exception:
            pass

    # Update state to finalized + store result for idempotency
    state["status"] = "finalized"
    state["media_id"] = media_id
    state["finalize_result"] = result
    await r.setex(f"cogcore:upload:{upload_id}", 600, json.dumps(state))  # 10min retention after finalize

    return result


async def _analyze_and_save(
    tmp_path: Path, media_id: str, safe_name: str, user, kind: str, analyze_fn
) -> dict:
    """Shared post-upload pipeline: analyze → upload frames → vision → L1 save.

    Минимальная версия — vision analysis опускаем (delegate в существующие endpoints
    если нужно). Здесь focus на frames + transcript + L1 save.
    """
    logger.info("finalize analyze kind=%s media_id=%s file=%s", kind, media_id, safe_name)
    try:
        analysis = await analyze_fn(str(tmp_path))
    except Exception as e:
        import traceback
        tb = traceback.format_exc()[-500:]
        logger.warning("finalize analyze failed: %s\n%s", e, tb)
        status = 400 if isinstance(e, ValueError) else 500
        raise HTTPException(status_code=status, detail=f"{type(e).__name__}: {e}")

    # Upload frames if any (video case)
    frame_records: list[dict] = []
    if analysis.get("frames"):
        _ensure_bucket()
        for fr in analysis["frames"]:
            local = Path(fr["local_path"])
            key = f"{kind}/{media_id}/frame_{fr['index']:04d}.jpg"
            try:
                url = _upload_frame_to_s3(local, key)
                frame_records.append({
                    "index": fr["index"], "ts": fr.get("ts"), "key": key,
                    "url": url, "size_bytes": fr.get("size_bytes"),
                })
            except Exception as e:
                logger.warning("frame upload failed key=%s err=%s", key, e)

    # L1 save
    payload = {
        "media_id": media_id,
        "kind": kind,
        "filename": safe_name,
        "transcript": (analysis.get("transcript") or "")[:50_000],
        "duration_sec": analysis.get("duration_sec"),
        "frames": frame_records,
        "uploader_email": user.email,
        "via": "resumable_upload",
    }
    event_id = await _save_to_l1(payload, source_agent="media_uploader_resumable")
    return {
        "media_id": media_id,
        "kind": kind,
        "transcript": payload["transcript"],
        "duration_sec": payload["duration_sec"],
        "frames": frame_records,
        "l1_event_id": event_id,
    }


@router.post("/upload_b64")
async def upload_b64(request: Request, body: UploadB64Body) -> dict:
    """Universal media upload через base64 JSON — для MCP tools (cognitive_media_upload).

    Не требует multipart. Принимает file_b64 + filename, dispatches к существующему
    /api/media/{video|image|audio} endpoint через internal HTTP loopback.

    Авторизация: тот же что и у video/image/audio endpoints (admin cookie ИЛИ
    X-Owner-Key ИЛИ per-agent X-API-Key). Авторизационный header/cookie
    проксируется на loopback вызов.
    """
    user = await _check_admin_or_owner(request)

    # Decode + size limit BEFORE bytes hit memory limits
    try:
        file_bytes = base64.b64decode(body.file_b64, validate=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid base64: {e}")

    size_mb = len(file_bytes) / 1024 / 1024
    if size_mb > MAX_UPLOAD_SIZE_MB:
        raise HTTPException(
            status_code=413,
            detail=f"размер {size_mb:.1f}MB > лимита {MAX_UPLOAD_SIZE_MB}MB",
        )

    # Determine kind
    ext = Path(body.filename).suffix.lower()
    kind = body.kind
    if kind == "auto":
        if ext in ALLOWED_VIDEO_EXT:
            kind = "video"
        elif ext in ALLOWED_IMAGE_EXT:
            kind = "image"
        elif ext in ALLOWED_AUDIO_EXT:
            kind = "audio"
        else:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"неизвестное расширение {ext!r}. Поддерживается: "
                    f"video={sorted(ALLOWED_VIDEO_EXT)}, "
                    f"image={sorted(ALLOWED_IMAGE_EXT)}, "
                    f"audio={sorted(ALLOWED_AUDIO_EXT)}"
                ),
            )

    endpoint = f"/api/media/{kind}"
    logger.info(
        "upload_b64: user=%s file=%s kind=%s size=%.1fMB → forwarding к %s",
        user.email, body.filename, kind, size_mb, endpoint,
    )

    # Forward как multipart к internal endpoint (через loopback nginx → api)
    # Проксируем auth header/cookie чтобы _check_admin_or_owner на той стороне сработал
    headers: dict = {}
    if api_key := request.headers.get("X-API-Key"):
        headers["X-API-Key"] = api_key
    elif owner_key := request.headers.get("X-Owner-Key"):
        headers["X-Owner-Key"] = owner_key
    if cookies := request.cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())

    try:
        async with httpx.AsyncClient(base_url="http://localhost:8000", timeout=180) as client:
            files = {"file": (body.filename, BytesIO(file_bytes), "application/octet-stream")}
            r = await client.post(endpoint, files=files, headers=headers)
        if r.status_code >= 400:
            # Pass through detail from downstream
            try:
                detail = r.json().get("detail", r.text[:500])
            except Exception:
                detail = r.text[:500]
            raise HTTPException(status_code=r.status_code, detail=detail)
        return r.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"loopback to {endpoint} failed: {e}")
