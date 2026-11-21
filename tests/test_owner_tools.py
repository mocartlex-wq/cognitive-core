import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app.api import owner_tools as api
from app.security.session import SESSION_COOKIE_NAME
from app.services import operative, shared_tools


def example():
    return {"name": "Ruler guide", "kind": "skill", "scope": "portable",
            "description": "Measure a drawing.", "instructions": "    measure(board)\n"}


def header_request():
    return Request({"type": "http", "headers": []})


def test_browser_endpoints_require_owner_session():
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        headers = {"X-Owner-User-Id": str(uuid4())}
        assert client.get("/user/shared-tools", headers=headers).status_code == 401
        assert client.get("/user/shared-tools/" + str(uuid4()), headers=headers).status_code == 401
        assert client.post("/user/shared-tools", json=example(), headers=headers).status_code == 401


def test_cookie_write_requires_same_origin():
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.require_user] = lambda: SimpleNamespace(user_id=str(uuid4()))
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, "test-cookie")
        response = client.post("/user/shared-tools", json=example(),
                               headers={"Origin": "https://unrelated.invalid", "Sec-Fetch-Site": "cross-site"})
        assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"location": "javascript:alert(42)"}, {"location": "https://user:pwd@example.invalid/tool"},
    {"instructions": "example rk_" + "A" * 24}, {"name": "invalid\x00name"},
])
async def test_unsafe_resource_rejected_before_embedding(change, monkeypatch):
    embed = AsyncMock()
    monkeypatch.setattr(api, "embed_text", embed)
    with pytest.raises(HTTPException) as ex:
        await api.add_shared_tool(api.SharedTool(**(example() | change)), header_request(),
                                  SimpleNamespace(user_id=str(uuid4())))
    assert ex.value.status_code == 422
    embed.assert_not_awaited()


def test_legacy_card_handles_local_tool_and_known_credentials():
    old = {"id": uuid4(), "domain": "engineering", "tool_name": "A local instrument",
           "tool_type": "script", "description": "Collects browser events",
           "usage_patterns": json.dumps({"local_path": "C:/tools/order.js", "recipients": "Another agent",
                                         "independent_use": "Checked on a sample", "author_check": "Bearer " + "a"*20})}
    card = shared_tools.resource_card(old, full=True)
    assert card["scope"] == "local" and card["location"] == "C:/tools/order.js"
    assert card["notes"]["recipients"] == "Another agent"
    assert "a" * 20 not in str(card)


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("COGCORE_TEST_DB_URL"), reason="needs isolated PostgreSQL integration database")
async def test_create_retry_recall_list_and_read_by_owner(monkeypatch):
    import asyncpg

    dsn = os.environ["COGCORE_TEST_DB_URL"]
    schema = "qa_shared_tools_" + uuid4().hex
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5, server_settings={"search_path": schema + ",public"})
    try:
        await pool.execute("""CREATE TABLE l3_tools_registry (LIKE public.l3_tools_registry INCLUDING ALL);
                              CREATE TABLE l3_master_knowledge (LIKE public.l3_master_knowledge INCLUDING ALL);""")
        for module in (api, shared_tools, operative):
            monkeypatch.setattr(module, "get_pool", AsyncMock(return_value=pool))
        vec = [1.] + [0.]*383
        monkeypatch.setattr(api, "embed_text", AsyncMock(return_value=vec))
        owner = SimpleNamespace(user_id=str(uuid4()))
        other = SimpleNamespace(user_id=str(uuid4()))
        payload = example()
        payload["instructions"] += "# retained\n" * 1500
        body = api.SharedTool(**payload)
        saves = await asyncio.gather(*(api.add_shared_tool(body, header_request(), owner) for _ in range(3)))
        assert sum(item["created"] for item in saves) == 1
        tool_id, = {item["item"]["id"] for item in saves}
        assert await pool.fetchval("SELECT version FROM l3_tools_registry WHERE id=$1", tool_id) == 1
        with pytest.raises(HTTPException) as conflict:
            await api.add_shared_tool(body.model_copy(update={"instructions": "different text"}), header_request(), owner)
        assert conflict.value.status_code == 409
        assert (await shared_tools.read_shared_tool(tool_id, owner.user_id))["instructions"] == payload["instructions"]
        assert await shared_tools.read_shared_tool(tool_id, other.user_id) is None
        assert await shared_tools.read_shared_tool(tool_id, "") is None
        assert (await api.add_shared_tool(body, header_request(), other))["item"]["id"] != tool_id
        await pool.execute("""INSERT INTO l3_tools_registry(domain, tool_name, tool_type, usage_patterns, owner_user_id)
                           VALUES ('art','Z scan','script','{"local_path":"C:/qa"}',$1::uuid)""", owner.user_id)
        first = await api.list_shared_tools("", 0, 1, owner)
        second = await api.list_shared_tools("", first["next_offset"], 1, owner)
        assert first["items"][0]["id"] == tool_id and second["items"][0]["domain"] == "art"
        assert second["next_offset"] is None and "instructions" not in first["items"][0]
        assert (await api.list_shared_tools("C:/qa", 0, 10, owner))["items"] == second["items"]
        # The actual owner-scoped retrieval path already sees the indexed card at insert commit.
        hits = await operative.search_pgvector(vec, "skills", 5, owner.user_id)
        assert [hit["id"] for hit in hits] == [tool_id]
        assert hits[0]["usage"]["instructions"] == payload["instructions"]
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
