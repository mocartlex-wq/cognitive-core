"""Isolated acceptance host for the real REST/MCP routers, not production startup.

Run only with a disposable PostgreSQL/Redis network and COGCORE_MEMORY_QA=1.
Uses synthetic owner/agent keys. No external LLM, scheduler or production data.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from app.api import events, mcp_protocol, operative
from app.db.postgres import get_pool
from app.db.redis import init_redis
from app.services import embedder

if os.environ.get("COGCORE_MEMORY_QA") != "1" or "/qa" not in os.environ.get("DATABASE_URL", ""):
    raise RuntimeError("This fixture requires an explicitly isolated QA database")

# L3 model inference is outside this L1/state/delivery test. L3 tables are empty.
embedder._fastembed_failed = True
OWNER = "d0309324-e48c-4d22-a573-a94ed3a1c61a"
OUTSIDER = "c000f410-ed1a-45d4-be15-b37590a189ba"


@asynccontextmanager
async def lifespan(app):
    await init_redis()
    pool = await get_pool()
    async with pool.acquire() as conn:
        for owner in (OWNER, OUTSIDER):
            await conn.execute("INSERT INTO accounts (user_id,email,display_name) VALUES ($1::uuid,$2,'Memory QA only') ON CONFLICT DO NOTHING",
                               owner, owner + "@example.invalid")
        for agent, key, owner in (("qa-producer", "qa-producer-only", OWNER),
                                  ("qa-consumer", "qa-consumer-only", OWNER),
                                  ("qa-outsider", "qa-outsider-only", OUTSIDER)):
            await conn.execute("INSERT INTO agent_states (agent_id, owner_user_id) VALUES ($1,$2::uuid) ON CONFLICT DO NOTHING", agent, owner)
            await conn.execute("INSERT INTO agent_keys (api_key, agent_id, owner_user_id) VALUES ($1,$2,$3::uuid) ON CONFLICT DO NOTHING", key, agent, owner)
    yield


app = FastAPI(lifespan=lifespan)
for router in (events.router, operative.router, mcp_protocol.router):
    app.include_router(router)


@app.get("/", response_class=HTMLResponse)
async def test_console():
    return """<!doctype html><html lang="ru"><meta charset="utf-8"><title>Проверка восстановления памяти</title>
    <style>body{font:18px system-ui;max-width:1050px;margin:40px auto;padding:0 20px}input{width:85%;font:inherit;padding:12px}button{padding:12px}pre{white-space:pre-wrap;word-break:break-word;background:#f4f5f7;padding:20px}</style>
    <h1>Проверка восстановления памяти</h1><p>Изолированный стенд. Получатель — другой агент того же тестового владельца.</p>
    <label for="question">Обычный вопрос</label><p><input id="question" value="Где остановились с проверкой Кедра, что дальше и где подтверждение?"><button id="ask">Найти</button></p>
    <pre id="answer">Введите вопрос.</pre><script>
    document.getElementById('ask').onclick=async()=>{
      let q=document.getElementById('question').value;
      const r=await fetch('/mcp',{method:'POST',headers:{'Content-Type':'application/json','X-API-Key':'qa-consumer-only'},body:JSON.stringify({jsonrpc:'2.0',id:1,method:'tools/call',params:{name:'cognitive_recall',arguments:{query:q,domain:'cognitive_core',include_tools:false}}})});
      const b=await r.json(); const s=b.result?.structuredContent;
      document.getElementById('answer').textContent=JSON.stringify(s?{current:s.frame?.current,recent:s.frame?.recent,state_warning:s.frame?.state_warning}:b,null,2);
    };</script></html>"""
