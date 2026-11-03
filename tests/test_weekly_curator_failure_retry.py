"""Регрессия 2026-11-03: сбой куратора НЕ бросает исключение — weekly_consolidate
ловит его внутри владельца и отдаёт curator_errors / owners[].status=error.
02.11 DeepSeek лежал весь понедельничный прогон, все домены «упали» именно так,
retry-очередь осталась пустой, и L3 стоял 8 дней без сигнала.

Проверяем: (1) такой результат ставит домен в очередь; (2) retry-прогон берёт
только очередь и молчит, когда она пуста; (3) systemd-страховка не гоняет
полный weekly, если in-app прогон был недавно; (4) /health.deep показывает
застой L3 и очередь.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import worker


@pytest.fixture
def fake_redis():
    members: set[str] = set()
    r = MagicMock()

    async def _sadd(_key, m):
        members.add(m)

    async def _srem(_key, m):
        members.discard(m)

    async def _smembers(_key):
        return set(members)

    r.sadd = AsyncMock(side_effect=_sadd)
    r.srem = AsyncMock(side_effect=_srem)
    r.smembers = AsyncMock(side_effect=_smembers)
    r.expire = AsyncMock(return_value=True)
    return r, members


def _pool(fetch_rows=None, fetchval=None):
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=fetch_rows or [])
    conn.fetchval = AsyncMock(return_value=fetchval)

    class _Ctx:
        async def __aenter__(self_inner):
            return conn

        async def __aexit__(self_inner, *a):
            return False

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Ctx())
    return pool


class TestResultFailed:
    def test_curator_errors_is_failure(self):
        assert worker._weekly_result_failed(
            {"status": "no_buffers", "curator_errors": [{"owner": "o", "error": "APIConnectionError"}]}
        )

    def test_owner_error_is_failure(self):
        assert worker._weekly_result_failed(
            {"status": "no_buffers", "owners": [{"owner": "o", "status": "error", "error": "x"}]}
        )

    def test_consolidated_is_not_failure(self):
        assert not worker._weekly_result_failed({"status": "consolidated", "owners": [{"status": "consolidated"}]})

    def test_insufficient_repetition_is_not_failure(self):
        # Мало повторов — решение по существу, а не сбой; повторять нечего.
        assert not worker._weekly_result_failed(
            {"status": "insufficient_repetition", "owners": [{"status": "insufficient_repetition"}]}
        )

    def test_lock_held_and_none_are_not_failure(self):
        assert not worker._weekly_result_failed({"status": "lock_held"})
        assert not worker._weekly_result_failed(None)


@pytest.mark.asyncio
async def test_curator_failure_without_exception_goes_to_retry_queue(fake_redis):
    r, members = fake_redis

    async def _weekly(domain):
        if domain == "yurist":
            return {"status": "no_buffers",
                    "curator_errors": [{"owner": "o", "error": "APIConnectionError: Connection error."}]}
        return {"status": "consolidated", "owners": [{"status": "consolidated"}]}

    with (
        patch.object(worker, "get_pool", new=AsyncMock(return_value=_pool([{"domain": "yurist"}, {"domain": "ok"}]))),
        patch.object(worker, "get_redis", new=AsyncMock(return_value=r)),
        patch.object(worker, "weekly_consolidate", new=AsyncMock(side_effect=_weekly)),
        patch.object(worker, "log_audit", new=AsyncMock()),
    ):
        await worker.run_weekly_cycle()

    assert members == {"yurist"}


@pytest.mark.asyncio
async def test_retry_cycle_drains_only_queue_and_is_silent_when_empty(fake_redis):
    r, members = fake_redis
    audit = AsyncMock()
    weekly = AsyncMock(return_value={"status": "consolidated", "owners": [{"status": "consolidated"}]})

    with (
        patch.object(worker, "get_redis", new=AsyncMock(return_value=r)),
        patch.object(worker, "weekly_consolidate", new=weekly),
        patch.object(worker, "log_audit", new=audit),
    ):
        assert await worker.run_weekly_retry_cycle() == []
        weekly.assert_not_awaited()
        audit.assert_not_awaited()

        members.update({"yurist", "work_journal"})
        results = await worker.run_weekly_retry_cycle()

    assert {x["domain"] for x in results} == {"yurist", "work_journal"}
    assert members == set()
    # Retry-прогон помечен в аудите — его не считать «полным» weekly.
    assert audit.await_args.kwargs["details"]["mode"] == "retry"


@pytest.mark.asyncio
async def test_retry_cycle_keeps_domain_when_curator_still_down(fake_redis):
    r, members = fake_redis
    members.add("yurist")
    still_down = {"status": "no_buffers", "curator_errors": [{"owner": "o", "error": "APIConnectionError"}]}

    with (
        patch.object(worker, "get_redis", new=AsyncMock(return_value=r)),
        patch.object(worker, "weekly_consolidate", new=AsyncMock(return_value=still_down)),
        patch.object(worker, "log_audit", new=AsyncMock()),
    ):
        await worker.run_weekly_retry_cycle()

    assert members == {"yurist"}


@pytest.mark.asyncio
async def test_if_due_skips_when_recent_full_run_exists():
    recent = datetime.now(timezone.utc) - timedelta(days=2)
    run = AsyncMock()
    with (
        patch.object(worker, "get_pool", new=AsyncMock(return_value=_pool(fetchval=recent))),
        patch.object(worker, "run_weekly_cycle", new=run),
    ):
        out = await worker.run_weekly_cycle_if_due(max_age_days=5)
    assert out["status"] == "skipped"
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_if_due_runs_when_last_full_run_is_old_or_missing():
    old = datetime.now(timezone.utc) - timedelta(days=8)
    for last in (old, None):
        run = AsyncMock(return_value=[{"domain": "a", "result": {"status": "consolidated"}},
                                      {"domain": "b", "error": "boom"}])
        with (
            patch.object(worker, "get_pool", new=AsyncMock(return_value=_pool(fetchval=last))),
            patch.object(worker, "run_weekly_cycle", new=run),
        ):
            out = await worker.run_weekly_cycle_if_due(max_age_days=5)
        assert out == {"status": "ran", "domains": 2, "failed": 1}
        run.assert_awaited_once()


def test_if_due_ignores_retry_runs_in_audit():
    # Retry-прогон тоже пишет weekly_consolidate; считать его «полным» нельзя —
    # иначе дренаж очереди бесконечно откладывал бы настоящий weekly.
    src = inspect.getsource(worker.run_weekly_cycle_if_due)
    assert "details->>'mode'" in src and "'retry'" in src


class TestHealthL3Stall:
    def _src(self) -> str:
        from app.main import health
        return inspect.getsource(health)

    def test_exposes_keys(self):
        src = self._src()
        for key in ("l3_staleness_days", "l2_buffers_since_l3", "l3_stalled", "weekly_retry_pending"):
            assert f'deep["{key}"]' in src, key

    def test_stall_requires_material(self):
        # Тихая неделя без новых L2 — не застой: условие смотрит на буферы ПОСЛЕ L3.
        src = self._src()
        assert "l2_daily_buffers WHERE created_at > $1" in src
        assert "min_l2_repetitions_for_l3" in src[src.index("l3_stalled"):]

    def test_probe_failure_does_not_break_health(self):
        src = self._src()
        i = src.index('deep["l3_stalled"]')
        assert "try:" in src[:i]
