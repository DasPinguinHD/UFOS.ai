"""h-day VaR / ES: square-root-of-time, GARCH Monte Carlo, bootstrap, true h-day risk, non-overlapping blocks."""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: tests run without installing the package
    sys.path.insert(0, _SRC)

import unittest

import numpy as np
from scipy import stats

from risklab.data import garch11, iid_normal, iid_student_t, regime_switching, true_var_es, true_var_es_h
from risklab.engine import walk_forward_h
from risklab.horizon import (BootstrapForecaster, GarchMCForecaster, SqrtTimeForecaster, empirical_var_es,
                             simulate_garch_sums, sqrt_time)
from risklab.models import Forecast, GarchParams, NormalModel


class SqrtTimeTests(unittest.TestCase):
    def test_exact_for_iid_normal_truth(self):
        s = iid_normal(50, 0.0, 0.01, 1)
        one = true_var_es(s, [0.99])
        ten = true_var_es_h(s, [0.99], 10, 0, 0)
        self.assertAlmostEqual(float(ten.var[0.99][0]), float(sqrt_time(one.var[0.99][0], 10)), places=15)
        self.assertAlmostEqual(float(ten.es[0.99][0]), float(sqrt_time(one.es[0.99][0], 10)), places=15)
        self.assertEqual(ten.method, "closed form")
        # independent: the sum of 10 iid N(0, 0.01^2) is N(0, 10 * 0.01^2)
        self.assertAlmostEqual(float(ten.var[0.99][0]), stats.norm.ppf(0.99, scale=0.01 * np.sqrt(10)), places=14)

    def test_forecaster(self):
        x = iid_normal(300, 0.0, 0.01, 2).losses
        f = SqrtTimeForecaster(NormalModel)
        f.fit(x)
        m = NormalModel()
        m.fit(x)
        self.assertAlmostEqual(f.forecast_h([0.99], 4).var[0.99], 2 * m.forecast([0.99]).var[0.99], places=15)
        with self.assertRaises(ValueError):
            sqrt_time(1.0, 0)


class MonteCarloTests(unittest.TestCase):
    def test_mc_agrees_with_closed_form_normal(self):
        # GARCH with alpha = beta = 0 is iid N(mu, omega): the h-day sum is N(h mu, h omega)
        p = GarchParams(0.0005, 1e-4, 0.0, 0.0, None)
        sums = simulate_garch_sums(p, 1e-4, 10, 200_000, np.random.default_rng(3))
        est = empirical_var_es(sums, 0.99)
        exact = stats.norm.ppf(0.99, loc=0.005, scale=np.sqrt(10 * 1e-4))
        self.assertLess(abs(est.var - exact), 3 * est.var_se)  # stated error: binomial order-statistic interval
        self.assertTrue(est.var_ci95[0] <= exact <= est.var_ci95[1])
        es_exact = 0.005 + np.sqrt(10e-4) * stats.norm.pdf(stats.norm.ppf(0.99)) / 0.01
        self.assertLess(abs(est.es - es_exact), 4 * est.es_se)

    def test_true_h_matches_one_day_at_h1(self):
        g = garch11(600, 2e-6, 0.08, 0.9, innovations="t", nu=6.0, seed=4)
        one = true_var_es(g, [0.99])
        h1 = true_var_es_h(g, [0.99], 1, 100_000, 5, origins=[300, 450])
        for i, t in enumerate((300, 450)):
            self.assertLess(abs(h1.var[0.99][i] - one.var[0.99][t]), 4 * h1.var_se[0.99][i])
        s = iid_student_t(10, 0.0, 0.01, 5.0, 6)
        t1 = true_var_es_h(s, [0.99], 1, 100_000, 7)
        self.assertLess(abs(t1.var[0.99][0] - true_var_es(s, [0.99]).var[0.99][0]), 4 * t1.var_se[0.99][0])

    def test_true_h_validation(self):
        with self.assertRaises(ValueError):
            true_var_es_h(garch11(100, 2e-6, 0.08, 0.9, seed=1), [0.99], 10, 1000, 0)  # origins required
        with self.assertRaises(ValueError):
            true_var_es_h(regime_switching(100, seed=1), [0.99], 10, 1000, 0)
        with self.assertRaises(ValueError):
            true_var_es_h(iid_normal(10, 0, 0.01, 1), [0.99], 0, 1000, 0)

    def test_forecasters_run(self):
        x = garch11(800, 2e-6, 0.08, 0.9, innovations="t", nu=6.0, seed=8).losses
        g = GarchMCForecaster(n_paths=2000, seed=1)
        g.fit(x)
        fc = g.forecast_h([0.99], 10)
        self.assertIsInstance(fc, Forecast)
        self.assertGreater(fc.es[0.99], fc.var[0.99])
        self.assertEqual(g.forecast_h([0.99], 10).var, fc.var)  # same seed, same paths
        b = BootstrapForecaster(block_len=1.0, n_paths=2000, seed=1)
        b.fit(x)
        self.assertGreater(b.forecast_h([0.99], 10).es[0.99], b.forecast_h([0.99], 10).var[0.99])
        with self.assertRaises(ValueError):
            BootstrapForecaster(block_len=0.5)


class NonOverlappingBlockTests(unittest.TestCase):
    def test_blocks_and_no_look_ahead(self):
        seen = []

        class Spy:
            name = "spy"

            def fit(self, losses_past):
                seen.append(np.array(losses_past))

            def forecast_h(self, alphas, h):
                return Forecast(var={a: float(seen[-1].max()) for a in alphas})

        x = np.arange(100, dtype=np.float64)
        f = walk_forward_h(x, Spy, 30, [0.99], 10)
        np.testing.assert_array_equal(f.t, [30, 40, 50, 60, 70, 80, 90])
        np.testing.assert_array_equal(f.loss, [x[t:t + 10].sum() for t in f.t])
        for t, w in zip(f.t, seen):
            np.testing.assert_array_equal(w, x[t - 30:t])
        self.assertEqual(f.meta["blocks"], "non-overlapping")
        with self.assertRaises(ValueError):
            walk_forward_h(np.arange(35.0), Spy, 30, [0.99], 10)


if __name__ == "__main__":
    unittest.main()
