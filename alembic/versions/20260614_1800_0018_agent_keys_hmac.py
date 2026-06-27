"""agent_keys: добавить колонку api_key_hmac + индекс (lookup-hash для plaintext)

Phase 1 из плана hashed API-key lookup (см. app/security/key_hash.py):
  1) [эта миграция] добавить колонку и индекс — schema-only, поведение не меняется.
  2) Owner устанавливает COGCORE_KEY_LOOKUP_SECRET в /opt/cognitive-core/.env.
  3) Backfill: scripts/backfill_agent_key_hmac.py — populates new column for all
     existing rows. Idempotent, безопасно прогонять повторно.
  4) verify_api_key уже умеет dual-path (api_key_hmac OR api_key) — лукапы
     продолжают работать на любой смеси старых/новых строк.
  5) После уверенности (отдельная миграция, не сейчас): DROP COLUMN api_key,
     переименовать api_key_hmac → api_key_lookup.

Эта миграция НЕ принудительно-NOT NULL — старые plaintext-only строки
остаются валидными до backfill. Индекс — partial, по непустым значениям,
чтобы не индексировать NULL.

Revision ID: 0018
Revises: 0017
"""
from alembic import op


revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # agent_keys создаётся в init_db() (app/db/postgres.py), но на больших
    # инсталляциях миграция может приходить раньше чем поднимется API — поэтому
    # обёрнуто в to_regclass на случай пустой схемы. ADD COLUMN IF NOT EXISTS
    # делает миграцию идемпотентной (повторный прогон — no-op).
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('public.agent_keys') IS NOT NULL THEN
                ALTER TABLE public.agent_keys
                    ADD COLUMN IF NOT EXISTS api_key_hmac VARCHAR(64);
                CREATE INDEX IF NOT EXISTS idx_agent_keys_hmac
                    ON public.agent_keys(api_key_hmac)
                    WHERE api_key_hmac IS NOT NULL AND revoked_at IS NULL;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('public.agent_keys') IS NOT NULL THEN
                DROP INDEX IF EXISTS idx_agent_keys_hmac;
                ALTER TABLE public.agent_keys DROP COLUMN IF EXISTS api_key_hmac;
            END IF;
        END $$;
        """
    )
