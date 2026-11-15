from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class TaskCheckpoint(BaseModel):
    """A dated report, never automatic proof of successful execution."""
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    task_id: str = Field(min_length=1, max_length=160)
    session_id: str = Field(min_length=1, max_length=160)
    status: Literal["unknown", "in_progress", "blocked", "awaiting_review", "completed", "cancelled"]
    next_step: str = Field(min_length=1, max_length=2000)
    evidence: list[str] = Field(default_factory=list, max_length=20)
    observed_at: datetime
    workspace: str = Field(min_length=1, max_length=1000)
    supersedes: UUID | None = None

    @field_validator("observed_at")
    @classmethod
    def observation_is_dated(cls, value):
        if value.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        if value > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError("observed_at cannot be in the future")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def completion_has_evidence(self):
        if self.status == "completed" and not any(x.strip() for x in self.evidence):
            raise ValueError("completed checkpoint requires evidence")
        return self


def validate_checkpoint(payload: dict) -> dict:
    if "checkpoint" not in payload:
        return payload
    point = TaskCheckpoint.model_validate(payload["checkpoint"])
    data = point.model_dump(mode="json")
    data["observed_at"] = point.observed_at.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return {**payload, "checkpoint": data}


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
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=160)


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
