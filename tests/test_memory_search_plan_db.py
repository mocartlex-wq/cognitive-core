"""Exercise the actual source-search plan with underestimated cardinalities."""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

asyncpg = pytest.importorskip("asyncpg")
from app.services.memory_records import search_records

DB_URL = os.getenv("COGCORE_TEST_DB_URL") or os.getenv("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="PostgreSQL integration environment required")


@pytest.fixture
async def db():
    schema = "qa_search_plan_" + uuid4().hex
    admin = await asyncpg.connect(DB_URL)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=2,
                                    server_settings={"search_path": schema})
    async with pool.acquire() as conn:
        await conn.execute("""CREATE TABLE l1_raw_events (
            id UUID PRIMARY KEY, source_agent TEXT NOT NULL, owner_user_id UUID,
            domain TEXT NOT NULL, raw_payload JSONB NOT NULL,
            timestamp TIMESTAMPTZ NOT NULL) WITH (autovacuum_enabled=false);
            CREATE INDEX idx_owner_domain ON l1_raw_events (owner_user_id, domain, timestamp);
            CREATE INDEX idx_ru ON l1_raw_events USING gin (to_tsvector('russian', raw_payload::text));
            CREATE INDEX idx_plain ON l1_raw_events USING gin (to_tsvector('simple', raw_payload::text));
            ANALYZE l1_raw_events;""")
    try:
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


class RecordedConnection:
    """Capture and execute the SQL used by the public search function."""

    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self

    async def fetch(self, sql, *args):
        self.sql, self.args = sql, args
        return await self.connection.fetch(sql, *args)


def nodes(node):
    yield node
    for child in node.get("Plans", []):
        yield from nodes(child)


@pytest.mark.asyncio
@pytest.mark.parametrize("plan_mode", ["force_custom_plan", "force_generic_plan"])
async def test_term_scores_are_not_recomputed_for_every_source_row(db, plan_mode):
    owner = uuid4()
    now = datetime.now(timezone.utc)
    count = 960
    payload = json.dumps({"task": "акт подпись оплата срок правило",
                          "result": "Подтверждённая дата и календарь. " * 55}, ensure_ascii=False)
    async with db.acquire() as conn:
        await conn.executemany(
            "INSERT INTO l1_raw_events VALUES ($1,'producer',$2,'ai_crm',$3::jsonb,$4)",
            [(UUID(int=i + 1), owner, payload, now) for i in range(count)],
        )
        # Empty-table statistics reproduce the underestimation seen in production.
        # Allow slower CI CPUs while asserting the stable work bound below.
        # The native recall deadline is checked separately on the acceptance host.
        await conn.execute("SET statement_timeout='10000ms'")
        await conn.execute(f"SET plan_cache_mode='{plan_mode}'")
        recorded = RecordedConnection(conn)
        found = await search_records(recorded, "акт подпись оплата срок правило", str(owner), "ai_crm")
        assert [r["id"] for r in found] == [str(UUID(int=i)) for i in range(count, count - 5, -1)]
        plan = json.loads(await conn.fetchval(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + recorded.sql, *recorded.args,
        ))[0]["Plan"]
        score_evaluations = sum(n["Actual Loops"] for n in nodes(plan)
                                if n.get("Parent Relationship") == "SubPlan" and n["Node Type"] == "Aggregate")
        assert 0 < score_evaluations <= count, "Per-record score must not be recalculated per join pair"


@pytest.mark.asyncio
async def test_materialization_preserves_history_owner_domain_and_ranking(db):
    owner, outsider = uuid4(), uuid4()
    now = datetime.now(timezone.utc)
    before = now - timedelta(days=80)

    async def put(number, text, task=None, *, who=owner, domain="ai_crm", age=0, workspace="D:/other", observed=None):
        payload = {"task": text}
        if task:
            payload["checkpoint"] = {"schema_version": 1, "task_id": task,
                                     "observed_at": (observed or now).isoformat(),
                                     "workspace": workspace, "status": "blocked"}
        async with db.acquire() as conn:
            await conn.execute("INSERT INTO l1_raw_events VALUES ($1,'producer',$2,$3,$4::jsonb,$5)",
                               UUID(int=number), who, domain, json.dumps(payload), now - timedelta(days=age))

    # Score is max per historical version, never a union of terms across versions.
    await put(1, "quartz", "split", age=80, observed=before)
    await put(2, "zinnia", "split", workspace="D:/target")
    # The matching old version is outside the recent window. The returned head
    # intentionally contains neither term, and its ingestion date is older.
    await put(3, "quartz zinnia", "both", age=79, observed=before)
    await put(4, "remains blocked", "both", age=81)
    await put(5, "quartz zinnia", "both", who=outsider)
    await put(6, "quartz zinnia", "both", domain="deploy")
    await put(7, "quartz zinnia", age=80)
    await put(8, "CHECKPOINT ai_crm [legacy@aaaa] quartz zinnia", age=80)
    await put(9, "crm-test_42 testing tests", "marker")
    await put(10, "quartz zinnia", "tie-a", workspace="D:/target")
    await put(11, "quartz zinnia", "tie-b", workspace="D:/target")

    async with db.acquire() as conn:
        recorded = RecordedConnection(conn)
        found = await search_records(recorded, "quartz zinnia", str(owner), "ai_crm", workspace="D:/target")
        assert [r["id"] for r in found] == [str(UUID(int=i)) for i in [11, 10, 4, 8, 2]]
        assert str(UUID(int=3)) not in [r["id"] for r in found]
        assert (await search_records(recorded, "quartz zinnia", str(outsider), "ai_crm"))[0]["id"] == str(UUID(int=5))
        for query in ("quartz zinnia", "crm-test_42", "testing tests", "", " ".join(f"term{i}" for i in range(32))):
            for domain in ("ai_crm", None):
                for limit in (1, 5, 20):
                    current = await search_records(recorded, query, str(owner), domain, limit=limit, workspace="D:/target")
                    original = await conn.fetch(recorded.sql.replace("scores AS MATERIALIZED (", "scores AS ("), *recorded.args)
                    assert [x["id"] for x in current] == [str(x["id"]) for x in original]
