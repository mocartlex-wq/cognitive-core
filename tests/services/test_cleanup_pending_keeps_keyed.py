"""Regression test for P0-1 orphaned-key incident (2026-05-31).

cleanup_stale_pending_agents() deletes stale pending_claim rows. agent_keys
has FK ON DELETE CASCADE → deleting agent_states also deletes the key. If a
claim-handshake created the key but didn't flip status to 'active' in time,
the cleanup used to delete the agent AND cascade-kill its working key →
agent orphaned ("API key not registered"), losing access + all memory.

Fix: cleanup skips any pending_claim that still has an active (non-revoked)
key. This test pins that behaviour:
  - pending + NO key, older than TTL     → deleted
  - pending + active key, older than TTL → KEPT (this is the orphan guard)
  - pending + NO key, younger than TTL   → kept (not stale yet)
"""
from __future__ import annotations

import pytest

from app.db.postgres import get_pool
from app.services.media_cleanup import cleanup_stale_pending_agents

# pytest.ini sets asyncio_mode = auto → async tests run without an explicit marker.

A_KEYLESS_OLD = "test_cleanup_keyless_old"
A_KEYED_OLD = "test_cleanup_keyed_old"
A_KEYLESS_NEW = "test_cleanup_keyless_new"
_ALL = (A_KEYLESS_OLD, A_KEYED_OLD, A_KEYLESS_NEW)


async def _cleanup_rows(conn):
    await conn.execute("DELETE FROM agent_keys WHERE agent_id = ANY($1::text[])", list(_ALL))
    await conn.execute("DELETE FROM agent_states WHERE agent_id = ANY($1::text[])", list(_ALL))


async def test_cleanup_keeps_pending_with_active_key():
    pool = await get_pool()
    async with pool.acquire() as conn:
        # agent_states.owner_user_id FK → accounts(user_id): use a real account.
        owner = await conn.fetchval("SELECT user_id::text FROM accounts ORDER BY created_at LIMIT 1")
        if not owner:
            pytest.skip("no accounts in DB to satisfy owner_user_id FK")

        await _cleanup_rows(conn)
        try:
            # 1) stale pending, NO key → should be deleted
            await conn.execute(
                "INSERT INTO agent_states (agent_id, owner_user_id, status, created_at) "
                "VALUES ($1, $2::uuid, 'pending_claim', NOW() - INTERVAL '20 minutes')",
                A_KEYLESS_OLD, owner,
            )
            # 2) stale pending, WITH active key → must be KEPT (the orphan guard)
            await conn.execute(
                "INSERT INTO agent_states (agent_id, owner_user_id, status, created_at) "
                "VALUES ($1, $2::uuid, 'pending_claim', NOW() - INTERVAL '20 minutes')",
                A_KEYED_OLD, owner,
            )
            await conn.execute(
                "INSERT INTO agent_keys (api_key, agent_id, owner_user_id) "
                "VALUES ($1, $2, $3::uuid)",
                "test-key-cleanup-guard-001", A_KEYED_OLD, owner,
            )
            # 3) fresh pending, NO key → not stale yet, kept
            await conn.execute(
                "INSERT INTO agent_states (agent_id, owner_user_id, status, created_at) "
                "VALUES ($1, $2::uuid, 'pending_claim', NOW())",
                A_KEYLESS_NEW, owner,
            )

            await cleanup_stale_pending_agents()

            rows = await conn.fetch(
                "SELECT agent_id FROM agent_states WHERE agent_id = ANY($1::text[])",
                list(_ALL),
            )
            survivors = {r["agent_id"] for r in rows}

            assert A_KEYLESS_OLD not in survivors, "stale keyless pending should be deleted"
            assert A_KEYED_OLD in survivors, "stale pending WITH active key must be kept (orphan guard)"
            assert A_KEYLESS_NEW in survivors, "fresh pending should not be touched"

            # And the key must still be alive (not cascade-killed)
            key_alive = await conn.fetchval(
                "SELECT count(*) FROM agent_keys WHERE agent_id = $1 AND revoked_at IS NULL",
                A_KEYED_OLD,
            )
            assert key_alive == 1, "active key of kept agent must survive cleanup"
        finally:
            await _cleanup_rows(conn)
