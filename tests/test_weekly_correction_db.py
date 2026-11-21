"""Apply the observed merge/retire collision against an explicit QA Postgres."""
import json
import os
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest

from app.services import consolidator


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["replacement", "below_threshold", "rollback", "ambiguous", "merge_only"])
async def test_apply_weekly_correction(case, monkeypatch):
    dsn = os.environ.get("COGCORE_TEST_DB_URL")
    if not dsn:
        pytest.skip("requires COGCORE_TEST_DB_URL")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    domain = f"weekly_qa_{uuid4().hex}"
    owner, old_id, prior_source = uuid4(), uuid4(), uuid4()
    new_sources = [uuid4(), uuid4()]
    old = {"description": "prior", "confidence": 0.99}
    corrected = {"description": "corrected", "confidence": 0.01 if case == "below_threshold" else 0.99}
    updates = [{"type": "rule", "merge_with_l3_id": str(old_id), "content": corrected}]
    if case == "rollback":
        updates.append({"type": "rule", "content": corrected, "merge_with_l3_id": str(uuid4())})
    if case == "ambiguous":
        updates.append({"type": "rule", "content": {"description": "other", "confidence": 0.99},
                        "merge_with_l3_id": str(old_id)})
    monkeypatch.setattr(consolidator, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(consolidator, "pre_weekly_check", AsyncMock(return_value={}))
    monkeypatch.setattr(consolidator, "analyze_weekly", AsyncMock(return_value={
        "new_or_updated": updates,
        "deprecated_l3_ids": [] if case == "merge_only" else [str(old_id)],
    }))
    try:
        await pool.execute(
            """INSERT INTO l3_master_knowledge
                (id, domain, knowledge_type, content, derived_from_l2_ids, owner_user_id)
                VALUES ($1, $2, 'rule', $3, $4, $5)""",
            old_id, domain, json.dumps(old), [prior_source], owner,
        )
        buffers = [{"id": i} for i in new_sources]
        operation = consolidator._weekly_for_owner(domain, str(owner), buffers)
        if case in {"rollback", "ambiguous"}:
            with pytest.raises(ValueError, match="merge target"):
                await operation
        else:
            result = await operation
            assert result["deprecated"] == int(case == "replacement")
            assert result["new_items"] == int(case != "below_threshold")
            cached = await consolidator._weekly_for_owner(domain, str(owner), list(reversed(buffers)))
            assert cached["already_applied"] is True
            assert cached["new_items"] == 0
            consolidator.analyze_weekly.assert_awaited_once()
        rows = await pool.fetch(
            """SELECT id, content, derived_from_l2_ids, version, effective_to
                 FROM l3_master_knowledge WHERE domain=$1 ORDER BY version""", domain,
        )
        active = [r for r in rows if r["effective_to"] is None]
        assert len(active) == 1
        if case in {"replacement", "merge_only"}:
            assert json.loads(active[0]["content"]) == corrected
            assert active[0]["version"] == 2
            assert set(active[0]["derived_from_l2_ids"]) == {prior_source, *new_sources}
            if case == "replacement":
                assert len(rows) == 2
                assert rows[0]["id"] == old_id and rows[0]["effective_to"] is not None
                assert json.loads(rows[0]["content"]) == old
            else:
                assert len(rows) == 1 and active[0]["id"] == old_id
        else:
            assert len(rows) == 1 and active[0]["id"] == old_id
            assert json.loads(active[0]["content"]) == old
            assert active[0]["version"] == 1
        if case in {"rollback", "ambiguous"}:
            # A failed proposal did not claim its inputs, even after executing
            # an INSERT earlier in the transaction.
            updates.pop()
            result = await consolidator._weekly_for_owner(domain, str(owner), buffers)
            assert result["new_items"] == 1 and result["deprecated"] == 1
        if case == "replacement":
            # Editing an existing buffer under the same ID is a new input.
            consolidator.analyze_weekly.return_value = {
                "new_or_updated": [{"type": "rule", "content": {"description": "new", "confidence": 0.99}}]
            }
            buffers[0]["summary"] = {"review": "additional observation"}
            result = await consolidator._weekly_for_owner(domain, str(owner), buffers)
            assert result["new_items"] == 1
            cached = await consolidator._weekly_for_owner(domain, str(owner), buffers)
            assert cached["already_applied"] is True
            assert await pool.fetchval("SELECT count(*) FROM l3_master_knowledge WHERE domain=$1", domain) == 3
    finally:
        await pool.execute("DELETE FROM l3_master_knowledge WHERE domain=$1", domain)
        await pool.close()
