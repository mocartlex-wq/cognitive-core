"""Регрессия (2026-06-14 аудит): упавшие домены должны попадать в
persistent retry-queue в Redis и повторяться на следующем цикле,
а не молча скипаться до тех пор, пока в них не появятся новые события."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import worker


@pytest.fixture
def fake_redis():
    """Мок Redis с set-операциями (sadd/srem/smembers/expire)."""
    members: set[str] = set()

    r = MagicMock()

    async def _sadd(_key, m):
        members.add(m)

    async def _srem(_key, m):
        members.discard(m)

    async def _smembers(_key):
        return set(members)

    async def _expire(_key, _ttl):
        return True

    r.sadd = AsyncMock(side_effect=_sadd)
    r.srem = AsyncMock(side_effect=_srem)
    r.smembers = AsyncMock(side_effect=_smembers)
    r.expire = AsyncMock(side_effect=_expire)
    return r, members


@pytest.mark.asyncio
async def test_failed_weekly_domain_goes_to_retry_queue(fake_redis):
    r, members = fake_redis

    fake_conn = MagicMock()
    fake_conn.fetch = AsyncMock(return_value=[{"domain": "good"}, {"domain": "bad"}])

    class _AcqCtx:
        async def __aenter__(self_inner):
            return fake_conn

        async def __aexit__(self_inner, *args):
            return False

    fake_pool = MagicMock()
    fake_pool.acquire = MagicMock(return_value=_AcqCtx())

    async def _weekly(domain):
        if domain == "bad":
            raise RuntimeError("LLM timeout")
        return {"status": "consolidated"}

    with (
        patch.object(worker, "get_pool", new=AsyncMock(return_value=fake_pool)),
        patch.object(worker, "get_redis", new=AsyncMock(return_value=r)),
        patch.object(worker, "weekly_consolidate", new=AsyncMock(side_effect=_weekly)),
        patch.object(worker, "log_audit", new=AsyncMock()),
    ):
        results = await worker.run_weekly_cycle()

    by_domain = {x["domain"]: x for x in results}
    assert "result" in by_domain["good"]
    assert "error" in by_domain["bad"]
    # bad попал в retry-queue, good — нет
    assert "bad" in members
    assert "good" not in members


@pytest.mark.asyncio
async def test_recovered_domain_is_removed_from_retry_queue(fake_redis):
    r, members = fake_redis
    members.add("flaky")  # домен висел с прошлого раза

    fake_conn = MagicMock()
    # В текущей выборке "flaky" нет — он должен подтянуться из retry-queue
    fake_conn.fetch = AsyncMock(return_value=[{"domain": "other"}])

    class _AcqCtx:
        async def __aenter__(self_inner):
            return fake_conn

        async def __aexit__(self_inner, *args):
            return False

    fake_pool = MagicMock()
    fake_pool.acquire = MagicMock(return_value=_AcqCtx())

    with (
        patch.object(worker, "get_pool", new=AsyncMock(return_value=fake_pool)),
        patch.object(worker, "get_redis", new=AsyncMock(return_value=r)),
        patch.object(worker, "weekly_consolidate", new=AsyncMock(return_value={"status": "consolidated"})),
        patch.object(worker, "log_audit", new=AsyncMock()),
    ):
        results = await worker.run_weekly_cycle()

    processed_domains = {x["domain"] for x in results}
    # Оба домена обработаны: один из текущей выборки, второй из retry-queue.
    assert processed_domains == {"other", "flaky"}
    # При успехе flaky выгребается из set'а.
    assert "flaky" not in members


@pytest.mark.asyncio
async def test_monthly_cycle_also_uses_retry_queue(fake_redis):
    r, members = fake_redis

    fake_conn = MagicMock()
    fake_conn.fetch = AsyncMock(return_value=[{"domain": "dom1"}])

    class _AcqCtx:
        async def __aenter__(self_inner):
            return fake_conn

        async def __aexit__(self_inner, *args):
            return False

    fake_pool = MagicMock()
    fake_pool.acquire = MagicMock(return_value=_AcqCtx())

    with (
        patch.object(worker, "get_pool", new=AsyncMock(return_value=fake_pool)),
        patch.object(worker, "get_redis", new=AsyncMock(return_value=r)),
        patch.object(worker, "run_monthly_audit", new=AsyncMock(side_effect=RuntimeError("boom"))),
        patch.object(worker, "log_audit", new=AsyncMock()),
    ):
        await worker.run_monthly_cycle()

    assert "dom1" in members  # упавший в monthly-cycle тоже в retry-set


@pytest.mark.asyncio
async def test_redis_failure_in_retry_queue_doesnt_break_cycle(fake_redis):
    """Если Redis недоступен — цикл должен идти, retry просто потеряется
    (best-effort). Иначе сбой Redis вешает весь worker."""
    fake_conn = MagicMock()
    fake_conn.fetch = AsyncMock(return_value=[{"domain": "x"}])

    class _AcqCtx:
        async def __aenter__(self_inner):
            return fake_conn

        async def __aexit__(self_inner, *args):
            return False

    fake_pool = MagicMock()
    fake_pool.acquire = MagicMock(return_value=_AcqCtx())

    async def _broken_redis():
        raise ConnectionError("redis down")

    with (
        patch.object(worker, "get_pool", new=AsyncMock(return_value=fake_pool)),
        patch.object(worker, "get_redis", new=AsyncMock(side_effect=_broken_redis)),
        patch.object(worker, "weekly_consolidate", new=AsyncMock(side_effect=RuntimeError("LLM"))),
        patch.object(worker, "log_audit", new=AsyncMock()),
    ):
        # Главное — не должно бросать.
        results = await worker.run_weekly_cycle()
    assert any("error" in x for x in results)
