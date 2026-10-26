"""Полные контрольные точки: сервис, REST, MCP и опциональный Postgres."""
import json
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.api import mcp_protocol as mcp
from app.api import operative as api
from app.services import operative as svc

OWNER = '35cc4c15-0054-477d-ad35-a7872fff7b71'


def pool_for(rows):
    conn = MagicMock(fetch=AsyncMock(return_value=rows))
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = acquire
    return pool, conn


def row(rid='new', task='CHECKPOINT ai_crm [feat/task@abcdef1]'):
    return dict(id=rid, domain='ai_crm', source_agent='claude',
                timestamp=datetime(2026, 10, 26, tzinfo=timezone.utc),
                raw_payload=json.dumps(dict(task=task, result='Полный текст ' * 500)))


@pytest.mark.asyncio
async def test_checkpoint_full_payload_and_scope():
    pool, conn = pool_for([row(), row('old')])
    with patch.object(svc, 'get_pool', AsyncMock(return_value=pool)):
        got = await svc.latest_checkpoint('ai_crm', OWNER)
    assert got['result'] == 'Полный текст ' * 500
    assert got['superseded_ids'] == ['old']
    assert got['branch'] == 'feat/task' and got['sha'] == 'abcdef1'
    assert got['source_agent'] == 'claude'
    sql, *args = conn.fetch.call_args.args
    assert 'owner_user_id = $1::uuid' in sql
    assert "INTERVAL '30 days'" in sql and 'ORDER BY timestamp DESC, id DESC' in sql
    assert args == [OWNER, 'ai_crm', 'CHECKPOINT ai!_crm%']


@pytest.mark.asyncio
async def test_checkpoint_absent_owner_does_not_query():
    with patch.object(svc, 'get_pool', AsyncMock()) as pool:
        assert await svc.latest_checkpoint('ai_crm', None) is None
        pool.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_checkpoint_and_literal_wildcards():
    pool, conn = pool_for([])
    with patch.object(svc, 'get_pool', AsyncMock(return_value=pool)):
        assert await svc.latest_checkpoint('a_%!', OWNER) is None
    assert conn.fetch.call_args.args[3] == 'CHECKPOINT a!_!%!!%'


@pytest.mark.asyncio
async def test_checkpoint_without_branch_metadata():
    r = row(task='CHECKPOINT ai_crm')
    r['raw_payload'] = json.loads(r['raw_payload'])
    pool, _ = pool_for([r])
    with patch.object(svc, 'get_pool', AsyncMock(return_value=pool)):
        got = await svc.latest_checkpoint('ai_crm', OWNER)
    assert 'branch' not in got and 'sha' not in got


@pytest.mark.asyncio
async def test_rest_auth_owner_and_domain_validation():
    app = FastAPI()
    app.include_router(api.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://t') as client:
        with patch.object(api, 'verify_api_key', AsyncMock()), \
             patch.object(api, 'resolve_owner_user_id', AsyncMock(return_value=OWNER)), \
             patch.object(api, 'latest_checkpoint', AsyncMock(return_value={'result': 'x' * 3000})) as get:
            res = await client.get('/operative/checkpoint', params={'domain': 'ai_crm'})
            assert res.status_code == 200 and len(res.json()['result']) == 3000
            get.assert_awaited_once_with('ai_crm', OWNER)
            assert (await client.get('/operative/checkpoint')).status_code == 422
            assert (await client.get('/operative/checkpoint?domain=')).status_code == 422
        with patch.object(api, 'verify_api_key', AsyncMock()), \
             patch.object(api, 'resolve_owner_user_id', AsyncMock(return_value=None)):
            assert (await client.get('/operative/checkpoint?domain=ai_crm')).status_code == 403
        with patch.object(api, 'verify_api_key', AsyncMock(side_effect=HTTPException(401))):
            assert (await client.get('/operative/checkpoint?domain=ai_crm')).status_code == 401


@pytest.mark.asyncio
async def test_mcp_dispatch_and_catalog():
    tool = next(t for t in mcp.TOOLS if t['name'] == 'cognitive_checkpoint')
    assert tool['inputSchema']['required'] == ['domain']
    request = MagicMock()
    with patch.object(mcp, '_call_self', AsyncMock(return_value={'result': 'full'})) as call:
        assert await mcp._dispatch_tool(request, 'cognitive_checkpoint', {'domain': 'ai_crm'}) == {'result': 'full'}
    call.assert_awaited_once_with(request, 'GET', '/operative/checkpoint',
                                 params={'domain': 'ai_crm'}, timeout_s=6.0)


@pytest.mark.asyncio
async def test_recent_dedup_sql_before_limit():
    pool, conn = pool_for([])
    with patch.object(svc, 'get_pool', AsyncMock(return_value=pool)):
        await svc.recent_events('CHECKPOINT', OWNER)
    sql = conn.fetch.call_args.args[0]
    assert 'NOT EXISTS' in sql and 'newer.owner_user_id = $1::uuid' in sql
    assert sql.index('NOT EXISTS') < sql.index('LIMIT $5')


@pytest.mark.asyncio
async def test_mcp_checkpoint_text_is_not_truncated():
    app = FastAPI()
    app.include_router(mcp.router)
    payload = {'task': 'CHECKPOINT ai_crm', 'result': 'полный' * 3000}
    with patch.object(mcp, '_dispatch_tool', AsyncMock(return_value=payload)), \
         patch.object(mcp, '_rate_limit_ok', AsyncMock(return_value=True)):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://t') as client:
            response = await client.post('/mcp', json={
                'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': 'cognitive_checkpoint', 'arguments': {'domain': 'ai_crm'}},
            })
    result = response.json()['result']
    assert json.loads(result['content'][0]['text']) == payload
    assert result['structuredContent'] == payload
    assert len(mcp._format_text(payload)) == 8000


@pytest.mark.asyncio
@pytest.mark.skipif(not os.environ.get('DATABASE_URL'), reason='DATABASE_URL не задан')
async def test_live_checkpoint_isolation_window_and_recent_dedup():
    import asyncpg

    conn = await asyncpg.connect(os.environ['DATABASE_URL'], timeout=5)
    try:
        await conn.execute('CREATE TEMP TABLE l1_raw_events (id uuid DEFAULT gen_random_uuid(), '
                           'domain text, source_agent text, owner_user_id uuid, '
                           'timestamp timestamptz DEFAULT now(), raw_payload jsonb)')
        other = '00000000-0000-0000-0000-000000000001'
        for owner, domain, age, task in [
            (OWNER, 'ai_crm', 2, 'CHECKPOINT ai_crm [old@aaa]'),
            (OWNER, 'ai_crm', 1, 'CHECKPOINT ai_crm [new@bbb]'),
            (other, 'ai_crm', 0, 'CHECKPOINT ai_crm [foreign@ccc]'),
            (OWNER, 'aiXcrm', 0, 'CHECKPOINT aiXcrm'),
            (OWNER, 'ai_crm', 31, 'CHECKPOINT ai_crm expired'),
            (OWNER, 'ai_crm', 0, 'ordinary event'),
        ]:
            await conn.execute('INSERT INTO l1_raw_events(owner_user_id,domain,timestamp,raw_payload,source_agent) '
                               "VALUES($1::uuid,$2,now()-$3*INTERVAL '1 day',$4::jsonb,'test')",
                               owner, domain, age, json.dumps({'task': task, 'result': 'x' * 5000}))
        pool, _ = pool_for([])
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        with patch.object(svc, 'get_pool', AsyncMock(return_value=pool)):
            got = await svc.latest_checkpoint('ai_crm', OWNER)
            assert got['branch'] == 'new' and len(got['result']) == 5000
            assert len(got['superseded_ids']) == 1
            recent = await svc.recent_events('', OWNER, domain='ai_crm')
            assert len(recent) == 2
            assert sum(r['content']['task'].startswith('CHECKPOINT') for r in recent) == 1
            assert await svc.latest_checkpoint('missing', OWNER) is None
    finally:
        await conn.close()
