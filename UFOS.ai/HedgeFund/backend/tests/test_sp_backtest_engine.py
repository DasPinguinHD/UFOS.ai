"""Stage 3c backtest engine (SPEC §8.3) on fake data: dividends (long
receives, short pays, nothing when bought on the ex-date), participation
cut, missing-volume refusal, next_open vs next_close timing (no same-bar
fill), stop loss, drawdown stop, custom costs, range end rules, benchmark,
determinism, cancel + progress. Offline, fakes only.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_backtest_engine -v
"""
import unittest
from datetime import date

try:
    from test_sp_backtest_support import DAYS, FakeLoader, series, spec
except ImportError:  # pragma: no cover - run as package
    from tests.test_sp_backtest_support import DAYS, FakeLoader, series, spec

from strategy_platform.backtest import engine
from strategy_platform.core.errors import InvalidRequest

LS = dict(mode="long_short", selection={"type": "threshold", "long_above": 100, "short_below": 50})
START = "2024-02-15"
I0 = DAYS.index(date(2024, 2, 15))
HALF = 5.0 / 2 / 10000          # fund default fees_bps 5 -> half slippage, half fee


def codes(r):
    return {w["code"]: w for w in r.warnings}


class DividendTest(unittest.TestCase):
    def test_long_receives_short_pays_new_buyer_gets_nothing(self):
        ex = DAYS[I0 + 10]
        gld = {d: ((90.0, 90.0) if d < DAYS[I0 + 9] else (110.0, 110.0)) for d in DAYS}   # enters on the ex-date
        loader = FakeLoader({"SPY": series(150), "TLT": series(20), "GLD": gld},
                            dividends={"SPY": [(ex, 1.5)], "TLT": [(ex, 0.07)], "GLD": [(ex, 2.0)]})
        r = engine.run(spec(["SPY", "TLT", "GLD"], **LS), data=loader, start=START)
        divs = [l for l in r.ledger if l["kind"].startswith("dividend")]
        self.assertEqual(sorted((l["symbol"], l["kind"]) for l in divs), [("SPY", "dividend"),
                                                                         ("TLT", "dividend_short")])
        held_before = [p for p in r.positions if p["date"] < ex.isoformat()][-1]["positions"]
        by = {l["symbol"]: l for l in divs}
        self.assertAlmostEqual(by["SPY"]["amount"], held_before["SPY"] * 1.5)
        self.assertAlmostEqual(by["TLT"]["amount"], held_before["TLT"] * 0.07)      # negative qty -> pays
        self.assertLess(by["TLT"]["amount"], 0)
        self.assertTrue(by["SPY"]["ts"].startswith(ex.isoformat()))
        gld_fill = [f for f in r.fills if f["symbol"] == "GLD"][0]
        self.assertTrue(gld_fill["ts"].startswith(ex.isoformat()))                  # bought ON the ex-date
        # benchmark (SPY buy and hold) gets the dividend too, as cash
        self.assertEqual([l["kind"] for l in r.benchmark_info["ledger"] if l["kind"] == "dividend"], ["dividend"])

    def test_dividend_without_amount_stops(self):
        loader = FakeLoader({"SPY": series(150)}, dividends={"SPY": [(DAYS[I0 + 3], None)]})
        with self.assertRaises(engine.BacktestDataError):
            engine.run(spec(["SPY"]), data=loader, start=START)


class ParticipationTest(unittest.TestCase):
    def test_cut_to_one_percent_of_adv_excess_dropped(self):
        prices = series(150)
        loader = FakeLoader({"SPY": prices}, volumes={"SPY": {d: 1_000_000.0 for d in DAYS}})
        r = engine.run(spec(["SPY"], rebalance="monthly"), data=loader, start=START, benchmark="SPY")
        first = r.fills[0]
        o = prices[DAYS[I0 + 1]][0]
        self.assertEqual(first["qty"], int(10_000.0 / o))                 # 1 % of $1M ADV
        self.assertTrue(first["ts"].startswith(DAYS[I0 + 1].isoformat()))
        # not carried: no more buys until the next (monthly) rebalance in March
        feb = [f for f in r.fills if f["ts"] < "2024-03-01"]
        self.assertEqual(len(feb), 1)
        w = codes(r)["participation_cut"]
        self.assertEqual(w["symbol"], "SPY")
        self.assertGreater(w["dropped_notional"], 0)
        self.assertEqual(r.stats["participation_cuts"], w["count"])

    def test_missing_volume_refuses_the_order(self):
        prices = series(150)
        vol = {d: 1e12 for d in DAYS if d != DAYS[I0]}
        loader = FakeLoader({"SPY": prices}, volumes={"SPY": vol})
        r = engine.run(spec(["SPY"]), data=loader, start=START)
        w = codes(r)["missing_volume"]
        self.assertEqual(w["dates"][0], DAYS[I0 + 1].isoformat())
        self.assertEqual(w["count"], 20)                                  # the gap sits in 20 windows
        self.assertTrue(r.fills[0]["ts"].startswith(DAYS[I0 + 21].isoformat()))

    def test_no_volume_rows_at_all_refuses(self):
        loader = FakeLoader({"SPY": series(150)}, volumes={"SPY": None})
        r = engine.run(spec(["SPY"]), data=loader, start=START, benchmark="SPY")
        self.assertEqual(r.fills, [])
        self.assertIn("missing_volume", codes(r))


class TimingTest(unittest.TestCase):
    def test_next_open_and_next_close_fill_on_the_next_day(self):
        prices = series(150)
        d1 = DAYS[I0 + 1]
        r_open = engine.run(spec(["SPY"]), data=FakeLoader({"SPY": prices}), start=START)
        r_close = engine.run(spec(["SPY"], timing="next_close"), data=FakeLoader({"SPY": prices}), start=START)
        fo, fc = r_open.fills[0], r_close.fills[0]
        self.assertEqual(fo["ts"], f"{d1.isoformat()}T14:36:00+00:00")       # 09:36 EST
        self.assertEqual(fc["ts"], f"{d1.isoformat()}T20:59:00+00:00")       # 15:59 EST
        self.assertAlmostEqual(fo["fill_price"], prices[d1][0] * (1 + HALF))
        self.assertAlmostEqual(fc["fill_price"], prices[d1][1] * (1 + HALF))
        self.assertEqual(r_open.positions[0]["positions"], {})              # day 0: decision only, no fill


class RiskTest(unittest.TestCase):
    def test_stop_loss_exits_and_stays_flat_until_next_rebalance(self):
        prices = series(150, amp=0.0, drift=0.0)
        crash = DAYS[I0 + 5]
        for d in DAYS:
            if d >= crash:
                prices[d] = (150.0 * 0.85, 150.0 * 0.85)
        r = engine.run(spec(["SPY"], rebalance="monthly", risk={"stop_loss_pct": 10}),
                       data=FakeLoader({"SPY": prices}), start=START)
        sells = [f for f in r.fills if f["side"] == "sell"]
        self.assertTrue(sells and sells[0]["ts"].startswith(DAYS[I0 + 6].isoformat()))
        buys_after = [f for f in r.fills if f["side"] == "buy" and f["ts"] > sells[0]["ts"]]
        self.assertTrue(buys_after and buys_after[0]["ts"] >= "2024-03-01")    # re-entry at the March rebalance
        self.assertEqual(r.stats["stop_exits"], 1)
        self.assertIn("stop_loss", codes(r))

    def test_drawdown_stop_liquidates_and_halts(self):
        prices = series(150, amp=0.0, drift=0.0)
        for d in DAYS:
            if d >= DAYS[I0 + 5]:
                prices[d] = (120.0, 120.0)
        r = engine.run(spec(["SPY"], risk={"max_drawdown_stop_pct": 10}), data=FakeLoader({"SPY": prices}),
                       start=START)
        self.assertIn("drawdown_stop", codes(r))
        self.assertEqual(r.positions[-1]["positions"], {})
        self.assertFalse([f for f in r.fills if f["side"] == "buy" and f["ts"] > DAYS[I0 + 6].isoformat()])

    def test_custom_costs_override_for_the_run(self):
        prices = series(150)
        r = engine.run(spec(["SPY"], costs={"model": "custom", "fee_bps": 10, "slippage_bps": 30}),
                       data=FakeLoader({"SPY": prices}), start=START)
        self.assertEqual(r.costs["fees_bps"], 40.0)
        self.assertAlmostEqual(r.fills[0]["fill_price"], prices[DAYS[I0 + 1]][0] * (1 + 0.002))
        self.assertIn("custom_costs_split", codes(r))
        from strategy_platform.core import params as fund_params
        self.assertEqual(fund_params.DEFAULTS["fees_bps"], 5.0)                 # nothing global changed


class RangeTest(unittest.TestCase):
    def test_default_end_is_limited_by_the_shortest_source(self):
        loader = FakeLoader({"SPY": series(150), "TLT": series(20)}, volume_last=DAYS[-5])
        r = engine.run(spec(["SPY", "TLT"], **LS), data=loader, start=START)
        self.assertEqual(r.range["end"], DAYS[-5].isoformat())
        self.assertIn("volume", r.range["end_limited_by"]["source"])
        with self.assertRaises(InvalidRequest) as ctx:
            engine.run(spec(["SPY", "TLT"], **LS), data=loader, start=START, end=DAYS[-2].isoformat())
        self.assertIn("volume", str(ctx.exception))

    def test_bad_requests(self):
        loader = FakeLoader({"SPY": series(150)})
        for kw in ({"capital": 10}, {"capital": "x"}, {"benchmark": "not a symbol!"}, {"start": "2024-03-01",
                                                                                        "end": "2024-02-01"}):
            with self.assertRaises(InvalidRequest):
                engine.run(spec(["SPY"]), data=loader, **kw)
        with self.assertRaises(InvalidRequest):
            engine.run({"schema": "ufos.strategy/1"}, data=loader)

    def test_symbol_without_data_is_excluded_and_reported(self):
        r = engine.run(spec(["SPY", "GLD"]), data=FakeLoader({"SPY": series(150)}), start=START, benchmark="SPY")
        self.assertIn("symbol_excluded", codes(r))
        self.assertTrue(r.data["GLD"]["excluded"])

    def test_default_start_after_warmup(self):
        r = engine.run(spec(["SPY"]), data=FakeLoader({"SPY": series(150)}))
        self.assertEqual(r.dates[0], DAYS[r.range["warmup_sessions"]].isoformat())


class SpecBenchmarkTest(unittest.TestCase):
    BASE = {"schema": "ufos.strategy/1", "name": "x", "universe": {"symbols": ["SPY"]}, "signal": {"field": "close"}}
    # content_hash of BASE before the optional key existed (stage 2): must never change
    OLD_HASH = "62133186922b7932794a607b5ece9a65333b7b780a4c85af71f5d4e669b5397f"

    def test_optional_key_validated_and_not_default_filled(self):
        from strategy_platform.strategy import schema, validator
        self.assertEqual(schema.content_hash(self.BASE), self.OLD_HASH)
        self.assertNotIn("benchmark", schema.with_defaults(self.BASE))
        self.assertTrue(validator.validate(dict(self.BASE, benchmark="QQQ")).ok)
        for bad in ("bad sym!", 3, None):
            self.assertEqual(validator.validate(dict(self.BASE, benchmark=bad)).codes(), ["bad_symbol"])
        self.assertNotEqual(schema.content_hash(dict(self.BASE, benchmark="QQQ")), self.OLD_HASH)

    def test_engine_uses_request_then_spec_then_spy(self):
        loader = lambda: FakeLoader({"SPY": series(150), "QQQ": series(400), "GLD": series(180)})
        s = dict(spec(["SPY"]), benchmark="QQQ")
        self.assertEqual(engine.run(s, data=loader(), start=START).benchmark_info["symbol"], "QQQ")
        self.assertEqual(engine.run(s, data=loader(), start=START, benchmark="GLD").benchmark_info["symbol"], "GLD")
        self.assertEqual(engine.run(spec(["GLD"]), data=loader(), start=START).benchmark_info["symbol"], "SPY")


class DeterminismCancelTest(unittest.TestCase):
    def test_same_inputs_same_result(self):
        mk = lambda: FakeLoader({"SPY": series(150), "TLT": series(20)}, dividends={"SPY": [(DAYS[I0 + 7], 1.0)]})
        a = engine.run(spec(["SPY", "TLT"], **LS), data=mk(), start=START).to_dict()
        b = engine.run(spec(["SPY", "TLT"], **LS), data=mk(), start=START).to_dict()
        self.assertEqual(a, b)
        self.assertEqual(a["engine_version"], engine.ENGINE_VERSION)
        self.assertEqual(len(a["equity"]), len(a["dates"]))
        self.assertEqual(len(a["benchmark"]), len(a["dates"]))

    def test_cancel_and_progress(self):
        events = []
        calls = {"n": 0}

        def cancel():
            calls["n"] += 1
            return calls["n"] > 10

        with self.assertRaises(engine.BacktestCancelled):
            engine.run(spec(["SPY"]), data=FakeLoader({"SPY": series(150)}), start=START,
                       progress=lambda *a: events.append(a), cancel=cancel)
        self.assertEqual(events[0][0], "data")
        self.assertTrue(any(e[0] == "simulate" for e in events))
        self.assertTrue(all(0 <= e[1] <= 100 for e in events))


if __name__ == "__main__":
    unittest.main()
