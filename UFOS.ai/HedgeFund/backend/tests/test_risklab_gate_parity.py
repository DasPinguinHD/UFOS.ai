"""Parity of the Risk Lab re-implementation of the validation gates (UFOS.ai/RiskLab,
``risklab.validation``) with ``strategy_platform.validation`` on fixed synthetic inputs.

Risk Lab never imports the HedgeFund backend; this test is the only place where the two meet.
It adds ``UFOS.ai/RiskLab/src`` to sys.path and is skipped with a reason when Risk Lab, numpy or
scipy is not available.

Tolerances (stated): DSR, PSR, expected maximum Sharpe, Sharpe and the Newey-West t / se /
coefficients agree to an absolute 1e-9 (rtol 1e-9 for the coefficients); the normal CDF / PPF
differ (scipy vs the platform's erfc / Acklam + Halley step), both at ~1e-15 relative accuracy.
PBO and its combination count agree exactly (the same 252 splits; the union Sharpe ratios are
computed from block sums in Risk Lab and from the concatenated rows in the platform - equal up
to rounding, so no ranking changes on these inputs). Fold bounds, grid axes, the 64-configuration
subsample with the forced base, the neighbour sets and every gate decision agree exactly.
Synthetic inputs only; offline.
"""
import math
import os
import sys
import unittest
from unittest import mock

TESTS = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(TESTS)
HF = os.path.dirname(BACKEND)
RISKLAB_SRC = os.path.join(os.path.dirname(HF), "RiskLab", "src")
sys.path.insert(0, BACKEND)

_SKIP = None
try:
    import numpy as np  # noqa: E402
    import scipy  # noqa: E402,F401
except ImportError as exc:  # pragma: no cover - depends on the environment
    _SKIP = f"numpy / scipy not available ({exc})"
if _SKIP is None and not os.path.isdir(os.path.join(RISKLAB_SRC, "risklab", "validation")):
    _SKIP = f"Risk Lab not found at {RISKLAB_SRC}"
if _SKIP is None:
    if RISKLAB_SRC not in sys.path:
        sys.path.insert(0, RISKLAB_SRC)
    from risklab.validation import folds as rl_folds  # noqa: E402
    from risklab.validation import gates as rl_gates  # noqa: E402
    from risklab.validation import grid as rl_grid  # noqa: E402
    from risklab.validation import stats as rl  # noqa: E402
    from strategy_platform.validation import folds as hf_folds  # noqa: E402
    from strategy_platform.validation import gates as hf_gates  # noqa: E402
    from strategy_platform.validation import grid as hf_grid  # noqa: E402
    from strategy_platform.validation import stats as hf  # noqa: E402

TOL = 1e-9


def _returns(seed, n, drift=0.0004, scale=0.01):
    rng = np.random.default_rng(seed)
    return rng.standard_t(5, n) * scale * math.sqrt(3 / 5) + drift


@unittest.skipIf(_SKIP is not None, _SKIP or "")
class StatsParity(unittest.TestCase):
    def test_sharpe_moments_psr(self):
        for seed, n in ((1, 30), (2, 504), (3, 2520)):
            r = _returns(seed, n)
            self.assertAlmostEqual(rl.sharpe(r, 252), hf.sharpe(list(r), 252), delta=TOL)
            a, b = rl.moments(r), hf.moments(list(r))
            for k in ("mean", "std", "skew", "kurtosis"):
                self.assertAlmostEqual(a[k], b[k], delta=TOL * max(1.0, abs(b[k])))
        self.assertIsNone(rl.sharpe([0.01] * 30))
        self.assertIsNone(hf.sharpe([0.01] * 30))
        for args in ((0.1, 0.05, 1000, -0.5, 6.0), (0.02, 0.0, 300, 0.2, 3.5), (0.5, 0.0, 50, 3.0, 2.0)):
            p, q = rl.psr(*args), hf.psr(*args)
            self.assertEqual(p is None, q is None)
            if p is not None:
                self.assertAlmostEqual(p, q, delta=TOL)

    def test_expected_max_and_dsr(self):
        for n in (1, 2, 9, 65, 257, 1000):
            for v in (0.05, 0.2, 1.0):
                self.assertAlmostEqual(rl.expected_max_sharpe(n, v), hf.expected_max_sharpe(n, v), delta=TOL)
        cases = [(_returns(4, 1260), 9, [0.1, 0.4, -0.2, 0.8, 0.3, 0.0, 0.5, 0.6, 0.9]),
                 (_returns(5, 2520, 0.0), 257, list(np.linspace(-0.6, 0.7, 256)) + [0.7]),
                 (_returns(6, 504), 2, [0.5, 0.5]),
                 (_returns(7, 300, 0.001), 65, [0.2] * 64 + [1.1]),
                 (_returns(8, 19), 5, [0.1, 0.2])]
        for r, n, trials in cases:
            a, b = rl.deflated_sharpe(r, 252, n, trials), hf.deflated_sharpe(list(r), 252, n, trials)
            self.assertEqual(a["value"] is None, b["value"] is None)
            self.assertEqual(a["variance_source"], b["variance_source"])
            if a["value"] is not None:
                self.assertAlmostEqual(a["value"], b["value"], delta=TOL)
                self.assertAlmostEqual(a["sr0_annual"], b["sr0_annual"], delta=TOL)
                self.assertAlmostEqual(a["trial_variance"], b["trial_variance"], delta=TOL)

    def test_pbo(self):
        rng = np.random.default_rng(11)
        mats = [rng.normal(0, 0.01, (1260, 64)),
                rng.normal(0, 0.01, (503, 16)) + np.linspace(0, 0.001, 16),
                rng.normal(0, 0.01, (50, 4)),
                rng.normal(0, 0.01, (2520, 64)) + np.where(np.arange(64) == 9, 0.003, 0.0)]
        for M in mats + [rng.normal(size=(49, 5)), rng.normal(size=(100, 3))]:
            a, b = rl.pbo_cscv(M), hf.pbo_cscv(M)
            self.assertEqual(a["value"], b["value"])
            self.assertEqual(a["combinations"], b["combinations"])
            self.assertEqual(a["overfit"], b["overfit_combinations"])
            if a["mean_logit"] is not None:
                self.assertAlmostEqual(a["mean_logit"], b["mean_logit"], delta=TOL)

    def test_newey_west(self):
        rng = np.random.default_rng(3)
        for n in (20, 100, 1260, 2520):
            self.assertEqual(rl.nw_lag(n), hf.nw_lag(n))
        x = rng.normal(size=600)
        y = 0.05 + 0.4 * x + np.convolve(rng.normal(size=602), [1, 0.5, 0.25], "valid")
        X = np.column_stack([np.ones(600), x])
        for lag in (None, 0, 5, 12):
            a, b = rl.newey_west_ols(y, X, lag), hf.newey_west_ols(y, X, lag)
            self.assertEqual(a["lag"], b["lag"])
            np.testing.assert_allclose(a["coef"], b["coef"], rtol=TOL, atol=0)
            np.testing.assert_allclose(a["se"], b["se"], rtol=TOL, atol=0)
            np.testing.assert_allclose(a["t"], b["t"], rtol=TOL, atol=0)
        for min_lag in (0, 4, 20):
            a, b = rl.mean_t_newey_west(y, min_lag), hf.mean_t_newey_west(list(y), min_lag)
            self.assertAlmostEqual(a["value"], b["value"], delta=TOL)
            self.assertEqual(a["lag"], b["lag"])
        rb = rng.normal(0, 0.01, 1000)
        rs = 0.0003 + 0.2 * rb + rng.normal(0, 0.006, 1000)
        a, b = rl.alpha_newey_west(rs, rb, 252), hf.alpha_newey_west(list(rs), list(rb), 252)
        for k in ("value", "alpha_daily", "alpha_annual", "beta", "se"):
            self.assertAlmostEqual(a[k], b[k], delta=TOL)
        self.assertEqual(a["lag"], b["lag"])
        self.assertIsNone(rl.alpha_newey_west(rs, np.zeros(1000), 252)["value"])
        self.assertIsNone(hf.alpha_newey_west(list(rs), [0.0] * 1000, 252)["value"])


SPECS = [
    {"fast": {"min": 4, "max": 19, "step": 1, "value": 8, "integer": True},
     "slow": {"min": 24, "max": 69, "step": 3, "value": 42, "integer": True}},
    {"fast": {"min": 4, "max": 19, "step": 1, "value": 4, "integer": True},
     "slow": {"min": 24, "max": 69, "step": 3, "value": 69, "integer": True}},
    {"fast": {"min": 4, "max": 19, "step": 1, "value": 13, "integer": True},
     "slow": {"min": 24, "max": 69, "step": 3, "value": 27, "integer": True}},
    {"b": {"min": 8, "max": 12, "step": 1, "value": 10, "integer": True},
     "a": {"min": 0.0, "max": 1.0, "step": 0.25, "value": 0.0}},
    {"x": {"min": 0.5, "max": 3.0, "step": 0.5, "value": 1.3}, "y": {"min": 1, "max": 40, "step": 1, "value": 17},
     "z": {"min": 1, "max": 2, "step": 1, "value": 1}},
]


@unittest.skipIf(_SKIP is not None, _SKIP or "")
class GridFoldParity(unittest.TestCase):
    def test_fold_bounds(self):
        for n in (5, 7, 1260, 2521, 5040):
            self.assertEqual(rl_folds.fold_bounds(n), hf_folds.fold_bounds(n))

    def test_axes_subsample_neighbours(self):
        for params in SPECS:
            with self.subTest(params=sorted(params)):
                for p in params.values():
                    self.assertEqual(rl_grid.axis_values(p), hf_grid.axis_values(p))
                spec = {"parameters": params}
                # every parameter of these specs is used: the platform's validator check is replaced
                with mock.patch.object(hf_grid, "unused_params", return_value=set()):
                    b = hf_grid.build(spec)
                a = rl_grid.build(params)
                self.assertEqual(a["configs"], b["configs"])
                self.assertEqual(a["base_index"], b["base_index"])
                self.assertEqual(a["total"], b["total"])
                self.assertEqual(rl_grid.neighbours(a, params), hf_grid.neighbours(b, spec))


@unittest.skipIf(_SKIP is not None, _SKIP or "")
class GateDecisionParity(unittest.TestCase):
    def _cases(self):
        rng = np.random.default_rng(17)
        for _ in range(300):
            yield {"folds": [float(v) if rng.random() > 0.05 else None for v in rng.normal(0.3, 0.8, 5)],
                   "alpha_t": float(rng.normal(1.5, 1.5)), "alpha_daily": float(rng.normal(1e-4, 2e-4)),
                   "dsr": float(rng.choice([rng.random(), 0.9, 0.95, 0.99])),
                   "pbo": float(rng.choice([rng.random(), 0.2, 0.3, 0.5])), "configs": int(rng.choice([3, 4, 64])),
                   "base": float(rng.normal(0.5, 0.5)), "nb": [float(v) for v in rng.normal(0.4, 0.4, rng.integers(0, 4))],
                   "trades": int(rng.choice([49, 50, 99, 100, 199, 200, 300])),
                   "h": float(rng.normal(0.3, 0.6)), "train": float(rng.normal(0.6, 0.6))}

    def test_decisions(self):
        n = 0
        for c in self._cases():
            for name in ("lenient", "standard", "strict"):
                th_hf = hf_gates.PRESETS[name]
                th_rl = rl_gates.PRESETS[name]
                for k, v in th_rl.items():
                    self.assertEqual(v, th_hf[k])
                folds = [{"sharpe": s} for s in c["folds"]]
                hf_dec = {
                    "G1": hf_gates.g1({"mode": "time_series", "folds": folds}, th_hf)["status"] == "pass",
                    "G2": hf_gates.g2({"alpha": {"value": c["alpha_t"], "alpha_daily": c["alpha_daily"],
                                                 "alpha_annual": 252 * c["alpha_daily"]}}, th_hf)["status"] == "pass",
                    "G4": hf_gates.g4({"dsr": {"value": c["dsr"], "n_trials": 9, "sr0_annual": 0.5}}, th_hf)["status"] == "pass",
                    "G5": hf_gates.g5({"pbo": {"value": c["pbo"], "configs": c["configs"]}}, th_hf)["status"] == "pass",
                    # neighbour part only: costs pass and no stress episode, so G6 decides on the neighbours
                    "G6": hf_gates.g6({"grid": {"base_sharpe": c["base"], "neighbours": list(range(1, len(c["nb"]) + 1)),
                                                "sharpes": [c["base"]] + c["nb"]},
                                       "costs": {"sharpe": 1.0}, "episodes": []}, th_hf)["status"] == "pass",
                    "G7": hf_gates.g7({"trades_closed": c["trades"]}, th_hf)["status"] == "pass",
                    "H": hf_gates.holdout_gate(sharpe=c["h"], training_sharpe=c["train"], mode="time_series",
                                               mean_ic=None, horizon=None)["status"] == "pass",
                }
                rl_dec = {
                    "G1": rl_gates.g1(c["folds"], th_rl)[0],
                    "G2": rl_gates.g2({"value": c["alpha_t"], "alpha_daily": c["alpha_daily"]}, th_rl)[0],
                    "G4": rl_gates.g4({"value": c["dsr"]}, th_rl)[0],
                    "G5": rl_gates.g5({"value": c["pbo"], "configs": c["configs"]}, th_rl)[0],
                    "G6": rl_gates.g6_neighbours(c["base"], c["nb"], th_rl)[0],
                    "G7": rl_gates.g7(c["trades"], th_rl)[0],
                    "H": rl_gates.holdout(c["h"], c["train"])[0],
                }
                self.assertEqual(rl_dec, hf_dec, (name, c))
                n += 1
        self.assertEqual(n, 900)
        self.assertEqual(rl_gates.H_TRAINING_RATIO, hf_gates.H_TRAINING_RATIO)


if __name__ == "__main__":
    unittest.main()
