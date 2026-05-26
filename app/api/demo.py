"""Streaming demo endpoint — для кнопки "Запустить полный цикл" на главной.

Возвращает NDJSON (newline-delimited JSON) поток с прогрессом:
  {"type":"step_start","message":"..."}
  {"type":"step_done","duration_ms":N}
  {"type":"step_error","error":"..."}
  {"type":"final","summary":"..."}

Это то же что seed_demo.py --full, но запускается из UI без терминала.
"""
import json
import time

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.models.tools import ToolRegistryInput
from app.services.consolidator import daily_consolidate, weekly_consolidate
from app.services.ingestor import save_raw_event
from app.services.operative import build_operative
from app.services.tools import register_tool

router = APIRouter(prefix="/demo", tags=["demo"])


SEED_EVENTS = [
    # memory_arch
    {"agent": "agent_designer", "domain": "memory_arch", "payload": {
        "task": "design 5-layer memory system",
        "decision": "isolate L1-L4 from agents, only OP is read-accessible",
        "feedback": "positive",
    }},
    {"agent": "agent_designer", "domain": "memory_arch", "payload": {
        "task": "decide consolidation cadence",
        "decision": "daily 02:00 UTC, weekly Monday 03:00",
        "feedback": "positive",
    }},
    {"agent": "agent_designer", "domain": "memory_arch", "payload": {
        "task": "choose LLM strategy for curator",
        "decision": "temperature=0.1 for strict quality",
        "feedback": "positive",
    }},
    {"agent": "agent_designer", "domain": "memory_arch", "payload": {
        "task": "implement L4 deduplication",
        "decision": "skip snapshot if SHA256 matches previous",
        "result": "snapshot space reduced 70%",
        "feedback": "positive",
    }},
    {"agent": "agent_designer", "domain": "memory_arch", "payload": {
        "task": "first attempt at vector search via text-only",
        "result": "low recall, missing semantically similar entries",
        "feedback": "negative",
        "lessons": "text search insufficient, need embeddings + KNN",
    }},
    {"agent": "agent_designer", "domain": "memory_arch", "payload": {
        "task": "switch to RediSearch FT.SEARCH with KNN",
        "result": "recall improved 3x at top-5",
        "feedback": "positive",
    }},
    # fastapi_dev
    {"agent": "agent_developer", "domain": "fastapi_dev", "payload": {
        "task": "configure CORS for sandbox UI",
        "result": "browser blocked credentials with allow_origins=*",
        "feedback": "negative",
        "lessons": "either restrict origins OR set credentials=false",
    }},
    {"agent": "agent_developer", "domain": "fastapi_dev", "payload": {
        "task": "add lifespan handler for startup/shutdown",
        "decision": "use @asynccontextmanager pattern",
        "feedback": "positive",
    }},
    {"agent": "agent_developer", "domain": "fastapi_dev", "payload": {
        "task": "switch to asyncio.create_task for background worker",
        "result": "no more blocking on long LLM calls",
        "feedback": "positive",
    }},
    {"agent": "agent_developer", "domain": "fastapi_dev", "payload": {
        "task": "add pydantic-settings for env config",
        "decision": "use SettingsConfigDict instead of class Config",
        "feedback": "positive",
    }},
    {"agent": "agent_developer", "domain": "fastapi_dev", "payload": {
        "task": "add Prometheus middleware",
        "result": "track HTTP latency p50/p95",
        "feedback": "positive",
    }},
    {"agent": "agent_developer", "domain": "fastapi_dev", "payload": {
        "task": "tried lambda as dependency",
        "result": "fastapi rejected, needed callable",
        "feedback": "negative",
    }},
    # deepseek_use
    {"agent": "agent_developer", "domain": "deepseek_use", "payload": {
        "task": "use deepseek-chat for daily analysis",
        "decision": "set response_format={'type':'json_object'}",
        "result": "JSON parsing success rate 99%",
        "feedback": "positive",
    }},
    {"agent": "agent_developer", "domain": "deepseek_use", "payload": {
        "task": "tried deepseek embeddings endpoint",
        "result": "404 - endpoint does not exist",
        "feedback": "negative",
        "lessons": "DeepSeek does not provide embeddings API",
    }},
    {"agent": "agent_developer", "domain": "deepseek_use", "payload": {
        "task": "configure fallback chain: deepseek -> openai -> cached",
        "feedback": "positive",
    }},
    {"agent": "agent_developer", "domain": "deepseek_use", "payload": {
        "task": "delegate plan analysis to DeepSeek (chunked)",
        "result": "16k tokens delegated, 5kb summary returned",
        "feedback": "positive",
    }},
    {"agent": "agent_developer", "domain": "deepseek_use", "payload": {
        "task": "try deepseek-reasoner for complex arbitration",
        "result": "better quality but 5x latency",
        "feedback": "neutral",
    }},
    {"agent": "agent_developer", "domain": "deepseek_use", "payload": {
        "task": "check rate-limits of DeepSeek API",
        "result": "comfortable up to 50 RPM on default tier",
        "feedback": "positive",
    }},
]

SEED_TOOLS = [
    {"domain": "memory_arch", "tool_name": "redis-stack-knn", "tool_type": "service",
     "description": "RediSearch FT.SEARCH with KNN over FLOAT32 vectors",
     "config_schema": {"index_prefix": "string"},
     "usage_patterns": {"when": "semantic-search-over-l3"}},
    {"domain": "deepseek_use", "tool_name": "deepseek-chat", "tool_type": "api",
     "description": "DeepSeek V4 Pro chat completions, OpenAI-compatible",
     "config_schema": {"base_url": "string", "model": "string"},
     "usage_patterns": {"when": "json-mode-analysis"}},
    {"domain": "fastapi_dev", "tool_name": "asyncpg-pool", "tool_type": "library",
     "description": "Async Postgres pool, min_size=2 max_size=10",
     "config_schema": {"dsn": "string"},
     "usage_patterns": {"when": "concurrent-db-access"}},
]


async def _stream_demo():
    """Генератор NDJSON-событий для StreamingResponse."""

    def emit(payload: dict) -> bytes:
        return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")

    counts = {"events": 0, "tools": 0, "buffers": 0, "knowledge": 0}

    # === Step 1: events ===
    yield emit({"type": "step_start", "message": f"Заливаем {len(SEED_EVENTS)} событий в L1 (3 домена)"})
    t0 = time.monotonic()
    for ev in SEED_EVENTS:
        try:
            await save_raw_event(
                agent_id=ev["agent"],
                domain=ev["domain"],
                payload=ev["payload"],
            )
            counts["events"] += 1
        except Exception as e:
            yield emit({"type": "step_error", "error": str(e)[:200]})
    yield emit({"type": "step_done", "duration_ms": int((time.monotonic() - t0) * 1000)})

    # === Step 2: tools ===
    yield emit({"type": "step_start", "message": f"Регистрируем {len(SEED_TOOLS)} инструментов"})
    t0 = time.monotonic()
    for t in SEED_TOOLS:
        try:
            await register_tool(ToolRegistryInput(**t))
            counts["tools"] += 1
        except Exception as e:
            yield emit({"type": "step_error", "error": str(e)[:200]})
    yield emit({"type": "step_done", "duration_ms": int((time.monotonic() - t0) * 1000)})

    # === Steps 3-5: daily consolidation per domain ===
    for dom in ["memory_arch", "fastapi_dev", "deepseek_use"]:
        yield emit({"type": "step_start", "message": f"Daily L1→L2: домен «{dom}» (DeepSeek)"})
        t0 = time.monotonic()
        try:
            res = await daily_consolidate(domain=dom)
            ok = res.get("status") == "ok"
            for r in res.get("results", []):
                if r.get("status") == "consolidated":
                    counts["buffers"] += 1
            yield emit({
                "type": "step_done" if ok else "step_error",
                "duration_ms": int((time.monotonic() - t0) * 1000),
                "error": "" if ok else str(res)[:200],
            })
        except Exception as e:
            yield emit({"type": "step_error", "error": str(e)[:200]})

    # === Steps 6-8: weekly consolidation per domain ===
    for dom in ["memory_arch", "fastapi_dev", "deepseek_use"]:
        yield emit({"type": "step_start", "message": f"Weekly L2→L3: домен «{dom}» (DeepSeek + Curator)"})
        t0 = time.monotonic()
        try:
            res = await weekly_consolidate(dom)
            ok = res.get("status") == "consolidated"
            counts["knowledge"] += res.get("new_items", 0)
            yield emit({
                "type": "step_done" if ok else "step_error",
                "duration_ms": int((time.monotonic() - t0) * 1000),
                "error": "" if ok else str(res)[:200],
            })
        except Exception as e:
            yield emit({"type": "step_error", "error": str(e)[:200]})

    # === Step 9: KNN queries ===
    queries = [
        ("memory_arch", "How to deduplicate snapshots in L4"),
        ("fastapi_dev", "CORS configuration with credentials"),
        ("deepseek_use", "JSON output mode for structured analysis"),
    ]
    for dom, q in queries:
        yield emit({"type": "step_start", "message": f"KNN-поиск в «{dom}»: «{q}»"})
        t0 = time.monotonic()
        try:
            results = await build_operative(query=q, domain=dom, top_k=3)
            yield emit({
                "type": "step_done",
                "duration_ms": int((time.monotonic() - t0) * 1000),
                "found": len(results),
            })
        except Exception as e:
            yield emit({"type": "step_error", "error": str(e)[:200]})

    # === Final ===
    yield emit({
        "type": "final",
        "summary": f"L1 +{counts['events']} | L2 +{counts['buffers']} | L3 +{counts['knowledge']} знаний | tools +{counts['tools']}",
        "counts": counts,
    })


@router.post("/run")
async def run_demo():
    """Запустить полный демо-цикл с streaming-прогрессом."""
    return StreamingResponse(_stream_demo(), media_type="application/x-ndjson")
