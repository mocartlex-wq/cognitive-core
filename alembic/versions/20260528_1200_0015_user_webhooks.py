"""user_webhooks: per-tenant outbound webhook endpoints (M4 v1.0 roadmap)

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-28

Owner-mandate (M4): owner/tenants хотят получать уведомления о key-events
(agent.claimed, billing.payment, room.created, quota.exceeded, agent.offline)
в Slack / Discord / Telegram / generic endpoint. Tenant настраивает URL в
profile, платформа POST-ит туда JSON (app/services/webhooks.py).

Дизайн:
  - id uuid PK
  - owner_user_id → accounts(user_id) ON DELETE CASCADE (soft-delete account
    чистит и webhooks)
  - url TEXT — только https, anti-SSRF validate на API-layer
  - events TEXT[] — список подписанных event_type (whitelist в коде)
  - secret_encrypted BYTEA nullable — Fernet-encrypted HMAC secret; plaintext
    не хранится никогда (как user_external_keys.api_key_encrypted)
  - enabled BOOL default TRUE — пауза без удаления
  - last_triggered_at / last_status — для UI чипа «delivered/failed»

Идемпотентно — IF NOT EXISTS на каждый объект. Таблица пустая при создании,
ничего не ломает (notify_event graceful если строк нет).
"""
from alembic import op

# revision identifiers, used by Alembic
revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS user_webhooks (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_user_id     UUID NOT NULL REFERENCES accounts(user_id) ON DELETE CASCADE,
            url               TEXT NOT NULL,
            events            TEXT[] NOT NULL DEFAULT '{}',
            secret_encrypted  BYTEA,
            enabled           BOOLEAN NOT NULL DEFAULT TRUE,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_triggered_at TIMESTAMPTZ,
            last_status       TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_user_webhooks_owner
            ON user_webhooks(owner_user_id)
            WHERE enabled = TRUE;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS user_webhooks CASCADE")
