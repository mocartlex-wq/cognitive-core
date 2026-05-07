"""Seed demo data into Cognitive Core for end-to-end testing.

Загружает реалистичные события в 3 домена, чтобы система имела с чем работать.
После seed — можно вручную или автоматически запустить consolidate/query.

Usage:
    python scripts/seed_demo.py
    python scripts/seed_demo.py --consolidate    # + запустить daily/weekly
    python scripts/seed_demo.py --full           # + operative queries
"""
import json
import sys
import time
from urllib import request, error

API = "http://localhost:9001"
KEY_DESIGN = "key-design-001"
KEY_DEV = "key-dev-001"


def _post(path: str, payload: dict, key: str, query: dict | None = None) -> dict:
    if query:
        from urllib.parse import urlencode
        path = f"{path}?{urlencode(query)}"
    body = json.dumps(payload).encode("utf-8") if payload else b""
    req = request.Request(
        f"{API}{path}",
        data=body,
        headers={"Content-Type": "application/json", "X-API-Key": key},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as e:
        return {"error": e.code, "body": e.read().decode("utf-8", errors="ignore")[:500]}
    except Exception as e:
        return {"error": str(e)}


def _get(path: str, key: str = KEY_DESIGN) -> dict:
    req = request.Request(f"{API}{path}", headers={"X-API-Key": key})
    try:
        with request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"error": str(e)}


# ============ Семена событий ============
SEED_EVENTS = [
    # === Домен: memory_arch (рассуждения про архитектуру памяти) ===
    {"agent": "agent_designer", "domain": "memory_arch", "payload": {
        "task": "design 5-layer memory system",
        "decision": "isolate L1-L4 from agents, only OP is read-accessible",
        "rationale": "prevents direct mutation of master knowledge",
        "feedback": "positive",
        "tools_used": ["postgresql", "redis-stack"],
    }},
    {"agent": "agent_designer", "domain": "memory_arch", "payload": {
        "task": "decide consolidation cadence",
        "decision": "daily L1->L2 at 02:00 UTC, weekly L2->L3 on Monday 03:00",
        "rationale": "off-peak hours, allows accumulation",
        "feedback": "positive",
    }},
    {"agent": "agent_designer", "domain": "memory_arch", "payload": {
        "task": "choose LLM strategy for curator",
        "decision": "temperature=0.1 for strict quality, separate from analyzer",
        "rationale": "curator needs determinism, analyzer needs creativity",
        "feedback": "positive",
        "lessons": "always separate models by cognitive function",
    }},
    {"agent": "agent_designer", "domain": "memory_arch", "payload": {
        "task": "implement L4 deduplication",
        "decision": "skip snapshot if SHA256 matches previous, delta if <20% changes",
        "feedback": "positive",
        "result": "snapshot space reduced 70%",
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
        "lessons": "vector index essential for semantic memory",
    }},
    {"agent": "agent_designer", "domain": "memory_arch", "payload": {
        "task": "missed parallel-instance race in consolidator",
        "result": "duplicate L2 entries during concurrent daily runs",
        "feedback": "negative",
        "lessons": "need distributed lock (Redis SETNX) for cross-instance idempotency",
    }},

    # === Домен: fastapi_dev (опыт работы с FastAPI) ===
    {"agent": "agent_developer", "domain": "fastapi_dev", "payload": {
        "task": "configure CORS for sandbox UI",
        "result": "browser blocked credentials with allow_origins=*",
        "feedback": "negative",
        "lessons": "either restrict origins OR set credentials=false",
    }},
    {"agent": "agent_developer", "domain": "fastapi_dev", "payload": {
        "task": "add lifespan handler for startup/shutdown",
        "decision": "use @asynccontextmanager pattern, not @on_event",
        "rationale": "@on_event is deprecated since FastAPI 0.93",
        "feedback": "positive",
    }},
    {"agent": "agent_developer", "domain": "fastapi_dev", "payload": {
        "task": "switch from threading to asyncio.create_task for background worker",
        "result": "no more blocking on long LLM calls",
        "feedback": "positive",
        "tools_used": ["asyncio", "fastapi"],
    }},
    {"agent": "agent_developer", "domain": "fastapi_dev", "payload": {
        "task": "add pydantic-settings for env config",
        "decision": "use SettingsConfigDict instead of class Config",
        "rationale": "deprecated in pydantic v2",
        "feedback": "positive",
    }},
    {"agent": "agent_developer", "domain": "fastapi_dev", "payload": {
        "task": "add Prometheus middleware",
        "result": "track HTTP latency p50/p95, LLM call success rates",
        "tools_used": ["prometheus_client"],
        "feedback": "positive",
    }},
    {"agent": "agent_developer", "domain": "fastapi_dev", "payload": {
        "task": "tried lambda as dependency",
        "result": "fastapi rejected, needed callable",
        "feedback": "negative",
        "lessons": "Depends() expects callable, not lambda directly",
    }},

    # === Домен: deepseek_use (опыт использования DeepSeek API) ===
    {"agent": "agent_developer", "domain": "deepseek_use", "payload": {
        "task": "use deepseek-chat for daily analysis",
        "decision": "set response_format={'type':'json_object'} for structured output",
        "result": "JSON parsing success rate 99%",
        "feedback": "positive",
    }},
    {"agent": "agent_developer", "domain": "deepseek_use", "payload": {
        "task": "tried deepseek embeddings endpoint",
        "result": "404 - endpoint does not exist",
        "feedback": "negative",
        "lessons": "DeepSeek does not provide embeddings API, use fastembed/openai/ollama",
    }},
    {"agent": "agent_developer", "domain": "deepseek_use", "payload": {
        "task": "configure fallback chain: deepseek -> openai -> cached",
        "result": "system survives DeepSeek outages",
        "feedback": "positive",
        "tools_used": ["openai-sdk-fallback"],
    }},
    {"agent": "agent_developer", "domain": "deepseek_use", "payload": {
        "task": "delegate plan analysis to DeepSeek (chunked)",
        "result": "16k tokens on DeepSeek side, ~5kb summary back, my context saved",
        "feedback": "positive",
        "lessons": "delegate large file analysis to cheap LLM, keep main agent for synthesis",
    }},
    {"agent": "agent_developer", "domain": "deepseek_use", "payload": {
        "task": "try deepseek-reasoner for complex arbitration",
        "result": "better quality on conflict resolution but 5x latency",
        "feedback": "neutral",
        "lessons": "use reasoner only for arbitration, not routine analysis",
    }},
]

# ============ Инструменты для регистрации ============
SEED_TOOLS = [
    {"agent": "agent_designer", "domain": "memory_arch", "payload": {
        "tool_name": "redis-stack-knn",
        "tool_type": "service",
        "description": "RediSearch FT.SEARCH with KNN over FLOAT32 vectors",
        "config_schema": {"index_prefix": "string", "vector_field": "string", "dim": "int"},
        "usage_patterns": {"when": "semantic-search-over-l3", "example": "FT.SEARCH idx:operative @domain:{x}=>[KNN 5 @embedding $vec]"},
    }},
    {"agent": "agent_developer", "domain": "deepseek_use", "payload": {
        "tool_name": "deepseek-chat",
        "tool_type": "api",
        "description": "DeepSeek V4 Pro chat completions, OpenAI-compatible",
        "config_schema": {"base_url": "string", "model": "string", "temperature": "float"},
        "usage_patterns": {"when": "json-mode-analysis", "tip": "set response_format={'type':'json_object'}"},
    }},
    {"agent": "agent_developer", "domain": "fastapi_dev", "payload": {
        "tool_name": "asyncpg-pool",
        "tool_type": "library",
        "description": "Async Postgres pool, min_size=2 max_size=10",
        "config_schema": {"dsn": "string", "min_size": "int", "max_size": "int"},
        "usage_patterns": {"when": "concurrent-db-access", "tip": "always async with pool.acquire()"},
    }},
]


def main():
    consolidate = "--consolidate" in sys.argv or "--full" in sys.argv
    full = "--full" in sys.argv

    print("=" * 60)
    print("[1/5] Health check")
    print("=" * 60)
    h = _get("/health")
    print(f"  healthy: {h.get('healthy')}, services: {h.get('services')}")
    print(f"  layers: {h.get('layers')}")

    print("\n" + "=" * 60)
    print(f"[2/5] Seeding {len(SEED_EVENTS)} events into 3 domains")
    print("=" * 60)
    success = 0
    for i, ev in enumerate(SEED_EVENTS, 1):
        key = KEY_DESIGN if ev["agent"] == "agent_designer" else KEY_DEV
        result = _post("/events", {
            "source_agent": ev["agent"],
            "domain": ev["domain"],
            "payload": ev["payload"],
        }, key)
        ok = "id" in result
        success += int(ok)
        marker = "OK" if ok else "FAIL"
        print(f"  [{i:2d}/{len(SEED_EVENTS)}] {marker} {ev['domain']:15s} | {ev['payload'].get('task', '?')[:50]}")
        if not ok:
            print(f"      -> {result}")
    print(f"\n  Total: {success}/{len(SEED_EVENTS)} events accepted")

    print("\n" + "=" * 60)
    print(f"[3/5] Registering {len(SEED_TOOLS)} tools")
    print("=" * 60)
    for t in SEED_TOOLS:
        key = KEY_DESIGN if t["agent"] == "agent_designer" else KEY_DEV
        result = _post("/tools", t["payload"] | {"domain": t["domain"]}, key)
        ok = "id" in result or "tool_id" in result
        print(f"  {'OK' if ok else 'FAIL'}  {t['payload']['tool_name']:25s} ({t['domain']})")
        if not ok:
            print(f"      -> {result}")

    if not consolidate:
        print("\n" + "=" * 60)
        print("[4/5] Skipped (no --consolidate flag)")
        print("[5/5] Skipped (no --full flag)")
        print("=" * 60)
        print("\nDone. Open http://localhost:9001/ui to inspect.")
        print("Run with --consolidate to trigger daily/weekly LLM passes.")
        return

    print("\n" + "=" * 60)
    print("[4/5] Daily consolidation L1 -> L2 (per domain)")
    print("=" * 60)
    for dom in ["memory_arch", "fastapi_dev", "deepseek_use"]:
        t0 = time.time()
        result = _post("/memory/consolidate/daily", None, KEY_DESIGN, query={"domain": dom})
        dt = time.time() - t0
        print(f"  {dom:15s} | {dt:5.1f}s | {json.dumps(result, ensure_ascii=False)[:200]}")

    print("\n  Weekly consolidation L2 -> L3")
    for dom in ["memory_arch", "fastapi_dev", "deepseek_use"]:
        t0 = time.time()
        result = _post("/memory/consolidate/weekly", None, KEY_DESIGN, query={"domain": dom})
        dt = time.time() - t0
        print(f"  {dom:15s} | {dt:5.1f}s | {json.dumps(result, ensure_ascii=False)[:200]}")

    print("\n  Health after consolidation:")
    h = _get("/health")
    print(f"  layers: {h.get('layers')}")

    if not full:
        print("\n[5/5] Skipped (no --full flag)")
        return

    print("\n" + "=" * 60)
    print("[5/5] Operative queries (KNN search)")
    print("=" * 60)
    queries = [
        ("memory_arch", "How to deduplicate snapshots in L4"),
        ("fastapi_dev", "CORS configuration with credentials"),
        ("deepseek_use", "JSON output mode for structured analysis"),
    ]
    for domain, query in queries:
        t0 = time.time()
        result = _post("/operative/query", {
            "domain": domain,
            "context": query,
            "top_k": 3,
        }, KEY_DESIGN)
        dt = time.time() - t0
        n = len(result.get("results", []))
        print(f"\n  Q: '{query}' (domain={domain}) [{dt:.2f}s, {n} hits]")
        for r in result.get("results", [])[:3]:
            kind = r.get("record_type", "?")
            dist = r.get("distance", 0)
            preview = json.dumps(r.get("content") or r.get("usage") or {}, ensure_ascii=False)[:120]
            print(f"    [{kind}] dist={dist:.3f}  {preview}")

    print("\n" + "=" * 60)
    print("Done. Visit http://localhost:9001/ui for visual inspection.")
    print("=" * 60)


if __name__ == "__main__":
    main()
