"""Read-only dashboard endpoints (для встроенной web UI).

⚠️ Раньше весь этот роутер был БЕЗ АВТОРИЗАЦИИ: `curl` без единого заголовка
получал 200 и сырые `raw_payload` из L1 — вместе с IP-адресами и содержимым
сообщений ВСЕХ владельцев. Проверено на проде 2026-08-11.

Теперь: ключ обязателен везде, данные режутся по владельцу. Исключение —
`/audit-tail`: у `l5_audit_log` нет колонки `owner_user_id`, разграничить его
нечем, поэтому он доступен только админскому ключу (owner is None), а не
«всем, у кого есть любой ключ».
"""
from fastapi import APIRouter, HTTPException, Query, Request

from app.db.postgres import get_pool
from app.security.auth import verify_api_key
from app.security.owner import resolve_owner_user_id

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


async def _authorize(request: Request) -> str | None:
    """Ключ обязателен; возвращает owner_user_id или None для админа/legacy-ключа.

    None означает «фильтровать не по чему» — это admin-режим, а не «показать всё
    всем»: до сюда доходят только запросы с валидным ключом.
    """
    await verify_api_key(request)
    return await resolve_owner_user_id(request)


@router.get("/recent-events")
async def recent_events(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    domain: str | None = None,
):
    """Последние L1-события владельца."""
    owner = await _authorize(request)
    pool = await get_pool()
    conds, args = [], []
    if domain:
        args.append(domain)
        conds.append(f"domain = ${len(args)}")
    if owner:
        args.append(owner)
        conds.append(f"owner_user_id = ${len(args)}::uuid")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    args.append(limit)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, timestamp, source_agent, domain, raw_payload, processed_to_l2
            FROM l1_raw_events {where}
            ORDER BY timestamp DESC LIMIT ${len(args)}
            """,
            *args,
        )
    return {
        "count": len(rows),
        "items": [
            {
                "id": str(r["id"]),
                "timestamp": r["timestamp"].isoformat(),
                "agent": r["source_agent"],
                "domain": r["domain"],
                "payload": r["raw_payload"],
                "processed": r["processed_to_l2"],
            }
            for r in rows
        ],
    }


@router.get("/audit-tail")
async def audit_tail(
    request: Request,
    limit: int = Query(100, ge=1, le=1000),
    only_failures: bool = False,
):
    """Последние записи аудит-лога (L5) — только админскому ключу.

    У `l5_audit_log` нет `owner_user_id`, разграничить записи по владельцу нечем,
    а лог содержит действия всех тенантов. Пока колонки нет — доступ только
    админский; иначе любой владелец читал бы чужую активность.
    """
    owner = await _authorize(request)
    if owner is not None:
        raise HTTPException(
            status_code=403,
            detail="Аудит-лог доступен только админскому ключу: записи не разделены по владельцам",
        )
    pool = await get_pool()
    async with pool.acquire() as conn:
        if only_failures:
            rows = await conn.fetch(
                """
                SELECT id, event_time, agent_id, action, target_table, target_id, details, success
                FROM l5_audit_log WHERE success = false
                ORDER BY event_time DESC LIMIT $1
                """,
                limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, event_time, agent_id, action, target_table, target_id, details, success
                FROM l5_audit_log ORDER BY event_time DESC LIMIT $1
                """,
                limit,
            )
    return {
        "count": len(rows),
        "items": [
            {
                "id": str(r["id"]),
                "time": r["event_time"].isoformat(),
                "agent": r["agent_id"],
                "action": r["action"],
                "target_table": r["target_table"],
                "target_id": str(r["target_id"]) if r["target_id"] else None,
                "details": r["details"],
                "success": r["success"],
            }
            for r in rows
        ],
    }


@router.get("/domains")
async def list_domains(request: Request):
    """Домены владельца со счётчиками по слоям."""
    owner = await _authorize(request)
    pool = await get_pool()
    # Один параметр $1 на все подзапросы: NULL для админа снимает фильтр
    # ($1::uuid IS NULL), иначе режет каждый слой по владельцу.
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT domain,
                   (SELECT COUNT(*) FROM l1_raw_events
                     WHERE domain = d.domain
                       AND ($1::uuid IS NULL OR owner_user_id = $1::uuid)) AS l1,
                   (SELECT COUNT(*) FROM l2_daily_buffers
                     WHERE domain = d.domain
                       AND ($1::uuid IS NULL OR owner_user_id = $1::uuid)) AS l2,
                   (SELECT COUNT(*) FROM l3_master_knowledge
                     WHERE domain = d.domain AND effective_to IS NULL
                       AND ($1::uuid IS NULL OR owner_user_id = $1::uuid)) AS l3_active,
                   (SELECT COUNT(*) FROM l3_tools_registry
                     WHERE domain = d.domain AND effective_to IS NULL
                       AND ($1::uuid IS NULL OR owner_user_id = $1::uuid)) AS tools_active
            FROM (
                SELECT DISTINCT domain FROM l1_raw_events
                 WHERE ($1::uuid IS NULL OR owner_user_id = $1::uuid)
                UNION SELECT DISTINCT domain FROM l2_daily_buffers
                 WHERE ($1::uuid IS NULL OR owner_user_id = $1::uuid)
                UNION SELECT DISTINCT domain FROM l3_master_knowledge
                 WHERE ($1::uuid IS NULL OR owner_user_id = $1::uuid)
            ) d
            ORDER BY l1 DESC
            """,
            owner,
        )
    return {
        "count": len(rows),
        "items": [dict(r) for r in rows],
    }


@router.get("/timeline")
async def timeline(request: Request, days: int = Query(7, ge=1, le=90)):
    """Активность по дням за последние N дней (для графика)."""
    owner = await _authorize(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        l1 = await conn.fetch(
            """
            SELECT DATE(timestamp) AS day, COUNT(*) AS cnt
            FROM l1_raw_events
            WHERE timestamp >= CURRENT_DATE - $1::int
              AND ($2::uuid IS NULL OR owner_user_id = $2::uuid)
            GROUP BY day ORDER BY day
            """,
            days, owner,
        )
        l2 = await conn.fetch(
            """
            SELECT date AS day, COUNT(*) AS cnt
            FROM l2_daily_buffers
            WHERE date >= CURRENT_DATE - $1::int
              AND ($2::uuid IS NULL OR owner_user_id = $2::uuid)
            GROUP BY day ORDER BY day
            """,
            days, owner,
        )
        # Аудит — только админу: разделить его по владельцам нечем (см. /audit-tail).
        l5 = []
        if owner is None:
            l5 = await conn.fetch(
                """
                SELECT DATE(event_time) AS day, COUNT(*) AS cnt
                FROM l5_audit_log WHERE event_time >= CURRENT_DATE - $1::int
                GROUP BY day ORDER BY day
                """,
                days,
            )
    return {
        "days": days,
        "l1_per_day": [{"day": r["day"].isoformat(), "count": r["cnt"]} for r in l1],
        "l2_per_day": [{"day": r["day"].isoformat(), "count": r["cnt"]} for r in l2],
        "audit_per_day": [{"day": r["day"].isoformat(), "count": r["cnt"]} for r in l5],
    }


@router.get("/tools-registry")
async def tools_registry(
    request: Request,
    sort: str = Query("instances", pattern="^(instances|domains|recent|name)$"),
    type_filter: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
):
    """Глобальный реестр инструментов с агрегацией across доменов.

    Group by tool_name (одинаковые имена в разных доменах объединяются).
    Возвращает: name, type, instances, domains_breadth, last_used,
                domains[] (список доменов где встречается).

    sort options: instances (default) | domains | recent | name
    """
    owner = await _authorize(request)
    pool = await get_pool()
    sort_sql = {
        "instances": "instances DESC, domains_breadth DESC",
        "domains": "domains_breadth DESC, instances DESC",
        "recent": "last_used DESC",
        "name": "tool_name ASC",
    }[sort]

    # $1 — владелец (NULL снимает фильтр), дальше опциональный тип и лимит.
    args: list = [owner]
    type_clause = ""
    if type_filter:
        args.append(type_filter)
        type_clause = f"AND tool_type = ${len(args)}"
    args.append(limit)
    limit_param = f"${len(args)}"
    owner_clause = "AND ($1::uuid IS NULL OR owner_user_id = $1::uuid)"

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT
                tool_name,
                MIN(tool_type) AS tool_type,
                COUNT(*) AS instances,
                COUNT(DISTINCT domain) AS domains_breadth,
                MAX(created_at) AS last_used,
                array_agg(DISTINCT domain ORDER BY domain) AS domains,
                MIN(description) AS description
            FROM l3_tools_registry
            WHERE effective_to IS NULL
              {owner_clause}
              {type_clause}
            GROUP BY tool_name
            ORDER BY {sort_sql}
            LIMIT {limit_param}
            """,
            *args,
        )

        # Type breakdown для фильтр-чипов
        type_rows = await conn.fetch(
            f"""
            SELECT tool_type, COUNT(*) AS cnt, COUNT(DISTINCT tool_name) AS unique_tools
            FROM l3_tools_registry
            WHERE effective_to IS NULL
              {owner_clause}
            GROUP BY tool_type
            ORDER BY cnt DESC
            """,
            owner,
        )

        # Общая статистика
        totals = await conn.fetchrow(
            f"""
            SELECT
                COUNT(*) AS total_instances,
                COUNT(DISTINCT tool_name) AS unique_tools,
                COUNT(DISTINCT domain) AS distinct_domains
            FROM l3_tools_registry
            WHERE effective_to IS NULL
              {owner_clause}
            """,
            owner,
        )

    return {
        "totals": dict(totals) if totals else {},
        "by_type": [dict(r) for r in type_rows],
        "items": [
            {
                "tool_name": r["tool_name"],
                "tool_type": r["tool_type"],
                "instances": r["instances"],
                "domains_breadth": r["domains_breadth"],
                "domains": list(r["domains"]),
                "description": r["description"][:200] if r["description"] else None,
                "last_used": r["last_used"].isoformat() if r["last_used"] else None,
            }
            for r in rows
        ],
        "count": len(rows),
    }


@router.get("/knowledge")
async def list_knowledge(
    request: Request,
    domain: str | None = None,
    limit: int = Query(50, ge=1, le=500),
):
    """Просмотр активных L3-знаний владельца."""
    owner = await _authorize(request)
    pool = await get_pool()
    conds = ["effective_to IS NULL"]
    args: list = []
    if domain:
        args.append(domain)
        conds.append(f"domain = ${len(args)}")
    if owner:
        args.append(owner)
        conds.append(f"owner_user_id = ${len(args)}::uuid")
    args.append(limit)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, domain, knowledge_type, content, version, effective_from
            FROM l3_master_knowledge
            WHERE {' AND '.join(conds)}
            ORDER BY effective_from DESC LIMIT ${len(args)}
            """,
            *args,
        )
    return {
        "count": len(rows),
        "items": [
            {
                "id": str(r["id"]),
                "domain": r["domain"],
                "type": r["knowledge_type"],
                "content": r["content"],
                "version": r["version"],
                "effective_from": r["effective_from"].isoformat() if r["effective_from"] else None,
            }
            for r in rows
        ],
    }
