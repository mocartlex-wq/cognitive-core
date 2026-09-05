"""Бездоменный поиск должен быть достижим оттуда, откуда им пользуются.

05.09. Соседний агент сообщил, что recall не находит ничего и домен
приходится угадывать из 54. Я ответил, что бездоменный поиск существует, —
и был неправ в главном: он существует в HTTP-модели `OperativeQuery`, где
`domain` необязателен и это описано в докстроке, но в схеме MCP-инструмента
`domain` стоял в `required`. Агенты ходят через MCP. Значит для КАЖДОГО
агента этой возможности не было с самого начала.

Класс — тот же, что дважды до этого за день: измерил не тот слой. Сначала
проверил заголовки `/sw.js` через `curl -I` (HEAD → 405) и чуть не объявил
поломкой nginx; потом проверил необязательность домена в модели API вместо
схемы MCP и объявил возможность работающей. Оба раза код был прав, а замер
смотрел не туда.

Отдельно: `domain=""` уходил в поиск как есть, искал домен с пустым именем и
у соседей завис на 300 с. Пустая строка — это «домен не указан».
"""
from __future__ import annotations

import pathlib
from unittest.mock import MagicMock, patch

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _recall_tool() -> dict:
    from app.api.mcp_protocol import TOOLS

    tools = {t["name"]: t for t in TOOLS}
    assert "cognitive_recall" in tools
    return tools["cognitive_recall"]


def test_domain_is_not_required_in_mcp_schema():
    """Единственная проверка, которая ловила бы исходный дефект."""
    schema = _recall_tool()["inputSchema"]
    assert schema["required"] == ["query"], (
        "domain в required делает бездоменный поиск недостижимым для агентов — "
        "они ходят через MCP, а не через HTTP-модель"
    )
    assert "domain" in schema["properties"], "сам параметр остаётся, он полезен"


def test_description_tells_that_domain_can_be_omitted():
    """Возможность, о которой неоткуда узнать, всё равно что отсутствует.

    Она и была таковой: в модели API описана дословно нужным симптомом
    («агент обязан был угадать нужный»), а в том единственном тексте, который
    видит агент, — ни слова.
    """
    d = _recall_tool()["description"]
    assert "НЕОБЯЗАТЕЛЕН" in d or "необязателен" in d.lower()
    for dom in ("work_journal", "infra_lessons", "skills"):
        assert dom in d, f"не сказано, где искать без домена: {dom}"


def test_schema_copies_agree():
    """Схема лежит в двух местах — они обязаны совпадать по обязательности.

    Расхождение двух копий одного описания уже давало молчаливую поломку
    сегодня (две независимые карты типов для звука), поэтому проверяем.
    """
    src = (ROOT / "app" / "api" / "openapi_gen.py").read_text(encoding="utf-8")
    block = src.split('"RecallRequest"')[1][:400]
    assert '"required": ["query"]' in block, (
        "в openapi_gen домен всё ещё обязателен — описания разошлись"
    )


async def _body_sent(args: dict) -> dict:
    """Зовём настоящий обработчик и перехватываем тело запроса к API.

    Проверять надо то, что уходит на сервер. Первая версия этой проверки
    повторяла нормализацию прямо в тесте — такая проверка зелена всегда, чем
    бы ни был занят обработчик.
    """
    from app.api import mcp_protocol as mp

    seen: dict = {}

    async def _fake_call_self(_request, _method, _path, json_body=None, **kw):
        seen.update(json_body or {})
        return {"ok": True}

    with patch.object(mp, "_call_self", _fake_call_self):
        await mp._dispatch_tool(MagicMock(), "cognitive_recall",
                                {"query": "что-нибудь", **args})
    return seen


@pytest.mark.parametrize("given,expected", [
    ({"domain": ""}, None),
    ({"domain": "   "}, None),
    ({}, None),
    ({"domain": "infra_lessons"}, "infra_lessons"),
    ({"domain": "  infra_lessons  "}, "infra_lessons"),
])
async def test_blank_domain_means_no_domain(given, expected):
    """Пустая строка — «домен не указан», а не домен с пустым именем.

    Раньше она уходила в поиск как есть: у соседей вызов висел 300 с и
    возвращал пустоту, что читалось как «памяти нет».
    """
    body = await _body_sent(given)
    assert body["domain"] == expected


async def test_query_still_reaches_the_api():
    """Нормализация домена не должна потерять сам запрос."""
    body = await _body_sent({"domain": "skills"})
    assert body["context"] == "что-нибудь"
