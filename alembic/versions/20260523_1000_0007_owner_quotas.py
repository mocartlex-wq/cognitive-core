"""owner_quotas: per-owner лимиты и счётчики для multi-tenant аренды

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-23

Per-owner quotas + tier (free/pro/enterprise). Counters обновляются:
  - events_today: insert trigger на l1_raw_events (увеличивает) + cron reset 00:00 UTC
  - storage_mb_now: cron каждый час пересчитывает через MinIO API
  - agents_count: триггер на agent_states INSERT/DELETE

Tier-defaults (free):
  - 10k events/day, 1 GB storage, 10 agents, 30 recall/min

Tier-defaults (pro): хардкодим в app/services/quota_enforcer.py — UPDATE тут не нужен.

Идемпотентно. Все аккаунты на момент применения получают free-tier строку
автоматически (INSERT ... ON CONFLICT DO NOTHING).
"""
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS owner_quotas (
            owner_user_id UUID PRIMARY KEY
                REFERENCES accounts(id) ON DELETE CASCADE,
            -- Лимиты
            max_events_per_day INT NOT NULL DEFAULT 10000,
            max_storage_mb     INT NOT NULL DEFAULT 1024,
            max_agents         INT NOT NULL DEFAULT 10,
            max_recall_per_min INT NOT NULL DEFAULT 30,
            -- Rolling счётчики (обновляются триггером + cron-job)
            events_today    INT   NOT NULL DEFAULT 0,
            storage_mb_now  FLOAT NOT NULL DEFAULT 0,
            agents_count    INT   NOT NULL DEFAULT 0,
            reset_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            -- Метаданные
            tier      VARCHAR(16) NOT NULL DEFAULT 'free'
                CHECK (tier IN ('free', 'pro', 'enterprise', 'admin')),
            suspended BOOLEAN NOT NULL DEFAULT FALSE,
            note      TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    # Индекс для admin-листинга по tier и подвешенным
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_owner_quotas_tier
        ON owner_quotas (tier, suspended)
    """)

    # Backfill: каждому существующему accounts.id создаём free-tier строку
    op.execute("""
        INSERT INTO owner_quotas (owner_user_id, tier)
        SELECT id, 'free' FROM accounts
        ON CONFLICT (owner_user_id) DO NOTHING
    """)

    # Auto-create quota record on accounts INSERT — чтобы новые регистрации
    # сразу получали лимиты, не зависеть от application-кода.
    op.execute("""
        CREATE OR REPLACE FUNCTION ensure_owner_quota()
        RETURNS TRIGGER AS $$
        BEGIN
            INSERT INTO owner_quotas (owner_user_id, tier)
            VALUES (NEW.id, 'free')
            ON CONFLICT (owner_user_id) DO NOTHING;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        DROP TRIGGER IF EXISTS trg_accounts_ensure_quota ON accounts
    """)
    op.execute("""
        CREATE TRIGGER trg_accounts_ensure_quota
        AFTER INSERT ON accounts
        FOR EACH ROW EXECUTE FUNCTION ensure_owner_quota()
    """)

    # Триггер на l1_raw_events INSERT — increments events_today.
    # Сбрасывается cron-задачей в 00:00 UTC через UPDATE ... SET events_today=0, reset_at=NOW().
    op.execute("""
        CREATE OR REPLACE FUNCTION increment_events_today()
        RETURNS TRIGGER AS $$
        BEGIN
            IF NEW.owner_user_id IS NOT NULL THEN
                UPDATE owner_quotas
                   SET events_today = events_today + 1
                 WHERE owner_user_id = NEW.owner_user_id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        DROP TRIGGER IF EXISTS trg_l1_increment_events ON l1_raw_events
    """)
    op.execute("""
        CREATE TRIGGER trg_l1_increment_events
        AFTER INSERT ON l1_raw_events
        FOR EACH ROW EXECUTE FUNCTION increment_events_today()
    """)

    # Триггер на agent_states — поддерживаем agents_count
    op.execute("""
        CREATE OR REPLACE FUNCTION sync_agents_count()
        RETURNS TRIGGER AS $$
        BEGIN
            IF TG_OP = 'INSERT' AND NEW.owner_user_id IS NOT NULL THEN
                UPDATE owner_quotas
                   SET agents_count = agents_count + 1
                 WHERE owner_user_id = NEW.owner_user_id;
            ELSIF TG_OP = 'DELETE' AND OLD.owner_user_id IS NOT NULL THEN
                UPDATE owner_quotas
                   SET agents_count = GREATEST(0, agents_count - 1)
                 WHERE owner_user_id = OLD.owner_user_id;
            END IF;
            RETURN COALESCE(NEW, OLD);
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        DROP TRIGGER IF EXISTS trg_agents_sync_count ON agent_states
    """)
    op.execute("""
        CREATE TRIGGER trg_agents_sync_count
        AFTER INSERT OR DELETE ON agent_states
        FOR EACH ROW EXECUTE FUNCTION sync_agents_count()
    """)

    # Backfill agents_count для существующих записей
    op.execute("""
        UPDATE owner_quotas q
           SET agents_count = sub.cnt
          FROM (
              SELECT owner_user_id, COUNT(*) AS cnt
                FROM agent_states
               WHERE owner_user_id IS NOT NULL
               GROUP BY owner_user_id
          ) sub
         WHERE q.owner_user_id = sub.owner_user_id
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_agents_sync_count ON agent_states")
    op.execute("DROP TRIGGER IF EXISTS trg_l1_increment_events ON l1_raw_events")
    op.execute("DROP TRIGGER IF EXISTS trg_accounts_ensure_quota ON accounts")
    op.execute("DROP FUNCTION IF EXISTS sync_agents_count()")
    op.execute("DROP FUNCTION IF EXISTS increment_events_today()")
    op.execute("DROP FUNCTION IF EXISTS ensure_owner_quota()")
    op.execute("DROP INDEX IF EXISTS idx_owner_quotas_tier")
    op.execute("DROP TABLE IF EXISTS owner_quotas")
