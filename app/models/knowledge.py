from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class MasterKnowledge(BaseModel):
    id: UUID
    domain: str
    knowledge_type: str  # pattern, mistake, rule
    content: dict
    version: int = 1
    derived_from_l2_ids: list[UUID] = []
    related_tool_ids: list[UUID] = []
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    created_at: datetime | None = None


class ToolRegistry(BaseModel):
    id: UUID
    domain: str
    tool_name: str
    tool_type: str  # api, script, prompt, library, service
    description: str | None = None
    config_schema: dict | None = None
    usage_patterns: dict | None = None
    l2_source_ids: list[UUID] = []
    version: int = 1
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    created_at: datetime | None = None
