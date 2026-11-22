"""Full ledger tests in an isolated PostgreSQL schema. All ballots below are synthetic."""

import asyncio
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import ValidationError

from app.api import mcp_protocol, team_rule_tools, team_rules
from app.models.team_rules import Case, Rule
from app.security import middleware
from app.services import team_rules as ledger


@pytest_asyncio.fixture
async def env(monkeypatch):
    dsn = os.environ.get("COGCORE_TEST_DB_URL")
    if not dsn:
        pytest.skip("Set COGCORE_TEST_DB_URL for PostgreSQL integration")
    schema = "qa_rules_" + uuid4().hex
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=6, server_settings={"search_path": schema})
    first, other = uuid4(), uuid4()
    try:
        await pool.execute("CREATE TABLE accounts(user_id uuid PRIMARY KEY)")
        await pool.execute("INSERT INTO accounts VALUES($1),($2)", first, other)
        path = next((Path(__file__).resolve().parents[1] / "alembic" / "versions").glob("*0031_team_rule_decisions.py"))
        module = importlib.util.module_from_spec(spec := importlib.util.spec_from_file_location("team_migration", path))
        spec.loader.exec_module(module)
        statements = []
        monkeypatch.setattr(module, "op", SimpleNamespace(execute=statements.append))
        module.upgrade()
        for sql in statements:
            await pool.execute(sql)

        async def get_pool():
            return pool

        async def verify(cookie):
            ids = {"owner-cookie": first, "other-cookie": other}
            return SimpleNamespace(user_id=ids[cookie], email="fixture@example.invalid") if cookie in ids else None

        monkeypatch.setattr(team_rules, "get_pool", get_pool)
        monkeypatch.setattr(team_rule_tools, "get_pool", get_pool)
        monkeypatch.setattr(middleware, "verify_session", verify)
        app = FastAPI()
        app.include_router(team_rules.router)
        app.include_router(mcp_protocol.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test.local", headers={"Origin": "https://test.local"}) as client:
            yield client, str(first), str(other)
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def call(tool, owner, args=None, actor="pilot-agent"):
    return await team_rule_tools.dispatch("cognitive_" + tool, args or {}, owner, actor)


def owner_headers(other=False):
    return {"Cookie": "cogcore_session=" + ("other-cookie" if other else "owner-cookie")}


@pytest.mark.asyncio
async def test_two_scopes_and_concurrent_ballots(env):
    client, owner, other = env
    key = (await call("team_rules", owner))["cases"][0]["case_id"]
    assert (await call("team_rules", other))["cases"][0]["case_id"] != key

    def action():
        return {"case_id": key, "instance_id": str(uuid4())}

    once = action()
    first, second = await asyncio.gather(*(call("resolve_team_rules", owner, once) for _ in range(2)))
    assert {first["ask_owner"], second["ask_owner"]} == {False, True}
    assert first["conflict_id"] == second["conflict_id"] and len(first["options"]) == 2
    url = "/user/team-decisions/" + first["conflict_id"]
    assert first["owner_url"].endswith(first["conflict_id"])
    payload = {"duration": "once", "choice": "table_first"}
    assert (await client.post(url + "/answer", json=payload, headers={"X-API-Key": "test-agent"})).status_code == 401
    assert (await client.post(url + "/answer", json=payload, headers={"X-Session-Id": "owner-cookie"})).status_code == 401
    assert (await client.post(url + "/answer", json=payload, headers={**owner_headers(), "Origin": "https://other.local"})).status_code == 403
    assert (await client.get(url, headers=owner_headers(True))).status_code == 404
    assert (await client.post(url + "/answer", json=payload, headers=owner_headers(True))).status_code == 404
    chosen = await client.post(url + "/answer", json=payload, headers=owner_headers())
    assert chosen.status_code == 200 and chosen.json()["choice"] == "table_first"
    assert (await client.post(url + "/answer", json=payload, headers=owner_headers())).json() == chosen.json()
    assert (await client.post(url + "/answer", json={**payload, "choice": "diagram_first"}, headers=owner_headers())).status_code == 409
    assert (await call("resolve_team_rules", owner, once))["duration"] == "once"

    future = await call("resolve_team_rules", owner, action())
    assert future["ask_owner"]
    url2 = "/user/team-decisions/" + future["conflict_id"]
    assert (await client.post(url2 + "/answer", json={"duration": "future", "choice": "diagram_first"}, headers=owner_headers())).status_code == 200
    fresh = await call("resolve_team_rules", owner, action(), actor="different-agent")
    assert (fresh["choice"], fresh["duration"], fresh["source_id"]) == ("diagram_first", "inherited", future["conflict_id"])
    assert (await call("resolve_team_rules", other, once))["_status"] == 404
    for scope in [{"project": "another"}, {"conditions": ["other"]}, {"role": "Другой участник"}, {"team": "other"}]:
        config = {**ledger.DEMO.model_dump(), **scope, "agents": ["different-agent"]}
        registered = await client.post("/user/team-decisions/cases", json=config, headers=owner_headers())
        sibling = {**action(), "case_id": registered.json()["case_id"]}
        assert (await call("resolve_team_rules", owner, sibling))["_status"] == 404
        assert (await call("resolve_team_rules", owner, sibling, actor="different-agent"))["ask_owner"]
    after = await client.post(url2 + "/revoke", json={}, headers=owner_headers())
    assert after.json()["revoked_at"]
    assert (await call("resolve_team_rules", owner, action()))["ask_owner"]
    assert (await call("resolve_team_rules", owner, once))["choice"] == "table_first"
    with pytest.raises(ValidationError):
        await call("resolve_team_rules", owner, {**once, "role": "admin", "choice": "table_first"})


@pytest.mark.asyncio
async def test_weight_log_and_compatible_rules(env):
    client, owner, _ = env
    cfg = ledger.DEMO.model_dump()
    cfg["project"] = "fixture"
    for r in cfg["rules"]:
        r.update(choice="diagram_first", weight=9)
    key = (await client.post("/user/team-decisions/cases", json=cfg, headers=owner_headers())).json()["case_id"]
    run = await call("resolve_team_rules", owner, {"case_id": key, "instance_id": str(uuid4())})
    assert not run["ask_owner"] and run["duration"] == "rules" and len(run["assessment"]["applied_rule_ids"]) == 2
    feedback = {"id": str(uuid4()), "rule_id": run["assessment"]["applied_rule_ids"][0], "delta": 1, "reason": "Проверено на демонстрационном материале", "evidence_run_id": run["conflict_id"]}
    path = "/user/team-decisions/cases/" + key + "/feedback"

    async def vote(body):
        return await client.post(path, json=body, headers=owner_headers())

    first, repeat = await asyncio.gather(vote(feedback), vote(feedback))
    assert first.json() == repeat.json() and first.json()["new_weight"] == 10 and first.json()["old_weight"] == 9
    assert (await vote({**feedback, "delta": -1})).status_code == 409
    assert (await vote({**feedback, "id": str(uuid4())})).status_code == 409
    assert (await vote({**feedback, "id": str(uuid4()), "evidence_run_id": str(uuid4())})).status_code == 422
    next_run = await call("resolve_team_rules", owner, {"case_id": key, "instance_id": str(uuid4())})
    saturated = await vote({**feedback, "id": str(uuid4()), "evidence_run_id": next_run["conflict_id"]})
    assert (saturated.json()["old_weight"], saturated.json()["new_weight"]) == (10, 10)
    history = await call("team_rule_history", owner, {"case_id": key})
    assert len(history["recent_feedback"]) == 2 and len(history["decisions"]) == 2
    exact = await call("team_rule_history", owner, {"case_id": key, "feedback_id": feedback["id"]})
    child = {"address": exact["address"] + ".10", "weight": 8, "reason": "Уточнение", "choice": "table_first", "when": ["Демонстрация"]}
    route = "/user/team-decisions/cases/" + key + "/refinements"
    refined = await client.post(route, json=child, headers=owner_headers())
    assert refined.status_code == 200 and refined.json()["refined_at"]
    assert (await client.post(route, json=child, headers=owner_headers())).json() == refined.json()
    assert (await client.post(route, json={**child, "address": exact["address"] + ".11", "when": ["чужая область"]}, headers=owner_headers())).status_code == 422
    new_run = await call("resolve_team_rules", owner, {"case_id": key, "instance_id": str(uuid4())})
    assert new_run["ask_owner"] and refined.json()["rule_id"] in new_run["assessment"]["applied_rule_ids"]
    assert (await call("resolve_team_rules", owner, {"case_id": key, "instance_id": run["instance_id"]}))["choice"] == "diagram_first"
    assert exact["source"] == "owner_cabinet" and exact["reason"] == feedback["reason"] and exact["version"] == 2


def test_addresses_and_bounded_action():
    spec = ledger.DEMO.model_dump()
    spec["conditions"] += ["С вложением"]
    root = Rule(address="10", choice="table_first", reason="Исходное правило", weight=10)
    children = [root.model_copy(update={"address": "10." + str(i), "when": ["С вложением"]}) for i in (1, 10, 19)]
    parsed = Case.model_validate({**spec, "rules": [root, *children]})
    rules = {r.address: {**r.model_dump(), "rule_id": r.address} for r in parsed.rules}
    assert ledger.assess(rules)["applied_rule_ids"] == ["10.1", "10.10", "10.19"]
    with pytest.raises(ValidationError):
        Case.model_validate({**spec, "action": "publish"})
    with pytest.raises(ValidationError):
        Case.model_validate({**spec, "rules": [root, root.model_copy(update={"address": "10.1"})]})
    names = {tool["name"] for tool in mcp_protocol.TOOLS}
    assert set(team_rule_tools.CATALOG) <= names and not any("answer_team_rule" in name for name in names)


@pytest.mark.asyncio
async def test_discover_and_resume_over_mcp(env, monkeypatch):
    from unittest.mock import AsyncMock

    from app.security import owner as owner_security

    client, owner, _ = env
    monkeypatch.setattr(mcp_protocol, "_owner_domains", AsyncMock(return_value=[]))
    monkeypatch.setattr(mcp_protocol, "_rate_limit_ok", AsyncMock(return_value=True))
    monkeypatch.setattr(mcp_protocol, "_load_keys", lambda: {"fixture-agent": "fixture-key"})
    monkeypatch.setattr(owner_security, "env_agent_owner", lambda: owner)

    async def rpc(method, params, headers=None):
        msg = {"jsonrpc": "2.0", "method": method, "id": 1, "params": params}
        reply = await client.post("/mcp", json=msg, headers=headers or {"X-API-Key": "fixture-key"})
        return reply.json()

    names = [tool["name"] for tool in (await rpc("tools/list", {}))["result"]["tools"]]
    assert set(team_rule_tools.CATALOG) <= set(names)

    async def use(name, args):
        return (await rpc("tools/call", {"name": name, "arguments": args}))["result"]["structuredContent"]

    case = (await use("cognitive_team_rules", {}))["cases"][0]
    args = {"case_id": case["case_id"], "instance_id": str(uuid4())}
    question = await use("cognitive_resolve_team_rules", args)
    assert question["ask_owner"] and question["opened_by"] == "fixture-agent"
    assert question["decision_source"] is None
    bad = await rpc("tools/call", {"name": "cognitive_resolve_team_rules", "arguments": {**args, "owner_confirmed": True}})
    assert "error" in bad
    path = "/user/team-decisions/" + question["conflict_id"] + "/answer"
    assert (await client.post(path, json={"choice": "diagram_first", "duration": "once"}, headers=owner_headers())).status_code == 200
    follow = await use("cognitive_resolve_team_rules", args)
    assert follow["decision_source"] == "owner_cabinet" and follow["choice"] == "diagram_first"
    assert (await use("cognitive_team_rule_history", {"case_id": case["case_id"]}))["decisions"][0]["decided_at"]
