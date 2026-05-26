from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RawEventInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_agent: str = Field(..., min_length=2, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    domain: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    payload: dict = Field(...)


class EventResponse(BaseModel):
    id: UUID
    status: str = "accepted"
    timestamp: datetime


class L1RawEvent(BaseModel):
    id: UUID
    timestamp: datetime
    source_agent: str
    domain: str
    raw_payload: dict
    processed_to_l2: bool = False
    created_at: datetime
