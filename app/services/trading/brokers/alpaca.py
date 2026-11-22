"""Alpaca Markets adapter (US equities).

Docs: https://docs.alpaca.markets/reference
По умолчанию — paper trading endpoint (без реальных денег).

Ключи: settings.alpaca_key + settings.alpaca_secret.
Live-режим: settings.alpaca_paper=False И settings.trading_allow_live=True.
"""
from __future__ import annotations

from typing import Any

import httpx

from app.config import settings
from app.services.trading.broker import BrokerError, FilledOrder
from app.services.trading.brokers._common import (
    list_orders as _list_orders,
    new_order_id,
    now_iso,
    persist_order,
    quote_price,
    run_risk,
)
from app.services.trading.market_data import Market
from app.services.trading.risk import Portfolio

PAPER_BASE = "https://paper-api.alpaca.markets"
LIVE_BASE = "https://api.alpaca.markets"
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class AlpacaBroker:
    name = "alpaca"

    def __init__(self) -> None:
        if not settings.alpaca_key or not settings.alpaca_secret:
            raise BrokerError("alpaca: alpaca_key/alpaca_secret не заданы")
        self.is_paper = bool(settings.alpaca_paper) or not settings.trading_allow_live
        self.base = PAPER_BASE if self.is_paper else LIVE_BASE
        self._headers = {
            "APCA-API-KEY-ID": settings.alpaca_key,
            "APCA-API-SECRET-KEY": settings.alpaca_secret,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _request(self, method: str, path: str, **kw: Any) -> dict:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=self._headers) as cli:
            r = await cli.request(method, f"{self.base}{path}", **kw)
        if r.status_code >= 400:
            raise BrokerError(f"alpaca {method} {path} → {r.status_code}: {r.text[:200]}")
        if r.status_code == 204 or not r.content:
            return {}
        return r.json()

    async def get_portfolio(self, _agent_id: str) -> Portfolio:
        account = await self._request("GET", "/v2/account")
        positions = await self._request("GET", "/v2/positions")
        cash = float(account.get("cash", 0))
        equity = float(account.get("equity", 0))
        pos_map = {p["symbol"]: float(p["qty"]) for p in (positions or []) if float(p.get("qty", 0)) != 0}
        return Portfolio(cash=cash, equity=equity, positions=pos_map, day_pnl=0.0, day_trades=0)

    async def submit_order(
        self,
        agent_id: str,
        symbol: str,
        market: Market,
        side: str,
        quantity: float,
        stop_loss: float | None = None,
    ) -> FilledOrder:
        if market != "us":
            raise BrokerError(f"alpaca: market '{market}' не поддерживается")

        price = await quote_price(symbol, "us")
        portfolio = await self.get_portfolio(agent_id)
        await run_risk(self.name, agent_id, portfolio, symbol, "us", side, quantity, price, stop_loss)

        body = {
            "symbol": symbol.upper(),
            "qty": str(quantity),
            "side": side,
            "type": "market",
            "time_in_force": "day",
        }
        if stop_loss is not None:
            body["order_class"] = "bracket"
            body["stop_loss"] = {"stop_price": str(stop_loss)}

        resp = await self._request("POST", "/v2/orders", json=body)
        order = FilledOrder(
            order_id=str(resp.get("id") or new_order_id()),
            agent_id=agent_id,
            symbol=symbol.upper(), market="us", side=side,
            quantity=float(resp.get("qty", quantity)),
            price=float(resp.get("filled_avg_price") or price),
            status=str(resp.get("status", "accepted")),
            ts=str(resp.get("submitted_at") or now_iso()),
            risk_reason="ok",
        )
        await persist_order(self.name, agent_id, order)
        return order

    async def list_orders(self, agent_id: str, limit: int = 50) -> list[dict]:
        return await _list_orders(self.name, agent_id, limit)

    async def reset(self, _agent_id: str, _cash: float | None = None) -> None:
        raise BrokerError("alpaca: reset недоступен (это реальный/paper счёт, не симулятор)")
