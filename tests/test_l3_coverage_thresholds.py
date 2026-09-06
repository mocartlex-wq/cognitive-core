"""Догоняющее окно weekly: почему у доменов с L2 не появлялся L3.

Замер 2026-09-06 (`/dashboard/domains`, 55 доменов): у ВСЕХ доменов с l2=1
l3_active=0 (~30 штук), а все домены с l2>=2 знание имеют — кроме infra-handoff,
у которого оба буфера старше окна. Отсюда l3_coverage_pct = 27.5 (14 из 51).

Причина не в качестве материала, а в арифметике: куратор проверяет
повторяемость (min_l2_repetitions_for_l3=2, prompts.py:282 «паттерн в
< min_repetitions буферах → рано в L3») по тем буферам, что ему передали, а
передаём мы окно weekly_days=7. Домен, роняющий буфер раз в месяц, каждую
неделю выглядит как «один буфер» — повтор невозможен в принципе.

Правка: если в обычном окне буферов меньше, чем нужно на повтор, берём
накопленный хвост (weekly_backfill_days). Ключевое требование — домены,
которые уже проходят, обязаны работать в точности как раньше.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.services import consolidator


@pytest.fixture(autouse=True)
def _no_snapshot():
    """Хвост weekly (L4-снапшот и переиндексация) ходит в БД и MinIO своим пулом —
    к порогам отношения не имеет, иначе тест лезет в реальную базу."""
    with patch.object(consolidator, "_maybe_snapshot", AsyncMock(return_value=None)),          patch.object(consolidator, "index_domain_vectors",
                      AsyncMock(return_value={"total": 0})):
        yield


def _buf(i: int, owner: str | None = None) -> dict:
    return {"id": f"b{i}", "date": f"2026-08-{i:02d}", "domain": "d",
            "owner_user_id": owner, "summary": {"s": i},
            "source_event_ids": [], "confidence": 0.9}


def _pool(windows: dict[int, list[dict]]):
    """Пул, отдающий буферы в зависимости от запрошенного окна (days)."""
    conn = MagicMock()
    calls: list[int] = []

    async def _fetch(_sql, domain, days):
        calls.append(days)
        return windows.get(days, [])

    conn.fetch = AsyncMock(side_effect=_fetch)

    class _Acquire:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *a):
            return False

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Acquire())
    return pool, calls


async def _run(windows):
    pool, calls = _pool(windows)
    with patch.object(consolidator, "get_pool", AsyncMock(return_value=pool)), \
         patch.object(consolidator, "_weekly_for_owner",
                      AsyncMock(return_value={"status": "consolidated", "new_items": 1})):
        res = await consolidator._weekly_consolidate_impl("d")
    return res, calls


NARROW = 7
WIDE = 90


class TestDefaults:
    def test_backfill_window_exists_and_is_wider(self):
        assert settings.weekly_backfill_days > settings.weekly_days
        assert settings.weekly_backfill_days == 90

    def test_repetition_threshold_unchanged(self):
        # Порог НЕ трогаем: снижать его — значит пускать в L3 неповторившийся шум.
        assert settings.min_l2_repetitions_for_l3 == 2


class TestPassingDomainsUnchanged:
    @pytest.mark.asyncio
    async def test_enough_buffers_makes_only_one_query(self):
        # Домен и так проходит → второго запроса быть не должно вообще.
        res, calls = await _run({NARROW: [_buf(1), _buf(2)]})
        assert calls == [settings.weekly_days]
        assert res["status"] == "consolidated"

    @pytest.mark.asyncio
    async def test_busy_domain_not_given_extra_history(self):
        # Иначе активным доменам поменялся бы вход куратора — это уже другое поведение.
        _, calls = await _run({NARROW: [_buf(i) for i in range(1, 6)], WIDE: [_buf(i) for i in range(1, 40)]})
        assert len(calls) == 1


class TestStuckDomainsGetHistory:
    @pytest.mark.asyncio
    async def test_single_recent_buffer_pulls_accumulated_tail(self):
        res, calls = await _run({NARROW: [_buf(1)], WIDE: [_buf(1), _buf(2), _buf(3)]})
        assert calls == [settings.weekly_days, settings.weekly_backfill_days]
        assert res["status"] == "consolidated"

    @pytest.mark.asyncio
    async def test_empty_narrow_window_still_reaches_old_buffers(self):
        # infra-handoff: буферы есть, но старше 7 дней — раньше был no_buffers навсегда.
        res, calls = await _run({NARROW: [], WIDE: [_buf(1), _buf(2)]})
        assert calls == [settings.weekly_days, settings.weekly_backfill_days]
        assert res["status"] == "consolidated"

    @pytest.mark.asyncio
    async def test_truly_empty_domain_still_no_buffers(self):
        res, calls = await _run({NARROW: [], WIDE: []})
        assert res["status"] == "no_buffers"
        assert calls == [settings.weekly_days, settings.weekly_backfill_days]

    @pytest.mark.asyncio
    async def test_wide_window_not_used_when_it_adds_nothing(self):
        # Широкое окно вернуло столько же — работаем на исходном наборе, без подмены.
        res, _ = await _run({NARROW: [_buf(1)], WIDE: [_buf(1)]})
        assert res["status"] == "consolidated"


class TestOwnerSplitPreserved:
    @pytest.mark.asyncio
    async def test_buffers_grouped_by_owner_after_widening(self):
        pool, _ = _pool({NARROW: [], WIDE: [_buf(1, "o1"), _buf(2, "o2")]})
        seen: list = []

        async def _per_owner(domain, owner, bufs):
            seen.append(owner)
            return {"status": "consolidated"}

        with patch.object(consolidator, "get_pool", AsyncMock(return_value=pool)), \
             patch.object(consolidator, "_weekly_for_owner", AsyncMock(side_effect=_per_owner)):
            await consolidator._weekly_consolidate_impl("d")
        # Разделение по владельцам — обязательное свойство: чужое знание смешивать нельзя.
        assert sorted(seen) == ["o1", "o2"]
