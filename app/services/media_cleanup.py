"""Background task: ПОЛНОЕ удаление media — файлы + L1 строка.

Owner-decision (2026-05-23 v2 — изменилось!): «B, полное» — и MinIO
объекты, и L1 строка удаляются через 15 минут после upload. Это
отличается от v1 (2026-05-22) где L1 метаданные оставались навсегда
для cognitive_recall'а.

Trade-off (osознанный owner-ом):
  + Освобождается место в БД (per-tenant quota не растёт)
  + UI «Мои медиа» сама собой чистится — без archive-chip'а
  − cognitive_recall(domain='media_analysis') не вернёт старые upload'ы
  − Транскрипт + описание кадров теряется через 15 мин

Логика:
- TTL = 15 минут от момента upload (timestamp в L1 raw_events)
- Каждые 5 минут scan: ищем media-events где (now - timestamp) > 15 мин
- Удаляем из MinIO bucket media-frames всё под префиксом
  {kind}/{media_id}/ (video — оригинал + frames, image — оригинал,
  audio — оригинал)
- DELETE FROM l1_raw_events WHERE id = $1 — полное удаление строки
- Старые cleaned_up=true строки (из v1) тоже удаляются — they're orphans.
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
TTL_MINUTES = 1440  # 24h (2026-05-26 per ewewew feedback — frames должны выживать compaction)
SCAN_INTERVAL_SECONDS = 300  # every 5 min


async def _cleanup_one_media(media_id: str, kind: str) -> tuple[int, list[str]]:
    """Удалить все объекты под префиксом {kind}/{media_id}/ из MinIO.

    Returns (count, errors). count=0 OK если files уже удалены ранее (v1 cleanup).
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
    """Одна итерация: найти + удалить expired media (файлы + L1 row).

    v2 (2026-05-23): hard-delete L1 row после удаления MinIO objects.
    Старые cleaned_up=true строки (из v1) тоже удаляются — orphans.

    Returns stats {scanned, cleaned, files_removed, rows_deleted, errors}.
    """
    stats = {"scanned": 0, "cleaned": 0, "files_removed": 0, "rows_deleted": 0, "errors": []}
    pool = await get_pool()
    cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=TTL_MINUTES)

    async with pool.acquire() as conn:
        # v2: scan ВСЕ media events старше TTL, без cleaned_up фильтра —
        # старые v1 cleaned-up rows тоже подлежат удалению.
        rows = await conn.fetch(
            """
            SELECT id::text AS id, raw_payload
              FROM l1_raw_events
             WHERE domain = 'media_analysis'
               AND timestamp < $1
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

            # Try cleanup files (no-op if already deleted by v1)
            if media_id and kind:
                count, errors = await _cleanup_one_media(media_id, kind)
                stats["files_removed"] += count
                stats["errors"].extend(errors)

            # v2: HARD-DELETE L1 row — без него ничего не значит
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM l1_raw_events WHERE id = $1::uuid",
                    row["id"],
                )
            stats["rows_deleted"] += 1
            stats["cleaned"] += 1
        except Exception as e:
            stats["errors"].append(f"{row['id']}: {type(e).__name__}: {e}")

    return stats


async def cleanup_stale_pending_agents() -> int:
    """PR #35: удалить agent_states pending_claim старше 10 мин (CLAIM_TTL).

    Если owner нажал «Передать ключ» но никто не сделал claim — pending row
    висит. Через 10 мин (CLAIM_TTL_SECONDS из connect.py) удаляем.

    P0-1 fix (orphaned-key incident, 2x за 4 дня): agent_keys.agent_id —
    FK ON DELETE CASCADE на agent_states. Если claim-handshake не успел
    перевести агента в status='active' (guardrails блокируют curl, MCP
    переподключается), но ключ уже создан — этот DELETE сносил agent_states
    И каскадом убивал РАБОЧИЙ ключ → агент терял доступ и всю память.
    Защита: НЕ трогаем pending, у которого есть активный (не отозванный)
    ключ — наличие ключа значит, что claim фактически прошёл. Висящий
    мусор без ключа по-прежнему чистится.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM agent_states ast "
            "WHERE ast.status = 'pending_claim' "
            "  AND ast.created_at < NOW() - INTERVAL '10 minutes' "
            "  AND NOT EXISTS ("
            "        SELECT 1 FROM agent_keys ak "
            "         WHERE ak.agent_id = ast.agent_id "
            "           AND ak.revoked_at IS NULL"
            "  )"
        )
    # Result format: "DELETE N"
    try:
        n = int(result.split()[-1])
    except Exception:
        n = 0
    if n > 0:
        logger.info("cleanup_stale_pending_agents: deleted %d expired pending agents", n)
    return n


async def cleanup_loop() -> None:
    """Бесконечный loop — каждые SCAN_INTERVAL_SECONDS вызывает cleanup.

    Делает 2 задачи:
    1. media_cleanup — удаляет файлы + L1 строки старше 15 мин
    2. pending_agents — удаляет неclaim'нутые agent_states старше 10 мин

    Запускается из app/main.py lifespan startup. Cancel при shutdown.
    """
    logger.info("media_cleanup v2 loop started (HARD-DELETE): TTL=%dmin scan=%ds",
                TTL_MINUTES, SCAN_INTERVAL_SECONDS)
    while True:
        try:
            stats = await cleanup_expired_media()
            if stats["cleaned"] > 0 or stats["errors"]:
                logger.info(
                    "media_cleanup: scanned=%d cleaned=%d files=%d rows_deleted=%d errors=%d",
                    stats["scanned"], stats["cleaned"], stats["files_removed"],
                    stats["rows_deleted"], len(stats["errors"]),
                )
                if stats["errors"]:
                    logger.warning("media_cleanup errors: %s", stats["errors"][:5])
        except asyncio.CancelledError:
            logger.info("cleanup loop cancelled")
            raise
        except Exception as e:
            logger.exception("media_cleanup tick error: %s", e)
        # Cleanup pending agents (PR #35) — отдельно чтобы не мешать media
        try:
            await cleanup_stale_pending_agents()
        except Exception as e:
            logger.exception("cleanup_stale_pending_agents tick error: %s", e)
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)
