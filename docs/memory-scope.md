# Memory scope in Cognitive Core

> 🇷🇺 Русская версия: [memory-scope.ru.md](memory-scope.ru.md)

**TL;DR**: memory (L1/L2/L3/L4) is shared between all agents of the same owner through an `owner_user_id` WHERE filter. Cross-owner isolation is guaranteed at the database level (Phase 4, PR #47–#53). If you need per-agent logical isolation, use a domain prefix as a convention.

## Who sees what

| Layer | Storage | Scope | Cross-agent, same owner? | Cross-owner? |
|---|---|---|---|---|
| L0 | Redis | per-agent (key prefix) | ❌ (separate keys) | ❌ |
| L1 events | Postgres `l1_raw_events` | per-owner (WHERE owner_user_id) | ✅ (visible to all) | ❌ (WHERE filter) |
| L2 daily | Postgres `l2_daily_summaries` | per-owner | ✅ | ❌ |
| L3 knowledge | Postgres `l3_knowledge_entries` (pgvector KNN) | per-owner | ✅ | ❌ |
| L4 snapshots | MinIO `s3://l4-snapshots/{owner_user_id}/...` | per-owner (path prefix) | ✅ | ❌ |
| State | Postgres `agent_states` | per-agent (`agent_id` PK) | ❌ (but visible via `cognitive_my_team`) | ❌ |

## Data flow

```
agent A → cognitive_remember(domain='msk-58', task=...)
  ↓
INSERT INTO l1_raw_events (agent_id='A', owner_user_id='owner-uuid',
                            domain='msk-58', payload={...})
  ↓ daily consolidation cron (cogcore-nightly.timer)
INSERT INTO l2_daily_summaries (owner_user_id='owner-uuid',
                                  date='2026-05-27',
                                  domain='msk-58', summary=...)
  ↓ weekly + L3 curator (DeepSeek)
INSERT INTO l3_knowledge_entries (owner_user_id='owner-uuid',
                                    domain='msk-58',
                                    content=..., embedding=vector)

agent B (same owner): cognitive_recall(query='project msk-58')
  ↓
SELECT FROM l3_knowledge_entries
  WHERE owner_user_id='owner-uuid'  ← both agent A and B = owner-uuid
  ORDER BY 1 - (embedding <=> :query_vec) LIMIT top_k
  ↓
Returns what agent A wrote. ✅
```

## If you need per-agent isolation (convention)

Cognitive Core itself does not partition memory between agents of the same owner; that is by design (a shared knowledge base). If you have a case where **agent B must not see** what agent A wrote, use a domain prefix:

**Convention**: `domain='agent:{agent_id}:project:{project_name}'`

```python
# Agent A
cognitive_remember(domain=f'agent:{me}:project:secret-research', ...)

# Agent B
cognitive_recall(query='research', domain='agent:B')  # will not return agent A's entries
```

This is a **convention, not enforcement**: agent B can still technically call `cognitive_recall(domain='agent:A:...')` and see the data. If you need hard isolation, that means a **separate owner_user_id** (create a second account).

## Cross-platform memory continuity

| Source | Where it lives | Synced with Cognitive Core? |
|---|---|---|
| Claude Code `~/.claude/projects/.../memory/*.md` | Locally on the agent's machine | ❌ Not synced automatically |
| Cursor MCP context | Local + cloud (Cursor) | ❌ |
| ChatGPT memory | OpenAI cloud (per account) | ❌ |
| **Cognitive Core (L1/L2/L3/L4)** | Server Postgres + MinIO | ✅ Centralized: every agent of the owner sees it |

**Pattern**: keep facts that must **persist across sessions and agents** in Cognitive Core via `cognitive_remember`. Local Claude Code memory is a fast file cache for the current machine, not for sharing.

## DM vs memory

| When | Tool | Storage |
|---|---|---|
| Record a fact for your future self or peers | `cognitive_remember` | L1 → L3 (Postgres, KNN-searchable) |
| Direct message to a specific agent | `cognitive_send(to='agent_id', text=...)` | `agent_messages` table (queue) |
| Read the DM queue | `cognitive_inbox(since_minutes=60)` | same table |
| Which of my agents are online | `cognitive_my_team()` | `agent_states` + Redis presence |

**Best practice**: DMs are ephemeral commands and coordination, not long-term storage. If you want an inbox item preserved as knowledge, call `cognitive_remember` after reading it.

## Sanity check of production scope

```bash
# How many L3 entries my owner has
curl https://mcp.me-ai.ru/agents/{my_id}/state \
  -H "X-API-Key: $KEY" | jq '.layers.l3'

# Cross-owner check (must return 0)
# Impossible to do through MCP: owner_user_id comes from the api_key, not from an argument
```

## Pitfalls

1. **The `domain` field is not sanitized**, but prefixed conventions (`agent:X:`) can be broken by accident if someone uses `domain='agent:X'` (without the trailing colon): `cognitive_recall(domain='agent:X')` will match everything with that prefix. Always use the full prefix.

2. **L3 KNN searches across all domains when no domain is given**: `cognitive_recall(query='...')` without `domain=` looks at the owner's ENTIRE knowledge base. Depending on the scenario this is a feature (cross-pollination between projects) or a bug (information leak between roles).

3. **Backups**: `cogcore-backup-tier.sh` runs daily at 03:30 and copies L4 snapshots from hot NVMe to cold HDD (7d → 90d TTL). If L3 is lost, restoring from an L4 snapshot is not trivial (no automation; a manual `INSERT FROM` the snapshot file is needed). Backlog: write `scripts/restore-from-l4.sh`.

## References

- Phase 4 multi-tenancy: PR #47–#53 in this repo (introduced `owner_user_id` columns + WHERE filters)
- L1–L3 design: `app/services/consolidator.py`
- L4 snapshot format: `app/services/l4_snapshot.py`
