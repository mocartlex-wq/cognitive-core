"""Выпадающие списки профиля читаемы в тёмной теме.

06.09 владелец открыл селектор канала на карточке агента: Chrome нарисовал
список на светлой подложке, а цвет текста наследовался белый — видны были
только иконки. Пунктам задан явный цвет и фон, независимо от color-scheme.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROFILE = (ROOT / "sandbox" / "profile.html").read_text(encoding="utf-8")


def test_select_options_have_explicit_colors():
    m = re.search(r"select option\{([^}]*)\}", PROFILE)
    assert m, "нет правила для option"
    rule = m.group(1)
    assert "color:#111" in rule and "background:#fff" in rule
