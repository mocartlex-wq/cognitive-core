"""Гейт владельца: ключи без owner_user_id теряют силу.

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-05

С этой ревизии код трактует `agent_keys.owner_user_id IS NULL` как отказ, а не
как «admin без фильтра». До того `POST /agents/register` без авторизации
выдавал такой ключ → чтение памяти всех владельцев + деплой на прод
(подтверждено на проде 2026-09-05, план «связь owner↔флот», Фаза 0).

Данные:
  1. Ключ без владельца, чей агент владельца ИМЕЕТ (ключ выпущен до появления
     колонки owner_user_id) — наследует владельца агента. Ничего не ломается.
  2. Остальные ключи без владельца отзываются (revoked_at = NOW()). На проде
     это 10 агентов: 9 тестовых заглушек мая–июня (`test_*`,
     `claude_via_mcp_test`) и `cognitive-core-laptop` (лэптоп до миграции на
     новый ПК; при необходимости переподключить через claim-wizard).

Строки agent_states не удаляются (add-only правило миграций: откат кода на
новую схему должен оставаться безопасным). Downgrade возвращает отозванным
ключам жизнь, но не «админство» — его в коде больше нет.
"""
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

_REVOKE_NOTE = "orphan key revoked by 0024 owner gate"


def upgrade() -> None:
    # 1. Наследование владельца от агента.
    op.execute("""
        UPDATE agent_keys k
           SET owner_user_id = s.owner_user_id
          FROM agent_states s
         WHERE k.agent_id = s.agent_id
           AND k.owner_user_id IS NULL
           AND s.owner_user_id IS NOT NULL
    """)
    # 2. Сироты — отзыв. description помечаем, чтобы downgrade вернул ровно их.
    op.execute(f"""
        UPDATE agent_keys
           SET revoked_at = NOW(),
               description = COALESCE(description, '') || ' [{_REVOKE_NOTE}]'
         WHERE owner_user_id IS NULL
           AND revoked_at IS NULL
    """)


def downgrade() -> None:
    op.execute(f"""
        UPDATE agent_keys
           SET revoked_at = NULL,
               description = REPLACE(description, ' [{_REVOKE_NOTE}]', '')
         WHERE description LIKE '%[{_REVOKE_NOTE}]%'
    """)
