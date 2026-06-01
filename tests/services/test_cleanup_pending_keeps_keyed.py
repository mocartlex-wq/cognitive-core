"""Regression test for P0-1 orphaned-key incident (2026-05-31).

cleanup_stale_pending_agents() deletes stale pending_claim rows. agent_keys
has FK ON DELETE CASCADE → deleting agent_states also deletes the key. If a
claim-handshake created the key but didn't flip status to 'active' in time,
the cleanup used to delete the agent AND cascade-kill its working key →
agent orphaned ("API key not registered"), losing access + all memory.

Fix: cleanup skips any pending_claim that still has an active (non-revoked)
key. This is a pure logic test against a MOCK pool — it captures the SQL the
function issues and asserts the guard clause is present, without touching the
shared app connection pool (doing so from inside the test runner while the
background API uses the same pool corrupts the event loop and breaks unrelated
async DB tests).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from app.services.media_cleanup import cleanup_stale_pending_agents


def _mock_pool(monkeypatch, captured: dict):
    """Patch get_pool() in media_cleanup to a mock whose conn.execute records SQL."""
    conn = MagicMock()

    async def _execute(sql, *args):
        captured["sql"] = sql
        captured["args"] = args
        return "DELETE 0"

    conn.execute = AsyncMock(side_effect=_execute)

    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_cm)

    async def _get_pool():
        return pool

    monkeypatch.setattr("app.services.media_cleanup.get_pool", _get_pool)
    return conn


async def test_cleanup_query_has_active_key_guard(monkeypatch):
    captured: dict = {}
    _mock_pool(monkeypatch, captured)

    await cleanup_stale_pending_agents()

    sql = " ".join(captured["sql"].split()).lower()
    # Still scopes to stale pending_claim rows…
    assert "delete from agent_states" in sql
    assert "status = 'pending_claim'" in sql
    assert "interval '10 minutes'" in sql
    # …but must NOT delete a pending row that still has an active (non-revoked) key.
    assert "not exists" in sql, "orphan guard missing: cleanup would cascade-kill live keys"
    assert "agent_keys" in sql
    assert "revoked_at is null" in sql


async def test_cleanup_returns_deleted_count(monkeypatch):
    """Smoke: function parses the 'DELETE N' status into an int and returns it."""
    captured: dict = {}
    conn = _mock_pool(monkeypatch, captured)
    conn.execute = AsyncMock(return_value="DELETE 3")

    n = await cleanup_stale_pending_agents()
    assert n == 3
