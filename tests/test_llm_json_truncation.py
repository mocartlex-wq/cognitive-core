"""Регрессия 03.11.2026: weekly-синтез больших доменов (ai_crm, cognitive_core,
frontend_errors, work_journal) падал с JSONDecodeError на ~11–13 тыс. символов —
это и есть max_tokens=4096 для кириллицы. _parse_json срезал обрезанный ответ
до последней «}» и отдавал на разбор, причина терялась, а analyze_weekly ловил
только ValueError валидации, не ошибку разбора — домен падал с первой попытки.

Закрепляем: лимит и таймаут берутся из settings, включён json_object (с откатом
для провайдера без него), обрезанный ответ — явная LLMTruncatedError, битый JSON
в weekly — повтор с просьбой «строго JSON».
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.services import analyzer, llm_client
from app.services.llm_client import LLMClient, LLMTruncatedError


def _response(text: str, finish_reason: str = "stop"):
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=text), finish_reason=finish_reason)])


def _fake_openai(create: AsyncMock):
    """AsyncOpenAI(...).with_options(timeout=...).chat.completions.create → create."""
    inst = MagicMock()
    inst.with_options.return_value.chat.completions.create = create
    return MagicMock(return_value=inst), inst


def _client() -> LLMClient:
    c = LLMClient.__new__(LLMClient)
    c.function_name = "weekly_consolidator"
    c.temperature = 0.3
    return c


CFG = {"base_url": "https://x", "api_key": "k"}


@pytest.mark.asyncio
async def test_limits_come_from_settings_and_json_mode_is_on():
    create = AsyncMock(return_value=_response('{"a": 1}'))
    fake, inst = _fake_openai(create)
    with patch.object(llm_client, "AsyncOpenAI", fake):
        out = await _client()._try_call(CFG, "m", [{"role": "user", "content": "json"}])
    assert out == {"a": 1}
    kw = create.await_args.kwargs
    assert kw["max_tokens"] == settings.llm_max_output_tokens
    assert settings.llm_max_output_tokens >= 8192
    assert kw["response_format"] == {"type": "json_object"}
    inst.with_options.assert_called_once_with(timeout=settings.llm_request_timeout)
    assert settings.llm_request_timeout >= 120


@pytest.mark.asyncio
async def test_truncated_response_raises_explicit_error_instead_of_json_garbage():
    # Ровно то, что было в логах: 13 тыс. символов, валидный префикс JSON без конца.
    cut = '{"new_or_updated": [{"type": "rule", "content": {"description": "' + "ш" * 13000
    create = AsyncMock(return_value=_response(cut, finish_reason="length"))
    fake, _ = _fake_openai(create)
    with patch.object(llm_client, "AsyncOpenAI", fake), \
         patch("app.services.metrics.log_event") as log_event:
        with pytest.raises(LLMTruncatedError, match="обрезан"):
            await _client()._try_call(CFG, "m", [])
    assert log_event.call_args.kwargs["chars"] == len(cut)


@pytest.mark.asyncio
async def test_provider_without_json_mode_gets_a_retry_without_the_parameter():
    create = AsyncMock(side_effect=[
        RuntimeError("400: unknown parameter response_format"),
        _response('{"ok": true}'),
    ])
    fake, _ = _fake_openai(create)
    with patch.object(llm_client, "AsyncOpenAI", fake):
        out = await _client()._try_call(CFG, "m", [])
    assert out == {"ok": True}
    first, second = create.await_args_list
    assert "response_format" in first.kwargs
    assert "response_format" not in second.kwargs


@pytest.mark.asyncio
async def test_unrelated_provider_error_is_not_swallowed():
    create = AsyncMock(side_effect=RuntimeError("502 bad gateway"))
    fake, _ = _fake_openai(create)
    with patch.object(llm_client, "AsyncOpenAI", fake):
        with pytest.raises(RuntimeError, match="502"):
            await _client()._try_call(CFG, "m", [])
    assert create.await_count == 1


@pytest.mark.asyncio
async def test_weekly_retries_on_broken_json_and_asks_for_strict_json():
    good = {"new_or_updated": [], "deprecated_l3_ids": [], "tools": []}
    c = MagicMock()
    c.primary_config, c.primary_model = {}, "m"
    c._try_call = AsyncMock(side_effect=[
        json.JSONDecodeError("Expecting ',' delimiter", "{", 0), good,
    ])
    with patch.object(analyzer, "get_llm_client", MagicMock(return_value=c)), \
         patch.object(analyzer, "validate_llm_response", MagicMock(return_value=good)):
        out = await analyzer.analyze_weekly("ai_crm", [], [], [{"id": "b1"}])
    assert out == good
    assert c._try_call.await_count == 2
    second_user = c._try_call.await_args_list[1].args[2][1]["content"]
    assert "JSON" in second_user and len(second_user) > len(c._try_call.await_args_list[0].args[2][1]["content"])


@pytest.mark.asyncio
async def test_weekly_gives_up_after_three_broken_answers():
    c = MagicMock()
    c.primary_config, c.primary_model = {}, "m"
    c._try_call = AsyncMock(side_effect=json.JSONDecodeError("x", "{", 0))
    with patch.object(analyzer, "get_llm_client", MagicMock(return_value=c)):
        with pytest.raises(json.JSONDecodeError):
            await analyzer.analyze_weekly("d", [], [], [{"id": "b1"}])
    assert c._try_call.await_count == 3
