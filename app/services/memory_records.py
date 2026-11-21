"""Owner-scoped source records and task heads; no LLM guesses about task status."""
from __future__ import annotations

import json
import re

# Question framing is not a useful fallback search key ("что", "где", "how").
# Project names and object/action words are deliberately not on this list.
QUESTION_WORDS = frozenset("""
а и или но в во на по из к ко у с со за для от до о об про при
это этот эта эти того том как какой какая какие что где когда почему зачем
ли же бы был была были есть мне меня мы нам ты вы он она они его её их
наш наши мой мои владельца владелец работу работы работ над сейчас ещё уже
последнее актуальное актуальный состояние статус
остановились остановился осталось сделано продолжи продолжить расскажи
дальше следующий шаг шаги проверка проверке проверкой проверку
подтверждение подтверждения доказательство доказательства
the a an and or of in on at to for from with is are was were be been
what where when how please latest current status state work we our my
""".split())


def search_terms(query: str) -> list[str]:
    """Literal bounded terms, never interpolated into SQL or tsquery syntax."""
    words = re.findall(r"[^\W_]+(?:[-_][^\W_]+)*", (query or "").casefold(), re.UNICODE)
    return list(dict.fromkeys(w for w in words if len(w) > 1 and w not in QUESTION_WORDS))[:32]


# Explicit task IDs are durable across sessions/branches. For old CHECKPOINT
# records only, a branch provides a best-effort lineage (labelled as inferred).
# Do NOT group arbitrary notes by agent or domain: each has many active tasks.
SOURCE_CTE = r"""
WITH source AS (
    SELECT e.*,
      CASE
        WHEN raw_payload #>> '{checkpoint,schema_version}' = '1'
          THEN 'task:' || (raw_payload #>> '{checkpoint,task_id}')
        WHEN raw_payload->>'task' ~ '^CHECKPOINT [^ ]+ \[[^@]+@[^]]+\]'
          THEN 'legacy:' || substring(raw_payload->>'task' from '^CHECKPOINT [^ ]+ \[([^@]+)@')
        ELSE 'event:' || id::text
      END AS task_key,
      CASE WHEN raw_payload #>> '{checkpoint,schema_version}' = '1'
           THEN raw_payload #>> '{checkpoint,observed_at}'
           ELSE to_char(timestamp AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
      END AS observed_at
    FROM l1_raw_events e
    WHERE owner_user_id = $1::uuid AND ($2::text IS NULL OR domain = $2)
), heads AS (
    SELECT DISTINCT ON (domain, task_key) * FROM source
    ORDER BY domain, task_key, observed_at DESC, timestamp DESC, id DESC
)
"""


def record_dict(row, *, preview: bool = True) -> dict:
    payload = row["raw_payload"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {"text": payload}
    if not isinstance(payload, dict):
        payload = {"text": str(payload)}
    checkpoint = payload.get("checkpoint")
    checkpoint = checkpoint if isinstance(checkpoint, dict) and checkpoint.get("schema_version") == 1 else None
    content = {k: str(payload[k]) for k in ("task", "result", "lessons") if payload.get(k)}
    if not content:
        content = {"text": json.dumps(payload, ensure_ascii=False)}
    truncated = preview and any(len(v) > 1200 for v in content.values())
    if preview:
        content = {k: v[:1200] for k, v in content.items()}
    ts = row["timestamp"]
    record = {
        "id": str(row["id"]), "record_type": "recent", "domain": row["domain"],
        "source_agent": row["source_agent"],
        "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
        "content": content, "truncated": bool(truncated),
        "read_tool": "cognitive_read_record",
    }
    key = row.get("task_key", "")
    if key.startswith(("task:", "legacy:")):
        record["task_state"] = {
            "task_key": key, "authority": "latest_reported",
            "identity": "explicit" if checkpoint else "inferred_legacy_branch",
            "observed_at": row.get("observed_at", record["timestamp"]),
            "status": checkpoint.get("status", "unknown") if checkpoint else "unknown",
        }
    if checkpoint:
        record["checkpoint"] = checkpoint
    if not preview:
        # Preserve every source field once, rather than repeating long bodies in
        # both content and payload. Checkpoint has its own typed envelope above.
        record["content"] = {k: v for k, v in payload.items() if k != "checkpoint" or not checkpoint}
    return record


async def search_records(pool, query: str, owner_user_id: str | None,
                         domain: str | None = None, days: int = 7, limit: int = 5,
                         workspace: str | None = None) -> list[dict]:
    if not owner_user_id:
        return []
    terms = search_terms(query)
    # Match history but return the head. An older "done" must not beat a newer
    # reopened checkpoint merely because it repeated the query words more often.
    # Keep each term count behind a materialization barrier: with stale table
    # statistics an inlined score filter can run again for every source row.
    candidates = r"""
    , searchable AS MATERIALIZED (
        SELECT e.id, to_tsvector('russian', e.raw_payload::text) AS ru,
               to_tsvector('simple', e.raw_payload::text) AS plain
        FROM l1_raw_events e
        WHERE owner_user_id = $1::uuid AND ($2::text IS NULL OR domain = $2)
          AND (to_tsvector('russian', e.raw_payload::text) @@ websearch_to_tsquery('russian', array_to_string($4::text[], ' OR '))
            OR to_tsvector('simple', e.raw_payload::text) @@ websearch_to_tsquery('simple', array_to_string($4::text[], ' OR ')))
          AND (e.timestamp >= NOW() - ($3::int * INTERVAL '1 day')
               OR e.raw_payload #>> '{checkpoint,schema_version}' = '1'
               OR e.raw_payload->>'task' ~ '^CHECKPOINT [^ ]+ \[[^@]+@[^]]+\]')
    ), scores AS MATERIALIZED (
        SELECT s.id, s.domain, s.task_key,
          (SELECT count(*) FROM unnest($4::text[]) AS t(term)
            WHERE c.ru @@ plainto_tsquery('russian', term)
               OR c.plain @@ plainto_tsquery('simple', term)) AS score
        FROM source s JOIN searchable c USING (id)
    )
    """ if terms else """
    , scores AS (
        SELECT s.id, s.domain, s.task_key, 0::bigint AS score FROM source s
        WHERE s.timestamp >= NOW() - ($3::int * INTERVAL '1 day')
           OR s.task_key NOT LIKE 'event:%'
    )
    """
    sql = SOURCE_CTE + candidates + r"""
    , matches AS (
        SELECT domain, task_key, max(score) AS score
        FROM scores WHERE score > 0 OR cardinality($4::text[]) = 0
        GROUP BY domain, task_key
    )
    SELECT h.*, m.score FROM heads h JOIN matches m USING (domain, task_key)
    ORDER BY m.score DESC,
             COALESCE(lower(rtrim(replace(h.raw_payload #>> '{checkpoint,workspace}', chr(92), '/'), '/')) = $6::text, false) DESC,
             (h.task_key NOT LIKE 'event:%') DESC,
             h.observed_at DESC, h.timestamp DESC, h.id DESC
    LIMIT $5
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, owner_user_id, domain,
                                max(1, min(int(days), 30)), terms,
                                max(1, min(int(limit), 20)),
                                workspace.replace('\\', '/').rstrip('/').lower() if workspace else None)
    return [record_dict(r) for r in rows]


async def read_record(pool, record_id: str, owner_user_id: str | None) -> dict | None:
    """Full L1 source, including a successor pointer; no cross-owner fallback."""
    if not owner_user_id:
        return None
    sql = SOURCE_CTE + """
    SELECT s.*, h.id AS latest_id
    FROM source s JOIN heads h USING (domain, task_key)
    WHERE s.id = $3::uuid
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, owner_user_id, None, record_id)
    if not row:
        return None
    result = record_dict(row, preview=False)
    result["is_latest"] = str(row["latest_id"]) == str(row["id"])
    result["latest_id"] = str(row["latest_id"])
    if not result["is_latest"]:
        result["superseded_by"] = str(row["latest_id"])
        if "task_state" in result:
            result["task_state"]["authority"] = "historical"
    return result
