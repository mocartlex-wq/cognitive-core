from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RawEventInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # FIX 2026-05-26: разрешить Unicode-буквы (Cyrillic etc) — agent_id уже
    # принимает их (PR #32), source_agent должен match'ить тот же alphabet.
    # `\w` в Python regex = [a-zA-Z0-9_] + Unicode-letters (без re.ASCII flag).
    source_agent: str = Field(..., min_length=2, max_length=64, pattern=r"^[\w.-]+$")
    # Domain: разрешить Unicode + hyphens для имён вроде 'офис-mocartlex' или
    # 'project-alpha'. Lowercase requirement убран — Cyrillic не имеет case.
    domain: str = Field(..., min_length=1, max_length=64, pattern=r"^[\w][\w.-]*$")
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
