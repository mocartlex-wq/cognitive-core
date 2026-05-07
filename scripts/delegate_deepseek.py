"""DeepSeek delegator — выгружает тяжёлый анализ на DeepSeek, экономит контекст Claude.

Usage:
    python scripts/delegate_deepseek.py analyze-plan
    python scripts/delegate_deepseek.py review-code <file>
    python scripts/delegate_deepseek.py freeform "<prompt>"
"""
import json
import os
import sys
import time
from pathlib import Path
from urllib import request, error

ROOT = Path(__file__).resolve().parent.parent

def load_env() -> dict:
    env = {}
    env_path = ROOT / ".env"
    if not env_path.exists():
        return env
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env

ENV = load_env()
API_KEY = ENV.get("DEEPSEEK_API_KEY", "")
BASE_URL = ENV.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")

if not API_KEY:
    print("ERROR: DEEPSEEK_API_KEY missing in .env", file=sys.stderr)
    sys.exit(1)


def call_deepseek(system: str, user: str, model: str = "deepseek-chat",
                  temperature: float = 0.3, json_mode: bool = True) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": 4096,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        f"{BASE_URL}/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    for attempt in range(3):
        try:
            with request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                content = data["choices"][0]["message"]["content"]
                tokens = data.get("usage", {})
                if json_mode:
                    try:
                        return {"ok": True, "data": json.loads(content), "tokens": tokens}
                    except json.JSONDecodeError:
                        return {"ok": True, "data": {"raw": content}, "tokens": tokens}
                return {"ok": True, "data": content, "tokens": tokens}
        except error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return {"ok": False, "error": f"HTTP {e.code}: {err_body[:500]}"}
        except Exception as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return {"ok": False, "error": str(e)}
    return {"ok": False, "error": "Exhausted retries"}


def chunk_text(text: str, chunk_size: int = 12000) -> list[str]:
    chunks = []
    for i in range(0, len(text), chunk_size):
        chunks.append(text[i:i + chunk_size])
    return chunks


def analyze_plan() -> dict:
    plan_path = Path("D:/ИИ/память/память 1/план.txt")
    if not plan_path.exists():
        return {"error": "plan not found"}
    text = plan_path.read_text(encoding="utf-8")
    chunks = chunk_text(text, 14000)
    summaries = []
    for i, ch in enumerate(chunks):
        print(f"[chunk {i+1}/{len(chunks)}] {len(ch)} chars", flush=True)
        result = call_deepseek(
            system=(
                "You are a senior architect reviewing a Russian-language technical spec for a 5-layer "
                "AI memory system. Extract only what is non-obvious or critical. Output JSON only."
            ),
            user=(
                f"Chunk {i+1}/{len(chunks)} of plan.txt. Extract:\n"
                "- key_design_decisions (3-7 items, each <120 chars)\n"
                "- non_obvious_constraints (subtle requirements that could be missed)\n"
                "- risks_or_gaps (where the plan is vague/contradictory)\n"
                "- chunk_topic (one short phrase)\n\n"
                "Reply JSON: {\"chunk_topic\":\"...\",\"key_design_decisions\":[...],"
                "\"non_obvious_constraints\":[...],\"risks_or_gaps\":[...]}\n\n"
                f"=== CHUNK ===\n{ch}"
            ),
            json_mode=True,
        )
        summaries.append({"chunk": i + 1, "result": result})
    # Финальный синтез
    print("[final synthesis]", flush=True)
    final = call_deepseek(
        system="You are synthesizing chunk-level reviews into one architectural summary.",
        user=(
            "Synthesize the following chunk-level reviews into ONE compact JSON:\n"
            "{\"top_design_decisions\":[5-10 items],\"top_constraints\":[5-10 items],"
            "\"top_risks\":[5-10 items],\"verdict\":\"one paragraph\"}\n\n"
            f"REVIEWS:\n{json.dumps(summaries, ensure_ascii=False)[:30000]}"
        ),
        json_mode=True,
    )
    return {"chunks": summaries, "synthesis": final}


def review_code(file_path: str) -> dict:
    p = Path(file_path)
    if not p.exists():
        return {"error": f"file not found: {file_path}"}
    code = p.read_text(encoding="utf-8")
    return call_deepseek(
        system="You are a senior Python code reviewer. Find real bugs, race conditions, security issues.",
        user=(
            f"Review this Python file ({file_path}). Output JSON:\n"
            "{\"bugs\":[{\"line\":N,\"severity\":\"low|med|high\",\"issue\":\"...\",\"fix\":\"...\"}],"
            "\"summary\":\"...\"}\n\n"
            f"=== CODE ===\n{code}"
        ),
        json_mode=True,
    )


def freeform(prompt: str) -> dict:
    return call_deepseek(
        system="Be concise, technical, output JSON when asked otherwise plain text.",
        user=prompt,
        json_mode=False,
    )


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    out_dir = ROOT / "scripts" / "deepseek_out"
    out_dir.mkdir(exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")

    if cmd == "analyze-plan":
        result = analyze_plan()
    elif cmd == "review-code":
        if len(sys.argv) < 3:
            print("Usage: review-code <file>", file=sys.stderr); sys.exit(1)
        result = review_code(sys.argv[2])
    elif cmd == "freeform":
        if len(sys.argv) < 3:
            print("Usage: freeform '<prompt>'", file=sys.stderr); sys.exit(1)
        result = freeform(" ".join(sys.argv[2:]))
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr); sys.exit(1)

    out_path = out_dir / f"{cmd}_{ts}.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OK -> {out_path}")


if __name__ == "__main__":
    main()
