"""agent_states: machine_fingerprint + machine_label + audit timestamps

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-22

Owner-vision (полный 3-й вариант multi-agent registry):
- machine_fingerprint = sha256(hostname+user+os)[:16] — стабильный
  identifier машины-владельца, не меняется между установками
- machine_label — человеческое имя «MacBook Pro M2», задаётся owner-ом
- last_mcp_disconnect_at — для презенс-логики, когда SSE-stream закрылся
- first_mcp_connect_at — для auto-DM «новый агент подключился» (только один раз)

Idempotent: IF NOT EXISTS, безопасно повторить.
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE agent_states
        ADD COLUMN IF NOT EXISTS machine_fingerprint VARCHAR(32),
        ADD COLUMN IF NOT EXISTS machine_label VARCHAR(128),
        ADD COLUMN IF NOT EXISTS last_mcp_disconnect_at TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS first_mcp_connect_at TIMESTAMPTZ
    """)
    # Композитный индекс — fast lookup «у меня уже есть agent на этой машине?»
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_agent_states_owner_machine
        ON agent_states (owner_user_id, machine_fingerprint)
        WHERE machine_fingerprint IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_agent_states_owner_machine")
    op.execute("""
        ALTER TABLE agent_states
        DROP COLUMN IF EXISTS machine_fingerprint,
        DROP COLUMN IF EXISTS machine_label,
        DROP COLUMN IF EXISTS last_mcp_disconnect_at,
        DROP COLUMN IF EXISTS first_mcp_connect_at
    """)
