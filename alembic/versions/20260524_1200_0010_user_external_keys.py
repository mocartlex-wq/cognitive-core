"""user_external_keys: per-tenant API keys для external AI providers

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-24

Owner-mandate (2026-05-24): vision-pipeline сейчас тратит shared platform ключ
(QWEN_API_KEY). Owner просит per-tenant opt-in — каждый платит сам со своего
api_key (особенно для дорогих как MiniMax / Claude Opus vision).

Дизайн:
  - PK = (owner_user_id, provider) — у каждого user'а max 1 ключ per provider
  - api_key_encrypted BYTEA — Fernet-encrypted; plaintext не хранится никогда
  - base_url / model_name — optional override (если tenant хочет EU endpoint
    или конкретную модель отличную от default)
  - last_test_status / last_test_at — UI чип «connected/failed» без plaintext
  - ON DELETE CASCADE на account — soft-delete account удаляет и ключи

Provider whitelist enforced на API-layer:
  qwen, minimax, gigachat, claude, openai, gemini

Идемпотентно — IF NOT EXISTS на каждый объект.
"""
from alembic import op


revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS user_external_keys (
            owner_user_id UUID NOT NULL REFERENCES accounts(user_id) ON DELETE CASCADE,
            provider VARCHAR(32) NOT NULL,
            api_key_encrypted BYTEA NOT NULL,
            base_url TEXT,
            model_name TEXT,
            last_used_at TIMESTAMPTZ,
            last_test_status VARCHAR(32),
            last_test_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (owner_user_id, provider)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_user_external_keys_owner
        ON user_external_keys(owner_user_id)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_user_external_keys_owner")
    op.execute("DROP TABLE IF EXISTS user_external_keys")
