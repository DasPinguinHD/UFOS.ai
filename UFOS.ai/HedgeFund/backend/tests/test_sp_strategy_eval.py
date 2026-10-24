"""strategy/evaluate.py + panel.py: the causality property for EVERY
operator (evaluate on panel[:t+1] == full-panel row t, random t, synthetic
data with NaNs), operator semantics, external-feature alignment, and
Panel.from_bars calendar alignment (NYSE, UTC days, mixed via known_at).
Offline.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_strategy_eval -v
"""
import math
import random
import unittest
from datetime import date

import numpy as np

try:
    from test_sp_strategy_support import fake_ext_values, synthetic_panel
except ImportError:  # pragma: no cover
    from tests.test_sp_strategy_support import fake_ext_values, synthetic_panel

from strategy_platform.strategy import dsl
from strategy_platform.strategy.evaluate import EvaluationError, align_ext, evaluate, evaluate_node
from strategy_platform.strategy.panel import Panel


def op(name, *args):
    return {"op": name, "args": list(args)}


C, O, H, L, V = ({"field": f} for f in ("close", "open", "high", "low", "volume"))


def sample_node(name):
    """An instance of every operator with valid sample arguments."""
    o = dsl.OPS[name]
    args = []
    xs = [op("sub", C, O), V, op("returns", 1)]
    xi = 0
    for i in range(o.min_args):
        kind = o.kind_at(i)
        if kind == "x":
            if name in ("log", "sqrt"):
                args.append(C)
            elif name == "where":
                args.append([op("gt", C, O), C, O][i])
            elif name in ("and", "or", "not"):
                args.append(op("gt", C, [O, H][i % 2]) if name != "not" else op("gt", C, O))
            else:
                args.append(xs[xi % len(xs)])
                xi += 1
        elif kind == "window":
            args.append(max(5, o.min_window))
        elif kind == "halflife":
            args.append(3)
        else:
            args.append(1.5 if name == "cs_winsorize" else 2)
    return op(name, *args)


def assert_rows_equal(tc, a, b, msg=""):
    np.testing.assert_allclose(a, b, rtol=1e-9, atol=1e-9, equal_nan=True, err_msg=msg)


class CausalityTest(unittest.TestCase):
    def test_every_operator(self):
        panel = synthetic_panel(160, nan_rate=0.04, seed=11)
        rng = random.Random(5)
        for name in sorted(dsl.OPS):
            node = sample_node(name)
            full = evaluate_node(node, panel)
            self.assertEqual(full.shape, panel.shape, name)
            ts = sorted({rng.randrange(0, len(panel)) for _ in range(5)} | {0, len(panel) - 1})
            for t in ts:
                part = evaluate_node(node, panel.head(t + 1))
                assert_rows_equal(self, part[t], full[t], f"{name} at t={t}")
            if name != "nan":
                self.assertTrue(np.isfinite(full).any(), f"{name} produced no values")

    def test_ext_and_nested(self):
        panel = synthetic_panel(120, nan_rate=0.03, seed=2)
        ext = fake_ext_values(panel)
        node = op("mul", op("ts_zscore", op("add", C, {"ext": "macro.regime_score"}), 10),
                  op("sub", 1, {"ext": "calendar.event_risk"}))
        full = evaluate_node(node, panel, ext_values=ext)
        for t in (0, 15, 64, 119):
            part = evaluate_node(node, panel.head(t + 1), ext_values=ext)
            assert_rows_equal(self, part[t], full[t])

    def test_future_change_does_not_move_the_past(self):
        panel = synthetic_panel(100, nan_rate=0.0, seed=4)
        node = op("cs_rank", op("ewm_mean", op("rsi", 3), 4))
        before = evaluate_node(node, panel)
        panel.fields["close"][60:] *= 3.0
        after = evaluate_node(node, panel)
        assert_rows_equal(self, before[:60], after[:60])


def tiny_panel(close, **fields):
    close = np.asarray(close, dtype=float)
    if close.ndim == 1:
        close = close[:, None]
    T, S = close.shape
    dates = [date(2024, 1, 1 + i) for i in range(T)] if T <= 31 else synthetic_panel(T).dates
    f = {"close": close}
    for k, v in fields.items():
        a = np.asarray(v, dtype=float)
        f[k] = a[:, None] if a.ndim == 1 else a
    return Panel(dates, [f"S{j}" for j in range(S)], f)


def ev(node, panel):
    return evaluate_node(node, panel)


class SemanticsTest(unittest.TestCase):
    def test_elementwise(self):
        p = tiny_panel([4.0, 0.0, -1.0, np.nan])
        np.testing.assert_array_equal(ev(op("div", 1, C), p)[:, 0], [0.25, np.nan, -1.0, np.nan])
        np.testing.assert_array_equal(ev(op("log", C), p)[:, 0], [math.log(4), np.nan, np.nan, np.nan])
        np.testing.assert_array_equal(ev(op("sqrt", C), p)[:, 0], [2.0, 0.0, np.nan, np.nan])
        np.testing.assert_array_equal(ev(op("gt", C, 0), p)[:, 0], [1, 0, 0, np.nan])
        np.testing.assert_array_equal(ev(op("is_nan", C), p)[:, 0], [0, 0, 0, 1])
        np.testing.assert_array_equal(ev(op("fill_nan", C, 7), p)[:, 0], [4, 0, -1, 7])
        np.testing.assert_array_equal(ev(op("where", op("gt", C, 0), 1, -1), p)[:, 0], [1, -1, -1, np.nan])
        np.testing.assert_array_equal(ev(op("signed_power", C, 2), p)[:, 0], [16, 0, -1, np.nan])
        np.testing.assert_array_equal(ev(op("pow", C, -1), p)[:, 0], [0.25, np.nan, -1, np.nan])
        np.testing.assert_array_equal(ev(op("and", op("gt", C, -2), op("lt", C, 1)), p)[:, 0], [0, 1, 1, np.nan])
        np.testing.assert_array_equal(ev(op("not", C), p)[:, 0], [0, 1, 0, np.nan])
        np.testing.assert_array_equal(ev(op("clip", C, 0, 1), p)[:, 0], [1, 0, 0, np.nan])
        self.assertTrue(np.isnan(ev(op("exp", 1000), p)).all())

    def test_time_series(self):
        p = tiny_panel([1.0, 2.0, 3.0, 4.0, 5.0, np.nan, 7.0, 8.0])
        c = lambda node: ev(node, p)[:, 0]  # noqa: E731
        np.testing.assert_array_equal(c(op("lag", C, 2)), [np.nan, np.nan, 1, 2, 3, 4, 5, np.nan])
        np.testing.assert_array_equal(c(op("ts_mean", C, 3)), [np.nan, np.nan, 2, 3, 4, np.nan, np.nan, np.nan])
        np.testing.assert_array_equal(c(op("ts_sum", C, 2)), [np.nan, 3, 5, 7, 9, np.nan, np.nan, 15])
        np.testing.assert_array_equal(c(op("delta", C, 1)), [np.nan, 1, 1, 1, 1, np.nan, np.nan, 1])
        np.testing.assert_allclose(c(op("pct_change", C, 1))[1:5], [1.0, 0.5, 1 / 3, 0.25])
        np.testing.assert_array_equal(c(op("ts_argmax", C, 3))[2:5], [0, 0, 0])
        np.testing.assert_array_equal(c(op("ts_argmin", C, 3))[2:5], [2, 2, 2])
        np.testing.assert_array_equal(c(op("ts_rank", C, 3))[2:5], [1, 1, 1])
        np.testing.assert_allclose(c(op("decay_linear", C, 3))[2], (1 + 4 + 9) / 6)
        np.testing.assert_allclose(c(op("ts_std", C, 3))[2], 1.0)
        np.testing.assert_allclose(c(op("ts_product", C, 3))[3], 24.0)
        e = c(op("ewm_mean", C, 1))
        self.assertEqual(e[0], 1.0)
        self.assertAlmostEqual(e[1], 1.5)
        self.assertTrue(np.isnan(e[5]))
        self.assertEqual(e[6], 7.0)                          # restarts after a missing value

    def test_ts_rank_ties_and_argmax_ties(self):
        p = tiny_panel([2.0, 1.0, 2.0])
        self.assertEqual(ev(op("ts_rank", C, 3), p)[2, 0], 0.75)    # (avg rank 2.5 - 1) / 2
        self.assertEqual(ev(op("ts_argmax", C, 3), p)[2, 0], 0.0)   # most recent of the tied maxima

    def test_rsi_matches_fund_indicator(self):
        from fund import indicators as ind
        rng = np.random.default_rng(1)
        closes = 100 + np.cumsum(rng.normal(0, 1, 80))
        p = tiny_panel(closes)
        r = ev(op("rsi", 14), p)[:, 0]
        self.assertTrue(np.isnan(r[13]))
        for t in (14, 30, 79):
            self.assertAlmostEqual(r[t], ind.rsi(list(closes[:t + 1]), 14), places=9)

    def test_atr_matches_fund_indicator(self):
        from types import SimpleNamespace
        from fund import indicators as ind
        rng = np.random.default_rng(2)
        c = 100 + np.cumsum(rng.normal(0, 1, 60))
        h = c + rng.uniform(0.1, 2, 60)
        lo = c - rng.uniform(0.1, 2, 60)
        p = tiny_panel(c, high=h, low=lo)
        a = ev(op("atr", 14), p)[:, 0]
        bars = [SimpleNamespace(high=h[i], low=lo[i], close=c[i]) for i in range(60)]
        self.assertTrue(np.isnan(a[13]))
        for t in (14, 40, 59):
            self.assertAlmostEqual(a[t], ind.atr(bars[:t + 1], 14), places=9)

    def test_cross_sectional(self):
        p = tiny_panel([[3.0, 1.0, 2.0, 2.0, np.nan]])
        np.testing.assert_allclose(ev(op("cs_rank", C), p)[0], [1.0, 0.0, 0.5, 0.5, np.nan])
        z = ev(op("cs_zscore", C), p)[0]
        self.assertAlmostEqual(float(np.nansum(z)), 0.0)
        np.testing.assert_allclose(ev(op("cs_demean", C), p)[0], [1, -1, 0, 0, np.nan])
        np.testing.assert_allclose(ev(op("cs_scale", C), p)[0], [3 / 8, 1 / 8, 2 / 8, 2 / 8, np.nan])
        w = ev(op("cs_winsorize", C, 0.5), p)[0]
        self.assertLess(w[0], 3.0)
        single = tiny_panel([[np.nan, 5.0]])
        np.testing.assert_allclose(ev(op("cs_rank", C), single)[0], [np.nan, 0.5])

    def test_params_and_overrides(self):
        p = tiny_panel([1.0, 2.0, 4.0])
        spec = {"parameters": {"n": {"value": 1, "min": 1, "max": 2, "integer": True}},
                "features": {"x": op("lag", C, {"param": "n"})}, "signal": {"ref": "x"}}
        np.testing.assert_array_equal(evaluate(spec, p)[:, 0], [np.nan, 1, 2])
        np.testing.assert_array_equal(evaluate(spec, p, param_values={"n": 2})[:, 0], [np.nan, np.nan, 1])
        with self.assertRaises(EvaluationError):
            evaluate(spec, p, param_values={"m": 2})

    def test_does_not_mutate_panel(self):
        p = synthetic_panel(60, seed=9)
        before = {k: v.copy() for k, v in p.fields.items()}
        for name in dsl.OPS:
            ev(sample_node(name), p)
        for k in before:
            np.testing.assert_array_equal(before[k], p.fields[k])


class ChunkingAndBudgetTest(unittest.TestCase):
    def test_chunked_equals_single_block(self):
        from unittest import mock
        from strategy_platform.strategy import evaluate as evm
        p = synthetic_panel(70, nan_rate=0.03, seed=13)
        names = [n for n, o in dsl.OPS.items() if o.lookback == "window"]
        ref = {n: ev(sample_node(n), p) for n in names}
        for chunk in (1, 7, 50):
            with mock.patch.object(evm, "ROLLING_CHUNK_ELEMENTS", chunk):
                for n in names:
                    np.testing.assert_allclose(ev(sample_node(n), p), ref[n], rtol=1e-12, atol=1e-12,
                                               equal_nan=True, err_msg=f"{n} chunk={chunk}")

    def test_chunks_are_bounded(self):
        from strategy_platform.strategy import evaluate as evm
        from strategy_platform.strategy.limits import ROLLING_CHUNK_ELEMENTS
        for T, S, n in ((3000, 100, 1500), (3000, 20, 2520), (2600, 500, 5), (10, 3, 10)):
            cover = np.zeros((T - n + 1, S), dtype=int)
            for r0, r1, c0, c1 in evm._chunks(T, S, n):
                self.assertLessEqual((r1 - r0) * (c1 - c0) * n, max(ROLLING_CHUNK_ELEMENTS, n))
                cover[r0:r1, c0:c1] += 1
            self.assertTrue((cover == 1).all())

    def test_long_window_large_panel_runs(self):
        p = synthetic_panel(3000, symbols=[f"S{i}" for i in range(40)], nan_rate=0.0, seed=1, late_listing=False)
        r = ev(op("ts_rank", C, 1500), p)
        self.assertEqual(int(np.isfinite(r).sum()), (3000 - 1499) * 40)

    def test_work_budget(self):
        from strategy_platform.core.errors import UserFacingError
        from strategy_platform.strategy.evaluate import EvaluationTooExpensive, estimate_cost
        p = synthetic_panel(3000, symbols=[f"S{i}" for i in range(600)], nan_rate=0.0, seed=2)
        heavy = op("add", *[op("ts_mean", C, 2520 - i) for i in range(10)])
        self.assertGreater(estimate_cost({"signal": heavy}, p.shape), 2e10)
        with self.assertRaises(EvaluationTooExpensive) as cm:
            ev(heavy, p)
        self.assertIsInstance(cm.exception, UserFacingError)
        self.assertIn("too expensive to evaluate", cm.exception.user_message)
        # features count once (memoised refs)
        spec = {"features": {"m": op("ts_mean", C, 100)},
                "signal": op("add", {"ref": "m"}, {"ref": "m"}, {"ref": "m"})}
        self.assertEqual(estimate_cost(spec, (10, 1)), 10 * (1 + (1 + 100 + 1)))   # add + ts_mean(close, 100) once


class ExtAlignTest(unittest.TestCase):
    def test_asof_alignment(self):
        p = Panel([date(2024, 1, d) for d in (2, 3, 4, 5)], ["A", "B"], {"close": np.ones((4, 2))})
        g = align_ext({"2024-01-01": 1.0, "2024-01-04": 2.0}, p)
        np.testing.assert_array_equal(g[:, 0], [1, 1, 2, 2])
        np.testing.assert_array_equal(g[:, 1], [1, 1, 2, 2])
        s = align_ext({"A": {"2024-01-03": 5.0}, "B": {}}, p)
        np.testing.assert_array_equal(s[:, 0], [np.nan, 5, 5, 5])
        self.assertTrue(np.isnan(s[:, 1]).all())
        arr = align_ext(np.zeros((4, 2)), p)
        self.assertEqual(arr.shape, (4, 2))
        with self.assertRaises(EvaluationError):
            align_ext(np.zeros((3, 2)), p)

    def test_registry_is_used_when_not_injected(self):
        from strategy_platform.strategy import features
        calls = []

        def load(start, end):
            calls.append((start, end))
            return {"2024-01-01": 0.5}
        feat = features.ExtFeature(name="test.b1_fake", description="t", load=load)
        features.register(feat, replace=True)
        try:
            p = Panel([date(2024, 1, 2), date(2024, 1, 3)], ["A"], {"close": np.ones((2, 1))})
            out = evaluate_node({"ext": "test.b1_fake"}, p)
            np.testing.assert_array_equal(out[:, 0], [0.5, 0.5])
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][1], date(2024, 1, 3))
        finally:
            features.unregister("test.b1_fake")


def ebar(sym, d, close, ts_hour=5):   # winter: Alpaca daily bars start at NY midnight = 05:00 UTC
    return {"symbol": sym, "ts": f"{d}T{ts_hour:02d}:00:00Z", "open": close, "high": close, "low": close,
            "close": close, "volume": 1000, "vwap": close, "trade_count": 10}


def cbar(sym, ts, close):
    from strategy_platform.data import pit
    return {"symbol": sym, "ts": ts, "open": close, "high": close, "low": close, "close": close, "volume": 5,
            "vwap": None, "trade_count": None, "known_at": pit.known_at_iso("crypto", "1Day", ts)}


class FromBarsTest(unittest.TestCase):
    def test_equities_on_nyse_sessions(self):
        bars = {"SPY": [ebar("SPY", "2024-01-02", 1), ebar("SPY", "2024-01-03", 2), ebar("SPY", "2024-01-05", 4)],
                "TLT": [ebar("TLT", "2024-01-03", 9)]}
        p = Panel.from_bars(bars)
        self.assertEqual(p.calendar, "nyse")
        self.assertEqual(p.dates, [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5)])
        np.testing.assert_array_equal(p.fields["close"][:, 0], [1, 2, np.nan, 4])
        np.testing.assert_array_equal(p.fields["close"][:, 1], [np.nan, 9, np.nan, np.nan])
        self.assertEqual(int(np.isnan(p.fields["vwap"]).sum()), 4)

    def test_skips_holidays(self):
        bars = {"SPY": [ebar("SPY", "2024-12-24", 1), ebar("SPY", "2024-12-26", 2)]}
        p = Panel.from_bars(bars)
        self.assertEqual(p.dates, [date(2024, 12, 24), date(2024, 12, 26)])

    def test_crypto_only_on_utc_days(self):
        bars = {"BTC-USD": [cbar("BTC-USD", "2024-01-06T00:00:00Z", 1), cbar("BTC-USD", "2024-01-08T00:00:00Z", 3)]}
        p = Panel.from_bars(bars)
        self.assertEqual(p.calendar, "utc")
        self.assertEqual(p.dates, [date(2024, 1, 6), date(2024, 1, 7), date(2024, 1, 8)])
        np.testing.assert_array_equal(p.fields["close"][:, 0], [1, np.nan, 3])

    def test_mixed_uses_known_at(self):
        crypto = [cbar("BTC-USD", f"2024-01-0{d}T00:00:00Z", float(d)) for d in range(1, 6)]
        bars = {"SPY": [ebar("SPY", "2024-01-02", 10), ebar("SPY", "2024-01-03", 11), ebar("SPY", "2024-01-04", 12)],
                "BTC-USD": crypto}
        p = Panel.from_bars(bars)
        self.assertEqual(p.calendar, "nyse")
        self.assertEqual(p.dates, [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)])
        # session 2024-01-02 closes 21:00 UTC: the bar starting 01-02 00:00 is known only on 01-03 00:00,
        # so the value is the bar of 01-01 (known 01-02 00:00)
        np.testing.assert_array_equal(p.fields["close"][:, 1], [1, 2, 3])
        self.assertTrue(np.isnan(p.fields["vwap"][:, 1]).all())

    def test_mixed_gap_is_not_carried_forward(self):
        crypto = [cbar("BTC-USD", "2024-01-01T00:00:00Z", 1.0)]
        bars = {"SPY": [ebar("SPY", "2024-01-02", 10), ebar("SPY", "2024-01-05", 12)], "BTC-USD": crypto}
        p = Panel.from_bars(bars)
        col = p.fields["close"][:, p.symbols.index("BTC-USD")]
        self.assertEqual(col[0], 1.0)
        self.assertTrue(np.isnan(col[1:]).all())

    def test_duplicates_and_bad_calendar(self):
        with self.assertRaises(ValueError):
            Panel.from_bars({"SPY": [ebar("SPY", "2024-01-02", 1), ebar("SPY", "2024-01-02", 2, ts_hour=6)]})
        with self.assertRaises(ValueError):
            Panel.from_bars({"SPY": [ebar("SPY", "2024-01-02", 1)]}, calendar="utc")
        p = Panel.from_bars({"SPY": [], "TLT": []})
        self.assertEqual(len(p), 0)

    def test_prefix_slicing(self):
        p = synthetic_panel(30)
        self.assertEqual(len(p[:10]), 10)
        self.assertEqual(p[:10].dates, p.dates[:10])
        with self.assertRaises(TypeError):
            p[5:10]


if __name__ == "__main__":
    unittest.main()
