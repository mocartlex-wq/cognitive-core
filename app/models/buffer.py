from pydantic import BaseModel
from uuid import UUID
from datetime import date, datetime


class DailyBufferOutput(BaseModel):
    id: UUID
    date: date
    domain: str
    summary: dict
    source_event_ids: list[UUID]
    confidence: float
    created_at: datetime


class ConsolidateRequest(BaseModel):
    since_hours: int | None = None  # переопределяет DAILY_HOURS
    domain: str | None = None       # фильтр по конкретному домену
