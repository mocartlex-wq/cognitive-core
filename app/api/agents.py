"""Agent state checkpoint API.

Per-agent persistence — recovery после срыва сессии / окончания токенов.
"""
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.db.postgres import get_pool
from app.security.auth import verify_api_key
from app.services.agent_state_service import (
    get_history,
    list_all_agents,
    restore_state,
    save_checkpoint,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/agents", tags=["agents"])


class CheckpointInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_task: str | None = Field(None, max_length=2000)
    state_data: dict | None = None
    active_session_ids: list[str] | None = None
    notes: str | None = Field(None, max_length=500)
    trigger: str = Field("manual", pattern="^(manual|auto|heartbeat|session_close|event_milestone)$")


@router.post("/{agent_id}/checkpoint")
async def post_checkpoint(agent_id: str, body: CheckpointInput, request: Request):
    """Сохранить checkpoint агента.

    Триггеры:
      - manual: явный вызов агентом (cognitive_save_state)
      - auto: периодический (каждые 5 мин)
      - heartbeat: keep-alive
      - session_close: после завершения OP-сессии
      - event_milestone: после важного действия

    После save — replication outbox (kind=agent_state) для server→local mirror.
    """
    await verify_api_key(request)
    try:
        result = await save_checkpoint(
            agent_id=agent_id,
            current_task=body.current_task,
            state_data=body.state_data,
            active_session_ids=body.active_session_ids,
            notes=body.notes,
            trigger=body.trigger,
        )
    except ValueError as e:
        raise HTTPException(status_code=413, detail=str(e))

    # Replication: server→local mirror via NATS
    try:
        from app.replication import write_outbox_event
        pool = await get_pool()
        async with pool.acquire() as conn:
            await write_outbox_event(
                conn,
                kind="agent_state",
                payload={
                    "agent_id": agent_id,
                    "current_task": body.current_task,
                    "state_data": body.state_data,
                    "active_session_ids": body.active_session_ids,
                    "notes": body.notes,
                    "trigger": body.trigger,
                    "saved_at": result.get("saved_at") if isinstance(result, dict) else None,
                },
            )
    except Exception as e:
        logger.warning("outbox write failed for agent_state %s: %s", agent_id, e)

    return result


async def _enforce_owns_agent(request: Request, agent_id: str) -> None:
    """PR #23 tenant isolation: проверяет что текущий caller владеет agent_id.

    Если owner_user_id из request не совпадает с agent_states.owner_user_id —
    выбрасывает HTTP 403. Legacy env-keys (owner=None) видят всё (admin mode).
    """
    from fastapi import HTTPException

    from app.db.postgres import get_pool
    from app.security.owner import resolve_owner_user_id

    caller_owner = await resolve_owner_user_id(request)
    if caller_owner is None:
        return  # admin/legacy env-key — bypass
    pool = await get_pool()
    async with pool.acquire() as conn:
        agent_owner = await conn.fetchval(
            "SELECT owner_user_id::text FROM agent_states WHERE agent_id = $1 LIMIT 1",
            agent_id,
        )
    if agent_owner is None:
        return  # legacy agent без owner — пропускаем (не наш)
    if str(agent_owner) != str(caller_owner):
        raise HTTPException(status_code=403, detail="agent not owned by caller")


@router.get("/{agent_id}/state")
async def get_state(
    agent_id: str,
    request: Request,
    recent_events: int = Query(10, ge=0, le=100),
    recent_knowledge: int = Query(5, ge=0, le=50),
):
    """Восстановить состояние агента (cognitive_continue).

    Возвращает: last checkpoint + recent_events + recent_knowledge
    из доменов где работал агент.
    """
    await verify_api_key(request)
    await _enforce_owns_agent(request, agent_id)
    return await restore_state(
        agent_id=agent_id,
        include_recent_events=recent_events,
        include_recent_knowledge=recent_knowledge,
    )


@router.get("/{agent_id}/history")
async def get_agent_history(
    agent_id: str,
    request: Request,
    limit: int = Query(20, ge=1, le=200),
):
    """История checkpoints — для отката или анализа эволюции state."""
    await verify_api_key(request)
    await _enforce_owns_agent(request, agent_id)
    return {
        "agent_id": agent_id,
        "items": await get_history(agent_id, limit),
    }


@router.get("")
async def list_agents(request: Request):
    """Все агенты с активным state — для дашборда."""
    await verify_api_key(request)
    items = await list_all_agents()
    return {"count": len(items), "items": items}
