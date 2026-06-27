"""Котировки и история цен — бесплатные публичные источники.

Поддерживается три рынка:
  • us     → Yahoo Finance (query2.finance.yahoo.com), без ключей
  • ru     → MOEX ISS (iss.moex.com), без ключей
  • crypto → CoinGecko (api.coingecko.com), без ключей (есть rate-limit)

Без сторонних SDK — только httpx. Если источник недоступен, поднимаем
MarketDataError, чтобы вызывающий слой принял решение (кэш, fallback,
сообщение пользователю).
"""
from __future__ import annotations

from typing import Literal

import httpx

Market = Literal["us", "ru", "crypto"]

_YF_QUOTE = "https://query2.finance.yahoo.com/v7/finance/quote"
_YF_CHART = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
_MOEX_TICKER = (
    "https://iss.moex.com/iss/engines/stock/markets/shares/securities/"
    "{symbol}.json?iss.meta=off"
)
_MOEX_HISTORY = (
    "https://iss.moex.com/iss/history/engines/stock/markets/shares/securities/"
    "{symbol}.json?iss.meta=off&limit={limit}&sort_order=desc"
)
_CG_SIMPLE = "https://api.coingecko.com/api/v3/simple/price"
_CG_HISTORY = "https://api.coingecko.com/api/v3/coins/{coin}/market_chart"

_HEADERS = {
    "User-Agent": "cognitive-core-trading/1.0 (+https://aimail.art)",
    "Accept": "application/json",
}
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class MarketDataError(RuntimeError):
    """Не удалось получить данные у провайдера."""


async def get_quote(symbol: str, market: Market) -> dict:
    """Текущая цена + базовые поля для одного тикера.

    Возвращает словарь:
      {symbol, market, price, currency, change_pct, source, ts}
    """
    sym = symbol.strip().upper()
    if market == "us":
        return await _yf_quote(sym)
    if market == "ru":
        return await _moex_quote(sym)
    if market == "crypto":
        return await _cg_quote(sym)
    raise MarketDataError(f"unknown market: {market}")


async def get_history(symbol: str, market: Market, days: int = 30) -> list[dict]:
    """История дневных цен. Возвращает список {date, open, high, low, close, volume}."""
    sym = symbol.strip().upper()
    days = max(1, min(int(days), 365))
    if market == "us":
        return await _yf_history(sym, days)
    if market == "ru":
        return await _moex_history(sym, days)
    if market == "crypto":
        return await _cg_history(sym, days)
    raise MarketDataError(f"unknown market: {market}")


# ────────────────────────────── Yahoo (US) ──────────────────────────────
async def _yf_quote(symbol: str) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS) as cli:
        r = await cli.get(_YF_QUOTE, params={"symbols": symbol})
    if r.status_code != 200:
        raise MarketDataError(f"yahoo status {r.status_code}")
    arr = (r.json().get("quoteResponse") or {}).get("result") or []
    if not arr:
        raise MarketDataError(f"yahoo: no data for {symbol}")
    q = arr[0]
    return {
        "symbol": symbol,
        "market": "us",
        "price": q.get("regularMarketPrice"),
        "currency": q.get("currency", "USD"),
        "change_pct": q.get("regularMarketChangePercent"),
        "source": "yahoo",
        "ts": q.get("regularMarketTime"),
    }


async def _yf_history(symbol: str, days: int) -> list[dict]:
    rng = "1mo" if days <= 30 else ("3mo" if days <= 90 else ("6mo" if days <= 180 else "1y"))
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS) as cli:
        r = await cli.get(_YF_CHART.format(symbol=symbol), params={"interval": "1d", "range": rng})
    if r.status_code != 200:
        raise MarketDataError(f"yahoo chart status {r.status_code}")
    res = (r.json().get("chart") or {}).get("result") or []
    if not res:
        raise MarketDataError(f"yahoo chart: empty for {symbol}")
    block = res[0]
    ts_list = block.get("timestamp") or []
    quote = (block.get("indicators") or {}).get("quote", [{}])[0]
    out = []
    for i, ts in enumerate(ts_list[-days:]):
        out.append({
            "ts": ts,
            "open": _get(quote.get("open"), i),
            "high": _get(quote.get("high"), i),
            "low": _get(quote.get("low"), i),
            "close": _get(quote.get("close"), i),
            "volume": _get(quote.get("volume"), i),
        })
    return out


# ────────────────────────────── MOEX (RU) ───────────────────────────────
async def _moex_quote(symbol: str) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS) as cli:
        r = await cli.get(_MOEX_TICKER.format(symbol=symbol))
    if r.status_code != 200:
        raise MarketDataError(f"moex status {r.status_code}")
    data = r.json()
    rows = data.get("marketdata", {}).get("data") or []
    cols = data.get("marketdata", {}).get("columns") or []
    if not rows:
        raise MarketDataError(f"moex: no data for {symbol}")
    row = dict(zip(cols, rows[0]))
    price = row.get("LAST") or row.get("LCURRENTPRICE") or row.get("MARKETPRICE")
    return {
        "symbol": symbol,
        "market": "ru",
        "price": price,
        "currency": "RUB",
        "change_pct": row.get("LASTTOPREVPRICE"),
        "source": "moex",
        "ts": row.get("SYSTIME"),
    }


async def _moex_history(symbol: str, days: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS) as cli:
        r = await cli.get(_MOEX_HISTORY.format(symbol=symbol, limit=days))
    if r.status_code != 200:
        raise MarketDataError(f"moex hist status {r.status_code}")
    block = r.json().get("history", {})
    cols = block.get("columns") or []
    rows = block.get("data") or []
    out = []
    for raw in rows:
        rec = dict(zip(cols, raw))
        out.append({
            "date": rec.get("TRADEDATE"),
            "open": rec.get("OPEN"),
            "high": rec.get("HIGH"),
            "low": rec.get("LOW"),
            "close": rec.get("CLOSE") or rec.get("LEGALCLOSEPRICE"),
            "volume": rec.get("VOLUME"),
        })
    return list(reversed(out))


# ────────────────────────────── CoinGecko (crypto) ──────────────────────
# Используем coin_id (например "bitcoin", "ethereum"). Принимаем как тикер
# (BTC → bitcoin) через простой mapping для популярных активов; для всего
# остального — passthrough.
_CG_SYMBOL_MAP = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "TON": "the-open-network",
    "BNB": "binancecoin", "XRP": "ripple", "ADA": "cardano", "DOGE": "dogecoin",
    "AVAX": "avalanche-2", "MATIC": "matic-network", "LINK": "chainlink",
    "TRX": "tron", "DOT": "polkadot", "USDT": "tether", "USDC": "usd-coin",
}


def _cg_id(symbol: str) -> str:
    return _CG_SYMBOL_MAP.get(symbol.upper(), symbol.lower())


async def _cg_quote(symbol: str) -> dict:
    coin = _cg_id(symbol)
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS) as cli:
        r = await cli.get(_CG_SIMPLE, params={
            "ids": coin, "vs_currencies": "usd",
            "include_24hr_change": "true", "include_last_updated_at": "true",
        })
    if r.status_code != 200:
        raise MarketDataError(f"coingecko status {r.status_code}")
    data = r.json().get(coin) or {}
    if not data:
        raise MarketDataError(f"coingecko: unknown coin {symbol}")
    return {
        "symbol": symbol.upper(),
        "market": "crypto",
        "price": data.get("usd"),
        "currency": "USD",
        "change_pct": data.get("usd_24h_change"),
        "source": "coingecko",
        "ts": data.get("last_updated_at"),
    }


async def _cg_history(symbol: str, days: int) -> list[dict]:
    coin = _cg_id(symbol)
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS) as cli:
        r = await cli.get(
            _CG_HISTORY.format(coin=coin),
            params={"vs_currency": "usd", "days": str(days)},
        )
    if r.status_code != 200:
        raise MarketDataError(f"coingecko hist status {r.status_code}")
    prices = r.json().get("prices") or []
    out = []
    for ts_ms, price in prices:
        out.append({"ts": int(ts_ms / 1000), "close": price})
    return out


def _get(arr, i):
    if arr is None or i >= len(arr):
        return None
    return arr[i]
