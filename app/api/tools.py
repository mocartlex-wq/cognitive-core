from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from app.models.tools import ToolRegistryInput
from app.security.auth import verify_api_key
from app.security.owner import resolve_owner_user_id
from app.services.shared_tools import agent_registry_record, read_shared_tool
from app.services.tools import OwnerManagedToolConflict, deprecate_tool, get_active_tools, register_tool

router = APIRouter(prefix="/tools", tags=["tools"])


async def _authorize(request: Request) -> str | None:
    """Ключ проверен + владелец резолвлен.

    До этого ручки проверяли только ключ: запись уходила без владельца,
    чтение возвращало чужой реестр, удаление снимало чужой инструмент по id.
    Тот же приём, что в `app/api/dashboard.py`.
    """
    await verify_api_key(request)
    return await resolve_owner_user_id(request)


@router.post("")
async def create_tool(body: ToolRegistryInput, request: Request):
    """Зарегистрировать новый инструмент в L3."""
    owner = await _authorize(request)
    try:
        tool_id = await register_tool(body, owner_user_id=owner)
    except OwnerManagedToolConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"status": "registered", "id": str(tool_id)}


@router.get("")
async def list_tools(domain: str, request: Request):
    """Список активных инструментов домена."""
    owner = await _authorize(request)
    tools = await get_active_tools(domain, owner_user_id=owner)
    return {"domain": domain, "count": len(tools), "tools": [agent_registry_record(row) for row in tools]}


@router.get("/{tool_id}")
async def get_tool(tool_id: UUID, request: Request):
    """Read the complete shared tool card within the authenticated owner's catalog."""
    owner = await _authorize(request)
    if not owner:
        raise HTTPException(403, "Требуется подключение агента с владельцем")
    card = await read_shared_tool(tool_id, owner, include_registration=True)
    if card is None:
        raise HTTPException(404, "Средство не найдено")
    return card


@router.delete("/{tool_id}")
async def delete_tool(tool_id: str, request: Request):
    """Деактивировать инструмент (soft delete)."""
    owner = await _authorize(request)
    touched = await deprecate_tool(UUID(tool_id), owner_user_id=owner)
    if not touched:
        raise HTTPException(status_code=404, detail="Инструмент не найден")
    return {"status": "deprecated", "id": tool_id}
