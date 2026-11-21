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


@pytest.fixture
async def registry(monkeypatch):
    if not os.getenv("COGCORE_TEST_DB_URL"):
        pytest.skip("needs isolated PostgreSQL integration database")
    import asyncpg

    dsn = os.environ["COGCORE_TEST_DB_URL"]
    schema = "qa_shared_tools_" + uuid4().hex
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5, server_settings={"search_path": schema + ",public"})
    try:
        await pool.execute("""CREATE TABLE l3_tools_registry (LIKE public.l3_tools_registry INCLUDING ALL);
                              CREATE TABLE weekly_applied_windows (LIKE public.weekly_applied_windows INCLUDING ALL);
                              CREATE TABLE l4_snapshots (LIKE public.l4_snapshots INCLUDING ALL);
                              CREATE TABLE l3_master_knowledge (LIKE public.l3_master_knowledge INCLUDING ALL);""")
        for module in (api, shared_tools, operative):
            monkeypatch.setattr(module, "get_pool", AsyncMock(return_value=pool))
        vec = [1.] + [0.]*383
        monkeypatch.setattr(api, "embed_text", AsyncMock(return_value=vec))
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest.mark.asyncio
async def test_create_retry_recall_list_and_read_by_owner(registry):
    pool = registry
    vec = [1.] + [0.]*383
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

@pytest.mark.asyncio
async def test_manual_card_survives_agent_weekly_and_monthly(registry, monkeypatch):
    from app.api import tools as routes
    from app.models.tools import ToolRegistryInput
    from app.services import consolidator, tools

    for module in (consolidator, tools):
        monkeypatch.setattr(module, "get_pool", AsyncMock(return_value=registry))
    owner = SimpleNamespace(user_id=str(uuid4()))
    neighbor = str(uuid4())
    manual = await api.add_shared_tool(api.SharedTool(**example()), header_request(), owner)
    tool_id = manual["item"]["id"]
    tool = ToolRegistryInput(domain="skills", tool_name=example()["name"], tool_type="service", description="generated")
    monkeypatch.setattr(routes, "_authorize", AsyncMock(return_value=owner.user_id))
    with pytest.raises(HTTPException) as collision:
        await routes.create_tool(tool, header_request())
    assert collision.value.status_code == 409
    neighbor_id = await tools.register_tool(tool, neighbor)
    assert neighbor_id == await tools.register_tool(tool, neighbor)
    assert str(neighbor_id) != tool_id
    proposals = [{"tool_name": tool.tool_name, "usage_pattern":"generated"},
                 {"tool_name":"weekly-created", "usage_pattern":"ordinary"}]
    analyze = AsyncMock(return_value={"tools":proposals})
    monkeypatch.setattr(consolidator, "pre_weekly_check", AsyncMock(return_value={}))
    monkeypatch.setattr(consolidator, "analyze_weekly", analyze)
    result = await consolidator._weekly_for_owner("skills", owner.user_id, [{"id":uuid4()} for _ in range(3)])
    assert result["tools_added"] == 1
    assert analyze.await_args.args[2] == []
    ordinary = await registry.fetchval("SELECT id FROM l3_tools_registry WHERE tool_name='weekly-created'")
    assert ordinary is not None
    audit = AsyncMock(return_value={"dead_tool_ids":[tool_id, str(ordinary), str(ordinary)]})
    monkeypatch.setattr(consolidator, "monthly_audit", audit)
    monkeypatch.setattr(consolidator, "index_domain_vectors", AsyncMock(return_value={"total":2}))
    invalidated = AsyncMock()
    monkeypatch.setattr(operative, "_remove_indexed_vectors", invalidated)
    result = await consolidator.run_monthly_audit("skills")
    assert all(str(row["id"]) != tool_id for row in audit.await_args.args[2])
    assert result["dead_tool_ids"] == [str(ordinary)]
    invalidated.assert_awaited_once_with([str(ordinary)])
    assert await shared_tools.read_shared_tool(tool_id, owner.user_id) == manual["item"]
    assert await shared_tools.read_shared_tool(tool_id, neighbor) is None
    assert await shared_tools.read_shared_tool(neighbor_id, neighbor)
    assert not await tools.deprecate_tool(tool_id, neighbor)
    assert await tools.deprecate_tool(tool_id, owner.user_id)
    assert await shared_tools.read_shared_tool(tool_id, owner.user_id) is None


@pytest.mark.asyncio
async def test_real_snapshot_roundtrip_preserves_manual_body_and_old_restore_cannot_replace_it(registry, monkeypatch):
    import hashlib
    import io
    from unittest.mock import MagicMock

    from app.api import memory
    from app.security import owner as owner_security
    from app.services import consolidator

    owner = SimpleNamespace(user_id=str(uuid4()))
    for module in (memory, consolidator):
        monkeypatch.setattr(module, "get_pool", AsyncMock(return_value=registry))
    monkeypatch.setattr(memory, "verify_api_key", AsyncMock())
    monkeypatch.setattr(owner_security, "resolve_owner_user_id", AsyncMock(return_value=owner.user_id))
    s3 = MagicMock()
    objects = {}
    s3.put_object.side_effect = lambda **kwargs: objects.update({kwargs["object_name"]:kwargs["data"].read()})
    s3.get_object.side_effect = lambda bucket,key: io.BytesIO(objects[key])
    for module in (memory, consolidator):
        monkeypatch.setattr(module, "get_s3", lambda: s3)
    body = await api.add_shared_tool(api.SharedTool(**example()), header_request(), owner)
    card = body["item"]
    snapshot = str(await consolidator._maybe_snapshot("skills"))
    key, = objects
    saved = json.loads(objects[key])
    assert saved["tools"][0]["owner_managed"] is True
    # Use an integrity-approved full-blob snapshot. Legacy content-hash verification
    # is a separate compatibility issue outside the registry lifecycle.
    await registry.execute("UPDATE l4_snapshots SET snapshot_hash=$1 WHERE id=$2",
                           hashlib.sha256(objects[key]).hexdigest(), snapshot)
    await registry.execute("DELETE FROM l3_tools_registry WHERE id=$1", card["id"])
    result = await memory.restore_snapshot(snapshot, header_request())
    assert result["status"] == "restored" and result["tools_count"] == 1
    assert await shared_tools.read_shared_tool(card["id"], owner.user_id) == card
    assert len(await operative.search_pgvector([1.]+[0.]*383, "skills", 3, owner.user_id)) == 1
    # Old full-blob backup has no authorship flag. Same UUID must not overwrite the new card.
    legacy = saved["tools"][0]
    legacy.pop("owner_managed")
    legacy["usage_patterns"] = {"instructions":"older text"}
    ordinary = dict(legacy, id=str(uuid4()), tool_name="old-ordinary")
    saved["tools"].append(ordinary)
    objects[key] = json.dumps(saved).encode()
    await registry.execute("UPDATE l4_snapshots SET snapshot_hash=$1, total_tools=2 WHERE id=$2",
                           hashlib.sha256(objects[key]).hexdigest(), snapshot)
    result = await memory.restore_snapshot(snapshot, header_request())
    assert result["tools_count"] == 1
    assert await shared_tools.read_shared_tool(card["id"], owner.user_id) == card
    assert not await registry.fetchval("SELECT owner_managed FROM l3_tools_registry WHERE id=$1", ordinary["id"])
