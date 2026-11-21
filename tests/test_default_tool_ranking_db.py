"""Cross-domain agent recall against pgvector and FTS with two isolated owners."""
import json
import os
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest

from app.api import operative as api
from app.services import operative as svc

DB_URL = os.getenv("COGCORE_TEST_DB_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="PostgreSQL integration environment required")


@pytest.fixture
async def catalog():
    schema = "qa_recall_" + uuid4().hex
    admin = await asyncpg.connect(DB_URL)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=6,
                                     server_settings={"search_path": schema + ",public"})
    try:
        async with pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE l3_master_knowledge (
                    id uuid, domain text, owner_user_id uuid, effective_to timestamptz,
                    knowledge_type text, content jsonb, embedding vector(3));
                CREATE TABLE l3_tools_registry (
                    id uuid, domain text, owner_user_id uuid, effective_to timestamptz,
                    tool_name text, tool_type text, description text, config_schema jsonb,
                    usage_patterns jsonb, embedding vector(3));
            """)
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest.mark.asyncio
async def test_first_question_finds_applicable_tool_and_preserves_factual_rules(catalog, monkeypatch):
    owner, outsider, tool, foreign_tool, rule = (uuid4() for _ in range(5))
    async with catalog.acquire() as conn:
        for rid, who, vector in [(tool, owner, "[.99,.1,0]"), (foreign_tool, outsider, "[1,0,0]")]:
            await conn.execute("""
                INSERT INTO l3_tools_registry
                VALUES ($1,'instruments',$2,NULL,'Spectrometer','cli','Analyse a spectrum',
                        '{}','{"command":"measure sample"}',$3::vector)
            """, rid, who, vector)
        for n in range(8):
            content = {"text": "invoice retention" if n == 0 else f"archived billing rule {n}",
                       "confidence": 0.99}
            await conn.execute("""
                INSERT INTO l3_master_knowledge VALUES ($1,'procedures',$2,NULL,'rule',$3,$4::vector)
            """, rule if n == 0 else uuid4(), owner, json.dumps(content),
                               "[0,1,0]" if n == 0 else "[.1,.9,.1]")

    measure = "How can I identify the material in this sample?"
    paraphrase = "Which wavelengths were absorbed?"
    factual = "invoice retention"
    vectors = {measure: [1, 0, 0], paraphrase: [.9, .1, 0], factual: [0, 1, 0]}
    monkeypatch.setattr(svc, "embed_text", AsyncMock(side_effect=vectors.__getitem__))
    monkeypatch.setattr(svc, "get_pool", AsyncMock(return_value=catalog))
    monkeypatch.setattr(svc, "restore_redis_from_pg", AsyncMock())
    monkeypatch.setattr(api.settings, "recall_default_domains", ["procedures", "instruments", "instruments"])

    async def recall(query, include_tools=True):
        rows = await api._search_default_domains(query, 5, include_tools, str(owner))
        ids = [r["id"] for r in rows]
        assert len(set(ids)) == len(ids) == 5
        assert str(foreign_tool) not in ids
        return api._group_results_for_agent(rows)

    for query in (measure, paraphrase):
        frame = await recall(query)
        assert [r["id"] for r in frame["tools"]] == [str(tool)]
        assert frame["tools"][0]["usage"]["command"] == "measure sample"
    assert not (await recall(measure, include_tools=False))["tools"]
    frame = await recall(factual)
    assert frame["all"][0]["id"] == str(rule)
    assert frame["tools"] == []
