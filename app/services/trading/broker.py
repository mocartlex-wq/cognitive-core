"""Broker-абстракция + PaperBroker (симулятор).

Состояние симулятора хранится в Redis под ключами:
  paper:{agent}:cash         (float)
  paper:{agent}:positions    (HSET symbol -> qty)
  paper:{agent}:cost_basis   (HSET symbol -> avg_price)
  paper:{agent}:orders       (LIST JSON)
  paper:{agent}:day_pnl      (float, сбрасывается на TTL 24h)
  paper:{agent}:day_trades   (int, TTL 24h)

Реальные брокеры (Tinkoff/Alpaca/Binance) можно добавить, реализовав
протокол BrokerClient и добавив адаптер в get_broker().
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import uuid4

from app.config import settings
from app.db.redis import get_redis
from app.services.trading.market_data import Market, get_quote
from app.services.trading.risk import OrderRequest, Portfolio, RiskDecision, evaluate

_STARTING_CASH = 100_000.0  # дефолтный депозит для paper trading
_DAY_TTL = 24 * 3600


@dataclass
class FilledOrder:
    order_id: str
    agent_id: str
    symbol: str
    market: Market
    side: str
    quantity: float
    price: float
    status: str
    ts: str
    risk_reason: str


class BrokerError(RuntimeError):
    pass


class BrokerClient(Protocol):
    async def get_portfolio(self, agent_id: str) -> Portfolio: ...
    async def submit_order(
        self,
        agent_id: str,
        symbol: str,
        market: Market,
        side: str,
        quantity: float,
        stop_loss: float | None = None,
    ) -> FilledOrder: ...
    async def list_orders(self, agent_id: str, limit: int = 50) -> list[dict]: ...
    async def reset(self, agent_id: str, cash: float | None = None) -> None: ...


class PaperBroker:
    """Симулятор: исполняет ордера по текущей рыночной цене."""

    def __init__(self) -> None:
        self.name = "paper"

    async def get_portfolio(self, agent_id: str) -> Portfolio:
        r = await get_redis()
        cash_raw = await r.get(f"paper:{agent_id}:cash")
        cash = float(cash_raw) if cash_raw is not None else _STARTING_CASH
        if cash_raw is None:
            await r.set(f"paper:{agent_id}:cash", cash)

        pos_map = await r.hgetall(f"paper:{agent_id}:positions")
        positions = {sym: float(q) for sym, q in pos_map.items() if float(q) != 0}

        cost_map = await r.hgetall(f"paper:{agent_id}:cost_basis")
        market_value = 0.0
        # Рыночную стоимость нельзя посчитать без живых котировок — приближение
        # делаем по cost basis (последняя цена покупки). Для точности дёрнуть
        # get_quote per symbol — но это сетевые вызовы; делаем только в API-слое.
        for sym, qty in positions.items():
            avg = float(cost_map.get(sym, 0))
            market_value += qty * avg

        pnl_raw = await r.get(f"paper:{agent_id}:day_pnl")
        day_pnl = float(pnl_raw) if pnl_raw else 0.0
        trades_raw = await r.get(f"paper:{agent_id}:day_trades")
        day_trades = int(trades_raw) if trades_raw else 0

        return Portfolio(
            cash=cash,
            equity=cash + market_value,
            positions=positions,
            day_pnl=day_pnl,
            day_trades=day_trades,
        )

    async def submit_order(
        self,
        agent_id: str,
        symbol: str,
        market: Market,
        side: str,
        quantity: float,
        stop_loss: float | None = None,
    ) -> FilledOrder:
        symbol = symbol.upper().strip()
        quote = await get_quote(symbol, market)
        price = quote.get("price")
        if price is None or price <= 0:
            raise BrokerError(f"no live price for {symbol}")

        portfolio = await self.get_portfolio(agent_id)
        order_req = OrderRequest(symbol=symbol, side=side, quantity=quantity,
                                  price=float(price), stop_loss=stop_loss)
        decision: RiskDecision = evaluate(order_req, portfolio)
        if not decision.allow:
            order = FilledOrder(
                order_id=str(uuid4()),
                agent_id=agent_id,
                symbol=symbol, market=market, side=side,
                quantity=quantity, price=float(price),
                status="rejected", ts=_now_iso(), risk_reason=decision.reason,
            )
            await self._persist_order(agent_id, order)
            return order

        # Исполняем
        r = await get_redis()
        cost = quantity * float(price)
        current_qty = portfolio.positions.get(symbol, 0.0)
        current_avg = float(await r.hget(f"paper:{agent_id}:cost_basis", symbol) or 0)

        if side == "buy":
            new_cash = portfolio.cash - cost
            new_qty = current_qty + quantity
            new_avg = (current_qty * current_avg + cost) / new_qty if new_qty != 0 else 0
        else:  # sell
            new_cash = portfolio.cash + cost
            new_qty = current_qty - quantity
            new_avg = current_avg  # avg цена не меняется при частичной продаже
            # реализованный P&L на закрытие
            realized = (float(price) - current_avg) * min(quantity, current_qty)
            await _incr_day_pnl(r, agent_id, realized)

        await r.set(f"paper:{agent_id}:cash", new_cash)
        if new_qty == 0:
            await r.hdel(f"paper:{agent_id}:positions", symbol)
            await r.hdel(f"paper:{agent_id}:cost_basis", symbol)
        else:
            await r.hset(f"paper:{agent_id}:positions", symbol, str(new_qty))
            await r.hset(f"paper:{agent_id}:cost_basis", symbol, str(new_avg))

        await r.incr(f"paper:{agent_id}:day_trades")
        await r.expire(f"paper:{agent_id}:day_trades", _DAY_TTL)

        order = FilledOrder(
            order_id=str(uuid4()),
            agent_id=agent_id,
            symbol=symbol, market=market, side=side,
            quantity=quantity, price=float(price),
            status="filled", ts=_now_iso(), risk_reason=decision.reason,
        )
        await self._persist_order(agent_id, order)
        return order

    async def list_orders(self, agent_id: str, limit: int = 50) -> list[dict]:
        r = await get_redis()
        raws = await r.lrange(f"paper:{agent_id}:orders", 0, limit - 1)
        out = []
        for raw in raws:
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
        return out

    async def reset(self, agent_id: str, cash: float | None = None) -> None:
        r = await get_redis()
        for key in (
            f"paper:{agent_id}:cash",
            f"paper:{agent_id}:positions",
            f"paper:{agent_id}:cost_basis",
            f"paper:{agent_id}:orders",
            f"paper:{agent_id}:day_pnl",
            f"paper:{agent_id}:day_trades",
        ):
            await r.delete(key)
        await r.set(f"paper:{agent_id}:cash", cash if cash is not None else _STARTING_CASH)

    async def _persist_order(self, agent_id: str, order: FilledOrder) -> None:
        r = await get_redis()
        await r.lpush(f"paper:{agent_id}:orders", json.dumps(asdict(order), ensure_ascii=False))
        await r.ltrim(f"paper:{agent_id}:orders", 0, 999)


async def _incr_day_pnl(r, agent_id: str, delta: float) -> None:
    key = f"paper:{agent_id}:day_pnl"
    cur = await r.get(key)
    new = (float(cur) if cur else 0.0) + float(delta)
    await r.set(key, new)
    await r.expire(key, _DAY_TTL)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_DEFAULT_BROKER: BrokerClient | None = None


def get_broker() -> BrokerClient:
    """Returns active broker per settings.trading_broker. Default = paper."""
    global _DEFAULT_BROKER
    if _DEFAULT_BROKER is not None:
        return _DEFAULT_BROKER
    name = (settings.trading_broker or "paper").lower()
    if name == "paper":
        _DEFAULT_BROKER = PaperBroker()
        return _DEFAULT_BROKER
    raise BrokerError(
        f"broker '{name}' is not implemented yet — only 'paper' is supported. "
        "Реальные брокеры (Tinkoff/Alpaca/Binance) добавляются отдельным адаптером."
    )
