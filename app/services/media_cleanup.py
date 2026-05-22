"""Background task: удаляет media-файлы из MinIO старше TTL.

Owner-decision (2026-05-22): «хранение 15 мин, для анализа разного типа
и повторного обращения, вдруг обсуждение по видео будет».

Логика:
- TTL = 15 минут от momentum upload (timestamp в L1 raw_events)
- Каждые 5 минут scan: ищем media-events где (now - timestamp) > 15 мин
  AND raw_payload.cleaned_up != True
- Удаляем из MinIO bucket media-frames всё под префиксом
  {kind}/{media_id}/ (video — оригинал + frames, image — оригинал,
  audio — оригинал)
- Marker raw_payload.cleaned_up = True в payload — чтобы не повторять

L1 метаданные (filename, size, duration, transcript) остаются НАВСЕГДА.
UI показывает «обработан, файл удалён» для cleaned events.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from app.db.postgres import get_pool
from app.db.s3 import get_s3

logger = logging.getLogger(__name__)

MEDIA_BUCKET = "media-frames"
TTL_MINUTES = 15
SCAN_INTERVAL_SECONDS = 300  # every 5 min


async def _cleanup_one_media(media_id: str, kind: str) -> tuple[int, list[str]]:
    """Удалить все объекты под префиксом {kind}/{media_id}/ из MinIO.

    Returns (count, errors).
    """
    s3 = get_s3()
    prefix = f"{kind}/{media_id}/"
    count = 0
    errors: list[str] = []
    try:
        objects = list(s3.list_objects(MEDIA_BUCKET, prefix=prefix, recursive=True))
    except Exception as e:
        return 0, [f"list failed: {e}"]
    for obj in objects:
        try:
            s3.remove_object(MEDIA_BUCKET, obj.object_name)
            count += 1
        except Exception as e:
            errors.append(f"{obj.object_name}: {e}")
    return count, errors


async def cleanup_expired_media() -> dict:
    """Одна итерация: найти + удалить expired media.

    Returns stats {scanned, cleaned, files_removed, errors}.
    """
    stats = {"scanned": 0, "cleaned": 0, "files_removed": 0, "errors": []}
    pool = await get_pool()
    cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=TTL_MINUTES)

    async with pool.acquire() as conn:
        # Find media events older than TTL that aren't yet cleaned
        rows = await conn.fetch(
            """
            SELECT id::text AS id, raw_payload
              FROM l1_raw_events
             WHERE domain = 'media_analysis'
               AND timestamp < $1
               AND (raw_payload->>'cleaned_up' IS NULL
                    OR raw_payload->>'cleaned_up' != 'true')
             ORDER BY timestamp ASC
             LIMIT 100
            """,
            cutoff,
        )

    for row in rows:
        stats["scanned"] += 1
        try:
            payload = row["raw_payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            media_id = payload.get("media_id")
            kind = payload.get("kind")
            if not media_id or not kind:
                continue

            count, errors = await _cleanup_one_media(media_id, kind)
            stats["files_removed"] += count
            stats["errors"].extend(errors)

            # Mark cleaned in raw_payload (idempotent — won't be re-scanned)
            payload["cleaned_up"] = True
            payload["cleaned_at"] = datetime.now(tz=timezone.utc).isoformat()
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE l1_raw_events SET raw_payload = $1::jsonb WHERE id = $2::uuid",
                    json.dumps(payload, ensure_ascii=False),
                    row["id"],
                )
            stats["cleaned"] += 1
        except Exception as e:
            stats["errors"].append(f"{row['id']}: {type(e).__name__}: {e}")

    return stats


async def cleanup_loop() -> None:
    """Бесконечный loop — каждые SCAN_INTERVAL_SECONDS вызывает cleanup.

    Запускается из app/main.py lifespan startup. Cancel при shutdown.
    """
    logger.info("media_cleanup loop started: TTL=%dmin scan=%ds", TTL_MINUTES, SCAN_INTERVAL_SECONDS)
    while True:
        try:
            stats = await cleanup_expired_media()
            if stats["cleaned"] > 0 or stats["errors"]:
                logger.info(
                    "media_cleanup: scanned=%d cleaned=%d files_removed=%d errors=%d",
                    stats["scanned"], stats["cleaned"], stats["files_removed"], len(stats["errors"]),
                )
                if stats["errors"]:
                    logger.warning("media_cleanup errors: %s", stats["errors"][:5])
        except asyncio.CancelledError:
            logger.info("media_cleanup loop cancelled")
            raise
        except Exception as e:
            logger.exception("media_cleanup tick error: %s", e)
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)
