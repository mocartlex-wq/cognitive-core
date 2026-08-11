"""Реестр инструментов: снять дубли и запретить их появление

`ON CONFLICT DO NOTHING` стоял на первичном ключе, а id генерировался свежим
uuid4 при каждой вставке — конфликт не наступал НИКОГДА. Каждая недельная
свёртка добавляла ещё по копии каждого инструмента.

На проде 2026-08-11: 2604 активных записи при 145 уникальных парах
(домен, имя). Один инструмент — `pydantic-settings` в домене fastapi_dev —
записан 121 раз. Это не только мусор в выдаче: реестр целиком уходит в промпт
месячного аудита, то есть за дубли ещё и платили LLM.

Уникальность ставится ЧАСТИЧНОЙ — только по действующим записям. История
депрекации должна сохранять повторы: инструмент может быть заведён, устареть и
появиться снова.
"""
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Оставляем самую раннюю запись в каждой группе, остальные — в историю.
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY domain, tool_name, owner_user_id
                       ORDER BY created_at ASC, id ASC
                   ) AS rn
              FROM l3_tools_registry
             WHERE effective_to IS NULL
        )
        UPDATE l3_tools_registry t
           SET effective_to = NOW()
          FROM ranked r
         WHERE t.id = r.id AND r.rn > 1
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_l3_tools_unique_active
            ON l3_tools_registry (domain, tool_name, owner_user_id)
            NULLS NOT DISTINCT
         WHERE effective_to IS NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_l3_tools_unique_active")
    # Депрекацию не откатываем: восстановить, какие записи были дублями,
    # уже нельзя — да и возвращать 2459 копий незачем.
