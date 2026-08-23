"""Новости + LLM-сентимент по тикеру/теме.

Источники по умолчанию (RSS, без ключей):
  • Yahoo Finance per-symbol RSS
  • Investing.com general feed
  • Cointelegraph (для крипты)

Парсер — stdlib xml.etree, чтобы не тянуть feedparser.
Сентимент — через LLMClient (функция curator_quality), single-shot JSON-ответ.
"""
from __future__ import annotations

import asyncio
import json
import re
import xml.etree.ElementTree as ET
from typing import Literal

import httpx

from app.services.llm_client import LLMClient

Sentiment = Literal["bullish", "bearish", "neutral"]

_HEADERS = {
    "User-Agent": "cognitive-core-trading/1.0 (+https://aimail.art)",
    "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
}
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


def _yf_rss(symbol: str) -> str:
    return f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US"


_GENERAL_FEEDS = (
    "https://www.investing.com/rss/news.rss",
    "https://www.cointelegraph.com/rss",
)


async def fetch_headlines(symbol: str | None = None, limit: int = 10) -> list[dict]:
    """Возвращает свежие заголовки. Если symbol задан — приоритет per-symbol feed."""
    urls: list[str] = []
    if symbol:
        urls.append(_yf_rss(symbol.upper()))
    urls.extend(_GENERAL_FEEDS)

    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS, follow_redirects=True) as cli:
        results = await asyncio.gather(*(_fetch_one(cli, u) for u in urls), return_exceptions=True)

    items: list[dict] = []
    for res in results:
        if isinstance(res, list):
            items.extend(res)
    # дедуп по title
    seen = set()
    uniq = []
    for it in items:
        t = (it.get("title") or "").strip().lower()
        if t and t not in seen:
            seen.add(t)
            uniq.append(it)
    return uniq[:limit]


async def analyze_sentiment(symbol: str, headlines: list[dict] | None = None) -> dict:
    """Считает агрегированный сентимент по тикеру через LLM.

    Возвращает {symbol, sentiment, score, summary, headlines_count}.
    score: -1.0 (очень bearish) … +1.0 (очень bullish).
    """
    if headlines is None:
        headlines = await fetch_headlines(symbol, limit=10)
    if not headlines:
        return {
            "symbol": symbol,
            "sentiment": "neutral",
            "score": 0.0,
            "summary": "Свежих новостей не найдено",
            "headlines_count": 0,
        }

    titles = "\n".join(f"- {h.get('title')}" for h in headlines[:10])
    system = (
        "Ты — аналитик финансовых новостей. По заголовкам определи общий настрой "
        "рынка к активу. Отвечай строго JSON: "
        '{"sentiment":"bullish|bearish|neutral","score":число_от_-1_до_1,"summary":"одна фраза"}'
    )
    user = f"Актив: {symbol}\nЗаголовки:\n{titles}\n\nОтвет JSON:"

    llm = LLMClient("curator_quality")
    try:
        result = await llm.call(system, user, domain="trading")
    except Exception as e:
        return {
            "symbol": symbol,
            "sentiment": "neutral",
            "score": 0.0,
            "summary": f"LLM недоступен: {str(e)[:120]}",
            "headlines_count": len(headlines),
        }

    parsed = _coerce_sentiment(result)
    parsed["symbol"] = symbol
    parsed["headlines_count"] = len(headlines)
    return parsed


# ──────────────────────── helpers ────────────────────────
async def _fetch_one(cli: httpx.AsyncClient, url: str) -> list[dict]:
    try:
        r = await cli.get(url)
        if r.status_code != 200:
            return []
        return _parse_rss(r.text)
    except Exception:
        return []


def _parse_rss(xml_text: str) -> list[dict]:
    out: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    # RSS 2.0: channel/item/title|link|pubDate
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        if title:
            out.append({"title": title, "link": link, "published": pub})
    # Atom fallback: entry/title/link[@href]
    if not out:
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for entry in root.findall("a:entry", ns):
            title = (entry.findtext("a:title", default="", namespaces=ns) or "").strip()
            link_el = entry.find("a:link", ns)
            link = link_el.get("href") if link_el is not None else ""
            pub = (entry.findtext("a:updated", default="", namespaces=ns) or "").strip()
            if title:
                out.append({"title": title, "link": link, "published": pub})
    return out


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _coerce_sentiment(llm_result: dict) -> dict:
    """LLMClient возвращает dict; полезные поля могут быть в .content/.text."""
    raw = ""
    if isinstance(llm_result, dict):
        raw = llm_result.get("content") or llm_result.get("text") or json.dumps(llm_result, ensure_ascii=False)
    else:
        raw = str(llm_result)
    m = _JSON_RE.search(raw)
    if not m:
        return {"sentiment": "neutral", "score": 0.0, "summary": "не удалось распарсить ответ"}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"sentiment": "neutral", "score": 0.0, "summary": "невалидный JSON от LLM"}
    s = str(data.get("sentiment", "neutral")).lower()
    if s not in ("bullish", "bearish", "neutral"):
        s = "neutral"
    try:
        score = float(data.get("score", 0.0))
    except (TypeError, ValueError):
        score = 0.0
    score = max(-1.0, min(1.0, score))
    return {
        "sentiment": s,
        "score": score,
        "summary": str(data.get("summary", ""))[:300],
    }
