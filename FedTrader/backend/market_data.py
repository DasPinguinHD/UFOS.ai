"""Background task that polls Yahoo Finance's chart API for the configured tickers."""
import asyncio
import logging
from datetime import datetime, timezone
from urllib.parse import quote

import aiohttp

from config import MARKET_DATA_POLL_SECONDS, TICKERS, MarketSnapshot, TickerQuote

logger = logging.getLogger(__name__)

# Per-symbol JSON endpoint avoids the ambiguity of scraping quote pages, which embed
# multiple elements sharing the same data-field attributes (nav strips, related quotes, etc.).
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; FedAgent/1.0)"}

ALL_SYMBOLS = sorted({symbol for symbols in TICKERS.values() for symbol in symbols})


async def _fetch_quote(session: aiohttp.ClientSession, symbol: str) -> TickerQuote:
    url = CHART_URL.format(symbol=quote(symbol))
    async with session.get(
        url, headers=HEADERS, params={"interval": "1d", "range": "1d"}, timeout=aiohttp.ClientTimeout(total=10)
    ) as resp:
        data = await resp.json(content_type=None)

    result = (data.get("chart") or {}).get("result") or []
    if not result:
        raise ValueError(f"no chart result for {symbol}: {data.get('chart', {}).get('error')}")

    meta = result[0].get("meta", {})
    price = meta.get("regularMarketPrice")
    previous_close = meta.get("chartPreviousClose") or meta.get("previousClose")

    change_percent = None
    if price is not None and previous_close:
        change_percent = (price - previous_close) / previous_close * 100

    return TickerQuote(symbol=symbol, price=price, change_percent=change_percent)


class MarketDataFeed:
    """Polls Yahoo Finance on a fixed interval and exposes the latest snapshot thread-safely."""

    def __init__(self) -> None:
        self._snapshot = MarketSnapshot()
        self._lock = asyncio.Lock()

    async def get_snapshot(self) -> MarketSnapshot:
        async with self._lock:
            return MarketSnapshot(quotes=dict(self._snapshot.quotes), timestamp=self._snapshot.timestamp)

    async def _refresh_once(self, session: aiohttp.ClientSession) -> None:
        results = await asyncio.gather(
            *(_fetch_quote(session, symbol) for symbol in ALL_SYMBOLS),
            return_exceptions=True,
        )
        quotes: dict[str, TickerQuote] = {}
        for symbol, result in zip(ALL_SYMBOLS, results):
            if isinstance(result, Exception):
                logger.warning("Failed to fetch quote for %s: %s", symbol, result)
                continue
            quotes[symbol] = result

        async with self._lock:
            self._snapshot = MarketSnapshot(quotes=quotes, timestamp=datetime.now(timezone.utc).isoformat())

    async def run(self) -> None:
        # Yahoo sends a CSP header larger than aiohttp's 8KB default line/field limit.
        async with aiohttp.ClientSession(max_line_size=32768, max_field_size=32768) as session:
            while True:
                await self._refresh_once(session)
                await asyncio.sleep(MARKET_DATA_POLL_SECONDS)
