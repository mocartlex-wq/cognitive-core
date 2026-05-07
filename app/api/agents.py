"""Agent state checkpoint API.

Per-agent persistence — recovery после срыва сессии / окончания токенов.
"""
import logging
from fastapi import APIRouter, Request, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from app.security.auth import verify_api_key
from app.services.agent_state_service import (
    save_checkpoint, restore_state, get_history, list_all_agents,
)
from app.db.postgres import get_pool

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
