"""Оба переключателя заместителя видны в одном ответе.

16.08 владелец снял заместителя у агента, а тот продолжил отвечать: снятым
оказался `agent_states.standin_enabled`, а `room_participants.auto_respond`
остался. Переключателя два, они в разных таблицах, и до сих пор **ни один
эндпоинт и ни один экран не отдавал оба сразу** — приходилось смотреть в лог
демона, чтобы узнать, кто на самом деле говорит именем агента.

Проверяем не только наличие полей, но и производные: оператора интересует не
состояние двух флагов, а ответ на вопрос «заговорит ли кто-то этим именем,
пока меня нет».

Отдельно закрепляем приоритет. При `standin_enabled` демон не читает
`auto_respond` вовсе (`cognitive-agent-runtime.py:1852`) — комнатный тумблер
в этом случае ни на что не влияет, и об этом нигде не сообщалось.
"""
from __future__ import annotations

import re
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "app" / "api" / "user.py"


def _participants_block() -> str:
    src = SRC.read_text(encoding="utf-8")
    m = re.search(r"prows = await conn\.fetch\((.*?)participants\.append\(d\)", src, re.S)
    assert m, "блок сбора участников не найден — изменилась структура /detail"
    return m.group(1)


def test_both_switches_are_selected():
    block = _participants_block()
    assert "auto_respond" in block, "комнатный переключатель пропал из выдачи"
    assert "standin_enabled" in block, (
        "полный заместитель не отдаётся вместе с комнатным — оператор снова "
        "увидит один флаг из двух и решит, что снял всё"
    )


def test_derived_answer_is_exposed():
    """Оператор спрашивает не «какие два флага», а «заговорят ли за меня».

    Проверяем состав ответа функции, а не текст запроса: прежняя версия
    этого теста искала имена полей в исходнике и сломалась ровно тогда,
    когда вывод вынесли в функцию — то есть когда код стал лучше.
    """
    from app.api.user import standin_view

    out = standin_view({"standin_enabled": False, "auto_respond": True})
    assert set(out) == {"answers_when_offline", "standin_scope"}


@pytest.mark.parametrize("standin,auto,expected_scope,expected_answers", [
    (True, True, "global", True),
    (True, False, "global", True),
    (False, True, "room", True),
    (False, False, None, False),
])
def test_scope_priority_matches_daemon(standin, auto, expected_scope, expected_answers):
    """Приоритет обязан совпадать с тем, как решает демон.

    Зовём саму функцию, а не повторяем её логику здесь: тест, содержащий
    копию правила, проверяет копию, а не правило.

    Если UI покажет «room», когда включён полный заместитель, оператор
    снимет комнатный тумблер и решит, что дело сделано, — а демон продолжит
    отвечать. Ровно эта последовательность и произошла 16.08.
    """
    from app.api.user import standin_view

    out = standin_view({"standin_enabled": standin, "auto_respond": auto})
    assert out["answers_when_offline"] is expected_answers
    assert out["standin_scope"] == expected_scope


def test_view_is_used_by_the_endpoint():
    """Функция без вызова — мёртвый код: вывод снова разъедется с ответом."""
    assert "standin_view(d)" in _participants_block()
