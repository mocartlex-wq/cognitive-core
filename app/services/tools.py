import json
from uuid import UUID, uuid4
from datetime import datetime, timezone
from app.db.postgres import get_pool
from app.models.tools import ToolRegistryInput


async def register_tool(data: ToolRegistryInput) -> UUID:
    """Добавляет инструмент в L3."""
    tool_id = uuid4()
    now = datetime.now(timezone.utc)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO l3_tools_registry
                (id, domain, tool_name, tool_type, description, config_schema,
                 usage_patterns, version, effective_from, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 1, $8, $9)
            """,
            tool_id, data.domain, data.tool_name, data.tool_type,
            data.description,
            json.dumps(data.config_schema or {}, ensure_ascii=False),
            json.dumps(data.usage_patterns or {}, ensure_ascii=False),
            now, now,
        )
    return tool_id


async def get_active_tools(domain: str) -> list[dict]:
    """Возвращает активные инструменты домена."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, domain, tool_name, tool_type, description,
                   config_schema, usage_patterns, version, created_at
            FROM l3_tools_registry
            WHERE domain = $1 AND effective_to IS NULL
            ORDER BY tool_name
            """,
            domain,
        )
        return [dict(r) for r in rows]


async def deprecate_tool(tool_id: UUID) -> None:
    """Помечает инструмент устаревшим."""
    now = datetime.now(timezone.utc)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE l3_tools_registry SET effective_to = $1 WHERE id = $2",
            now, tool_id,
        )
