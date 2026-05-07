"""DeepSeek в роли третьего архитектора.

Передаёт DeepSeek контекст проекта + список открытых дизайн-вопросов.
Получает структурированное мнение по каждому: рекомендация, обоснование, риски.

Usage:
    python scripts/consult_deepseek.py questions.json
    python scripts/consult_deepseek.py --inline "Q1?" "Q2?"
    python scripts/consult_deepseek.py --from-file questions.txt
"""
import json
import sys
import time
from pathlib import Path
from urllib import request, error

ROOT = Path(__file__).resolve().parent.parent


def load_env() -> dict:
    env = {}
    p = ROOT / ".env"
    if not p.exists():
        return env
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


ENV = load_env()
API_KEY = ENV.get("DEEPSEEK_API_KEY", "")
BASE_URL = ENV.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")

PROJECT_CONTEXT = """
Cognitive Core v0.2.0 — 5-layer persistent memory system for AI agents.

STACK:
- Python 3.11 + FastAPI + asyncpg (Postgres 16) + Redis Stack (RediSearch) + MinIO (S3)
- LLM: DeepSeek V4 Pro (chat), fastembed multilingual-e5-small (384-dim embeddings, local CPU)
- Docker Compose, ~4200 LOC, 69 tests passing

ARCHITECTURE (5 layers):
- L1 raw_events (Postgres) — sole entry point for events
- L2 daily_buffers — daily LLM consolidation by domain
- L3 master_knowledge + tools_registry — weekly consolidation, source of truth
- L4 snapshots (MinIO) — backup/restore, currently full snapshots only (plan asked for delta)
- L5 audit_log — every action logged
- OP — operative workspace, RediSearch KNN over L3 (24h TTL)

CURRENT STATE:
- 200 L1 events, 13 L2 buffers, 61 L3 knowledge, 41 L3 tools, 25 L4 snapshots
- Real embeddings working via fastembed (384-dim)
- 13 domains with real LLM-extracted knowledge
- Dashboard at /ui + sandbox at /
- Single API instance, scheduler in same process via asyncio
- No git repo yet, no pgvector, no Alembic migrations, no MCP server

NICHE: self-hosted enterprise memory with explicit consolidation cycles, audit-log,
multilingual prompts (8 langs), AI-curator quality gates. Competitors:
Mem0 (flat memory + graph), Letta (memory blocks), Zep (temporal graph), Cognee.
"""

CONSULT_SYSTEM = """You are a senior software architect helping evaluate design decisions
for a self-hosted AI memory system (Cognitive Core). You are the third reviewer alongside
the user (project owner) and Claude (implementing engineer).

For EACH question, output:
- recommendation: ONE clear choice (max 100 chars)
- why: 1-2 sentences with reasoning grounded in the actual project context
- alternative_considered: what you rejected and why
- risk_if_wrong: what breaks if recommendation is bad fit
- effort_estimate: trivial | small | medium | large
- priority: blocker | high | medium | low

Be opinionated — no fence-sitting. If the question is too vague, say so explicitly.
Output STRICT JSON only.
"""


def call_deepseek(messages: list, model: str = "deepseek-chat") -> dict:
    if not API_KEY:
        return {"ok": False, "error": "DEEPSEEK_API_KEY missing"}
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 6000,
        "response_format": {"type": "json_object"},
    }
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        f"{BASE_URL}/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(3):
        try:
            with request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                content = data["choices"][0]["message"]["content"]
                tokens = data.get("usage", {})
                try:
                    return {"ok": True, "data": json.loads(content), "tokens": tokens}
                except json.JSONDecodeError:
                    return {"ok": True, "data": {"raw": content}, "tokens": tokens}
        except error.HTTPError as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return {"ok": False, "error": f"HTTP {e.code}: {e.read().decode('utf-8', errors='ignore')[:300]}"}
        except Exception as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return {"ok": False, "error": str(e)}
    return {"ok": False, "error": "exhausted retries"}


def consult(questions: list[str]) -> dict:
    """Передаёт список вопросов DeepSeek, получает мнение по каждому."""
    user_prompt = (
        f"PROJECT CONTEXT:\n{PROJECT_CONTEXT}\n\n"
        f"OPEN DESIGN QUESTIONS ({len(questions)}):\n"
        + "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions))
        + "\n\nReturn JSON: {\"opinions\": [{\"question_index\": N, \"question\": \"...\","
        " \"recommendation\": \"...\", \"why\": \"...\","
        " \"alternative_considered\": \"...\", \"risk_if_wrong\": \"...\","
        " \"effort_estimate\": \"...\", \"priority\": \"...\"}]}"
    )
    return call_deepseek([
        {"role": "system", "content": CONSULT_SYSTEM},
        {"role": "user", "content": user_prompt},
    ])


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    questions = []
    if sys.argv[1] == "--inline":
        questions = sys.argv[2:]
    elif sys.argv[1] == "--from-file":
        path = Path(sys.argv[2])
        questions = [l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
    else:
        path = Path(sys.argv[1])
        data = json.loads(path.read_text(encoding="utf-8"))
        questions = data.get("questions", []) if isinstance(data, dict) else data

    if not questions:
        print("ERROR: no questions provided", file=sys.stderr)
        sys.exit(1)

    print(f"Consulting DeepSeek on {len(questions)} questions...", file=sys.stderr)
    result = consult(questions)

    out_dir = ROOT / "scripts" / "deepseek_out"
    out_dir.mkdir(exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"consult_{ts}.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n[saved to {out_path}]", file=sys.stderr)


if __name__ == "__main__":
    main()
