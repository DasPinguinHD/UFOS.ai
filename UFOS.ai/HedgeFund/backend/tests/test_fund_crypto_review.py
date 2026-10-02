"""Crypto review fixes (2026-09-27, fund/SPEC.md §3, §5, §6, §9, §10, §11,
§12): perp maintenance strictly below initial and margin measured against
the entry quote (a fresh short never starts below maintenance), funding
booked before any fill that changes a short (atomic, deduplicated per
window), per-class trade counters in the backtest, effective_from only
counting a risk watch that will run, the risk watch filling the user's
queued crypto orders and expiring stale autopilot ones, stale fallback
quotes never liquidating, and strict qty validation. Paper trading only.

Run from HedgeFund/backend:  python -m unittest tests.test_fund_crypto_review -v
"""
import asyncio
import random
import unittest
from datetime import date, datetime, timedelta, timezone

try:
    from crypto_testkit import BTC, ETH, CryptoCase
    from fund_testkit import FRI, SAT
except ImportError:  # pragma: no cover - run as package
    from tests.crypto_testkit import BTC, ETH, CryptoCase
    from tests.fund_testkit import FRI, SAT

from fund import market
from fund import params as P
from fund import store
from fund.backtest import run_backtest
from fund.engine import EngineError
from fund.market import YFinanceFeed
from fund.types import Bar, Quote


class PerpMarginFixTest(CryptoCase):
    def test_maintenance_must_be_strictly_below_initial(self):
        _c, errors = P.validate({"crypto_initial_margin_pct": 50, "crypto_maintenance_margin_pct": 50})
        self.assertIn("crypto_maintenance_margin_pct", errors)
        _c, errors = P.validate({"crypto_initial_margin_pct": 50, "crypto_maintenance_margin_pct": 49})
        self.assertEqual(errors, {})
        _c, errors = P.validate({"initial_margin_pct": 30, "maintenance_margin_pct": 30})    # Reg T: <= stays
        self.assertEqual(errors, {})

    def test_fresh_short_never_starts_below_maintenance(self):
        self.set_params(crypto_fees_bps=20.0, crypto_initial_margin_pct=50.0, crypto_maintenance_margin_pct=49.0)
        self.feed.quotes[BTC] = 100000.0
        self.now = SAT
        self.assertEqual(self.order(BTC, "short", qty=0.2).status, "filled")
        acct = self.broker.account(now=self.now)
        self.assertAlmostEqual(acct.crypto_margin_excess, 0.01 * 20000, places=6)   # (im - mm) x notional
        self.assertEqual(self.broker.check_margin(self.now), [])
        self.assertEqual(self.qty(BTC), -0.2)


class FundingFixTest(CryptoCase):
    def setUp(self):
        super().setUp()
        self.broker.ensure_initialized(FRI)
        self.order(BTC, "short", qty=0.5)                         # FRI 14:00
        self.now = SAT                                            # 3 windows due, not yet accrued

    def test_cover_books_due_funding_first(self):
        self.assertEqual(self.order(BTC, "cover", qty=0.5).status, "filled")
        self.assertEqual(len(self.ledger("funding")), 3)
        self.assertAlmostEqual(sum(r["amount"] for r in self.ledger("funding")), 7.5)
        out = self.broker.daily_accruals(self.now)                 # nothing twice
        self.assertEqual(out["funding_windows"], 0)
        self.assertEqual(len(self.ledger("funding")), 3)
        self.assertEqual(store.get_last_funding_at(), SAT.replace(hour=8).isoformat())

    def test_kill_switch_and_liquidation_book_funding(self):
        asyncio.run(self.engine.kill("test"))
        self.assertEqual(self.qty(BTC), 0)
        self.assertEqual(len(self.ledger("funding")), 3)

    def test_accruals_run_before_pending_fills(self):
        oid = store.insert_order(self.now, BTC, "cover", 0.5, "pending", "manual")
        house = self.engine._housekeeping(self.now, self.params(), {BTC: Quote(BTC, 50000.0)}, {"open": False})
        self.assertEqual(house["accruals"]["funding_windows"], 3)
        self.assertEqual(store.get_order(oid)["status"], "filled")
        self.assertEqual(len(self.ledger("funding")), 3)

    def test_atomic_and_deduplicated_after_a_failure(self):
        real = store.add_cash_entry
        calls = {"n": 0}

        def flaky(ts, kind, amount, symbol=None, note=None):
            if kind == "funding":
                calls["n"] += 1
                if calls["n"] == 2:
                    return None                                   # the 2nd window fails
            return real(ts, kind, amount, symbol=symbol, note=note)
        store.add_cash_entry = flaky
        try:
            out = self.broker.daily_accruals(self.now)
        finally:
            store.add_cash_entry = real
        self.assertEqual(out["funding_windows"], 0)               # the symbol did not complete
        self.assertEqual(len(self.ledger("funding")), 1)
        self.assertEqual(store.get_last_funding_at(), FRI.replace(hour=8).isoformat())   # not advanced
        out = self.broker.daily_accruals(self.now)                 # retry: the rest, no double booking
        self.assertEqual(out["funding_windows"], 2)
        self.assertEqual(sorted(r["note"][:16] for r in self.ledger("funding")),
                         ["fw 2026-10-02T16", "fw 2026-10-03T00", "fw 2026-10-03T08"])
        self.assertEqual(store.get_last_funding_at(), SAT.replace(hour=8).isoformat())


class RiskWatchFixTest(CryptoCase):
    def test_fills_manual_crypto_and_expires_stale_autopilot_orders(self):
        self.order(ETH, "buy", qty=1)                             # a crypto position -> the watch runs
        self.now = SAT
        manual = store.insert_order(self.now - timedelta(minutes=5), BTC, "buy", 0.01, "pending", "manual")
        old = store.insert_order(self.now - timedelta(minutes=61), BTC, "buy", 0.02, "pending", "autopilot")
        new = store.insert_order(self.now - timedelta(minutes=10), BTC, "buy", 0.03, "pending", "autopilot")
        eq = store.insert_order(self.now - timedelta(hours=5), "SPY", "buy", 1, "pending", "autopilot")
        out = asyncio.run(self.engine.run_risk_watch())
        self.assertEqual(out["status"], "ok", out)
        self.assertEqual(store.get_order(manual)["status"], "filled")
        self.assertEqual((store.get_order(old)["status"], store.get_order(old)["reject_reason"]),
                         ("cancelled", "stale — re-evaluated at the next run"))
        self.assertEqual(store.get_order(new)["status"], "pending")          # not filled, not expired
        self.assertEqual(store.get_order(eq)["status"], "pending")           # equities untouched
        self.assertIn("autopilot_orders_expired", [a["action"] for a in store.list_audit(20)])
        self.assertTrue(any("stale" in e["title"] for e in store.events_after(0) if e["kind"] == "autopilot"))


class StaleQuoteTest(CryptoCase):
    def test_yfinance_fallback_quote_is_marked_stale(self):
        feed = YFinanceFeed(cache_store=None, clock=lambda: SAT)
        feed._mem_bars[BTC] = (SAT, 400, [Bar("2026-10-02", 1, 1, 1, 49000.0), Bar("2026-10-03", 1, 1, 1, 50000.0)])

        def boom(_sym):
            raise RuntimeError("offline")
        feed._ticker = boom
        q = feed.quote(BTC)
        self.assertEqual((q.price, q.stale, q.ts), (50000.0, True, "2026-10-03T00:00:00+00:00"))
        self.assertTrue(market.quote_is_stale(q, SAT))
        self.assertFalse(market.quote_is_stale(Quote(BTC, 1.0), SAT))
        self.assertTrue(market.quote_is_stale(None, SAT))

    def stale(self, price):
        return Quote(BTC, price, ts=(SAT - timedelta(hours=1)).isoformat(), stale=True)

    def test_no_liquidation_on_a_stale_quote(self):
        self.now = SAT
        self.order(BTC, "short", qty=0.5)
        self.feed.quotes[BTC] = self.stale(61000.0)
        self.assertEqual(self.broker.check_margin(self.now), [])
        self.assertTrue(self.broker.margin_warnings)
        out = asyncio.run(self.engine.run_risk_watch())
        self.assertEqual(out["status"], "partial")
        self.assertIn("stale", out["error"])
        self.assertEqual(self.qty(BTC), -0.5)
        # no fill on a stale crypto quote either: the order waits
        self.assertEqual(self.order(BTC, "cover", qty=0.1).status, "pending")
        self.feed.quotes[BTC] = 61000.0                              # live again -> liquidation
        asyncio.run(self.engine.run_risk_watch())
        self.assertEqual(self.qty(BTC), 0)

    def test_no_drawdown_stop_on_a_stale_quote(self):
        self.order(BTC, "buy", qty=1.0)
        self.cycle()
        self.now = SAT
        self.feed.quotes[BTC] = self.stale(40000.0)
        out = asyncio.run(self.engine.run_risk_watch())
        self.assertFalse(store.get_halted()["active"])
        self.assertEqual(out["status"], "partial")
        self.assertIn("drawdown stop skipped", out["error"])
        self.assertEqual(self.qty(BTC), 1.0)


class QtyValidationTest(CryptoCase):
    def test_fractional_equity_and_non_numbers(self):
        cases = [({"symbol": "SPY", "side": "buy", "qty": 0.5}, "qty", "fractional"),
                 ({"symbol": "SPY", "side": "buy", "qty": "1"}, "qty", "JSON number"),
                 ({"symbol": BTC, "side": "buy", "qty": "0.1"}, "qty", "JSON number"),
                 ({"symbol": BTC, "side": "buy", "notional": "100"}, "notional", "JSON number"),
                 ({"symbol": "SPY", "side": "buy", "qty": True}, "qty", "JSON number")]
        for body, field, text in cases:
            with self.assertRaises(EngineError) as cm:
                asyncio.run(self.engine.manual_order(body))
            self.assertEqual(cm.exception.status, 400, body)
            self.assertIn(text, cm.exception.errors[field], body)


def gen(seed, n, drift, weekends, start=date(2021, 1, 1), p=100.0):
    r, d, out = random.Random(seed), start, []
    while len(out) < n:
        d += timedelta(days=1)
        if not weekends and d.weekday() >= 5:
            continue
        o = p * (1 + r.gauss(0, 0.001))
        p = o * (1 + drift + r.gauss(0, 0.002))
        out.append(Bar(d.isoformat(), o, max(o, p) * 1.003, min(o, p) * 0.997, p))
    return out


class BacktestCounterTest(unittest.TestCase):
    def test_trade_counter_per_asset_class(self):
        bars = {BTC: gen(1, 900, 0.002, True, p=30000.0), "SPY": gen(3, 640, 0.0015, False)}
        start = bars["SPY"][300].date
        base = {"min_confidence": 55, "agent_weights": {"technical": 1.0, "macro": 0.0}, "allow_shorts": False}
        one = run_backtest(bars, [], dict(base, max_trades_per_day=1), start=start)
        many = run_backtest(bars, [], dict(base, max_trades_per_day=20), start=start)
        by1 = {p["symbol"]: p for p in one["per_symbol"]}
        by20 = {p["symbol"]: p for p in many["per_symbol"]}
        for s in (BTC, "SPY"):                 # one symbol per class: a 1-trade limit per class changes nothing
            self.assertGreater(by1[s]["trades"], 0)
            self.assertEqual((by1[s]["trades"], by1[s]["days_long"]), (by20[s]["trades"], by20[s]["days_long"]), s)


if __name__ == "__main__":
    unittest.main()
