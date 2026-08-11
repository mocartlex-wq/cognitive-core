"""Защиты заместителей: адресация, петли, живой агент.

Ни на одну из них тестов не было — все три написаны по следам инцидентов
2026-08-10, когда контур из 12 агентов начал мешать сам себе:

  • dsdsd (заместитель владельца) отвечал на сообщения, адресованные ДРУГИМ
    агентам, — потому что триггер срабатывал на любой непробельный символ;
  • analyst и claude-8d5b07 сцепились в пинг-понг: 9 вызовов LLM за минуту,
    останавливал их только часовой лимит;
  • живая сессия и её заместитель отвечали под ОДНИМ именем в одну минуту,
    противореча друг другу, и живой агент публиковал разъяснение вручную.

Функции живут в systemd-демоне вне пакета `app`, поэтому загружаем модуль по
пути. Так тест проверяет ровно тот код, который работает на сервере.
"""
import importlib.util
import pathlib
import time

import pytest

DAEMON = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "cognitive-agent-runtime.py"


@pytest.fixture(scope="module")
def daemon(tmp_path_factory):
    """Загружает демон как модуль.

    Пути журнала и истории переопределяются на временные: иначе модуль пытается
    открыть /var/log/... и не импортируется нигде, кроме сервера. Ровно из-за
    этого на защиты заместителей раньше не было тестов.
    """
    import os

    if not DAEMON.exists():
        pytest.skip("демон отсутствует в этой сборке")
    tmp = tmp_path_factory.mktemp("runtime")
    os.environ["COGCORE_RUNTIME_LOG"] = str(tmp / "runtime.log")
    os.environ["COGCORE_RUNTIME_HISTORY"] = str(tmp / "history")

    spec = importlib.util.spec_from_file_location("cogcore_agent_runtime", DAEMON)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        pytest.skip(f"демон не загружается в тестовом окружении: {e}")
    return mod


# ─── Адресация ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,expected_silent,why", [
    ("@claude-code:CRM-kadastr проверка", True, "адресовано другому — молчим"),
    ("@analyst @orchestrator", True, "оба других — молчим"),
    ("@dsdsd да", False, "адресовано мне — отвечаем"),
    ("@dsdsd: да", False, "мне, с двоеточием в конце — отвечаем"),
    ("@Память привет", False, "мне по метке — отвечаем"),
    ("@claude-code:CRM и @dsdsd оба", False, "мне в том числе — отвечаем"),
    ("просто вопрос без адреса", False, "безадресное — отвечаем"),
])
def test_addressed_to_others(daemon, text, expected_silent, why):
    got = daemon.addressed_to_others(text, "dsdsd", "Память")
    assert got is expected_silent, f"{why}: {text!r} → молчит={got}"


# ─── Петля между заместителями ────────────────────────────────────────────
def test_peer_exchange_guard_breaks_ping_pong(daemon, tmp_path):
    """После порога обменов с одним собеседником — стоп.

    Именно этого не хватало: loop_depth считает ВЕТВЛЕНИЕ (ответы на один
    parent), а в цепочке A→B→A→B у каждого сообщения свой parent с
    единственным ответом — счётчик не растёт никогда.
    """
    history = daemon.HistoryStore(str(tmp_path / "hist.json"))
    persona = {"escalation_rules": {"max_exchanges_per_peer": 3, "peer_window_sec": 600}}

    allowed = 0
    for _ in range(10):
        ok, _reason = history.peer_exchange_ok(persona, "claude-8d5b07")
        if not ok:
            break
        allowed += 1
        history.record_peer_exchange("claude-8d5b07")

    assert allowed == 3, f"прошло {allowed} обменов вместо 3 — петля не обрывается"

    # Другой собеседник не должен страдать от лимита первого
    ok, _ = history.peer_exchange_ok(persona, "someone-else")
    assert ok, "лимит на одного собеседника не должен блокировать остальных"


def test_peer_window_expires(daemon, tmp_path):
    """Лимит скользящий: за пределами окна обмены снова разрешены."""
    history = daemon.HistoryStore(str(tmp_path / "hist2.json"))
    persona = {"escalation_rules": {"max_exchanges_per_peer": 2, "peer_window_sec": 1}}
    for _ in range(2):
        history.record_peer_exchange("peer")
    assert not history.peer_exchange_ok(persona, "peer")[0]
    time.sleep(1.1)
    assert history.peer_exchange_ok(persona, "peer")[0], "окно должно истекать"


# ─── Живой агент ──────────────────────────────────────────────────────────
def test_self_posts_are_not_mistaken_for_live_agent(daemon, tmp_path):
    """Собственные посты демона не должны считаться признаком живой сессии.

    Иначе заместитель, ответив один раз, сам себя объявил бы «живым агентом» и
    замолчал бы навсегда.
    """
    history = daemon.HistoryStore(str(tmp_path / "hist3.json"))
    history.record_self_post("msg-1")
    assert history.is_self_post("msg-1") is True
    assert history.is_self_post("msg-2") is False
