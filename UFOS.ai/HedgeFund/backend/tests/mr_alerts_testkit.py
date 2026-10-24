"""Shared fakes for the MR7 alert tests (no network)."""
import asyncio
from datetime import datetime, timedelta, timezone

from market_research import store
from market_research import drawings as dr
from market_research.alerts import AlertEngine
from market_research.assets import SymbolNotCovered
from market_research.hub.hub import MarketDataHub

UTC = timezone.utc
NOW = datetime(2026, 10, 15, 15, 0, tzinfo=UTC)      # Thursday, market open


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now = self.now + timedelta(**kw)


class AnyData:
    """data.asset() for any syntactically valid symbol except ZZZZ (not covered)."""

    def asset(self, symbol):
        if symbol == "ZZZZ":
            raise SymbolNotCovered("Not available from Alpaca: ZZZZ is not in Alpaca's list of active US equities.")
        return {"symbol": symbol}


def ts(sec):
    return (NOW + timedelta(seconds=sec)).isoformat(timespec="seconds")


def tr(sym, price, sec):
    return {"symbol": sym, "price": price, "size": 1.0, "ts": NOW + timedelta(seconds=sec)}


def alert_line(symbol="AAPL", p=100.0):
    return dr.create({"symbol": symbol, "tool": "alertLine", "points": [{"t": "2026-10-14T14:00:00+00:00", "p": p}]},
                     now=NOW)


def engine(clock=None, hub=None, prev=None, day=None):
    hub = hub or MarketDataHub(now=clock or Clock())
    kw = {}
    if prev is not None:
        kw["prev_close_fn"] = prev
    if day is not None:
        kw["day_fn"] = day
    eng = AlertEngine(hub, lambda: None, now=clock or Clock(), **kw)
    eng.running = True
    return eng


def run(c):
    return asyncio.run(c)
