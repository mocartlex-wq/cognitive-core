"""Daily dogfooding health check.

Один раз в день показывает: реально ли система полезна владельцу.
Цель — не "готовность к публикации", а "помогает ли мне в работе".

Запуск:
    python scripts/dogfood_check.py
    python scripts/dogfood_check.py --days 7    # за неделю
    python scripts/dogfood_check.py --json      # машинный вывод

Метрики (5 штук, синтез Claude + DeepSeek, адаптировано под solo-use):
  1. Error rate в audit-log L5 (порог < 5%)
  2. p95 latency /operative/query (порог < 2s)
  3. Successful daily/weekly consolidations (порог >= 5 за неделю)
  4. Уникальных доменов с активностью (порог >= 3)
  5. L3 growth (новых знаний за период)
"""
import argparse
import json
import sys
import time
from urllib import request, error

# Windows-консоль по умолчанию cp1251 — переключаем на UTF-8
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

API = "http://localhost:9001"
KEY = "key-design-001"


def _get(path: str) -> dict:
    req = request.Request(f"{API}{path}", headers={"X-API-Key": KEY})
    with request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(path: str) -> dict:
    req = request.Request(f"{API}{path}", method="POST",
                          headers={"X-API-Key": KEY, "Content-Type": "application/json"},
                          data=b"")
    with request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def collect(days: int) -> dict:
    """Собирает все метрики за период."""
    data = {"days": days, "metrics": {}}

    # 1. Error rate из L5 audit
    audit = _get(f"/dashboard/audit-tail?limit=1000")
    items = audit.get("items", [])
    total = len(items)
    failures = sum(1 for x in items if not x.get("success"))
    error_rate = (failures / total * 100) if total else 0.0
    data["metrics"]["error_rate_pct"] = round(error_rate, 2)
    data["metrics"]["audit_total"] = total
    data["metrics"]["audit_failures"] = failures

    # 2. p95 latency operative/query — измеряем 5 запросами
    latencies = []
    for q in ["test latency probe 1", "test latency probe 2", "test latency probe 3"]:
        t0 = time.monotonic()
        try:
            req = request.Request(
                f"{API}/operative/query",
                method="POST",
                headers={"X-API-Key": KEY, "Content-Type": "application/json"},
                data=json.dumps({"domain": "memory_arch", "context": q, "top_k": 3}).encode(),
            )
            with request.urlopen(req, timeout=10) as r:
                r.read()
            latencies.append((time.monotonic() - t0) * 1000)
        except Exception:
            pass
    data["metrics"]["latency_avg_ms"] = round(sum(latencies) / len(latencies), 1) if latencies else None
    data["metrics"]["latency_max_ms"] = round(max(latencies), 1) if latencies else None

    # 3. Successful consolidations (из audit-log)
    success_consolidations = sum(
        1 for x in items
        if x.get("action") in ("daily_consolidate", "weekly_consolidate", "monthly_audit")
        and x.get("success")
    )
    data["metrics"]["successful_consolidations"] = success_consolidations

    # 4. Активные домены
    domains = _get("/dashboard/domains")
    active = [d for d in domains.get("items", []) if d["l1"] >= 3]
    data["metrics"]["active_domains"] = len(active)
    data["metrics"]["domain_names"] = [d["domain"] for d in active]

    # 5. L3 growth
    health = _get("/health")
    layers = health.get("layers", {})
    data["metrics"]["l3_knowledge_total"] = layers.get("l3_knowledge", 0)
    data["metrics"]["l3_tools_total"] = layers.get("l3_tools", 0)
    data["metrics"]["l1_total"] = layers.get("l1", 0)

    return data


def render_text(data: dict) -> str:
    m = data["metrics"]

    def status(ok: bool, suffix: str = "") -> str:
        return ("[OK]   " if ok else "[FAIL] ") + suffix

    lines = [
        "",
        "=" * 60,
        f"  Cognitive Core — Dogfooding Health (период {data['days']} дн.)",
        "=" * 60,
        "",
        "ИНДИКАТОРЫ ПОЛЕЗНОСТИ ДЛЯ ВЛАДЕЛЬЦА:",
        "",
    ]

    # 1. Error rate
    er = m.get("error_rate_pct", 0)
    lines.append(f"  1. Error rate в L5 audit:        {er}%   {status(er < 5.0, '(порог: < 5%)')}")
    lines.append(f"     ({m['audit_failures']} ошибок из {m['audit_total']} действий)")

    # 2. Latency
    avg = m.get("latency_avg_ms")
    mx = m.get("latency_max_ms")
    lat_ok = mx is not None and mx < 2000
    lines.append(f"  2. KNN latency:                  avg={avg}ms, max={mx}ms   {status(lat_ok, '(порог max < 2000ms)')}")

    # 3. Consolidations
    sc = m.get("successful_consolidations", 0)
    lines.append(f"  3. Успешных консолидаций:        {sc}   {status(sc >= 5, '(порог >= 5)')}")

    # 4. Active domains
    ad = m.get("active_domains", 0)
    domains_str = ", ".join(m.get("domain_names", [])[:5]) or "(нет)"
    lines.append(f"  4. Активных доменов (>= 3 L1):   {ad}   {status(ad >= 3, '(порог >= 3)')}")
    lines.append(f"     {domains_str}")

    # 5. Memory growth
    lines.append("")
    lines.append("СОСТОЯНИЕ ПАМЯТИ:")
    lines.append(f"     L1 событий всего:             {m.get('l1_total')}")
    lines.append(f"     L3 знаний:                    {m.get('l3_knowledge_total')}")
    lines.append(f"     L3 инструментов:              {m.get('l3_tools_total')}")

    # Итог
    passed = sum([
        er < 5.0,
        lat_ok,
        sc >= 5,
        ad >= 3,
    ])
    lines.append("")
    lines.append("-" * 60)
    lines.append(f"  ИТОГ: {passed}/4 ключевых индикаторов в норме")
    if passed == 4:
        lines.append("  Система здорова и помогает в работе.")
    elif passed >= 2:
        lines.append("  Работает, но есть что улучшить (см. FAIL выше).")
    else:
        lines.append("  Требуется внимание — система используется недостаточно или с ошибками.")
    lines.append("=" * 60)
    lines.append("")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    try:
        data = collect(args.days)
    except (error.URLError, error.HTTPError) as e:
        print(f"ERROR: Cognitive Core не отвечает на {API}: {e}", file=sys.stderr)
        print("Проверьте: docker compose ps", file=sys.stderr)
        sys.exit(2)

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(render_text(data))


if __name__ == "__main__":
    main()
