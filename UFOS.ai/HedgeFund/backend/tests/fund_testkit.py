"""Shared fixtures for the fund engine / API / export tests (paper trading
only): temp SQLite store, FakeFeed with synthetic bars/quotes/headlines, a
fake OpenRouter transport (core.llm.TRANSPORT) and a fixed clock.
Stdlib only."""
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

from strategy_platform.core import llm as llm_mod
from strategy_platform.core import params as P
from fund import store
from strategy_platform.execution.paper_broker import PaperBroker
from fund.engine import FundEngine
from strategy_platform.data.market_feed import FakeFeed
from strategy_platform.core.types import Bar
from newsroom import db

FRI = datetime(2026, 10, 2, 14, 0, tzinfo=timezone.utc)      # Friday, session open (10:00 EDT)
SAT = datetime(2026, 10, 3, 15, 0, tzinfo=timezone.utc)      # Saturday, closed
MON = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)      # Monday, session open

SYMS = ["SPY", "QQQ"]
PRICES = {"SPY": 500.0, "QQQ": 400.0}


def make_bars(last_close: float, n: int = 300, end: date = date(2026, 10, 1)):
    """Steady uptrend with a ~1 % daily range (ATR ≈ 1 %, vol scaler 1)."""
    bars = []
    start = last_close / (1.0 + 0.001) ** (n - 1)
    d = end - timedelta(days=n - 1)
    for i in range(n):
        c = start * (1.0 + 0.001) ** i
        bars.append(Bar(date=(d + timedelta(days=i)).isoformat(), open=c * 0.999, high=c * 1.005, low=c * 0.995,
                        close=c, volume=1e6))
    return bars


class FakeLLM:
    """Replacement for the OpenRouter transport: answers the sentiment and
    judge prompts with fixed JSON and counts the calls."""

    def __init__(self, action="LONG", confidence=0.9, cost=0.001):
        self.action = action
        self.confidence = confidence
        self.cost = cost
        self.calls = []
        self.hook = None          # optional callable(kind) run during a call

    async def __call__(self, messages, model, max_tokens, temperature):
        system = messages[0]["content"]
        kind = "sentiment" if "news sentiment" in system else "judge"
        self.calls.append(kind)
        if self.hook:
            self.hook(kind)
        if kind == "sentiment":
            data = [{"symbol": s, "score": 0.4, "confidence": 0.6, "summary": "Upbeat coverage."}
                    for s in SYMS + ["IWM", "TLT"]]
        else:
            data = [{"symbol": s, "action": self.action, "confidence": self.confidence,
                     "rationale": "Technical trend and sentiment agree."} for s in SYMS + ["IWM", "TLT"]]
        return json.dumps(data), model, {"cost_usd": self.cost, "prompt_tokens": 100, "completion_tokens": 20}


class EngineCase(unittest.TestCase):
    """Temp DB + FakeFeed + PaperBroker + FundEngine with a controllable clock."""

    watchlist = SYMS
    extra_params = {}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.assertTrue(db.init(os.path.join(self._tmp.name, "n.db"), legacy=""))
        self.now = FRI
        self.feed = FakeFeed(bars={s: make_bars(p) for s, p in PRICES.items()}, quotes=dict(PRICES))
        self.set_headlines()
        self.fake = FakeLLM()
        llm_mod.TRANSPORT = self.fake
        base = {"watchlist": list(self.watchlist), "fees_bps": 0.0, "min_hold_days": 0}
        base.update(self.extra_params)
        store.save_params(P.merge(None, base), "test")
        self.broker = PaperBroker(store, self.feed.quote, clock=lambda: self.now, dividends_fn=self.feed.dividends,
                                  bars_fn=self.feed.daily_bars)
        self.engine = FundEngine(self.feed, self.broker, store, clock=lambda: self.now, startup_delay=0.0)

    def tearDown(self):
        llm_mod.TRANSPORT = None
        db.close()
        self._tmp.cleanup()

    def set_headlines(self):
        ts = (self.now - timedelta(hours=2)).isoformat(timespec="seconds")
        self.feed.headlines = {s: [{"title": f"{s} rallies on strong data", "publisher": "Wire",
                                    "published_at": ts, "url": "https://example.com/a"}] for s in SYMS}

    def cycle(self, trigger="manual"):
        import asyncio
        return asyncio.run(self.engine.run_cycle(trigger))

    def qty(self, symbol):
        p = store.get_position(symbol)
        return p["qty"] if p else 0.0
