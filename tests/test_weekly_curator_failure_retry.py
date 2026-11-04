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
import json
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


GOOD_RUN = {"results": [{"domain": "a", "result": {"status": "consolidated"}}]}
ALL_FAILED_RUN = {"results": [
    {"domain": "a", "result": {"status": "no_buffers", "curator_errors": [{"owner": "o", "error": "APIConnectionError"}]}},
    {"domain": "b", "error": "boom"},
    # Домен с одним буфером: LLM не звали, ответ «мало повторов» — не признак
    # живого куратора. Ровно так прогон 02.11 выглядел «состоявшимся».
    {"domain": "quiet", "result": {"status": "insufficient_repetition", "owners": [{"status": "insufficient_repetition"}]}},
]}
QUIET_RUN = {"results": [
    {"domain": "quiet", "result": {"status": "insufficient_repetition"}},
    {"domain": "empty", "result": {"status": "no_buffers", "owners": []}},
]}


def test_run_produced_quiet_week_without_failures_counts():
    assert worker._weekly_run_produced(QUIET_RUN)


def test_run_produced_failures_with_only_quiet_domains_does_not_count():
    assert not worker._weekly_run_produced(ALL_FAILED_RUN)


def _audit_rows(*pairs):
    return [{"event_time": t, "details": d} for t, d in pairs]


@pytest.mark.asyncio
async def test_if_due_skips_when_recent_full_run_exists():
    recent = datetime.now(timezone.utc) - timedelta(days=2)
    run = AsyncMock()
    with (
        patch.object(worker, "get_pool", new=AsyncMock(return_value=_pool(fetch_rows=_audit_rows((recent, GOOD_RUN))))),
        patch.object(worker, "run_weekly_cycle", new=run),
    ):
        out = await worker.run_weekly_cycle_if_due(max_age_days=5)
    assert out["status"] == "skipped"
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_if_due_runs_when_last_full_run_is_old_or_missing():
    old = datetime.now(timezone.utc) - timedelta(days=8)
    for rows in (_audit_rows((old, GOOD_RUN)), []):
        run = AsyncMock(return_value=[{"domain": "a", "result": {"status": "consolidated"}},
                                      {"domain": "b", "error": "boom"}])
        with (
            patch.object(worker, "get_pool", new=AsyncMock(return_value=_pool(fetch_rows=rows))),
            patch.object(worker, "run_weekly_cycle", new=run),
        ):
            out = await worker.run_weekly_cycle_if_due(max_age_days=5)
        assert out == {"status": "ran", "domains": 2, "failed": 1}
        run.assert_awaited_once()


@pytest.mark.asyncio
async def test_if_due_ignores_run_where_every_domain_failed():
    # 02.11.2026: понедельничный прогон упал на всех доменах, но в аудите он
    # success=True. Страховка не должна «ждать 5 дней» после такого прогона.
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    old = datetime.now(timezone.utc) - timedelta(days=8)
    run = AsyncMock(return_value=[])
    rows = _audit_rows((yesterday, ALL_FAILED_RUN), (old, GOOD_RUN))
    with (
        patch.object(worker, "get_pool", new=AsyncMock(return_value=_pool(fetch_rows=rows))),
        patch.object(worker, "run_weekly_cycle", new=run),
    ):
        out = await worker.run_weekly_cycle_if_due(max_age_days=5)
    assert out["status"] == "ran"
    run.assert_awaited_once()


def test_run_produced_accepts_json_string_and_legacy_details():
    assert worker._weekly_run_produced(json.dumps(GOOD_RUN))
    assert not worker._weekly_run_produced(json.dumps(ALL_FAILED_RUN))
    assert worker._weekly_run_produced({})          # старый формат — считаем прогоном
    assert worker._weekly_run_produced(None)


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


def test_curator_error_with_consolidated_owner_is_not_failure():
    # 04.11.2026: куратор качества обрезан (22 тыс. символов), а синтез прошёл и
    # записал L3 — повторять домен нельзя, иначе те же буферы уйдут в модель дважды.
    assert not worker._weekly_result_failed({
        "status": "consolidated",
        "curator_errors": [{"owner": "o", "error": "LLMTruncatedError"}],
        "owners": [{"owner": "o", "status": "consolidated", "curator_error": "LLMTruncatedError"}],
    })
