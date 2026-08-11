"""L2: уникальность буфера в границах владельца, а не глобально

Было: UNIQUE (date, domain). Два владельца с одинаковым доменом в один день
попадали в ОДНУ строку — `ON CONFLICT (date, domain) DO UPDATE` склеивал их
source_event_ids и усреднял confidence. То есть свёртки разных тенантов
смешивались, а не просто «не фильтровались».

Сейчас это не проявляется: владелец в системе один. Но чинить надо до того, как
появится второй, а не после.

Стратегия — в два приёма, чтобы не было окна отказа:
  0019 (эта)  — добавляет новый уникальный индекс, СТАРЫЙ ОСТАВЛЯЕТ.
                Работающий код с `ON CONFLICT (date, domain)` продолжает жить.
  0020 (позже) — снимает старый, когда новый код уже раскатан.

NULLS NOT DISTINCT (Postgres 15+) обязателен: по умолчанию NULL-ы считаются
различными, и записи без владельца — а их сейчас большинство — не конфликтовали
бы между собой вовсе, то есть индекс не защищал бы ровно тот случай, ради
которого создан.
"""
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_l2_date_domain_owner
            ON l2_daily_buffers (date, domain, owner_user_id) NULLS NOT DISTINCT
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_l2_date_domain_owner")
