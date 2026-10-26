"""T-Bank Invest (Tinkoff) adapter.

Docs: https://russianinvestments.github.io/investAPI/
По умолчанию — Sandbox-методы (SandboxService/*), без реальных денег.

Особенности:
  • Авторизация: Bearer token
  • API представляет собой POST по полным методам gRPC-контракта
    (`tinkoff.public.invest.api.contract.v1.X/Method`) с JSON-телом
  • Тикер → FIGI преобразуется через InstrumentsService/FindInstrument

Live-режим: settings.tinkoff_sandbox=False И settings.trading_allow_live=True.
В live используются OrdersService/* и OperationsService/* вместо SandboxService.
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
    run_risk,
)
from app.services.trading.market_data import Market
from app.services.trading.risk import Portfolio

BASE = "https://invest-public-api.tinkoff.ru/rest"
_TIMEOUT = httpx.Timeout(15.0, connect=5.0)

_NS = "tinkoff.public.invest.api.contract.v1"

# in-process кэш ticker → figi (живёт пока процесс работает)
_FIGI_CACHE: dict[str, str] = {}


def _quotation_to_float(q: dict | None) -> float:
    """Tinkoff возвращает цены как {units: '100', nano: 500000000}."""
    if not q:
        return 0.0
    return float(q.get("units", 0)) + float(q.get("nano", 0)) / 1e9


class TinkoffBroker:
    name = "tinkoff"

    def __init__(self) -> None:
        if not settings.tinkoff_token:
            raise BrokerError("tinkoff: tinkoff_token не задан")
        self.is_sandbox = bool(settings.tinkoff_sandbox) or not settings.trading_allow_live
        self._headers = {
            "Authorization": f"Bearer {settings.tinkoff_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        # account_id берётся из settings или подбирается первым доступным
        self._account_id: str | None = settings.tinkoff_account_id or None

    async def _call(self, service: str, method: str, body: dict) -> dict:
        url = f"{BASE}/{_NS}.{service}/{method}"
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=self._headers) as cli:
            r = await cli.post(url, json=body)
        if r.status_code >= 400:
            raise BrokerError(f"tinkoff {service}.{method} → {r.status_code}: {r.text[:200]}")
        return r.json() if r.content else {}

    async def _ensure_account(self) -> str:
        if self._account_id:
            return self._account_id
        svc = "SandboxService" if self.is_sandbox else "UsersService"
        method = "GetSandboxAccounts" if self.is_sandbox else "GetAccounts"
        data = await self._call(svc, method, {})
        accounts = data.get("accounts") or []
        if not accounts:
            raise BrokerError("tinkoff: нет ни одного счёта (для sandbox создайте через OpenSandboxAccount)")
        self._account_id = str(accounts[0].get("id"))
        return self._account_id

    async def _ticker_to_figi(self, ticker: str) -> str:
        key = ticker.upper()
        if key in _FIGI_CACHE:
            return _FIGI_CACHE[key]
        data = await self._call("InstrumentsService", "FindInstrument",
                                {"query": key, "instrumentKind": "INSTRUMENT_TYPE_SHARE", "apiTradeAvailableFlag": True})
        instruments = data.get("instruments") or []
        # выбираем тикер с биржей MOEX и валютой RUB, иначе первый
        chosen = next(
            (i for i in instruments if i.get("ticker", "").upper() == key and i.get("currency", "").lower() == "rub"),
            instruments[0] if instruments else None,
        )
        if not chosen:
            raise BrokerError(f"tinkoff: тикер {ticker} не найден")
        figi = chosen.get("figi")
        _FIGI_CACHE[key] = figi
        return figi

    async def get_portfolio(self, _agent_id: str) -> Portfolio:
        account_id = await self._ensure_account()
        svc = "SandboxService" if self.is_sandbox else "OperationsService"
        method = "GetSandboxPortfolio" if self.is_sandbox else "GetPortfolio"
        data = await self._call(svc, method, {"accountId": account_id, "currency": "RUB"})
        positions = data.get("positions") or []
        pos_map: dict[str, float] = {}
        market_value = 0.0
        cash = 0.0
        for p in positions:
            instr_type = p.get("instrumentType", "")
            qty = _quotation_to_float(p.get("quantity"))
            price = _quotation_to_float(p.get("currentPrice"))
            if instr_type == "currency":
                cash += qty * price if price else qty
                continue
            ticker = p.get("ticker") or p.get("figi")
            if ticker and qty != 0:
                pos_map[ticker] = qty
                market_value += qty * price
        # totalAmountPortfolio даёт полный equity
        equity = _quotation_to_float(data.get("totalAmountPortfolio")) or (cash + market_value)
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
        if market != "ru":
            raise BrokerError(f"tinkoff: market '{market}' не поддерживается")

        account_id = await self._ensure_account()
        figi = await self._ticker_to_figi(symbol)

        # current price для risk-чека
        last = await self._call("MarketDataService", "GetLastPrices", {"figi": [figi]})
        prices = last.get("lastPrices") or []
        price = _quotation_to_float(prices[0].get("price")) if prices else 0.0
        if price <= 0:
            raise BrokerError(f"tinkoff: нет цены для {symbol}")

        portfolio = await self.get_portfolio(agent_id)
        await run_risk(self.name, agent_id, portfolio, symbol, "ru", side, quantity, price, stop_loss)

        svc = "SandboxService" if self.is_sandbox else "OrdersService"
        method = "PostSandboxOrder" if self.is_sandbox else "PostOrder"
        body: dict[str, Any] = {
            "instrumentId": figi,
            "quantity": str(int(quantity)),  # lots
            "direction": "ORDER_DIRECTION_BUY" if side == "buy" else "ORDER_DIRECTION_SELL",
            "accountId": account_id,
            "orderType": "ORDER_TYPE_MARKET",
            "orderId": new_order_id(),
        }
        resp = await self._call(svc, method, body)

        exec_price = _quotation_to_float(resp.get("executedOrderPrice")) or price
        status = str(resp.get("executionReportStatus", "EXECUTION_REPORT_STATUS_NEW")).lower()
        order = FilledOrder(
            order_id=str(resp.get("orderId", body["orderId"])),
            agent_id=agent_id,
            symbol=symbol.upper(), market="ru", side=side,
            quantity=float(resp.get("lotsExecuted", quantity)),
            price=exec_price,
            status=status,
            ts=now_iso(),
            risk_reason="ok",
        )
        await persist_order(self.name, agent_id, order)
        return order

    async def list_orders(self, agent_id: str, limit: int = 50) -> list[dict]:
        return await _list_orders(self.name, agent_id, limit)

    async def reset(self, _agent_id: str, cash: float | None = None) -> None:
        """Только для sandbox: пополнить счёт виртуальными деньгами."""
        if not self.is_sandbox:
            raise BrokerError("tinkoff: reset недоступен для live-счёта")
        account_id = await self._ensure_account()
        amount = int(cash if cash is not None else 100_000)
        await self._call("SandboxService", "SandboxPayIn", {
            "accountId": account_id,
            "amount": {"currency": "rub", "units": str(amount), "nano": 0},
        })
