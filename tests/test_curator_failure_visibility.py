"""Сбой куратора должен быть отличим от вердикта «нечего продвигать».

app/services/curator.py гасил ЛЮБОЕ исключение (`except Exception: pass`) и
возвращал пустой ready_for_l3 — ровно то же, что при честном «рано в L3».
Логирования в модуле не было вовсе, поэтому недоступная LLM (таймаут, 402,
circuit breaker) выглядела как отсутствие материала: домен молча оставался без
L3, и следа нигде. Это могло давать часть провала l3_coverage_pct (27.5 на 06.09).

Здесь закрепляем: сбой пишется в лог, помечается curator_error и доезжает
наверх в результат weekly; при нормальном ответе не меняется ничего.
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import curator

GOOD = {
    "ready_for_l3": [{"id": "x"}], "not_ready_for_l3": [], "deprecated_l3": [],
    "conflicts": [], "deduplicated_to_existing": [],
}


def _client(raw=None, exc: Exception | None = None):
    c = MagicMock()
    c.primary_config = {}
    c.primary_model = "m"
    c._try_call = AsyncMock(side_effect=exc) if exc else AsyncMock(return_value=raw)
    return c


async def _check(client):
    with patch.object(curator, "get_llm_client", MagicMock(return_value=client)), \
         patch.object(curator, "get_quality_prompt", MagicMock(return_value=MagicMock(format=MagicMock(return_value="p")))), \
         patch.object(curator, "validate_llm_response", MagicMock(return_value=GOOD)):
        return await curator.pre_weekly_check("d", [], [{"id": "b1", "summary": {}}])


class TestFailureIsVisible:
    @pytest.mark.asyncio
    async def test_exception_sets_flag_and_logs(self, caplog):
        with caplog.at_level(logging.WARNING, logger="app.services.curator"):
            res = await _check(_client(exc=TimeoutError("upstream timed out")))
        assert res["curator_error"].startswith("TimeoutError")
        assert "upstream timed out" in res["curator_error"]
        assert res["ready_for_l3"] == []          # форма ответа не сломана
        assert "curator quality" in caplog.text
        assert "domain=d" in caplog.text           # какой домен пострадал
        assert "TimeoutError" in caplog.text       # и почему

    @pytest.mark.asyncio
    async def test_empty_response_also_flagged(self, caplog):
        # Пустой ответ LLM — тоже сбой, а не вердикт.
        with caplog.at_level(logging.WARNING, logger="app.services.curator"):
            res = await _check(_client(raw=None))
        assert res["curator_error"] == "empty response"
        assert caplog.records

    @pytest.mark.asyncio
    async def test_no_buffers_is_not_an_error(self):
        # «Нечего смотреть» обязано остаться БЕЗ признака сбоя.
        res = await curator.pre_weekly_check("d", [], [])
        assert "curator_error" not in res
        assert res["ready_for_l3"] == []


class TestHappyPathUnchanged:
    @pytest.mark.asyncio
    async def test_good_answer_has_no_flag_and_no_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="app.services.curator"):
            res = await _check(_client(raw={"any": "json"}))
        assert res == GOOD
        assert "curator_error" not in res
        assert not caplog.records


class TestSecretsNotLeaked:
    def test_api_keys_redacted(self):
        for secret in ("sk-abcdef0123456789", "rk_abcdef0123456789", "Bearer abcdef012345"):
            out = curator._safe_err(RuntimeError(f"401 from provider ({secret})"))
            assert secret not in out, out
            assert "<redacted>" in out

    def test_long_message_truncated(self):
        out = curator._safe_err(RuntimeError("x" * 5000))
        assert len(out) < 300

    def test_type_always_present(self):
        assert curator._safe_err(ValueError("")).startswith("ValueError")


class TestFlagReachesWeeklyResult:
    """Признак обязан доехать наверх: /dashboard смотрит результат weekly."""

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

    async def _weekly(self, owner_result):
        from app.services import consolidator
        buf = [{"id": "b1", "date": "2026-08-01", "domain": "d", "owner_user_id": "o1",
                "summary": {}, "source_event_ids": [], "confidence": 0.9}] * 2
        with patch.object(consolidator, "get_pool", AsyncMock(return_value=self._pool(buf))), \
             patch.object(consolidator, "_weekly_for_owner", AsyncMock(return_value=owner_result)), \
             patch.object(consolidator, "_maybe_snapshot", AsyncMock(return_value=None)), \
             patch.object(consolidator, "index_domain_vectors", AsyncMock(return_value={"total": 0})):
            return await consolidator._weekly_consolidate_impl("d")

    @pytest.mark.asyncio
    async def test_curator_error_surfaces_in_result(self):
        res = await self._weekly({"status": "consolidated", "new_items": 0,
                                  "curator_error": "TimeoutError: upstream timed out"})
        assert res["curator_errors"], "сбой куратора должен быть виден в результате weekly"
        assert res["curator_errors"][0]["error"].startswith("TimeoutError")
        assert res["curator_errors"][0]["owner"] == "o1"

    @pytest.mark.asyncio
    async def test_clean_run_reports_no_errors(self):
        res = await self._weekly({"status": "consolidated", "new_items": 3})
        assert res["curator_errors"] == []
        assert res["status"] == "consolidated"

    @pytest.mark.asyncio
    async def test_error_does_not_change_status(self):
        # Статус считается по проделанной работе; признак — отдельная ось.
        res = await self._weekly({"status": "consolidated", "new_items": 1,
                                  "curator_error": "RuntimeError: 402"})
        assert res["status"] == "consolidated"
        assert len(res["curator_errors"]) == 1
