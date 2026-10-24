"""Stage 3 review fixes (2026-10-04): absolute price-level warnings
(validator + run-level two-sources warning), the engine's participation-cut
flag used by capacity, no defaults for missing bar fields. Offline.
Part of UFOS.ai (proof of concept)."""
import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strategy_platform.backtest import engine, metrics  # noqa: E402
from strategy_platform.core import calendars  # noqa: E402
from strategy_platform.strategy import alphas, validator  # noqa: E402
import test_sp_backtest_support as sup  # noqa: E402

CLOSE = {"field": "close"}


def codes(spec):
    return [(w["path"], w["code"]) for w in validator.validate(spec).warnings if w["code"] == "absolute_price_level"]


def base(signal, **kw):
    s = {"schema": "ufos.strategy/1", "name": "x", "universe": {"symbols": ["SPY"]}, "signal": signal}
    s.update(kw)
    return s


class AbsoluteLevelValidatorTest(unittest.TestCase):
    def test_level_vs_constant_or_param_warns(self):
        self.assertEqual(codes(base({"op": "gt", "args": [CLOSE, 100]})), [("$.signal", "absolute_price_level")])
        self.assertTrue(codes(base({"op": "le", "args": [{"op": "sma", "args": [{"field": "vwap"}, 20]}, 50]})))
        self.assertTrue(codes(base({"op": "min", "args": [{"field": "high"}, 300]})))
        self.assertTrue(codes(base({"op": "clip", "args": [CLOSE, 10, 500]})))
        self.assertTrue(codes(base({"op": "where", "args": [{"op": "ge", "args": [{"field": "open"}, 20]}, 1, 0]})))
        with_param = base({"op": "gt", "args": [CLOSE, {"param": "lvl"}]},
                          parameters={"lvl": {"value": 100, "min": 1, "max": 1000}})
        self.assertTrue(codes(with_param))
        via_ref = base({"op": "gt", "args": [{"ref": "px"}, 100]},
                       features={"px": {"op": "lag", "args": [CLOSE, 1]}})
        self.assertEqual(codes(via_ref), [("$.signal", "absolute_price_level")])
        thr = base(CLOSE, portfolio={"mode": "long_only", "selection": {"type": "threshold", "long_above": 100},
                                     "weighting": "equal"})
        self.assertEqual(codes(thr), [("$.portfolio.selection", "absolute_price_level")])
        msg = validator.validate(base({"op": "gt", "args": [CLOSE, 100]})).warnings
        self.assertIn("Tiingo", next(w["message"] for w in msg if w["code"] == "absolute_price_level"))

    def test_scale_free_comparisons_do_not_warn(self):
        sma = {"op": "sma", "args": [CLOSE, 50]}
        self.assertEqual(codes(base({"op": "gt", "args": [CLOSE, sma]})), [])
        self.assertEqual(codes(base({"op": "gt", "args": [{"op": "div", "args": [CLOSE, sma]}, 1.05]})), [])
        self.assertEqual(codes(base({"op": "gt", "args": [{"op": "delta", "args": [CLOSE, 1]}, 0]})), [])
        self.assertEqual(codes(base({"op": "gt", "args": [{"op": "rsi", "args": [14]}, 70]})), [])
        self.assertEqual(codes(base({"op": "gt", "args": [{"field": "volume"}, 1000]})), [])

    def test_library_is_warning_free(self):
        bad = [e["key"] for e in alphas.library() if codes(e["spec"])]
        self.assertEqual(bad, [])


class RunLevelTwoSourcesTest(unittest.TestCase):
    def run_from(self, start_days):
        days = calendars.trading_days(date(2015, 1, 2), date(2016, 6, 30))
        prices = {"AAA": sup.series(60, days), "SPY": sup.series(200, days)}
        spec = sup.spec(["AAA"], signal={"op": "gt", "args": [CLOSE, 50]})
        return engine.run(spec, start=start_days, end=date(2016, 6, 30), data=sup.FakeLoader(prices))

    def test_warns_only_before_the_seam(self):
        before = [w["code"] for w in self.run_from(date(2015, 9, 1)).warnings]
        self.assertIn("absolute_price_level", before)
        self.assertIn("absolute_level_two_sources", before)
        after = [w["code"] for w in self.run_from(date(2016, 4, 1)).warnings]
        self.assertIn("absolute_price_level", after)
        self.assertNotIn("absolute_level_two_sources", after)


class ParticipationFlagTest(unittest.TestCase):
    def test_cut_orders_are_flagged_and_counted(self):
        prices = {"AAA": sup.series(37.3), "SPY": sup.series(400)}
        vols = {"AAA": {d: 2_000_000.0 for d in prices["AAA"]}}      # cap = 20,000 $ per order
        res = engine.run(sup.spec(["AAA"]), data=sup.FakeLoader(prices, volumes=vols))
        cut = [f for f in res.fills if f["participation_cut"]]
        self.assertTrue(cut)
        self.assertTrue(all(f["notional"] < 0.01 * 2_000_000.0 * 1.001 for f in cut))
        self.assertTrue(any(f["notional"] / 20_000.0 < metrics.CAPACITY_AT_LIMIT for f in cut))   # whole shares
        self.assertLessEqual(len(cut), res.stats["participation_cuts"])
        cap = metrics.capacity(res)
        self.assertGreaterEqual(cap["orders_at_limit"], len([f for f in cut if f["origin"] == "autopilot"]))


class NoDefaultBarFieldsTest(unittest.TestCase):
    def test_missing_low_is_a_data_error(self):
        prices = {"AAA": sup.series(50), "SPY": sup.series(400)}

        class NoLow(sup.FakeLoader):
            def history(self, symbol, adjustment, start, end=None, *, progress=None):
                rows, info = super().history(symbol, adjustment, start, end, progress=progress)
                if symbol == "AAA" and adjustment == "split":
                    rows[5] = dict(rows[5], low=None)
                return rows, info

        with self.assertRaises(engine.BacktestDataError) as cm:
            engine.run(sup.spec(["AAA"]), data=NoLow(prices))
        self.assertIn("low", cm.exception.user_message)

    def test_crypto_known_at_uses_the_pit_rule(self):
        row = sup.cr_bar("BTC-USD", date(2024, 1, 2), 1.0, 1.0)
        explicit = engine._crypto_known(row)
        row.pop("known_at")
        self.assertEqual(engine._crypto_known(row), explicit)          # pit.known_at = start + 1 day
        self.assertEqual(explicit - engine._ts(row["ts"]), timedelta(days=1))


if __name__ == "__main__":
    unittest.main()
