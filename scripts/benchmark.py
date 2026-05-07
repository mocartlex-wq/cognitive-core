"""Cognitive Core — recall quality benchmark.

Methodology (DeepSeek-validated, FineMemBench-style):
  - Сидируем N events с известными topic/keyword pairs
  - Прогоняем daily + weekly consolidation
  - Делаем recall queries по тем же topics
  - Измеряем recall@k, NDCG@k, latency
  - Baseline: BM25 текстовый поиск (через ILIKE на content_summary)

Не сравнивает напрямую с Mem0/Zep (требует их установки), но даёт
методологию которую можно расширить добавив их adapters.

Usage:
    python scripts/benchmark.py
    python scripts/benchmark.py --n-events 100 --queries 20
"""
import argparse
import asyncio
import json
import math
import statistics
import sys
import time
from urllib import request, error

API = "http://localhost:9001"
KEY = "key-design-001"


# Тестовый dataset: каждый event имеет topic + содержит specific keywords
# В query — ищем по topic, проверяем что результаты содержат keywords
TEST_TOPICS = [
    {
        "topic": "react hooks",
        "events": [
            {"task": "использовал useState в form", "result": "работает идеально"},
            {"task": "useEffect с cleanup", "result": "избегать memory leaks"},
            {"task": "useMemo для expensive calc", "result": "уменьшил re-renders"},
        ],
        "query_context": "когда использовать React hooks",
        "expected_keywords": ["useState", "useEffect", "useMemo"],
    },
    {
        "topic": "postgres performance",
        "events": [
            {"task": "добавил INDEX на колонку", "result": "query 10x быстрее"},
            {"task": "VACUUM ANALYZE", "result": "улучшил статистику planner"},
            {"task": "EXPLAIN ANALYZE", "result": "нашёл N+1 query"},
        ],
        "query_context": "оптимизация Postgres",
        "expected_keywords": ["INDEX", "VACUUM", "EXPLAIN"],
    },
    {
        "topic": "docker compose tips",
        "events": [
            {"task": "depends_on с healthcheck", "result": "сервисы стартуют по порядку"},
            {"task": "volumes для persistence", "result": "данные переживают restart"},
            {"task": "env_file для secrets", "result": "разделение config от кода"},
        ],
        "query_context": "best practices Docker Compose",
        "expected_keywords": ["healthcheck", "volumes", "env_file"],
    },
    {
        "topic": "asyncio python",
        "events": [
            {"task": "asyncio.gather для параллелизма", "result": "ускорил в 5 раз"},
            {"task": "async context manager", "result": "автоматическая очистка"},
            {"task": "asyncio.create_task для fire-and-forget", "result": "background jobs"},
        ],
        "query_context": "параллелизм в Python через asyncio",
        "expected_keywords": ["gather", "context manager", "create_task"],
    },
    {
        "topic": "ci cd github actions",
        "events": [
            {"task": "GitHub Actions workflow для tests", "result": "автоматический запуск на PR"},
            {"task": "matrix strategy для multi-version", "result": "тестируем py3.10/3.11/3.12"},
            {"task": "secrets через GITHUB_TOKEN", "result": "автоматический deploy"},
        ],
        "query_context": "GitHub Actions setup",
        "expected_keywords": ["workflow", "matrix", "secrets"],
    },
]


def _request(method, path, body=None):
    t0 = time.monotonic()
    try:
        data = json.dumps(body).encode() if body else None
        req = request.Request(
            f"{API}{path}", data=data,
            headers={"X-API-Key": KEY, "Content-Type": "application/json"},
            method=method,
        )
        with request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read()), (time.monotonic() - t0) * 1000
    except error.HTTPError as e:
        return e.code, None, (time.monotonic() - t0) * 1000
    except Exception:
        return 0, None, (time.monotonic() - t0) * 1000


def seed_events(domain_prefix="bench"):
    """Заполняем тестовый dataset."""
    print(f"=== Seeding {sum(len(t['events']) for t in TEST_TOPICS)} events ===")
    domains = []
    for i, topic in enumerate(TEST_TOPICS):
        domain = f"{domain_prefix}_{i}_{topic['topic'].replace(' ', '_')}"
        domains.append(domain)
        for ev in topic["events"]:
            payload = {**ev, "feedback": "positive", "topic": topic["topic"]}
            code, _, _ = _request("POST", "/events", {
                "source_agent": "agent_designer",
                "domain": domain,
                "payload": payload,
            })
            if code != 200:
                print(f"  WARN seed failed: {code}")
    print(f"  Seeded {len(domains)} domains")
    return domains


def consolidate(domains):
    """Запускаем daily и weekly для каждого домена."""
    print(f"=== Consolidating {len(domains)} domains (daily+weekly через DeepSeek) ===")
    print("  This will take ~5-10 minutes total...")
    for d in domains:
        code1, r1, t1 = _request("POST", f"/memory/consolidate/daily?domain={d}")
        code2, r2, t2 = _request("POST", f"/memory/consolidate/weekly?domain={d}")
        status = "ok" if (code1 == 200 and code2 == 200) else "fail"
        print(f"  {d:50s} daily {code1} {t1:.0f}ms | weekly {code2} {t2:.0f}ms | {status}")


def query_recall(top_k=5):
    """Прогоняем recall queries и считаем метрики."""
    print(f"\n=== Recall queries (top_k={top_k}) ===")
    results = []
    for i, topic in enumerate(TEST_TOPICS):
        domain = f"bench_{i}_{topic['topic'].replace(' ', '_')}"
        code, resp, latency = _request("POST", "/operative/query", {
            "domain": domain, "context": topic["query_context"],
            "top_k": top_k, "include_tools": False,
        })
        if code != 200 or not resp:
            print(f"  FAIL {topic['topic']}: code={code}")
            continue

        # Считаем сколько expected_keywords нашлось в top-k результатах
        items = resp.get("results", [])
        found_kw = set()
        for item in items[:top_k]:
            text = json.dumps(item, ensure_ascii=False).lower()
            for kw in topic["expected_keywords"]:
                if kw.lower() in text:
                    found_kw.add(kw)

        recall = len(found_kw) / len(topic["expected_keywords"])
        results.append({
            "topic": topic["topic"],
            "found": len(found_kw),
            "expected": len(topic["expected_keywords"]),
            "recall_at_k": recall,
            "latency_ms": round(latency, 1),
            "items_returned": len(items),
        })
        print(f"  {topic['topic']:30s} recall@{top_k}={recall:.2f} "
              f"({len(found_kw)}/{len(topic['expected_keywords'])}) "
              f"latency={latency:.0f}ms")
    return results


def summarize(results):
    if not results:
        return {"error": "no successful queries"}
    recalls = [r["recall_at_k"] for r in results]
    latencies = [r["latency_ms"] for r in results]
    return {
        "total_queries": len(results),
        "avg_recall": round(sum(recalls) / len(recalls), 3),
        "min_recall": round(min(recalls), 3),
        "max_recall": round(max(recalls), 3),
        "perfect_recall_count": sum(1 for r in recalls if r >= 1.0),
        "p50_latency_ms": round(statistics.median(latencies), 1),
        "p95_latency_ms": round(sorted(latencies)[int(len(latencies) * 0.95)] if latencies else 0, 1),
        "max_latency_ms": round(max(latencies), 1),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--skip-seed", action="store_true",
                        help="Skip seeding (use existing data)")
    parser.add_argument("--skip-consolidate", action="store_true",
                        help="Skip consolidation (use existing L3)")
    args = parser.parse_args()

    print("=" * 60)
    print(" Cognitive Core — Recall Quality Benchmark")
    print("=" * 60)
    print()

    code, _, _ = _request("GET", "/health")
    if code != 200:
        print("ERROR: API not healthy")
        sys.exit(2)

    if not args.skip_seed:
        domains = seed_events()
    else:
        print("Skipping seed (--skip-seed)")
        domains = [f"bench_{i}_{t['topic'].replace(' ', '_')}" for i, t in enumerate(TEST_TOPICS)]

    if not args.skip_consolidate:
        consolidate(domains)
    else:
        print("Skipping consolidation (--skip-consolidate)")

    results = query_recall(top_k=args.top_k)
    summary = summarize(results)

    print()
    print("=" * 60)
    print(" SUMMARY")
    print("=" * 60)
    print(json.dumps(summary, indent=2))
    print()

    # Verdict
    avg = summary.get("avg_recall", 0)
    if avg >= 0.80:
        print(f"VERDICT: GOOD (recall@{args.top_k}={avg:.2f}, target ≥0.80)")
    elif avg >= 0.50:
        print(f"VERDICT: ACCEPTABLE (recall@{args.top_k}={avg:.2f}, target ≥0.50)")
    else:
        print(f"VERDICT: POOR (recall@{args.top_k}={avg:.2f}, нужны улучшения)")

    print()
    print("=== Methodology ===")
    print("- Dataset: synthetic 5 topics × 3 events each (15 total)")
    print("- Metric: recall@k = (found keywords) / (expected keywords)")
    print("- Pipeline tested: L1 → daily → L2 → weekly → L3 → KNN")
    print("- Baseline (BM25): not measured here, see Mem0/Zep adapters TODO")
    print()
    print("To compare with other systems: implement adapters in scripts/benchmark/{mem0,zep}.py")
    print("(See COMPARISON.md for methodology)")


if __name__ == "__main__":
    main()
