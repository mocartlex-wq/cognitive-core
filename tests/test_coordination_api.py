"""HTTP and native MCP acceptance against an explicit, isolated live test stack."""
import json
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def actors():
    url = os.getenv("COGCORE_TEST_DB_URL")
    if not url:
        pytest.skip("explicit test DB required")
    asyncpg = pytest.importorskip("asyncpg")
    conn = await asyncpg.connect(url)
    identities = []
    try:
        for _ in range(2):
            token = uuid4().hex
            owner = await conn.fetchval("INSERT INTO accounts(email,email_verified) VALUES($1,true) RETURNING user_id", f"coord_{token}@cogcore.test")
            agent = "coord-test-" + token
            key, sid = "fixture-" + uuid4().hex, uuid4().hex + uuid4().hex
            await conn.execute("INSERT INTO agent_states(agent_id,owner_user_id) VALUES($1,$2)", agent, owner)
            await conn.execute("INSERT INTO agent_keys(api_key,agent_id,owner_user_id) VALUES($1,$2,$3)", key, agent, owner)
            await conn.execute("INSERT INTO sessions(session_id,user_id,expires_at) VALUES($1,$2,$3)", sid, owner, datetime.now(timezone.utc) + timedelta(hours=1))
            identities.append({"owner": owner, "agent": agent, "key": key, "sid": sid})
        yield identities
    finally:
        for identity in identities:
            await conn.execute("DELETE FROM orchestrator_tasks WHERE owner_user_id=$1", identity["owner"])
            await conn.execute("DELETE FROM agent_states WHERE agent_id=$1", identity["agent"])
            await conn.execute("DELETE FROM accounts WHERE user_id=$1", identity["owner"])
        await conn.close()


@pytest.fixture
async def api(actors):
    async with httpx.AsyncClient(base_url=os.getenv("COGCORE_TEST_API_URL", "http://localhost:8000"), timeout=20) as client:
        yield client


def create_body():
    return {"idempotency_key": uuid4().hex, "session_id": "coordinator", "project": "api-test",
            "title": "API acceptance", "description": "Exact description", "acceptance": "Verified by second session", "next_step": "Claim"}


async def test_anonymous_and_cross_owner_access_denied(api, actors):
    assert (await api.get("/coord/tasks")).status_code == 401
    owner, other = actors
    headers = {"X-API-Key": owner["key"]}
    result = await api.post("/coord/tasks", headers=headers, json=create_body())
    assert result.status_code == 201, result.text
    task = result.json()
    tid = task["task_id"]
    forged = {"X-API-Key": other["key"], "X-Owner-User-Id": str(owner["owner"])}
    assert (await api.get(f"/coord/tasks/{tid}", headers=forged)).status_code == 404
    assert (await api.get(f"/coord/tasks/{tid}/history", headers=forged)).status_code == 404
    assert (await api.get("/coord/tasks", headers=forged)).json()["tasks"] == []
    bad_action = {"idempotency_key": uuid4().hex, "session_id": "intruder", "expected_version": 0, "action": "claim"}
    assert (await api.post(f"/coord/tasks/{tid}/actions", headers=forged, json=bad_action)).status_code == 404
    assert (await api.get(f"/coord/tasks/{tid}", headers=headers)).json()["description"] == "Exact description"


async def test_browser_cookie_csrf_and_review_gate(api, actors):
    owner = actors[0]
    api.cookies.set("cogcore_session", owner["sid"])
    body = create_body()
    assert (await api.post("/coord/tasks", json=body)).status_code == 403
    assert (await api.post("/coord/tasks", headers={"Origin": "https://foreign.invalid"}, json=body)).status_code == 403
    origin = str(api.base_url).rstrip("/")
    result = await api.post("/coord/tasks", headers={"Origin": origin}, json=body)
    assert result.status_code == 201, result.text
    task = result.json()
    api.cookies.clear()
    headers = {"X-API-Key": owner["key"]}

    async def act(verb, session="executor", **kw):
        return await api.post(f"/coord/tasks/{task['task_id']}/actions", headers=headers,
                              json={"idempotency_key": uuid4().hex, "expected_version": task["version"],
                                    "session_id": session, "action": verb, **kw})

    task = (await act("claim")).json()
    result = await act("submit", attempt_id=task["attempt_id"], detail="Report", evidence=[{"uri": "qa://report", "description": "report"}])
    assert result.status_code == 200, result.text
    task = result.json()
    assert task["status"] == "review"
    proof = {"verdict": "approve", "detail": "Independent comparison", "evidence": [{"uri": "qa://review", "description": "comparison"}]}
    assert (await act("review", **proof)).status_code == 403
    result = await act("review", session="second-session", **proof)
    assert result.status_code == 200, result.text
    task = result.json()
    assert task["status"] == "verified_done"
    events = (await api.get(f"/coord/tasks/{task['task_id']}/history", headers=headers)).json()
    assert [e["action"] for e in events["events"]] == ["claim", "submit", "review"]


async def test_native_mcp_uses_same_owner_and_contract(api, actors):
    headers = {"X-API-Key": actors[0]["key"]}

    async def mcp(method, params):
        response = await api.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        assert response.status_code == 200, response.text
        data = response.json()
        assert "error" not in data, data
        return data["result"]

    names = {t["name"] for t in (await mcp("tools/list", {}))["tools"]}
    assert {"coord_task_create", "coord_task_list", "coord_task_get", "coord_task_action", "coord_task_history"} <= names
    created = await mcp("tools/call", {"name": "coord_task_create", "arguments": create_body()})
    assert not created["isError"], created
    task = created["structuredContent"]
    got = await mcp("tools/call", {"name": "coord_task_get", "arguments": {"task_id": task["task_id"]}})
    assert got["structuredContent"]["task_id"] == task["task_id"]
    assert json.loads(got["content"][0]["text"])["description"] == "Exact description"
    invalid = await api.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "coord_task_get", "arguments": {"task_id": "../../events"}}})
    assert invalid.json()["error"]["code"] == -32602
