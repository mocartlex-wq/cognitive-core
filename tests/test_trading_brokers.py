"""Тесты конструкторов реальных брокерских адаптеров и фабрики get_broker.

Проверяют:
  • ошибку при отсутствии ключей,
  • работу глобального safety-guard trading_allow_live=False (всегда sandbox),
  • маршрутизацию get_broker('auto') по рынкам,
  • отказ при неизвестном режиме.
Без сетевых вызовов.
"""
import pytest

from app.config import settings
from app.services.trading.broker import BrokerError, get_broker, reset_broker_cache
from app.services.trading.brokers.alpaca import AlpacaBroker, LIVE_BASE as A_LIVE, PAPER_BASE as A_PAPER
from app.services.trading.brokers.binance import BinanceBroker, LIVE_BASE as B_LIVE, TESTNET_BASE as B_TEST
from app.services.trading.brokers.tinkoff import TinkoffBroker


@pytest.fixture(autouse=True)
def _isolate_settings():
    """Сохраняем и восстанавливаем все trading-настройки между тестами."""
    snapshot = {
        k: getattr(settings, k) for k in (
            "trading_broker", "trading_allow_live",
            "alpaca_key", "alpaca_secret", "alpaca_paper",
            "binance_key", "binance_secret", "binance_testnet",
            "tinkoff_token", "tinkoff_sandbox",
        )
    }
    yield
    for k, v in snapshot.items():
        setattr(settings, k, v)
    reset_broker_cache()


def test_alpaca_requires_keys():
    settings.alpaca_key = ""
    settings.alpaca_secret = ""
    with pytest.raises(BrokerError, match="alpaca_key"):
        AlpacaBroker()


def test_binance_requires_keys():
    settings.binance_key = ""
    settings.binance_secret = ""
    with pytest.raises(BrokerError, match="binance_key"):
        BinanceBroker()


def test_tinkoff_requires_token():
    settings.tinkoff_token = ""
    with pytest.raises(BrokerError, match="tinkoff_token"):
        TinkoffBroker()


def test_safety_guard_forces_sandbox_when_live_not_allowed():
    """Даже если alpaca_paper=False, при trading_allow_live=False → paper-base."""
    settings.alpaca_key = "k"
    settings.alpaca_secret = "s"
    settings.alpaca_paper = False
    settings.trading_allow_live = False
    b = AlpacaBroker()
    assert b.is_paper is True
    assert b.base == A_PAPER


def test_live_when_both_flags_set():
    settings.alpaca_key = "k"
    settings.alpaca_secret = "s"
    settings.alpaca_paper = False
    settings.trading_allow_live = True
    b = AlpacaBroker()
    assert b.is_paper is False
    assert b.base == A_LIVE


def test_binance_safety_guard():
    settings.binance_key = "k"
    settings.binance_secret = "s"
    settings.binance_testnet = False
    settings.trading_allow_live = False
    b = BinanceBroker()
    assert b.is_testnet is True
    assert b.base == B_TEST


def test_binance_live_when_both_flags_set():
    settings.binance_key = "k"
    settings.binance_secret = "s"
    settings.binance_testnet = False
    settings.trading_allow_live = True
    b = BinanceBroker()
    assert b.is_testnet is False
    assert b.base == B_LIVE


def test_tinkoff_safety_guard():
    settings.tinkoff_token = "tok"
    settings.tinkoff_sandbox = False
    settings.trading_allow_live = False
    b = TinkoffBroker()
    assert b.is_sandbox is True


def test_factory_paper_mode():
    settings.trading_broker = "paper"
    reset_broker_cache()
    b = get_broker("us")
    assert b.name == "paper"


def test_factory_auto_falls_back_to_paper_without_keys():
    settings.trading_broker = "auto"
    settings.alpaca_key = ""
    settings.binance_key = ""
    settings.tinkoff_token = ""
    reset_broker_cache()
    assert get_broker("us").name == "paper"
    assert get_broker("ru").name == "paper"
    assert get_broker("crypto").name == "paper"


def test_factory_auto_routes_by_market():
    settings.trading_broker = "auto"
    settings.alpaca_key = "k"; settings.alpaca_secret = "s"
    settings.binance_key = "k"; settings.binance_secret = "s"
    settings.tinkoff_token = "tok"
    reset_broker_cache()
    assert get_broker("us").name == "alpaca"
    assert get_broker("ru").name == "tinkoff"
    assert get_broker("crypto").name == "binance"


def test_factory_unknown_mode_raises():
    settings.trading_broker = "nonsense"
    reset_broker_cache()
    with pytest.raises(BrokerError, match="unknown broker"):
        get_broker("us")
