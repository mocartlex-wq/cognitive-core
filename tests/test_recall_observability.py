"""Recall наблюдается: аудит + метрики, и ни то ни другое не роняет поиск.

До 17.08 про поиск по памяти система не помнила ничего. Слот `operative_query`
в CHECK таблицы аудита (`app/db/postgres.py:131-136`) существовал с самого
начала и не заполнялся никем; метрик recall не было вовсе; `feedback_record`
писал в Redis с TTL 24 часа и не читался никогда.

Следствие: на вопрос «какие записи памяти хоть раз пригодились» ответить было
нечем. Именно поэтому 59% недостижимых знаний нашлись только ручной проверкой,
а не сигналом «поиск отвечает пустотой».

Отдельно проверяем, что инструмент измерения не стал причиной отказа: ручка
`/operative/query` — горячий путь (порог p95 в `scripts/dogfood_check.py` — 2 с),
и до этой правки в ней не было ни одного try/except.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.operative import OperativeQuery


def _request():
    req = MagicMock()
    req.state = MagicMock()
    req.state.agent_id = "test-agent"
    return req


def _records(n: int) -> list[dict]:
    return [{"id": f"id-{i}", "record_type": "knowledge",
             "knowledge_type": "rule", "distance": 0.1} for i in range(n)]


async def _call(body: OperativeQuery, *, results: list[dict], path: str = "hybrid",
                audit=None, grouped: bool = False):
    """Зовём ручку с подменённым окружением, возвращаем перехваченный аудит."""
    from app.api import operative as api
    from app.services.operative import recall_path

    captured: dict = {}

    async def _fake_search(*a, **k):
        recall_path.set(path)
        return results

    async def _fake_audit(**kw):
        captured.update(kw)
        if audit == "boom":
            raise RuntimeError("аудит недоступен")

    with patch.object(api, "verify_api_key", AsyncMock()), \
         patch.object(api, "resolve_owner_user_id", AsyncMock(return_value="own-1")), \
         patch.object(api, "build_operative", _fake_search), \
         patch.object(api, "_search_default_domains", _fake_search), \
         patch.object(api, "create_session", AsyncMock(return_value={
             "session_id": "s1", "domain": body.domain or "all",
             "expires_in": 86400, "results": results})), \
         patch.object(api, "log_audit", _fake_audit):
        resp = await api.query_operative(body, _request(), grouped)
    return resp, captured


@pytest.mark.asyncio
async def test_query_is_recorded_in_audit():
    body = OperativeQuery(domain="infra_lessons", context="кодировки", top_k=5)
    resp, audit = await _call(body, results=_records(3))

    assert resp["session_id"] == "s1"
    assert audit["action"] == "operative_query", "слот в CHECK так и остался пустым"
    d = audit["details"]
    assert d["domain"] == "infra_lessons"
    assert d["results"] == 3 and d["empty"] is False
    assert d["path"] == "hybrid"
    assert "latency_ms" in d
    assert d["owner_user_id"] == "own-1", (
        "у l5_audit_log нет колонки владельца — без него в details агрегат "
        "по тенанту не собрать"
    )


@pytest.mark.asyncio
async def test_empty_result_is_marked():
    """Пустая выдача — главный симптом; она обязана быть отличима в логе."""
    body = OperativeQuery(domain="media_analysis", context="что-нибудь", top_k=5)
    _resp, audit = await _call(body, results=[])
    assert audit["details"]["results"] == 0
    assert audit["details"]["empty"] is True


@pytest.mark.asyncio
async def test_domainless_search_is_marked_multi():
    """Бездоменный путь идёт через asyncio.gather.

    Задача получает КОПИЮ контекста, поэтому recall_path, выставленный внутри,
    наверх не поднимается. Если бы мы этого не учли, в логе стояло бы
    «unknown», и это читалось бы как сбой, а не как устройство кода.
    """
    body = OperativeQuery(context="без домена", top_k=5)
    _resp, audit = await _call(body, results=_records(2), path="redis")
    assert audit["details"]["path"] == "multi"
    assert audit["details"]["domain"] == "*"


@pytest.mark.asyncio
async def test_audit_failure_does_not_break_search():
    """Инструмент измерения не имеет права стать причиной отказа."""
    body = OperativeQuery(domain="skills", context="навык", top_k=5)
    resp, _audit = await _call(body, results=_records(1), audit="boom")
    assert resp["session_id"] == "s1", (
        "падение записи в аудит уронило сам поиск — ровно то, чего "
        "инструментирование делать не должно"
    )


@pytest.mark.asyncio
async def test_metrics_failure_does_not_break_search():
    from app.api import operative as api

    body = OperativeQuery(domain="skills", context="навык", top_k=5)
    with patch.object(api, "track_recall", MagicMock(side_effect=RuntimeError("prom off"))):
        resp, _ = await _call(body, results=_records(1))
    assert resp["session_id"] == "s1"


@pytest.mark.asyncio
async def test_query_text_is_truncated():
    """В context попадает кусок переписки — в аудит он целиком не едет."""
    body = OperativeQuery(domain="work_journal", context="я" * 5000, top_k=5)
    _resp, audit = await _call(body, results=_records(1))
    assert len(audit["details"]["query"]) <= 200
