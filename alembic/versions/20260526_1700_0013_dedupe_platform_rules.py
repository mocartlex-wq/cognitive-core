"""Dedupe platform agent_rules + add partial unique index for NULL owner.

Phase 6 hotfix (2026-05-26): обнаружен баг в schema 0011 — `UNIQUE (owner_user_id, rule_id)`
не работает для NULL owner_user_id (PostgreSQL NULL != NULL). В результате migration 0012
создавала дубликаты платформенных правил при повторной установке.

Fix:
1. Удалить duplicate rows (оставить newest per rule_id)
2. Добавить partial unique index ON (rule_id) WHERE owner_user_id IS NULL
3. Future INSERT'ы на NULL+existing rule_id будут падать с ON CONFLICT DO NOTHING
   через constraint name (idx_agent_rules_platform_unique)
"""

from alembic import op

# revision identifiers, used by Alembic
revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Dedupe platform rules — keep newest id per rule_id where owner_user_id IS NULL
    op.execute("""
        DELETE FROM agent_rules a
        USING agent_rules b
        WHERE a.owner_user_id IS NULL
          AND b.owner_user_id IS NULL
          AND a.rule_id = b.rule_id
          AND a.id < b.id
    """)

    # 2. Add partial unique index so future duplicates are blocked
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_rules_platform_unique
          ON agent_rules (rule_id) WHERE owner_user_id IS NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_agent_rules_platform_unique")
