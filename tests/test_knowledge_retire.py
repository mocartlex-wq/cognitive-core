"""Owner-scoped снятие и поиск L3 без обращения к настоящей БД."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.api import mcp_protocol as mcp
from app.api import memory as api

OWNER = '35cc4c15-0054-477d-ad35-a7872fff7b71'
A = '00000000-0000-0000-0000-000000000001'
B = '00000000-0000-0000-0000-000000000002'


@pytest.fixture
async def client():
    app = FastAPI()
    app.include_router(api.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://t') as c:
        yield c


@pytest.fixture
def db():
    conn = MagicMock(fetch=AsyncMock(return_value=[]))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    with patch.object(api, 'get_pool', AsyncMock(return_value=pool)), \
         patch.object(api, 'verify_api_key', AsyncMock()), \
         patch.object(api, 'resolve_owner_user_id', AsyncMock(return_value=OWNER)):
        yield conn


@pytest.mark.asyncio
async def test_retire_only_own_active_ids_and_dedup(client, db):
    db.fetch.return_value = [{'id': UUID(A)}]
    response = await client.post('/memory/knowledge/retire', json={'ids': [A, B, A], 'reason': ' stale '})
    assert response.status_code == 200
    assert response.json() == {'retired': 1, 'skipped': [B]}
    sql, *args = db.fetch.call_args.args
    assert args == [OWNER, [UUID(A), UUID(B)], 'stale']
    assert 'owner_user_id = $1::uuid' in sql and 'ANY($2::uuid[])' in sql
    assert 'retired_at = NOW()' in sql and 'retire_reason = $3' in sql
    assert 'effective_to = NOW()' in sql
    assert 'effective_to IS NULL AND retired_at IS NULL' in sql


@pytest.mark.asyncio
async def test_retire_missing_foreign_or_already_inactive_skipped(client, db):
    response = await client.post('/memory/knowledge/retire', json={'ids': [A, B], 'reason': 'stale'})
    assert response.json() == {'retired': 0, 'skipped': [A, B]}


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [
    {'ids': [], 'reason': 'x'}, {'ids': ['bad'], 'reason': 'x'},
    {'ids': [A], 'reason': '   '}, {'ids': [A]},
    {'ids': [A] * 201, 'reason': 'x'},
])
async def test_validation(client, db, body):
    assert (await client.post('/memory/knowledge/retire', json=body)).status_code == 422
    db.fetch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('path,body', [('/memory/knowledge/retire', {'ids': [A], 'reason': 'x'}),
                                             ('/memory/knowledge/search?q=x', None)])
async def test_no_owner_and_bad_auth(client, db, path, body):
    method = client.post if body else client.get
    kwargs = {'json': body} if body else {}
    with patch.object(api, 'resolve_owner_user_id', AsyncMock(return_value=None)):
        assert (await method(path, **kwargs)).status_code == 403
    with patch.object(api, 'verify_api_key', AsyncMock(side_effect=HTTPException(401))):
        assert (await method(path, **kwargs)).status_code == 401
    db.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_scoped_escaped_and_bounded(client, db):
    ts = datetime(2026, 10, 26, tzinfo=timezone.utc)
    db.fetch.return_value = [{'id': UUID(A), 'domain': 'deploy', 'created_at': ts, 'preview': 'x' * 200}]
    response = await client.get('/memory/knowledge/search', params={'q': '100%_!', 'domain': 'deploy', 'limit': 3})
    assert response.status_code == 200
    assert response.json() == {'count': 1, 'items': [
        {'id': A, 'domain': 'deploy', 'created_at': ts.isoformat(), 'preview': 'x' * 200}]}
    sql, *args = db.fetch.call_args.args
    assert args == [OWNER, '%100!%!_!!%', 'deploy', 3]
    assert 'owner_user_id = $1::uuid' in sql
    assert 'left(content::text, 200)' in sql and "ILIKE $2 ESCAPE '!'" in sql
    assert 'effective_to IS NULL' in sql and 'retired_at IS NULL' in sql


@pytest.mark.asyncio
async def test_search_empty_and_limit_validation(client, db):
    assert (await client.get('/memory/knowledge/search?q=none')).json() == {'count': 0, 'items': []}
    for query in ['q=x&limit=0', 'q=x&limit=101', 'q=']:
        assert (await client.get('/memory/knowledge/search?' + query)).status_code == 422


@pytest.mark.asyncio
async def test_mcp_dispatch():
    req = MagicMock()
    with patch.object(mcp, '_call_self', AsyncMock(return_value={'ok': True})) as call:
        await mcp._dispatch_tool(req, 'cognitive_knowledge_retire', {'ids': [A], 'reason': 'stale'})
        call.assert_awaited_with(req, 'POST', '/memory/knowledge/retire',
                                json_body={'ids': [A], 'reason': 'stale'}, timeout_s=6.0)
        await mcp._dispatch_tool(req, 'cognitive_knowledge_search', {'q': 'stale', 'domain': 'deploy'})
        call.assert_awaited_with(req, 'GET', '/memory/knowledge/search',
                                params={'q': 'stale', 'domain': 'deploy', 'limit': 20}, timeout_s=6.0)
    names = {t['name'] for t in mcp.TOOLS}
    assert {'cognitive_knowledge_retire', 'cognitive_knowledge_search'} <= names
