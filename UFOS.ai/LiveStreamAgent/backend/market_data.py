"""Background task that polls Yahoo Finance's chart API for the configured tickers."""
import asyncio
import logging
from datetime import datetime, timezone
from urllib.parse import quote

import aiohttp

try:
    import yfinance as yf
except Exception:
    yf = None

from config import MARKET_DATA_POLL_SECONDS, MarketSnapshot, TickerQuote, all_ticker_symbols

logger = logging.getLogger(__name__)

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; FedAgent/1.0)"}

ALL_SYMBOLS = all_ticker_symbols()


async def _fetch_chart_quote(session: aiohttp.ClientSession, symbol: str) -> TickerQuote:
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


def _extract_yfinance_quote(symbol: str) -> TickerQuote:
    if yf is None:
        raise RuntimeError("yfinance is not available")

    ticker = yf.Ticker(symbol)
    fi = ticker.fast_info or {}

    price = fi.get("lastPrice") or fi.get("regularMarketPrice") or fi.get("previousClose")
    previous_close = fi.get("previousClose") or fi.get("regularMarketPreviousClose")

    if (price is None or previous_close is None) and hasattr(ticker, "history"):
        hist = ticker.history(period="5d", interval="1d", auto_adjust=False)
        if not hist.empty:
            close_series = hist["Close"].dropna()
            if not close_series.empty:
                price = price or float(close_series.iloc[-1])
            if len(close_series) >= 2:
                previous_close = float(close_series.iloc[-2])
            elif len(close_series) == 1:
                previous_close = float(close_series.iloc[-1])

    if price is None:
        raise ValueError(f"no yfinance price for {symbol}")

    change_percent = None
    if previous_close not in (None, 0):
        try:
            change_percent = (float(price) - float(previous_close)) / float(previous_close) * 100
        except Exception:
            change_percent = None

    return TickerQuote(symbol=symbol, price=float(price), change_percent=change_percent)


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
            *(_fetch_chart_quote(session, symbol) for symbol in ALL_SYMBOLS),
            return_exceptions=True,
        )

        quotes: dict[str, TickerQuote] = {}
        missing_symbols: list[str] = []
        for symbol, result in zip(ALL_SYMBOLS, results):
            if isinstance(result, Exception):
                logger.warning("Failed to fetch chart quote for %s: %s", symbol, result)
                missing_symbols.append(symbol)
                continue
            quotes[symbol] = result

        if missing_symbols and yf is not None:
            yf_results = await asyncio.gather(
                *(asyncio.to_thread(_extract_yfinance_quote, symbol) for symbol in missing_symbols),
                return_exceptions=True,
            )
            for symbol, result in zip(missing_symbols, yf_results):
                if isinstance(result, Exception):
                    logger.warning("Failed to fetch yfinance fallback for %s: %s", symbol, result)
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
