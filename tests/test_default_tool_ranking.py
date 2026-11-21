from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api import operative as api
from app.services import operative as svc


@pytest.mark.asyncio
async def test_semantic_and_exact_matches_survive_competing_domains(monkeypatch):
    tool = {"id": "meter", "record_type": "tool", "cosine_similarity": .9,
            "usage": {"command": "measure"}, "distance": .1}
    exact = {"id": "error-code", "record_type": "knowledge", "fts_rank": .95}
    weak = [{"id": f"rule-{n}", "record_type": "knowledge", "confidence": 1,
             "cosine_similarity": .3 - n / 100, "fts_rank": .2 - n / 100} for n in range(6)]
    monkeypatch.setattr(api.settings, "recall_default_domains", ["broken", "rules", "catalog", "catalog"])

    async def search(*, query, domain, top_k, include_tools, owner_user_id):
        assert owner_user_id == "owner-a"
        if domain == "broken":
            raise TimeoutError()
        return [exact, *weak] if domain == "rules" else [tool]

    monkeypatch.setattr(api, "build_operative", search)
    rows = await api._search_default_domains("Can we identify this sample?", 5, True, "owner-a")
    assert len({r["id"] for r in rows}) == len(rows) == 5
    assert {tool["id"], exact["id"]} <= {r["id"] for r in rows}
    assert api._group_results_for_agent(rows)["tools"][0]["usage"] == {"command": "measure"}
    hidden = await api._search_default_domains("Can we identify this sample?", 5, False, "owner-a")
    assert len(hidden) == 5 and not api._group_results_for_agent(hidden)["tools"]
    initial = await api._search_default_domains("", 1, True, "owner-a")
    assert initial[0]["confidence"] == 1


@pytest.mark.asyncio
async def test_redis_supplies_real_cosine_distance_and_filters_before_knn(monkeypatch):
    redis = AsyncMock()
    redis.execute_command.return_value = [
        1, b"op:42", [b"id", b"42", b"record_type", b"knowledge", b"__embedding_score", b"0.75"],
    ]
    monkeypatch.setattr(svc, "get_redis_raw", AsyncMock(return_value=redis))
    hits = await svc._search_redis_vector([1., 0.], "manuals", 4, include_tools=False)
    args = redis.execute_command.call_args.args
    assert "@record_type:{knowledge}" in args[2]
    assert "__embedding_score" in args[args.index("RETURN") + 2:]
    assert hits == [{"id": "42", "record_type": "knowledge", "distance": .75}]


@pytest.mark.asyncio
async def test_python_fallback_exposes_similarity_without_inverting_it(monkeypatch):
    conn = AsyncMock()
    conn.fetch.side_effect = [
        [{"id": "rule", "domain": "lab", "content": {"text": "policy"}}],
        [{"id": "meter", "domain": "lab", "tool_name": "meter", "description": "measure spectra"}],
    ]
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    monkeypatch.setattr(svc, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(svc, "search_pgvector", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc, "search_fulltext", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc, "_index_l3_vector", AsyncMock())
    monkeypatch.setattr(svc, "embed_text",
                        AsyncMock(side_effect=lambda text: [0., 1.] if "policy" in text else [1., 0.]))
    rows = await svc.build_operative("measure", "lab", owner_user_id="owner-a")
    assert [(r["id"], r["cosine_similarity"]) for r in rows] == [("meter", 1.), ("rule", 0.)]
