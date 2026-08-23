"""push_subscriptions — подписки браузера на уведомления.

Revision ID: 0022_push
Revises: 0021
Create Date: 2026-08-23

Ключ таблицы — endpoint, а не суррогатный id: браузер выдаёт endpoint на пару
(устройство, установка), и повторная подписка того же устройства обязана
обновлять строку. С суррогатным ключом ON CONFLICT не наступал бы никогда, и
одно событие приходило бы на телефон столько раз, сколько раз страница
переподписывалась — ровно та ошибка, из-за которой в реестре инструментов
накопилось 2604 записи при 145 уникальных.

Зеркало в app/db/postgres.py (CREATE_TABLES_SQL) — таблица создаётся и на
старте API, потому что на прод миграции катятся руками.
"""
from alembic import op

revision = "0022_push"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            endpoint TEXT PRIMARY KEY,
            user_id UUID NOT NULL,
            p256dh TEXT NOT NULL,
            auth TEXT NOT NULL,
            user_agent TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            last_ok_at TIMESTAMPTZ,
            failures INT NOT NULL DEFAULT 0
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_push_sub_user ON push_subscriptions(user_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS push_subscriptions")
