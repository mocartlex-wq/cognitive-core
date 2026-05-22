"""agent_states: last_mcp_connect_at для presence-indicator в /ui/profile

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-22

Owner-feedback: нужен видимый зелёный/серый индикатор «MCP подключён».
При открытии /mcp/sse handler пишет NOW() в эту колонку. UI поллит
GET /user/agents каждые 30 сек, рисует:
  🟢 зелёный — last_mcp_connect_at < 60 сек назад
  ⚪ серый  — больше 60 сек или NULL

IF NOT EXISTS — миграция safe даже если применялась дважды (на случай
если auto-deploy упадёт между шагами).
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE agent_states
        ADD COLUMN IF NOT EXISTS last_mcp_connect_at TIMESTAMPTZ
    """)
    # Index для быстрых presence-запросов «WHERE last_mcp_connect_at > NOW() - INTERVAL '60s'»
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_agent_states_mcp_connect
        ON agent_states (last_mcp_connect_at)
        WHERE last_mcp_connect_at IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_agent_states_mcp_connect")
    op.execute("ALTER TABLE agent_states DROP COLUMN IF EXISTS last_mcp_connect_at")
