"""Регистрирует trading-инструменты в L3 (tools_registry).

Запускать из контейнера api:
  docker exec cognitive_api python scripts/seed_trading_tools.py
"""
import asyncio

from app.db.postgres import close_db, init_db
from app.models.tools import ToolRegistryInput
from app.services.tools import get_active_tools, register_tool

DOMAIN = "trading"

TOOLS = [
    ToolRegistryInput(
        domain=DOMAIN,
        tool_name="market_data",
        tool_type="service",
        description="Котировки и история по US/RU/crypto активам (Yahoo, MOEX, CoinGecko).",
        config_schema={
            "endpoints": [
                "POST /trading/quote   {symbol, market}",
                "POST /trading/history {symbol, market, days}",
            ],
            "markets": ["us", "ru", "crypto"],
            "free": True,
        },
        usage_patterns={
            "когда": "перед любым решением о сделке или при анализе позиции",
            "пример": {"symbol": "SBER", "market": "ru"},
        },
    ),
    ToolRegistryInput(
        domain=DOMAIN,
        tool_name="news_sentiment",
        tool_type="service",
        description="RSS-новости + LLM-сентимент по тикеру (bullish/bearish/neutral, score -1..+1).",
        config_schema={
            "endpoints": [
                "POST /trading/news      {symbol?, limit}",
                "POST /trading/sentiment {symbol}",
            ],
            "sources": ["yahoo-finance-rss", "investing.com", "cointelegraph"],
        },
        usage_patterns={
            "когда": "для подтверждения сигнала рынка контекстом новостей",
            "пример": {"symbol": "BTC"},
        },
    ),
    ToolRegistryInput(
        domain=DOMAIN,
        tool_name="risk_manager",
        tool_type="library",
        description="Жёсткие лимиты ДО исполнения: max-позиция, stop-loss, дневная просадка.",
        config_schema={
            "module": "app.services.trading.risk",
            "defaults": {
                "max_position_pct": 10.0,
                "max_stop_pct": 2.0,
                "max_daily_drawdown_pct": 5.0,
                "max_day_trades": 20,
            },
        },
        usage_patterns={
            "когда": "вызывается автоматически внутри broker.submit_order",
            "вручную": "evaluate(OrderRequest(...), Portfolio(...))",
        },
    ),
    ToolRegistryInput(
        domain=DOMAIN,
        tool_name="paper_broker",
        tool_type="service",
        description="Симулятор торговли. Старт-капитал 100000, состояние в Redis.",
        config_schema={
            "endpoints": [
                "GET  /trading/portfolio",
                "POST /trading/order            {symbol, market, side, quantity, stop_loss?}",
                "GET  /trading/orders?limit=50",
                "POST /trading/portfolio/reset  {cash?}",
            ],
            "broker": "paper",
            "real_brokers_supported": ["tinkoff", "alpaca", "binance"],
            "real_brokers_status": "адаптеры подключаются по запросу + API-ключи",
        },
        usage_patterns={
            "роль": "по умолчанию — тренировка стратегий без денежного риска",
            "переключение_на_реальный": "settings.trading_broker = 'tinkoff' и т.п.",
        },
    ),
]


async def main():
    await init_db()
    existing = {t["tool_name"] for t in await get_active_tools(DOMAIN)}
    added = 0
    for tool in TOOLS:
        if tool.tool_name in existing:
            print(f"  skip  {tool.tool_name} (уже зарегистрирован)")
            continue
        tid = await register_tool(tool)
        print(f"  add   {tool.tool_name}  id={tid}")
        added += 1
    print(f"\nDone. Added {added}, total in domain '{DOMAIN}': {len(existing) + added}")
    await close_db()


if __name__ == "__main__":
    asyncio.run(main())
