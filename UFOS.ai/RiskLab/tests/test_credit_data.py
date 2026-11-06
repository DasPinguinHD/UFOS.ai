"""Synthetic credit data: the true PD model, the one-factor copula (unconditional PD, conditional PD), seeds."""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: tests run without installing the package
    sys.path.insert(0, _SRC)

import math
import unittest

import numpy as np
from scipy import stats

from risklab.credit import data as D


class DataTests(unittest.TestCase):
    def test_true_pd_is_the_logit(self):
        x = np.array([[0.5, 1.0, 3.0, 1.0, 0.0, -1.0]])
        c = D.TRUE_COEF
        eta = c["const"] + 0.5 * c["leverage"] + 1.0 * c["log_icr"] + 3.0 * c["log_size"] + c["sector_1"] - c["macro"]
        self.assertAlmostEqual(float(D.true_pd(x)[0]), 1 / (1 + math.exp(-eta)), places=14)

    def test_unconditional_default_rate_equals_mean_pd(self):
        pn = D.generate_panel(2000, 200, seed=4)
        se = math.sqrt(np.mean(pn.pd_true) * (1 - np.mean(pn.pd_true)) / pn.y.size)
        # the systematic factor inflates the variance: allow the binomial se times the design effect
        self.assertLess(abs(pn.y.mean() - pn.pd_true.mean()), 6 * se * math.sqrt(1 + 2000 * 0.01))

    def test_correlation_moves_period_default_rates(self):
        pn = D.generate_panel(20000, 40, seed=5)
        dr = np.array([pn.y[pn.period == t].mean() for t in range(40)])
        pd = np.array([pn.pd_true[pn.period == t].mean() for t in range(40)])
        z = pn.z
        self.assertLess(np.corrcoef(dr / pd, z)[0, 1], -0.8)          # bad z (low) -> more defaults
        flat = D.generate_panel(20000, 40, seed=5, rho=0.0)
        dr0 = np.array([flat.y[flat.period == t].mean() for t in range(40)])
        self.assertLess(np.std(dr0 / np.array([flat.pd_true[flat.period == t].mean() for t in range(40)])),
                        np.std(dr / pd))

    def test_conditional_pd_integrates_to_pd(self):
        z = np.linspace(-8, 8, 20001)
        w = stats.norm.pdf(z) * (z[1] - z[0])
        for p in (0.003, 0.02, 0.1):
            self.assertAlmostEqual(float(np.sum(D.conditional_pd(p, 0.12, z) * w)), p, places=6)

    def test_seeds_and_errors(self):
        a, b = D.generate_panel(50, 3, seed=1), D.generate_panel(50, 3, seed=1)
        np.testing.assert_array_equal(a.y, b.y)
        np.testing.assert_array_equal(a.X, b.X)
        m = D.generate_panel(10, 2, seed=1, macro_path=[-2.0, 1.0])
        np.testing.assert_array_equal(m.macro, [-2.0, 1.0])
        with self.assertRaises(ValueError):
            D.generate_panel(10, 2, seed=1, rho=1.0)
        with self.assertRaises(ValueError):
            D.generate_panel(10, 2, seed=1, macro_path=[0.0])


if __name__ == "__main__":
    unittest.main()
