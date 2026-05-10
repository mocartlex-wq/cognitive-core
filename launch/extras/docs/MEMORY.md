# Memory model

Cognitive Core stores agent context across **5 layers**, each with its own retention,
shape, and consumer. Understanding which layer holds what makes recall predictable and
helps with GDPR / data-deletion requests.

## L1 — raw events

Append-only log of every message, tool call, decision, file ingestion. Schema:

```sql
l1_raw_events (
  id          UUID PK,
  source_agent TEXT,
  domain      TEXT,        -- agent_inbox | agent_decision | tool_call | file_ingest | …
  raw_payload JSONB,
  timestamp   TIMESTAMPTZ,
  archive     BOOLEAN DEFAULT false  -- true = consolidated into L2/L3, safe to prune
)
```

- **Dedup**: `BEFORE INSERT` trigger computes SHA-256 of
  `(payload, agent, domain, ts_minute_bucket)` and silently drops collisions.
- **Pruning**: cron `cogcore-l1-prune` runs daily at 03:00 UTC, deletes rows where
  `archive=true AND timestamp < NOW() - RETENTION_DAYS days` (default 14).
- **Recall**: `SELECT … WHERE source_agent = ? ORDER BY timestamp DESC LIMIT N`.

## L2 — daily buffers

DeepSeek summarizes each agent's L1 events into a **daily compact JSON**: action list,
decisions, learnings, open questions. Used as the next session's "what happened
yesterday" preamble.

```sql
l2_daily_buffers (
  agent_id TEXT, date DATE, summary JSONB, created_at TIMESTAMPTZ,
  PRIMARY KEY (agent_id, date)
)
```

Trigger: `cogcore-curator-daily` cron at 04:00 UTC.

## L3 — master knowledge

Stable facts, preferences, patterns extracted from L2 buffers when repeated ≥2 days with
confidence ≥ `MIN_CONFIDENCE_FOR_L3` (default 0.6). This is what `cognitive_recall`
returns by default.

Tables:
- `l3_master_knowledge (id, scope, key, value, confidence, last_used_at, …)`
- `l3_tools_registry (tool_name, definition_jsonb, usage_count, …)`
- `l3_entities (id, type, name, attributes JSONB)` — KG nodes
- `l3_knowledge_links (subject_id, predicate, object_id, confidence)` — KG edges
- `l3_domain_links (from_domain, to_domain, weight)` — cross-domain bridges

Cron: `cogcore-kg` Sunday 04:00 UTC re-runs DeepSeek extraction.

## L4 — snapshots

Periodic point-in-time **MinIO snapshots** of L3 + agent state, for time-travel debug
and recovery. Stored as `s3://l4-snapshots/<agent>/<date>/state.tar.gz`.

Cadence: full snapshot every `L4_FULL_SNAPSHOT_INTERVAL_WEEKS` (default 4) or when L3
delta exceeds `L4_MIN_CHANGE_PERCENT` (default 5).

## L5 — audit log

Tamper-evident JSONL audit of every privileged operation: lock acquisition, write-tool
execution, approval grants, force-release. Lives in `l5_audit_log` table + mirrored to
`/var/log/cognitive-audit.jsonl` (append-only, immutable mount in production).

## GDPR / data-deletion

Per-agent purge:

```sql
DELETE FROM l1_raw_events    WHERE source_agent = 'X';
DELETE FROM l2_daily_buffers WHERE agent_id    = 'X';
DELETE FROM l3_master_knowledge WHERE scope    = 'agent:X';
-- L4 snapshots: rclone delete s3://l4-snapshots/X/
-- L5 audit: keep (legal-hold), but redact agent_id → 'redacted-<sha256>'
INSERT INTO l5_audit_log (event_type, payload)
  VALUES ('gdpr_purge', jsonb_build_object('agent','X','at',NOW()));
```

Per-room purge:
```sql
DELETE FROM room_questions WHERE room_id = 'R';
DELETE FROM room_messages  WHERE room_id = 'R';
DELETE FROM room_participants WHERE room_id = 'R';
DELETE FROM rooms          WHERE id      = 'R';
```

## Tuning knobs (`.env`)

| Variable | Default | Effect |
|----------|---------|--------|
| `RETENTION_DAYS` | 14 | L1 prune horizon |
| `MIN_EVENTS_FOR_DAILY` | 3 | Skip L2 if < N events |
| `MIN_CONFIDENCE_FOR_L3` | 0.6 | Threshold for promoting L2 → L3 |
| `MIN_L2_REPETITIONS_FOR_L3` | 2 | Repetitions across days needed |
| `L3_STALENESS_DAYS` | 90 | Decay confidence after no use |
| `TOOL_UNUSED_DAYS` | 60 | Drop tool from L3 registry if unused |
| `L4_FULL_SNAPSHOT_INTERVAL_WEEKS` | 4 | Snapshot cadence |
| `L4_MIN_CHANGE_PERCENT` | 5 | Snapshot trigger on delta |
| `CURATOR_TEMPERATURE` | 0.1 | DeepSeek temp for consolidation |
