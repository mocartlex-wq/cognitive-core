"""Миграция 0025: platform-правила гигиены записи в память.

Проверяем без БД — по исходнику миграции и по всей цепочке alembic:
правила именно platform-уровня, вставка add-only и идемпотентная,
downgrade убирает ровно их, а цепочка ревизий остаётся линейной.
"""
import re
from pathlib import Path

VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
MIGRATION = VERSIONS / "20260906_1200_0025_memory_hygiene_rules.py"

EXPECTED_RULES = (
    "rule-memory-domain-by-project",
    "rule-memory-worth-recording",
    "rule-memory-do-not-record",
)


def _src() -> str:
    return MIGRATION.read_text(encoding="utf-8")


class TestMigrationShape:
    def test_migration_exists(self):
        assert MIGRATION.is_file(), f"нет файла {MIGRATION.name}"

    def test_revision_chain(self):
        src = _src()
        assert re.search(r'^revision\s*=\s*"0025"', src, re.M)
        assert re.search(r'^down_revision\s*=\s*"0024"', src, re.M)

    def test_seeds_three_expected_rules(self):
        src = _src()
        for rule_id in EXPECTED_RULES:
            assert src.count(f"'{rule_id}'") >= 1, rule_id

    def test_rules_are_platform_level(self):
        # owner_user_id не задаётся → NULL → правило видно всем владельцам.
        src = _src()
        upgrade = src.split("def upgrade")[1].split("def downgrade")[0]
        assert "owner_user_id" not in upgrade.split("ON CONFLICT")[0], (
            "owner_user_id не должен задаваться: platform-правило = owner_user_id IS NULL"
        )
        assert upgrade.count("'platform'") == len(EXPECTED_RULES)

    def test_rules_are_core_post_task(self):
        upgrade = _src().split("def upgrade")[1].split("def downgrade")[0]
        assert upgrade.count("'core'") == len(EXPECTED_RULES)
        assert upgrade.count("'post-task'") == len(EXPECTED_RULES)

    def test_idempotent_insert(self):
        # Без цели конфликта: у platform-правил owner_user_id IS NULL, и их
        # уникальность держит partial index из 0013 (rule_id WHERE owner IS NULL),
        # а не UNIQUE (owner_user_id, rule_id) — с целью повтор дал бы ошибку.
        assert "ON CONFLICT DO NOTHING" in _src()

    def test_upgrade_is_add_only(self):
        # Никаких UPDATE/DELETE/DROP по чужим правилам в upgrade.
        upgrade = _src().split("def upgrade")[1].split("def downgrade")[0].upper()
        for forbidden in ("UPDATE ", "DELETE ", "DROP ", "TRUNCATE"):
            assert forbidden not in upgrade, f"upgrade должен быть add-only, найдено {forbidden!r}"

    def test_downgrade_removes_exactly_these_rules(self):
        down = _src().split("def downgrade")[1]
        assert "owner_user_id IS NULL" in down, "downgrade обязан ограничиться platform-правилами"
        for rule_id in EXPECTED_RULES:
            assert rule_id in down, rule_id

    def test_positions_do_not_collide(self):
        # Позиции 21..23 — сразу после rule-memory-after-task (20), до plan (30).
        upgrade = _src().split("def upgrade")[1].split("def downgrade")[0]
        positions = [int(m) for m in re.findall(r"^\s+(\d+),\s*$", upgrade, re.M)]
        assert sorted(positions) == [21, 22, 23], positions


class TestAlembicChainStaysLinear:
    """Страховка от реального инцидента: две миграции взяли один номер."""

    def _revisions(self):
        out = []
        for f in VERSIONS.glob("*.py"):
            src = f.read_text(encoding="utf-8")
            rev = re.search(r'^revision\s*=\s*"([^"]+)"', src, re.M)
            down = re.search(r'^down_revision\s*=\s*"?([^"\s]+)"?', src, re.M)
            if rev:
                out.append((f.name, rev.group(1), down.group(1) if down else None))
        return out

    def test_revision_ids_unique(self):
        revs = [r for _, r, _ in self._revisions()]
        dupes = {r for r in revs if revs.count(r) > 1}
        assert not dupes, f"дублирующиеся revision: {dupes}"

    def test_no_forked_chain(self):
        downs = [d for _, _, d in self._revisions() if d and d != "None"]
        dupes = {d for d in downs if downs.count(d) > 1}
        assert not dupes, f"цепочка раздвоилась на: {dupes}"

    def test_0025_is_the_only_head(self):
        revs = self._revisions()
        all_revs = {r for _, r, _ in revs}
        downs = {d for _, _, d in revs}
        heads = all_revs - downs
        assert heads == {"0025"}, f"ожидали единственную голову 0025, получили {heads}"
