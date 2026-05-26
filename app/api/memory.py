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
async def trigger_daily(since_hours: int | None = None, domain: str | None = None, request: Request = None):
    """Ручной запуск L1→L2 консолидации."""
    if request:
        await verify_api_key(request)
    result = await daily_consolidate(since_hours, domain)
    return result


@router.post("/consolidate/weekly")
async def trigger_weekly(domain: str, request: Request):
    """Ручной запуск L2→L3 консолидации."""
    await verify_api_key(request)
    result = await weekly_consolidate(domain)
    return result


@router.post("/audit/monthly")
async def trigger_monthly_audit(domain: str, request: Request):
    """Ручной запуск ежемесячной ревизии L3."""
    await verify_api_key(request)
    result = await run_monthly_audit(domain)
    return result


@router.get("/snapshots")
async def list_snapshots(domain: str | None = None, request: Request = None):
    """Список L4-снапшотов."""
    if request:
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
                            (id, domain, knowledge_type, content, version, effective_from)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        ON CONFLICT (id) DO UPDATE
                        SET content = EXCLUDED.content, version = EXCLUDED.version,
                            effective_to = NULL
                        """,
                        UUID(k["id"]), k["domain"], k.get("knowledge_type", "rule"),
                        json.dumps(k.get("content", {})), k.get("version", 1),
                        _parse_ts(k.get("effective_from")),
                    )

                for t in data.get("tools", []):
                    await conn.execute(
                        """
                        INSERT INTO l3_tools_registry
                            (id, domain, tool_name, tool_type, description, config_schema,
                             usage_patterns, version, effective_from)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                        ON CONFLICT (id) DO UPDATE
                        SET description = EXCLUDED.description,
                            config_schema = EXCLUDED.config_schema,
                            usage_patterns = EXCLUDED.usage_patterns,
                            effective_to = NULL
                        """,
                        UUID(t["id"]), t["domain"], t.get("tool_name", ""),
                        t.get("tool_type", "service"), t.get("description", ""),
                        json.dumps(t.get("config_schema", {})),
                        json.dumps(t.get("usage_patterns", {})),
                        t.get("version", 1), _parse_ts(t.get("effective_from")),
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
    """Очистка устаревших L1-событий."""
    await verify_api_key(request)

    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            DELETE FROM l1_raw_events
            WHERE created_at < NOW() - ($1 || ' days')::INTERVAL
            """,
            str(settings.retention_days),
        )
        # Убираем отметку deleted
        deleted = int(result.split()[-1]) if result else 0

    return {"status": "cleaned", "deleted_events": deleted}
