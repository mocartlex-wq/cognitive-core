"""Корпус инцидентов: формат обязан держать настоящие случаи.

Шаг 0 плана «опыт агентов как передаваемая величина», и он сознательно идёт
до всякого кода: если формат не удержит пять уже случившихся инцидентов,
неверен формат, а не случаи. Дешевле этой проверки замысла ничего нет.

Единица переноса опыта между агентами — исполняемая проверка ПЛЮС инцидент,
который её оправдывает. Порознь они не живут: проверка без истории удаляется
как шум при первой же неудобности, инцидент без проверки забывается следующей
сессией. Поэтому формат требует обе половины и требует честно помечать те
случаи, где вторая половина отсутствует.
"""
from __future__ import annotations

import json
import pathlib

import pytest

CORPUS = pathlib.Path(__file__).resolve().parent / "fixtures" / "incidents"
ROOT = pathlib.Path(__file__).resolve().parent.parent

REQUIRED = [
    "id", "title", "first_seen", "last_seen", "occurrences", "cost",
    "detected_by", "unasked_question", "false_confirmations",
    "check", "repro", "external_deps", "status",
]
STATUSES = {"open", "covered", "prose_only"}


def _incidents() -> list[tuple[str, dict]]:
    out = []
    for p in sorted(CORPUS.glob("*.json")):
        out.append((p.stem, json.loads(p.read_text(encoding="utf-8"))))
    return out


def test_corpus_is_not_empty():
    assert len(_incidents()) >= 5, (
        "формат проверяется настоящими случаями; на трёх он выглядит верным всегда"
    )


@pytest.mark.parametrize("name,inc", _incidents())
def test_required_fields_present(name, inc):
    missing = [f for f in REQUIRED if f not in inc]
    assert not missing, f"{name}: нет полей {missing}"


@pytest.mark.parametrize("name,inc", _incidents())
def test_id_matches_filename(name, inc):
    assert inc["id"] == name, (
        f"{name}: id — ключ повторяемости и связь с записью в базе; "
        "расхождение с именем файла разорвёт её молча"
    )


@pytest.mark.parametrize("name,inc", _incidents())
def test_status_is_known_and_consistent_with_check(name, inc):
    assert inc["status"] in STATUSES, f"{name}: неизвестный статус {inc['status']}"
    has_check = bool((inc.get("check") or {}).get("ref"))
    if inc["status"] == "covered":
        assert has_check, f"{name}: статус covered без ссылки на проверку"
    if inc["status"] == "prose_only":
        assert not has_check, (
            f"{name}: статус prose_only, но проверка указана — "
            "статус обязан отражать, чем урок держится на самом деле"
        )


@pytest.mark.parametrize("name,inc", _incidents())
def test_check_file_exists(name, inc):
    ref = (inc.get("check") or {}).get("ref")
    if not ref:
        pytest.skip(f"{name}: урок держится на прозе, проверки нет")
    path = ROOT / ref.split("::")[0]
    assert path.exists(), (
        f"{name}: проверка {ref} не найдена. Мёртвая ссылка хуже отсутствующей: "
        "инцидент числится закрытым, а стража нет"
    )


@pytest.mark.parametrize("name,inc", _incidents())
def test_replay_has_both_sides(name, inc):
    """Односторонний прогон не отличает «ловит» от «баг уже починен»."""
    repro = inc["repro"]
    assert repro.get("bad"), f"{name}: нет входов, на которых проверка обязана упасть"
    assert repro.get("good"), (
        f"{name}: нет входов, на которых проверка обязана пройти — "
        "без них проверка, блокирующая всё подряд, получает идеальный балл"
    )


@pytest.mark.parametrize("name,inc", _incidents())
def test_false_confirmations_explain_why_they_lied(name, inc):
    """Список ложных подтверждений — половина ценности записи.

    Именно он отвечает на вопрос «почему это не нашли раньше», и именно его
    не хватало каждый раз: сигнал был, ему верили, он лгал.
    """
    fc = inc["false_confirmations"]
    assert fc, f"{name}: пусто — значит не разобрано, почему дефект жил незамеченным"
    for item in fc:
        assert item.get("signal") and item.get("why_it_lied"), (
            f"{name}: у ложного подтверждения нет объяснения, чем именно оно лгало"
        )


@pytest.mark.parametrize("name,inc", _incidents())
def test_cost_is_stated(name, inc):
    assert len(inc["cost"]) > 40, (
        f"{name}: цена не названа. Правило без цены снимают первым, когда оно мешает"
    )


def test_repeated_class_is_counted_not_duplicated():
    """Повтор инкрементирует occurrences, а не заводит вторую запись.

    Иначе повторяемость превращается в задачу дедупликации, а она и есть
    единственная объективная мера ценности знания.
    """
    ids = [name for name, _ in _incidents()]
    assert len(ids) == len(set(ids))
    repeated = [(n, i["occurrences"]) for n, i in _incidents() if i["occurrences"] > 1]
    assert repeated, (
        "ни одного класса с повтором — либо корпус слишком мал, либо "
        "повторы записывают как новые случаи"
    )
