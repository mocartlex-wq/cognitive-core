"""Keep annotated sources retrievable and full skill text available on demand."""
import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest

from app.api import mcp_protocol, memory


@pytest.mark.asyncio
async def test_regular_note_keeps_chat_id_and_authenticated_author(monkeypatch):
    call = AsyncMock(return_value={"status": "accepted"})
    monkeypatch.setattr(mcp_protocol, "_resolve_agent", AsyncMock(return_value="registered-agent"))
    monkeypatch.setattr(mcp_protocol, "_call_self", call)
    await mcp_protocol._dispatch_tool(None, "cognitive_remember", {
        "domain": "work", "task": "Record the decision", "session_id": "work-chat-42",
    })
    body = call.call_args.kwargs["json_body"]
    assert body["source_agent"] == "registered-agent"
    assert body["payload"]["session_id"] == "work-chat-42"


@pytest.mark.asyncio
async def test_large_skill_discovery_and_exact_read(monkeypatch):
    rid = str(uuid4())
    original = "Run the requested check.\n" * 800
    card = {"id": rid, "record_type": "tool", "usage": {"resource_kind": "skill", "instructions": original}}
    detail = {"id": rid, "instructions": original}
    call = AsyncMock(side_effect=[{"frame": {"tools": [card.copy()], "all": [card.copy()]}}, detail])
    monkeypatch.setattr(mcp_protocol, "_call_self", call)

    found = await mcp_protocol._dispatch_tool(None, "cognitive_recall", {"query": "How can I check this?"})
    for section in ("all", "tools"):
        result = found["frame"][section][0]
        assert result["usage"]["instructions_truncated"] and result["read_tool"] == "cognitive_read_tool"
        assert original.startswith(result["usage"]["instructions"])
    assert await mcp_protocol._dispatch_tool(None, "cognitive_read_tool", {"tool_id": rid}) == detail
    assert call.call_args.args == (None, "GET", f"/tools/{rid}")


@pytest.mark.asyncio
async def test_cleanup_preserves_annotated_memory(monkeypatch):
    dsn = os.getenv("COGCORE_TEST_DB_URL")
    if not dsn:
        pytest.skip("requires isolated PostgreSQL")
    conn = await asyncpg.connect(dsn)
    try:
        # A connection-local table shadows the real event table for this cleanup.
        await conn.execute("""
            CREATE TEMP TABLE l1_raw_events (
                processed_to_l2 bool, has_owner_comment bool, created_at timestamptz);
            INSERT INTO l1_raw_events
            SELECT processed, annotated, now() - interval '10 years'
            FROM unnest(ARRAY[true,false]) AS processed
            CROSS JOIN unnest(ARRAY[true,false]) AS annotated;
        """)

        class FixturePool:
            @asynccontextmanager
            async def acquire(self):
                yield conn

        monkeypatch.setattr(memory, "verify_api_key", AsyncMock())
        monkeypatch.setattr(memory, "get_pool", AsyncMock(return_value=FixturePool()))
        result = await memory.run_cleanup(None)
        assert result["deleted_events"] == result["deleted_stale_unprocessed"] == 1
        assert await conn.fetchval("SELECT count(*) FROM l1_raw_events WHERE has_owner_comment") == 2
    finally:
        await conn.close()
