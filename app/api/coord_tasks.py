"""Authenticated task operations for browser sessions and agents of one owner."""
from __future__ import annotations

from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request

from app.db.postgres import get_pool
from app.models.coordination import TaskAction, TaskCreate
from app.security.auth import verify_api_key
from app.security.middleware import optional_user
from app.security.owner import resolve_owner_user_id
from app.services import task_ledger

router = APIRouter(prefix="/coord/tasks", tags=["coordination-tasks"])


async def identity(request: Request) -> tuple[str, str, bool]:
    user = await optional_user(request)
    if user:
        if request.method != "GET" and request.cookies:
            origin = request.headers.get("origin", "")
            if not origin or urlsplit(origin).netloc != request.headers.get("host"):
                raise HTTPException(403, "same_origin_required")
        return str(user.user_id), "user:" + str(user.user_id), True
    await verify_api_key(request)
    owner = await resolve_owner_user_id(request)
    if not owner:
        raise HTTPException(403, "Ключ должен быть привязан к владельцу очереди")
    return owner, request.state.agent_id, False


@router.get("")
async def tasks(request: Request, project: str | None = Query(None, max_length=160), limit: int = Query(200, ge=1, le=500),
                status: str | None = Query(None, pattern="^(queued|running|blocked|review|verified_done|failed|cancelled)$"),
                cursor: str | None = Query(None, max_length=1000)):
    owner, _, _ = await identity(request)
    return await task_ledger.list_tasks(await get_pool(), owner, project, limit, status, cursor)


@router.post("", status_code=201)
async def create(body: TaskCreate, request: Request):
    owner, agent, _ = await identity(request)
    return await task_ledger.create_task(await get_pool(), owner, {"agent_id": agent, "session_id": body.session_id}, body)


@router.get("/{task_id}")
async def task(task_id: UUID, request: Request):
    owner, _, _ = await identity(request)
    return await task_ledger.read_task(await get_pool(), owner, task_id)


@router.get("/{task_id}/history")
async def task_history(task_id: UUID, request: Request, limit: int = Query(200, ge=1, le=200), before_version: int | None = Query(None, ge=1)):
    owner, _, _ = await identity(request)
    return await task_ledger.history(await get_pool(), owner, task_id, limit, before_version)


@router.post("/{task_id}/actions")
async def action(task_id: UUID, body: TaskAction, request: Request):
    owner, agent, is_user = await identity(request)
    return await task_ledger.act(await get_pool(), owner, task_id, {"agent_id": agent, "session_id": body.session_id}, body, account_user=is_user)
