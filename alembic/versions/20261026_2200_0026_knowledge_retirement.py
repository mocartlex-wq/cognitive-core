"""Причина и время адресного снятия знаний владельца."""
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE l3_master_knowledge ADD COLUMN IF NOT EXISTS retired_at TIMESTAMPTZ")
    op.execute("ALTER TABLE l3_master_knowledge ADD COLUMN IF NOT EXISTS retire_reason TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE l3_master_knowledge DROP COLUMN IF EXISTS retire_reason")
    op.execute("ALTER TABLE l3_master_knowledge DROP COLUMN IF EXISTS retired_at")
