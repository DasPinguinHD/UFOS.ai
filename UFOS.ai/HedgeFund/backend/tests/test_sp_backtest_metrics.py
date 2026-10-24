"""Stage 3d: backtest metrics (SPEC §8.4 / §8.10) — formulas on hand-computed
series, rank IC on a planted signal vs noise, IC half-life, time-series mode,
capacity, and the engine's analysis hook. Offline (fakes / synthetic data).
Part of UFOS.ai (proof of concept)."""
import json
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402

from strategy_platform.backtest import engine, metrics as M  # noqa: E402
import test_sp_backtest_support as sup  # noqa: E402

DATES = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"]


def result(equity, *, bench=None, long_mv=None, short_mv=None, fills=None, ledger=None, capital=100.0,
           calendar="nyse", dates=None):
    n = len(equity)
    return {"range": {"calendar": calendar}, "dates": (dates or DATES)[:n], "equity": equity,
            "benchmark": bench or [None] * n, "long_mv": long_mv or [0.0] * n, "short_mv": short_mv or [0.0] * n,
            "drawdown_pct": [0.0] * n, "fills": fills or [], "ledger": ledger or [], "orders": [],
            "broker_params": {"fees_bps": 5.0, "crypto_fees_bps": 20.0}, "request": {"capital": capital},
            "benchmark_info": {"symbol": "SPY"}, "stats": {}}


class PerformanceFormulaTest(unittest.TestCase):
    def test_hand_computed_series(self):
        eq = [100.0, 110.0, 99.0, 105.0, 121.0]
        r = [0.1, -0.1, 6 / 99, 16 / 105]
        out = M.compute(result(eq), include_signal=False)
        p = out["performance"]
        self.assertAlmostEqual(p["total_return"], 0.21)
        self.assertAlmostEqual(p["cagr"], 1.21 ** (252 / 4) - 1)
        m = sum(r) / 4
        sd = math.sqrt(sum((x - m) ** 2 for x in r) / 3)
        self.assertAlmostEqual(p["volatility"], sd * math.sqrt(252))
        self.assertAlmostEqual(p["sharpe"], m / sd * math.sqrt(252))
        self.assertAlmostEqual(p["sortino"], m / math.sqrt(0.01 / 4) * math.sqrt(252))
        self.assertAlmostEqual(p["max_drawdown"], 0.1)
        self.assertEqual((p["max_drawdown_peak"], p["max_drawdown_trough"], p["max_drawdown_recovery"]),
                         ("2024-01-03", "2024-01-04", "2024-01-08"))
        self.assertEqual(p["max_drawdown_duration_days"], 3)
        self.assertAlmostEqual(p["calmar"], p["cagr"] / 0.1)
        self.assertAlmostEqual(p["hit_rate_daily"], 0.75)
        self.assertAlmostEqual(p["profit_factor_daily"], 32 / 11)
        self.assertAlmostEqual(p["best_day"], max(r))
        self.assertAlmostEqual(p["worst_day"], min(r))
        self.assertEqual(set(out["unavailable"]), {"benchmark.beta", "benchmark.alpha_annual",
                                                   "benchmark.correlation", "performance.hit_rate_trades",
                                                   "performance.profit_factor_trades", "performance.avg_trade_pnl"})
        self.assertIn("both accounts valued", out["unavailable"]["benchmark.beta"])

    def test_crypto_calendar_annualises_with_365(self):
        p = M.compute(result([100.0, 110.0, 99.0, 105.0, 121.0], calendar="utc"), include_signal=False)
        self.assertAlmostEqual(p["performance"]["cagr"], 1.21 ** (365 / 4) - 1)

    def test_not_computable_is_null_with_reason(self):
        out = M.compute(result([100.0, 100.0, 100.0]), include_signal=False)
        p = out["performance"]
        self.assertIsNone(p["sharpe"])
        self.assertIsNone(p["sortino"])
        self.assertIsNone(p["calmar"])
        self.assertIsNone(p["profit_factor_daily"])
        self.assertIsNone(p["hit_rate_daily"])
        self.assertEqual((p["trades_closed"], p["trades_open"]), (0, 0))
        self.assertIn("no closed trade", out["unavailable"]["performance.hit_rate_trades"])
        self.assertEqual(p["volatility"], 0.0)
        self.assertIn("zero variance", out["unavailable"]["performance.sharpe"])
        self.assertIn("no drawdown", out["unavailable"]["performance.calmar"])
        one = M.compute(result([100.0]), include_signal=False)
        self.assertIsNone(one["performance"]["cagr"])
        self.assertIn("fewer than two", one["unavailable"]["performance.cagr"])

    def test_valuation_gap_spans_the_paused_day(self):
        p = M.compute(result([100.0, None, 121.0]), include_signal=False)["performance"]
        self.assertEqual((p["returns"], p["valuation_gaps"]), (1, 1))
        self.assertAlmostEqual(p["cagr"], 1.21 ** (252 / 2) - 1)

    def test_underwater_until_the_end(self):
        p = M.compute(result([100.0, 90.0, 95.0]), include_signal=False)["performance"]
        self.assertEqual(p["max_drawdown_duration_days"], 2)
        self.assertFalse(p["longest_underwater"]["recovered"])
        self.assertIsNone(p["max_drawdown_recovery"])


def fill(side, qty, px, fee, sym="AAA", ts="t"):
    return {"symbol": sym, "side": side, "qty": qty, "fill_price": px, "fees": fee, "notional": qty * px,
            "origin": "autopilot", "ts": ts}


class TradeMatchingTest(unittest.TestCase):
    def test_fifo_round_trips_hand_computed(self):
        fills = [fill("buy", 10, 100.0, 1.0), fill("buy", 10, 110.0, 1.0),      # two lots (scale-in)
                 fill("sell", 15, 120.0, 1.5),                                    # closes lot 1, half of lot 2
                 fill("sell", 5, 90.0, 0.5),                                      # closes lot 2
                 fill("short", 4, 95.0, 0.4), fill("cover", 4, 85.0, 0.4),       # a short round trip
                 fill("buy", 3, 50.0, 0.3, sym="BBB")]                            # open at the end
        m = M.match_trades(fills)
        pnl = [round(t["pnl"], 9) for t in m["closed"]]
        # lot 1: 10 x 20 - 1 - 1.5 x 10/15 = 198; lot 2: 5 x 10 + 5 x (-20) - 1 - 0.5 - 0.5 = -52
        # short: 4 x 10 - 0.4 - 0.4 = 39.2
        self.assertEqual(pnl, [198.0, -52.0, 39.2])
        self.assertEqual([t["direction"] for t in m["closed"]], [1.0, 1.0, -1.0])
        self.assertEqual(len(m["open"]), 1)
        out = M.compute(result([100.0, 101.0], fills=fills), include_signal=False)["performance"]
        self.assertEqual((out["trades_closed"], out["trades_open"]), (3, 1))
        self.assertAlmostEqual(out["hit_rate_trades"], 2 / 3)
        self.assertAlmostEqual(out["profit_factor_trades"], (198 + 39.2) / 52)
        self.assertAlmostEqual(out["avg_trade_pnl"], (198 - 52 + 39.2) / 3)

    def test_flip_and_partial_open_lot(self):
        fills = [fill("buy", 5, 10.0, 0.0), fill("sell", 8, 12.0, 0.8),         # closes 5 long, opens 3 short
                 fill("cover", 1, 11.0, 0.0)]                                     # short lot still 2 open
        m = M.match_trades(fills)
        self.assertEqual(len(m["closed"]), 1)
        self.assertAlmostEqual(m["closed"][0]["pnl"], 5 * 2 - 0.8 * 5 / 8)
        self.assertEqual(len(m["open"]), 1)
        lot = m["open"][0]
        self.assertEqual((lot["direction"], lot["qty"], lot["remaining"]), (-1.0, 3, 2))
        p = M.compute(result([100.0, 101.0], fills=fills), include_signal=False)["performance"]
        self.assertEqual((p["trades_closed"], p["trades_open"]), (1, 1))
        self.assertEqual(p["hit_rate_trades"], 1.0)
        self.assertIsNone(p["profit_factor_trades"])

    def test_unknown_side_is_an_error(self):
        with self.assertRaises(ValueError):
            M.match_trades([fill("hold", 1, 1.0, 0.0)])


class BenchmarkExposureCostTest(unittest.TestCase):
    def test_alpha_beta(self):
        rb = [0.01, -0.02, 0.015, 0.0]
        bench = [100.0]
        eq = [100.0]
        for x in rb:
            bench.append(bench[-1] * (1 + x))
            eq.append(eq[-1] * (1 + 2 * x + 0.001))
        b = M.compute(result(eq, bench=bench), include_signal=False)["benchmark"]
        self.assertAlmostEqual(b["beta"], 2.0)
        self.assertAlmostEqual(b["alpha_annual"], 0.001 * 252)
        self.assertAlmostEqual(b["correlation"], 1.0)
        self.assertAlmostEqual(b["total_return"], bench[-1] / 100 - 1)

    def test_exposure_and_turnover(self):
        eq = [100.0, 100.0, 100.0]
        out = M.compute(result(eq, long_mv=[50.0, 80.0, 80.0], short_mv=[0.0, 20.0, 40.0],
                               fills=[{"symbol": "AAA", "side": "buy", "qty": 1, "fill_price": 100.0,
                                       "notional": 100.0, "origin": "autopilot", "ts": "x"}]),
                        include_signal=False)
        e = out["exposure"]
        self.assertAlmostEqual(e["avg_gross_exposure"], (0.5 + 1.0 + 1.2) / 3)
        self.assertAlmostEqual(e["avg_net_exposure"], (0.5 + 0.6 + 0.4) / 3)
        self.assertAlmostEqual(e["turnover_annual"], 100.0 / 2 / 100.0 / (2 / 252))

    def test_costs_breakdown(self):
        h = 2.5e-4                                   # fees_bps 5 -> half
        fills = [{"symbol": "AAA", "side": "buy", "qty": 10, "fill_price": 100 * (1 + h), "notional": 1000.25,
                  "origin": "autopilot", "ts": "x"},
                 {"symbol": "AAA", "side": "sell", "qty": 10, "fill_price": 110 * (1 - h), "notional": 1099.725,
                  "origin": "autopilot", "ts": "y"},
                 {"symbol": "BTC-USD", "side": "buy", "qty": 1, "fill_price": 50 * (1 + 1e-3), "notional": 50.05,
                  "origin": "autopilot", "ts": "z"}]
        ledger = [{"kind": "fee", "amount": -0.5}, {"kind": "fee", "amount": -0.25},
                  {"kind": "borrow_fee", "amount": -1.0}, {"kind": "funding", "amount": -0.5},
                  {"kind": "funding", "amount": 0.2}, {"kind": "dividend", "amount": 3.0},
                  {"kind": "fill", "amount": -1000.0}, {"kind": "liquidation_fee", "amount": -0.1}]
        c = M.compute(result([100.0, 150.0], fills=fills, ledger=ledger), include_signal=False)["costs"]
        self.assertAlmostEqual(c["slippage"], 10 * 100 * h + 10 * 110 * h + 50 * 1e-3)
        self.assertAlmostEqual(c["fees"], 0.75)
        self.assertAlmostEqual(c["borrow"], 1.0)
        self.assertAlmostEqual(c["funding"], 0.3)
        self.assertAlmostEqual(c["liquidation_fees"], 0.1)
        self.assertAlmostEqual(c["dividends_received"], 3.0)
        total = c["slippage"] + 0.75 + 1.0 + 0.3 + 0.1
        self.assertAlmostEqual(c["total"], total)
        self.assertAlmostEqual(c["net_pnl"], 50.0)
        self.assertAlmostEqual(c["gross_pnl"], 50.0 + total)
        self.assertAlmostEqual(c["pct_of_gross_pnl"], 100 * total / (50.0 + total))
        loss = M.compute(result([100.0, 50.0], ledger=ledger), include_signal=False)
        self.assertIsNone(loss["costs"]["pct_of_gross_pnl"])
        self.assertIn("not positive", loss["unavailable"]["costs.pct_of_gross_pnl"])


def planted_panel(T=320, N=20, strength=1.0, seed=7, persistent=False):
    """Signal S (random), close panel whose next-day returns load on S[t] with ``strength``."""
    rng = np.random.default_rng(seed)
    S = rng.standard_normal((T, N))
    if persistent:                                 # a slowly changing signal
        for t in range(1, T):
            S[t] = 0.9 * S[t - 1] + math.sqrt(1 - 0.81) * rng.standard_normal(N)
    rets = np.zeros((T, N))
    rets[1:] = 0.01 * (strength * S[:-1] + rng.standard_normal((T - 1, N)))
    C = 100 * np.cumprod(1 + rets, axis=0)
    return S, C


def analysis(S, C, i0=0, calendar="nyse", symbols=None):
    T, N = S.shape
    return {"dates": [f"d{t}" for t in range(T)], "symbols": symbols or [f"S{j}" for j in range(N)], "signal": S,
            "close_all": C, "i0": i0, "i1": T - 1, "calendar": calendar, "dollar_volume": {}, "crypto": []}


class SignalQualityTest(unittest.TestCase):
    def test_rank_and_spearman(self):
        self.assertEqual(list(M.rank([3.0, 1.0, 3.0, 2.0])), [3.5, 1.0, 3.5, 2.0])
        self.assertAlmostEqual(M.spearman([1, 2, 3, 4, 5], [10, 20, 30, 40, 50], 5), 1.0)
        self.assertAlmostEqual(M.spearman([1, 2, 3, 4, 5], [5, 4, 3, 2, 1], 5), -1.0)
        self.assertIsNone(M.spearman([1, 1, 1, 1, 1], [1, 2, 3, 4, 5], 5))
        self.assertIsNone(M.spearman([1, 2, float("nan"), 4, 5], [1, 2, 3, 4, 5], 5))

    def test_planted_signal_positive_noise_zero(self):
        S, C = planted_panel()
        q = M.signal_quality(None, analysis(S, C))
        self.assertEqual(q["mode"], "cross_sectional")
        h1 = q["horizons"][0]
        self.assertEqual(h1["horizon"], 1)
        self.assertGreater(h1["mean_ic"], 0.4)
        self.assertGreater(h1["icir"], 1.0)
        self.assertEqual([h["horizon"] for h in q["horizons"]], [1, 5, 10, 21, 63])
        noise = np.random.default_rng(99).standard_normal(S.shape)
        qn = M.signal_quality(None, analysis(noise, C))
        self.assertLess(abs(qn["horizons"][0]["mean_ic"]), 0.05)

    def test_decay_curve_and_half_life_of_a_persistent_signal(self):
        S, C = planted_panel(T=400, persistent=True)
        q = M.signal_quality(None, analysis(S, C))
        lag_ic = {d["lag"]: d["mean_ic"] for d in q["decay"]}
        self.assertGreater(lag_ic[1], lag_ic[5])
        self.assertGreater(lag_ic[5], lag_ic[10])
        # signal AR(1) 0.9 -> IC at lag h ~ IC1 * 0.9 ** (h - 1) -> half-life ~ ln 2 / -ln 0.9 = 6.6 days
        self.assertIsNotNone(q["half_life_days"])
        self.assertTrue(3.0 < q["half_life_days"] < 15.0, q["half_life_days"])

    def test_half_life_fit(self):
        lags = [1, 5, 10, 21, 63]
        ics = [0.1 * math.exp(-math.log(2) * h / 10.0) for h in lags]
        hl, why, fit = M.half_life(lags, ics)
        self.assertAlmostEqual(hl, 10.0)
        self.assertIsNone(why)
        self.assertAlmostEqual(fit["amplitude"], 0.1)
        neg = [-x for x in ics]
        self.assertAlmostEqual(M.half_life(lags, neg)[0], 10.0)
        hl, why, _ = M.half_life(lags, [0.05, 0.06, 0.07, 0.08, 0.09])
        self.assertIsNone(hl)
        self.assertIn("does not decay", why)
        hl, why, _ = M.half_life(lags, [0.1, -0.05, -0.02, 0.01, None])
        self.assertIsNone(hl)
        self.assertIn("fewer than 3", why)

    def test_too_few_days_is_null(self):
        S, C = planted_panel(T=30)
        q = M.signal_quality(None, analysis(S, C))
        h63 = q["horizons"][-1]
        self.assertIsNone(h63["mean_ic"])
        self.assertIn("needs 20", h63["reason"])

    def test_time_series_mode_rolling_positive_share(self):
        S, C = planted_panel(T=400, N=2, strength=2.0)
        q = M.signal_quality(None, analysis(S, C))
        self.assertEqual(q["mode"], "time_series")
        self.assertEqual(q["window_days"], 65)
        self.assertGreater(q["positive_share"], 0.9)
        self.assertEqual(len(q["per_symbol"]), 2)
        noise = np.random.default_rng(5).standard_normal(S.shape)
        qn = M.signal_quality(None, analysis(noise, C))
        self.assertTrue(0.2 < qn["positive_share"] < 0.8)
        const = np.ones_like(S)
        qc = M.signal_quality(None, analysis(const, C))
        self.assertIsNone(qc["positive_share"])
        self.assertIn("no 13-week window", qc["reason"])

    def test_without_analysis(self):
        q = M.signal_quality({"dates": []})
        self.assertIsNone(q["mode"])
        self.assertIn("not available", q["reason"])


class CapacityTest(unittest.TestCase):
    def setUp(self):
        self.dates = [f"2024-01-{d:02d}" for d in range(1, 31)]

    def res(self, notionals, capital=100000.0, cut=()):
        fills = [{"symbol": "AAA", "side": "buy", "qty": 1, "fill_price": n, "notional": n, "origin": "autopilot",
                  "ts": f"2024-01-{25 + (k % 5):02d}T15:00:00+00:00", "participation_cut": k in cut}
                 for k, n in enumerate(notionals)]
        r = result([capital] * len(self.dates), dates=self.dates, fills=fills, capital=capital)
        an = {"dates": self.dates, "symbols": ["AAA"], "signal": None, "close_all": None, "i0": 0,
              "i1": len(self.dates) - 1, "calendar": "nyse", "crypto": [],
              "dollar_volume": {"AAA": {d: 1_000_000.0 for d in self.dates}}}
        return r, an

    def test_capacity_scales_with_the_q90_ratio(self):
        notionals = [1000.0 * (k + 1) for k in range(10)]           # ratios 0.1 .. 1.0 of the 10k cap
        notionals[-1] = 9000.0
        r, an = self.res(notionals)
        c = M.capacity(r, an)
        ratios = np.array(notionals) / 10000.0
        q90 = float(np.quantile(ratios, 0.9))
        self.assertEqual(c["orders"], 10)
        self.assertAlmostEqual(c["q90_ratio"], q90)
        self.assertAlmostEqual(c["capacity_usd"], 100000.0 / q90)

    def test_already_binding_and_too_few_orders(self):
        r, an = self.res([10000.0] * 3 + [100.0] * 7)
        c = M.capacity(r, an)
        self.assertIsNone(c["capacity_usd"])
        self.assertEqual(c["at_most"], 100000.0)
        r, an = self.res([100.0] * 3)
        c = M.capacity(r, an)
        self.assertIsNone(c["capacity_usd"])
        self.assertIn("needs 10", c["reason"])
        an["crypto"] = ["AAA"]
        self.assertIn("crypto has no participation limit", M.capacity(r, an)["reason"])

    def test_engine_cut_flag_counts_even_below_the_ratio(self):
        # whole-share rounding leaves a cut order's ratio below 1 (e.g. 0.97): the flag decides
        r, an = self.res([9700.0] * 2 + [100.0] * 8, cut={0, 1})
        c = M.capacity(r, an)
        self.assertEqual(c["orders_at_limit"], 2)
        self.assertIsNone(c["capacity_usd"])
        self.assertEqual(c["at_most"], 100000.0)
        r, an = self.res([9700.0] * 2 + [100.0] * 8)               # not flagged -> an estimate
        self.assertIsNotNone(M.capacity(r, an)["capacity_usd"])
        r["request"]["capital"] = None
        self.assertIn("no tested capital", M.capacity(r, an)["reason"])
        r["request"]["capital"] = 100000.0
        del r["fills"][0]["participation_cut"]
        with self.assertRaises(ValueError):
            M.capacity(r, an)


class EngineHookTest(unittest.TestCase):
    def test_engine_result_metrics(self):
        syms = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
        prices = {s: sup.series(50 + 10 * k, drift=0.0005 * (k - 2), amp=0.01 + 0.002 * k)
                  for k, s in enumerate(syms)}
        prices["SPY"] = sup.series(400)
        spec = sup.spec(syms, mode="long_short", selection={"type": "top_bottom_n", "n": 2},
                        signal={"op": "returns", "args": [3]})
        res = engine.run(spec, data=sup.FakeLoader(prices))
        self.assertNotIn("analysis", res.to_dict())
        self.assertEqual(res.analysis["symbols"], syms)
        out = M.compute(res)
        json.dumps(out, allow_nan=False)
        self.assertEqual(out["signal_quality"]["mode"], "cross_sectional")
        self.assertIsNotNone(out["performance"]["cagr"])
        self.assertIn("capacity", out)
        self.assertEqual(set(M.summary(out)), set(M.SUMMARY_KEYS))


if __name__ == "__main__":
    unittest.main()
