"""Демон заместителей просыпается по PG NOTIFY, а не раз в 5с
(план «связь owner↔флот», Фаза 2, 2026-09-05).

До этого `main()` спал `time.sleep(poll_sec)` и НЕ слушал ни один канал, хотя
триггеры room_event / agent_inbox в БД были: потолок задержки owner→агент = 5с
даже для мгновенных каналов. Теперь фоновый поток держит LISTEN и поднимает
событие, а опрос остаётся страховкой раз в 30с.
"""
from __future__ import annotations

import builtins
import importlib.util
import os
import pathlib
import threading

import pytest

DAEMON = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "cognitive-agent-runtime.py"


@pytest.fixture(scope="module")
def daemon(tmp_path_factory):
    d = tmp_path_factory.mktemp("rt-wake")
    os.environ["COGCORE_RUNTIME_LOG"] = str(d / "rt.log")
    os.environ["COGCORE_RUNTIME_HISTORY"] = str(d)
    spec = importlib.util.spec_from_file_location("cogcore_rt_wake", DAEMON)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_wait_is_safety_interval_only_with_live_listen(daemon):
    assert daemon.compute_wait_seconds(5, listen_active=False) == 5
    assert daemon.compute_wait_seconds(5, listen_active=True) == daemon.SAFETY_POLL_SEC
    # персональный интервал длиннее страховочного — уважаем его
    assert daemon.compute_wait_seconds(120, listen_active=True) == 120
    # мусор в конфиге — не падаем и не крутимся вхолостую
    assert daemon.compute_wait_seconds(0, listen_active=False) == daemon.DEFAULT_POLL_SEC
    assert daemon.compute_wait_seconds(None, listen_active=False) == daemon.DEFAULT_POLL_SEC
    assert daemon.compute_wait_seconds(-3, listen_active=False) == 1


def test_notify_wakes_waiter_immediately(daemon):
    """Событие, поднятое «слушателем», обрывает страховочное ожидание сразу."""
    daemon._WAKE.clear()
    t = threading.Timer(0.05, daemon._WAKE.set)
    t.start()
    try:
        woke = daemon._WAKE.wait(daemon.compute_wait_seconds(5, listen_active=True))
    finally:
        t.cancel()
        daemon._WAKE.clear()
    assert woke is True


def test_listener_without_psycopg_leaves_polling(daemon, monkeypatch):
    """Нет psycopg — поток завершается, флаг active остаётся False (опрос 5с)."""
    real_import = builtins.__import__

    def _no_psycopg(name, *a, **kw):
        if name == "psycopg":
            raise ImportError("no psycopg")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _no_psycopg)
    daemon._LISTEN_STATE["active"] = False
    daemon.wake_listener()  # должен вернуться сразу, не зациклиться
    assert daemon._LISTEN_STATE["active"] is False


def test_main_loop_uses_wake_event_not_sleep(daemon):
    """Страховка от регресса: в главном цикле ожидание идёт через _WAKE.wait,
    а старый `time.sleep(poll_sec)` не вернулся."""
    src = DAEMON.read_text(encoding="utf-8")
    assert "_WAKE.wait(compute_wait_seconds(" in src
    assert "time.sleep(poll_sec)" not in src
    assert 'LISTEN room_event;' in src and 'LISTEN agent_inbox;' in src
