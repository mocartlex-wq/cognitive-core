"""Service layer для agent operating rules (Phase 6).

Используется orchestrator + agent_runtime для инъекции правил в system_prompt
агентов при работе для конкретного owner'a.

Pattern:
    rules = await fetch_rules_for_owner(owner_user_id)
    system_prompt = build_rules_section(rules) + base_persona_prompt
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from app.db.postgres import get_pool

SEVERITY_LABEL = {
    "core": "MUST follow (платформенное обязательное)",
    "recommended": "Should follow (рекомендация платформы)",
    "user": "User-specific preference",
}


async def fetch_rules_for_owner(owner_user_id: UUID | str | None) -> list[dict[str, Any]]:
    """Возвращает active rules для owner'a:
      - все платформенные (owner_user_id IS NULL, active=TRUE)
      - все user rules данного owner'a (active=TRUE)

    Order: core first, then by position, then created_at.

    Override логика (будущая): user-rule c override_of=<recommended_platform_id>
    подавляет соответствующий platform rule. Сейчас просто возвращаем все —
    инжектор сам отфильтрует дубли по rule_id.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id::text AS id,
                   owner_user_id::text AS owner_user_id,
                   rule_id, severity, scope, lang, position, body,
                   active, source, override_of::text AS override_of,
                   created_at
            FROM agent_rules
            WHERE active = TRUE
              AND (owner_user_id IS NULL OR owner_user_id = $1::uuid)
            ORDER BY
              (severity = 'core') DESC,
              position,
              created_at
            """,
            str(owner_user_id) if owner_user_id else None,
        )
    return [dict(r) for r in rows]


def build_rules_section(rules: list[dict[str, Any]]) -> str:
    """Форматирует список правил в Markdown-секцию для инъекции в system_prompt.

    Структура:
      # Operating Rules

      ## MUST follow (core)
      1. <body>
      ...

      ## Should follow (recommended)
      ...

      ## User-specific preferences
      ...
    """
    if not rules:
        return ""

    # Group by severity, dedupe by rule_id (user-override wins)
    by_rule_id: dict[str, dict[str, Any]] = {}
    for r in rules:
        rid = r["rule_id"]
        if rid not in by_rule_id or r.get("owner_user_id"):
            by_rule_id[rid] = r

    by_sev: dict[str, list[dict[str, Any]]] = {"core": [], "recommended": [], "user": []}
    for r in by_rule_id.values():
        by_sev.setdefault(r["severity"], []).append(r)

    out = ["# Operating Rules\n"]
    for sev in ("core", "recommended", "user"):
        items = by_sev.get(sev, [])
        if not items:
            continue
        out.append(f"\n## {SEVERITY_LABEL[sev]}\n")
        for i, r in enumerate(items, 1):
            out.append(f"{i}. **[{r['scope']}]** {r['body']}\n")
    return "".join(out)
