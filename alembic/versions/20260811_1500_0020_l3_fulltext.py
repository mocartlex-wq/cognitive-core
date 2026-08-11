"""L3: полнотекстовый индекс для гибридного поиска

Векторный поиск хорошо ловит смысл и плохо — точные токены: имя файла
`GeometryPreview.tsx`, код ошибки `525`, флаг `--no-random-sleep-on-renew`.
Для них нужен обычный полнотекстовый поиск, и он же вытягивает случаи, где
формулировка запроса не совпала с формулировкой знания ни по смыслу, ни по
эмбеддингу — просто по редкому слову.

Конфигурация `russian` выбрана осознанно: содержимое базы знаний — русский
текст с вкраплениями английских идентификаторов. Русский снежок нормализует
окончания («воронок» → «воронк»), а латиница проходит через него без потерь.

Индекс строится по JSONB, приведённому к тексту: содержимое знаний — свободная
структура, отдельного текстового поля у него нет.
"""
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_l3_fts
            ON l3_master_knowledge
         USING GIN (to_tsvector('russian', content::text))
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_l3_tools_fts
            ON l3_tools_registry
         USING GIN (to_tsvector('russian',
                    coalesce(tool_name,'') || ' ' || coalesce(description,'')))
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_l3_fts")
    op.execute("DROP INDEX IF EXISTS idx_l3_tools_fts")
