"""Общие утилиты для брокерских адаптеров: risk-чек, журнал, идентификатор."""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from uuid import uuid4

from app.db.redis import get_redis
from app.services.trading.broker import BrokerError, FilledOrder
from app.services.trading.market_data import Market, get_quote
from app.services.trading.risk import OrderRequest, Portfolio, evaluate


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_order_id() -> str:
    return str(uuid4())


async def run_risk(
    broker_name: str,
    agent_id: str,
    portfolio: Portfolio,
    symbol: str,
    market: Market,
    side: str,
    quantity: float,
    price: float,
    stop_loss: float | None,
) -> None:
    """Risk-gate перед отправкой ордера в брокер. Если не пропускает —
    raise BrokerError, чтобы caller вернул 502 с понятным reason."""
    decision = evaluate(
        OrderRequest(symbol=symbol, side=side, quantity=quantity, price=price, stop_loss=stop_loss),
        portfolio,
    )
    if not decision.allow:
        await persist_order(
            broker_name, agent_id,
            FilledOrder(
                order_id=new_order_id(), agent_id=agent_id,
                symbol=symbol, market=market, side=side,
                quantity=quantity, price=price,
                status="rejected", ts=now_iso(), risk_reason=decision.reason,
            ),
        )
        raise BrokerError(f"risk: {decision.reason}")


async def quote_price(symbol: str, market: Market) -> float:
    """Получает текущую цену для risk-чека (когда брокер не возвращает её в response)."""
    q = await get_quote(symbol, market)
    price = q.get("price")
    if price is None or price <= 0:
        raise BrokerError(f"no live price for {symbol}")
    return float(price)


async def persist_order(broker_name: str, agent_id: str, order: FilledOrder) -> None:
    """Журнал ордеров в Redis (общий для paper и реальных брокеров).
    Ключ: {broker}:{agent_id}:orders, ограничиваем 1000 последних."""
    r = await get_redis()
    key = f"{broker_name}:{agent_id}:orders"
    await r.lpush(key, json.dumps(asdict(order), ensure_ascii=False))
    await r.ltrim(key, 0, 999)


async def list_orders(broker_name: str, agent_id: str, limit: int) -> list[dict]:
    r = await get_redis()
    raws = await r.lrange(f"{broker_name}:{agent_id}:orders", 0, limit - 1)
    out = []
    for raw in raws:
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return out
