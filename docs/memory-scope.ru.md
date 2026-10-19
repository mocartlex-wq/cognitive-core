# Memory Scope в Cognitive Core

**TL;DR**: память (L1/L2/L3/L4) shared между всеми агентами одного owner-а через `owner_user_id` WHERE-фильтр. Cross-owner isolation гарантирована БД-уровнем (Phase 4, PR #47-#53). Если нужна per-agent логическая изоляция — используйте domain prefix как convention.

## Кто что видит

| Слой | Storage | Scope | Cross-agent same owner? | Cross-owner? |
|---|---|---|---|---|
| L0 | Redis | per-agent (key prefix) | ❌ (отдельные ключи) | ❌ |
| L1 events | Postgres `l1_raw_events` | per-owner (WHERE owner_user_id) | ✅ (видны всем) | ❌ (WHERE filter) |
| L2 daily | Postgres `l2_daily_summaries` | per-owner | ✅ | ❌ |
| L3 knowledge | Postgres `l3_knowledge_entries` (pgvector KNN) | per-owner | ✅ | ❌ |
| L4 snapshots | MinIO `s3://l4-snapshots/{owner_user_id}/...` | per-owner (path prefix) | ✅ | ❌ |
| State | Postgres `agent_states` | per-agent (`agent_id` PK) | ❌ (но видно через `cognitive_my_team`) | ❌ |

## Поток данных

```
агент A → cognitive_remember(domain='msk-58', task=...)
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

агент B (same owner): cognitive_recall(query='проект msk-58')
  ↓
SELECT FROM l3_knowledge_entries
  WHERE owner_user_id='owner-uuid'  ← оба agent A и B = owner-uuid
  ORDER BY 1 - (embedding <=> :query_vec) LIMIT top_k
  ↓
Возвращает что записал агент A. ✅
```

## Если нужна per-agent изоляция (convention)

Сам Cognitive Core не разделяет память между агентами одного owner-а — это by-design (общая база знаний). Если у вас case где **агент B не должен видеть** что записал агент A, используйте domain prefix:

**Convention**: `domain='agent:{agent_id}:project:{project_name}'`

```python
# Агент A
cognitive_remember(domain=f'agent:{me}:project:secret-research', ...)

# Агент B  
cognitive_recall(query='research', domain='agent:B')  # не вернёт записи агента A
```

Это **convention, не enforcement** — агент B всё ещё технически может вызвать `cognitive_recall(domain='agent:A:...')` и увидеть. Если нужна жёсткая изоляция — это **отдельный owner_user_id** (создать второй account).

## Cross-platform memory continuity

| Источник | Где живёт | Sync с Cognitive Core? |
|---|---|---|
| Claude Code `~/.claude/projects/.../memory/*.md` | Локально на машине агента | ❌ Не синхронизируется автоматически |
| Cursor MCP context | Локально + cloud (Cursor) | ❌ |
| ChatGPT memory | OpenAI cloud (per-account) | ❌ |
| **Cognitive Core (L1/L2/L3/L4)** | Server Postgres + MinIO | ✅ Centralized — все агенты owner-а видят |

**Pattern**: храните **persistent across sessions / cross-agent** факты в Cognitive Core через `cognitive_remember`. Локальная Claude Code memory = быстрый file-cache для текущей машины, не для шаринга.

## DM vs Memory

| Когда | Tool | Storage |
|---|---|---|
| Записать факт для будущего себя или peers | `cognitive_remember` | L1 → L3 (postgres, KNN searchable) |
| Прямое сообщение конкретному агенту | `cognitive_send(to='agent_id', text=...)` | `agent_messages` table (queue) |
| Прочитать DM очередь | `cognitive_inbox(since_minutes=60)` | same table |
| Кто из my agents online | `cognitive_my_team()` | `agent_states` + Redis presence |

**Best practice**: DM = эфемерные команды/координация, не для долгого хранения. Если хочешь чтоб inbox сохранился в knowledge — после прочтения вызови `cognitive_remember`.

## Sanity check production scope

```bash
# Сколько L3 entries у моего owner-а
curl https://mcp.me-ai.ru/agents/{my_id}/state \
  -H "X-API-Key: $KEY" | jq '.layers.l3'

# Cross-owner проверка (должна возвращать 0)
# Невозможно сделать через MCP — owner_user_id берётся из api_key, не из аргумента
```

## Pitfalls

1. **`domain` поле не санитизируется** — но за-prefix-нутые конвенции (`agent:X:`) можно случайно сломать если кто-то делает `domain='agent:X'` (без post-colon) — `cognitive_recall(domain='agent:X')` найдёт всё с этим префиксом. Используйте полный prefix всегда.

2. **L3 KNN ищет across all domains если domain не передан** — `cognitive_recall(query='...')` без `domain=` смотрит ВЮ knowledge base owner-а. Это feature (cross-pollination между проектами) или bug (information leak между ролями) в зависимости от сценария.

3. **Backups** — `cogcore-backup-tier.sh` daily 03:30 копирует L4 snapshots с hot NVMe на cold HDD (7d → 90d TTL). Если уронили L3 — восстановление из L4 snapshot не trivial (нет автоматики, нужно manual `INSERT FROM` snapshot file). Backlog: написать `scripts/restore-from-l4.sh`.

## References

- Phase 4 multi-tenancy: PR #47-#53 в этом репо (введены `owner_user_id` columns + WHERE filters)
- L1-L3 design: `app/services/consolidator.py`
- L4 snapshot format: `app/services/l4_snapshot.py`
