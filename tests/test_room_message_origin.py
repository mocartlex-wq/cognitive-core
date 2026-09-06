"""Происхождение реплики: кто ФАКТИЧЕСКИ произвёл текст.

16.08 в комнате появились реплики с обязательствами, которых живая сессия не
давала — «подтяну ветку, прогоню 37 проверок» написал DeepSeek-заместитель, а
адресат принял за слово агента. Отличить было нельзя: заместитель постит под
тем же `from_agent`, что живая сессия, и это сделано намеренно
(`cognitive-agent-runtime.py:1500-1502`, «agent answers in its own voice»).

Колонка `room_messages.metadata` существовала с самого начала и не
использовалась ни на запись, ни на чтение — миграция не понадобилась.

Ключевое соглашение, которое тесты и закрепляют: **пусто означает «не
помечено», а не «живая сессия»**. Доказательства живости у нас нет, и
выдавать одно за другое нельзя — на этом уже обожглись, когда
`live_agent_active` посчитал молчащую сессию отсутствующей.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

ROOMS = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "cognitive-rooms.py"
DAEMON = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "cognitive-agent-runtime.py"


def _source(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def daemon(tmp_path_factory):
    d = tmp_path_factory.mktemp("rt")
    import os
    os.environ["COGCORE_RUNTIME_LOG"] = str(d / "rt.log")
    os.environ["COGCORE_RUNTIME_HISTORY"] = str(d)
    spec = importlib.util.spec_from_file_location("cogcore_rt_origin", DAEMON)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_daemon_marks_its_own_replies(daemon, tmp_path, monkeypatch):
    """Пометка уходит на сервер вместе с текстом, а не выводится сервером."""
    captured = {}

    def _fake_post(url, body, headers=None):
        captured.update(body)
        return {"id": "m-1"}

    monkeypatch.setattr(daemon, "resolve_room_key", lambda r: "rk")
    monkeypatch.setattr(daemon, "http_post", _fake_post)
    history = daemon.HistoryStore(str(tmp_path / "h.json"))

    daemon.post_to_room("room-1", "agent-x", "текст", history, model="deepseek")

    meta = captured.get("origin_meta")
    assert meta, "реплика ушла без пометки — отличить от живой сессии снова нельзя"
    assert meta["origin"] == "standin"
    assert meta["model"] == "deepseek", "модель нужна: 'ответил заместитель' и 'ответил DeepSeek' — разные факты"


def test_every_daemon_branch_names_its_model(daemon):
    """Ветка без модели пометит реплику как unknown — это потеря, а не деталь."""
    src = _source(DAEMON)
    calls = [ln for ln in src.splitlines() if "post_to_room(room_id" in ln and "def " not in ln]
    assert len(calls) == 4, f"веток ответа в комнату стало {len(calls)}, ожидалось 4"
    # Аргумент model может стоять на следующей строке — берём вызов с продолжением.
    joined = src.replace("\n", " ")
    for marker in ('model="auto_ack"', 'model="deepseek"', 'model="managed"', "model=model"):
        assert marker in joined, f"ветка без {marker}: её реплики будут помечены unknown"


def test_rooms_service_persists_and_returns_origin():
    src = _source(ROOMS)
    assert "metadata" in src.split("def post_message")[1][:900], (
        "запись происхождения не дошла до INSERT — колонка так и осталась мёртвой"
    )
    assert '"origin": meta.get("origin")' in src, (
        "происхождение не отдаётся наружу: пометка пишется в никуда"
    )


def test_absent_mark_is_not_claimed_as_live():
    """Нигде не должно появиться origin='live' по умолчанию.

    Соблазн велик: раз заместитель помечается, значит остальное — живая
    сессия. Это вывод, а не факт: так же рассуждал `live_agent_active`,
    когда счёл молчащую сессию отсутствующей.
    """
    src = _source(ROOMS)
    assert '"live"' not in src.split("def post_message")[1][:900]
    assert 'or "live"' not in src


def test_ui_shows_the_mark_where_the_confusion_happened():
    """Пометка обязана быть в шапке сообщения, рядом с именем.

    Именно там читатель решает, чьё это слово. В JSON её достаточно для
    разбора постфактум, но 16.08 обязательство приняли за слово агента,
    ЧИТАЯ ленту — значит и различать надо в ленте.

    Проверено в браузере на стенде, собранном из настоящего кода страницы:
    у реплик с origin=standin значок есть, у остальных нет, подсказка несёт
    имя модели, переполнения шапки нет ни на 393px, ни на 320px.
    """
    ui = (pathlib.Path(__file__).resolve().parent.parent / "sandbox" / "room.html")
    src = ui.read_text(encoding="utf-8")
    block = src.split("function messagesHtml")[1][:1600]
    assert "originBadge" in block, "значок не рисуется в ленте"
    assert "m.origin === 'standin'" in block, (
        "значок вешается не по происхождению — либо не на том условии"
    )
    assert "origin_model" in block, "в подсказке нет модели: «заместитель» и «DeepSeek» — разные факты"
    assert ".chip-standin" in src, "нет стиля: значок сольётся с обычными чипами"


def test_ui_does_not_invent_a_live_badge():
    """Обратной пометки быть не должно.

    Нарисовав «живая сессия» там, где просто нет метки, мы выдали бы
    предположение за факт — и повторили бы ошибку live_agent_active.
    """
    ui = (pathlib.Path(__file__).resolve().parent.parent / "sandbox" / "room.html")
    block = ui.read_text(encoding="utf-8").split("function messagesHtml")[1][:1600]
    assert "живая сессия<" not in block and "chip-live" not in block


def test_owner_path_also_carries_origin():
    """Владельческая страница читает сообщения СВОИМ запросом.

    Найдено проверкой в браузере под живой сессией владельца 17.08: пометка
    писалась rooms-сервисом и им же отдавалась, но `sandbox/room.html` ходит
    в `/user/rooms/{id}/detail` (`app/api/user.py`) — там отдельный SELECT, и
    происхождение до экрана не доезжало.

    Классика двух источников одной правды: правку внесли в одном месте, а
    смотрят в другое. Ровно так же расходились CREATE_TABLES_SQL и миграция.
    """
    user_api = (pathlib.Path(__file__).resolve().parent.parent / "app" / "api" / "user.py")
    src = user_api.read_text(encoding="utf-8")
    block = src.split("mrows = await conn.fetch")[1][:2200]
    assert "m.metadata" in block, "владельческий SELECT не забирает происхождение"
    assert '"origin"' in block, "происхождение не попадает в ответ владельческого пути"


def test_both_read_paths_agree_on_field_names():
    """Имена полей обязаны совпадать: фронт один, читает и то и другое."""
    rooms = _source(ROOMS)
    user_api = (pathlib.Path(__file__).resolve().parent.parent / "app" / "api" / "user.py")
    for field in ('"origin"', '"origin_model"'):
        assert field in rooms, f"{field} нет в rooms-сервисе"
        assert field in user_api.read_text(encoding="utf-8"), f"{field} нет во владельческом пути"
