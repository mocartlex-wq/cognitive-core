"""Billing infrastructure tables.

billing_processed_events:
  Idempotency для webhook processing. Каждый event_id (Stripe или ЮKassa)
  сохраняется здесь — повторный webhook с тем же id не upgrade'ит tier
  второй раз (защита от Stripe retry storm + ЮKassa duplicate notifications).

subscriptions:
  Tracking активных подписок: provider, external_id, owner, tier, status,
  current_period_end. Используется для UI (показать «текущая подписка»),
  для cron-job (downgrade когда subscription expired), для audit.

Не блокирует — таблицы пустые при создании. Billing scaffold (PR feat/
billing-scaffold) ждёт что owner добавит Stripe + ЮKassa creds в .env,
потом первый платёж создаст первую запись.
"""
from alembic import op

# revision identifiers, used by Alembic
revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS billing_processed_events (
            event_id      TEXT PRIMARY KEY,
            provider      TEXT NOT NULL CHECK (provider IN ('stripe', 'yookassa')),
            processed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            event_type    TEXT,
            owner_user_id UUID REFERENCES accounts(user_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_billing_processed_owner
            ON billing_processed_events(owner_user_id, processed_at DESC)
            WHERE owner_user_id IS NOT NULL;
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_user_id        UUID NOT NULL REFERENCES accounts(user_id) ON DELETE CASCADE,
            provider             TEXT NOT NULL CHECK (provider IN ('stripe', 'yookassa')),
            external_subscription_id TEXT NOT NULL,
            tier                 TEXT NOT NULL CHECK (tier IN ('pro', 'enterprise')),
            status               TEXT NOT NULL DEFAULT 'active',
            current_period_start TIMESTAMPTZ,
            current_period_end   TIMESTAMPTZ,
            created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            metadata             JSONB DEFAULT '{}'::jsonb,
            UNIQUE (provider, external_subscription_id)
        );
        CREATE INDEX IF NOT EXISTS idx_subscriptions_owner_active
            ON subscriptions(owner_user_id)
            WHERE status = 'active';
        CREATE INDEX IF NOT EXISTS idx_subscriptions_period_end
            ON subscriptions(current_period_end)
            WHERE status = 'active';
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS subscriptions CASCADE")
    op.execute("DROP TABLE IF EXISTS billing_processed_events CASCADE")
