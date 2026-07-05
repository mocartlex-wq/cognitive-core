"""Юнит-тесты risk-менеджера. Чистые функции, без I/O."""
from app.services.trading.risk import OrderRequest, Portfolio, evaluate


def _portfolio(**overrides) -> Portfolio:
    base = dict(cash=10_000.0, equity=10_000.0, positions={}, day_pnl=0.0, day_trades=0)
    base.update(overrides)
    return Portfolio(**base)


def test_allows_simple_buy_within_limits():
    order = OrderRequest("AAPL", "buy", quantity=5, price=100.0)
    d = evaluate(order, _portfolio())
    assert d.allow, d.reason


def test_rejects_zero_quantity():
    order = OrderRequest("AAPL", "buy", quantity=0, price=100.0)
    d = evaluate(order, _portfolio())
    assert not d.allow


def test_rejects_when_no_cash():
    order = OrderRequest("AAPL", "buy", quantity=100, price=200.0)
    d = evaluate(order, _portfolio(cash=500.0))
    assert not d.allow
    assert "insufficient" in d.reason


def test_rejects_when_position_exceeds_cap():
    # equity 10k, cap 10% = 1000. Покупка 100*15 = 1500 — мимо
    order = OrderRequest("AAPL", "buy", quantity=100, price=15.0)
    d = evaluate(order, _portfolio(cash=5000.0))
    assert not d.allow
    assert "position" in d.reason
    assert d.adjusted_quantity is not None
    assert d.adjusted_quantity * 15.0 <= 1000.0 + 1e-6


def test_rejects_too_wide_stop_loss():
    # стоп 10% при cap 2%
    order = OrderRequest("AAPL", "buy", quantity=1, price=100.0, stop_loss=90.0)
    d = evaluate(order, _portfolio())
    assert not d.allow
    assert "stop_loss" in d.reason


def test_allows_tight_stop_loss():
    order = OrderRequest("AAPL", "buy", quantity=1, price=100.0, stop_loss=98.5)
    d = evaluate(order, _portfolio())
    assert d.allow, d.reason


def test_halts_after_daily_drawdown():
    # просадка > 5%
    order = OrderRequest("AAPL", "buy", quantity=1, price=10.0)
    d = evaluate(order, _portfolio(day_pnl=-600.0))
    assert not d.allow
    assert "drawdown" in d.reason


def test_halts_after_trade_cap():
    order = OrderRequest("AAPL", "buy", quantity=1, price=10.0)
    d = evaluate(order, _portfolio(day_trades=20))
    assert not d.allow
    assert "day trades" in d.reason
