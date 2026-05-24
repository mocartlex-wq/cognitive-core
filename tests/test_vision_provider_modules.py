"""Smoke-тесты vision_providers — без реальных API вызовов.

Проверяем:
  - Все 6 провайдеров импортируются + exposed analyze + test_connection
  - PROVIDER_REGISTRY содержит все 6
  - PROVIDER_ORDER корректный
  - is_valid_provider() работает
  - analyze() с empty api_key → fallback_recommended=True
  - analyze() с empty frame_urls → fallback_recommended=False
"""
from __future__ import annotations

import pytest

from app.services.vision_providers import (
    PROVIDER_LABELS,
    PROVIDER_ORDER,
    PROVIDER_REGISTRY,
    get_analyzer,
    is_valid_provider,
)


EXPECTED_PROVIDERS = {"qwen", "minimax", "gigachat", "claude", "openai", "gemini"}


def test_all_providers_registered():
    assert set(PROVIDER_REGISTRY.keys()) == EXPECTED_PROVIDERS
    assert set(PROVIDER_ORDER) == EXPECTED_PROVIDERS
    assert set(PROVIDER_LABELS.keys()) == EXPECTED_PROVIDERS


def test_get_analyzer_for_each():
    for p in EXPECTED_PROVIDERS:
        fn = get_analyzer(p)
        assert fn is not None
        assert callable(fn)


def test_get_analyzer_unknown():
    assert get_analyzer("unknown_provider_xyz") is None
    assert get_analyzer("") is None


def test_is_valid_provider():
    for p in EXPECTED_PROVIDERS:
        assert is_valid_provider(p) is True
    assert is_valid_provider("unknown") is False
    assert is_valid_provider("") is False


def test_each_provider_has_test_connection():
    """Каждый provider module exposed test_connection() — критично для UI test endpoint."""
    import importlib
    for p in EXPECTED_PROVIDERS:
        mod = importlib.import_module(f"app.services.vision_providers.{p}")
        assert hasattr(mod, "test_connection"), f"{p}.test_connection missing"
        assert callable(mod.test_connection), f"{p}.test_connection not callable"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", sorted(EXPECTED_PROVIDERS))
async def test_analyze_with_empty_api_key(provider):
    """Empty api_key → return {error, fallback_recommended:True} (graceful)."""
    fn = get_analyzer(provider)
    result = await fn(
        api_key="",
        frame_urls=["https://example.com/x.jpg"],
        transcript=None,
        duration_seconds=10.0,
    )
    assert "error" in result
    assert result.get("fallback_recommended") is True


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", sorted(EXPECTED_PROVIDERS))
async def test_analyze_with_empty_frames(provider):
    """Empty frame_urls → return {error, fallback_recommended:False}.

    Это не auth-issue — нечего анализировать, fallback тоже не поможет.
    """
    fn = get_analyzer(provider)
    result = await fn(
        api_key="any-key-doesnt-matter",
        frame_urls=[],
        transcript=None,
        duration_seconds=10.0,
    )
    assert "error" in result
    assert result.get("fallback_recommended") is False
