"""Порог повторяемости — в коде, а не в промпте.

06.09 прогон weekly по 35 застрявшим доменам поднял l3_coverage_pct 27.5 → 94.1,
но куратор проигнорировал правило prompts.py:282 «паттерн в < min_repetitions
буферах → рано в L3» и продвинул «знания» из ОДНОГО буфера (домен tests получил
три записи вида «необходимо фиксировать инструменты»). Просьба в промпте не
гарантия — гарантия — не звать модель вовсе.

Цель — память, а не процент: метрика после этого снова опустится, и это верно.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.services import consolidator


def _buf(i: int, owner: str | None = "o1") -> dict:
    return {"id": f"b{i}", "date": f"2026-08-{i:02d}", "domain": "d", "owner_user_id": owner,
            "summary": {"s": i}, "source_event_ids": [], "confidence": 0.9}


class TestGateBlocksSingleBuffer:
    @pytest.mark.asyncio
    async def test_one_buffer_returns_status_without_touching_db_or_llm(self):
        # Ни пула, ни куратора: гейт стоит до всякой работы, поэтому и не платим.
        with patch.object(consolidator, "get_pool", AsyncMock(side_effect=AssertionError("БД трогать нельзя"))), \
             patch.object(consolidator, "pre_weekly_check", AsyncMock(side_effect=AssertionError("LLM звать нельзя"))):
            res = await consolidator._weekly_for_owner("d", "o1", [_buf(1)])
        assert res["status"] == "insufficient_repetition"
        assert res["buffers"] == 1

    @pytest.mark.asyncio
    async def test_zero_buffers_also_gated(self):
        with patch.object(consolidator, "get_pool", AsyncMock(side_effect=AssertionError("БД трогать нельзя"))):
            res = await consolidator._weekly_for_owner("d", "o1", [])
        assert res["status"] == "insufficient_repetition"
        assert res["buffers"] == 0

    @pytest.mark.asyncio
    async def test_threshold_comes_from_settings(self, monkeypatch):
        # Порог один и тот же и для гейта, и для промпта — расхождение недопустимо.
        monkeypatch.setattr(settings, "min_l2_repetitions_for_l3", 3)
        with patch.object(consolidator, "get_pool", AsyncMock(side_effect=AssertionError("БД трогать нельзя"))):
            res = await consolidator._weekly_for_owner("d", "o1", [_buf(1), _buf(2)])
        assert res["status"] == "insufficient_repetition"
        assert res["buffers"] == 2


class TestEnoughRepetitionStillWorks:
    @pytest.mark.asyncio
    async def test_two_buffers_go_through_as_before(self):
        # Достаточный повтор — поведение прежнее, гейт в стороне.
        called = {}

        async def _pool_stub():
            called["pool"] = True
            raise RuntimeError("дальше не идём, важен сам факт вызова")

        with patch.object(consolidator, "get_pool", AsyncMock(side_effect=_pool_stub)):
            with pytest.raises(RuntimeError):
                await consolidator._weekly_for_owner("d", "o1", [_buf(1), _buf(2)])
        assert called.get("pool"), "при достаточном повторе работа должна начаться"


class TestPerOwnerCounting:
    def test_counts_by_owner_not_by_domain(self):
        # Два буфера разных владельцев — это НЕ повтор: знание не смешивается.
        assert consolidator._max_per_owner([_buf(1, "o1"), _buf(2, "o2")]) == 1
        assert consolidator._max_per_owner([_buf(1, "o1"), _buf(2, "o1")]) == 2

    def test_empty_is_zero(self):
        assert consolidator._max_per_owner([]) == 0

    def test_none_owner_is_a_key(self):
        assert consolidator._max_per_owner([_buf(1, None), _buf(2, None)]) == 2


class TestDomainStatusIsHonest:
    @staticmethod
    def _pool(buffers):
        conn = MagicMock()
        conn.fetch = AsyncMock(return_value=buffers)

        class _Acquire:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False

        pool = MagicMock()
        pool.acquire = MagicMock(return_value=_Acquire())
        return pool

    async def _weekly(self, buffers, owner_result):
        with patch.object(consolidator, "get_pool", AsyncMock(return_value=self._pool(buffers))), \
             patch.object(consolidator, "_weekly_for_owner", AsyncMock(return_value=owner_result)), \
             patch.object(consolidator, "_maybe_snapshot", AsyncMock(return_value=None)), \
             patch.object(consolidator, "index_domain_vectors", AsyncMock(return_value={"total": 0})):
            return await consolidator._weekly_consolidate_impl("d")

    @pytest.mark.asyncio
    async def test_gated_domain_does_not_report_no_buffers(self):
        # Буферы были — сказать «no_buffers» значит соврать в отчёте.
        res = await self._weekly([_buf(1)], {"status": "insufficient_repetition", "buffers": 1})
        assert res["status"] == "insufficient_repetition"

    @pytest.mark.asyncio
    async def test_truly_empty_domain_still_no_buffers(self):
        res = await self._weekly([], {"status": "insufficient_repetition", "buffers": 0})
        assert res["status"] == "no_buffers"

    @pytest.mark.asyncio
    async def test_consolidated_wins(self):
        res = await self._weekly([_buf(1), _buf(2)], {"status": "consolidated", "new_items": 2})
        assert res["status"] == "consolidated"
