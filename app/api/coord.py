"""Coordination API — общий слой для агентов разных программ.

Закрывает четыре боли мульти-агентной работы, которые файловый почтовый ящик
решить не может (сформулированы сессией AI-CRM 2026-08-09):

  1. ПРОИСХОЖДЕНИЕ — кто именно написал: программа + конкретная сессия, а не
     «тип агента». Здесь это `holder`/`actor` в свободной форме
     `program:session` (напр. `claude-code:a7cf644a`, `codex-app:начало`).
  2. КВИТАНЦИИ — дошло / прочитано / в работе / сделано по каждому сообщению.
  3. ДЕДУПЛИКАЦИЯ — «это уже делали?» ДО начала работы: журнал выполненных
     работ с поиском, не дожидаясь ночной консолидации L1→L3.
  4. ВЛАДЕНИЕ ФАЙЛАМИ — атомарный лок на ресурс, чтобы два агента не правили
     один файл. Redis SET NX EX — единственный способ сделать это без гонки
     (файловый флаг в общей папке гонку не исключает).

Всё владельческое: ключи Redis и записи журнала неймспейсятся по owner_user_id,
поэтому агенты разных владельцев не видят и не блокируют друг друга.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.db.postgres import get_pool
from app.db.redis import get_redis
from app.security.auth import verify_api_key

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/coord", tags=["coordination"])

LOCK_MAX_TTL = 24 * 3600
JOURNAL_DOMAIN = "work_journal"


async def _owner_of(request: Request) -> str:
    """owner_user_id вызывающего агента — префикс изоляции для всех ключей."""
    agent_id = getattr(request.state, "agent_id", None) or "unknown"
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT owner_user_id::text AS o FROM agent_keys WHERE agent_id = $1 LIMIT 1",
                agent_id,
            )
            if row and row["o"]:
                return row["o"]
    except Exception as e:
        logger.info("coord: owner lookup failed agent=%s err=%s", agent_id, e)
    return f"agent:{agent_id}"


# ─────────────────────────── 4. Локи на ресурсы ───────────────────────────
class LockInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resource: str = Field(..., min_length=1, max_length=300,
                          description="что блокируем: путь к файлу, модуль, ветка")
    holder: str = Field(..., min_length=1, max_length=120,
                        description="кто держит: program:session, напр. codex-app:начало")
    ttl_seconds: int = Field(1800, ge=30, le=LOCK_MAX_TTL)
    note: str = Field("", max_length=500, description="зачем взят лок")


@router.post("/lock")
async def acquire_lock(body: LockInput, request: Request):
    """Взять лок на ресурс. Атомарно (Redis SET NX) — гонка невозможна.

    Если ресурс занят — 409 с описанием, КТО его держит и сколько осталось,
    чтобы агент сразу знал, к кому идти договариваться.
    Повторный вызов тем же holder продлевает лок (re-entrant).
    """
    await verify_api_key(request)
    owner = await _owner_of(request)
    key = f"coord:lock:{owner}:{body.resource}"
    payload = json.dumps({
        "holder": body.holder,
        "note": body.note,
        "acquired_at": time.time(),
        "agent_id": getattr(request.state, "agent_id", None),
    }, ensure_ascii=False)

    r = await get_redis()
    ok = await r.set(key, payload, ex=body.ttl_seconds, nx=True)
    if not ok:
        current = await r.get(key)
        ttl = await r.ttl(key)
        try:
            held = json.loads(current) if current else {}
        except Exception:
            held = {}
        if held.get("holder") == body.holder:  # свой же лок — продлеваем
            await r.set(key, payload, ex=body.ttl_seconds)
            return {"ok": True, "resource": body.resource, "renewed": True,
                    "ttl_seconds": body.ttl_seconds}
        raise HTTPException(status_code=409, detail={
            "error": "resource_locked",
            "resource": body.resource,
            "held_by": held.get("holder"),
            "note": held.get("note"),
            "expires_in_seconds": ttl,
        })
    return {"ok": True, "resource": body.resource, "holder": body.holder,
            "ttl_seconds": body.ttl_seconds}


@router.delete("/lock")
async def release_lock(request: Request, resource: str = Query(...), holder: str = Query(...)):
    """Снять свой лок. Чужой снять нельзя — только дождаться TTL."""
    await verify_api_key(request)
    owner = await _owner_of(request)
    key = f"coord:lock:{owner}:{resource}"
    r = await get_redis()
    current = await r.get(key)
    if not current:
        return {"ok": True, "released": False, "reason": "not_locked"}
    try:
        held = json.loads(current)
    except Exception:
        held = {}
    if held.get("holder") != holder:
        raise HTTPException(status_code=403, detail={
            "error": "not_your_lock", "held_by": held.get("holder")})
    await r.delete(key)
    return {"ok": True, "released": True, "resource": resource}


@router.get("/locks")
async def list_locks(request: Request):
    """Все активные локи владельца — «кто что сейчас правит»."""
    await verify_api_key(request)
    owner = await _owner_of(request)
    r = await get_redis()
    out: list[dict[str, Any]] = []
    prefix = f"coord:lock:{owner}:"
    async for key in r.scan_iter(match=f"{prefix}*", count=100):
        raw = await r.get(key)
        ttl = await r.ttl(key)
        try:
            held = json.loads(raw) if raw else {}
        except Exception:
            held = {}
        out.append({"resource": key[len(prefix):], "expires_in_seconds": ttl, **held})
    return {"count": len(out), "locks": out}


# ─────────────────────────── 2. Квитанции ───────────────────────────
class ReceiptInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: str = Field(..., min_length=1, max_length=100)
    actor: str = Field(..., min_length=1, max_length=120, description="program:session")
    status: str = Field(..., pattern="^(received|read|in_progress|done|rejected)$")
    detail: str = Field("", max_length=1000)


@router.post("/receipt")
async def post_receipt(body: ReceiptInput, request: Request):
    """Отметить статус обработки сообщения. Хранится 7 дней (Redis list)."""
    await verify_api_key(request)
    owner = await _owner_of(request)
    key = f"coord:receipt:{owner}:{body.message_id}"
    entry = json.dumps({
        "actor": body.actor, "status": body.status,
        "detail": body.detail, "at": time.time(),
    }, ensure_ascii=False)
    r = await get_redis()
    await r.rpush(key, entry)
    await r.expire(key, 7 * 24 * 3600)
    return {"ok": True, "message_id": body.message_id, "status": body.status}


@router.get("/receipts/{message_id}")
async def get_receipts(message_id: str, request: Request):
    """История статусов сообщения: кто получил, прочитал, взял в работу."""
    await verify_api_key(request)
    owner = await _owner_of(request)
    r = await get_redis()
    raw = await r.lrange(f"coord:receipt:{owner}:{message_id}", 0, -1)
    items = []
    for x in raw or []:
        try:
            items.append(json.loads(x))
        except Exception:
            pass
    return {"message_id": message_id, "count": len(items), "receipts": items}


# ─────────────────────── 1+3. Журнал работ и дедуп ───────────────────────
class JournalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: str = Field(..., min_length=3, max_length=500, description="что сделано")
    actor: str = Field(..., min_length=1, max_length=120, description="program:session")
    result: str = Field("", max_length=2000)
    files: list[str] = Field(default_factory=list, description="затронутые файлы")
    tags: list[str] = Field(default_factory=list)


@router.post("/journal")
async def add_journal(body: JournalInput, request: Request):
    """Записать выполненную работу. Пишется в L1 (domain=work_journal) —
    попадёт и в обычную консолидацию, но доступна для поиска СРАЗУ."""
    await verify_api_key(request)
    from app.services.ingestor import save_raw_event
    agent_id = getattr(request.state, "agent_id", None) or "unknown"
    event_id = await save_raw_event(
        agent_id=agent_id,
        domain=JOURNAL_DOMAIN,
        payload={
            "task": body.task, "actor": body.actor, "result": body.result,
            "files": body.files, "tags": body.tags,
        },
    )
    return {"ok": True, "event_id": str(event_id)}


@router.get("/journal/search")
async def search_journal(
    request: Request,
    q: str = Query(..., min_length=2, description="что ищем: «уже делали X?»"),
    days: int = Query(90, ge=1, le=365),
    limit: int = Query(10, ge=1, le=50),
):
    """«Это уже делали?» — поиск по журналу ДО начала работы.

    Ищет по свежему L1 напрямую (не дожидаясь ночной консолидации в L3).

    МОРФОЛОГИЯ (правка по замечанию сессии AI-CRM 2026-08-09): человек ищет
    своими словами, а не цитатой. Буквальный ILIKE находил запись по точной
    подстроке, но не по «копирование воронки перенос полей» ↔ «копирование
    воронок (настройки + поля)» — расходятся словоформы. Русский snowball
    тоже не спасает: «копирование»→«копирован» стеммится, а «воронок»→
    «воронки» нет (беглая гласная).

    Поэтому пословное сходство pg_trgm: каждое значимое слово запроса ищется
    через word_similarity() по тексту записи; запись считается найденной,
    если совпала половина слов (минимум одно). На разобранном кейсе:
    воронки 0.63, копирование 1.0, полей 0.5 — три из четырёх, лишнее
    «перенос» 0.13 корректно отсеивается. Точный ILIKE остаётся как
    OR-ветка, чтобы не потерять поиск по id/пути/цитате.
    """
    await verify_api_key(request)
    agent_id = getattr(request.state, "agent_id", None) or "unknown"

    stop = {"для", "как", "что", "это", "или", "при", "над", "под", "the", "and", "for"}
    words = [w.strip(".,;:()[]«»\"'") for w in q.lower().split()]
    words = [w for w in words if len(w) >= 3 and w not in stop][:12]
    need = max(1, (len(words) + 1) // 2)  # половина слов, округляя вверх

    pool = await get_pool()
    async with pool.acquire() as conn:
        owner_row = await conn.fetchrow(
            "SELECT owner_user_id FROM agent_keys WHERE agent_id = $1 LIMIT 1", agent_id
        )
        owner_uid = owner_row["owner_user_id"] if owner_row else None
        sql = """
            SELECT id::text, timestamp, source_agent, raw_payload,
                   (SELECT count(*) FROM unnest($4::text[]) AS w
                     WHERE word_similarity(w, lower(raw_payload::text)) >= 0.4) AS hits
              FROM l1_raw_events
             WHERE domain = $1
               AND timestamp > NOW() - ($2 || ' days')::INTERVAL
               AND (
                    raw_payload::text ILIKE '%' || $3 || '%'
                    OR (SELECT count(*) FROM unnest($4::text[]) AS w
                         WHERE word_similarity(w, lower(raw_payload::text)) >= 0.4) >= $5
               )
        """
        args: list[Any] = [JOURNAL_DOMAIN, str(days), q, words or [q.lower()], need]
        if owner_uid is not None:
            sql += " AND owner_user_id = $6"
            args.append(owner_uid)
        sql += " ORDER BY hits DESC, timestamp DESC LIMIT 50"
        rows = await conn.fetch(sql, *args)

    items = []
    for r0 in rows[:limit]:
        try:
            p = json.loads(r0["raw_payload"]) if isinstance(r0["raw_payload"], str) else dict(r0["raw_payload"])
        except Exception:
            p = {}
        items.append({
            "id": r0["id"],
            "at": r0["timestamp"].isoformat() if r0["timestamp"] else None,
            "by": r0["source_agent"],
            "task": p.get("task"), "actor": p.get("actor"),
            "result": (p.get("result") or "")[:300], "files": p.get("files", []),
            "matched_words": r0["hits"], "of_words": len(words),
        })
    return {
        "query": q, "found": len(items),
        "already_done": bool(items),  # прямой ответ на «делали ли уже?»
        "items": items,
    }
