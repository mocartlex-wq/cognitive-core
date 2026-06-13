"""room_participants: per-room auto-responder binding (auto_respond)

Позволяет владельцу привязать КОНКРЕТНОГО агента к авто-ответам в КОНКРЕТНОЙ
комнате. Демон cognitive-agent-runtime будит такого агента на ПРЯМОЕ @упоминание
в этой комнате и постит ответ обратно — БЕЗ включения полного 24/7-«дежурного»
(agent_states.standin_enabled). Флаг живёт на участнике комнаты, поэтому привязка
ровно per-room: включил в одной комнате — выключено в другой.

Зеркалит механизм 0003 (ADD COLUMN IF NOT EXISTS к rooms): колонка добавляется и
в launch/extras/init/01-rooms-schema.sql для свежих БД / CI, а эта миграция
патчит уже существующие инсталляции на деплое.

Revision ID: 0017
Revises: 0016
"""
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF EXISTS: таблица room_participants создаётся rooms-схемой (init SQL), а не
    # alembic-ом. На инсталляции без комнат миграция тогда безопасный no-op.
    op.execute(
        """
        ALTER TABLE IF EXISTS room_participants
        ADD COLUMN IF NOT EXISTS auto_respond BOOLEAN NOT NULL DEFAULT false
        """
    )
    # Частичный индекс под запрос демона load_room_responder_agents()
    # (WHERE auto_respond = true): обычно таких строк мало.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_rp_auto_respond "
        "ON room_participants(agent_id) WHERE auto_respond = true"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_rp_auto_respond")
    op.execute(
        "ALTER TABLE IF EXISTS room_participants DROP COLUMN IF EXISTS auto_respond"
    )
