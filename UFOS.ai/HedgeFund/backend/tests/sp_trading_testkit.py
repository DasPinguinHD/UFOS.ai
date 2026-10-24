"""Fixtures for the stage-7 tests (SPEC §17): temp hedgefund.db + temp newsroom.db
(never the real databases), a mutable fake quote source, a controllable clock and helpers
to create strategies in a given lifecycle state. Offline. Part of UFOS.ai (paper only)."""
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from strategy_platform.core.types import Quote
from newsroom import db as newsroom_db
from strategy_platform.execution import accounts, control, oms
from strategy_platform.live import runtime
from strategy_platform.storage import db

FRI = datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc)     # Friday 11:00 NY, session open
SAT = datetime(2026, 10, 3, 15, 0, tzinfo=timezone.utc)     # Saturday


class Quotes:
    def __init__(self, prices=None, clock=None):
        self.prices = dict(prices or {"SPY": 500.0, "QQQ": 400.0, "TLT": 90.0, "BTC-USD": 60000.0})
        self.prev = {}
        self.clock = clock
        self.fail = set()
        self.calls = 0

    def __call__(self, symbol):
        self.calls += 1
        if symbol in self.fail:
            raise RuntimeError("quote source down")
        p = self.prices.get(symbol)
        if p is None:
            return None
        ts = (self.clock() if self.clock else FRI).isoformat(timespec="seconds")
        return Quote(symbol=symbol, price=float(p), prev_close=self.prev.get(symbol, float(p)), ts=ts)


class TradingCase(unittest.TestCase):
    """hedgefund.db + newsroom.db in a temp dir, accounts configured with fakes."""
    now = FRI
    open_main = True
    capital = 100000.0

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sp7-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        db.close()
        db.init(os.path.join(self.tmp, "hedgefund.db"))
        self.addCleanup(db.close)
        self.assertTrue(newsroom_db.init(os.path.join(self.tmp, "newsroom.db"), legacy=""))
        self.addCleanup(newsroom_db.close)
        self.clock_now = [self.now]
        self.quotes = Quotes(clock=self.clock)
        accounts.configure(quote_fn=self.quotes, clock=self.clock)
        self.addCleanup(accounts.configure)
        oms._sleep = lambda s: None
        self.addCleanup(setattr, oms, "_sleep", __import__("time").sleep)
        control.set_legacy_engine(None)
        self.addCleanup(control.set_legacy_engine, None)
        control._last_seen = None
        self.rt = runtime.Runtime()
        runtime.set_default(self.rt)
        self.addCleanup(runtime.set_default, None)
        if self.open_main:
            self.acc = accounts.open_main(self.capital)

    def clock(self):
        return self.clock_now[0]

    def set_now(self, dt):
        self.clock_now[0] = dt

    def advance(self, **kw):
        self.clock_now[0] = self.clock_now[0] + timedelta(**kw)

    def manual(self, symbol, side, qty, **kw):
        intent = {"symbol": symbol, "side": side, "qty": qty, "origin": "manual",
                  "source_id": kw.pop("source_id", "_unallocated")}
        intent.update(kw)
        o, _c = oms.place(self.acc, intent)
        from strategy_platform.portfolio import ledger
        ledger.attribute(self.acc)
        return o


def make_strategy(symbols=("SPY", "QQQ"), state="validated", name="sp7 test", **spec_kw):
    """A user strategy moved straight to ``state`` (test shortcut: db.strategy_set_state)."""
    from strategy_platform.lifecycle import registry
    spec = {"schema": "ufos.strategy/1", "name": name, "universe": {"symbols": list(symbols)},
            "signal": {"field": "close"},
            "portfolio": {"mode": "long_only", "selection": {"type": "all"}, "weighting": "equal",
                          "max_weight": 1.0, "gross_exposure": 1.0},
            "rebalance": {"frequency": "daily"}, "execution": {"order_type": "market", "timing": "next_open"}}
    spec.update(spec_kw)
    created = registry.create_from_text(json.dumps(spec), "user")
    sid = created["id"]
    if state != "draft":
        row = db.strategy_get(sid)
        t = {"ts": db.utc_now_iso(), "strategy_id": sid, "version": row["current_version"], "from_state": "draft",
             "to_state": state, "actor": "system", "reason": "test shortcut", "evidence_json": None}
        assert db.strategy_set_state(sid, "draft", t, ("system", "strategy_transition", {"test": True}))
    return sid


EXPECTED = {"mean_daily": 0.0005, "std_daily": 0.01, "vol": 0.1587, "max_drawdown": 0.05,
            "source_run_id": "bt-test", "sessions": 250, "calendar": "nyse",
            "returns": [[(datetime(2025, 1, 1) + timedelta(days=i)).date().isoformat(), ((i * 7) % 11 - 5) / 1000.0]
                        for i in range(60)]}
