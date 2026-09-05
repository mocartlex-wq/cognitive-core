"""Реестр инструментов обязан быть owner-scoped на всех трёх операциях.

Найдено 17.08 проверкой в браузере: на главной сайта стояло «2 959 инструментов»
при 157 живых. Цифра оказалась безобидной (счётчик считал и депрецированные),
а вот путь, который к ней вёл, — нет.

`/tools` проверял только ключ и не резолвил владельца:

  POST   писал без owner_user_id  -> запись-сирота: owner-scoped чтение её не
         видит, а консолидатор рядом создаёт свою, с владельцем;
  GET    читал без фильтра        -> тенант видел чужой реестр;
  DELETE снимал по одному id      -> тенант мог деактивировать чужой инструмент,
         причём id даже угадывать не надо — он возвращается при регистрации.

На проде это дало три пары «один инструмент, у одного владелец есть, у второго
нет»: deepseek_use/deepseek-chat, fastapi_dev/asyncpg-pool,
memory_arch/redis-stack-knn — все 15.08 16:37, прогон демо-сценария.
Уникальный индекс их не поймал и не мог: он по тройке
(domain, tool_name, owner_user_id) с NULLS NOT DISTINCT, и NULL — законное
третье значение, а не «пусто».

Тот же класс, что утечка /dashboard/*, закрытая 16.08.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.models.tools import ToolRegistryInput
from app.services.tools import deprecate_tool, get_active_tools, register_tool

OWNER = "11111111-1111-1111-1111-111111111111"


def _pool(fetch_result=None, execute_result="UPDATE 1", fetchrow_result=None):
    """Пул, у которого можно спросить, с каким SQL и параметрами его звали."""
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=fetch_result or [])
    conn.execute = AsyncMock(return_value=execute_result)
    conn.fetchrow = AsyncMock(return_value=fetchrow_result)

    class _Acquire:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *a):
            return False

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Acquire())
    return pool, conn


@pytest.mark.asyncio
async def test_read_filters_by_owner():
    pool, conn = _pool(fetch_result=[])
    with patch("app.services.tools.get_pool", AsyncMock(return_value=pool)):
        await get_active_tools("memory_arch", owner_user_id=OWNER)

    sql, *params = conn.fetch.call_args[0]
    assert "owner_user_id" in sql, "чтение без owner-фильтра отдаёт чужой реестр"
    assert OWNER in params


@pytest.mark.asyncio
async def test_read_without_owner_is_admin_mode():
    """owner=None — legacy env-key, фильтр не применяется: контракт owner.py."""
    pool, conn = _pool(fetch_result=[])
    with patch("app.services.tools.get_pool", AsyncMock(return_value=pool)):
        await get_active_tools("memory_arch")

    sql, *params = conn.fetch.call_args[0]
    assert "owner_user_id" not in sql
    assert params == ["memory_arch"]


@pytest.mark.asyncio
async def test_deprecate_filters_by_owner():
    pool, conn = _pool(execute_result="UPDATE 1")
    tool_id = uuid4()
    with patch("app.services.tools.get_pool", AsyncMock(return_value=pool)):
        touched = await deprecate_tool(tool_id, owner_user_id=OWNER)

    sql, *params = conn.execute.call_args[0]
    assert "owner_user_id" in sql, "без owner-фильтра снимается чужой инструмент"
    assert OWNER in params
    assert touched is True


@pytest.mark.asyncio
async def test_deprecate_reports_untouched_row():
    """Чужой id -> UPDATE 0 -> False, чтобы ручка ответила 404, а не «ок»."""
    pool, _ = _pool(execute_result="UPDATE 0")
    with patch("app.services.tools.get_pool", AsyncMock(return_value=pool)):
        touched = await deprecate_tool(uuid4(), owner_user_id=OWNER)
    assert touched is False


@pytest.mark.asyncio
async def test_register_carries_owner_and_is_idempotent():
    """Владелец уходит в запись, повтор обновляет её, а не плодит близнеца."""
    new_id = uuid4()
    pool, conn = _pool(fetchrow_result={"id": new_id})
    data = ToolRegistryInput(domain="memory_arch", tool_name="redis-stack-knn",
                             tool_type="service")
    with patch("app.services.tools.get_pool", AsyncMock(return_value=pool)):
        got = await register_tool(data, owner_user_id=OWNER)

    sql, *params = conn.fetchrow.call_args[0]
    assert "owner_user_id" in sql, "запись без владельца становится сиротой"
    assert OWNER in params
    assert "ON CONFLICT" in sql, (
        "без ON CONFLICT каждая регистрация вставляет свежий uuid и плодит дубли — "
        "так на проде накопилось 2604 записи при 145 уникальных"
    )
    assert got == new_id
