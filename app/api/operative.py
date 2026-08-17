import asyncio
import logging
import time

from fastapi import APIRouter, HTTPException, Query, Request

from app.config import settings
from app.models.operative import OperativeClose, OperativeFeedback, OperativeQuery, OperativeRecallUI
from app.security.audit import log_audit
from app.security.auth import verify_api_key
from app.security.owner import resolve_owner_user_id
from app.services.metrics import track_recall
from app.services.operative import (
    build_operative,
    close_session,
    create_session,
    feedback_record,
    recall_any_domain,
    recall_path,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/operative", tags=["operative"])


async def _observe_recall(*, request: Request, body: OperativeQuery, results: list,
                          duration: float, owner_user_id: str | None,
                          grouped: bool) -> None:
    """Запись факта поиска: аудит + метрики.

    Слот `operative_query` в CHECK таблицы аудита существовал с самого начала
    (`app/db/postgres.py:131-136`) и не заполнялся никем. Поэтому на вопрос
    «какие записи памяти хоть раз пригодились» ответить было нечем: обращений
    к recall система не помнила вовсе.

    ⚠️ Строго best-effort. Это горячий путь: в `scripts/dogfood_check.py`
    порог p95 для `/operative/query` — 2 секунды, а до этой правки в ручке не
    было ни одного try/except. Падение записи наблюдения не имеет права ронять
    сам поиск — иначе инструмент измерения станет причиной отказа.
    """
    path = recall_path.get()
    count = len(results)
    try:
        track_recall(path, duration, count, owner_scoped=owner_user_id is not None)
    except Exception as e:  # pragma: no cover — метрика не должна ломать поиск
        log.warning("recall metrics failed: %s", e)

    try:
        agent_id = getattr(getattr(request, "state", None), "agent_id", "") or ""
        await log_audit(
            agent_id=agent_id,
            action="operative_query",
            target_table="l3_master_knowledge",
            details={
                # Сам запрос обрезаем: в контексте бывает кусок переписки.
                "query": (body.context or "")[:200],
                "domain": body.domain or "*",
                "top_k": body.top_k,
                "include_tools": body.include_tools,
                "grouped": grouped,
                "path": path,
                "results": count,
                "empty": count == 0,
                "latency_ms": int(duration * 1000),
                # У l5_audit_log нет колонки owner_user_id — кладём в details,
                # иначе агрегат «по тенанту» не собрать.
                "owner_user_id": owner_user_id,
            },
            success=True,
        )
    except Exception as e:  # pragma: no cover
        log.warning("recall audit failed: %s", e)


async def _search_default_domains(
    query: str,
    top_k: int,
    include_tools: bool,
    owner_user_id: str | None,
) -> list[dict]:
    """Поиск без указания домена: по доменам знаний, параллельно, с дедупом.

    Домены опрашиваются одновременно — последовательный обход пяти доменов
    складывал бы латентности и делал бы «поиск без домена» заметно дороже
    обычного. Сбой одного домена не рушит выдачу: он просто не даёт результатов.
    """
    domains = settings.recall_default_domains
    if not domains:
        return []
    per_domain = max(2, top_k)
    tasks = [
        build_operative(
            query=query or dom,
            domain=dom,
            top_k=per_domain,
            include_tools=include_tools,
            owner_user_id=owner_user_id,
        )
        for dom in domains
    ]
    chunks = await asyncio.gather(*tasks, return_exceptions=True)

    merged: list[dict] = []
    seen: set[str] = set()
    for chunk in chunks:
        if isinstance(chunk, Exception):
            continue
        for item in chunk:
            key = str(item.get("id"))
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)

    # Сортировка по релевантности: у путей поиска исторически разная семантика
    # поля distance (у одного это расстояние, у другого — сходство), поэтому
    # опираемся на confidence, который считается одинаково везде.
    merged.sort(key=lambda r: r.get("confidence") or 0.0, reverse=True)
    return merged[:top_k]


def _group_results_for_agent(results: list[dict]) -> dict:
    """Группирует плоский список results в семантические разделы для удобства агента.

    Возвращает структуру:
      {
        patterns: [...],       # знания типа "так делать"
        mistakes: [...],       # знания типа "так не делать"
        rules: [...],          # знания типа "правило"
        tools: [...],          # инструменты в реестре
        all: [...]             # оригинальный плоский список (на всякий случай)
      }
    """
    grouped = {"patterns": [], "mistakes": [], "rules": [], "tools": [], "all": results}
    for r in results:
        rtype = r.get("record_type", "")
        if rtype == "tool":
            grouped["tools"].append(r)
        elif rtype == "knowledge":
            ktype = r.get("knowledge_type", "")
            if ktype == "pattern":
                grouped["patterns"].append(r)
            elif ktype == "mistake":
                grouped["mistakes"].append(r)
            elif ktype == "rule":
                grouped["rules"].append(r)
            else:
                # Без явного типа → в patterns по умолчанию
                grouped["patterns"].append(r)
    return grouped


@router.post("/query")
async def query_operative(
    body: OperativeQuery,
    request: Request,
    grouped: bool = Query(False, description="Если true — вернуть результаты по семантическим разделам (patterns/mistakes/rules/tools) для удобства агента"),
):
    """KNN-поиск по L3 + создание OP-сессии.

    По умолчанию возвращает плоский список results (backward-compat).
    С ?grouped=true возвращает структурированный пакет:
      session_id, domain, expires_in, frame: {patterns:[], mistakes:[], rules:[], tools:[], all:[]}
    """
    await verify_api_key(request)
    owner_user_id = await resolve_owner_user_id(request)

    recall_path.set("unknown")
    _t0 = time.monotonic()

    # Домен не задан — ищем по доменам знаний и склеиваем. Раньше домен был
    # обязателен: агент, промахнувшийся с одним из 18 доменов, получал пустой
    # ответ, неотличимый от «в памяти ничего нет».
    if body.domain:
        results = await build_operative(
            query=body.context or body.domain,
            domain=body.domain,
            top_k=body.top_k,
            include_tools=body.include_tools,
            owner_user_id=owner_user_id,
        )
    else:
        results = await _search_default_domains(
            query=body.context or "",
            top_k=body.top_k,
            include_tools=body.include_tools,
            owner_user_id=owner_user_id,
        )
        # Домены идут через asyncio.gather — задача получает КОПИЮ контекста,
        # и recall_path, выставленный внутри, сюда не поднимется. Помечаем
        # честно, а не оставляем "unknown", который читался бы как сбой.
        recall_path.set("multi")

    await _observe_recall(
        request=request,
        body=body,
        results=results,
        duration=time.monotonic() - _t0,
        owner_user_id=owner_user_id,
        grouped=grouped,
    )

    session = await create_session(body.domain or "all", results)

    if grouped:
        # Заменяем плоский results на семантический frame
        return {
            "session_id": session["session_id"],
            "domain": session["domain"],
            "expires_in": session["expires_in"],
            "frame": _group_results_for_agent(results),
            "counts": {
                "patterns": sum(1 for r in results if r.get("knowledge_type") == "pattern"),
                "mistakes": sum(1 for r in results if r.get("knowledge_type") == "mistake"),
                "rules": sum(1 for r in results if r.get("knowledge_type") == "rule"),
                "tools": sum(1 for r in results if r.get("record_type") == "tool"),
                "total": len(results),
            },
        }

    return session


@router.post("/recall_ui")
async def recall_ui(body: OperativeRecallUI, request: Request):
    """Session-cookie-authed recall for the in-product assistant (no API key).

    Owner is taken STRICTLY from the validated cogcore_session cookie via
    verify_session. The spoofable X-Owner-User-Id header is deliberately
    not consulted here, so a caller can only ever read its own memory.
    """
    from app.security.session import SESSION_COOKIE_NAME, verify_session
    sid = request.cookies.get(SESSION_COOKIE_NAME) or request.headers.get("X-Session-Id")
    session = await verify_session(sid)
    owner_user_id = session.user_id if session else None
    if not owner_user_id:
        raise HTTPException(status_code=401, detail="session required")
    top_k = body.top_k if isinstance(body.top_k, int) else 5
    top_k = max(1, min(top_k, 8))
    rows = await recall_any_domain(
        query=body.context or "",
        top_k=top_k,
        owner_user_id=owner_user_id,
    )
    return {"results": rows, "count": len(rows)}


@router.post("/recall_internal")
async def recall_internal(body: OperativeRecallUI, request: Request):
    """Server-to-server recall for trusted internal callers (e.g. the orchestrator).

    Auth: a valid agent API key (verify_api_key) PLUS an explicit X-Owner-User-Id
    header naming whose memory to read. This is the same internal-trust header the
    MCP dispatcher already uses; it is only reachable on the internal docker network
    (never exposed publicly via nginx), so the caller must already hold an agent key.
    """
    await verify_api_key(request)
    owner_user_id = request.headers.get("x-owner-user-id")
    if not owner_user_id:
        raise HTTPException(status_code=400, detail="X-Owner-User-Id required")
    top_k = body.top_k if isinstance(body.top_k, int) else 5
    top_k = max(1, min(top_k, 8))
    rows = await recall_any_domain(
        query=body.context or "",
        top_k=top_k,
        owner_user_id=owner_user_id,
    )
    return {"results": rows, "count": len(rows)}


@router.post("/sessions/{session_id}/close")
async def close_operative_session(session_id: str, body: OperativeClose, request: Request):
    """Закрытие OP-сессии с опциональной обратной связью."""
    await verify_api_key(request)

    from uuid import UUID
    result = await close_session(
        session_id=UUID(session_id),
        keep_results=body.keep_results,
        results_summary=body.results_summary,
        source_agent=body.source_agent,
    )
    return result


@router.post("/sessions/{session_id}/feedback")
async def record_feedback(session_id: str, body: OperativeFeedback, request: Request):
    """Обратная связь по записи в OP-сессии."""
    await verify_api_key(request)

    from uuid import UUID
    result = await feedback_record(
        session_id=UUID(session_id),
        record_id=body.record_id,
        record_type=body.record_type,
        useful=body.useful,
    )
    return result
