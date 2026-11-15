"""Regression scenarios against PostgreSQL, isolated schema, no LLM/network mocks."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

asyncpg = pytest.importorskip("asyncpg")
from app.models.event import validate_checkpoint
from app.services import ingestor
from app.services.memory_records import read_record, search_records

DB_URL = os.getenv("COGCORE_TEST_DB_URL") or os.getenv("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="PostgreSQL integration environment required")


@pytest.fixture
async def db():
    schema = "qa_memory_" + uuid4().hex
    admin = await asyncpg.connect(DB_URL)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=4, server_settings={"search_path": schema})
    async with pool.acquire() as conn:
        await conn.execute("""CREATE TABLE l1_raw_events (
          id UUID PRIMARY KEY, source_agent TEXT NOT NULL, owner_user_id UUID,
          domain TEXT NOT NULL, raw_payload JSONB NOT NULL,
          timestamp TIMESTAMPTZ NOT NULL DEFAULT now(), created_at TIMESTAMPTZ DEFAULT now())""")
    try:
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def put(db, owner, payload, *, domain="ai_crm", age=0, agent="producer"):
    rid = uuid4()
    async with db.acquire() as conn:
        await conn.execute("INSERT INTO l1_raw_events (id, source_agent, owner_user_id, domain, raw_payload, timestamp) "
                           "VALUES ($1,$2,$3::uuid,$4,$5::jsonb,$6)", rid, agent, owner, domain,
                           json.dumps(payload, ensure_ascii=False), datetime.now(timezone.utc) - timedelta(days=age))
    return str(rid)


def point(status="in_progress", *, task_id="word-editor", observed=None, supersedes=None):
    return validate_checkpoint({"task": "Редактор Word", "result": "Блокировка Enter ещё не исправлена.", "checkpoint": {
        "task_id": task_id, "session_id": "session-producer", "status": status,
        "observed_at": (observed or datetime.now(timezone.utc)).isoformat(),
        "next_step": "Проверить локальный ввод в браузере", "evidence": ["evidence://word/owner-video"],
        "workspace": "D:/work/word", "supersedes": supersedes,
    }})


@pytest.mark.asyncio
async def test_natural_question_finds_reopened_legacy_head_and_keeps_other_tasks(db):
    owner = str(uuid4())
    old = await put(db, owner, {"task": "CHECKPOINT ai_crm [codex/word@aaa]",
                                "result": "Word редактор готов, принят владельцем. Проверены изменения."}, age=2)
    latest = await put(db, owner, {"task": "CHECKPOINT ai_crm [codex/word@bbb]",
                                   "result": "Не принят: Enter блокируется. Дальше: локальный ввод. Доказательство video-15."}, age=1)
    cloud = await put(db, owner, {"task": "CHECKPOINT ai_crm [codex/cloud@ccc]",
                                  "result": "Облачная синхронизация выпущена. KATYA offline."})
    await put(db, str(uuid4()), {"task": "Word редактор", "result": "Чужой владелец"})
    got = await search_records(db, "Где остановились работы над Word и принят ли редактор владельцем?", owner, "ai_crm")
    assert got[0]["id"] == latest
    assert old not in [x["id"] for x in got]
    assert all(x["content"].get("result") != "Чужой владелец" for x in got)
    history = await read_record(db, old, owner)
    assert history["superseded_by"] == latest and not history["is_latest"]
    assert (await read_record(db, cloud, owner))["is_latest"]


@pytest.mark.asyncio
async def test_kimi_morphology_unknown_and_cross_owner(db):
    owner, other = str(uuid4()), str(uuid4())
    kimi = await put(db, owner, {"task": "Kimi: Перезвонить ответственному", "result": "Тестирование не закончено, браузерная проверка осталась."})
    await put(db, owner, {"task": "Что сделано с Word", "result": "Нерелевантное старое совпадение по что"})
    got = await search_records(db, "Что осталось Kimi по кнопке Перезвонить ответственному?", owner)
    assert got[0]["id"] == kimi
    assert not got[0].get("task_state"), "A summary is not a verified task checkpoint"
    assert (await search_records(db, "тестированием Kimi", owner))[0]["id"] == kimi
    assert await search_records(db, "НесуществующийЖуравль987", owner) == []
    assert await search_records(db, "Где остановились с проверкой НесуществующийЖуравль987, что дальше и где подтверждение?", owner) == []
    assert await read_record(db, kimi, other) is None
    assert await search_records(db, "Kimi", None) == []


@pytest.mark.asyncio
async def test_checkpoint_survives_window_and_full_source_not_truncated(db):
    owner = str(uuid4())
    payload = point(observed=datetime.now(timezone.utc) - timedelta(days=80))
    payload["result"] = "Длинное доказательство " * 180 + "следующий шаг в конце"
    rid = await put(db, owner, payload, age=80)
    got = await search_records(db, "Что дальше с редактором Word?", owner)
    assert got[0]["id"] == rid and got[0]["truncated"]
    assert got[0]["checkpoint"]["next_step"] == payload["checkpoint"]["next_step"]
    full = await read_record(db, rid, owner)
    assert {**full["content"], "checkpoint": full["checkpoint"]} == payload
    assert "payload" not in full, "Do not duplicate the full source in the tool response"
    assert full["content"]["result"].endswith("следующий шаг в конце")
    # Restore an explicit workspace, without magic CHECKPOINT/domain words.
    other = point(task_id="other-workspace")
    other["checkpoint"]["workspace"] = "D:/work/other"
    await put(db, owner, other)
    resumed = await search_records(db, "", owner, workspace="D:\\work\\word", limit=1)
    assert resumed[0]["id"] == rid


@pytest.mark.asyncio
async def test_idempotency_ack_loss_concurrent_retries_and_payload_conflict(db):
    import asyncio
    owner = str(uuid4())
    payload = {"task": "Kimi callback", "result": "Дальше проверить браузер"}
    with patch.object(ingestor, "get_pool", AsyncMock(return_value=db)):
        receipts = await asyncio.gather(*[
            ingestor.save_idempotent_event("producer", "ai_crm", payload, owner, "one-delivery") for _ in range(4)
        ])
        assert len({r[0] for r in receipts}) == 1
        assert sum(r[2] for r in receipts) == 1
        with pytest.raises(ValueError, match="different content"):
            await ingestor.save_idempotent_event("producer", "ai_crm", {"task": "changed"}, owner, "one-delivery")
        # Same key on another authenticated account is a separate event.
        other = await ingestor.save_idempotent_event("producer", "ai_crm", payload, str(uuid4()), "one-delivery")
        assert other[0] != receipts[0][0]


@pytest.mark.asyncio
async def test_explicit_predecessor_prevents_lost_update_and_delayed_note_override(db):
    owner = str(uuid4())
    before = datetime.now(timezone.utc) - timedelta(days=1)
    first = point("completed", observed=before)
    with patch.object(ingestor, "get_pool", AsyncMock(return_value=db)):
        old, _, _ = await ingestor.save_idempotent_event("producer", "ai_crm", first, owner, "first")
        reopened = point("blocked", supersedes=str(old))
        new, _, _ = await ingestor.save_idempotent_event("reviewer", "ai_crm", reopened, owner, "reopen")
        # An ack-lost retry of the first point must remain harmless after reopen.
        assert not (await ingestor.save_idempotent_event("producer", "ai_crm", first, owner, "first"))[2]
        with pytest.raises(ValueError, match="Checkpoint conflict"):
            await ingestor.save_idempotent_event("producer", "ai_crm", point(supersedes=str(old)), owner, "stale-writer")
    await put(db, owner, {"task": "Word session ended", "kind": "session_note", "observed_at": before.isoformat(), "result": "старый отчёт готово"})
    got = await search_records(db, "Где остановились работы над Word?", owner)
    heads = [r for r in got if r.get("task_state")]
    assert heads[0]["id"] == str(new)
    assert heads[0]["checkpoint"]["status"] == "blocked"
    assert (await read_record(db, str(old), owner))["latest_id"] == str(new)
