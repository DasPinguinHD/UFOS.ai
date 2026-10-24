"""Stage 4 (V2): validation statistics — normal CDF/inverse, PSR, expected
maximum Sharpe (Bailey & López de Prado 2014 numbers), deflated Sharpe
(trial count / variance floor), PBO via CSCV (noise vs a dominant
configuration, input guards), Newey-West OLS (lag rule, lag 0 = White,
autocorrelation widens the error), alpha t-stat. Offline, numpy only.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_validation_stats -v
"""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from strategy_platform.validation import stats  # noqa: E402


class NormalTest(unittest.TestCase):
    def test_cdf_ppf(self):
        self.assertAlmostEqual(stats.norm_cdf(0.0), 0.5, places=15)
        self.assertAlmostEqual(stats.norm_cdf(1.959963984540054), 0.975, places=12)
        for p in (1e-12, 1e-6, 0.01, 0.02425, 0.3, 0.5, 0.7, 0.97575, 0.999, 1 - 1e-10):
            self.assertAlmostEqual(stats.norm_cdf(stats.norm_ppf(p)), p, delta=1e-12 * max(1.0, p * 100))
        self.assertAlmostEqual(stats.norm_ppf(0.975), 1.959963984540054, places=10)
        for bad in (0.0, 1.0, -0.1, 1.5):
            with self.assertRaises(ValueError):
                stats.norm_ppf(bad)


class SharpeTest(unittest.TestCase):
    def test_moments_and_sharpe(self):
        r = [0.01, -0.02, 0.03, 0.0, 0.015]
        m = stats.moments(r)
        self.assertEqual(m["n"], 5)
        self.assertAlmostEqual(m["mean"], np.mean(r))
        self.assertAlmostEqual(m["std"], np.std(r, ddof=1))
        self.assertAlmostEqual(stats.sharpe(r, 252), np.mean(r) / np.std(r, ddof=1) * math.sqrt(252))
        self.assertIsNone(stats.sharpe([0.01, 0.01, 0.01], 252))
        self.assertIsNone(stats.sharpe([0.01], 252))
        normal = np.random.default_rng(3).normal(size=200000)
        mo = stats.moments(normal)
        self.assertAlmostEqual(mo["skew"], 0.0, delta=0.02)
        self.assertAlmostEqual(mo["kurtosis"], 3.0, delta=0.05)

    def test_psr(self):
        self.assertAlmostEqual(stats.psr(0.1, 0.1, 500, 0.0, 3.0), 0.5)
        # normal returns: z = (SR - SR0) sqrt(T-1) / sqrt(1 + SR^2 / 2)
        z = (0.1 - 0.0) * math.sqrt(499) / math.sqrt(1 + 0.5 * 0.01)
        self.assertAlmostEqual(stats.psr(0.1, 0.0, 500, 0.0, 3.0), stats.norm_cdf(z))
        # negative skew / fat tails lower the PSR of a positive Sharpe
        self.assertLess(stats.psr(0.1, 0.0, 500, -2.0, 10.0), stats.psr(0.1, 0.0, 500, 0.0, 3.0))
        self.assertIsNone(stats.psr(1.0, 0.0, 500, 5.0, 3.0))     # non-positive variance term

    def test_expected_max_sharpe_reference_values(self):
        self.assertEqual(stats.expected_max_sharpe(1, 1.0), 0.0)
        # Bailey & López de Prado (2014): E[max] of N standard normals ~ 2.53 for N = 100, 3.26 for N = 1000
        self.assertAlmostEqual(stats.expected_max_sharpe(100, 1.0), 2.53, delta=0.01)
        self.assertAlmostEqual(stats.expected_max_sharpe(1000, 1.0), 3.26, delta=0.01)
        self.assertAlmostEqual(stats.expected_max_sharpe(100, 0.25), 0.5 * stats.expected_max_sharpe(100, 1.0))
        with self.assertRaises(ValueError):
            stats.expected_max_sharpe(0, 1.0)

    def test_deflated_sharpe(self):
        rng = np.random.default_rng(5)
        r = rng.normal(0.001, 0.01, 1000)           # annual Sharpe ~ 1.6, 4 years
        one = stats.deflated_sharpe(r, 252, 1, [])
        self.assertEqual(one["sr0_annual"], 0.0)
        self.assertEqual(one["variance_source"], "minimum")
        self.assertAlmostEqual(one["trial_variance"], 252 / 1000)
        many = stats.deflated_sharpe(r, 252, 100, [0.5, 1.0, 1.5, 2.0, -0.5])
        self.assertEqual(many["variance_source"], "trials")
        self.assertGreater(many["sr0_annual"], 1.0)
        self.assertLess(many["value"], one["value"])
        # identical re-runs: the variance floor applies
        same = stats.deflated_sharpe(r, 252, 10, [1.6] * 10)
        self.assertEqual(same["variance_source"], "minimum")
        self.assertGreater(same["sr0_annual"], 0.0)
        self.assertIsNone(stats.deflated_sharpe(r[:5], 252, 1, [])["value"])
        self.assertIn("needs", stats.deflated_sharpe(r[:5], 252, 1, [])["reason"])
        self.assertIsNone(stats.deflated_sharpe(np.zeros(100), 252, 1, [])["value"])
        self.assertIsNone(stats.deflated_sharpe(r, 252, 0, [])["value"])


class PboTest(unittest.TestCase):
    def test_combinations_and_noise(self):
        M = np.random.default_rng(1).normal(0, 0.01, (600, 20))
        out = stats.pbo_cscv(M)
        self.assertEqual(out["combinations"], 252)            # C(10, 5)
        self.assertEqual(len(out["logits"]), 252)
        vals = [stats.pbo_cscv(np.random.default_rng(s).normal(0, 0.01, (500, 32)))["value"] for s in range(12)]
        self.assertGreater(float(np.mean(vals)), 0.3)          # noise: around 0.5 on average
        self.assertLess(float(np.mean(vals)), 0.7)

    def test_dominant_configuration(self):
        rng = np.random.default_rng(2)
        M = rng.normal(0, 0.01, (600, 16)) + np.linspace(0.0, 0.003, 16)
        self.assertLess(stats.pbo_cscv(M)["value"], 0.1)
        # a dominant configuration that is the IS winner but WORST out of sample cannot exist with complementary
        # halves; a reversed trend in the second half gives a high PBO
        M2 = rng.normal(0, 0.01, (600, 16))
        M2[:300] += np.linspace(0.0, 0.003, 16)
        M2[300:] -= np.linspace(0.0, 0.003, 16)
        self.assertGreater(stats.pbo_cscv(M2)["value"], 0.3)

    def test_guards(self):
        self.assertIn("needs >= 4", stats.pbo_cscv(np.zeros((100, 3)))["reason"])
        self.assertIsNone(stats.pbo_cscv(np.zeros((100, 3)))["value"])
        self.assertIn("common days", stats.pbo_cscv(np.random.default_rng(0).normal(size=(30, 5)))["reason"])
        bad = np.random.default_rng(0).normal(size=(100, 5))
        bad[3, 2] = np.nan
        self.assertIn("missing", stats.pbo_cscv(bad)["reason"])
        self.assertIn("two-dimensional", stats.pbo_cscv([1, 2, 3])["reason"])
        with self.assertRaises(ValueError):
            stats.pbo_cscv(np.zeros((100, 5)), blocks=5)
        # constant columns rank by sign without crashing
        const = np.random.default_rng(0).normal(0, 0.01, (100, 5))
        const[:, 0] = 0.0
        self.assertIsNotNone(stats.pbo_cscv(const)["value"])


class NeweyWestTest(unittest.TestCase):
    def test_lag_rule(self):
        self.assertEqual(stats.nw_lag(100), 4)
        self.assertEqual(stats.nw_lag(1000), 6)
        self.assertEqual(stats.nw_lag(0), 0)

    def test_lag0_is_white_and_coefficients_match_ols(self):
        rng = np.random.default_rng(4)
        x = rng.normal(size=400)
        y = 0.5 + 2.0 * x + rng.normal(size=400)
        X = np.column_stack([np.ones(400), x])
        fit = stats.newey_west_ols(y, X, lag=0)
        b = np.linalg.lstsq(X, y, rcond=None)[0]
        self.assertTrue(np.allclose(fit["coef"], b))
        e = y - X @ b
        inv = np.linalg.inv(X.T @ X)
        white = inv @ (X.T * e ** 2) @ X @ inv * 400 / 398
        self.assertTrue(np.allclose(fit["se"], np.sqrt(np.diag(white))))

    def test_autocorrelation_widens_error(self):
        rng = np.random.default_rng(6)
        e = rng.normal(size=3000)
        ma = np.convolve(e, np.ones(10) / 10, mode="valid") + 0.02      # MA(9): strongly autocorrelated
        naive = np.mean(ma) / (np.std(ma, ddof=1) / math.sqrt(len(ma)))
        nw = stats.mean_t_newey_west(ma, min_lag=9)
        self.assertEqual(nw["lag"], 9)
        self.assertLess(abs(nw["value"]), abs(naive) / 2)
        self.assertIsNone(stats.mean_t_newey_west([0.1] * 5)["value"])

    def test_alpha(self):
        rng = np.random.default_rng(7)
        rb = rng.normal(0.0003, 0.01, 2000)
        rs = 0.001 + 0.5 * rb + rng.normal(0, 0.005, 2000)
        a = stats.alpha_newey_west(rs, rb, 252)
        self.assertAlmostEqual(a["beta"], 0.5, delta=0.05)
        self.assertAlmostEqual(a["alpha_annual"], 0.252, delta=0.05)
        self.assertGreater(a["value"], 5)
        self.assertIsNone(stats.alpha_newey_west(rs[:10], rb[:10], 252)["value"])
        self.assertIn("zero variance", stats.alpha_newey_west(rs, np.zeros(2000), 252)["reason"])
        noise = stats.alpha_newey_west(rng.normal(0, 0.01, 2000), rb, 252)
        self.assertLess(abs(noise["value"]), 4)


if __name__ == "__main__":
    unittest.main()
