"""`Authorization: Bearer <ключ>` принимается как X-API-Key.

Codex CLI подключает Streamable-HTTP MCP только с bearer-токеном
(`codex mcp add --url … --bearer-token-env-var …`) — заголовок X-API-Key ему не
задать. Шим в app/main.py подставляет ключ из Bearer, когда X-API-Key
отсутствует; при обоих заголовках побеждает X-API-Key.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI, Request

from app.main import BearerApiKeyShim


def _app():
    app = FastAPI()

    @app.get("/who")
    async def who(request: Request):
        return {"key": request.headers.get("x-api-key")}

    app.add_middleware(BearerApiKeyShim)
    return app


@pytest.mark.asyncio
async def test_bearer_becomes_api_key():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://t") as c:
        r = await c.get("/who", headers={"Authorization": "Bearer ck_agent_123"})
    assert r.json() == {"key": "ck_agent_123"}


@pytest.mark.asyncio
async def test_explicit_api_key_wins_and_non_bearer_ignored():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://t") as c:
        r1 = await c.get("/who", headers={"Authorization": "Bearer other", "X-API-Key": "real"})
        r2 = await c.get("/who", headers={"Authorization": "Basic abc"})
        r3 = await c.get("/who", headers={"Authorization": "Bearer "})
    assert r1.json() == {"key": "real"}
    assert r2.json() == {"key": None}
    assert r3.json() == {"key": None}
