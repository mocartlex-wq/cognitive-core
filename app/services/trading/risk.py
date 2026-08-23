"""Risk manager — жёсткие лимиты ДО исполнения ордера.

Принцип: чистые функции без I/O, чтобы было легко тестировать. Решение
возвращается как RiskDecision: allow=False означает «не пропускать».

Лимиты берутся из app.config.settings (значения по умолчанию консервативные
для новичка — макс. 10% капитала в одну позицию, 2% стоп, 5% дневная просадка).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.config import settings

Side = Literal["buy", "sell"]


@dataclass
class Portfolio:
    cash: float                       # свободный кэш в базовой валюте
    equity: float                     # полный капитал (cash + рыночная стоимость позиций)
    positions: dict[str, float]       # symbol -> количество (>0 long, <0 short)
    day_pnl: float = 0.0              # реализованный + нереализованный P&L за сегодня
    day_trades: int = 0               # сколько ордеров уже исполнено сегодня


@dataclass
class OrderRequest:
    symbol: str
    side: Side
    quantity: float
    price: float
    stop_loss: float | None = None


@dataclass
class RiskDecision:
    allow: bool
    reason: str
    adjusted_quantity: float | None = None


def evaluate(order: OrderRequest, portfolio: Portfolio) -> RiskDecision:
    """Применяет цепочку проверок. Первая failing — возвращается."""
    checks = (
        _check_basic,
        _check_funds,
        _check_position_cap,
        _check_stop_loss,
        _check_day_drawdown,
        _check_day_trade_count,
    )
    for check in checks:
        decision = check(order, portfolio)
        if not decision.allow:
            return decision
    return RiskDecision(allow=True, reason="ok")


def _check_basic(order: OrderRequest, _portfolio: Portfolio) -> RiskDecision:
    if order.quantity <= 0:
        return RiskDecision(False, "quantity must be > 0")
    if order.price <= 0:
        return RiskDecision(False, "price must be > 0")
    if order.side not in ("buy", "sell"):
        return RiskDecision(False, f"unknown side: {order.side}")
    return RiskDecision(True, "ok")


def _check_funds(order: OrderRequest, portfolio: Portfolio) -> RiskDecision:
    if order.side != "buy":
        return RiskDecision(True, "ok")  # шорт оценивается на уровне position_cap
    cost = order.quantity * order.price
    if cost > portfolio.cash:
        return RiskDecision(False, f"insufficient cash: need {cost:.2f}, have {portfolio.cash:.2f}")
    return RiskDecision(True, "ok")


def _check_position_cap(order: OrderRequest, portfolio: Portfolio) -> RiskDecision:
    """Позиция не должна превышать settings.trading_max_position_pct от equity."""
    max_pct = settings.trading_max_position_pct / 100.0
    if portfolio.equity <= 0:
        return RiskDecision(False, "equity is zero — cannot open positions")
    max_value = portfolio.equity * max_pct
    current_qty = portfolio.positions.get(order.symbol, 0.0)
    new_qty = current_qty + (order.quantity if order.side == "buy" else -order.quantity)
    new_value = abs(new_qty) * order.price
    if new_value > max_value:
        # подрезаем до лимита, но не меньше 0
        allowed_qty = max(0.0, (max_value / order.price) - abs(current_qty))
        return RiskDecision(
            False,
            f"position {new_value:.2f} exceeds {max_pct * 100:.1f}% cap ({max_value:.2f})",
            adjusted_quantity=allowed_qty if allowed_qty > 0 else None,
        )
    return RiskDecision(True, "ok")


def _check_stop_loss(order: OrderRequest, _portfolio: Portfolio) -> RiskDecision:
    """Если задан stop_loss, дистанция не должна превышать settings.trading_max_stop_pct."""
    if order.stop_loss is None:
        return RiskDecision(True, "ok")
    if order.stop_loss <= 0:
        return RiskDecision(False, "stop_loss must be > 0")
    distance_pct = abs(order.price - order.stop_loss) / order.price * 100.0
    max_stop = settings.trading_max_stop_pct
    if distance_pct > max_stop:
        return RiskDecision(False, f"stop_loss {distance_pct:.2f}% > max {max_stop:.2f}%")
    return RiskDecision(True, "ok")


def _check_day_drawdown(_order: OrderRequest, portfolio: Portfolio) -> RiskDecision:
    """Если дневная просадка уже превысила лимит — блокируем новые ордера."""
    if portfolio.equity <= 0:
        return RiskDecision(True, "ok")
    dd_pct = (-portfolio.day_pnl / portfolio.equity) * 100.0
    max_dd = settings.trading_max_daily_drawdown_pct
    if dd_pct >= max_dd:
        return RiskDecision(False, f"daily drawdown {dd_pct:.2f}% >= {max_dd:.2f}% — trading halted")
    return RiskDecision(True, "ok")


def _check_day_trade_count(_order: OrderRequest, portfolio: Portfolio) -> RiskDecision:
    if portfolio.day_trades >= settings.trading_max_day_trades:
        return RiskDecision(False, f"day trades {portfolio.day_trades} >= cap {settings.trading_max_day_trades}")
    return RiskDecision(True, "ok")
