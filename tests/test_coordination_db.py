"""Real PostgreSQL checks, isolated in a random schema of the explicit test DB."""
import asyncio
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.models.coordination import TaskAction, TaskCreate
from app.services import task_ledger as ledger

pytestmark = pytest.mark.asyncio
OWNER = str(uuid4())
OTHER = str(uuid4())
COORD = {"agent_id": "test-agent", "session_id": "coordinator"}
WORKER = {"agent_id": "test-agent", "session_id": "executor"}
REVIEWER = {"agent_id": "test-agent", "session_id": "reviewer"}
PROOF = [{"uri": "https://example.invalid/receipt", "description": "Independent test receipt"}]


def migration_sql(method):
    file = Path(__file__).resolve().parents[1] / "alembic/versions/20261115_1400_0027_coordination_tasks.py"
    spec = importlib.util.spec_from_file_location("coord_migration", file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sql = []
    module.op = SimpleNamespace(execute=sql.append)
    getattr(module, method)()
    return "\n".join(sql)


@pytest.fixture
async def db():
    url = os.getenv("COGCORE_TEST_DB_URL")
    if not url:
        pytest.skip("explicit COGCORE_TEST_DB_URL required")
    asyncpg = pytest.importorskip("asyncpg")
    schema = "coordtest_" + uuid4().hex
    admin = await asyncpg.connect(url)
    pool = None
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        pool = await asyncpg.create_pool(url, min_size=1, max_size=5, server_settings={"search_path": schema})
        async with pool.acquire() as conn:
            await conn.execute("CREATE TABLE accounts(user_id uuid PRIMARY KEY)")
            await conn.execute("INSERT INTO accounts VALUES ($1),($2)", UUID(OWNER), UUID(OTHER))
            await conn.execute(migration_sql("upgrade"))
        yield pool
    finally:
        if pool:
            await pool.close()
        # schema is a generated identifier owned solely by this fixture.
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def create(pool, *, owner=OWNER, **kw):
    body = TaskCreate(idempotency_key=kw.pop("idempotency_key", uuid4().hex), session_id=COORD["session_id"],
                      project="coord-test", title=kw.pop("title", "Read-only snapshot"),
                      acceptance="Snapshot agrees with Git", next_step="Claim once", description="Keep this description", **kw)
    return await ledger.create_task(pool, owner, COORD, body)


async def action(pool, task, verb, actor=WORKER, owner=OWNER, **kw):
    body = TaskAction(idempotency_key=kw.pop("idempotency_key", uuid4().hex), session_id=actor["session_id"],
                      expected_version=kw.pop("expected_version", task["version"]), action=verb, **kw)
    return await ledger.act(pool, owner, UUID(task["task_id"]), actor, body)


async def test_racing_claims_and_repeated_request(db):
    task = await create(db)
    result = await asyncio.gather(
        action(db, task, "claim", idempotency_key="first-claim"),
        action(db, task, "claim", REVIEWER, idempotency_key="second-claim"), return_exceptions=True)
    winners = [r for r in result if isinstance(r, dict)]
    losers = [r for r in result if isinstance(r, HTTPException)]
    assert len(winners) == len(losers) == 1
    assert losers[0].status_code == 409
    winner = winners[0]
    key = "first-claim" if winner["worker"] == WORKER else "second-claim"
    replay = await action(db, task, "claim", winner["worker"], idempotency_key=key)
    assert replay == winner
    assert len((await ledger.history(db, OWNER, UUID(task["task_id"])))['events']) == 1
    with pytest.raises(HTTPException, match="idempotency_key_reused"):
        await action(db, task, "claim", winner["worker"], idempotency_key=key, lease_seconds=123)


async def test_create_is_idempotent_including_parallel_calls(db):
    a, b = await asyncio.gather(create(db, idempotency_key="creation-race"), create(db, idempotency_key="creation-race"))
    assert a["task_id"] == b["task_id"]
    assert a["description"] == "Keep this description"
    with pytest.raises(HTTPException, match="idempotency_key_reused"):
        await create(db, idempotency_key="creation-race", title="Different input")


async def test_owner_isolation_and_forged_dependency(db):
    task = await create(db)
    with pytest.raises(HTTPException) as err:
        await ledger.read_task(db, OTHER, UUID(task["task_id"]))
    assert err.value.status_code == 404
    with pytest.raises(HTTPException) as err:
        await action(db, task, "claim", owner=OTHER)
    assert err.value.status_code == 404
    with pytest.raises(HTTPException, match="dependency_not_found"):
        await create(db, owner=OTHER, depends_on=[task["task_id"]])
    assert (await ledger.list_tasks(db, OTHER, None, 20))["tasks"] == []


async def test_dependencies_wait_for_independent_review(db):
    parent = await create(db)
    child = await create(db, depends_on=[parent["task_id"]])
    with pytest.raises(HTTPException, match="not_claimable"):
        await action(db, child, "claim")
    running = await action(db, parent, "claim")
    review = await action(db, running, "submit", attempt_id=running["attempt_id"], detail="Report ready", evidence=PROOF)
    with pytest.raises(HTTPException, match="not_claimable"):
        await action(db, child, "claim")
    with pytest.raises(HTTPException, match="independent_session"):
        await action(db, review, "review", verdict="approve", detail="self", evidence=PROOF)
    done = await action(db, review, "review", REVIEWER, verdict="approve", detail="Verified Git", evidence=PROOF)
    assert done["status"] == "verified_done"
    assert (await action(db, child, "claim"))["status"] == "running"
    events = (await ledger.history(db, OWNER, UUID(parent["task_id"])))['events']
    assert [e["action"] for e in events] == ["claim", "submit", "review"]


async def test_invalid_transition_is_atomic_and_receipt_can_be_retried(db):
    task = await create(db)
    with pytest.raises(HTTPException):
        await action(db, task, "submit", idempotency_key="failed-operation", detail="not claimed", evidence=PROOF)
    assert (await ledger.read_task(db, OWNER, UUID(task["task_id"])))["version"] == 0
    assert (await ledger.history(db, OWNER, UUID(task["task_id"])))['events'] == []
    assert (await action(db, task, "claim", idempotency_key="failed-operation"))["version"] == 1


async def test_keyset_pages_filter_and_invalid_cursor(db):
    tasks = [await create(db) for _ in range(5)]
    page = await ledger.list_tasks(db, OWNER, "coord-test", 2)
    seen = page["tasks"]
    while page["next_cursor"]:
        page = await ledger.list_tasks(db, OWNER, "coord-test", 2, cursor=page["next_cursor"])
        seen += page["tasks"]
    assert len({t["task_id"] for t in seen}) == 5
    await action(db, tasks[0], "claim")
    assert len((await ledger.list_tasks(db, OWNER, None, 10, status="running"))["tasks"]) == 1
    with pytest.raises(HTTPException) as err:
        await ledger.list_tasks(db, OWNER, None, 10, cursor="bad")
    assert err.value.status_code == 422


async def test_migration_keeps_legacy_and_refuses_destructive_rollback(db):
    async with db.acquire() as conn:
        legacy = uuid4()
        await conn.execute("INSERT INTO orchestrator_tasks(task_id,user_id,request_text,status) VALUES($1,'legacy','untouched','completed')", legacy)
        await conn.execute(migration_sql("downgrade"))
        await conn.execute(migration_sql("upgrade"))
        assert await conn.fetchval("SELECT status FROM orchestrator_tasks WHERE task_id=$1", legacy) == "completed"
    await create(db)
    async with db.acquire() as conn:
        with pytest.raises(Exception, match="Coordination tasks exist"):
            await conn.execute(migration_sql("downgrade"))
        assert await conn.fetchval("SELECT count(*) FROM orchestrator_tasks WHERE coord_data IS NOT NULL") == 1


async def test_retry_history_retains_evidence_reconciliation_and_old_attempt(db):
    task = await create(db)
    running = await action(db, task, "claim")
    review = await action(db, running, "submit", attempt_id=running["attempt_id"], detail="Original report", evidence=PROOF)
    rejected = await action(db, review, "review", REVIEWER, verdict="reject", detail="Wrong file", evidence=PROOF)
    retry = await action(db, rejected, "retry", COORD, reconciliation="Old artifact inspected, no external effect")
    assert retry["evidence"] == []
    page = await ledger.history(db, OWNER, UUID(task["task_id"]), limit=2)
    assert page["events"][-1]["reconciliation"] == "Old artifact inspected, no external effect"
    assert page["events"][0]["review"]["verdict"] == "reject"
    older = await ledger.history(db, OWNER, UUID(task["task_id"]), limit=2, before_version=page["next_cursor"])
    submitted = older["events"][-1]
    assert submitted["result"] == "Original report"
    assert submitted["evidence"] == PROOF
    assert submitted["attempt_id"] == running["attempt_id"]
    assert older["next_cursor"] is None


async def test_rollback_guard_serializes_with_uncommitted_first_task(db):
    async with db.acquire() as writer, db.acquire() as rollback, db.acquire() as observer:
        tx = writer.transaction()
        await tx.start()
        await writer.execute("INSERT INTO orchestrator_tasks(user_id,request_text,coord_data) VALUES('test','pending','{}'::jsonb)")
        rollback_pid = await rollback.fetchval("SELECT pg_backend_pid()")
        operation = asyncio.create_task(rollback.execute(migration_sql("downgrade")))
        try:
            # Wait until the actual DB session, not just a Python task, blocks.
            for _ in range(30):
                if await observer.fetchval("SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=$1", rollback_pid):
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("Downgrade did not wait for the creating transaction")
            await tx.commit()
            with pytest.raises(Exception, match="Coordination tasks exist"):
                await operation
            assert await writer.fetchval("SELECT count(*) FROM orchestrator_tasks WHERE coord_data IS NOT NULL") == 1
        finally:
            if not operation.done():
                await tx.rollback()
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
