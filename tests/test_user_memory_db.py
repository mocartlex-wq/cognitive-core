"""Exercise the cabinet with real sessions for two owners in the designated QA DB."""
import asyncio
import json
import os
import secrets
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import httpx
import pytest
from fastapi import FastAPI

from app.api import user_memory as memory
from app.security import session


@pytest.mark.asyncio
async def test_private_memory_feed_and_retryable_comments(monkeypatch, test_account_session):
    pool = await asyncpg.create_pool(os.environ["COGCORE_TEST_DB_URL"], min_size=1, max_size=8)
    agent = f"qa-mem-{uuid4().hex}"
    owner = test_account_session["user_id"]
    outsider = uuid4()
    older, foreign, hidden, foreign_label = uuid4(), uuid4(), uuid4(), uuid4()
    ids = []
    app = FastAPI()
    app.include_router(memory.router)
    monkeypatch.setattr(memory, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(session, "get_pool", AsyncMock(return_value=pool))
    try:
        admin_account_session = {"user_id": str(outsider), "session_id": secrets.token_hex(32)}
        await pool.execute("INSERT INTO accounts(user_id,email,is_admin,email_verified) VALUES($1,$2,true,true)",
                           outsider, f"qa-{outsider}@test.invalid")
        await pool.execute("INSERT INTO sessions(session_id,user_id,expires_at) VALUES($1,$2,now() + interval '1 day')",
                           admin_account_session["session_id"], outsider)
        await pool.execute("INSERT INTO agent_states(agent_id,owner_user_id,agent_label) VALUES($1,$2,'Bench Agent')", agent, owner)
        long_result = "water 85%_coupon " + "x" * 800
        async def seed(rid, who, domain, payload, age=0):
            ids.append(rid)
            await pool.execute("""
                INSERT INTO l1_raw_events(id,source_agent,owner_user_id,domain,timestamp,raw_payload)
                VALUES($1,$2,$3,$4,'2026-01-01'::timestamptz - ($5::int * interval '1 day'),$6::jsonb)
            """, rid, agent, who, domain, age, json.dumps(payload))

        await seed(older, owner, "work", {"task": "Survey", "result": long_result, "lessons": "datum",
                                         "checkpoint": {"schema_version": 1, "task_id": "qa-task", "status": "completed", "session_id": "qa-chat",
                                                        "observed_at": "2025-12-30T00:00:00Z", "next_step": "verify specimen",
                                                        "evidence": ["qa-proof"]}}, age=2)
        await seed(foreign, outsider, "work", {"task": "private", "result": "outsider-only"})
        await seed(hidden, owner, "agent_inbox", {"task": "inbox", "result": "must stay hidden"})
        await seed(foreign_label, owner, "work", {"task": "internal", "result": "secret", "actor": "internal"})
        await seed(uuid4(), owner, "media_analysis", {"kind": "video", "task": "transcode", "result": "temporary URL"})
        for n in range(19):
            await seed(uuid4(), owner, "media_analysis" if n == 0 else "work", {"kind": "session_note", "session_id": "other-chat",
                                               "task": f"note {n}", "result": "ordinary", "private_metadata": "not-in-output"})

        url = "/user/memory/records"
        base = "https://cabinet.test"
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=base) as client:
            def sign(account):
                client.cookies.set(session.SESSION_COOKIE_NAME, account["session_id"])

            sign(test_account_session)
            first = await client.get(url, params={"limit": 15})
            assert first.status_code == 200, first.text
            assert "no-store" in first.headers["cache-control"]
            assert "not-in-output" not in first.text
            first = first.json()
            assert len(first["items"]) == 15 and first["next_cursor"]
            second = (await client.get(url, params={"cursor": first["next_cursor"], "limit": 15})).json()
            visible = first["items"] + second["items"]
            assert len({r["id"] for r in visible}) == len(visible) == 20
            assert [r["task"] for r in visible if r["domain"] == "media_analysis"] == ["note 0"]
            assert second["items"][-1]["id"] == str(older) and not second["next_cursor"]
            filters = {"record_id": str(older), "q": "85%_", "domain": "work", "agent": "bench agent", "session_id": "qa-chat"}
            found = (await client.get(url, params=filters)).json()["items"]
            assert len(found) == 1 and found[0]["truncated"] and found[0]["status"] == "completed"
            assert (await client.get(url, params={**filters, "q": "%missing"})).json()["items"] == []
            assert (await client.get(url, params={**filters, "agent": agent})).json()["items"] == found
            detail = (await client.get(f"{url}/{older}")).json()
            assert detail["result"] == long_result and not detail["truncated"]
            assert detail["is_latest"] and detail["latest_id"] == str(older)
            assert detail["evidence"] == ["qa-proof"] and detail["next_step"] == "verify specimen"
            comments = f"{url}/{older}/comments"
            before_write = await pool.fetchrow("SELECT has_owner_comment,raw_payload::text AS raw FROM l1_raw_events WHERE id=$1", older)
            assert not before_write["has_owner_comment"]
            key = str(uuid4())
            body = {"request_id": key, "text": " confirmed "}
            assert (await client.post(comments, json=body, headers={"Origin": "https://other.test"})).status_code == 403
            assert (await client.post(comments, json={**body, "owner_user_id": str(outsider)}, headers={"Origin": base})).status_code == 422
            client.headers["Origin"] = base
            replies = await asyncio.gather(*(client.post(comments, json=body) for _ in range(3)))
            assert all(r.status_code == 200 for r in replies), [r.text for r in replies]
            assert len({r.json()["id"] for r in replies}) == 1
            assert await pool.fetchval("SELECT has_owner_comment FROM l1_raw_events WHERE id=$1", older)
            assert replies[0].json()["text"] == "confirmed"
            assert (await client.post(comments, json={**body, "text": "changed"})).status_code == 409
            distinct = await asyncio.gather(*(client.post(comments, json={"text": val, "request_id": str(uuid4())})
                                               for val in ("second", "third")))
            assert all(r.status_code == 200 for r in distinct)
            page = (await client.get(comments, params={"limit": 1})).json()
            rest = (await client.get(comments, params={"cursor": page["next_cursor"]})).json()["items"]
            assert {r["text"] for r in page["items"] + rest} == {"confirmed", "second", "third"}
            assert rest[-1] == replies[0].json()
            assert (await client.get(f"{url}/{older}")).json() == {**detail, "comment_count": 3}
            assert await pool.fetchval("SELECT raw_payload::text FROM l1_raw_events WHERE id=$1", older) == before_write["raw"]
            for invisible in (hidden, foreign, foreign_label):
                assert (await client.get(f"{url}/{invisible}")).status_code == 404
                assert (await client.get(f"{url}/{invisible}/comments")).status_code == 404
                assert (await client.post(f"{url}/{invisible}/comments", json=body)).status_code == 404
            successor = uuid4()
            await seed(successor, owner, "work", {"task": "Survey follow-up", "result": "reopened",
                                                  "checkpoint": {"schema_version": 1, "task_id": "qa-task", "session_id": "qa-chat",
                                                                 "observed_at": "2026-01-01T00:00:00Z", "status": "in_progress"}})
            historical = (await client.get(f"{url}/{older}")).json()
            assert historical["is_latest"] is False and historical["latest_id"] == str(successor)
            assert historical["comment_count"] == 3
            sign(admin_account_session)
            assert (await client.get(comments)).status_code == 404
            assert (await client.post(comments, json=body)).status_code == 404
            private = (await client.get(f"{url}/{foreign}")).json()
            assert private["agent_label"] is None and private["result"] == "outsider-only"
            assert (await client.post(f"{url}/{foreign}/comments", json=body)).status_code == 200
    finally:
        await pool.execute("DELETE FROM l1_raw_events WHERE id=ANY($1::uuid[])", ids)
        await pool.execute("DELETE FROM agent_states WHERE agent_id=$1 AND owner_user_id=$2", agent, owner)
        await pool.execute("DELETE FROM accounts WHERE user_id=$1", outsider)
        await pool.close()
