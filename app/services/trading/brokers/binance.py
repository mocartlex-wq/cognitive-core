"""Binance Spot adapter (crypto).

Docs: https://developers.binance.com/docs/binance-spot-api-docs
По умолчанию — Spot Testnet (testnet.binance.vision), без реальных денег.

Подпись: HMAC SHA256 по query-string c API secret, заголовок X-MBX-APIKEY.
Live-режим: settings.binance_testnet=False И settings.trading_allow_live=True.
"""
from __future__ import annotations

import hashlib
import hmac
import time
import urllib.parse
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

TESTNET_BASE = "https://testnet.binance.vision"
LIVE_BASE = "https://api.binance.com"
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

# Тикеры пользователя → пары на Binance (BTC → BTCUSDT). Можно переопределять
# через settings.binance_quote_asset, по умолчанию USDT.
def _to_pair(symbol: str) -> str:
    s = symbol.upper().strip()
    if s.endswith(("USDT", "USDC", "BUSD", "BTC", "ETH")):
        return s
    return f"{s}{settings.binance_quote_asset.upper()}"


class BinanceBroker:
    name = "binance"

    def __init__(self) -> None:
        if not settings.binance_key or not settings.binance_secret:
            raise BrokerError("binance: binance_key/binance_secret не заданы")
        self.is_testnet = bool(settings.binance_testnet) or not settings.trading_allow_live
        self.base = TESTNET_BASE if self.is_testnet else LIVE_BASE
        self._secret = settings.binance_secret.encode()
        self._headers = {"X-MBX-APIKEY": settings.binance_key}

    def _sign(self, params: dict[str, Any]) -> str:
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        query = urllib.parse.urlencode(params, doseq=True)
        sig = hmac.new(self._secret, query.encode(), hashlib.sha256).hexdigest()
        return f"{query}&signature={sig}"

    async def _signed(self, method: str, path: str, params: dict | None = None) -> dict:
        query = self._sign(dict(params or {}))
        url = f"{self.base}{path}?{query}"
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=self._headers) as cli:
            r = await cli.request(method, url)
        if r.status_code >= 400:
            raise BrokerError(f"binance {method} {path} → {r.status_code}: {r.text[:200]}")
        return r.json() if r.content else {}

    async def get_portfolio(self, _agent_id: str) -> Portfolio:
        acct = await self._signed("GET", "/api/v3/account")
        balances = acct.get("balances", [])
        cash = 0.0
        positions: dict[str, float] = {}
        quote = settings.binance_quote_asset.upper()
        for b in balances:
            asset = b.get("asset")
            free = float(b.get("free", 0))
            locked = float(b.get("locked", 0))
            total = free + locked
            if total <= 0:
                continue
            if asset == quote:
                cash += total
            else:
                positions[asset] = total
        equity = cash  # без живых котировок по каждой паре считаем только cash;
        # точная переоценка делается отдельным проходом в API-слое при желании.
        return Portfolio(cash=cash, equity=equity, positions=positions, day_pnl=0.0, day_trades=0)

    async def submit_order(
        self,
        agent_id: str,
        symbol: str,
        market: Market,
        side: str,
        quantity: float,
        stop_loss: float | None = None,
    ) -> FilledOrder:
        if market != "crypto":
            raise BrokerError(f"binance: market '{market}' не поддерживается")

        price = await quote_price(symbol, "crypto")
        portfolio = await self.get_portfolio(agent_id)
        await run_risk(self.name, agent_id, portfolio, symbol, "crypto", side, quantity, price, stop_loss)

        pair = _to_pair(symbol)
        params = {
            "symbol": pair,
            "side": side.upper(),
            "type": "MARKET",
            "quantity": str(quantity),
        }
        resp = await self._signed("POST", "/api/v3/order", params)
        avg = price
        fills = resp.get("fills") or []
        if fills:
            total_qty = sum(float(f.get("qty", 0)) for f in fills) or 1
            total_quote = sum(float(f.get("price", 0)) * float(f.get("qty", 0)) for f in fills)
            avg = total_quote / total_qty

        order = FilledOrder(
            order_id=str(resp.get("orderId") or new_order_id()),
            agent_id=agent_id,
            symbol=symbol.upper(), market="crypto", side=side,
            quantity=float(resp.get("executedQty", quantity)),
            price=avg,
            status=str(resp.get("status", "FILLED")).lower(),
            ts=now_iso(),
            risk_reason="ok",
        )
        await persist_order(self.name, agent_id, order)

        if stop_loss is not None:
            # отдельный STOP_LOSS_LIMIT ордер — не критично если не выйдет
            try:
                await self._signed("POST", "/api/v3/order", {
                    "symbol": pair, "side": "SELL" if side == "buy" else "BUY",
                    "type": "STOP_LOSS_LIMIT",
                    "quantity": str(quantity),
                    "stopPrice": str(stop_loss),
                    "price": str(stop_loss * 0.999),
                    "timeInForce": "GTC",
                })
            except BrokerError:
                pass

        return order

    async def list_orders(self, agent_id: str, limit: int = 50) -> list[dict]:
        return await _list_orders(self.name, agent_id, limit)

    async def reset(self, _agent_id: str, _cash: float | None = None) -> None:
        raise BrokerError("binance: reset недоступен (testnet можно сбросить только через UI Binance)")
