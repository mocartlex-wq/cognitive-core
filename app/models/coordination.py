"""Explicit contracts for the shared task ledger (separate from chat replies)."""
from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    uri: str = Field(min_length=1, max_length=2048)
    description: str = Field(min_length=1, max_length=1000)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    idempotency_key: str = Field(min_length=8, max_length=160)
    session_id: str = Field(min_length=1, max_length=200)
    project: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=12000)
    acceptance: str = Field(min_length=1, max_length=6000)
    next_step: str = Field(min_length=1, max_length=2000)
    depends_on: list[UUID] = Field(default_factory=list, max_length=30)
    assigned_agent_id: str | None = Field(default=None, max_length=200)
    assigned_session_id: str | None = Field(default=None, max_length=200)
    worktree: str | None = Field(default=None, max_length=1000)


class TaskAction(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    idempotency_key: str = Field(min_length=8, max_length=160)
    session_id: str = Field(min_length=1, max_length=200)
    expected_version: int = Field(ge=0)
    action: Literal["claim", "heartbeat", "recover", "submit", "block", "fail", "retry", "cancel", "review", "assign"]
    attempt_id: UUID | None = None
    lease_seconds: int = Field(default=300, ge=30, le=3600)
    detail: str = Field(default="", max_length=4000)
    next_step: str = Field(default="", max_length=2000)
    evidence: list[Evidence] = Field(default_factory=list, max_length=30)
    verdict: Literal["approve", "reject"] | None = None
    reconciliation: str = Field(default="", max_length=4000)
    assigned_agent_id: str | None = Field(default=None, max_length=200)
    assigned_session_id: str | None = Field(default=None, max_length=200)
