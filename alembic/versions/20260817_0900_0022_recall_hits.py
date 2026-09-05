"""Построчный лог показов знания: что recall вернул и пригодилось ли это

До сих пор система не помнила, какие записи памяти кому показывались. Ни
счётчика использования, ни лога обращений: единственная обратная связь —
`feedback_record` — писала в Redis с TTL 24 часа и не читалась никем.

Поэтому на вопрос «какие записи хоть раз предотвратили повтор» ответить было
нечем. Именно так 59% недостижимых знаний прожили незамеченными до ручного
разбора 2026-08-11: поиск отвечал пустотой, и никто этого не считал.

ПОЧЕМУ СТРОКА НА ПОКАЗ, А НЕ СЧЁТЧИК НА ЗАПИСИ

Денормализованные `usage_count` / `last_used_at` на `l3_master_knowledge`
дешевле, но теряют временнýю ось. А вопрос звучит «предотвратила ли запись
повтор ПОСЛЕ того, как начала показываться» — без даты показа на него не
ответить никогда. При `top_k=5` это пять строк на вызов, цена невелика.

`useful` заполняется отложенно, когда агент даёт обратную связь по конкретной
записи. NULL — «не сказал», это не то же самое, что «бесполезно».

Внешнего ключа на l3 намеренно НЕТ: показывать могут и инструмент из
`l3_tools_registry`, и запись из `l3_master_knowledge`, а запись может быть
депрецирована позже — история показов от этого не должна исчезать.
"""
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS l3_recall_hits (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            record_id UUID NOT NULL,
            record_type VARCHAR(16) NOT NULL,
            session_id UUID,
            domain VARCHAR(64),
            rank INT,
            distance DOUBLE PRECISION,
            owner_user_id UUID,
            useful BOOLEAN,
            shown_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    # «Сколько раз показывалась эта запись и когда впервые» — основной запрос.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_recall_hits_record "
        "ON l3_recall_hits(record_id, shown_at DESC)"
    )
    # Обратная связь приходит по паре (сессия, запись) — по ней и обновляем.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_recall_hits_session "
        "ON l3_recall_hits(session_id, record_id)"
    )
    # Срезы по тенанту и времени: «что показывалось этому владельцу за период».
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_recall_hits_owner_time "
        "ON l3_recall_hits(owner_user_id, shown_at DESC) "
        "WHERE owner_user_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS l3_recall_hits")
