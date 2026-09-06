"""MCP Streamable HTTP (план «связь owner↔флот», Фаза 1, 2026-09-05).

HTTP+SSE deprecated спекой MCP 2026-07-28 (12-месячный offramp). Наш SSE шёл
через Redis pub/sub: POST /mcp/messages?session_id=… публиковал ответ в канал,
на который никто не подписан (воркер перезапущен, ключ mcp:sess:* доживал 120с
TTL), отдавал 202 «принято» — и клиент ждал свой 300с-таймаут. Здесь:

  • новый `POST /mcp` (и `/mcp/http`): ответ инлайн, stateless, batch;
  • initialize отражает версию клиента из поддерживаемых;
  • legacy SSE-путь: PUBLISH без подписчиков → ответ инлайн, а не 202.

Роутер поднимается отдельным FastAPI-приложением — БД/Redis не нужны.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI

from app.api import mcp_protocol
from app.api.mcp_protocol import router


@pytest.fixture
async def client():
    app = FastAPI()
    app.include_router(router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


def _rpc(method: str, params: dict | None = None, id_: int | str | None = 1) -> dict:
    body: dict = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if id_ is not None:
        body["id"] = id_
    return body


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/mcp", "/mcp/http"])
async def test_initialize_inline_and_version_echo(client, path):
    r = await client.post(path, json=_rpc("initialize", {"protocolVersion": "2025-06-18"}))
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == 1
    assert body["result"]["protocolVersion"] == "2025-06-18"
    assert body["result"]["serverInfo"]["name"] == "cognitive-core"
    assert r.headers.get("Mcp-Protocol-Version")


@pytest.mark.asyncio
async def test_initialize_unknown_version_falls_back(client):
    r = await client.post("/mcp", json=_rpc("initialize", {"protocolVersion": "2099-01-01"}))
    assert r.json()["result"]["protocolVersion"] == mcp_protocol.DEFAULT_PROTOCOL_VERSION


@pytest.mark.asyncio
async def test_tools_list_inline(client):
    r = await client.post("/mcp", json=_rpc("tools/list"))
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert {"cognitive_recall", "cognitive_remember", "room_post"} <= names


@pytest.mark.asyncio
async def test_notification_is_202_without_body(client):
    r = await client.post("/mcp", json=_rpc("notifications/initialized", id_=None))
    assert r.status_code == 202
    assert r.content in (b"", b"null")


@pytest.mark.asyncio
async def test_batch_keeps_order_and_drops_notifications(client):
    batch = [
        _rpc("ping", id_="a"),
        _rpc("notifications/initialized", id_=None),
        _rpc("tools/list", id_="b"),
    ]
    r = await client.post("/mcp", json=batch)
    assert r.status_code == 200
    ids = [x["id"] for x in r.json()]
    assert ids == ["a", "b"]


@pytest.mark.asyncio
async def test_parse_error_is_400(client):
    r = await client.post("/mcp", content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32700


@pytest.mark.asyncio
async def test_get_is_405_delete_is_ok(client):
    assert (await client.get("/mcp")).status_code == 405
    assert (await client.delete("/mcp")).json()["stateless"] is True


@pytest.mark.asyncio
async def test_tool_error_carries_request_id(client):
    """Ошибка инструмента должна нести request_id — иначе её не найти в логах."""
    r = await client.post("/mcp", json=_rpc("tools/call", {"name": "cognitive_remember",
                                                           "arguments": {}}))
    err = r.json()["error"]["message"]
    assert "request_id=" in err


# ─── legacy SSE: publish без подписчиков → inline ─────────────────────────

def _redis(receivers: int):
    r = MagicMock()
    r.exists = AsyncMock(return_value=1)
    r.publish = AsyncMock(return_value=receivers)
    r.delete = AsyncMock(return_value=1)
    return r


@pytest.mark.asyncio
async def test_legacy_sse_with_live_subscriber_returns_202(client):
    r = _redis(receivers=1)
    with patch("app.db.redis.get_redis", AsyncMock(return_value=r)):
        resp = await client.post("/mcp/messages?session_id=abc", json=_rpc("ping"))
    assert resp.status_code == 202
    r.publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_sse_without_subscriber_answers_inline(client):
    r = _redis(receivers=0)
    with patch("app.db.redis.get_redis", AsyncMock(return_value=r)):
        resp = await client.post("/mcp/messages?session_id=dead", json=_rpc("ping"))
    assert resp.status_code == 200, "ответ в никуда должен превращаться в inline"
    assert resp.json()["id"] == 1
    r.delete.assert_awaited_once_with("mcp:sess:dead")
