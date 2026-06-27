"""Tests для room_* MCP wrappers в app/api/mcp_protocol.py.

DS recommendation 2026-05-26: «room_* handlers без тестов owner может
отклонить». Покрываем:
  - правильный URL/method/body/headers для каждого handler-а
  - missing required params → ValueError (не 500)
  - request.state agent_id resolved correctly

Mock'аем httpx.AsyncClient + _resolve_agent, не используем real rooms service.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api import mcp_protocol


def _make_request(api_key: str = "test-key-цувуцу", resolved_agent: str = "цувуцу"):
    """Build minimal FastAPI Request mock с request.state + headers."""
    request = MagicMock()
    request.headers = {"x-api-key": api_key}
    request.app = MagicMock()
    # Cache resolved agent чтобы _resolve_agent не дёргал БД
    request.state = MagicMock()
    request.state._resolved_agent = (resolved_agent, None)
    request.state._resolved_owner_user_id = None
    return request


@pytest.fixture
def mock_resolve_agent():
    """Patch _resolve_agent to avoid DB calls."""
    with patch.object(mcp_protocol, "_resolve_agent", new=AsyncMock(return_value="цувуцу")):
        yield


@pytest.fixture
def mock_async_client():
    """Patch httpx.AsyncClient.request to capture call + return canned response."""
    captured = {"calls": []}

    class MockResp:
        def __init__(self, status: int = 200, payload: dict | None = None):
            self.status_code = status
            self._payload = payload or {"ok": True}
            self.text = json.dumps(self._payload)

        def json(self):
            return self._payload

    class MockClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def request(self, method, url, headers=None, json=None, params=None):
            captured["calls"].append({
                "method": method, "url": url,
                "headers": headers or {}, "json": json, "params": params,
            })
            # Return method-specific canned data
            if method == "POST" and url.endswith("/rooms"):
                return MockResp(201, {"room_id": "rid-123", "api_key": "rk_xxx", "name": "Test"})
            if "/join" in url:
                return MockResp(200, {"ok": True, "agent_id": "цувуцу"})
            if "/post" in url:
                return MockResp(200, {"ok": True, "message_id": "mid-1"})
            if "/messages" in url:
                return MockResp(200, {"messages": []})
            if "/ask" in url:
                return MockResp(200, {"question_id": "qid-1", "status": "pending"})
            if "/answer/" in url:
                return MockResp(200, {"ok": True, "message_id": "mid-2"})
            if "/pending" in url:
                return MockResp(200, {"pending": []})
            return MockResp(200, {"ok": True})

    with patch.object(mcp_protocol, "AsyncClient", MockClient):
        yield captured


# ───────── room_create ─────────
@pytest.mark.asyncio
async def test_room_create_passes_creator_from_auth(mock_resolve_agent, mock_async_client):
    req = _make_request()
    result = await mcp_protocol._dispatch_tool(req, "room_create",
                                               {"name": "Test", "description": "desc"})
    assert result["room_id"] == "rid-123"
    call = mock_async_client["calls"][-1]
    assert call["method"] == "POST"
    assert call["url"].endswith("/rooms")
    assert call["json"] == {"name": "Test", "description": "desc", "created_by": "цувуцу"}


@pytest.mark.asyncio
async def test_room_create_defaults_name_untitled(mock_resolve_agent, mock_async_client):
    req = _make_request()
    await mcp_protocol._dispatch_tool(req, "room_create", {})
    call = mock_async_client["calls"][-1]
    assert call["json"]["name"] == "Untitled"
    assert call["json"]["created_by"] == "цувуцу"


# ───────── room_join ─────────
@pytest.mark.asyncio
async def test_room_join_requires_room_id_and_key(mock_resolve_agent, mock_async_client):
    req = _make_request()
    with pytest.raises(ValueError, match="room_id и room_key"):
        await mcp_protocol._dispatch_tool(req, "room_join", {"room_id": "rid-1"})
    with pytest.raises(ValueError, match="room_id и room_key"):
        await mcp_protocol._dispatch_tool(req, "room_join", {"room_key": "rk_x"})


@pytest.mark.asyncio
async def test_room_join_passes_room_key_as_header(mock_resolve_agent, mock_async_client):
    req = _make_request()
    await mcp_protocol._dispatch_tool(req, "room_join",
                                      {"room_id": "rid-7", "room_key": "rk_secret"})
    call = mock_async_client["calls"][-1]
    assert call["url"].endswith("/rid-7/join")
    assert call["headers"].get("X-Room-Key") == "rk_secret"
    # Cyrillic agent_id can't go in an HTTP header → routed to query params;
    # identity also travels in the JSON body.
    assert "X-Agent-Id" not in call["headers"]
    assert call["params"]["agent_id"] == "цувуцу"
    assert call["json"]["agent_id"] == "цувуцу"


# ───────── room_post ─────────
@pytest.mark.asyncio
async def test_room_post_rejects_empty_text(mock_resolve_agent, mock_async_client):
    req = _make_request()
    with pytest.raises(ValueError, match="text непустой"):
        await mcp_protocol._dispatch_tool(req, "room_post",
                                          {"room_id": "rid-1", "room_key": "rk_x", "text": ""})
    with pytest.raises(ValueError, match="text непустой"):
        await mcp_protocol._dispatch_tool(req, "room_post",
                                          {"room_id": "rid-1", "room_key": "rk_x", "text": "   "})


@pytest.mark.asyncio
async def test_room_post_with_parent_id(mock_resolve_agent, mock_async_client):
    req = _make_request()
    await mcp_protocol._dispatch_tool(req, "room_post", {
        "room_id": "rid-1", "room_key": "rk_x", "text": "ответ", "parent_id": "mid-parent",
    })
    call = mock_async_client["calls"][-1]
    assert call["url"].endswith("/rid-1/post")
    assert call["json"] == {"from_agent": "цувуцу", "text": "ответ", "parent_id": "mid-parent"}


# ───────── room_read ─────────
@pytest.mark.asyncio
async def test_room_read_with_since_and_limit(mock_resolve_agent, mock_async_client):
    req = _make_request()
    await mcp_protocol._dispatch_tool(req, "room_read", {
        "room_id": "rid-1", "room_key": "rk_x",
        "since": "2026-05-26T00:00:00Z", "limit": 100,
    })
    call = mock_async_client["calls"][-1]
    assert call["method"] == "GET"
    assert call["url"].endswith("/rid-1/messages")
    # Cyrillic agent_id is routed to query params (can't go in an HTTP header).
    assert call["params"] == {"limit": 100, "since": "2026-05-26T00:00:00Z", "agent_id": "цувуцу"}


# ───────── room_ask ─────────
@pytest.mark.asyncio
async def test_room_ask_requires_wait_for_list(mock_resolve_agent, mock_async_client):
    req = _make_request()
    with pytest.raises(ValueError, match="wait_for должен быть"):
        await mcp_protocol._dispatch_tool(req, "room_ask", {
            "room_id": "rid-1", "room_key": "rk_x", "text": "Q?", "wait_for": [],
        })
    with pytest.raises(ValueError, match="wait_for должен быть"):
        await mcp_protocol._dispatch_tool(req, "room_ask", {
            "room_id": "rid-1", "room_key": "rk_x", "text": "Q?", "wait_for": "not-a-list",
        })


@pytest.mark.asyncio
async def test_room_ask_wait_response_false_returns_quick(mock_resolve_agent, mock_async_client):
    req = _make_request()
    await mcp_protocol._dispatch_tool(req, "room_ask", {
        "room_id": "rid-1", "room_key": "rk_x", "text": "Q?",
        "wait_for": ["bob"], "timeout_sec": 30, "wait_response": False,
    })
    call = mock_async_client["calls"][-1]
    assert call["json"]["asker"] == "цувуцу"
    assert call["json"]["wait_for"] == ["bob"]
    assert call["json"]["wait_response"] is False


# ───────── room_answer ─────────
@pytest.mark.asyncio
async def test_room_answer_includes_answerer(mock_resolve_agent, mock_async_client):
    req = _make_request()
    await mcp_protocol._dispatch_tool(req, "room_answer", {
        "room_id": "rid-1", "room_key": "rk_x",
        "question_id": "qid-9", "text": "ответ",
    })
    call = mock_async_client["calls"][-1]
    assert call["url"].endswith("/rid-1/answer/qid-9")
    assert call["json"] == {"answerer": "цувуцу", "text": "ответ"}


@pytest.mark.asyncio
async def test_room_answer_requires_all_fields(mock_resolve_agent, mock_async_client):
    req = _make_request()
    for missing_args in (
        {"room_id": "rid-1", "room_key": "rk_x", "text": "x"},  # no question_id
        {"room_id": "rid-1", "question_id": "q", "text": "x"},  # no room_key
        {"room_id": "rid-1", "room_key": "rk_x", "question_id": "q", "text": ""},  # empty text
    ):
        with pytest.raises(ValueError):
            await mcp_protocol._dispatch_tool(req, "room_answer", missing_args)


# ───────── room_pending ─────────
@pytest.mark.asyncio
async def test_room_pending_propagates_agent_id(mock_resolve_agent, mock_async_client):
    req = _make_request()
    await mcp_protocol._dispatch_tool(req, "room_pending", {"room_id": "rid-1", "room_key": "rk_x"})
    call = mock_async_client["calls"][-1]
    assert call["method"] == "GET"
    assert call["url"].endswith("/rid-1/pending")
    # Cyrillic agent_id can't go in an HTTP header → routed to query params.
    assert "X-Agent-Id" not in call["headers"]
    assert call["params"]["agent_id"] == "цувуцу"
    assert call["headers"].get("X-Room-Key") == "rk_x"


# ───────── non-latin-1 agent_id (regression) ─────────
@pytest.mark.asyncio
async def test_call_rooms_routes_non_latin1_agent_id_to_query(mock_async_client):
    """Regression (observed live 2026-06-14): a non-latin-1 agent_id such as the
    Cyrillic "сервер_память" must never be placed in an HTTP header — httpx raises
    UnicodeEncodeError and breaks every room call. It is routed to the query string
    instead, while ASCII ids keep using the X-Agent-Id header (back-compat)."""
    # Non-latin-1 id → query param, never a header; all header values stay encodable.
    await mcp_protocol._call_rooms("GET", "/r1/messages", room_key="rk", agent_id="сервер_память")
    call = mock_async_client["calls"][-1]
    assert "X-Agent-Id" not in call["headers"]
    assert call["params"]["agent_id"] == "сервер_память"
    for value in call["headers"].values():
        value.encode("latin-1")  # must not raise UnicodeEncodeError

    # ASCII id → unchanged behavior: header set, no query param injected.
    await mcp_protocol._call_rooms("GET", "/r1/messages", room_key="rk", agent_id="bob")
    call = mock_async_client["calls"][-1]
    assert call["headers"]["X-Agent-Id"] == "bob"
    assert call["params"] is None
