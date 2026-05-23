"""agent_keys: add missing owner_user_id column

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-23

Phase 4 (PR #23) добавлял owner_user_id в memory layers + agent_states +
создавал owner_quotas. Но agent_keys table эту колонку пропустила —
видимо я забыл когда писал migration 0006.

Симптом: `_resolve_agent_full` в mcp_protocol.py делает
  SELECT agent_id, owner_user_id FROM agent_keys WHERE api_key = $1
и падает с UndefinedColumnError → except ловит → возвращает None →
caller думает что api_key не найден → MCP tools (например cognitive_my_team)
возвращают «API key not registered».

Этот фикс:
1. ADD COLUMN owner_user_id UUID (nullable)
2. Backfill из agent_states по JOIN agent_id (т.к. agent_keys.agent_id и
   agent_states.agent_id это одна и та же сущность)
3. Index для fast lookup в auth-резолве

Идемпотентно — IF NOT EXISTS.
"""
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. ADD COLUMN (nullable пока — старые env-keys не имеют owner)
    op.execute("""
        ALTER TABLE agent_keys
        ADD COLUMN IF NOT EXISTS owner_user_id UUID
    """)

    # 2. Backfill через agent_states (тот же agent_id)
    op.execute("""
        UPDATE agent_keys k
           SET owner_user_id = s.owner_user_id
          FROM agent_states s
         WHERE s.agent_id = k.agent_id
           AND s.owner_user_id IS NOT NULL
           AND k.owner_user_id IS NULL
    """)

    # 3. Index — основной hot-path resolve api_key → owner
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_agent_keys_owner
        ON agent_keys (owner_user_id)
        WHERE owner_user_id IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_agent_keys_owner")
    op.execute("ALTER TABLE agent_keys DROP COLUMN IF EXISTS owner_user_id")
