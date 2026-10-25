"""cognitive_recall показывает свежие L1 владельца (секция recent).

Совместная проверка Codex↔Claude 25.10.2026: Claude записал факт через
cognitive_remember, Codex через cognitive_recall его не нашёл — recall читал
только L3. До недельной консолидации (≥2 дневных буфера в домене) запись была
невидима для напарника. Теперь grouped-ответ несёт frame.recent: L1 владельца за
7 дней по FTS/ILIKE, owner-scoped, без владельца — пусто.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI

from app.api import operative as api_mod
from app.services import operative as svc

OWNER = "35cc4c15-0054-477d-ad35-a7872fff7b71"


def _pool(rows):
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows)

    class _Acq:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *a):
            return False

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Acq())
    return pool, conn


# ─── сервис ──────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_recent_is_owner_scoped_and_empty_without_owner():
    pool, conn = _pool([])
    with patch.object(svc, "get_pool", AsyncMock(return_value=pool)):
        assert await svc.recent_events("мост", owner_user_id=None) == []
        conn.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_recent_sql_filters_owner_window_domain_and_query():
    ts = datetime(2026, 10, 25, 16, 8, tzinfo=timezone.utc)
    pool, conn = _pool([{
        "id": "e9a89bbc", "domain": "cognitive_core", "source_agent": "claude-8d5b07",
        "timestamp": ts, "raw_payload": {"task": "Проверка тандема", "result": "канал заработал", "lessons": "", "x": 1},
    }])
    with patch.object(svc, "get_pool", AsyncMock(return_value=pool)):
        out = await svc.recent_events("канал Codex", owner_user_id=OWNER, domain="cognitive_core", days=99, limit=99)
    sql, args = conn.fetch.call_args[0][0], conn.fetch.call_args[0][1:]
    assert "owner_user_id = $1::uuid" in sql and "l1_raw_events" in sql
    assert "INTERVAL '1 day'" in sql and "plainto_tsquery" in sql and "ILIKE" in sql
    assert args == (OWNER, 30, "cognitive_core", "канал Codex", 20), "окно и лимит зажаты"
    assert out == [{
        "id": "e9a89bbc", "record_type": "recent", "domain": "cognitive_core",
        "source_agent": "claude-8d5b07", "timestamp": ts.isoformat(),
        "content": {"task": "Проверка тандема", "result": "канал заработал"},
    }]


@pytest.mark.asyncio
async def test_recent_payload_as_string_and_without_known_keys():
    pool, _ = _pool([
        {"id": "a", "domain": "d", "source_agent": "s", "timestamp": "2026-10-25", "raw_payload": '{"note": "строка json"}'},
        {"id": "b", "domain": "d", "source_agent": "s", "timestamp": "2026-10-25", "raw_payload": "просто текст"},
    ])
    with patch.object(svc, "get_pool", AsyncMock(return_value=pool)):
        out = await svc.recent_events("", owner_user_id=OWNER)
    assert out[0]["content"] == {"text": '{"note": "строка json"}'}
    assert out[1]["content"] == {"text": '{"text": "просто текст"}'}


# ─── API ─────────────────────────────────────────────────────────────────────
@pytest.fixture
async def client():
    app = FastAPI()
    app.include_router(api_mod.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


@pytest.mark.asyncio
async def test_grouped_query_carries_recent_section(client):
    knowledge = [{"id": "k1", "record_type": "knowledge", "knowledge_type": "rule", "domain": "deploy", "content": {}}]
    recent = [{"id": "e1", "record_type": "recent", "domain": "cognitive_core", "source_agent": "claude-8d5b07",
               "timestamp": "2026-10-25T16:08:08+00:00", "content": {"task": "тандем"}}]
    with patch.object(api_mod, "verify_api_key", AsyncMock(return_value="codex-desktop")), \
            patch.object(api_mod, "resolve_owner_user_id", AsyncMock(return_value=OWNER)), \
            patch.object(api_mod, "build_operative", AsyncMock(return_value=knowledge)), \
            patch.object(api_mod, "recent_events", AsyncMock(return_value=recent)) as rec, \
            patch.object(api_mod, "create_session", AsyncMock(return_value={"session_id": "s1", "domain": "cognitive_core", "expires_in": 1})), \
            patch.object(api_mod, "record_recall_hits", AsyncMock(return_value=None)), \
            patch.object(api_mod, "_observe_recall", AsyncMock(return_value=None)):
        r = await client.post("/operative/query?grouped=true", json={"domain": "cognitive_core", "context": "канал Codex", "top_k": 5})
    body = r.json()
    assert r.status_code == 200, body
    assert body["frame"]["recent"] == recent
    assert body["frame"]["rules"] == knowledge and body["frame"]["all"] == knowledge
    assert body["counts"]["recent"] == 1 and body["counts"]["total"] == 1
    rec.assert_awaited_once_with(query="канал Codex", owner_user_id=OWNER, domain="cognitive_core")


@pytest.mark.asyncio
async def test_recent_failure_does_not_break_recall(client):
    with patch.object(api_mod, "verify_api_key", AsyncMock(return_value="a")), \
            patch.object(api_mod, "resolve_owner_user_id", AsyncMock(return_value=OWNER)), \
            patch.object(api_mod, "build_operative", AsyncMock(return_value=[])), \
            patch.object(api_mod, "recent_events", AsyncMock(side_effect=RuntimeError("db down"))), \
            patch.object(api_mod, "create_session", AsyncMock(return_value={"session_id": "s1", "domain": "d", "expires_in": 1})), \
            patch.object(api_mod, "record_recall_hits", AsyncMock(return_value=None)), \
            patch.object(api_mod, "_observe_recall", AsyncMock(return_value=None)):
        r = await client.post("/operative/query?grouped=true", json={"domain": "d", "context": "x", "top_k": 5})
    assert r.status_code == 200 and r.json()["frame"]["recent"] == []


def test_mcp_description_mentions_recent():
    from app.api.mcp_protocol import TOOLS
    t = next(x for x in TOOLS if x["name"] == "cognitive_recall")
    assert "recent" in t["description"]
