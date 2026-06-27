"""Регрессия (2026-06-14 раунд 3): один сбойный домен не должен ронять
весь daily-цикл. До фикса: первый raise в pre_daily_filter или
analyze_daily_events выкидывал ВСЕХ последующих доменов даже если они
здоровы. Теперь: failure пишется в results, цикл идёт дальше. Worker
кладёт упавший в retry-queue для следующего прохода."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app import worker
from app.services import consolidator


@pytest.mark.asyncio
async def test_one_bad_domain_does_not_block_others():
    """Если pre_daily_filter падает на домене A, домен B всё равно обработан."""
    eid_a, eid_b = uuid4(), uuid4()
    events = [
        {"id": eid_a, "domain": "bad_domain", "source_agent": "x", "raw_payload": {}, "timestamp": None},
        {"id": eid_b, "domain": "good_domain", "source_agent": "x", "raw_payload": {}, "timestamp": None},
    ]

    mock_conn = MagicMock()
    mock_conn.execute = AsyncMock()

    class _TxCtx:
        async def __aenter__(self_inner):
            return None

        async def __aexit__(self_inner, *args):
            return False

    mock_conn.transaction = MagicMock(return_value=_TxCtx())

    class _AcqCtx:
        async def __aenter__(self_inner):
            return mock_conn

        async def __aexit__(self_inner, *args):
            return False

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=_AcqCtx())

    async def _filter(events_in, dom):
        if dom == "bad_domain":
            raise RuntimeError("simulated LLM timeout")
        return {"skip": False, "filtered_event_ids": [str(e["id"]) for e in events_in]}

    with (
        patch.object(consolidator, "get_pool", new=AsyncMock(return_value=mock_pool)),
        patch.object(consolidator, "get_unprocessed_events", new=AsyncMock(return_value=events)),
        patch.object(consolidator, "pre_daily_filter", new=AsyncMock(side_effect=_filter)),
        patch.object(consolidator, "analyze_daily_events",
                     new=AsyncMock(return_value={"summary": "ok", "confidence": 0.6})),
        patch.object(consolidator, "mark_events_processed", new=AsyncMock()),
    ):
        result = await consolidator._daily_consolidate_impl(since_hours=24)

    assert result["status"] == "ok"
    by_dom = {r["domain"]: r for r in result["results"]}
    assert by_dom["bad_domain"]["status"] == "error"
    assert "RuntimeError" in by_dom["bad_domain"]["error"]
    assert by_dom["good_domain"]["status"] == "consolidated"


@pytest.mark.asyncio
async def test_failed_daily_domain_lands_in_retry_queue():
    """`run_daily_cycle` должен класть упавший domain в Redis-set'е."""
    members: set[str] = set()

    r = MagicMock()
    r.sadd = AsyncMock(side_effect=lambda _k, m: members.add(m))
    r.srem = AsyncMock(side_effect=lambda _k, m: members.discard(m))
    r.smembers = AsyncMock(return_value=set())
    r.expire = AsyncMock()

    daily_result = {
        "status": "ok",
        "results": [
            {"domain": "ok_dom", "status": "consolidated", "buffer_id": "..."},
            {"domain": "bad_dom", "status": "error", "error": "LLM timeout"},
        ],
    }

    with (
        patch.object(worker, "get_redis", new=AsyncMock(return_value=r)),
        patch.object(worker, "daily_consolidate", new=AsyncMock(return_value=daily_result)),
        patch.object(worker, "log_audit", new=AsyncMock()),
    ):
        await worker.run_daily_cycle()

    assert "bad_dom" in members
    assert "ok_dom" not in members


@pytest.mark.asyncio
async def test_daily_retry_queue_replays_pending_domains():
    """На следующем заходе run_daily_cycle подбирает домены из retry-set'а
    и вызывает daily_consolidate(domain=...) для них персонально."""
    members: set[str] = {"flaky_dom"}

    r = MagicMock()
    r.sadd = AsyncMock(side_effect=lambda _k, m: members.add(m))
    r.srem = AsyncMock(side_effect=lambda _k, m: members.discard(m))
    r.smembers = AsyncMock(return_value=set(members))
    r.expire = AsyncMock()

    captured = []

    async def _consolidate(*, since_hours=None, domain=None):
        captured.append({"since_hours": since_hours, "domain": domain})
        if domain == "flaky_dom":
            return {
                "status": "ok",
                "results": [{"domain": "flaky_dom", "status": "consolidated", "buffer_id": "..."}],
            }
        return {"status": "no_events", "buffer_id": None}

    with (
        patch.object(worker, "get_redis", new=AsyncMock(return_value=r)),
        patch.object(worker, "daily_consolidate", new=AsyncMock(side_effect=_consolidate)),
        patch.object(worker, "log_audit", new=AsyncMock()),
    ):
        await worker.run_daily_cycle()

    # Первый вызов — retry с domain=flaky_dom, второй — обычный без domain
    assert any(c["domain"] == "flaky_dom" for c in captured), \
        "retry-queue domain must be re-consolidated explicitly"
    assert any(c["domain"] is None for c in captured), \
        "regular cycle must still run after retries"
    # Успех → выгребание из set'а
    assert "flaky_dom" not in members
