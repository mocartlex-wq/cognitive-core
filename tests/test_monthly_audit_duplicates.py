"""Ежемесячный аудит: duplicate_pairs от аудитора теперь применяются.

Раньше код снимал только stale_knowledge_ids и dead_tool_ids, а пары дублей
оставались в отчёте: 04.11.2026 cognitive_core после повторного weekly —
8 пар на 67 записей, health 0.62, и ничего не менялось. Решение, кого снять,
принимают данные (confidence, возраст), а не модель; чужие/выдуманные id,
инструменты и цепочки пар не роняют лишнего.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import consolidator
from app.services.consolidator import _pick_duplicate_losers

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


def _k(conf: float, days: int = 0, kid: uuid.UUID | None = None, content_as_str: bool = False):
    kid = kid or uuid.uuid4()
    content = {"confidence": conf, "description": "x"}
    return {"id": kid, "domain": "d", "knowledge_type": "rule",
            "content": __import__("json").dumps(content) if content_as_str else content,
            "effective_from": T0 + timedelta(days=days)}


class TestPickLosers:
    def test_lower_confidence_loses(self):
        hi, lo = _k(0.9), _k(0.7)
        assert _pick_duplicate_losers([hi, lo], [[str(hi["id"]), str(lo["id"])]]) == [lo["id"]]
        # порядок в паре не важен
        assert _pick_duplicate_losers([hi, lo], [[str(lo["id"]), str(hi["id"])]]) == [lo["id"]]

    def test_equal_confidence_keeps_older(self):
        old, new = _k(0.8, days=0), _k(0.8, days=3)
        assert _pick_duplicate_losers([old, new], [[str(new["id"]), str(old["id"])]]) == [new["id"]]

    def test_content_may_be_json_string(self):
        hi, lo = _k(0.9, content_as_str=True), _k(0.6, content_as_str=True)
        assert _pick_duplicate_losers([hi, lo], [[str(hi["id"]), str(lo["id"])]]) == [lo["id"]]

    def test_dict_form_with_ids_key(self):
        hi, lo = _k(0.9), _k(0.5)
        pairs = [{"ids": [str(hi["id"]), str(lo["id"])], "type": "duplicate_rule"}]
        assert _pick_duplicate_losers([hi, lo], pairs) == [lo["id"]]

    def test_foreign_tool_or_invented_ids_are_skipped(self):
        k = _k(0.9)
        tool_id, bogus = str(uuid.uuid4()), "not-a-uuid"
        pairs = [[str(k["id"]), tool_id], [str(k["id"]), bogus], [str(k["id"]), str(k["id"])], ["a"], None]
        assert _pick_duplicate_losers([k], pairs) == []

    def test_chain_does_not_retire_everything(self):
        # a~b, b~c: b проигрывает a, затем c сравнивается не с b (его уже нет),
        # а пара b~c пропускается — c остаётся.
        a, b, c = _k(0.9), _k(0.8), _k(0.7)
        pairs = [[str(a["id"]), str(b["id"])], [str(b["id"]), str(c["id"])]]
        assert _pick_duplicate_losers([a, b, c], pairs) == [b["id"]]


@pytest.mark.asyncio
async def test_run_monthly_audit_retires_duplicate_losers_in_domain_only():
    hi, lo = _k(0.9), _k(0.6)
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=[[hi, lo], []])
    conn.execute = AsyncMock()

    class _Ctx:
        async def __aenter__(self_inner):
            return conn

        async def __aexit__(self_inner, *a):
            return False

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Ctx())
    audit = {"stale_knowledge_ids": [], "dead_tool_ids": [], "internal_conflicts": [],
             "duplicate_pairs": [[str(hi["id"]), str(lo["id"])]], "health_score": 0.7}
    with (
        patch.object(consolidator, "get_pool", new=AsyncMock(return_value=pool)),
        patch.object(consolidator, "monthly_audit", new=AsyncMock(return_value=audit)),
        patch.object(consolidator, "index_domain_vectors", new=AsyncMock(return_value={"total": 1})),
        patch("app.services.operative._remove_indexed_vectors", new=AsyncMock()) as rm,
    ):
        out = await consolidator.run_monthly_audit("d")

    assert out["duplicates_retired"] == [str(lo["id"])]
    sql, now, kid, dom = conn.execute.await_args_list[0].args
    assert "effective_to IS NULL" in sql and "domain = $3" in sql
    assert kid == lo["id"] and dom == "d"
    rm.assert_awaited_once_with([str(lo["id"])])
