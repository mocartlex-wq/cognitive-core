import json
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Request

from app.config import settings
from app.db.postgres import get_pool
from app.db.s3 import get_s3
from app.security.auth import verify_api_key
from app.services.consolidator import (
    daily_consolidate,
    run_monthly_audit,
    weekly_consolidate,
)
from app.services.operative import (
    cleanup_stale_vectors,
    index_domain_vectors,
    restore_redis_from_pg,
)

router = APIRouter(prefix="/memory", tags=["memory"])


@router.post("/consolidate/daily")
async def trigger_daily(request: Request, since_hours: int | None = None, domain: str | None = None):
    """Ручной запуск L1→L2 консолидации.

    `request` обязателен намеренно. Раньше было `request: Request = None` плюс
    `if request:` — то есть проверка ключа выполнялась, только если запрос
    оказался передан. Дефолт убран, авторизация безусловна.
    """
    await verify_api_key(request)
    result = await daily_consolidate(since_hours, domain)
    return result


@router.post("/consolidate/backfill")
async def trigger_backfill(
    request: Request,
    max_domains: int = 5,
    max_events_per_domain: int = 60,
    domain: str | None = None,
):
    """Догнать накопленный хвост L1, который выпал из суточного окна.

    Обычный daily смотрит только последние daily_hours часов — событие, не
    попавшее туда за сутки, не консолидируется уже никогда. Этот режим снимает
    окно и разгребает очередь порциями. Вызывать повторно, пока
    backlog_remaining не станет 0 (безопасно: advisory lock не даст двум
    прогонам столкнуться).
    """
    await verify_api_key(request)
    result = await daily_consolidate(
        domain=domain, backfill=True,
        max_domains=max_domains, max_events_per_domain=max_events_per_domain,
    )
    # Сколько ещё осталось — чтобы вызывающий видел, тает ли очередь.
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            remaining = await conn.fetchval(
                "SELECT COUNT(*) FROM l1_raw_events WHERE processed_to_l2 = FALSE"
            )
        result["backlog_remaining"] = remaining or 0
    except Exception as e:
        result["backlog_remaining"] = f"unknown: {type(e).__name__}"
    return result


@router.post("/consolidate/weekly")
async def trigger_weekly(domain: str, request: Request):
    """Ручной запуск L2→L3 консолидации."""
    await verify_api_key(request)
    result = await weekly_consolidate(domain)
    return result


@router.post("/knowledge/retire-thin")
async def retire_thin_knowledge(request: Request, max_sources: int = 1, since_hours: int = 24,
                                dry_run: bool = True, domain: str | None = None):
    """Снять (effective_to = NOW()) активные L3-записи владельца, выведенные из
    ≤ max_sources L2-буферов за последние since_hours.

    Зачем: 06.09 разовый прогон weekly по 35 застрявшим доменам продвинул в L3
    знания из ОДНОГО буфера — куратор проигнорировал порог повторяемости в
    промпте (домен tests получил «необходимо фиксировать инструменты…»). Порог
    теперь в коде (insufficient_repetition), а эта ручка убирает то, что успело
    пройти. Мягко: запись остаётся в таблице с effective_to, L2-буферы целы —
    как только у домена появится второй буфер, weekly продвинет знание честно.

    Только свой владелец; NULL-owner = отказ (гейт 05.09). По умолчанию dry_run.
    """
    from collections import Counter

    from fastapi import HTTPException

    from app.security.owner import resolve_owner_user_id

    await verify_api_key(request)
    owner = await resolve_owner_user_id(request)
    if not owner:
        raise HTTPException(status_code=403, detail="ключ без владельца — снимать знания нельзя")
    max_sources = max(0, min(int(max_sources), 5))
    since_hours = max(1, min(int(since_hours), 24 * 30))
    where = """effective_to IS NULL
               AND owner_user_id = $1::uuid
               AND effective_from >= NOW() - ($2::int * INTERVAL '1 hour')
               AND COALESCE(cardinality(derived_from_l2_ids), 0) <= $3::int
               AND ($4::text IS NULL OR domain = $4::text)"""
    args = (str(owner), since_hours, max_sources, domain)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT id, domain, knowledge_type FROM l3_master_knowledge WHERE {where}", *args,
        )
        retired = 0
        if rows and not dry_run:
            res = await conn.execute(
                f"UPDATE l3_master_knowledge SET effective_to = NOW() WHERE {where}", *args,
            )
            try:
                retired = int(str(res).split()[-1])
            except (ValueError, IndexError):
                retired = len(rows)
    by_domain = dict(Counter(r["domain"] for r in rows))
    reindex = []
    if retired:
        # Индекс OP/pgvector строится по активным записям — пересобираем
        # затронутые домены, чтобы recall не отдавал снятое (best-effort).
        for d in sorted(by_domain):
            try:
                await index_domain_vectors(d)
                reindex.append(d)
            except Exception:
                pass
    return {
        "dry_run": dry_run, "matched": len(rows), "retired": retired,
        "by_domain": by_domain, "max_sources": max_sources, "since_hours": since_hours,
        "reindexed": reindex,
    }


@router.post("/audit/monthly")
async def trigger_monthly_audit(domain: str, request: Request):
    """Ручной запуск ежемесячной ревизии L3."""
    await verify_api_key(request)
    result = await run_monthly_audit(domain)
    return result


@router.get("/snapshots")
async def list_snapshots(request: Request, domain: str | None = None):
    """Список L4-снапшотов. Авторизация безусловна (см. trigger_daily)."""
    await verify_api_key(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        if domain:
            rows = await conn.fetch(
                """
                SELECT id, snapshot_time, snapshot_type, total_knowledge_records,
                       total_tools, changed_knowledge_records, changed_tools,
                       snapshot_hash, s3_path, is_verified, comment
                FROM l4_snapshots
                ORDER BY snapshot_time DESC
                LIMIT 50
                """
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, snapshot_time, snapshot_type, total_knowledge_records,
                       total_tools, changed_knowledge_records, changed_tools,
                       snapshot_hash, s3_path, is_verified, comment
                FROM l4_snapshots
                ORDER BY snapshot_time DESC
                LIMIT 50
                """
            )
        return [dict(r) for r in rows]


async def _verify_snapshot_integrity(snap: dict, blob: bytes) -> dict:
    """Проверяет целостность L4 снапшота перед restore.

    Проверки:
      1. JSON парсится
      2. SHA-256 blob совпадает с snapshot_hash из БД
      3. Структура: должны быть поля knowledge и tools (списки)
      4. total_knowledge_records и total_tools совпадают с фактическим len()

    Returns: {ok: bool, errors: [...], data: dict | None}
    """
    import hashlib
    errors = []

    # 1. SHA-256
    actual_hash = hashlib.sha256(blob).hexdigest()
    expected_hash = snap.get("snapshot_hash")
    if expected_hash and actual_hash != expected_hash:
        errors.append(
            f"hash_mismatch: expected={expected_hash[:16]}... actual={actual_hash[:16]}..."
        )

    # 2. JSON parse
    try:
        data = json.loads(blob)
    except json.JSONDecodeError as e:
        errors.append(f"invalid_json: {str(e)[:200]}")
        return {"ok": False, "errors": errors, "data": None}

    # 3. Структура
    if not isinstance(data, dict):
        errors.append("not_a_dict")
        return {"ok": False, "errors": errors, "data": None}

    knowledge = data.get("knowledge")
    tools = data.get("tools")
    if knowledge is None:
        errors.append("missing_knowledge_field")
    elif not isinstance(knowledge, list):
        errors.append(f"knowledge_not_a_list (got {type(knowledge).__name__})")
    if tools is None:
        errors.append("missing_tools_field")
    elif not isinstance(tools, list):
        errors.append(f"tools_not_a_list (got {type(tools).__name__})")

    # 4. Counts (если поля есть в БД)
    if isinstance(knowledge, list):
        expected_k = snap.get("total_knowledge_records")
        if expected_k is not None and len(knowledge) != expected_k:
            errors.append(
                f"knowledge_count_mismatch: db_says={expected_k} blob_has={len(knowledge)}"
            )
    if isinstance(tools, list):
        expected_t = snap.get("total_tools")
        if expected_t is not None and len(tools) != expected_t:
            errors.append(
                f"tools_count_mismatch: db_says={expected_t} blob_has={len(tools)}"
            )

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "data": data,
        "actual_hash": actual_hash,
        "knowledge_in_blob": len(knowledge) if isinstance(knowledge, list) else None,
        "tools_in_blob": len(tools) if isinstance(tools, list) else None,
    }


@router.post("/snapshots/{snapshot_id}/verify")
async def verify_snapshot(snapshot_id: str, request: Request):
    """Проверяет целостность L4-снапшота БЕЗ восстановления.

    Безопасный health-check: загружает blob из S3, считает hash, проверяет структуру.
    Полезно для регулярного мониторинга бэкапов."""
    await verify_api_key(request)

    pool = await get_pool()
    async with pool.acquire() as conn:
        snap = await conn.fetchrow(
            """SELECT id, s3_path, snapshot_hash, snapshot_time,
                      total_knowledge_records, total_tools, is_verified
               FROM l4_snapshots WHERE id = $1""",
            UUID(snapshot_id),
        )
    if not snap:
        return {"status": "not_found"}

    s3 = get_s3()
    try:
        blob = s3.get_object(settings.s3_bucket, snap["s3_path"]).read()
    except Exception as e:
        return {"status": "error", "detail": f"s3_read_failed: {str(e)[:200]}"}

    result = await _verify_snapshot_integrity(dict(snap), blob)

    # Если valid — обновляем is_verified=TRUE
    if result["ok"]:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE l4_snapshots SET is_verified = TRUE WHERE id = $1",
                UUID(snapshot_id),
            )

    return {
        "status": "ok" if result["ok"] else "integrity_failed",
        "snapshot_id": snapshot_id,
        "snapshot_time": snap["snapshot_time"].isoformat() if snap["snapshot_time"] else None,
        "errors": result["errors"],
        "actual_hash": result["actual_hash"],
        "knowledge_in_blob": result["knowledge_in_blob"],
        "tools_in_blob": result["tools_in_blob"],
        "db_total_knowledge": snap["total_knowledge_records"],
        "db_total_tools": snap["total_tools"],
    }


@router.post("/snapshots/restore/{snapshot_id}")
async def restore_snapshot(snapshot_id: str, request: Request, strict: bool = True):
    """Восстановление L3 из L4-снапшота.

    Двухфазное:
      Фаза 1 — Verify: SHA-256 хэш + структура + total_records counts.
                Если strict=true (default) и проверка не прошла → отклонить.
                Если strict=false — продолжить с warning в response.
      Фаза 2 — Restore: атомарная транзакция INSERT/UPDATE.

    Параметры:
      strict=true (default): отклонить restore если integrity-checks failed.
                              Безопасно для production.
      strict=false: вернуть warning, но всё равно восстановить.
                    Только для recovery когда другие снапшоты испорчены.
    """
    await verify_api_key(request)
    # Восстановленные записи обязаны получить владельца: без него они не видны
    # ни одному owner-scoped recall (2026-08-11: 159/269 знаний без владельца).
    # Берём владельца из снапшота, если он там есть, иначе — вызывающего.
    from app.security.owner import resolve_owner_user_id
    caller_owner = await resolve_owner_user_id(request)

    pool = await get_pool()
    async with pool.acquire() as conn:
        snap = await conn.fetchrow(
            """SELECT id, s3_path, snapshot_hash, snapshot_time,
                      total_knowledge_records, total_tools
               FROM l4_snapshots WHERE id = $1""",
            UUID(snapshot_id),
        )

    if not snap:
        return {"status": "error", "detail": "Snapshot not found"}

    def _parse_ts(val: str | None):
        if not val:
            return datetime.now(timezone.utc)
        try:
            return datetime.fromisoformat(val)
        except (ValueError, TypeError):
            return datetime.now(timezone.utc)

    s3 = get_s3()
    try:
        # Фаза 1 — Verify
        blob = s3.get_object(settings.s3_bucket, snap["s3_path"]).read()
        verify = await _verify_snapshot_integrity(dict(snap), blob)
        if not verify["ok"] and strict:
            return {
                "status": "integrity_failed",
                "detail": "Snapshot failed integrity checks. Use strict=false to override (dangerous).",
                "errors": verify["errors"],
                "actual_hash": verify["actual_hash"],
            }

        if verify["data"] is None:
            return {
                "status": "error",
                "detail": "Cannot parse snapshot data",
                "errors": verify["errors"],
            }

        data = verify["data"]
        warnings = verify["errors"] if not verify["ok"] else []

        # Фаза 2 — Atomic restore
        async with pool.acquire() as conn:
            async with conn.transaction():
                for k in data.get("knowledge", []):
                    await conn.execute(
                        """
                        INSERT INTO l3_master_knowledge
                            (id, domain, knowledge_type, content, version, effective_from,
                             owner_user_id)
                        VALUES ($1, $2, $3, $4, $5, $6, $7::uuid)
                        ON CONFLICT (id) DO UPDATE
                        SET content = EXCLUDED.content, version = EXCLUDED.version,
                            effective_to = NULL,
                            owner_user_id = COALESCE(l3_master_knowledge.owner_user_id,
                                                     EXCLUDED.owner_user_id)
                        """,
                        UUID(k["id"]), k["domain"], k.get("knowledge_type", "rule"),
                        json.dumps(k.get("content", {})), k.get("version", 1),
                        _parse_ts(k.get("effective_from")),
                        k.get("owner_user_id") or caller_owner,
                    )

                for t in data.get("tools", []):
                    await conn.execute(
                        """
                        INSERT INTO l3_tools_registry
                            (id, domain, tool_name, tool_type, description, config_schema,
                             usage_patterns, version, effective_from, owner_user_id)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::uuid)
                        ON CONFLICT (id) DO UPDATE
                        SET description = EXCLUDED.description,
                            config_schema = EXCLUDED.config_schema,
                            usage_patterns = EXCLUDED.usage_patterns,
                            effective_to = NULL,
                            owner_user_id = COALESCE(l3_tools_registry.owner_user_id,
                                                     EXCLUDED.owner_user_id)
                        """,
                        UUID(t["id"]), t["domain"], t.get("tool_name", ""),
                        t.get("tool_type", "service"), t.get("description", ""),
                        json.dumps(t.get("config_schema", {})),
                        json.dumps(t.get("usage_patterns", {})),
                        t.get("version", 1), _parse_ts(t.get("effective_from")),
                        t.get("owner_user_id") or caller_owner,
                    )

                # Помечаем как verified если integrity прошёл
                if verify["ok"]:
                    await conn.execute(
                        "UPDATE l4_snapshots SET is_verified = TRUE WHERE id = $1",
                        UUID(snapshot_id),
                    )

        return {
            "status": "restored",
            "snapshot_id": snapshot_id,
            "knowledge_count": len(data.get("knowledge", [])),
            "tools_count": len(data.get("tools", [])),
            "integrity_verified": verify["ok"],
            "warnings": warnings,
        }

    except Exception as e:
        return {"status": "error", "detail": str(e)}


@router.post("/reindex")
async def reindex_vectors(request: Request, domain: str | None = None, drop_stale: bool = True):
    """Hot-reload эмбеддингов.

    1. Если drop_stale=true — удаляет из Redis векторы с устаревшей model_version
    2. Переиндексирует L3 знания и инструменты (или один домен) с актуальной моделью

    Используется при смене EMBEDDING_MODEL_NAME в коде или при подозрении на drift."""
    await verify_api_key(request)

    result = {"stale_cleanup": None, "reindex": []}
    if drop_stale:
        result["stale_cleanup"] = await cleanup_stale_vectors()

    if domain:
        result["reindex"].append(await index_domain_vectors(domain))
    else:
        # Все активные домены
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT domain FROM l3_master_knowledge WHERE effective_to IS NULL
                UNION SELECT DISTINCT domain FROM l3_tools_registry WHERE effective_to IS NULL
                """
            )
            domains = [r["domain"] for r in rows]
        for d in domains:
            result["reindex"].append(await index_domain_vectors(d))

    return result


@router.post("/restore-redis")
async def restore_redis(request: Request, domain: str | None = None):
    """Cold-start: загружает векторы из pgvector обратно в Redis.
    Без LLM-вызовов — берёт уже посчитанные эмбеддинги из Postgres."""
    await verify_api_key(request)
    return await restore_redis_from_pg(domain)


@router.post("/cleanup")
async def run_cleanup(request: Request):
    """Очистка устаревших L1-событий.

    Обычный retention применяется ТОЛЬКО к событиям, уже прошедшим в L2
    (processed_to_l2 = TRUE) — иначе при низкой активности (< MIN_EVENTS_FOR_DAILY
    событий/день куратор скипает консолидацию) prune удалял бы опыт, который ещё
    ни разу не анализировался (реальная потеря: июнь 2026, ~2 недели событий).
    Для необработанных действует страховочный потолок retention_unprocessed_days,
    чтобы брошенные домены не копились вечно.
    """
    await verify_api_key(request)

    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            DELETE FROM l1_raw_events
            WHERE created_at < NOW() - ($1 || ' days')::INTERVAL
              AND processed_to_l2 = TRUE
            """,
            str(settings.retention_days),
        )
        deleted = int(result.split()[-1]) if result else 0

        result_stale = await conn.execute(
            """
            DELETE FROM l1_raw_events
            WHERE created_at < NOW() - ($1 || ' days')::INTERVAL
              AND processed_to_l2 = FALSE
            """,
            str(settings.retention_unprocessed_days),
        )
        deleted_stale = int(result_stale.split()[-1]) if result_stale else 0

    return {
        "status": "cleaned",
        "deleted_events": deleted,
        "deleted_stale_unprocessed": deleted_stale,
    }
