"""24/7 crypto in the HedgeFund paper engine — detection, params, calendar
helpers and the paper broker (fund/SPEC.md §3, §5, §6): fractional sizing
and the $10 minimum, crypto fees, perp shorts with isolated margin and the
liquidation fee, funding catch-up and sign, no Rule 201 / borrow fee /
dividends for crypto. Engine/agents/backtest/API: test_fund_crypto_engine.py
and test_fund_crypto_misc.py. Paper trading only; offline (fakes, temp DB).

Run from HedgeFund/backend:  python -m unittest tests.test_fund_crypto -v
"""
import unittest
from datetime import date, datetime, timedelta, timezone

try:
    from crypto_testkit import BTC, ETH, CryptoCase
    from fund_testkit import FRI, MON, SAT
except ImportError:  # pragma: no cover - run as package
    from tests.crypto_testkit import BTC, ETH, CryptoCase
    from tests.fund_testkit import FRI, MON, SAT

from fund import market
from fund import params as P
from fund import store
from fund.agents import macro as macro_agent
from fund.broker import PROTECTIVE_ORIGINS, fmt_qty, round_qty
from fund.types import Quote


class DetectionTest(unittest.TestCase):
    def test_crypto_symbols(self):
        for s in ("BTC-USD", "ETH-USD", "SOL-USD", "1INCH-USD", "DOGE-USD"):
            self.assertTrue(P.is_crypto(s), s)
            self.assertEqual(P.asset_type(s), "crypto")
            self.assertEqual(P.normalise_symbol(s.lower()), s)
        for s in ("SPY", "BRK.B", "BTCUSD", "BTC-EUR", "X-USD", "SPY-"):
            self.assertFalse(P.is_crypto(s), s)
        self.assertEqual(P.asset_type("SPY"), "equity")
        self.assertTrue(set(P.CRYPTO_ROLLOUT_SYMBOLS) <= set(P.KNOWN_CRYPTO))
        self.assertEqual(macro_agent.asset_class(BTC), "crypto")
        self.assertEqual(macro_agent.asset_class("XLK"), "equities_growth")

    def test_defaults_limits_and_validation(self):
        d = P.defaults()
        self.assertEqual(len(d["watchlist"]), 11)
        self.assertEqual(d["watchlist"][-3:], ["BTC-USD", "ETH-USD", "SOL-USD"])
        expect = {"crypto_fees_bps": 20.0, "crypto_max_position_pct": 3.0, "crypto_initial_margin_pct": 50.0,
                  "crypto_maintenance_margin_pct": 25.0, "crypto_funding_rate_pct_8h": 0.01,
                  "crypto_liquidation_fee_pct": 0.5, "crypto_risk_watch": True}
        for k, v in expect.items():
            self.assertEqual(d[k], v, k)
            self.assertIn(k, P.LIMITS)
        lim = P.LIMITS["crypto_funding_rate_pct_8h"]
        self.assertEqual((lim["min"], lim["max"]), (-0.5, 0.5))
        self.assertIn("Risk-watch", P.LIMITS["closed_interval_minutes"]["label"])
        clean, errors = P.validate({"watchlist": ["btc-usd", "spy"], "crypto_funding_rate_pct_8h": -0.25,
                                    "crypto_risk_watch": False})
        self.assertEqual(errors, {})
        self.assertEqual(clean["watchlist"], ["BTC-USD", "SPY"])
        _c, errors = P.validate({"crypto_maintenance_margin_pct": 60})
        self.assertIn("crypto_maintenance_margin_pct", errors)
        _c, errors = P.validate({"crypto_funding_rate_pct_8h": 0.6, "crypto_fees_bps": 101,
                                 "crypto_max_position_pct": 0.4, "crypto_risk_watch": "yes"})
        self.assertEqual(set(errors), {"crypto_funding_rate_pct_8h", "crypto_fees_bps", "crypto_max_position_pct",
                                       "crypto_risk_watch"})
        self.assertEqual(P.borrow_rate_for(BTC, d), 0.0)
        self.assertEqual((P.fees_bps_for(BTC, d), P.fees_bps_for("SPY", d)), (20.0, 5.0))
        self.assertEqual((P.max_position_pct_for(BTC, d), P.max_position_pct_for("SPY", d)), (3.0, 5.0))
        self.assertEqual(P.margin_pcts_for(BTC, d), (50.0, 25.0))
        self.assertEqual(P.margin_pcts_for("SPY", d), (50.0, 30.0))

    def test_quantity_helpers(self):
        self.assertEqual(round_qty(BTC, 0.123456789), 0.12345678)
        self.assertEqual(round_qty("SPY", 9.99), 9)
        self.assertEqual(fmt_qty(0.054), "0.054")
        self.assertEqual(fmt_qty(9.0), "9")


class CalendarTest(unittest.TestCase):
    def test_tradability_and_funding_windows(self):
        self.assertTrue(market.is_tradable(BTC, SAT))
        self.assertFalse(market.is_tradable("SPY", SAT))
        self.assertTrue(market.is_tradable("SPY", FRI))
        self.assertEqual(market.last_funding_window(SAT), SAT.replace(hour=8))
        w = market.funding_windows_between(FRI.replace(hour=8), SAT)
        self.assertEqual(w, [FRI.replace(hour=16), SAT.replace(hour=0), SAT.replace(hour=8)])
        self.assertEqual(market.funding_windows_between(SAT, SAT), [])
        self.assertEqual(market.utc_days_between(datetime(2026, 10, 3, 23, 0, tzinfo=timezone.utc),
                                                 datetime(2026, 10, 4, 1, 0, tzinfo=timezone.utc)), 1)


class CryptoBrokerTest(CryptoCase):
    def test_fractional_fill_on_a_saturday(self):
        self.now = SAT
        r = self.order(BTC, "buy", qty=0.123456789)
        self.assertEqual((r.status, r.qty), ("filled", 0.12345678))
        self.assertAlmostEqual(self.qty(BTC), 0.12345678)
        self.assertEqual(store.pending_orders(), [])
        self.assertEqual(self.order(BTC, "buy", notional=1000).qty, 0.02)
        self.assertEqual(self.order("SPY", "buy", qty=3).status, "pending")    # equities wait for the open

    def test_minimum_notional(self):
        self.now = SAT
        r = self.order(BTC, "buy", notional=5)
        self.assertEqual(r.status, "rejected")
        self.assertTrue(r.reject_reason.startswith("size below minimum"))
        self.assertEqual(self.order(BTC, "buy", qty=0.0001).status, "rejected")    # $5
        self.assertEqual(self.order(BTC, "buy", qty=0.0003).status, "filled")      # $15
        self.assertEqual(self.order(BTC, "sell", qty=0.0001).status, "rejected")   # partial $5
        self.feed.quotes[BTC] = 20000.0                                          # position now $6
        self.assertEqual(self.order(BTC, "sell", qty=0.0003).status, "filled")     # full close of dust
        self.assertEqual(self.qty(BTC), 0)

    def test_equities_stay_whole_shares(self):
        r = self.order("SPY", "buy", qty=2.7)
        self.assertEqual((r.status, r.qty), ("filled", 2))
        self.assertEqual(self.order("SPY", "buy", qty=0.5).reject_reason, "size below one share")

    def test_target_uses_fractional_quantities_and_both_legs_24_7(self):
        self.now = SAT
        res = self.broker.target(BTC, 0.027, "autopilot", self.now, self.params())
        self.assertEqual([(r.side, r.qty, r.status) for r in res], [("buy", 0.054, "filled")])
        self.assertEqual(self.broker.target(BTC, 0.027, "autopilot", self.now, self.params()), [])
        res = self.broker.target(BTC, -0.02, "autopilot", self.now, self.params())
        self.assertEqual([(r.side, r.status) for r in res], [("sell", "filled"), ("short", "filled")])
        self.assertAlmostEqual(self.qty(BTC), -0.04)
        # a change below the $10 minimum is not traded
        self.assertEqual(self.broker.target(BTC, -0.02005, "autopilot", self.now, self.params()), [])

    def test_crypto_fees(self):
        self.set_params(crypto_fees_bps=20.0)
        self.now = SAT
        r = self.order(BTC, "buy", qty=0.1)
        self.assertAlmostEqual(r.fill_price, 50000 * 1.001)
        self.assertAlmostEqual(r.fees, 0.1 * 50000 * 1.001 * 0.001)

    def test_perp_short_margin_and_liquidation_fee(self):
        self.now = SAT
        self.assertEqual(self.order(BTC, "short", qty=0.5).status, "filled")
        acct = self.broker.account(now=self.now)
        self.assertAlmostEqual(acct.short_mv, 25000)
        self.assertAlmostEqual(acct.buying_power, 125000 - 25000 * 1.5)
        self.assertAlmostEqual(acct.maintenance_required, 0.25 * 25000)
        self.assertAlmostEqual(acct.crypto_margin_excess, 12500 - 0.25 * 25000)
        pos = self.broker.positions()[0]
        self.assertEqual((pos.asset_class, pos.instrument, pos.borrow_rate_pct), ("crypto", "perp", None))
        self.feed.quotes[BTC] = 60000.0            # posted 12500 - loss 5000 - maint 7500 = 0 -> kept
        self.assertEqual(self.broker.check_margin(self.now), [])
        self.feed.quotes[BTC] = 61000.0            # 12500 - 5500 - 7625 < 0 -> liquidation
        res = self.broker.check_margin(self.now)
        self.assertEqual([(r.side, r.qty, r.status, r.origin) for r in res],
                         [("cover", 0.5, "filled", "margin-call")])
        self.assertAlmostEqual(res[0].fees, 0.005 * 30500)
        liq = self.ledger("liquidation_fee")
        self.assertEqual(len(liq), 1)
        self.assertAlmostEqual(liq[0]["amount"], -152.5)
        self.assertEqual(self.qty(BTC), 0)
        ev = [e for e in store.events_after(0) if e["kind"] == "margin_call"]
        self.assertTrue(ev and ev[-1]["title"].startswith("Liquidation"))
        self.assertAlmostEqual(self.broker.account(now=self.now).equity, 100000 - 5500 - 152.5)

    def test_crypto_short_initial_margin_uses_crypto_pct(self):
        self.set_params(crypto_initial_margin_pct=100.0, crypto_maintenance_margin_pct=25.0,
                        short_exposure_max_pct=100.0)
        self.now = SAT
        r = self.order(BTC, "short", qty=3, allow_reduce=True)     # BP 100k at 100 % -> 2 BTC at 50k
        self.assertEqual((r.status, r.qty), ("filled", 2.0))

    def test_short_exposure_cap_is_gross_over_both_classes(self):
        self.order("SPY", "short", qty=40)                          # 20k of the 30k cap (FRI, session)
        r = self.order(BTC, "short", qty=0.3)                       # +15k > 30k
        self.assertEqual(r.status, "rejected")
        self.assertIn("short exposure limit", r.reject_reason)

    def test_funding_catch_up_and_sign(self):
        self.broker.ensure_initialized(FRI)
        self.assertEqual(store.get_last_funding_at(), FRI.replace(hour=8).isoformat())
        self.order(BTC, "short", qty=0.5)                           # FRI 14:00
        self.order(ETH, "buy", qty=1)                               # spot long: no funding
        self.now = SAT                                              # 15:00: windows FRI 16, SAT 00, SAT 08
        out = self.broker.daily_accruals(self.now)
        self.assertEqual(out["funding_windows"], 3)
        self.assertAlmostEqual(out["funding"], 0.5 * 50000 * 0.0001 * 3)       # + = the short receives
        rows = self.ledger("funding")                               # one row per window, deduplicated
        self.assertEqual(sorted((r["symbol"], round(r["amount"], 6), r["note"][:16]) for r in rows),
                         [(BTC, 2.5, "fw 2026-10-02T16"), (BTC, 2.5, "fw 2026-10-03T00"),
                          (BTC, 2.5, "fw 2026-10-03T08")])
        self.assertEqual(self.broker.daily_accruals(self.now)["funding"], 0.0)  # booked once
        self.assertEqual(store.get_last_funding_at(), SAT.replace(hour=8).isoformat())
        self.assertAlmostEqual(self.broker.account(now=self.now).funding_total, 7.5)
        self.set_params(crypto_funding_rate_pct_8h=-0.02)           # negative: the short pays
        self.now = SAT.replace(hour=16, minute=30)
        out = self.broker.daily_accruals(self.now)
        self.assertAlmostEqual(out["funding"], -0.5 * 50000 * 0.0002)
        self.assertAlmostEqual(self.broker.account(now=self.now).funding_total, 2.5)

    def test_funding_only_for_windows_after_the_open(self):
        self.broker.ensure_initialized(FRI)
        self.now = SAT.replace(hour=9)
        self.order(BTC, "short", qty=0.5)                           # after the SAT 08:00 window
        self.broker.daily_accruals(self.now)
        self.assertEqual(self.ledger("funding"), [])
        self.now = SAT.replace(hour=17)
        self.assertEqual(self.broker.daily_accruals(self.now)["funding_windows"], 1)

    def test_no_rule201_borrow_or_dividends_for_crypto(self):
        self.feed.quotes[BTC] = Quote(symbol=BTC, price=42000.0, prev_close=50000.0)
        self.feed.quotes["SPY"] = Quote(symbol="SPY", price=440.0, prev_close=500.0)
        self.assertIn("Rule 201", self.order("SPY", "short", qty=1).reject_reason)
        self.assertEqual(self.order(BTC, "short", qty=0.1).status, "filled")
        self.feed.divs[BTC] = [(date(2026, 10, 5), 1.0)]
        self.now = MON + timedelta(days=3)
        self.broker.daily_accruals(self.now)
        self.assertEqual(self.ledger("borrow_fee"), [])
        self.assertEqual(self.ledger("dividend") + self.ledger("dividend_short"), [])
        self.assertIsNone(store.get_position(BTC)["borrow_rate_pct"])

    def test_process_pending_origins_filter(self):
        self.now = SAT
        self.broker.ensure_initialized(self.now)
        oid = store.insert_order(self.now, BTC, "buy", 0.01, "pending", "autopilot")
        self.assertEqual(self.broker.process_pending(self.now, origins=PROTECTIVE_ORIGINS), [])
        self.assertEqual(store.get_order(oid)["status"], "pending")
        res = self.broker.process_pending(self.now)
        self.assertEqual([(r.order_id, r.status) for r in res], [(oid, "filled")])


if __name__ == "__main__":
    unittest.main()
