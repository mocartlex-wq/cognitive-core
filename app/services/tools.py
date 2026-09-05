"""Реестр инструментов L3.

⚠️ Владелец здесь обязателен по той же причине, что и в конвейере L1→L3.
Без него запись становится сиротой: owner-scoped чтение её не видит, а
консолидатор рядом создаёт свою — с владельцем. На проде это дало три пары
«одинаковый инструмент, у одного владелец есть, у второго нет»
(`deepseek-chat`, `asyncpg-pool`, `redis-stack-knn`, все 15.08 16:37).
Уникальный индекс их не поймал и не мог: он по тройке
(domain, tool_name, owner_user_id), и NULL — законное третье значение.
"""
import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

from app.db.postgres import get_pool
from app.models.tools import ToolRegistryInput


async def register_tool(data: ToolRegistryInput, owner_user_id: str | None = None) -> UUID:
    """Добавляет инструмент в L3.

    ON CONFLICT — тот же, что в консолидаторе (`consolidator.py:441`): повторная
    регистрация обновляет описание и поднимает версию. Раньше конфликта не было
    вовсе — вставка шла со свежим uuid, и каждый вызов плодил близнеца.
    """
    tool_id = uuid4()
    now = datetime.now(timezone.utc)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO l3_tools_registry
                (id, domain, tool_name, tool_type, description, config_schema,
                 usage_patterns, version, effective_from, created_at, owner_user_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 1, $8, $9, $10::uuid)
            ON CONFLICT (domain, tool_name, owner_user_id) WHERE effective_to IS NULL
            DO UPDATE SET description = EXCLUDED.description,
                          config_schema = EXCLUDED.config_schema,
                          usage_patterns = EXCLUDED.usage_patterns,
                          version = l3_tools_registry.version + 1
            RETURNING id
            """,
            tool_id, data.domain, data.tool_name, data.tool_type,
            data.description,
            json.dumps(data.config_schema or {}, ensure_ascii=False),
            json.dumps(data.usage_patterns or {}, ensure_ascii=False),
            now, now, owner_user_id,
        )
    return row["id"] if row else tool_id


async def get_active_tools(domain: str, owner_user_id: str | None = None) -> list[dict]:
    """Возвращает активные инструменты домена.

    `owner_user_id=None` — админ-режим (legacy env-key), фильтр не применяется:
    контракт `app/security/owner.py`. Для запроса с ключом агента владелец
    резолвится и фильтр обязателен, иначе тенант видит чужой реестр.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if owner_user_id is None:
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
        else:
            rows = await conn.fetch(
                """
                SELECT id, domain, tool_name, tool_type, description,
                       config_schema, usage_patterns, version, created_at
                FROM l3_tools_registry
                WHERE domain = $1 AND effective_to IS NULL
                  AND owner_user_id = $2::uuid
                ORDER BY tool_name
                """,
                domain, owner_user_id,
            )
        return [dict(r) for r in rows]


async def deprecate_tool(tool_id: UUID, owner_user_id: str | None = None) -> bool:
    """Помечает инструмент устаревшим. Возвращает False, если строка не тронута.

    Без owner-фильтра любой тенант мог деактивировать чужой инструмент по id —
    id угадывать не нужно, он отдаётся в ответе на регистрацию.
    """
    now = datetime.now(timezone.utc)
    pool = await get_pool()
    async with pool.acquire() as conn:
        if owner_user_id is None:
            res = await conn.execute(
                "UPDATE l3_tools_registry SET effective_to = $1 WHERE id = $2",
                now, tool_id,
            )
        else:
            res = await conn.execute(
                "UPDATE l3_tools_registry SET effective_to = $1 "
                "WHERE id = $2 AND owner_user_id = $3::uuid",
                now, tool_id, owner_user_id,
            )
    return res.split()[-1] != "0"
