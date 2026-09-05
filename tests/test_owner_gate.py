"""Гейт владельца (план «связь owner↔флот», Фаза 0, 2026-09-05).

Подтверждено на проде: `POST /agents/register` без авторизации выдавал ключ с
`owner_user_id = NULL`, а NULL везде значил «admin без фильтра» → чтение памяти
всех владельцев + `cognitive_deploy_promote_branch`. Здесь закрепляем новый
контракт:

  • ключ из БД без владельца → 403 / ValueError, а не admin;
  • env-ключ (AGENT_API_KEYS) → владелец из COGCORE_ADMIN_OWNER_USER_ID или
    None (единственный оставшийся admin-режим, config-provisioned);
  • register требует владельца и не отдаёт чужой agent_id;
  • _can_deploy(None) → False.

Чистые unit-тесты: пул и настройки подменяются, БД/Redis не нужны.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api import agents_collab
from app.api.agents_collab import RegisterRequest, register_agent
from app.api.mcp_protocol import _can_deploy, _resolve_agent_full
from app.security import owner as owner_mod
from app.security.owner import resolve_owner_user_id

OWNER = "35cc4c15-0054-477d-ad35-a7872fff7b71"
OTHER = "11111111-1111-1111-1111-111111111111"


def _request(headers: dict | None = None):
    req = MagicMock()
    req.headers = {k.lower(): v for k, v in (headers or {}).items()}
    req.state = SimpleNamespace()
    return req


def _pool(fetchrow=None, execute="INSERT 0 1"):
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=fetchrow if callable(fetchrow) else None,
                              return_value=None if callable(fetchrow) else fetchrow)
    conn.execute = AsyncMock(return_value=execute)

    class _Acquire:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *a):
            return False

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Acquire())
    return pool, conn


def _no_session():
    return patch("app.security.middleware.optional_user", AsyncMock(return_value=None))


@pytest.mark.asyncio
async def test_session_cookie_resolves_owner():
    """UI-путь через cookie: раньше импортировал несуществующую функцию и молчал."""
    session = SimpleNamespace(user_id=OWNER)
    with patch("app.security.middleware.optional_user", AsyncMock(return_value=session)):
        assert await resolve_owner_user_id(_request()) == OWNER


# ─── resolve_owner_user_id ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_db_key_without_owner_is_forbidden():
    pool, _ = _pool(fetchrow={"owner_user_id": None})
    with _no_session(), patch("app.db.postgres.get_pool", AsyncMock(return_value=pool)):
        with pytest.raises(HTTPException) as ei:
            await resolve_owner_user_id(_request({"X-API-Key": "orphan"}))
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_db_key_with_owner_resolves_and_caches():
    pool, conn = _pool(fetchrow={"owner_user_id": OWNER})
    req = _request({"X-API-Key": "k"})
    with _no_session(), patch("app.db.postgres.get_pool", AsyncMock(return_value=pool)):
        assert await resolve_owner_user_id(req) == OWNER
        assert await resolve_owner_user_id(req) == OWNER
    assert conn.fetchrow.await_count == 1, "второй вызов должен идти из кеша request.state"


@pytest.mark.asyncio
async def test_env_key_uses_configured_admin_owner():
    """Ключа нет в БД (env-ключ) → владелец из настроек, если задан."""
    pool, _ = _pool(fetchrow=None)
    with _no_session(), patch("app.db.postgres.get_pool", AsyncMock(return_value=pool)), \
            patch.object(owner_mod, "env_agent_owner", return_value=OWNER):
        assert await resolve_owner_user_id(_request({"X-API-Key": "env"})) == OWNER


@pytest.mark.asyncio
async def test_env_key_without_setting_stays_admin_none():
    pool, _ = _pool(fetchrow=None)
    with _no_session(), patch("app.db.postgres.get_pool", AsyncMock(return_value=pool)), \
            patch.object(owner_mod, "env_agent_owner", return_value=None):
        assert await resolve_owner_user_id(_request({"X-API-Key": "env"})) is None


# ─── MCP _resolve_agent_full ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mcp_orphan_db_key_rejected():
    pool, _ = _pool(fetchrow={"agent_id": "evil", "owner_user_id": None})
    with patch("app.api.mcp_protocol._load_keys", return_value={}), \
            patch("app.db.postgres.get_pool", AsyncMock(return_value=pool)):
        with pytest.raises(ValueError, match="без владельца"):
            await _resolve_agent_full(_request({"X-API-Key": "orphan"}))


@pytest.mark.asyncio
async def test_mcp_env_key_gets_admin_owner():
    with patch("app.api.mcp_protocol._load_keys", return_value={"agent_env": "env-key"}), \
            patch("app.security.owner.env_agent_owner", return_value=OWNER):
        agent_id, owner = await _resolve_agent_full(_request({"X-API-Key": "env-key"}))
    assert (agent_id, owner) == ("agent_env", OWNER)


def test_can_deploy_none_is_forbidden(monkeypatch):
    monkeypatch.setenv("COGCORE_DEPLOY_ADMIN_OWNER_IDS", OWNER)
    assert _can_deploy(None) is False
    assert _can_deploy("") is False
    assert _can_deploy(OWNER) is True
    assert _can_deploy(OTHER) is False


# ─── POST /agents/register ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_register_without_owner_401():
    with patch.object(agents_collab, "resolve_owner_user_id", AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as ei:
            await register_agent(RegisterRequest(agent_id="new-agent"), _request())
    assert ei.value.status_code == 401


@pytest.mark.asyncio
async def test_register_writes_owner_to_both_tables():
    pool, conn = _pool(fetchrow=None)
    with patch.object(agents_collab, "resolve_owner_user_id", AsyncMock(return_value=OWNER)), \
            patch.object(agents_collab, "get_pool", AsyncMock(return_value=pool)):
        out = await register_agent(RegisterRequest(agent_id="new-agent", project="p"), _request())
    assert out.agent_id == "new-agent" and len(out.api_key) == 64
    sqls = [c.args for c in conn.execute.await_args_list]
    assert len(sqls) == 2
    for sql, *params in sqls:
        assert "owner_user_id" in sql, sql
        assert OWNER in params, "владелец обязан попасть в параметры"


@pytest.mark.asyncio
async def test_register_cannot_take_over_foreign_agent():
    pool, conn = _pool(fetchrow={"owner_user_id": OTHER})
    with patch.object(agents_collab, "resolve_owner_user_id", AsyncMock(return_value=OWNER)), \
            patch.object(agents_collab, "get_pool", AsyncMock(return_value=pool)):
        with pytest.raises(HTTPException) as ei:
            await register_agent(RegisterRequest(agent_id="orchestrator"), _request())
    assert ei.value.status_code == 403
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_register_reissues_key_for_own_agent():
    pool, conn = _pool(fetchrow={"owner_user_id": OWNER})
    with patch.object(agents_collab, "resolve_owner_user_id", AsyncMock(return_value=OWNER)), \
            patch.object(agents_collab, "get_pool", AsyncMock(return_value=pool)):
        out = await register_agent(RegisterRequest(agent_id="mine"), _request())
    assert out.agent_id == "mine"
    assert conn.execute.await_count == 2


# ─── миграция 0024 ────────────────────────────────────────────────────────

def test_migration_0024_chain_and_sql():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / \
        "20260905_2100_0024_orphan_keys_owner_gate.py"
    spec = importlib.util.spec_from_file_location("m0024", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert (mod.revision, mod.down_revision) == ("0024", "0023")
    executed: list[str] = []
    mod.op = MagicMock(execute=lambda sql: executed.append(sql))  # alembic.op — прокси без контекста
    mod.upgrade()
    assert len(executed) == 2
    inherit, revoke = executed
    assert "owner_user_id = s.owner_user_id" in inherit and "IS NOT NULL" in inherit
    assert "revoked_at = NOW()" in revoke and "owner_user_id IS NULL" in revoke
    assert "DELETE" not in inherit.upper() and "DELETE" not in revoke.upper()
