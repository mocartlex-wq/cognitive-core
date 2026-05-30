from fastapi import APIRouter, HTTPException, Query, Request

from app.models.operative import OperativeClose, OperativeFeedback, OperativeQuery, OperativeRecallUI
from app.security.auth import verify_api_key
from app.security.owner import resolve_owner_user_id
from app.services.operative import (
    build_operative,
    close_session,
    create_session,
    feedback_record,
    recall_any_domain,
)

router = APIRouter(prefix="/operative", tags=["operative"])


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

    results = await build_operative(
        query=body.context or body.domain,
        domain=body.domain,
        top_k=body.top_k,
        include_tools=body.include_tools,
        owner_user_id=owner_user_id,
    )

    session = await create_session(body.domain, results)

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
