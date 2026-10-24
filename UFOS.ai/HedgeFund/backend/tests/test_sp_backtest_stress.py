"""Stage 3d: stress tests (SPEC §8.4 / §8.10) — historical episodes incl. the
"not available" paths (no Tiingo key, no data, partial coverage), synthetic
costs via the custom-cost path, Monte-Carlo determinism. Fake data, offline.
Part of UFOS.ai (proof of concept)."""
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strategy_platform.backtest import engine, metrics, stress  # noqa: E402
from strategy_platform.core import calendars  # noqa: E402
from strategy_platform.core.errors import DataSourceUnavailable, InvalidRequest, SecretMissing  # noqa: E402
from strategy_platform.data import seam  # noqa: E402
import test_sp_backtest_support as sup  # noqa: E402

LONG_DAYS = calendars.trading_days(date(2018, 1, 2), date(2022, 12, 30))
SYMS = ["AAA", "BBB"]


class KeylessLoader(sup.FakeLoader):
    """FakeLoader whose equity history before the seam needs the (missing) Tiingo key, like seam.history."""

    def _need_key(self, start):
        if start < seam.SEAM_DATE:
            raise SecretMissing("tiingo-api-key", "Tiingo API key")

    def history(self, symbol, adjustment, start, end=None, *, progress=None):
        self._need_key(start)
        return super().history(symbol, adjustment, start, end, progress=progress)

    def corporate_actions(self, symbol, start, end=None, *, progress=None):
        self._need_key(start)
        return super().corporate_actions(symbol, start, end, progress=progress)

    def volume(self, symbol, start, end=None, *, progress=None):
        self._need_key(start)
        return super().volume(symbol, start, end, progress=progress)


def long_prices():
    return {"AAA": sup.series(50, LONG_DAYS, drift=0.0003), "BBB": sup.series(80, LONG_DAYS, drift=-0.0002),
            "SPY": sup.series(300, LONG_DAYS, drift=0.0002)}


class EpisodeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.loader = KeylessLoader(long_prices())
        cls.spec = sup.spec(SYMS)
        cls.base = engine.run(cls.spec, start=date(2021, 1, 4), end=date(2021, 12, 30), data=cls.loader)

    def test_pre_seam_episode_without_key_is_not_available(self):
        out = stress.episode("gfc", "GFC", date(2007, 10, 9), date(2009, 3, 9), self.spec, capital=100000.0,
                             benchmark="SPY", data=self.loader)
        self.assertEqual(out["status"], "not_available")
        self.assertTrue(out["reason"].startswith("not available: "))
        self.assertIn("Tiingo API key", out["reason"])
        self.assertIsNone(out["metrics"])

    def test_episode_runs_on_its_window(self):
        out = stress.episode("covid", "Covid", date(2020, 2, 19), date(2020, 3, 23), self.spec, capital=100000.0,
                             benchmark="SPY", data=self.loader)
        self.assertEqual(out["status"], "done", out["reason"])
        self.assertEqual(out["range"], {"start": "2020-02-19", "end": "2020-03-23"})
        self.assertIn("max_drawdown", out["metrics"]["strategy"])
        self.assertEqual(out["metrics"]["benchmark"]["symbol"], "SPY")

    def test_partial_coverage_is_not_available(self):
        out = stress.episode("crypto_2022", "Crypto 2022", date(2021, 11, 10), date(2023, 6, 30), self.spec,
                             capital=100000.0, benchmark="SPY", data=self.loader)
        self.assertEqual(out["status"], "not_available")
        self.assertIn("not available:", out["reason"])

    def test_data_starting_inside_the_episode_is_not_available(self):
        late = {s: {d: oc for d, oc in rows.items() if d >= date(2020, 3, 9)} for s, rows in long_prices().items()}
        out = stress.episode("covid", "Covid", date(2020, 2, 19), date(2020, 3, 23), self.spec, capital=100000.0,
                             benchmark="SPY", data=KeylessLoader(late))
        self.assertEqual(out["status"], "not_available")
        self.assertIn("covers only", out["reason"])

    def test_no_symbol_data_is_not_available(self):
        out = stress.episode("covid", "Covid", date(2020, 2, 19), date(2020, 3, 23), sup.spec(["ZZZ"]),
                             capital=100000.0, benchmark="SPY", data=self.loader)
        self.assertEqual(out["status"], "not_available")

    def test_source_failure_is_failed_not_hidden(self):
        def boom(spec, **kw):
            raise DataSourceUnavailable("tiingo", "equity_volume", "HTTP 500")
        out = stress.episode("covid", "Covid", date(2020, 2, 19), date(2020, 3, 23), self.spec, capital=1e5,
                             benchmark="SPY", run_engine=boom)
        self.assertEqual(out["status"], "failed")
        self.assertTrue(out["reason"])

    def test_cancel_propagates(self):
        def cancelled(spec, **kw):
            raise engine.BacktestCancelled()
        with self.assertRaises(engine.BacktestCancelled):
            stress.episode("covid", "Covid", date(2020, 2, 19), date(2020, 3, 23), self.spec, capital=1e5,
                           benchmark="SPY", run_engine=cancelled)

    def test_run_stress_all_scenarios(self):
        events = []
        out = stress.run_stress(self.spec, self.base, data=self.loader, progress=lambda *a: events.append(a))
        keys = [s["key"] for s in out["scenarios"]]
        self.assertEqual(keys, [e[0] for e in stress.EPISODES] + ["costs_x2", "slippage_x3", "monte_carlo"])
        st = {s["key"]: s for s in out["scenarios"]}
        for k in ("dotcom", "gfc"):
            self.assertEqual(st[k]["status"], "not_available")
            self.assertIn("Tiingo API key", st[k]["reason"])
        for k in ("q4_2018", "covid", "rate_shock_2022", "crypto_2022"):
            self.assertEqual(st[k]["status"], "done", (k, st[k]["reason"]))
        self.assertEqual(st["costs_x2"]["costs"]["equity"]["per_fill_bps"], 10.0)
        self.assertEqual((st["slippage_x3"]["costs"]["equity"]["fee_bps"],
                          st["slippage_x3"]["costs"]["equity"]["slippage_bps"]), (2.5, 7.5))
        base_cost = metrics.compute(self.base, include_signal=False)["costs"]["total"]
        self.assertGreater(st["costs_x2"]["metrics"]["costs_total"], base_cost)
        self.assertLessEqual(st["costs_x2"]["metrics"]["delta_vs_base"]["total_return"], 0.0)
        self.assertEqual(st["monte_carlo"]["status"], "done")
        self.assertEqual(out["seed"], stress.MC_SEED)
        # click-test 2026-10-22: paper-broker costs (fee = slippage) → "Slippage x3" = "Costs x2" by construction;
        # presentation fields are added on read (runcard.get → stress.with_summaries)
        self.assertNotIn("warning_summary", st["costs_x2"])
        stress.with_summaries(out)
        self.assertEqual(st["slippage_x3"]["note"], stress.SAME_COST_NOTE)
        self.assertNotIn("note", st["costs_x2"])
        self.assertEqual(st["costs_x2"]["metrics"]["strategy"], st["slippage_x3"]["metrics"]["strategy"])
        for sc in out["scenarios"]:
            self.assertEqual(set(sc["warning_summary"]), {"total", "counts", "symbols", "text"})
        self.assertEqual(events[-1][0:2], ("stress", 100.0))

    def test_cost_scenarios_differ_when_fee_and_slippage_differ(self):
        custom = sup.spec(SYMS, costs={"model": "custom", "fee_bps": 1.0, "slippage_bps": 4.0})
        base = engine.run(custom, start=date(2021, 1, 4), end=date(2021, 12, 30), data=self.loader)
        a = stress.synthetic_costs("costs_x2", "Costs x2", custom, base, 2.0, 2.0, data=self.loader)
        b = stress.synthetic_costs("slippage_x3", "Slippage x3", custom, base, 1.0, 3.0, data=self.loader)
        self.assertEqual((a["costs"]["equity"]["per_fill_bps"], b["costs"]["equity"]["per_fill_bps"]), (10.0, 13.0))
        self.assertNotEqual(a["metrics"]["strategy"]["total_return"], b["metrics"]["strategy"]["total_return"])
        self.assertGreater(a["metrics"]["strategy"]["total_return"], b["metrics"]["strategy"]["total_return"])
        stress.same_cost_note(a, b)
        self.assertNotIn("note", b)


class WarningSummaryTest(unittest.TestCase):
    def test_structured_summary(self):
        w = ([{"code": "symbol_excluded", "symbol": f"S{i:02d}", "message": "x"} for i in range(40)]
             + [{"code": "seam_mismatch", "symbol": s, "message": "x"} for s in ("SPY", "XLE", "SPY")]
             + [{"code": "short_warmup", "message": "x"}])
        out = stress.warning_summary(w)
        self.assertEqual(out["total"], 44)
        self.assertEqual(out["counts"], {"symbol_excluded": 40, "seam_mismatch": 3, "short_warmup": 1})
        self.assertEqual(out["symbols"]["seam_mismatch"], ["SPY", "XLE"])
        self.assertTrue(out["text"].startswith("40 symbols excluded (no data in the window): S00, S01"))
        self.assertIn("and 32 more; 2 symbols with a seam mismatch (no pre-2016 data used): SPY, XLE; shorter "
                      "warm-up than requested", out["text"])
        self.assertEqual(stress.warning_summary([]), {"total": 0, "counts": {}, "symbols": {}, "text": ""})


class SyntheticCostTest(unittest.TestCase):
    def test_scaled_costs_per_class(self):
        o, d, why = stress.scaled_costs(sup.spec(SYMS), 2.0, 2.0)
        self.assertIsNone(why)
        self.assertEqual(o, {"fees_bps": 10.0, "crypto_fees_bps": 40.0})
        self.assertEqual((d["equity"]["fee_bps"], d["equity"]["slippage_bps"]), (5.0, 5.0))
        o, d, _ = stress.scaled_costs(sup.spec(["BTC-USD"]), 1.0, 3.0)
        self.assertEqual(o, {"fees_bps": 10.0, "crypto_fees_bps": 40.0})      # 2.5 + 7.5 / 10 + 30
        self.assertEqual((d["crypto"]["fee_bps"], d["crypto"]["slippage_bps"]), (10.0, 30.0))
        custom = sup.spec(SYMS, costs={"model": "custom", "fee_bps": 1.0, "slippage_bps": 4.0})
        o, _, _ = stress.scaled_costs(custom, 1.0, 3.0)
        self.assertEqual(o, {"fees_bps": 13.0, "crypto_fees_bps": 13.0})
        big = sup.spec(SYMS, costs={"model": "custom", "fee_bps": 600.0, "slippage_bps": 600.0})
        o, _, why = stress.scaled_costs(big, 2.0, 2.0)
        self.assertIsNone(o)
        self.assertTrue(why.startswith("not available: "))

    def test_mixed_universe_costs_exactly_x2_per_class(self):
        from datetime import timedelta
        days = sup.DAYS
        crypto, d, k = {}, days[0] - timedelta(days=40), 0
        while d <= days[-1]:
            o, c = 30000 * (1 + 0.002 * ((k * 7) % 5 - 2)), 30000 * (1 + 0.002 * ((k * 3) % 5 - 2))
            crypto[d] = (o, c, 1e6)
            d += timedelta(days=1)
            k += 1
        prices = {"AAA": sup.series(50), "SPY": sup.series(400)}
        loader = sup.FakeLoader(prices, crypto={"BTC-USD": crypto})
        spec = sup.spec(["AAA", "BTC-USD"], signal={"op": "returns", "args": [1]})
        base = engine.run(spec, data=loader)
        overrides, _, why = stress.scaled_costs(spec, 2.0, 2.0)
        self.assertIsNone(why)
        x2 = engine.run(spec, data=loader, cost_overrides=overrides)
        self.assertEqual(x2.costs["overrides"], overrides)
        self.assertIsNone(base.costs["overrides"])

        def rates(res, crypto_cls):
            """(fee / fill notional, slippage / quote notional) of one asset class's fills."""
            fills = [f for f in res.fills if (f["symbol"] == "BTC-USD") == crypto_cls]
            self.assertTrue(fills)
            slip = metrics.compute(dict(res.to_dict(), fills=fills, ledger=[]), include_signal=False)["costs"]
            h = (res.broker_params["crypto_fees_bps"] if crypto_cls else res.broker_params["fees_bps"]) / 2e4
            quote = sum(f["qty"] * f["fill_price"] / (1 + h if f["side"] in ("buy", "cover") else 1 - h)
                        for f in fills)
            return sum(f["fees"] for f in fills) / sum(f["notional"] for f in fills), slip["slippage"] / quote

        for crypto_cls, bps in ((False, 5.0), (True, 20.0)):
            f0, s0 = rates(base, crypto_cls)
            f2, s2 = rates(x2, crypto_cls)
            self.assertAlmostEqual(f0, bps / 2 / 10000, places=12)
            self.assertAlmostEqual(s0, bps / 2 / 10000, places=12)
            self.assertAlmostEqual(f2 / f0, 2.0, places=9)                # fee rate exactly x2
            self.assertAlmostEqual(s2 / s0, 2.0, places=9)                # slippage rate exactly x2

    def test_engine_rejects_bad_overrides(self):
        prices = {"AAA": sup.series(50), "BBB": sup.series(80), "SPY": sup.series(400)}
        for bad in ({"fees_bps": -1}, {"bogus": 1}, {"fees_bps": 5000}, {}):
            with self.assertRaises(InvalidRequest):
                engine.run(sup.spec(SYMS), data=sup.FakeLoader(prices), cost_overrides=bad)


class MonteCarloTest(unittest.TestCase):
    def rets(self, n=500):
        import numpy as np
        return list(np.random.default_rng(3).normal(0.0004, 0.01, n))

    def test_deterministic_with_seed(self):
        a = stress.monte_carlo(self.rets(), periods_per_year=252, seed=11)
        b = stress.monte_carlo(self.rets(), periods_per_year=252, seed=11)
        c = stress.monte_carlo(self.rets(), periods_per_year=252, seed=12)
        self.assertEqual(a, b)
        self.assertNotEqual(a["metrics"], c["metrics"])
        self.assertEqual((a["paths"], a["block_length"], a["returns"]), (1000, 8, 500))
        mdd, cagr = a["metrics"]["max_drawdown"], a["metrics"]["cagr"]
        self.assertTrue(0 <= mdd["p5"] <= mdd["p50"] <= mdd["p95"] < 1)
        self.assertTrue(cagr["p5"] <= cagr["p50"] <= cagr["p95"])

    def test_constant_returns_give_the_exact_values(self):
        out = stress.monte_carlo([0.001] * 252, periods_per_year=252, seed=1)
        self.assertAlmostEqual(out["metrics"]["cagr"]["p50"], 1.001 ** 252 - 1)
        self.assertEqual(out["metrics"]["max_drawdown"]["p95"], 0.0)

    def test_too_few_returns(self):
        out = stress.monte_carlo([0.01] * 10, periods_per_year=252)
        self.assertEqual(out["status"], "not_available")
        self.assertIn("needs 60", out["reason"])
        self.assertEqual(stress.block_length(1000), 10)
        self.assertEqual(stress.block_length(1), 2)


if __name__ == "__main__":
    unittest.main()
