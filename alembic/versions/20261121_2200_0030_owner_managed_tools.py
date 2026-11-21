"""Keep owner-authored catalog entries out of automated registry maintenance."""
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE l3_tools_registry ADD COLUMN IF NOT EXISTS owner_managed boolean NOT NULL DEFAULT false")


def downgrade():
    op.execute("ALTER TABLE l3_tools_registry DROP COLUMN IF EXISTS owner_managed")
