from fastapi import APIRouter, Request, HTTPException
from app.models.tools import ToolRegistryInput
from app.security.auth import verify_api_key
from app.services.tools import register_tool, get_active_tools, deprecate_tool
from uuid import UUID

router = APIRouter(prefix="/tools", tags=["tools"])


@router.post("")
async def create_tool(body: ToolRegistryInput, request: Request):
    """Зарегистрировать новый инструмент в L3."""
    await verify_api_key(request)
    tool_id = await register_tool(body)
    return {"status": "registered", "id": str(tool_id)}


@router.get("")
async def list_tools(domain: str, request: Request):
    """Список активных инструментов домена."""
    await verify_api_key(request)
    tools = await get_active_tools(domain)
    return {"domain": domain, "count": len(tools), "tools": tools}


@router.delete("/{tool_id}")
async def delete_tool(tool_id: str, request: Request):
    """Деактивировать инструмент (soft delete)."""
    await verify_api_key(request)
    await deprecate_tool(UUID(tool_id))
    return {"status": "deprecated", "id": tool_id}
