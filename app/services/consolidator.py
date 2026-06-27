import hashlib
import io
import json
import logging
import uuid as _uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone

from app.config import settings
from app.db.postgres import get_pool
from app.db.s3 import get_s3, snapshot_key
from app.services.analyzer import analyze_daily_events, analyze_weekly
from app.services.curator import monthly_audit, pre_daily_filter, pre_weekly_check
from app.services.ingestor import get_unprocessed_events, mark_events_processed
from app.services.operative import index_domain_vectors

log = logging.getLogger(__name__)


@asynccontextmanager
async def _advisory_lock(lock_name: str):
    """Postgres advisory lock — атомарный, освобождается при дисконнекте.
    Защита от двойной консолидации при N>1 инстансах API.
    Yield True если lock взят, False если уже занят."""
    pool = await get_pool()
    conn = await pool.acquire()
    acquired = False
    try:
        # hashtext возвращает int4, advisory_lock принимает int8
        row = await conn.fetchrow("SELECT pg_try_advisory_lock(hashtext($1)) AS got", lock_name)
        acquired = bool(row["got"])
        yield acquired
    finally:
        if acquired:
            try:
                await conn.execute("SELECT pg_advisory_unlock(hashtext($1))", lock_name)
            except Exception:
                pass
        await pool.release(conn)


def _to_uuid(val) -> _uuid.UUID:
    """Конвертирует значение в UUID, обрабатывая оба типа."""
    if isinstance(val, _uuid.UUID):
        return val
    return _uuid.UUID(str(val))


async def daily_consolidate(since_hours: int | None = None, domain: str | None = None) -> dict:
    """L1 → L2: фильтрация + анализ + сохранение дневного буфера.
    Защищён advisory lock — при параллельном вызове на тот же domain
    второй вернёт {status: 'lock_held'} вместо дубля."""
    lock_key = f"daily:{domain or 'all'}"
    async with _advisory_lock(lock_key) as got:
        if not got:
            return {"status": "lock_held", "domain": domain, "lock": lock_key}
        return await _daily_consolidate_impl(since_hours, domain)


async def _daily_consolidate_impl(since_hours: int | None = None, domain: str | None = None) -> dict:
    hours = since_hours or settings.daily_hours
    events = await get_unprocessed_events(hours, domain)
    if not events:
        return {"status": "no_events", "buffer_id": None}

    domains = {e["domain"] for e in events}
    results = []

    for dom in domains:
        dom_events = [e for e in events if e["domain"] == dom]

        # Шаг 1: Куратор — фильтрация шума
        curator_result = await pre_daily_filter(dom_events, dom)
        if curator_result.get("skip"):
            results.append({"domain": dom, "status": "skipped", "reason": curator_result.get("reason")})
            continue

        filtered_ids = set(curator_result.get("filtered_event_ids", []))
        filtered_events = [e for e in dom_events if str(e["id"]) in filtered_ids]

        if not filtered_events:
            # Используем все события если куратор ничего не оставил
            filtered_events = dom_events

        # Шаг 2: Анализ
        analysis = await analyze_daily_events(filtered_events, dom)

        # Шаги 3+4 в одной транзакции: INSERT L2-буфера И mark_events_processed
        # должны коммититься атомарно. Иначе сбой между ними оставлял события
        # помеченными как обработанные при пустом L2 (или, наоборот, рождал
        # дубль L2 на следующем цикле — ON CONFLICT DO UPDATE склеивал бы
        # source_event_ids и портил confidence).
        buffer_id = _uuid.uuid4()
        today = date.today()
        now = datetime.now(timezone.utc)
        filtered_event_ids = [e["id"] for e in filtered_events]
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO l2_daily_buffers (id, date, domain, summary, source_event_ids, confidence, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (date, domain) DO UPDATE
                    SET summary = EXCLUDED.summary,
                        source_event_ids = l2_daily_buffers.source_event_ids || EXCLUDED.source_event_ids,
                        confidence = (l2_daily_buffers.confidence + EXCLUDED.confidence) / 2,
                        created_at = EXCLUDED.created_at
                    """,
                    buffer_id, today, dom, json.dumps(analysis, ensure_ascii=False),
                    filtered_event_ids,
                    analysis.get("confidence", 0.5),
                    now,
                )
                await mark_events_processed(filtered_event_ids, conn=conn)

        results.append({"domain": dom, "status": "consolidated", "buffer_id": str(buffer_id)})

    return {"status": "ok", "results": results}


async def weekly_consolidate(domain: str) -> dict:
    """L2 → L3: проверка качества + синтез + L4-снапшот.
    Защищён advisory lock от параллельных запусков на тот же domain."""
    async with _advisory_lock(f"weekly:{domain}") as got:
        if not got:
            return {"status": "lock_held", "domain": domain}
        return await _weekly_consolidate_impl(domain)


async def _weekly_consolidate_impl(domain: str) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Загружаем L2 буферы за неделю
        l2_rows = await conn.fetch(
            """
            SELECT id, date, domain, summary, source_event_ids, confidence
            FROM l2_daily_buffers
            WHERE domain = $1
              AND date >= CURRENT_DATE - $2::int
            ORDER BY date
            """,
            domain, settings.weekly_days,
        )
        l2_buffers = [dict(r) for r in l2_rows]

        # Загружаем активные L3 знания
        l3_rows = await conn.fetch(
            """
            SELECT id, domain, knowledge_type, content, version, derived_from_l2_ids
            FROM l3_master_knowledge
            WHERE domain = $1 AND effective_to IS NULL
            """,
            domain,
        )
        current_l3 = [dict(r) for r in l3_rows]

        # Загружаем активные инструменты
        tool_rows = await conn.fetch(
            """
            SELECT id, domain, tool_name, tool_type, description, config_schema, usage_patterns
            FROM l3_tools_registry
            WHERE domain = $1 AND effective_to IS NULL
            """,
            domain,
        )
        current_tools = [dict(r) for r in tool_rows]

    if not l2_buffers:
        return {"status": "no_buffers"}

    # Шаг 1: Куратор качества
    quality = await pre_weekly_check(domain, current_l3, l2_buffers)

    # Шаг 2: LLM-синтез
    synthesis = await analyze_weekly(domain, current_l3, current_tools, l2_buffers)

    # Шаг 3: Применяем изменения
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        # Новые/обновлённые знания
        for item in synthesis.get("new_or_updated", []):
            new_id = _uuid.uuid4()
            content = item.get("content", {})
            knowledge_type = item.get("type", "rule")
            merge_id = item.get("merge_with_l3_id")

            if merge_id:
                # Обновляем существующее
                await conn.execute(
                    """
                    UPDATE l3_master_knowledge
                    SET content = $1, version = version + 1
                    WHERE id = $2
                    """,
                    json.dumps(content, ensure_ascii=False),
                    _to_uuid(merge_id),
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO l3_master_knowledge
                        (id, domain, knowledge_type, content, version, derived_from_l2_ids, effective_from, created_at)
                    VALUES ($1, $2, $3, $4, 1, $5, $6, $7)
                    """,
                    new_id, domain, knowledge_type,
                    json.dumps(content, ensure_ascii=False),
                    [str(b["id"]) for b in l2_buffers],
                    now, now,
                )

        # Депрекация устаревшего
        for dep_id in synthesis.get("deprecated_l3_ids", []):
            await conn.execute(
                "UPDATE l3_master_knowledge SET effective_to = $1 WHERE id = $2",
                now, _to_uuid(dep_id),
            )

        # Инструменты
        VALID_TOOL_TYPES = {"api", "script", "prompt", "library", "service"}
        for tool in synthesis.get("tools", []):
            tool_id = _uuid.uuid4()
            raw_type = tool.get("tool_type", "service")
            safe_type = raw_type if raw_type in VALID_TOOL_TYPES else "service"
            await conn.execute(
                """
                INSERT INTO l3_tools_registry
                    (id, domain, tool_name, tool_type, description, config_schema,
                     usage_patterns, version, effective_from, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, 1, $8, $9)
                ON CONFLICT DO NOTHING
                """,
                tool_id, domain,
                tool.get("tool_name", "unknown"),
                safe_type,
                tool.get("usage_pattern", ""),
                "{}",
                json.dumps({"pattern": tool.get("usage_pattern", "")}, ensure_ascii=False),
                now, now,
            )

    # Шаг 4: L4 снапшот
    snapshot_id = await _maybe_snapshot(domain)

    # Шаг 5: Индексация векторов для RediSearch KNN
    index_result = await index_domain_vectors(domain)

    return {
        "status": "consolidated",
        "new_items": len(synthesis.get("new_or_updated", [])),
        "deprecated": len(synthesis.get("deprecated_l3_ids", [])),
        "tools_added": len(synthesis.get("tools", [])),
        "quality": quality,
        "snapshot_id": str(snapshot_id) if snapshot_id else None,
        "vectors_indexed": index_result["total"],
    }


async def _maybe_snapshot(domain: str) -> _uuid.UUID | None:
    """Создаёт L4-снапшот если достаточно изменений или интервал превышен."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Последний снапшот
        last = await conn.fetchrow(
            """
            SELECT snapshot_time, snapshot_hash
            FROM l4_snapshots
            WHERE snapshot_type = 'full'
            ORDER BY snapshot_time DESC
            LIMIT 1
            """
        )

        # Текущие L3 данные
        knowledge_rows = await conn.fetch(
            "SELECT * FROM l3_master_knowledge WHERE domain = $1 AND effective_to IS NULL", domain
        )
        tool_rows = await conn.fetch(
            "SELECT * FROM l3_tools_registry WHERE domain = $1 AND effective_to IS NULL", domain
        )

    knowledge_data = json.dumps([dict(r) for r in knowledge_rows], ensure_ascii=False, default=str)
    tools_data = json.dumps([dict(r) for r in tool_rows], ensure_ascii=False, default=str)
    current_hash = hashlib.sha256((knowledge_data + tools_data).encode()).hexdigest()

    # Проверяем изменения
    if last:
        last_time = last["snapshot_time"]
        weeks_elapsed = (datetime.now(timezone.utc) - last_time).days / 7
        hash_changed = last["snapshot_hash"] != current_hash

        if weeks_elapsed < settings.l4_full_snapshot_interval_weeks and not hash_changed:
            # Раньше — тихий None: /health показывал «last_l4_snapshot 13 дней
            # назад», operator не понимал, баг это или by-design. Теперь явно
            # логируем причину, чтобы не было ложной паники в инцидент-чате.
            log.info(
                "l4_snapshot_skipped domain=%s weeks_elapsed=%.2f "
                "interval_weeks=%d hash_changed=%s",
                domain, weeks_elapsed,
                settings.l4_full_snapshot_interval_weeks, hash_changed,
            )
            return None

    # Создаём полный снапшот
    snapshot_id = _uuid.uuid4()
    now = datetime.now(timezone.utc)
    s3_path = snapshot_key(domain, str(snapshot_id))
    s3 = get_s3()
    snapshot_blob = json.dumps({
        "knowledge": [dict(r) for r in knowledge_rows],
        "tools": [dict(r) for r in tool_rows],
        "hash": current_hash,
        "created_at": now.isoformat(),
    }, ensure_ascii=False, default=str).encode("utf-8")

    s3.put_object(
        bucket_name=settings.s3_bucket,
        object_name=s3_path,
        data=io.BytesIO(snapshot_blob),
        length=len(snapshot_blob),
    )

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO l4_snapshots
                (id, snapshot_time, snapshot_type, l3_knowledge_snapshot, l3_tools_snapshot,
                 total_knowledge_records, total_tools, changed_knowledge_records, changed_tools,
                 snapshot_hash, s3_path, is_verified, comment)
            VALUES ($1, $2, 'full', $3, $4, $5, $6, $7, $8, $9, $10, false, 'weekly snapshot')
            """,
            snapshot_id, now,
            json.dumps([dict(r) for r in knowledge_rows], ensure_ascii=False, default=str),
            json.dumps([dict(r) for r in tool_rows], ensure_ascii=False, default=str),
            len(knowledge_rows), len(tool_rows),
            len(knowledge_rows), len(tool_rows),
            current_hash, s3_path,
        )

    return snapshot_id


async def run_monthly_audit(domain: str) -> dict:
    """Ежемесячная ревизия L3."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        knowledge_rows = await conn.fetch(
            "SELECT * FROM l3_master_knowledge WHERE domain = $1", domain
        )
        tool_rows = await conn.fetch(
            "SELECT * FROM l3_tools_registry WHERE domain = $1", domain
        )

    l3_knowledge = [dict(r) for r in knowledge_rows]
    l3_tools = [dict(r) for r in tool_rows]

    result = await monthly_audit(domain, l3_knowledge, l3_tools)

    # Применяем рекомендации
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        for stale_id in result.get("stale_knowledge_ids", []):
            await conn.execute(
                "UPDATE l3_master_knowledge SET effective_to = $1 WHERE id = $2",
                now, _to_uuid(stale_id),
            )
        for dead_id in result.get("dead_tool_ids", []):
            await conn.execute(
                "UPDATE l3_tools_registry SET effective_to = $1 WHERE id = $2",
                now, _to_uuid(dead_id),
            )

    # Переиндексация векторов после очистки
    from app.services.operative import _remove_indexed_vectors
    stale_strs = [str(s) for s in result.get("stale_knowledge_ids", [])]
    dead_strs = [str(d) for d in result.get("dead_tool_ids", [])]
    await _remove_indexed_vectors(stale_strs + dead_strs)
    index_result = await index_domain_vectors(domain)
    result["vectors_indexed"] = index_result["total"]

    return result
