"""agent_states: status column + pending_claim для UX visibility

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-23

Owner-issue: при генерации claim-token agent не виден в /ui/profile до тех
пор пока новая Claude Code сессия не сделает curl /claim. Owner недоумевает
«нажал генерировать — где агент?». Для service-grade UX нужно:

1. issue_claim_token СРАЗУ создаёт agent_states row со status='pending_claim'
2. /ui/profile показывает его с countdown «10 мин до удаления»
3. claim → status='active'
4. Cron каждую минуту удаляет pending старше 10 мин (token expired)

Это убирает confusion + даёт owner-у visibility в момент когда agent
ещё не подключился.

Идемпотентно.
"""
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE agent_states
        ADD COLUMN IF NOT EXISTS status VARCHAR(32) NOT NULL DEFAULT 'active'
    """)
    # Все существующие — active. Новые pending помечаются явно в connect.py.
    op.execute("""
        UPDATE agent_states SET status = 'active' WHERE status IS NULL OR status = ''
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_agent_states_pending
        ON agent_states (created_at)
        WHERE status = 'pending_claim'
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_agent_states_pending")
    op.execute("ALTER TABLE agent_states DROP COLUMN IF EXISTS status")
