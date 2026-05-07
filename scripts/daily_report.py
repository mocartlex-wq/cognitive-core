"""Daily report — что система выучила за сутки.

Показывает владельцу:
  - какие новые события записаны в L1 (по доменам)
  - какие L2 буферы созданы
  - какие новые L3 знания появились
  - какие KNN-запросы делались
  - какие ошибки были (если)

Запуск:
    python scripts/daily_report.py                 # за последние 24 часа
    python scripts/daily_report.py --days 7        # за неделю
    python scripts/daily_report.py --md > today.md # сохранить в markdown-файл
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from urllib import request, error

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
    with request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def collect(hours: int) -> dict:
    """Собирает что произошло за последние N часов."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    cutoff_iso = cutoff.isoformat()

    # Все недавние события
    events = _get(f"/dashboard/recent-events?limit=500").get("items", [])
    recent_events = [e for e in events if e.get("timestamp", "") >= cutoff_iso]

    # Аудит за период
    audit = _get(f"/dashboard/audit-tail?limit=500").get("items", [])
    recent_audit = [a for a in audit if a.get("time", "") >= cutoff_iso]

    # Все знания
    knowledge = _get(f"/dashboard/knowledge?limit=500").get("items", [])
    recent_knowledge = [k for k in knowledge if k.get("effective_from", "") >= cutoff_iso]

    # Domain breakdown
    domains_data = _get("/dashboard/domains").get("items", [])

    return {
        "since": cutoff_iso,
        "until": datetime.now(timezone.utc).isoformat(),
        "events": recent_events,
        "audit": recent_audit,
        "new_knowledge": recent_knowledge,
        "domains": domains_data,
    }


def render_md(data: dict, hours: int) -> str:
    out = []
    out.append(f"# Cognitive Core — отчёт за {hours} часов\n")
    out.append(f"_Период: {data['since']} → {data['until']}_\n")

    # === Сводка ===
    out.append("## Сводка\n")
    out.append(f"- Новых L1 событий: **{len(data['events'])}**")
    out.append(f"- Новых L3 знаний: **{len(data['new_knowledge'])}**")
    consolidations = [a for a in data['audit'] if a.get("action") in ("daily_consolidate", "weekly_consolidate", "monthly_audit")]
    out.append(f"- Консолидаций (daily/weekly/monthly): **{len(consolidations)}**")
    queries = [a for a in data['audit'] if a.get("action") == "operative_query"]
    out.append(f"- Operative-запросов: **{len(queries)}**")
    failures = [a for a in data['audit'] if not a.get("success")]
    out.append(f"- Ошибок в L5 audit: **{len(failures)}**\n")

    # === По доменам ===
    by_domain = {}
    for e in data["events"]:
        d = e.get("domain", "?")
        by_domain.setdefault(d, []).append(e)
    if by_domain:
        out.append("## События по доменам\n")
        out.append("| Домен | Событий | Примеры задач |")
        out.append("|---|---|---|")
        for d, evs in sorted(by_domain.items(), key=lambda x: -len(x[1])):
            tasks = []
            for e in evs[:3]:
                p = e.get("payload") or {}
                t = p.get("task") if isinstance(p, dict) else None
                if t:
                    tasks.append(f"_{t[:60]}_")
            out.append(f"| `{d}` | {len(evs)} | {' · '.join(tasks) or '—'} |")
        out.append("")

    # === Новые знания ===
    if data["new_knowledge"]:
        out.append("## Новые L3 знания\n")
        for k in data["new_knowledge"][:20]:
            kd = k.get("type", "?")
            dom = k.get("domain", "?")
            content = k.get("content", {})
            if isinstance(content, dict):
                desc = content.get("description") or content.get("content") or json.dumps(content, ensure_ascii=False)[:200]
            else:
                desc = str(content)[:200]
            out.append(f"- **[{kd}]** `{dom}` — {desc}")
        if len(data["new_knowledge"]) > 20:
            out.append(f"\n_... и ещё {len(data['new_knowledge']) - 20}_")
        out.append("")

    # === Ошибки ===
    if failures:
        out.append("## Ошибки\n")
        for f in failures[:15]:
            details = f.get("details") or {}
            err = details.get("error", "") if isinstance(details, dict) else str(details)
            out.append(f"- **{f.get('action')}** by `{f.get('agent', '?')}` — {str(err)[:200]}")
        out.append("")

    # === Состояние памяти ===
    out.append("## Состояние памяти (всё время)\n")
    out.append("| Домен | L1 | L2 | L3 знаний | Tools |")
    out.append("|---|---|---|---|---|")
    for d in data["domains"][:10]:
        out.append(f"| `{d['domain']}` | {d['l1']} | {d['l2']} | {d['l3_active']} | {d['tools_active']} |")

    out.append("\n---")
    out.append(f"_Сгенерировано: {datetime.now(timezone.utc).isoformat()}_")
    return "\n".join(out)


def render_text(data: dict, hours: int) -> str:
    """Краткая текстовая версия для терминала."""
    lines = [
        "",
        "=" * 60,
        f"  Cognitive Core — Daily Report (last {hours}h)",
        "=" * 60,
        f"  Период: {data['since'][:19]} → {data['until'][:19]}",
        "",
        f"  Новых L1 событий:     {len(data['events'])}",
        f"  Новых L3 знаний:      {len(data['new_knowledge'])}",
        f"  Консолидаций:         {sum(1 for a in data['audit'] if 'consolidate' in (a.get('action') or '') or a.get('action') == 'monthly_audit')}",
        f"  Operative запросов:   {sum(1 for a in data['audit'] if a.get('action') == 'operative_query')}",
        f"  Ошибок:               {sum(1 for a in data['audit'] if not a.get('success'))}",
    ]

    by_domain = {}
    for e in data["events"]:
        by_domain.setdefault(e.get("domain", "?"), 0)
        by_domain[e.get("domain", "?")] += 1
    if by_domain:
        lines.append("")
        lines.append("  По доменам (новые L1):")
        for d, n in sorted(by_domain.items(), key=lambda x: -x[1])[:8]:
            lines.append(f"    {d:20s}  {n}")

    if data["new_knowledge"]:
        lines.append("")
        lines.append("  Новые L3 (превью):")
        for k in data["new_knowledge"][:5]:
            content = k.get("content", {})
            desc = (content.get("description") if isinstance(content, dict) else str(content))[:80]
            lines.append(f"    [{k.get('type', '?'):8s}] {k.get('domain', '?'):15s} {desc}")

    lines.append("=" * 60)
    lines.append("")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=1)
    p.add_argument("--md", action="store_true", help="Output as markdown")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    hours = args.days * 24
    try:
        data = collect(hours)
    except (error.URLError, error.HTTPError) as e:
        print(f"ERROR: Cognitive Core не отвечает: {e}", file=sys.stderr)
        sys.exit(2)

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    elif args.md:
        print(render_md(data, hours))
    else:
        print(render_text(data, hours))


if __name__ == "__main__":
    main()
