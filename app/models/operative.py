from uuid import UUID

from pydantic import BaseModel


class OperativeRule(BaseModel):
    id: UUID
    record_type: str  # knowledge, tool
    domain: str
    content: dict | None = None
    tool_name: str | None = None
    tool_type: str | None = None
    config_schema: dict | None = None
    usage: dict | None = None
    confidence: float = 0.0
    distance: float | None = None  # для KNN


class OperativeQuery(BaseModel):
    domain: str
    context: str | None = None
    top_k: int = 5
    include_tools: bool = True


class OperativeRecallUI(BaseModel):
    context: str | None = None
    top_k: int = 5


class OperativeSession(BaseModel):
    session_id: UUID
    domain: str
    fragment: dict  # {"knowledge": [...], "tools": [...]}
    expires_in: int = 86400


class OperativeClose(BaseModel):
    session_id: UUID
    keep_results: bool = False
    results_summary: dict | None = None
    source_agent: str = "user"


class OperativeFeedback(BaseModel):
    session_id: UUID
    record_id: UUID
    record_type: str  # knowledge, tool
    useful: bool
