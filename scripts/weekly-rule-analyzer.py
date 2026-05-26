#!/usr/bin/env python3
"""Weekly Rule Analyzer — self-improvement loop для Cognitive Core (Phase 6).

Запускается cogcore-rule-analyzer.timer (Sun 04:00 UTC). Что делает:
1. Собирает rule_proposals со status='pending' и votes_up >= vote_threshold
2. Для каждого зовёт DeepSeek с запросом:
   - Оценить значимость предложения (1-10)
   - Предложить severity ('core' для критичных, 'recommended' для полезных)
   - Найти duplicate среди существующих agent_rules
3. Пишет ds_analysis + ds_suggested_severity + ds_duplicate_of в rule_proposals
4. Status переводит pending → reviewing
5. Логирует общий отчёт в /var/log/cogcore/rule-proposals-report.log
6. Пишет L1 event domain='rule_review_report' с stats

Admin потом видит в /ui/admin/rule-proposals — Approve/Reject.

Конфиг через env:
  DATABASE_URL=postgresql://cognitive:...@localhost:5432/cognitive_core
  DEEPSEEK_API_KEY=sk-...
  DEEPSEEK_BASE_URL=https://api.deepseek.com/v1 (default)
  DEEPSEEK_MODEL=deepseek-chat (default)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

import asyncpg
import httpx

LOG_FILE = "/var/log/cogcore/rule-proposals-report.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE),
    ],
)
log = logging.getLogger("rule-analyzer")


ANALYZE_PROMPT = """Ты — куратор Operating Rules для AI-агентов на платформе Cognitive Core.

Тебе предлагают новое правило от tenant'a. Оцени его:

1. Значимость (1-10): насколько это правило критично для качества и безопасности работы агентов?
2. Severity: какой уровень предлагаешь?
   - "core" = critical, обязательно для ВСЕХ агентов всех tenants (например: проверка памяти, план перед deploy)
   - "recommended" = полезно, но не критично (например: использовать русский в комментариях)
   - "reject" = слабое предложение, дубль или спорное
3. Если есть подобное правило среди существующих — укажи rule_id duplicate.

Существующие правила платформы:
{existing_rules}

Новое предложение от tenant'a:
- scope: {scope}
- body: {body}
- rationale: {rationale}
- votes: ↑{votes_up} / ↓{votes_down}

Отвечай СТРОГО валидным JSON без markdown:
{{
  "significance": 1-10,
  "suggested_severity": "core" | "recommended" | "reject",
  "duplicate_of_rule_id": null | "rule-xxx",
  "analysis": "краткое обоснование в 2-3 предложениях"
}}
"""


async def fetch_existing_rules(pool: asyncpg.Pool) -> list[dict]:
    """Все active platform rules для контекста для DeepSeek."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT rule_id, severity, scope, body
            FROM agent_rules
            WHERE owner_user_id IS NULL AND active = TRUE
            ORDER BY severity, position
            """
        )
    return [dict(r) for r in rows]


async def fetch_pending_proposals(pool: asyncpg.Pool) -> list[dict]:
    """Pending proposals с votes_up >= vote_threshold."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id::text AS id, owner_user_id::text AS owner_user_id,
                   proposed_body, proposed_scope, rationale,
                   votes_up, votes_down, vote_threshold
            FROM rule_proposals
            WHERE status = 'pending'
              AND votes_up >= COALESCE(vote_threshold, 3)
            ORDER BY votes_up DESC, created_at
            """
        )
    return [dict(r) for r in rows]


async def analyze_proposal(
    http: httpx.AsyncClient,
    proposal: dict,
    existing_rules: list[dict],
    *,
    api_key: str,
    base_url: str,
    model: str,
) -> dict:
    """Зовёт DeepSeek для анализа одного proposal. Возвращает dict с keys:
    significance, suggested_severity, duplicate_of_rule_id, analysis.
    """
    rules_listing = "\n".join(
        f"  [{r['severity']}] {r['rule_id']} (scope={r['scope']}): {r['body'][:120]}"
        for r in existing_rules
    ) or "  (нет правил)"

    user_prompt = ANALYZE_PROMPT.format(
        existing_rules=rules_listing,
        scope=proposal["proposed_scope"],
        body=proposal["proposed_body"],
        rationale=proposal.get("rationale") or "(не указано)",
        votes_up=proposal["votes_up"],
        votes_down=proposal["votes_down"],
    )
    try:
        r = await http.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": "Ты валидный JSON-respondent. Никакого markdown."},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.2,
                "max_tokens": 500,
            },
            timeout=30,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"].strip()
        # strip code fence if any
        if content.startswith("```"):
            content = content.split("```", 2)[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip().rstrip("`").strip()
        return json.loads(content)
    except Exception as e:
        log.warning("DeepSeek call failed for proposal %s: %s", proposal["id"], e)
        return {
            "significance": 0,
            "suggested_severity": "reject",
            "duplicate_of_rule_id": None,
            "analysis": f"DeepSeek error: {e}",
        }


async def update_proposal(pool: asyncpg.Pool, proposal_id: str, analysis: dict) -> None:
    """Записать DeepSeek analysis + перевести status в reviewing."""
    async with pool.acquire() as conn:
        # duplicate_of_rule_id может быть rule_id (text), не UUID — попробуем найти UUID
        duplicate_uuid = None
        dup_id = analysis.get("duplicate_of_rule_id")
        if dup_id and dup_id != "null":
            row = await conn.fetchrow(
                "SELECT id::text AS id FROM agent_rules WHERE rule_id = $1 LIMIT 1",
                dup_id,
            )
            if row:
                duplicate_uuid = row["id"]
        await conn.execute(
            """
            UPDATE rule_proposals
            SET status = 'reviewing',
                ds_analysis = $1,
                ds_suggested_severity = $2,
                ds_duplicate_of = $3::uuid
            WHERE id = $4::uuid
            """,
            (analysis.get("analysis") or "")[:2000],
            analysis.get("suggested_severity"),
            duplicate_uuid,
            proposal_id,
        )


async def write_l1_event(pool: asyncpg.Pool, payload: dict) -> None:
    """Лог в L1 для analytics dashboard."""
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO l1_raw_events (source_agent, domain, raw_payload, timestamp)
                VALUES ($1, $2, $3::jsonb, NOW())
                """,
                "rule-analyzer",
                "rule_review_report",
                json.dumps(payload),
            )
    except Exception as e:
        log.warning("L1 event write failed: %s", e)


async def main() -> int:
    log.info("=" * 60)
    log.info("Weekly rule analyzer starting")

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        # Fallback: try cognitive standard
        db_url = "postgresql://cognitive:cognitive@localhost:5432/cognitive_core"
        # Read POSTGRES_PASSWORD from env if available
        pwd = os.getenv("POSTGRES_PASSWORD")
        if pwd:
            db_url = f"postgresql://cognitive:{pwd}@localhost:5432/cognitive_core"

    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        log.error("DEEPSEEK_API_KEY not set — cannot analyze. Exit.")
        return 2
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    try:
        existing = await fetch_existing_rules(pool)
        pending = await fetch_pending_proposals(pool)
        log.info("Pending proposals (votes ≥ threshold): %d", len(pending))
        log.info("Existing platform rules: %d", len(existing))

        if not pending:
            log.info("Nothing to analyze. Done.")
            await write_l1_event(pool, {
                "analyzed": 0,
                "pending_count": 0,
                "ts": datetime.now(timezone.utc).isoformat(),
            })
            return 0

        analyzed_summary = []
        async with httpx.AsyncClient() as http:
            for p in pending:
                log.info("Analyzing proposal %s (votes ↑%d)", p["id"][:8], p["votes_up"])
                analysis = await analyze_proposal(
                    http, p, existing,
                    api_key=api_key, base_url=base_url, model=model,
                )
                await update_proposal(pool, p["id"], analysis)
                analyzed_summary.append({
                    "proposal_id": p["id"],
                    "votes_up": p["votes_up"],
                    "significance": analysis.get("significance"),
                    "suggested_severity": analysis.get("suggested_severity"),
                    "duplicate": analysis.get("duplicate_of_rule_id"),
                })
                log.info("  → suggested_severity=%s significance=%s",
                         analysis.get("suggested_severity"),
                         analysis.get("significance"))

        await write_l1_event(pool, {
            "analyzed": len(analyzed_summary),
            "pending_count": len(pending),
            "summary": analyzed_summary,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        log.info("Report written. %d proposals moved to status=reviewing.", len(analyzed_summary))
        log.info("Admin может теперь approve/reject на /ui/admin/rule-proposals")
        return 0
    finally:
        await pool.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
