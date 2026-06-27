"""REST API: котировки, история, новости/сентимент, paper-trading.

Все ендпоинты требуют X-API-Key (verify_api_key).
В paper-режиме agent_id из ключа становится owner'ом портфеля.
"""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request

from app.models.trading import (
    HistoryRequest,
    NewsRequest,
    OrderInput,
    QuoteRequest,
    ResetPortfolioInput,
    SentimentRequest,
)
from app.security.auth import verify_api_key
from app.services.trading.broker import BrokerError, get_broker
from app.services.trading.market_data import MarketDataError, get_history, get_quote
from app.services.trading.news import analyze_sentiment, fetch_headlines

router = APIRouter(prefix="/trading", tags=["trading"])


@router.post("/quote")
async def quote(body: QuoteRequest, request: Request):
    await verify_api_key(request)
    try:
        return await get_quote(body.symbol, body.market)
    except MarketDataError as e:
        raise HTTPException(status_code=502, detail=f"market_data: {e}")


@router.post("/history")
async def history(body: HistoryRequest, request: Request):
    await verify_api_key(request)
    try:
        rows = await get_history(body.symbol, body.market, body.days)
    except MarketDataError as e:
        raise HTTPException(status_code=502, detail=f"market_data: {e}")
    return {"symbol": body.symbol.upper(), "market": body.market, "days": body.days, "rows": rows}


@router.post("/news")
async def news(body: NewsRequest, request: Request):
    await verify_api_key(request)
    items = await fetch_headlines(body.symbol, body.limit)
    return {"symbol": (body.symbol or "").upper() or None, "count": len(items), "items": items}


@router.post("/sentiment")
async def sentiment(body: SentimentRequest, request: Request):
    await verify_api_key(request)
    return await analyze_sentiment(body.symbol.upper())


@router.get("/portfolio")
async def portfolio(request: Request):
    agent_id = await verify_api_key(request)
    p = await get_broker().get_portfolio(agent_id)
    return {
        "agent_id": agent_id,
        "broker": getattr(get_broker(), "name", "paper"),
        "cash": p.cash,
        "equity": p.equity,
        "positions": p.positions,
        "day_pnl": p.day_pnl,
        "day_trades": p.day_trades,
    }


@router.post("/order")
async def submit_order(body: OrderInput, request: Request):
    agent_id = await verify_api_key(request)
    try:
        order = await get_broker().submit_order(
            agent_id=agent_id,
            symbol=body.symbol,
            market=body.market,
            side=body.side,
            quantity=body.quantity,
            stop_loss=body.stop_loss,
        )
    except BrokerError as e:
        raise HTTPException(status_code=502, detail=f"broker: {e}")
    except MarketDataError as e:
        raise HTTPException(status_code=502, detail=f"market_data: {e}")
    return asdict(order)


@router.get("/orders")
async def list_orders(request: Request, limit: int = 50):
    agent_id = await verify_api_key(request)
    limit = max(1, min(limit, 500))
    return {"agent_id": agent_id, "orders": await get_broker().list_orders(agent_id, limit)}


@router.post("/portfolio/reset")
async def reset_portfolio(body: ResetPortfolioInput, request: Request):
    """Сбрасывает paper-портфель в исходное состояние. Для реальных брокеров — 405."""
    agent_id = await verify_api_key(request)
    broker = get_broker()
    if getattr(broker, "name", "") != "paper":
        raise HTTPException(status_code=405, detail="reset допустим только для paper-broker")
    await broker.reset(agent_id, body.cash)
    return {"status": "reset", "agent_id": agent_id, "cash": body.cash}
