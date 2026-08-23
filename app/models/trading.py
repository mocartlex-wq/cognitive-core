"""Pydantic-модели для /trading/* эндпоинтов."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Market = Literal["us", "ru", "crypto"]
Side = Literal["buy", "sell"]


class QuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=1, max_length=20)
    market: Market = "us"


class HistoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=1, max_length=20)
    market: Market = "us"
    days: int = Field(default=30, ge=1, le=365)


class NewsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str | None = None
    limit: int = Field(default=10, ge=1, le=50)


class SentimentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=1, max_length=20)


class OrderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=1, max_length=20)
    market: Market = "us"
    side: Side
    quantity: float = Field(gt=0)
    stop_loss: float | None = Field(default=None, gt=0)

    @field_validator("symbol")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class ResetPortfolioInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cash: float | None = Field(default=None, gt=0)
