"""agent_states: per-agent display label (agent_label)

Позволяет owner-у переименовывать КОНКРЕТНОГО помощника (отображаемое имя),
не трогая agent_id (он завязан на api_key). Это чисто UI-подпись.

machine_label остаётся атрибутом ГРУППЫ машины; agent_label — индивидуальный.

Revision ID: 0016_agent_label
Revises: 0015_user_webhooks
"""
from alembic import op

revision = "0016_agent_label"
down_revision = "0015_user_webhooks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE agent_states
        ADD COLUMN IF NOT EXISTS agent_label VARCHAR(128)
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agent_states DROP COLUMN IF EXISTS agent_label")
