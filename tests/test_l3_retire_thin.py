"""POST /memory/knowledge/retire-thin — снять L3, выведенные из одного буфера.

06.09 прогон weekly по 35 застрявшим доменам продвинул знания из ОДНОГО
L2-буфера (куратор проигнорировал порог в промпте). Порог теперь в коде,
а эта ручка убирает то, что успело пройти: мягко (effective_to), только у
своего владельца, по умолчанию dry_run, с пересборкой индекса затронутых
доменов.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI

from app.api import memory as memory_mod

OWNER = "35cc4c15-0054-477d-ad35-a7872fff7b71"


def _pool(rows):
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows)
    conn.execute = AsyncMock(return_value=f"UPDATE {len(rows)}")

    class _Acq:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *a):
            return False

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Acq())
    return pool, conn


@pytest.fixture
async def client():
    app = FastAPI()
    app.include_router(memory_mod.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


ROWS = [
    {"id": "a", "domain": "tests", "knowledge_type": "rule"},
    {"id": "b", "domain": "tests", "knowledge_type": "pattern"},
    {"id": "c", "domain": "design", "knowledge_type": "mistake"},
]


def _patches(pool, owner=OWNER):
    return (
        patch.object(memory_mod, "verify_api_key", AsyncMock(return_value="dsdsd")),
        patch("app.security.owner.resolve_owner_user_id", AsyncMock(return_value=owner)),
        patch.object(memory_mod, "get_pool", AsyncMock(return_value=pool)),
        patch.object(memory_mod, "index_domain_vectors", AsyncMock(return_value={"ok": True})),
    )


@pytest.mark.asyncio
async def test_dry_run_by_default_touches_nothing(client):
    pool, conn = _pool(ROWS)
    p1, p2, p3, p4 = _patches(pool)
    with p1, p2, p3, p4:
        r = await client.post("/memory/knowledge/retire-thin")
    body = r.json()
    assert r.status_code == 200 and body["dry_run"] is True
    assert body["matched"] == 3 and body["retired"] == 0
    assert body["by_domain"] == {"tests": 2, "design": 1}
    conn.execute.assert_not_awaited()
    assert body["reindexed"] == []


@pytest.mark.asyncio
async def test_real_run_retires_owner_scoped_and_reindexes(client):
    pool, conn = _pool(ROWS)
    p1, p2, p3, p4 = _patches(pool)
    with p1, p2, p3, p4:
        r = await client.post("/memory/knowledge/retire-thin?dry_run=false&max_sources=1&since_hours=24")
    body = r.json()
    assert body["retired"] == 3 and body["reindexed"] == ["design", "tests"]
    sql, args = conn.execute.call_args[0][0], conn.execute.call_args[0][1:]
    assert "SET effective_to = NOW()" in sql
    assert "owner_user_id = $1::uuid" in sql and "cardinality(derived_from_l2_ids)" in sql
    assert "effective_from >= NOW()" in sql, "снимаем только свежие записи"
    assert args[0] == OWNER and args[1] == 24 and args[2] == 1
    # SELECT и UPDATE — одно и то же условие: что показали в dry_run, то и снимаем
    assert conn.fetch.call_args[0][0].split("WHERE", 1)[1] == sql.split("WHERE", 1)[1]


@pytest.mark.asyncio
async def test_null_owner_is_refused(client):
    pool, conn = _pool(ROWS)
    p1, p2, p3, p4 = _patches(pool, owner=None)
    with p1, p2, p3, p4:
        r = await client.post("/memory/knowledge/retire-thin?dry_run=false")
    assert r.status_code == 403
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_bounds_are_clamped(client):
    pool, conn = _pool([])
    p1, p2, p3, p4 = _patches(pool)
    with p1, p2, p3, p4:
        r = await client.post("/memory/knowledge/retire-thin?max_sources=99&since_hours=99999")
    body = r.json()
    assert body["max_sources"] == 5 and body["since_hours"] == 24 * 30
