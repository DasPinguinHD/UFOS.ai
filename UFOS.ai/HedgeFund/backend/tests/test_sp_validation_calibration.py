"""Stage 4 (V2) calibration (SPEC §9.6) on synthetic data with the fast
stand-in engine of ``test_sp_validation_support`` (the real engine is
exercised end-to-end in ``test_sp_validation_jobs``):

- a strategy using a planted, persistent cross-sectional signal passes
  G1–G8 (Standard) and the holdout gate H;
- pure-noise strategies: ≥ 95 % of 40 seeds fail at least one gate;
- an overfitted grid (the best of 64 noise configurations, all 64 tried as
  backtests = trials) fails G4 or G5.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_validation_calibration -v
"""
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strategy_platform.validation import folds, gates, jobs, stats  # noqa: E402
import test_sp_validation_support as sup  # noqa: E402

TRAINING_END = date(2024, 10, 7)          # holdout window from 2024-10-08 (24 months before 2026-10-08)
TH = gates.effective("standard")


def validate(market, kind, spec, n_before, sharpes_before, episode=None):
    run_stress, synthetic_costs = sup.fake_stress(market, kind, episode)
    return jobs.run_validation(spec, run_engine=market.engine(kind), compute_metrics=sup.fake_metrics,
                               run_stress=run_stress, synthetic_costs=synthetic_costs, training_end=TRAINING_END,
                               thresholds=TH, settings={"preset": "standard", "overrides": {}},
                               n_trials_before=n_before, trial_sharpes_before=sharpes_before)


def failed(rep):
    return [g["gate"] for g in rep["gates"] if g["status"] != "pass"]


class PlantedSignalTest(unittest.TestCase):
    def test_planted_signal_passes_all_gates_and_holdout(self):
        m = sup.Market(1)
        out = validate(m, "planted", sup.spec_planted(), 3, [3.0, 3.3, 3.1], ("2020-02-19", "2020-03-23"))
        rep = out["report"]
        self.assertEqual(failed(rep), [], [(g["gate"], g["explanation"]) for g in rep["gates"]])
        self.assertTrue(rep["passed"])
        self.assertEqual(rep["mode"], "cross_sectional")
        self.assertEqual(len(rep["folds"]), 5)
        self.assertEqual(rep["grid"]["total"], 11)
        self.assertEqual(rep["pbo"]["combinations"], 252)
        self.assertEqual(rep["trial_count"], 4)
        self.assertEqual(rep["training"]["range"]["end"], "2024-10-07")
        # grid runs share the training range; no run touched the holdout window
        self.assertTrue(all(c["end"] is None or str(c["end"]) <= "2024-10-07" for c in m.calls))
        self.assertFalse(any(c["holdout_access"] for c in m.calls))
        g6 = next(g for g in rep["gates"] if g["gate"] == "G6")
        self.assertEqual([e["check"] for e in g6["details"]["episodes"]], ["pass", "skipped"])
        # holdout on the full window
        h = jobs.run_holdout(sup.spec_planted(), run_engine=m.engine("planted"), compute_metrics=sup.fake_metrics,
                             window={"start": "2024-10-08", "end": "2026-10-08"},
                             training_sharpe=rep["training"]["sharpe"], horizon=rep["ic"]["best_horizon"])
        self.assertTrue(m.calls[-1]["holdout_access"])
        hg = h["report"]["gates"][0]
        self.assertEqual(hg["status"], "pass", hg["explanation"])
        self.assertTrue(h["report"]["passed"])
        self.assertGreater(hg["details"]["mean_ic"], 0)


class CryptoCapacityTest(unittest.TestCase):
    def test_crypto_only_g8_not_applicable(self):
        """User decision 2026-10-09: G8 for crypto-only universes = not applicable (pass)."""
        m = sup.Market(1)
        spec = sup.spec_planted()
        spec["universe"] = {"symbols": ["BTC-USD", "ETH-USD", "SOL-USD"]}
        rep = validate(m, "planted", spec, 3, [3.0, 3.3, 3.1])["report"]
        g8 = next(g for g in rep["gates"] if g["gate"] == "G8")
        self.assertEqual((g8["status"], g8["details"]["universe_kind"]), ("pass", "crypto"))
        self.assertIn("No capacity measurement for crypto", g8["explanation"])


class NoiseTest(unittest.TestCase):
    def test_noise_strategies_fail(self):
        fails = 0
        for seed in range(40):
            m = sup.Market(1000 + seed, first=date(2022, 1, 3), last=TRAINING_END, n_noise=8)
            res = m.simulate(m.noise[0], end=TRAINING_END)
            s = stats.sharpe([r for _, r in folds.daily_returns(res["equity"])], 252)
            rep = validate(m, "noise", sup.spec_noise(0, 7), 1, [s])["report"]
            fails += bool(failed(rep))
        self.assertGreaterEqual(fails / 40.0, 0.95)

    def test_best_of_64_noise_fails_g4_or_g5(self):
        for seed in (7, 8, 9):
            m = sup.Market(seed, first=date(2022, 1, 3), last=TRAINING_END)
            sharpes = []
            for k in range(64):
                res = m.simulate(m.noise[k], end=TRAINING_END)
                sharpes.append(stats.sharpe([r for _, r in folds.daily_returns(res["equity"])], 252))
            k = max(range(64), key=lambda i: sharpes[i])
            rep = validate(m, "noise", sup.spec_noise(k, 63), 64, sharpes)["report"]
            self.assertEqual(rep["grid"]["total"], 64)
            st = {g["gate"]: g["status"] for g in rep["gates"]}
            self.assertTrue(st["G4"] == "fail" or st["G5"] == "fail", (seed, st, rep["pbo"]["value"],
                                                                        rep["dsr"]["value"]))


class LibraryAlphaTest(unittest.TestCase):
    """SPEC §9.6: a library alpha's report runs end-to-end (no requirement to pass) — here through the
    real grid/report code with the stand-in engine (its signal is the planted factor; the library spec
    supplies the parameters, so the grid is built from a real library entry)."""

    def test_library_entry_grid_and_report(self):
        from strategy_platform.strategy import alphas
        entry = next(e for e in alphas.library() if (e["spec"].get("parameters") or {}))
        spec = dict(entry["spec"])
        m = sup.Market(3, first=date(2023, 1, 3), last=TRAINING_END)

        def run(s, **kw):
            return m.engine("planted")(sup.spec_planted(), **kw)
        rs0, sc0 = sup.fake_stress(m, "planted")

        def rs(s, base, **kw):
            return rs0(sup.spec_planted(), base, **kw)

        def sc(key, label, s, base, fm, sm, **kw):
            return sc0(key, label, sup.spec_planted(), base, fm, sm, **kw)
        out = jobs.run_validation(spec, run_engine=run, compute_metrics=sup.fake_metrics, run_stress=rs,
                                  synthetic_costs=sc, training_end=TRAINING_END, thresholds=TH,
                                  settings={"preset": "standard"}, n_trials_before=0, trial_sharpes_before=[])
        rep = out["report"]
        self.assertEqual([g["gate"] for g in rep["gates"]], list(gates.TRAINING_GATES))
        self.assertIn(rep["passed"], (True, False))
        self.assertGreaterEqual(len(rep["grid"]["configs"]), 1)


if __name__ == "__main__":
    unittest.main()
