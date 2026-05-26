from fastapi import APIRouter, Query, Request

from app.models.operative import OperativeClose, OperativeFeedback, OperativeQuery
from app.security.auth import verify_api_key
from app.security.owner import resolve_owner_user_id
from app.services.operative import (
    build_operative,
    close_session,
    create_session,
    feedback_record,
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
