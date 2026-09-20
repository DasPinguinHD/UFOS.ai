#!/usr/bin/env python3
"""Debug all configured market-data tickers against multiple public sources.

Usage examples:
  python .\tools\debug_tickers.py
  python .\tools\debug_tickers.py --symbols BIL,UUP,GLD
  python .\tools\debug_tickers.py --source yfinance

The script prints per-symbol results for the configured tickers, tries multiple sources
(quote, chart, quoteSummary, Stooq, yfinance), and highlights which source produced a
usable price / change result.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

try:
    from config import TICKERS, all_ticker_symbols, iter_ticker_groups
except Exception as ex:  # pragma: no cover - import-time diagnostics
    print(f"ERROR importing backend config: {ex}", file=sys.stderr)
    raise

try:
    import yfinance as yf
except Exception:
    yf = None

YAHOO_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_SUMMARY_URL = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
STOOQ_URL = "https://stooq.com/q/d/l/"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; FedTraderDebug/1.0)"}


@dataclass
class SourceResult:
    source: str
    symbol: str
    price: float | None
    change_percent: float | None
    note: str = ""

    def usable(self) -> bool:
        return self.price not in (None, 0)


def http_get_json(url: str, params: dict[str, str]) -> dict[str, Any]:
    qs = urlencode(params)
    full_url = f"{url}?{qs}" if qs else url
    req = Request(full_url, headers=HEADERS)
    with urlopen(req, timeout=20) as resp:
        data = resp.read().decode("utf-8", errors="replace")
    return json.loads(data)


def http_get_text(url: str, params: dict[str, str]) -> str:
    qs = urlencode(params)
    full_url = f"{url}?{qs}" if qs else url
    req = Request(full_url, headers=HEADERS)
    with urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


def extract_number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        raw = value.get("raw")
        if isinstance(raw, (int, float)):
            return float(raw)
        fmt = value.get("fmt")
        if isinstance(fmt, str):
            try:
                return float(fmt.replace(",", ""))
            except Exception:
                return None
    if isinstance(value, str):
        try:
            return float(value.replace(",", ""))
        except Exception:
            return None
    return None


def try_yahoo_quote(symbol: str) -> SourceResult:
    data = http_get_json(YAHOO_QUOTE_URL, {"symbols": symbol})
    result = (data.get("quoteResponse") or {}).get("result") or []
    if not result:
        return SourceResult("yahoo_quote", symbol, None, None, "no result")
    item = result[0]
    price = extract_number(item.get("regularMarketPrice"))
    change_percent = extract_number(item.get("regularMarketChangePercent"))
    if price is None and item.get("postMarketPrice") is not None:
        price = extract_number(item.get("postMarketPrice"))
    return SourceResult("yahoo_quote", symbol, price, change_percent)


def try_yahoo_chart(symbol: str) -> SourceResult:
    data = http_get_json(YAHOO_CHART_URL.format(symbol=symbol), {"interval": "1d", "range": "1d"})
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        return SourceResult("yahoo_chart", symbol, None, None, "no result")
    meta = result[0].get("meta", {})
    price = extract_number(meta.get("regularMarketPrice"))
    previous_close = extract_number(meta.get("chartPreviousClose") or meta.get("previousClose"))
    change_percent = None
    if price is not None and previous_close not in (None, 0):
        change_percent = (price - previous_close) / previous_close * 100
    return SourceResult("yahoo_chart", symbol, price, change_percent)


def try_yahoo_summary(symbol: str) -> SourceResult:
    data = http_get_json(YAHOO_SUMMARY_URL.format(symbol=symbol), {"modules": "price,summaryDetail"})
    result = (data.get("quoteSummary") or {}).get("result") or []
    if not result:
        return SourceResult("yahoo_summary", symbol, None, None, "no result")
    info = result[0]
    price_info = info.get("price") or {}
    summary_info = info.get("summaryDetail") or {}
    price = extract_number(price_info.get("regularMarketPrice"))
    previous_close = extract_number(price_info.get("regularMarketPreviousClose"))
    if previous_close is None:
        previous_close = extract_number(summary_info.get("previousClose"))
    change_percent = extract_number(price_info.get("regularMarketChangePercent"))
    if change_percent is None and price is not None and previous_close not in (None, 0):
        change_percent = (price - previous_close) / previous_close * 100
    return SourceResult("yahoo_summary", symbol, price, change_percent)


def stooq_symbol(symbol: str) -> str | None:
    if symbol.startswith("^"):
        return None
    return f"{symbol.lower()}.us"


def try_stooq(symbol: str) -> SourceResult:
    mapped = stooq_symbol(symbol)
    if not mapped:
        return SourceResult("stooq", symbol, None, None, "no mapping")
    text = http_get_text(STOOQ_URL, {"s": mapped, "i": "d"})
    rows = list(csv.DictReader(StringIO(text)))
    if not rows:
        return SourceResult("stooq", symbol, None, None, "no rows")
    row = rows[-1]
    price = None
    for key in ("Close", "close"):
        val = row.get(key)
        if val not in (None, "", "N/D"):
            try:
                price = float(val)
                break
            except Exception:
                pass
    return SourceResult("stooq", symbol, price, None, "csv")


def try_yfinance(symbol: str) -> SourceResult:
    if yf is None:
        return SourceResult("yfinance", symbol, None, None, "module missing")
    try:
        ticker = yf.Ticker(symbol)
        fi = ticker.fast_info or {}
        price = fi.get("lastPrice") or fi.get("regularMarketPrice") or fi.get("previousClose")
        prev = fi.get("previousClose") or fi.get("regularMarketPreviousClose")
        if prev is None:
            hist = ticker.history(period="5d", interval="1d", auto_adjust=False)
            if not hist.empty:
                close_series = hist["Close"].dropna()
                if not close_series.empty:
                    price = price or float(close_series.iloc[-1])
                if len(close_series) >= 2:
                    prev = float(close_series.iloc[-2])
                elif len(close_series) == 1:
                    prev = float(close_series.iloc[-1])
        change_percent = None
        if price is not None and prev not in (None, 0):
            change_percent = (float(price) - float(prev)) / float(prev) * 100
        return SourceResult("yfinance", symbol, float(price) if price is not None else None, change_percent)
    except Exception as ex:
        return SourceResult("yfinance", symbol, None, None, type(ex).__name__)


def summarize_result(res: SourceResult) -> str:
    price = "None" if res.price is None else f"{res.price:.6g}"
    change = "None" if res.change_percent is None else f"{res.change_percent:+.4f}%"
    extra = f" [{res.note}]" if res.note else ""
    return f"{res.source}: price={price} change={change}{extra}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Debug all configured market-data tickers.")
    parser.add_argument("--symbols", help="Comma-separated symbol list to debug instead of all configured tickers")
    parser.add_argument("--source", choices=["all", "yahoo", "stooq", "yfinance"], default="all", help="Limit the sources to test")
    args = parser.parse_args()

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = all_ticker_symbols()

    print("CONFIGURED GROUPS:")
    for section_name, group_name, group_symbols in iter_ticker_groups():
        print(f"  {section_name}/{group_name}: {', '.join(group_symbols)}")
    print()

    print(f"YFINANCE_MODULE: {'available' if yf is not None else 'missing'}")
    print()

    source_order = ["yahoo_quote", "yahoo_chart", "yahoo_summary", "stooq", "yfinance"]
    if args.source == "yahoo":
        source_order = ["yahoo_quote", "yahoo_chart", "yahoo_summary"]
    elif args.source == "stooq":
        source_order = ["stooq"]
    elif args.source == "yfinance":
        source_order = ["yfinance"]

    for symbol in symbols:
        print(f"== {symbol} ==")
        collected: list[SourceResult] = []
        for source in source_order:
            try:
                if source == "yahoo_quote":
                    res = try_yahoo_quote(symbol)
                elif source == "yahoo_chart":
                    res = try_yahoo_chart(symbol)
                elif source == "yahoo_summary":
                    res = try_yahoo_summary(symbol)
                elif source == "stooq":
                    res = try_stooq(symbol)
                else:
                    res = try_yfinance(symbol)
            except (HTTPError, URLError) as ex:
                res = SourceResult(source, symbol, None, None, type(ex).__name__)
            except Exception as ex:
                res = SourceResult(source, symbol, None, None, type(ex).__name__)

            collected.append(res)
            print("  " + summarize_result(res))

        usable = next((r for r in collected if r.usable()), None)
        if usable:
            print(f"  => usable via {usable.source}: price={usable.price:.6g} change={(f'{usable.change_percent:+.4f}%' if usable.change_percent is not None else 'None')}")
        else:
            print("  => NO USABLE DATA")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
