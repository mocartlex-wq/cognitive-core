"""tools/list подсказывает домены владельца в cognitive_remember (Фаза 5б).

У владельца 55 доменов, 30+ из них одиночки (test, tests, functest, e2e_test,
имя агента…): агенты выдумывали домен на каждую запись, накопительной
консолидации нечего было копить, L3-покрытие 27.5%. Описание «e.g. fastapi_dev»
этому помогало. Теперь описание поля domain — живой список прижившихся доменов
ЭТОГО владельца; без ключа/для env-агентов — базовый текст без примера-одиночки.
"""
from __future__ import annotations

import copy
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI

from app.api import mcp_protocol
from app.api.mcp_protocol import router

OWNER = "35cc4c15-0054-477d-ad35-a7872fff7b71"


@pytest.fixture
async def client():
    app = FastAPI()
    app.include_router(router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


@pytest.fixture(autouse=True)
def _fresh_cache():
    mcp_protocol._DOMAIN_HINT_CACHE.clear()
    yield
    mcp_protocol._DOMAIN_HINT_CACHE.clear()


def _pool(rows):
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows)

    class _Acq:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *a):
            return False

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Acq())
    return pool, conn


def _domain_desc(body) -> str:
    tool = next(t for t in body["result"]["tools"] if t["name"] == "cognitive_remember")
    return tool["inputSchema"]["properties"]["domain"]["description"]


def _list():
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


@pytest.mark.asyncio
async def test_owner_sees_own_domains(client):
    pool, conn = _pool([{"domain": "cognitive_core", "w": 30}, {"domain": "deploy", "w": 12}])
    with patch.object(mcp_protocol, "_resolve_owner", AsyncMock(return_value=OWNER)), \
            patch("app.db.postgres.get_pool", AsyncMock(return_value=pool)):
        r = await client.post("/mcp", json=_list())
    desc = _domain_desc(r.json())
    assert "cognitive_core, deploy" in desc
    assert "test_/probe_" in desc
    assert "fastapi_dev" not in desc
    # SQL режет по владельцу и берёт только прижившиеся домены
    sql, args = conn.fetch.call_args[0][0], conn.fetch.call_args[0][1:]
    assert "owner_user_id = $1::uuid" in sql and "HAVING SUM(w) >= 2" in sql
    assert args[0] == OWNER


@pytest.mark.asyncio
async def test_without_owner_base_description_no_singleton_example(client):
    with patch.object(mcp_protocol, "_resolve_owner", AsyncMock(side_effect=ValueError("no key"))):
        r = await client.post("/mcp", json=_list())
    desc = _domain_desc(r.json())
    assert desc.startswith("Предметная область = проект")
    assert "e.g. fastapi_dev" not in desc and "уже есть" not in desc


@pytest.mark.asyncio
async def test_module_tools_are_not_mutated_and_cache_holds(client):
    before = copy.deepcopy(mcp_protocol.TOOLS)
    pool, conn = _pool([{"domain": "ai_crm", "w": 5}])
    with patch.object(mcp_protocol, "_resolve_owner", AsyncMock(return_value=OWNER)), \
            patch("app.db.postgres.get_pool", AsyncMock(return_value=pool)):
        for _ in range(3):
            r = await client.post("/mcp", json=_list())
    assert "ai_crm" in _domain_desc(r.json())
    assert mcp_protocol.TOOLS == before
    assert conn.fetch.await_count == 1, "подсказка должна кэшироваться на владельца"


@pytest.mark.asyncio
async def test_db_failure_does_not_break_tools_list(client):
    with patch.object(mcp_protocol, "_resolve_owner", AsyncMock(return_value=OWNER)), \
            patch("app.db.postgres.get_pool", AsyncMock(side_effect=RuntimeError("db down"))):
        r = await client.post("/mcp", json=_list())
    assert r.status_code == 200
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert "cognitive_remember" in names
