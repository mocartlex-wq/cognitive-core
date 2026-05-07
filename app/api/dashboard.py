"""Read-only dashboard endpoints (для встроенной web UI)."""
from fastapi import APIRouter, Query
from app.db.postgres import get_pool

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/recent-events")
async def recent_events(limit: int = Query(50, ge=1, le=500), domain: str | None = None):
    """Последние L1-события."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if domain:
            rows = await conn.fetch(
                """
                SELECT id, timestamp, source_agent, domain, raw_payload, processed_to_l2
                FROM l1_raw_events WHERE domain = $1
                ORDER BY timestamp DESC LIMIT $2
                """,
                domain, limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, timestamp, source_agent, domain, raw_payload, processed_to_l2
                FROM l1_raw_events ORDER BY timestamp DESC LIMIT $1
                """,
                limit,
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
async def audit_tail(limit: int = Query(100, ge=1, le=1000), only_failures: bool = False):
    """Последние записи аудит-лога (L5)."""
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
async def list_domains():
    """Все домены, встречающиеся в L1/L2/L3, со счётчиками."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT domain,
                   (SELECT COUNT(*) FROM l1_raw_events WHERE domain = d.domain) AS l1,
                   (SELECT COUNT(*) FROM l2_daily_buffers WHERE domain = d.domain) AS l2,
                   (SELECT COUNT(*) FROM l3_master_knowledge WHERE domain = d.domain AND effective_to IS NULL) AS l3_active,
                   (SELECT COUNT(*) FROM l3_tools_registry WHERE domain = d.domain AND effective_to IS NULL) AS tools_active
            FROM (
                SELECT DISTINCT domain FROM l1_raw_events
                UNION SELECT DISTINCT domain FROM l2_daily_buffers
                UNION SELECT DISTINCT domain FROM l3_master_knowledge
            ) d
            ORDER BY l1 DESC
            """
        )
    return {
        "count": len(rows),
        "items": [dict(r) for r in rows],
    }


@router.get("/timeline")
async def timeline(days: int = Query(7, ge=1, le=90)):
    """Активность по дням за последние N дней (для графика)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        l1 = await conn.fetch(
            """
            SELECT DATE(timestamp) AS day, COUNT(*) AS cnt
            FROM l1_raw_events WHERE timestamp >= CURRENT_DATE - $1::int
            GROUP BY day ORDER BY day
            """,
            days,
        )
        l2 = await conn.fetch(
            """
            SELECT date AS day, COUNT(*) AS cnt
            FROM l2_daily_buffers WHERE date >= CURRENT_DATE - $1::int
            GROUP BY day ORDER BY day
            """,
            days,
        )
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
    pool = await get_pool()
    sort_sql = {
        "instances": "instances DESC, domains_breadth DESC",
        "domains": "domains_breadth DESC, instances DESC",
        "recent": "last_used DESC",
        "name": "tool_name ASC",
    }[sort]

    type_clause = "AND tool_type = $1" if type_filter else ""
    args = [type_filter, limit] if type_filter else [limit]
    limit_param = "$2" if type_filter else "$1"

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
              {type_clause}
            GROUP BY tool_name
            ORDER BY {sort_sql}
            LIMIT {limit_param}
            """,
            *args,
        )

        # Type breakdown для фильтр-чипов
        type_rows = await conn.fetch(
            """
            SELECT tool_type, COUNT(*) AS cnt, COUNT(DISTINCT tool_name) AS unique_tools
            FROM l3_tools_registry
            WHERE effective_to IS NULL
            GROUP BY tool_type
            ORDER BY cnt DESC
            """
        )

        # Общая статистика
        totals = await conn.fetchrow(
            """
            SELECT
                COUNT(*) AS total_instances,
                COUNT(DISTINCT tool_name) AS unique_tools,
                COUNT(DISTINCT domain) AS distinct_domains
            FROM l3_tools_registry
            WHERE effective_to IS NULL
            """
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
async def list_knowledge(domain: str | None = None, limit: int = Query(50, ge=1, le=500)):
    """Просмотр активных L3-знаний."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if domain:
            rows = await conn.fetch(
                """
                SELECT id, domain, knowledge_type, content, version, effective_from
                FROM l3_master_knowledge
                WHERE domain = $1 AND effective_to IS NULL
                ORDER BY effective_from DESC LIMIT $2
                """,
                domain, limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, domain, knowledge_type, content, version, effective_from
                FROM l3_master_knowledge WHERE effective_to IS NULL
                ORDER BY effective_from DESC LIMIT $1
                """,
                limit,
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
