"""Stage 7 (SPEC §17.6): every pre-trade check — trading hours, halt, daily loss, duplicate
guard, daily order count, max order notional, max position, price collar, short/borrow
rules, buying power, valuation. Temp databases, fake quotes. Paper only."""
import unittest
from datetime import datetime, timezone

try:
    from sp_trading_testkit import SAT, TradingCase
except ImportError:  # pragma: no cover
    from tests.sp_trading_testkit import SAT, TradingCase

from strategy_platform.execution import oms
from strategy_platform.portfolio import settings as pf_settings
from strategy_platform.storage import db


def failed(o):
    return [c["check"] for c in o["checks"] if not c["ok"]]


class PreTradeTest(TradingCase):
    def test_trading_hours(self):
        self.set_now(SAT)
        o = self.manual("SPY", "buy", 1)
        self.assertEqual((o["state"], failed(o)), ("rejected", ["trading_hours"]))
        self.assertIn("outside trading hours", o["reject_reason"])
        c = self.manual("BTC-USD", "buy", 0.01)                     # crypto trades 24/7
        self.assertEqual(c["state"], "filled")

    def test_halt_blocks_new_risk_not_closing(self):
        self.manual("SPY", "buy", 10)
        self.acc.set_halt(True, "kill", "test halt")
        o = self.manual("QQQ", "buy", 1)
        self.assertEqual(failed(o), ["halt"])
        self.assertEqual(self.manual("SPY", "sell", 10)["state"], "filled")

    def test_daily_loss_blocks_until_the_next_day(self):
        self.manual("SPY", "buy", 90)
        self.quotes.prices["SPY"] = 440.0                            # ≈ −5.4 % of the day's reference (100,000)
        o = self.manual("QQQ", "buy", 1)
        self.assertEqual(failed(o), ["daily_loss"])
        self.assertIn("daily loss limit reached", o["reject_reason"])
        c = self.manual("BTC-USD", "buy", 0.01)                     # UTC day: reference = start capital, too
        self.assertEqual(failed(c), ["daily_loss"])
        self.assertEqual(self.manual("SPY", "sell", 10)["state"], "filled")   # reducing passes
        # next NY day: the reference is the last snapshot before midnight -> unblocked
        self.acc.store.add_equity_point(datetime(2026, 10, 3, 1, 0, tzinfo=timezone.utc), 94600.0, 50000, 44600, 0)
        self.set_now(datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc))
        self.assertEqual(self.manual("QQQ", "buy", 1)["state"], "filled")

    def test_daily_loss_block_holds_when_the_price_recovers_the_same_day(self):
        # review fix M1 (2026-10-18): once hit, the block holds until the next NY day although the P&L recovers
        self.manual("SPY", "buy", 90)
        self.quotes.prices["SPY"] = 440.0
        self.assertEqual(failed(self.manual("QQQ", "buy", 1)), ["daily_loss"])     # hit -> latched
        self.quotes.prices["SPY"] = 500.0                                         # fully recovered, same day
        self.advance(minutes=30)
        o = self.manual("QQQ", "buy", 2)
        self.assertEqual(failed(o), ["daily_loss"])
        self.assertIn("the block holds", o["reject_reason"])
        self.assertIn("2026-10-03", o["reject_reason"])                           # lifts at the next NY midnight
        from strategy_platform.portfolio import risk
        dl = risk.day_loss(self.acc, self.clock(), 100000.0, 2.0)
        self.assertTrue(dl["equity"]["blocked"] and dl["equity"]["latched"])
        self.assertEqual(self.manual("SPY", "sell", 10)["state"], "filled")       # reducing still passes
        titles = [e["title"] for e in self.acc.store.events_after(0, 500)]
        self.assertEqual(titles.count("Daily loss limit reached (equity)"), 1)     # latched once
        self.set_now(datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc))            # next NY day: lifted
        self.acc.store.add_equity_point(datetime(2026, 10, 3, 1, 0, tzinfo=timezone.utc), 99990.0, 50000, 49990, 0)
        self.assertEqual(self.manual("QQQ", "buy", 2)["state"], "filled")

    def test_duplicate_guard(self):
        a = self.manual("SPY", "buy", 2)
        b = self.manual("SPY", "buy", 2)
        self.assertEqual((a["state"], failed(b)), ("filled", ["duplicate"]))
        self.advance(seconds=61)
        self.assertEqual(self.manual("SPY", "buy", 2)["state"], "filled")
        # a working order for the symbol blocks a new one
        ts = self.clock().isoformat(timespec="seconds")
        with db._connect() as conn:
            oms._insert(conn, {"client_order_id": "working-0001", "account_id": "paper-main", "created_at": ts,
                               "updated_at": ts, "symbol": "QQQ", "side": "buy", "qty": 1.0, "origin": "runtime",
                               "tif": "day", "state": "accepted", "filled_qty": 0.0, "fees": 0.0, "attempts": 1,
                               "allow_reduce": 1, "allocations_json": "[]", "checks_json": "[]"})
        self.assertEqual(failed(self.manual("QQQ", "buy", 3)), ["duplicate"])

    def test_daily_order_count(self):
        pf_settings.put("paper-main", {"max_orders_per_day": 2}, "test")
        self.manual("SPY", "buy", 1)
        self.manual("QQQ", "buy", 1)
        o = self.manual("TLT", "buy", 1)
        self.assertEqual(failed(o), ["daily_order_count"])
        self.assertEqual(self.manual("BTC-USD", "buy", 0.01)["state"], "filled")   # counted per class

    def test_max_order_notional_and_position(self):
        o = self.manual("SPY", "buy", 101)                           # $50,500 > 50 % of $100,000
        self.assertEqual(failed(o), ["max_order_notional"])
        pf_settings.put("paper-main", {"max_order_notional_usd": 20000.0}, "test")
        self.assertEqual(failed(self.manual("SPY", "buy", 41)), ["max_order_notional"])
        pf_settings.put("paper-main", {"max_order_notional_usd": 1000000.0}, "test")
        self.assertEqual(self.manual("SPY", "buy", 90)["state"], "filled")
        self.advance(seconds=61)
        o = self.manual("SPY", "buy", 20)                            # 110 × 500 = 55,000 > 50 % of equity
        self.assertEqual(failed(o), ["max_position"])

    def test_price_collar(self):
        o, _ = oms.place(self.acc, {"symbol": "SPY", "side": "buy", "qty": 1, "origin": "runtime", "ref_price": 470.0,
                                    "allocations": [("_unallocated", 1)]})
        self.assertEqual(failed(o), ["price_collar"])                # 6.4 % > 5 %
        self.assertIn("possible bad tick", o["reject_reason"])
        o, _ = oms.place(self.acc, {"symbol": "BTC-USD", "side": "buy", "qty": 0.01, "origin": "runtime",
                                    "ref_price": 56000.0, "allocations": [("_unallocated", 0.01)]})
        self.assertEqual(o["state"], "filled")                       # 7.1 % < the crypto collar 10 %
        ok = self.manual("QQQ", "buy", 1)                            # retest 2026-10-23: the latest quote is the
        self.assertEqual([c for c in ok["checks"] if c["check"] == "price_collar"][0]["message"],   # reference
                         f"0.00 % from the latest quote {self.quotes.prices['QQQ']:,.4f} (collar 5 %)")

    def test_price_collar_without_a_reference_fails(self):
        from strategy_platform.portfolio import checks
        self.assertEqual(self.manual("QQQ", "buy", 2)["state"], "filled")      # a position to close later
        self.advance(seconds=61)
        from strategy_platform.execution import oms as _oms
        orig_q = _oms._quotes_within
        fresh = {"v": None}

        def quotes_within(account, symbols, deadline, fresh_for=None):      # the collar's own read, controlled
            got, why, _f = orig_q(account, symbols, deadline, fresh_for)
            return got, why, (fresh["v"](fresh_for) if callable(fresh["v"]) else fresh["v"])
        _oms._quotes_within = quotes_within
        self.addCleanup(setattr, _oms, "_quotes_within", orig_q)
        _ = checks
        o = self.manual("QQQ", "buy", 1)
        self.assertEqual(failed(o), ["price_collar"])
        self.assertEqual(o["reject_reason"], "no reference price for QQQ — the latest quote could not be read, so the "
                                             "price collar cannot be checked")
        fresh["v"] = lambda sym: self.quotes.prices[sym] * 1.2              # a bad tick vs a fresh read
        self.advance(seconds=61)
        o = self.manual("QQQ", "buy", 1)
        self.assertEqual(failed(o), ["price_collar"])
        self.assertIn("from the latest quote", o["reject_reason"])
        fresh["v"] = None
        self.advance(seconds=61)
        c = self.manual("QQQ", "sell", 1)                         # closing: never held back by missing data
        self.assertEqual(c["state"], "filled")
        self.assertEqual([x for x in c["checks"] if x["check"] == "price_collar"][0]["message"],
                         "closing order: no reference price for QQQ could be read — not blocked (closing orders are "
                         "never held back by missing data)")

    def test_slow_quote_gives_a_clear_reject_within_the_deadline(self):
        import threading
        import time as _t
        from strategy_platform.execution import accounts as _acc
        from strategy_platform.execution import oms as _oms
        release = threading.Event()
        orig = _acc.quote_for

        def slow(account, sym):
            if sym == "BTC-USD":
                release.wait(5)                                  # a flaky network: the read hangs
            return orig(account, sym)
        _acc.quote_for = slow
        self.addCleanup(setattr, _acc, "quote_for", orig)
        self.addCleanup(release.set)
        old = _oms.ORDER_QUOTE_DEADLINE_SECONDS
        _oms.ORDER_QUOTE_DEADLINE_SECONDS = 0.3
        self.addCleanup(setattr, _oms, "ORDER_QUOTE_DEADLINE_SECONDS", old)
        t0 = _t.monotonic()
        o = self.manual("BTC-USD", "buy", 0.001)
        self.assertLess(_t.monotonic() - t0, 3.0)
        self.assertEqual(failed(o), ["quote"])
        self.assertEqual(o["reject_reason"], "no current quote for BTC-USD (no quote within 0.3 s) — order not sent")
        release.set()

    def test_manual_fill_is_valued_before_the_next_cycle(self):
        from strategy_platform.live import runtime
        from strategy_platform.portfolio import views
        self.assertNotIn("BTC-USD", runtime.default().last_quotes.get(self.acc.id) or {})
        o = self.manual("BTC-USD", "buy", 0.001)
        self.assertEqual(o["state"], "filled")
        self.assertIn("BTC-USD", runtime.default().last_quotes[self.acc.id])
        summary = views.account_summary(self.acc)
        self.assertEqual(summary["valuation"], {"ok": True, "message": None})
        self.assertIsNotNone(summary["account"]["equity"])

    def test_short_rules(self):
        self.acc.params["not_shortable"] = ["QQQ"]
        self.assertEqual(failed(self.manual("QQQ", "short", 1)), ["short_rules"])
        self.acc.params["allow_shorts"] = False
        self.assertEqual(failed(self.manual("SPY", "short", 1)), ["short_rules"])

    def test_buying_power(self):
        pf_settings.put("paper-main", {"max_order_notional_pct": 100.0, "max_position_pct_equity": 100.0}, "test")
        self.manual("TLT", "short", 300)                             # locks margin
        o = self.manual("SPY", "buy", 180)
        self.assertEqual(failed(o), ["buying_power"])

    def test_valuation_needed_for_new_risk(self):
        self.manual("SPY", "buy", 10)
        del self.quotes.prices["SPY"]
        o = self.manual("QQQ", "buy", 1)
        self.assertEqual(failed(o), ["valuation"])
        self.assertIn("valuation paused", o["reject_reason"])

    def test_protective_orders_skip_the_checks(self):
        self.manual("SPY", "buy", 10)
        self.acc.set_halt(True, "kill", "x")
        self.set_now(SAT)
        o, _ = oms.place(self.acc, {"symbol": "SPY", "side": "sell", "qty": 10, "origin": "kill"})
        self.assertEqual(o["state"], "accepted")
        self.assertEqual([c["check"] for c in o["checks"]], ["trading_hours"])


if __name__ == "__main__":
    unittest.main()
