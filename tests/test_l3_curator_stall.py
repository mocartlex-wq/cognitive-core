"""L2→L3: тихие владельцы и сбои синтеза не должны теряться."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import worker
from app.config import settings
from app.services import analyzer, consolidator


def pool_for(conn):
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    return pool


def buf(i, owner):
    return {"id": i, "owner_user_id": owner}


@pytest.mark.asyncio
@pytest.mark.parametrize("quiet_recent", [True, False])
async def test_window_is_chosen_per_owner_including_absent_owner(monkeypatch, quiet_recent):
    monkeypatch.setattr(settings, "weekly_days", 7)
    monkeypatch.setattr(settings, "weekly_backfill_days", 90)
    monkeypatch.setattr(settings, "min_l2_repetitions_for_l3", 2)
    active = [buf("a1", "active"), buf("a2", "active")]
    quiet = [buf("q1", "quiet"), buf("q2", "quiet")]
    recent = active + (quiet[:1] if quiet_recent else [])
    wide = [buf("a0", "active"), *active, *quiet, buf("s1", None)]
    fetch = AsyncMock(side_effect=[recent, wide])
    synthesize = AsyncMock(return_value={"status": "consolidated", "new_items": 0})
    with (
        patch.object(consolidator, "get_pool", AsyncMock(return_value=pool_for(MagicMock()))),
        patch.object(consolidator, "_fetch_l2_window", fetch),
        patch.object(consolidator, "_weekly_for_owner", synthesize),
        patch.object(consolidator, "_maybe_snapshot", AsyncMock(return_value=None)),
        patch.object(consolidator, "index_domain_vectors", AsyncMock(return_value={"total": 0})),
    ):
        await consolidator._weekly_consolidate_impl("d")
    supplied = {call.args[1]: call.args[2] for call in synthesize.await_args_list}
    assert supplied == {"active": active, "quiet": quiet, None: [buf("s1", None)]}
    assert [call.args[2] for call in fetch.await_args_list] == [7, 90]


@pytest.mark.asyncio
async def test_missing_weekly_llm_responses_are_failure():
    client = MagicMock()
    client.primary_config = {}
    client.primary_model = "mock"
    client._try_call = AsyncMock(return_value=None)
    with patch.object(analyzer, "get_llm_client", return_value=client):
        with pytest.raises(RuntimeError, match="no response after 3 attempts"):
            await analyzer.analyze_weekly("d", [], [], [])
    assert client._try_call.await_count == 3


@pytest.mark.asyncio
async def test_valid_empty_synthesis_is_not_failure():
    client = MagicMock()
    client.primary_config = {}
    client.primary_model = "mock"
    empty = {"new_or_updated": [], "deprecated_l3_ids": [], "tools": []}
    client._try_call = AsyncMock(return_value=empty)
    with patch.object(analyzer, "get_llm_client", return_value=client):
        assert await analyzer.analyze_weekly("d", [], [], []) == empty
    assert client._try_call.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("healthy_owner", [False, True])
async def test_owner_failure_is_visible_at_domain_level(healthy_owner):
    buffers = [buf("a1", "bad"), buf("a2", "bad")]
    if healthy_owner:
        buffers += [buf("b1", "good"), buf("b2", "good")]

    async def run(_domain, owner, _buffers):
        if owner == "bad":
            raise RuntimeError("mock synthesis failure")
        return {"status": "consolidated", "new_items": 1}

    with (
        patch.object(consolidator, "get_pool", AsyncMock(return_value=pool_for(MagicMock()))),
        patch.object(consolidator, "_fetch_l2_window", AsyncMock(return_value=buffers)),
        patch.object(consolidator, "_weekly_for_owner", AsyncMock(side_effect=run)),
        patch.object(consolidator, "_maybe_snapshot", AsyncMock(return_value=None)),
        patch.object(consolidator, "index_domain_vectors", AsyncMock(return_value={"total": 0})),
    ):
        result = await consolidator._weekly_consolidate_impl("d")
    assert result["status"] == ("partial_error" if healthy_owner else "error")
    assert result["new_items"] == int(healthy_owner)


@pytest.mark.asyncio
@pytest.mark.parametrize("was_pending", [False, True])
@pytest.mark.parametrize("status", ["error", "partial_error", "consolidated"])
async def test_returned_owner_error_keeps_domain_in_retry(was_pending, status):
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[{"domain": "d"}])
    with (
        patch.object(worker, "get_pool", AsyncMock(return_value=pool_for(conn))),
        patch.object(worker, "_retry_queue_pending", AsyncMock(return_value={"d"} if was_pending else set())),
        patch.object(worker, "_retry_queue_add", AsyncMock()) as add,
        patch.object(worker, "_retry_queue_remove", AsyncMock()) as remove,
        patch.object(worker, "weekly_consolidate", AsyncMock(return_value={
            "status": status, "owners": [{"status": "error"}],
        })),
        patch.object(worker, "log_audit", AsyncMock()),
    ):
        await worker.run_weekly_cycle()
    add.assert_awaited_once_with(worker.RETRY_KEY_WEEKLY, "d")
    remove.assert_not_awaited()
