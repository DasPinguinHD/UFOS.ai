"""strategy/portfolio.py + sdk.py: target weights (modes, top/bottom/
top_bottom_n, threshold, all, signal & inverse-vol weighting, max_weight not
redistributed, ties by symbol), rebalance dates, SDK step-by-step ==
vectorised. Offline.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_strategy_portfolio -v
"""
import math
import unittest
from datetime import date

import numpy as np

try:
    from test_sp_strategy_support import synthetic_panel
except ImportError:  # pragma: no cover
    from tests.test_sp_strategy_support import synthetic_panel

from strategy_platform.strategy import portfolio, sdk
from strategy_platform.strategy.panel import Panel

NAN = math.nan
SYMS = ["A", "B", "C", "D", "E"]


def panel_with(close=None, n=5):
    close = np.ones((n, len(SYMS))) if close is None else np.asarray(close, dtype=float)
    return Panel([date(2024, 1, 1 + i) for i in range(close.shape[0])], SYMS, {"close": close})


def spec(mode="long_only", selection=None, weighting="equal", **pf):
    p = {"mode": mode, "selection": selection or {"type": "all"}, "weighting": weighting, "vol_lookback": 3,
         "max_weight": 1.0, "gross_exposure": 1.0}
    p.update(pf)
    return {"schema": "ufos.strategy/1", "name": "t", "universe": {"symbols": SYMS},
            "signal": {"field": "close"}, "portfolio": p}


def tw(s, row, panel=None, t=None):
    panel = panel or panel_with()
    return portfolio.target_weights(s, row, panel, len(panel) - 1 if t is None else t)


class SelectionTest(unittest.TestCase):
    row = [3.0, 1.0, 2.0, NAN, 5.0]

    def test_top_n_long(self):
        self.assertEqual(tw(spec(selection={"type": "top_n", "n": 2}), self.row), {"A": 0.5, "E": 0.5})

    def test_top_n_short_only(self):
        self.assertEqual(tw(spec("short_only", {"type": "top_n", "n": 2}), self.row), {"A": -0.5, "E": -0.5})

    def test_bottom_n(self):
        self.assertEqual(tw(spec(selection={"type": "bottom_n", "n": 2}), self.row), {"B": 0.5, "C": 0.5})
        self.assertEqual(tw(spec("long_short", {"type": "bottom_n", "n": 1}), self.row), {"B": -1.0})

    def test_top_bottom_n(self):
        w = tw(spec("long_short", {"type": "top_bottom_n", "n": 1}), self.row)
        self.assertEqual(w, {"B": -0.5, "E": 0.5})
        # fewer eligible symbols than 2n: never on both sides
        w = tw(spec("long_short", {"type": "top_bottom_n", "n": 3}), [1.0, 2.0, NAN, NAN, 3.0])
        self.assertEqual(set(w), {"A", "B", "E"})
        self.assertEqual(sum(1 for v in w.values() if v > 0), 3)

    def test_threshold(self):
        s = spec("long_short", {"type": "threshold", "long_above": 2.0, "short_below": 1.5})
        self.assertEqual(tw(s, self.row), {"A": 1 / 3, "B": -1 / 3, "E": 1 / 3})
        s = spec("long_only", {"type": "threshold", "long_above": 3.0})        # strict: 3.0 is not > 3.0
        self.assertEqual(tw(s, self.row), {"E": 1.0})

    def test_all_modes(self):
        row = [1.0, -2.0, 0.0, NAN, 3.0]
        self.assertEqual(set(tw(spec("long_only"), row)), {"A", "B", "C", "E"})
        w = tw(spec("long_short"), row)
        self.assertEqual(w, {"A": 1 / 3, "B": -1 / 3, "E": 1 / 3})       # sign-based, 0 not held

    def test_ties_by_symbol(self):
        w = tw(spec(selection={"type": "top_n", "n": 2}), [1.0, 1.0, 1.0, 1.0, 1.0])
        self.assertEqual(w, {"A": 0.5, "B": 0.5})
        w = tw(spec(selection={"type": "bottom_n", "n": 1}), [2.0, 1.0, 1.0, 3.0, 3.0])
        self.assertEqual(w, {"B": 1.0})

    def test_nan_only(self):
        self.assertEqual(tw(spec(selection={"type": "top_n", "n": 2}), [NAN] * 5), {})


class WeightingTest(unittest.TestCase):
    def test_signal_weighting(self):
        w = tw(spec(selection={"type": "top_n", "n": 2}, weighting="signal"), [3.0, 1.0, 2.0, NAN, 1.0])
        self.assertAlmostEqual(w["A"], 0.6)
        self.assertAlmostEqual(w["C"], 0.4)
        self.assertEqual(tw(spec(weighting="signal"), [0.0, 0.0, 0.0, 0.0, 0.0]), {})   # nothing to weight: cash

    def test_inverse_vol(self):
        rng = np.random.default_rng(0)
        r = rng.normal(0, 1, (4, 5)) * np.array([0.01, 0.02, 0.04, 0.01, 0.01])
        close = 100 * np.cumprod(1 + np.vstack([np.zeros(5), r]), axis=0)
        close[2, 3] = NAN                                   # D: incomplete window -> not eligible
        p = panel_with(close)
        vol = portfolio.realized_vol(p, 4, 3)
        self.assertTrue(math.isnan(vol[3]))
        w = tw(spec(weighting="inverse_vol"), [1.0] * 5, p)
        self.assertNotIn("D", w)
        inv = {s: 1 / vol[j] for j, s in enumerate(SYMS) if s != "D"}
        tot = sum(inv.values())
        for s, v in inv.items():
            self.assertAlmostEqual(w[s], v / tot)
        self.assertEqual(tw(spec(weighting="inverse_vol"), [1.0] * 5, p, t=1), {})   # too early for any vol

    def test_max_weight_not_redistributed(self):
        w = tw(spec(selection={"type": "top_n", "n": 2}, max_weight=0.3), [3.0, 1.0, 2.0, NAN, 5.0])
        self.assertEqual(w, {"A": 0.3, "E": 0.3})
        self.assertAlmostEqual(sum(w.values()), 0.6)       # 40 % stays in cash
        w = tw(spec(selection={"type": "top_n", "n": 2}, weighting="signal", max_weight=0.6),
               [3.0, NAN, NAN, NAN, 1.0])
        self.assertEqual(w, {"A": 0.6, "E": 0.25})

    def test_gross_exposure(self):
        w = tw(spec("long_short", {"type": "top_bottom_n", "n": 1}, gross_exposure=2.0), [1.0, 2.0, 3.0, NAN, NAN])
        self.assertEqual(w, {"A": -1.0, "C": 1.0})

    def test_deterministic_and_sorted(self):
        s = spec(selection={"type": "top_n", "n": 3})
        a = tw(s, [5.0, 4.0, 3.0, 2.0, 1.0])
        self.assertEqual(list(a), sorted(a))
        self.assertEqual(a, tw(s, [5.0, 4.0, 3.0, 2.0, 1.0]))


class RebalanceTest(unittest.TestCase):
    def test_frequencies(self):
        dates = [date(2024, 1, 30), date(2024, 1, 31), date(2024, 2, 1), date(2024, 2, 2), date(2024, 2, 5)]
        s = spec()
        s["rebalance"] = {"frequency": "monthly"}
        self.assertEqual(portfolio.rebalance_dates(s, dates), [date(2024, 1, 30), date(2024, 2, 1)])
        s["rebalance"] = {"frequency": "weekly"}
        self.assertEqual(portfolio.rebalance_dates(s, dates), [date(2024, 1, 30), date(2024, 2, 5)])
        s["rebalance"] = {"frequency": "daily"}
        self.assertEqual(portfolio.rebalance_dates(s, dates), dates)


class SdkTest(unittest.TestCase):
    def test_step_by_step_equals_vectorised(self):
        from strategy_platform.strategy.evaluate import evaluate
        p = synthetic_panel(90, nan_rate=0.03, seed=21)
        s = {"schema": "ufos.strategy/1", "name": "x", "universe": {"symbols": list(p.symbols)},
             "features": {"m": {"op": "pct_change", "args": [{"field": "close"}, 10]}},
             "signal": {"op": "cs_rank", "args": [{"op": "ts_mean", "args": [{"ref": "m"}, 5]}]},
             "portfolio": {"mode": "long_short", "selection": {"type": "top_bottom_n", "n": 2},
                           "weighting": "inverse_vol", "vol_lookback": 10, "max_weight": 0.3},
             "rebalance": {"frequency": "weekly"}}
        stepped = sdk.run(sdk.SpecStrategy(s), p)
        vector = portfolio.weights_history(s, evaluate(s, p), p)
        self.assertEqual(len(stepped), len(vector))
        for (d1, w1), (d2, w2) in zip(stepped, vector):
            self.assertEqual(d1, d2)
            if w1 is None or w2 is None:
                self.assertIs(w1, w2)
            else:
                self.assertEqual(set(w1), set(w2))
                for k in w1:
                    self.assertAlmostEqual(w1[k], w2[k], places=12)
        self.assertTrue(any(w for _d, w in vector))

    def test_context_is_point_in_time(self):
        p = synthetic_panel(20)
        ctx = sdk.Context(p, 9)
        self.assertEqual(len(ctx.panel), 10)
        self.assertEqual(ctx.history("close").shape[0], 10)
        self.assertEqual(ctx.history("close", 3).shape[0], 3)
        self.assertEqual(ctx.date, p.dates[9])
        with self.assertRaises(sdk.LookAheadError):
            ctx.value("close", p.symbols[0], 10)
        self.assertEqual(ctx.value("close", p.symbols[1], 9), ctx.value("close", p.symbols[1]))

    def test_custom_strategy(self):
        class Hold(sdk.Strategy):
            def on_bar(self, ctx):
                return {ctx.symbols[0]: 1.0} if ctx.t % 2 == 0 else None
        out = sdk.run(Hold(), synthetic_panel(6))
        self.assertEqual([w is None for _d, w in out], [False, True, False, True, False, True])


if __name__ == "__main__":
    unittest.main()
