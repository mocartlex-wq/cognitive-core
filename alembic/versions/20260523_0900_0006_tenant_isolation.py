"""tenant isolation: owner_user_id on l1/l2/l3 + l4_snapshots + backfill + indexes

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-23

Critical security: до этой миграции memory-таблицы (l1_raw_events, l2_daily_buffers,
l3_master_knowledge, l3_tools_registry, l4_snapshots) НЕ имели owner_user_id и
любой агент мог через cognitive_recall(domain="sales") прочитать данные ДРУГИХ
владельцев из того же domain.

В single-owner режиме (только агенты владельца) это OK. Для аренды (rental,
multi-tenant) — критическая cross-tenant утечка.

Эта миграция:
  1. ADD COLUMN owner_user_id UUID (nullable пока) на 5 memory-таблицах
  2. Backfill из agent_states через source_agent (l1) и через l2.source_event_ids → l1 (l2/l3)
  3. Composite indexes (owner_user_id, domain[, timestamp]) для быстрых WHERE
  4. NOT NULL остаётся ОТЛОЖЕННЫМ — отдельная миграция 0008 после того
     как все WHERE-фильтры в коде задеплоятся и backfill точно полный

Idempotent: IF NOT EXISTS на колонках и индексах, безопасно повторить.

Downgrade: только DROP COLUMN — данные потеряются (FK не было).
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ──────────────────────────────────────────────────────────────────
    # 1. ADD COLUMN owner_user_id UUID (nullable)
    # ──────────────────────────────────────────────────────────────────
    op.execute("ALTER TABLE l1_raw_events       ADD COLUMN IF NOT EXISTS owner_user_id UUID")
    op.execute("ALTER TABLE l2_daily_buffers    ADD COLUMN IF NOT EXISTS owner_user_id UUID")
    op.execute("ALTER TABLE l3_master_knowledge ADD COLUMN IF NOT EXISTS owner_user_id UUID")
    op.execute("ALTER TABLE l3_tools_registry   ADD COLUMN IF NOT EXISTS owner_user_id UUID")
    op.execute("ALTER TABLE l4_snapshots        ADD COLUMN IF NOT EXISTS owner_user_id UUID")

    # ──────────────────────────────────────────────────────────────────
    # 2. Backfill: l1 — через source_agent → agent_states.owner_user_id
    # ──────────────────────────────────────────────────────────────────
    op.execute("""
        UPDATE l1_raw_events e
           SET owner_user_id = s.owner_user_id
          FROM agent_states s
         WHERE s.agent_id = e.source_agent
           AND s.owner_user_id IS NOT NULL
           AND e.owner_user_id IS NULL
    """)

    # l2 — через source_event_ids (array of UUIDs из l1)
    # Берём owner_user_id первого попавшегося связанного l1-события.
    op.execute("""
        UPDATE l2_daily_buffers b
           SET owner_user_id = sub.owner_user_id
          FROM (
              SELECT b2.id AS buf_id, e.owner_user_id
                FROM l2_daily_buffers b2
                LEFT JOIN LATERAL (
                    SELECT owner_user_id
                      FROM l1_raw_events
                     WHERE id = ANY(b2.source_event_ids)
                       AND owner_user_id IS NOT NULL
                     LIMIT 1
                ) e ON TRUE
               WHERE b2.owner_user_id IS NULL
          ) sub
         WHERE b.id = sub.buf_id
           AND sub.owner_user_id IS NOT NULL
    """)

    # l3_master_knowledge — нет прямой связи с l1/agent, но domain+content имеет
    # source-tracking в `content->'source_l2_ids'` (если consolidator писал).
    # Backfill через l2 → owner для тех записей где l2 уже имеет owner.
    op.execute("""
        UPDATE l3_master_knowledge k
           SET owner_user_id = sub.owner_user_id
          FROM (
              SELECT DISTINCT ON (k2.id) k2.id AS kid, b.owner_user_id
                FROM l3_master_knowledge k2
                JOIN l2_daily_buffers b ON b.domain = k2.domain
                                       AND b.owner_user_id IS NOT NULL
               WHERE k2.owner_user_id IS NULL
               ORDER BY k2.id, b.created_at DESC
          ) sub
         WHERE k.id = sub.kid
           AND sub.owner_user_id IS NOT NULL
    """)

    # l3_tools_registry — аналогично через l2.domain
    op.execute("""
        UPDATE l3_tools_registry t
           SET owner_user_id = sub.owner_user_id
          FROM (
              SELECT DISTINCT ON (t2.id) t2.id AS tid, b.owner_user_id
                FROM l3_tools_registry t2
                JOIN l2_daily_buffers b ON b.domain = t2.domain
                                       AND b.owner_user_id IS NOT NULL
               WHERE t2.owner_user_id IS NULL
               ORDER BY t2.id, b.created_at DESC
          ) sub
         WHERE t.id = sub.tid
           AND sub.owner_user_id IS NOT NULL
    """)

    # l4_snapshots — нет прямой связи, оставляем NULL (агрегаты).
    # При создании новых снапшотов код будет писать owner_user_id явно.

    # ──────────────────────────────────────────────────────────────────
    # 3. Composite indexes — fast WHERE owner_user_id = $1 AND domain = $2
    # ──────────────────────────────────────────────────────────────────
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_l1_owner_domain_ts
        ON l1_raw_events (owner_user_id, domain, timestamp DESC)
        WHERE owner_user_id IS NOT NULL
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_l2_owner_domain
        ON l2_daily_buffers (owner_user_id, domain)
        WHERE owner_user_id IS NOT NULL
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_l3_know_owner_domain
        ON l3_master_knowledge (owner_user_id, domain)
        WHERE owner_user_id IS NOT NULL
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_l3_tools_owner_domain
        ON l3_tools_registry (owner_user_id, domain)
        WHERE owner_user_id IS NOT NULL
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_l4_owner
        ON l4_snapshots (owner_user_id)
        WHERE owner_user_id IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_l4_owner")
    op.execute("DROP INDEX IF EXISTS idx_l3_tools_owner_domain")
    op.execute("DROP INDEX IF EXISTS idx_l3_know_owner_domain")
    op.execute("DROP INDEX IF EXISTS idx_l2_owner_domain")
    op.execute("DROP INDEX IF EXISTS idx_l1_owner_domain_ts")
    op.execute("ALTER TABLE l4_snapshots        DROP COLUMN IF EXISTS owner_user_id")
    op.execute("ALTER TABLE l3_tools_registry   DROP COLUMN IF EXISTS owner_user_id")
    op.execute("ALTER TABLE l3_master_knowledge DROP COLUMN IF EXISTS owner_user_id")
    op.execute("ALTER TABLE l2_daily_buffers    DROP COLUMN IF EXISTS owner_user_id")
    op.execute("ALTER TABLE l1_raw_events       DROP COLUMN IF EXISTS owner_user_id")
