"""fund/broker.py PaperBroker (paper trading only): fills with fees and
slippage, buying power, realistic shorts (margin, exposure cap, not
shortable, Rule 201, borrow fees with weekend catch-up, dividends), margin
calls, pending orders at the open with re-validation, long<->short flips,
liquidation, reset, equity snapshots. Temp SQLite, fake quotes and clock.

Run from HedgeFund/backend:  python -m unittest tests.test_fund_broker -v
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from fund import params as P
from fund import store
from fund import broker as broker_mod
from fund.broker import PaperBroker, StateUnavailable, fill_values
from fund.market import FakeFeed
from fund.types import Bar, OrderRequest, Quote
from newsroom import db

FRI = datetime(2026, 10, 2, 14, 0, tzinfo=timezone.utc)      # Friday, session open (10:00 EDT)
SAT = datetime(2026, 10, 3, 15, 0, tzinfo=timezone.utc)      # Saturday, closed
MON = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)      # Monday, session open
MON_OPEN = datetime(2026, 10, 5, 13, 30, tzinfo=timezone.utc)  # Monday 09:30 EDT
TUE = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)


class BrokerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.assertTrue(db.init(os.path.join(self._tmp.name, "n.db"), legacy=""))
        self.now = FRI
        self.feed = FakeFeed(quotes={"SPY": 500.0, "QQQ": 400.0, "XLF": 50.0, "XLE": 90.0, "ABC": 50.0})
        self.params = P.merge(None, {"fees_bps": 10.0})
        self.b = PaperBroker(store, self.feed.quote, clock=lambda: self.now, dividends_fn=self.feed.dividends,
                             params_fn=lambda: self.params, bars_fn=self.feed.daily_bars)

    def tearDown(self):
        db.close()
        self._tmp.cleanup()

    def order(self, symbol, side, qty=None, notional=None, origin="manual", **kw):
        return self.b.submit(OrderRequest(symbol=symbol, side=side, qty=qty, notional=notional, origin=origin), **kw)

    def pos(self, symbol):
        p = store.get_position(symbol)
        return p["qty"] if p else 0.0


class BasicsTest(BrokerCase):
    def test_paper_only(self):
        with self.assertRaises(ValueError):
            PaperBroker(store, self.feed.quote, mode="live")
        self.assertEqual(self.b.mode, "paper")

    def test_initial_deposit(self):
        acct = self.b.account()
        self.assertEqual((acct.cash, acct.equity, acct.starting_capital), (100000.0, 100000.0, 100000.0))
        self.assertEqual(store.cash_entries()[0]["kind"], "deposit")
        self.assertFalse(self.b.ensure_initialized())
        self.assertEqual(store.get_last_accrual_date(), "2026-10-02")

    def test_fill_values(self):
        fp, fee, notional = fill_values("buy", 100, 500.0, 10.0)
        self.assertAlmostEqual(fp, 500.25)
        self.assertAlmostEqual(notional, 50025.0)
        self.assertAlmostEqual(fee, 25.0125)
        self.assertAlmostEqual(fill_values("short", 1, 500.0, 10.0)[0], 499.75)

    def test_buy_sell_fees_slippage_realized(self):
        r = self.order("spy", "buy", 100)
        self.assertEqual((r.status, r.symbol, r.qty), ("filled", "SPY", 100))
        self.assertAlmostEqual(r.fill_price, 500.25)
        self.assertAlmostEqual(r.fees, 25.0125)
        self.assertAlmostEqual(store.cash_balance(), 49949.9875)
        self.assertAlmostEqual(store.get_position("SPY")["avg_price"], 500.25)
        self.feed.quotes["SPY"] = 510.0
        r = self.order("SPY", "sell", 40)
        self.assertAlmostEqual(r.fill_price, 509.745)
        self.assertAlmostEqual(r.fees, 10.1949)
        p = store.get_position("SPY")
        self.assertEqual(p["qty"], 60)
        self.assertAlmostEqual(p["realized_pnl"], 379.8 - 25.0125 - 10.1949)
        self.assertAlmostEqual(store.cash_balance(), 70329.5926)
        acct = self.b.account()
        self.assertAlmostEqual(acct.equity, 70329.5926 + 60 * 510)
        self.assertAlmostEqual(acct.long_mv, 30600)
        self.assertEqual(len(store.cash_entries()), 5)       # deposit + 2 x (fill, fee)
        order = store.get_order(r.order_id)
        self.assertEqual((order["status"], order["origin"], order["mode"]), ("filled", "manual", "paper"))
        # weighted average on an add
        self.order("SPY", "buy", 60)
        self.assertAlmostEqual(store.get_position("SPY")["avg_price"], (60 * 500.25 + 60 * 510.255) / 120)

    def test_size_rules(self):
        self.order("SPY", "buy", 10)
        r = self.order("SPY", "sell", 11)
        self.assertEqual((r.status, r.reject_reason), ("rejected", "exceeds the position (10 shares)"))
        self.assertEqual(self.order("SPY", "buy", notional=1000).qty, 2)
        r = self.order("SPY", "buy", notional=100)
        self.assertEqual((r.status, r.reject_reason), ("rejected", "size below one share"))
        self.assertIsNotNone(r.order_id)                     # rejections are recorded
        self.assertEqual(self.order("SPY", "buy", 0.7).reject_reason, "size below one share")
        self.assertEqual(self.order("SPY", "buy").reject_reason, "qty or notional required")
        self.assertEqual(self.order("ZZZ", "buy", 1).reject_reason, "no quote available")
        bad = self.b.submit(OrderRequest(symbol="SPY", side="yolo", qty=1))
        self.assertEqual((bad.status, bad.order_id), ("rejected", None))
        self.assertEqual(self.order("QQQ", "sell", 1).reject_reason, "no long position to sell")
        self.assertEqual(self.order("QQQ", "cover", 1).reject_reason, "no short position to cover")

    def test_buying_power(self):
        r = self.order("SPY", "buy", 300)
        self.assertEqual(r.status, "rejected")
        self.assertTrue(r.reject_reason.startswith("insufficient buying power"))
        self.assertEqual(store.get_order(r.order_id)["status"], "rejected")
        res = self.b.target("SPY", 1.5, "autopilot", run_id=None)
        self.assertEqual([(x.side, x.qty, x.status) for x in res], [("buy", 199, "filled")])
        self.assertGreaterEqual(store.cash_balance(), 0)


class ShortTest(BrokerCase):
    def test_short_open_with_margin(self):
        r = self.order("SPY", "short", 50)
        self.assertEqual(r.status, "filled")
        self.assertAlmostEqual(r.fill_price, 499.75)
        self.assertAlmostEqual(store.cash_balance(), 100000 + 24987.5 - 12.49375)   # proceeds in cash
        p = store.get_position("SPY")
        self.assertEqual((p["qty"], p["borrow_rate_pct"]), (-50, 0.3))
        acct = self.b.account()
        self.assertAlmostEqual(acct.short_mv, 25000)
        self.assertAlmostEqual(acct.equity, 124975.00625 - 25000)
        self.assertAlmostEqual(acct.buying_power, 124975.00625 - 25000 * 1.5)
        self.assertAlmostEqual(acct.maintenance_required, 7500)
        self.assertAlmostEqual(acct.margin_excess, 99975.00625 - 7500)
        self.assertAlmostEqual(acct.short_exposure_pct, 25000 / 99975.00625 * 100)
        self.order("ABC", "short", 10)
        self.assertEqual(store.get_position("ABC")["borrow_rate_pct"], 3.0)          # stock default
        pv = {v.symbol: v for v in self.b.positions()}
        self.assertEqual((pv["SPY"].side, pv["SPY"].market_value), ("short", -25000.0))
        # cover realizes P&L
        self.feed.quotes["SPY"] = 480.0
        r = self.order("SPY", "cover", 50)
        self.assertAlmostEqual(store.get_position("SPY")["realized_pnl"], (499.75 - 480.24) * 50 - 12.49375 - r.fees)
        self.assertEqual(self.b.positions()[0].symbol, "ABC")

    def test_short_exposure_cap(self):
        r = self.order("SPY", "short", 70)
        self.assertEqual(r.status, "rejected")
        self.assertTrue(r.reject_reason.startswith("short exposure limit"))
        res = self.b.target("SPY", -0.35, "autopilot")
        self.assertEqual([(x.side, x.qty) for x in res], [("short", 59)])
        self.assertLessEqual(self.b.account().short_exposure_pct, 30.0)

    def test_short_needs_initial_margin(self):
        self.order("QQQ", "buy", 225)
        r = self.order("SPY", "short", 50)
        self.assertEqual(r.status, "rejected")
        self.assertIn("buying power for the short margin", r.reject_reason)
        res = self.b.target("SPY", -0.3, "autopilot")
        self.assertEqual([(x.side, x.qty, x.status) for x in res], [("short", 39, "filled")])
        self.assertGreaterEqual(self.b.account().buying_power, 0)

    def test_short_restrictions(self):
        self.params["not_shortable"] = ["XLE"]
        self.assertEqual(self.order("XLE", "short", 1).reject_reason, "not shortable (no borrow available)")
        self.order("SPY", "buy", 1)
        self.assertEqual(self.order("SPY", "short", 1).reject_reason, "sell the long position first")
        self.params["allow_shorts"] = False
        self.assertEqual(self.order("QQQ", "short", 1).reject_reason, "short selling is disabled")

    def test_rule_201(self):
        self.feed.quotes["SPY"] = Quote("SPY", 440.0, prev_close=500.0)
        self.assertIn("Rule 201", self.order("SPY", "short", 1).reject_reason)
        self.assertEqual(self.order("SPY", "buy", 1).status, "filled")               # longs unaffected
        # yesterday's intraday drop >= 10 % (daily bars) still restricts today
        self.feed.quotes["QQQ"] = Quote("QQQ", 380.0, prev_close=370.0)
        self.feed.bars["QQQ"] = [Bar("2026-09-30", 400, 401, 399, 400), Bar("2026-10-01", 395, 396, 355, 370)]
        self.assertIn("Rule 201", self.order("QQQ", "short", 1).reject_reason)
        # an older drop does not
        self.feed.bars["QQQ"] = [Bar("2026-09-28", 400, 401, 399, 400), Bar("2026-09-29", 395, 396, 355, 370),
                                 Bar("2026-10-01", 370, 372, 368, 370)]
        self.assertEqual(self.order("QQQ", "short", 1).status, "filled")


class AccrualTest(BrokerCase):
    def test_borrow_fee_catch_up_over_weekend(self):
        self.order("SPY", "short", 50)
        self.assertEqual(self.b.daily_accruals()["days"], 0)          # same day as the reset
        self.now = MON
        res = self.b.daily_accruals()
        self.assertEqual(res["days"], 3)                               # Sat, Sun, Mon
        self.assertAlmostEqual(res["borrow_fees"], -0.625)             # 25000 x 0.3 % / 360 x 3
        entry = store.cash_entries(kind="borrow_fee")[0]
        self.assertAlmostEqual(entry["amount"], -0.625)
        self.assertEqual(self.b.daily_accruals()["borrow_fees"], 0.0)  # once per day
        self.params["borrow_rate_overrides"] = {"SPY": 3.6}
        self.now = datetime(2026, 10, 6, 14, tzinfo=timezone.utc)
        self.assertAlmostEqual(self.b.daily_accruals()["borrow_fees"], -2.5)
        self.assertEqual(store.get_position("SPY")["borrow_rate_pct"], 3.6)

    def test_dividends_long_vs_short(self):
        self.order("SPY", "buy", 100)
        self.order("XLF", "short", 40)
        self.feed.divs["SPY"] = [("2026-10-05", 1.5)]
        self.feed.divs["XLF"] = [("2026-09-30", 9.0), ("2026-10-05", 0.2), ("2026-10-09", 0.3)]
        self.now = MON
        before = store.cash_balance()
        res = self.b.daily_accruals()
        self.assertAlmostEqual(res["dividends"], 150.0 - 8.0)
        self.assertAlmostEqual(res["borrow_fees"], -40 * 50 * 0.003 / 360 * 3)
        self.assertAlmostEqual(store.cash_balance() - before, 142.0 + res["borrow_fees"])
        self.assertEqual(store.cash_entries(kind="dividend")[0]["amount"], 150.0)
        self.assertAlmostEqual(store.cash_entries(kind="dividend_short")[0]["amount"], -8.0)
        self.assertEqual(self.b.daily_accruals()["dividends"], 0.0)    # no double booking
        self.assertEqual(store.get_last_dividend_date(), "2026-10-05")


class MarginCallTest(BrokerCase):
    def setup_book(self):
        self.feed.quotes["SPY"] = 100.0
        self.order("QQQ", "buy", 150)
        self.assertEqual(self.order("SPY", "short", 290).status, "filled")
        self.feed.quotes["SPY"] = 200.0

    def test_forced_cover_when_open(self):
        self.setup_book()
        acct = self.b.account()
        self.assertLess(acct.margin_excess, 0)
        res = self.b.check_margin()
        self.assertEqual([(r.side, r.qty, r.status, r.origin) for r in res], [("cover", 109, "filled", "margin-call")])
        self.assertGreaterEqual(self.b.account().margin_excess, 0)
        self.assertEqual(self.b.check_margin(), [])
        ev = store.events_after(0)
        self.assertEqual([e["kind"] for e in ev], ["margin_call"])

    def test_forced_cover_pending_when_closed(self):
        self.setup_book()
        self.now = SAT
        res = self.b.check_margin()
        self.assertEqual([(r.side, r.qty, r.status) for r in res], [("cover", 109, "pending")])
        self.assertEqual(self.b.check_margin(), [])                    # pending cover counts
        self.assertEqual(len(store.pending_orders()), 1)
        self.now = MON
        filled = self.b.process_pending()
        self.assertEqual([(r.side, r.qty, r.status) for r in filled], [("cover", 109, "filled")])
        self.assertEqual(self.pos("SPY"), -181)
        self.assertEqual(self.b.check_margin(), [])


class PendingTest(BrokerCase):
    def test_pending_at_open_with_revalidation(self):
        self.b.ensure_initialized()
        self.now = SAT
        a = self.order("SPY", "buy", 150)
        c = self.order("QQQ", "buy", 150)
        self.assertEqual((a.status, c.status), ("pending", "pending"))
        self.assertAlmostEqual(a.notional, 75000)
        self.assertEqual(self.order("XLF", "sell", 1).reject_reason, "no long position to sell")
        self.assertEqual(self.b.process_pending(), [])                 # still closed
        self.assertEqual(store.cash_balance(), 100000)
        self.now = MON
        res = {r.symbol: r for r in self.b.process_pending()}
        self.assertEqual((res["SPY"].status, res["SPY"].qty), ("filled", 150))
        self.assertEqual((res["QQQ"].status, res["QQQ"].qty), ("filled", 62))   # reduced to buying power
        self.assertEqual(store.get_order(c.order_id)["qty"], 62)
        self.assertEqual(store.get_order(c.order_id)["filled_at"], "2026-10-05T14:00:00+00:00")
        self.assertEqual(store.pending_orders(), [])
        self.assertGreaterEqual(store.cash_balance(), 0)

    def test_pending_sells_clipped_and_rejected(self):
        self.order("SPY", "buy", 10)
        self.now = SAT
        self.order("SPY", "sell", 10)
        self.order("SPY", "sell", 10)
        self.now = MON
        res = self.b.process_pending()
        self.assertEqual([(r.status, r.reject_reason) for r in res],
                         [("filled", None), ("rejected", "no long position to sell")])
        self.assertEqual(self.pos("SPY"), 0)

    def test_pending_short_rechecks_rule_201_at_open(self):
        self.b.ensure_initialized()
        self.now = SAT
        self.assertEqual(self.order("SPY", "short", 5).status, "pending")
        self.now = MON
        self.feed.quotes["SPY"] = Quote("SPY", 400.0, prev_close=500.0)
        self.assertIn("Rule 201", self.b.process_pending()[0].reject_reason)

    def test_no_quote_keeps_closing_order_pending(self):
        self.order("SPY", "buy", 10)
        del self.feed.quotes["SPY"]
        r = self.order("SPY", "sell", 10)
        self.assertEqual(r.status, "pending")
        self.assertEqual(self.b.process_pending(), [])                 # still no quote
        self.feed.quotes["SPY"] = 505.0
        self.assertEqual(self.b.process_pending()[0].status, "filled")


class TargetTest(BrokerCase):
    def test_long_to_short_is_two_orders(self):
        self.order("SPY", "buy", 100)
        res = self.b.target("SPY", -0.05, "autopilot", run_id=7, recommendation_id=3, rationale="flip")
        self.assertEqual([(r.side, r.qty, r.status) for r in res], [("sell", 100, "filled"), ("short", 9, "filled")])
        self.assertEqual(self.pos("SPY"), -9)
        o = store.get_order(res[1].order_id)
        self.assertEqual((o["run_id"], o["recommendation_id"], o["origin"], o["rationale"]), (7, 3, "autopilot", "flip"))
        self.assertEqual(self.b.target("SPY", -0.05, "autopilot"), [])  # already there
        res = self.b.target("SPY", 0.05, "autopilot")
        self.assertEqual([(r.side, r.qty) for r in res], [("cover", 9), ("buy", 9)])
        self.assertEqual([(r.side, r.qty) for r in self.b.target("SPY", 0.0, "autopilot")], [("sell", 9)])
        self.assertEqual(store.get_position("SPY")["qty"], 0)
        self.assertEqual(self.b.positions(), [])

    def test_flip_second_leg_skipped_when_first_rejected(self):
        self.order("SPY", "buy", 10)
        self.params["allow_shorts"] = False
        res = self.b.target("SPY", -0.05, "autopilot")
        self.assertEqual([(r.side, r.status) for r in res], [("sell", "filled"), ("short", "rejected")])

    def test_pending_target_no_churn(self):
        self.b.ensure_initialized()
        self.now = SAT
        res = self.b.target("SPY", 0.05, "autopilot")
        self.assertEqual([(r.side, r.qty, r.status) for r in res], [("buy", 10, "pending")])
        self.assertEqual(self.b.target("SPY", 0.05, "autopilot"), [])
        self.assertEqual(self.b.projected_qty("SPY"), 10)
        res2 = self.b.target("SPY", 0.1, "autopilot")
        self.assertEqual([(r.side, r.qty) for r in res2], [("buy", 20)])
        self.assertEqual(store.get_order(res[0].order_id)["status"], "cancelled")
        self.assertEqual(len(store.pending_orders()), 1)

    def test_no_quote(self):
        res = self.b.target("ZZZ", 0.05, "autopilot")
        self.assertEqual((res[0].status, res[0].order_id, res[0].reject_reason),
                         ("rejected", None, "no quote available"))


class LiquidateResetTest(BrokerCase):
    def test_liquidate_all_when_closed(self):
        self.order("SPY", "buy", 20)
        self.order("QQQ", "short", 10)
        self.now = SAT
        pend = self.order("XLF", "buy", 5)
        res = self.b.liquidate_all("kill-switch")
        self.assertEqual(sorted((r.symbol, r.side, r.qty, r.status) for r in res),
                         [("QQQ", "cover", 10, "pending"), ("SPY", "sell", 20, "pending")])
        self.assertEqual(store.get_order(pend.order_id)["status"], "cancelled")
        blocked = self.b.target("SPY", 0.05, "autopilot")
        self.assertEqual((blocked[0].order_id, blocked[0].reject_reason), (None, "protective order pending"))
        self.now = MON
        done = self.b.process_pending()
        self.assertEqual(sorted((r.symbol, r.status, r.origin) for r in done),
                         [("QQQ", "filled", "kill-switch"), ("SPY", "filled", "kill-switch")])
        self.assertEqual(self.b.positions(), [])

    def test_liquidate_all_when_open(self):
        self.order("SPY", "buy", 20)
        res = self.b.liquidate_all("drawdown-stop")
        self.assertEqual([(r.side, r.status, r.origin) for r in res], [("sell", "filled", "drawdown-stop")])

    def test_reset(self):
        self.order("SPY", "buy", 20)
        self.order("QQQ", "short", 10)
        self.now = SAT
        pend = self.order("XLF", "buy", 5)
        with self.assertRaises(ValueError):
            self.b.reset(10)
        acct = self.b.reset(50000)
        self.assertEqual((acct.cash, acct.equity, acct.starting_capital, acct.total_pnl), (50000, 50000, 50000, 0))
        self.assertEqual(self.b.positions(), [])
        self.assertEqual(store.get_order(pend.order_id)["status"], "cancelled")
        self.assertEqual(store.get_reset_at(), "2026-10-03T15:00:00+00:00")
        self.assertEqual(store.get_peak_equity(), 50000)
        self.assertEqual(store.cash_entries(kind="reset")[0]["kind"], "reset")
        self.assertEqual(store.get_last_accrual_date(), "2026-10-03")


class EquityTest(BrokerCase):
    def test_snapshot_peak_drawdown_day_pnl(self):
        self.order("SPY", "buy", 100)
        acct = self.b.account()
        self.assertAlmostEqual(acct.day_pnl, acct.equity - 100000)     # reset today -> vs starting capital
        self.feed.quotes["SPY"] = 520.0
        pt = self.b.snapshot_equity(datetime(2026, 10, 2, 19, tzinfo=timezone.utc), benchmark_price=520.0)
        self.assertEqual(store.get_peak_equity(), pt["equity"])
        self.assertEqual(store.get_position("SPY")["last_price"], 520.0)
        self.feed.quotes["SPY"] = 480.0
        pt2 = self.b.snapshot_equity(datetime(2026, 10, 2, 19, 30, tzinfo=timezone.utc))
        self.assertAlmostEqual(pt2["drawdown_pct"], (pt["equity"] - pt2["equity"]) / pt["equity"] * 100)
        self.assertEqual(store.get_peak_equity(), pt["equity"])
        self.now = MON
        acct = self.b.account()
        self.assertAlmostEqual(acct.day_pnl, 0.0)                     # vs Friday's last point, same price
        self.assertAlmostEqual(acct.drawdown_pct, pt2["drawdown_pct"])
        pts = store.equity_points()
        self.assertEqual(pts[-1]["drawdown_pct"], pt2["drawdown_pct"])
        self.assertEqual(pts[-2]["benchmark"], 520.0)


class ClosedTransitionTest(BrokerCase):
    """Review fix 1: long->short / short->long while the market is closed
    queues only the first leg; no rejected rows, no cancel/re-queue churn;
    the second leg follows after the first filled at the open."""

    def statuses(self):
        return sorted(o["status"] for o in store.list_orders(1000))

    def test_long_to_short_over_the_weekend(self):
        self.order("SPY", "buy", 100)
        self.now = SAT
        res = self.b.target("SPY", -0.05, "autopilot")
        self.assertEqual([(r.side, r.qty, r.status) for r in res], [("sell", 100, "pending")])
        for hour in range(10):                       # a weekend of closed cycles
            self.now = SAT + timedelta(hours=hour)
            self.assertEqual(self.b.target("SPY", -0.05, "autopilot"), [])
        self.assertEqual(self.statuses(), ["filled", "pending"])  # no rejected, no cancelled rows
        self.now = MON
        self.assertEqual([(r.side, r.status) for r in self.b.process_pending()], [("sell", "filled")])
        res = self.b.target("SPY", -0.05, "autopilot")
        self.assertEqual([(r.side, r.status) for r in res], [("short", "filled")])
        self.assertLess(self.pos("SPY"), 0)

    def test_short_to_long_waits_for_the_cover(self):
        self.order("SPY", "short", 10)
        self.now = SAT
        res = self.b.target("SPY", 0.05, "autopilot")
        self.assertEqual([(r.side, r.qty, r.status) for r in res], [("cover", 10, "pending")])
        self.assertEqual(self.b.target("SPY", 0.05, "autopilot"), [])
        self.assertNotIn("rejected", self.statuses())
        self.now = MON
        self.b.process_pending()
        self.assertEqual([(r.side, r.status) for r in self.b.target("SPY", 0.05, "autopilot")], [("buy", "filled")])

    def test_changed_target_replaces_the_pending_leg(self):
        self.order("SPY", "buy", 100)
        self.now = SAT
        first = self.b.target("SPY", -0.05, "autopilot")[0]
        res = self.b.target("SPY", 0.02, "autopilot")            # now only a reduction
        self.assertEqual([(r.side, r.qty, r.status) for r in res], [("sell", 97, "pending")])   # 2 % of ~99,975 equity = 3 shares
        self.assertEqual(store.get_order(first.order_id)["status"], "cancelled")


class ManualPendingTest(BrokerCase):
    """Review fix 2: target() never cancels pending manual orders."""

    def test_autopilot_skips_symbol_with_manual_order(self):
        self.b.ensure_initialized()
        self.now = SAT
        manual = self.order("SPY", "buy", 5)
        self.assertEqual(manual.status, "pending")
        for origin in ("autopilot", "manual-accept"):
            res = self.b.target("SPY", 0.05, origin)
            self.assertEqual((res[0].order_id, res[0].status, res[0].reject_reason),
                             (None, "rejected", "manual order pending"))
        self.assertEqual(store.get_order(manual.order_id)["status"], "pending")
        self.assertEqual(len(store.list_orders()), 1)            # nothing recorded

    def test_manual_accept_is_left_alone_by_the_autopilot(self):
        self.b.ensure_initialized()
        self.now = SAT
        acc = self.b.target("SPY", 0.05, "manual-accept")[0]
        res = self.b.target("SPY", 0.1, "autopilot")
        self.assertEqual(res[0].reject_reason, "manual order pending")
        self.assertEqual(store.get_order(acc.order_id)["status"], "pending")
        # a newer manual accept may replace the earlier accept
        res = self.b.target("SPY", 0.1, "manual-accept")
        self.assertEqual([(r.side, r.qty, r.status) for r in res], [("buy", 20, "pending")])
        self.assertEqual(store.get_order(acc.order_id)["status"], "cancelled")

    def test_autopilot_replaces_its_own_pending_order(self):
        self.b.ensure_initialized()
        self.now = SAT
        a = self.b.target("SPY", 0.05, "autopilot")[0]
        self.assertEqual([(r.side, r.qty) for r in self.b.target("SPY", 0.1, "autopilot")], [("buy", 20)])
        self.assertEqual(store.get_order(a.order_id)["status"], "cancelled")


class DividendRobustnessTest(BrokerCase):
    """Review fix 3: dedupe by (symbol, ex-date), per-symbol failures are
    retried, late listings are picked up, quantity held at the ex-date."""

    def divs(self):
        return {e["symbol"]: e["amount"] for e in store.cash_entries(kind="dividend")
                + store.cash_entries(kind="dividend_short")}

    def test_partial_failure_no_double_booking_and_retry(self):
        self.order("SPY", "buy", 100)
        self.order("XLF", "short", 40)
        self.feed.divs["SPY"] = [("2026-10-05", 1.5)]
        self.feed.divs["XLF"] = [("2026-10-05", 0.2)]
        self.feed.div_errors.add("XLF")
        self.now = MON
        self.assertAlmostEqual(self.b.daily_accruals()["dividends"], 150.0)
        self.assertEqual(store.get_last_dividend_date(), "2026-10-02")   # incomplete scan
        self.feed.div_errors.clear()
        self.now = MON + timedelta(minutes=15)
        self.assertAlmostEqual(self.b.daily_accruals()["dividends"], -8.0)   # only the retried symbol
        self.assertEqual(self.b.daily_accruals()["dividends"], 0.0)
        self.assertEqual(len(store.cash_entries(kind="dividend")), 1)
        self.assertEqual(len(store.cash_entries(kind="dividend_short")), 1)
        self.assertEqual(store.get_last_dividend_date(), "2026-10-05")

    def test_exception_in_one_symbol_does_not_stop_others(self):
        self.order("SPY", "buy", 10)
        self.order("QQQ", "buy", 10)
        self.feed.divs["QQQ"] = [("2026-10-05", 1.0)]

        def divs(sym, since):
            if sym == "SPY":
                raise RuntimeError("boom")
            return self.feed.dividends(sym, since)
        self.b.dividends_fn = divs
        self.now = MON
        self.assertAlmostEqual(self.b.daily_accruals()["dividends"], 10.0)

    def test_late_listing_is_picked_up(self):
        self.order("SPY", "buy", 10)
        self.now = MON
        self.assertEqual(self.b.daily_accruals()["dividends"], 0.0)   # not listed yet on the ex-date
        self.feed.divs["SPY"] = [("2026-10-05", 2.0)]
        self.now = TUE
        self.assertAlmostEqual(self.b.daily_accruals()["dividends"], 20.0)
        self.assertEqual(store.cash_entries(kind="dividend")[0]["note"], "ex 2026-10-05: 2/share x 10")

    def test_quantity_held_at_the_ex_date(self):
        self.order("SPY", "buy", 10)                    # Friday: held before the ex-date
        self.order("QQQ", "buy", 10)
        self.now = MON
        self.order("SPY", "sell", 4)                    # sold after the ex-date began
        self.order("QQQ", "sell", 10)                   # closed after the ex-date: still entitled
        self.order("XLE", "buy", 10)                    # bought on the ex-date: not entitled
        for sym in ("SPY", "QQQ", "XLE"):
            self.feed.divs[sym] = [("2026-10-05", 1.0)]
        self.assertAlmostEqual(self.b.daily_accruals()["dividends"], 20.0)
        self.assertEqual(self.divs(), {"SPY": 10.0, "QQQ": 10.0})

    def test_ex_dates_before_the_reset_are_ignored(self):
        self.b.ensure_initialized()
        self.order("SPY", "buy", 10)
        self.feed.divs["SPY"] = [("2026-09-30", 5.0), ("2026-10-02", 5.0)]
        self.now = MON
        self.assertEqual(self.b.daily_accruals()["dividends"], 0.0)

    def test_unreadable_ledger_books_nothing(self):
        self.order("SPY", "buy", 10)
        self.feed.divs["SPY"] = [("2026-10-05", 1.0)]
        self.now = MON
        saved = store.booked_dividend_keys
        store.booked_dividend_keys = lambda *a, **k: None
        try:
            self.assertEqual(self.b.daily_accruals()["dividends"], 0.0)
        finally:
            store.booked_dividend_keys = saved
        self.assertAlmostEqual(self.b.daily_accruals()["dividends"], 10.0)


class StateUnavailableTest(BrokerCase):
    """Review fix 4: an unreadable account never becomes "cash 0"."""

    def break_state(self):
        saved_snap, saved_cash = store.account_snapshot, store.cash_balance
        store.account_snapshot = lambda: None
        store.cash_balance = lambda: None

        def restore():
            store.account_snapshot, store.cash_balance = saved_snap, saved_cash
        self.addCleanup(restore)

    def test_views_raise_and_nothing_is_written(self):
        self.order("SPY", "buy", 100)
        n_orders, n_points = len(store.list_orders()), len(store.equity_points())
        self.break_state()
        for call in (self.b.account, self.b.snapshot_equity, self.b.check_margin):
            with self.assertRaises(StateUnavailable):
                call()
        with self.assertRaises(StateUnavailable):
            self.b.reset(50000)
        res = self.b.target("SPY", 0.2, "autopilot")
        self.assertEqual((res[0].order_id, res[0].reject_reason), (None, broker_mod.STATE_UNAVAILABLE))
        r = self.order("QQQ", "buy", 1)
        self.assertEqual((r.order_id, r.status), (None, "rejected"))
        self.assertEqual(self.b.daily_accruals(now=MON)["days"], 0)
        self.assertEqual(store.get_last_accrual_date(), "2026-10-02")   # not advanced
        self.assertEqual((len(store.list_orders()), len(store.equity_points())), (n_orders, n_points))
        self.assertEqual(self.pos("SPY"), 100)                           # reset did not clear positions

    def test_no_deposit_when_unreadable(self):
        self.break_state()
        self.assertFalse(self.b.ensure_initialized())
        self.assertIsNone(store.get_starting_capital())

    def test_pending_left_for_next_cycle(self):
        self.b.ensure_initialized()
        self.now = SAT
        self.order("SPY", "buy", 5)
        self.now = MON
        self.break_state()
        self.assertEqual(self.b.process_pending(), [])
        self.assertEqual(len(store.pending_orders()), 1)


class OpeningGraceTest(BrokerCase):
    """Review fix 9: nothing fills in the first 5 minutes of the session
    (the delayed quote can still be yesterday's close)."""

    def test_pending_waits_until_0935(self):
        self.b.ensure_initialized()
        self.now = SAT
        self.order("SPY", "buy", 5)
        self.now = MON_OPEN + timedelta(seconds=5)
        self.assertEqual(self.b.process_pending(), [])
        r = self.order("QQQ", "buy", 1)                                   # new orders queue as well
        self.assertEqual(r.status, "pending")
        self.now = MON_OPEN + timedelta(minutes=5)
        self.assertEqual(sorted((x.symbol, x.status) for x in self.b.process_pending()),
                         [("QQQ", "filled"), ("SPY", "filled")])

    def test_quote_from_before_the_open_is_stale(self):
        self.b.ensure_initialized()
        self.now = SAT
        self.order("SPY", "buy", 5)
        self.now = MON
        self.feed.quotes["SPY"] = Quote("SPY", 500.0, ts="2026-10-02T20:00:00+00:00")
        self.assertEqual(self.b.process_pending(), [])
        self.feed.quotes["SPY"] = Quote("SPY", 500.0, ts="2026-10-05T13:59:00+00:00")
        self.assertEqual(self.b.process_pending()[0].status, "filled")


if __name__ == "__main__":
    unittest.main()
