"""Разбор @-упоминаний должен быть одинаков во всех реализациях.

Он живёт в ТРЁХ местах — процессы разные, общий импорт невозможен:
  1. scripts/cognitive-rooms.py      — сервис комнат (systemd, вне docker)
  2. app/api/user.py                 — владельческий путь (FastAPI, в docker)
  3. scripts/cognitive-agent-runtime.py — демон заместителей (systemd)

Из-за этого правка расходилась: 2026-08-10 двоеточие в идентификаторах
(`claude-code:CRM-kadastr`) починили в сервисе и демоне, а во владельческом
пути забыли. Наружу это выглядело так, будто заместитель владельца
перехватывает чужую почту: обращение схлопывалось до `claude-code`, не
резолвилось и уходило «безадресным» дирижёру.

Тест сравнивает поведение всех трёх на одном наборе входов. Он не проверяет
«правильность» в отрыве от кода — он ловит РАСХОЖДЕНИЕ: если одну копию
поправили, а другую нет, сборка падает.
"""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Где искать регулярку в каждой реализации
SOURCES = {
    "rooms-service": ROOT / "scripts" / "cognitive-rooms.py",
    "owner-path": ROOT / "app" / "api" / "user.py",
    "standin-daemon": ROOT / "scripts" / "cognitive-agent-runtime.py",
}

# Реальные обращения, встречавшиеся в проде
CASES = [
    ("@claude-code:CRM-kadastr проверка", ["claude-code:CRM-kadastr"]),
    ("@claude-code:Designer тест", ["claude-code:Designer"]),
    ("@codex-app связь", ["codex-app"]),
    ("@dsdsd да", ["dsdsd"]),
    ("@Растр привет", ["Растр"]),          # кириллическая метка
    ("без обращения вовсе", []),
    ("@claude-code:CRM и @dsdsd оба", ["claude-code:CRM", "dsdsd"]),
]


def _extract_regex(path: pathlib.Path) -> re.Pattern:
    """Достаёт объявление регулярки упоминаний из исходника."""
    src = path.read_text(encoding="utf-8-sig")
    m = re.search(r"_MENTION_RE(?:_RT)?\s*=\s*re\.compile\(\s*r?[\"']([^\"']+)[\"']", src)
    if not m:
        pytest.fail(f"{path.name}: не найдено объявление _MENTION_RE — разбор @ переехал?")
    return re.compile(m.group(1), re.UNICODE)


@pytest.mark.parametrize("name,path", list(SOURCES.items()))
@pytest.mark.parametrize("text,expected", CASES)
def test_each_implementation_parses_the_same(name, path, text, expected):
    """Каждая реализация должна разобрать обращение одинаково."""
    if not path.exists():
        pytest.skip(f"{path} отсутствует в этой сборке")
    rx = _extract_regex(path)
    got = rx.findall(text)
    assert got == expected, (
        f"{name} ({path.name}) разобрал {text!r} как {got}, ожидалось {expected}. "
        f"Похоже, реализации разошлись — правку внесли не во все три."
    )


def test_colon_is_supported_everywhere():
    """Отдельно и явно: двоеточие в идентификаторе. Именно оно ломалось трижды."""
    broken = []
    for name, path in SOURCES.items():
        if not path.exists():
            continue
        rx = _extract_regex(path)
        if rx.findall("@claude-code:CRM-kadastr") != ["claude-code:CRM-kadastr"]:
            broken.append(f"{name} ({path.name})")
    assert not broken, (
        "Идентификаторы с двоеточием не разбираются в: " + ", ".join(broken) +
        ". Обращение схлопнется до префикса и уйдёт не тому агенту."
    )
