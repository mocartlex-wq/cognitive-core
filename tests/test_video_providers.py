"""Tests для video generation providers (Kling + Sora scaffold).

Tests structure готов для real key verification — сейчас покрывают:
  - registry: PROVIDER_REGISTRY + get_provider/is_valid_provider
  - kling: _parse_key, _generate_jwt (HS256 канонически), submit/poll
    error-paths (missing key, invalid format)
  - sora: stub возвращает понятный «not yet» message
  - HTTP mocking — без живых вызовов к Kling/OpenAI

Когда owner добавит реальный Kling access_key|secret_key — раскоммент
секцию `INTEGRATION_TESTS` в конце.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from app.services.video_providers import (
    PROVIDER_LABELS,
    PROVIDER_REGISTRY,
    get_provider,
    is_valid_provider,
    kling,
    sora,
)


# ───────── Registry ─────────
def test_registry_contains_kling_and_sora():
    assert "kling_video" in PROVIDER_REGISTRY
    assert "sora_video" in PROVIDER_REGISTRY
    assert PROVIDER_LABELS["kling_video"] == "Kling.ai (Kuaishou)"


def test_get_provider_returns_module():
    mod = get_provider("kling_video")
    assert mod is kling
    assert hasattr(mod, "submit")
    assert hasattr(mod, "poll")


def test_is_valid_provider():
    assert is_valid_provider("kling_video") is True
    assert is_valid_provider("sora_video") is True
    assert is_valid_provider("unknown") is False
    assert is_valid_provider("") is False


# ───────── Kling: key parsing ─────────
def test_parse_key_pipe_format():
    access, secret = kling._parse_key("AKIA123|SECRET456")
    assert access == "AKIA123"
    assert secret == "SECRET456"


def test_parse_key_invalid_no_pipe():
    access, secret = kling._parse_key("just-an-api-key")
    assert access == ""
    assert secret == ""


def test_parse_key_strips_whitespace():
    access, secret = kling._parse_key("  AKIA  |  SECRET  ")
    assert access == "AKIA"
    assert secret == "SECRET"


# ───────── Kling: JWT generation ─────────
def test_jwt_format_three_segments():
    token = kling._generate_jwt("access123", "secret456")
    parts = token.split(".")
    assert len(parts) == 3, "JWT должен быть header.payload.signature"


def test_jwt_header_decoded():
    import base64
    token = kling._generate_jwt("access123", "secret456")
    header_b64 = token.split(".")[0]
    # base64-url decode (add padding)
    header_b64 += "=" * (4 - len(header_b64) % 4)
    header = json.loads(base64.urlsafe_b64decode(header_b64))
    assert header == {"alg": "HS256", "typ": "JWT"}


def test_jwt_payload_contains_iss_exp():
    import base64
    token = kling._generate_jwt("access-foo", "secret-bar", ttl_sec=600)
    payload_b64 = token.split(".")[1]
    payload_b64 += "=" * (4 - len(payload_b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    assert payload["iss"] == "access-foo"
    assert "exp" in payload
    assert "nbf" in payload


# ───────── Kling: submit error paths ─────────
@pytest.mark.asyncio
async def test_kling_submit_missing_key():
    result = await kling.submit("", "prompt")
    assert "error" in result
    assert result["fallback_recommended"] is True


@pytest.mark.asyncio
async def test_kling_submit_missing_prompt():
    result = await kling.submit("access|secret", "")
    assert "error" in result
    assert "prompt" in result["error"]


@pytest.mark.asyncio
async def test_kling_submit_invalid_key_format():
    result = await kling.submit("not-pipe-separated", "valid prompt")
    assert "error" in result
    assert "access_key|secret_key" in result["error"]


# ───────── Kling: HTTP mocked ─────────
@pytest.mark.asyncio
async def test_kling_submit_happy_path():
    """Mock httpx response → verify task_id parsed correctly."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json = lambda: {"code": 0, "data": {"task_id": "task-abc-123"}, "message": "ok"}

    class MockClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **kw): return mock_response

    with patch("app.services.video_providers.kling.httpx.AsyncClient", return_value=MockClient()):
        result = await kling.submit("access|secret", "test prompt", duration_sec=5)

    assert result.get("task_id") == "task-abc-123"
    assert result.get("mode") == "text2video"
    assert "error" not in result


@pytest.mark.asyncio
async def test_kling_submit_image2video_uses_image_endpoint():
    captured = {}

    class MockResp:
        status_code = 201
        def json(self): return {"code": 0, "data": {"task_id": "tid"}, "message": "ok"}

    class MockClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, **kw):
            captured["url"] = url
            captured["json"] = kw.get("json")
            return MockResp()

    with patch("app.services.video_providers.kling.httpx.AsyncClient", return_value=MockClient()):
        await kling.submit("access|secret", "анимация", image_url="https://example.com/cat.jpg")

    assert "/v1/videos/image2video" in captured["url"]
    assert captured["json"]["image"] == "https://example.com/cat.jpg"


@pytest.mark.asyncio
async def test_kling_poll_completed():
    """Mock Kling response для completed task — извлекаем video_url."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json = lambda: {
        "code": 0,
        "data": {
            "task_status": "succeed",
            "progress": 100,
            "task_result": {"videos": [{"url": "https://kling.cdn/abc.mp4", "duration": 5}]},
        },
    }

    class MockClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **kw): return mock_response

    with patch("app.services.video_providers.kling.httpx.AsyncClient", return_value=MockClient()):
        result = await kling.poll("access|secret", "task-xyz")

    assert result["status"] == "completed"
    assert result["video_url"] == "https://kling.cdn/abc.mp4"
    assert result["duration_sec"] == 5


# ───────── Sora: stub behaviour ─────────
@pytest.mark.asyncio
async def test_sora_submit_returns_not_yet_message():
    result = await sora.submit("any-key", "prompt")
    assert "error" in result
    assert "wait-list" in result["error"].lower() or "не открыт" in result["error"]
    assert result.get("fallback_provider") == "kling_video"


@pytest.mark.asyncio
async def test_sora_test_connection_returns_wait_list():
    result = await sora.test_connection("any-key")
    assert result["ok"] is False
    assert "openai.com/sora" in result["message"]


# ───────── INTEGRATION TESTS (требуют real Kling key, off by default) ─────────
# Раскомментировать когда owner добавит KLING_TEST_KEY env var:
#
# import os
# KLING_TEST_KEY = os.environ.get("KLING_TEST_KEY", "")
# pytestmark_integration = pytest.mark.skipif(
#     not KLING_TEST_KEY, reason="KLING_TEST_KEY not set — skipping integration"
# )
#
# @pytestmark_integration
# @pytest.mark.asyncio
# async def test_kling_test_connection_real_key():
#     result = await kling.test_connection(KLING_TEST_KEY)
#     assert result["ok"] is True
