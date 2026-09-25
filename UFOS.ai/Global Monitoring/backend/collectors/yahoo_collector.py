"""Yahoo Finance chart API scraping — market quotes.

Free, no API key, ~15 minutes delayed (the "alter Yahoo scraping" reused from
LiveStreamAgent/backend/market_data.py — same endpoint and header pattern).
Each tracked index is pinned to its exchange's financial-center coordinates
(see geo.FINANCIAL_CENTERS) for the Markets layer.
"""
import asyncio
import logging
from urllib.parse import quote

import aiohttp

from config import YAHOO_POLL_SECONDS
from events import make_event
from geo import FINANCIAL_CENTERS

logger = logging.getLogger(__name__)

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; UFOSaiGlobalMonitoring/1.0)"}


async def _fetch_quote(session: aiohttp.ClientSession, symbol: str) -> dict:
    url = CHART_URL.format(symbol=quote(symbol))
    async with session.get(url, headers=HEADERS, params={"interval": "1d", "range": "1d"}, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        raise ValueError(f"no chart result for {symbol}")
    meta = result[0].get("meta", {})
    price = meta.get("regularMarketPrice")
    previous_close = meta.get("chartPreviousClose") or meta.get("previousClose")
    change_pct = None
    if price is not None and previous_close:
        change_pct = (price - previous_close) / previous_close * 100
    return {"price": price, "change_percent": change_pct}


async def _poll_once(session: aiohttp.ClientSession, emit, report_status) -> None:
    placed = 0
    errors = []
    for symbol, center in FINANCIAL_CENTERS.items():
        try:
            quote_data = await _fetch_quote(session, symbol)
            price = quote_data["price"]
            change_pct = quote_data["change_percent"]
            change_str = f"{change_pct:+.2f}%" if change_pct is not None else "n/a"
            event = make_event(
                event_id=f"markets-{symbol}",
                category="markets",
                title=f"{center['label']} ({center['name']})",
                summary=f"Last: {price}, change: {change_str} (delayed ~15min, Yahoo)",
                source="Yahoo Finance",
                lat=center["lat"], lon=center["lon"],
                url=f"https://finance.yahoo.com/quote/{quote(symbol)}",
            )
            emit(event)
            placed += 1
        except Exception as ex:
            errors.append(f"{symbol}: {ex}")
            logger.warning("Yahoo quote %s failed: %s", symbol, ex)

    if errors and placed == 0:
        await report_status(False, "; ".join(errors))
    else:
        await report_status(True, f"{placed}/{len(FINANCIAL_CENTERS)} quotes updated" + (f" ({len(errors)} failed)" if errors else ""))


async def run(emit, report_status) -> None:
    async with aiohttp.ClientSession() as session:
        while True:
            await _poll_once(session, emit, report_status)
            await asyncio.sleep(YAHOO_POLL_SECONDS)
