"""Acerbi-Szekely Z2 ES backtest.

Size tolerance: 2000 replications at 5 %; the rejection rate must lie within
3 binomial standard deviations of 5 % (0.05 +- 0.0146), i.e. in [3.5 %, 6.5 %].
"""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: tests run without installing the package
    sys.path.insert(0, _SRC)

import unittest

import numpy as np
from scipy import stats

from risklab.backtest import REJECT, acerbi_szekely_z2, z2_from_frame, z2_statistic
from risklab.data import iid_normal
from risklab.dists import Empirical, LocScale
from risklab.engine import walk_forward
from risklab.models import HistoricalSimulation, NormalModel

A = 0.975
VAR = stats.norm.ppf(A)
ES = stats.norm.pdf(VAR) / (1 - A)


class Z2StatisticTests(unittest.TestCase):
    def test_sign_on_constructed_examples(self):
        T = 200
        v, e = np.full(T, VAR), np.full(T, ES)
        calm = np.zeros(T)
        self.assertEqual(z2_statistic(calm, v, e, A), -1.0)  # no exceedance at all
        hot = np.zeros(T)
        hot[:5] = 3 * ES  # exactly T (1 - alpha) = 5 exceedances, each 3 x ES
        self.assertAlmostEqual(z2_statistic(hot, v, e, A), 2.0, places=12)  # underestimation -> positive
        fair = np.zeros(T)
        fair[:5] = ES
        self.assertAlmostEqual(z2_statistic(fair, v, e, A), 0.0, places=12)

    def test_against_paper_sign(self):
        # paper: P&L X = -L, Z2 = sum X I / (T a ES) + 1 with a = 1 - alpha; ours is its negative
        rng = np.random.default_rng(1)
        L = rng.standard_normal(300) * 1.2
        v, e = np.full(300, VAR), np.full(300, ES)
        x = -L
        paper = np.sum(x * (x + v < 0) / e) / (300 * (1 - A)) + 1
        self.assertAlmostEqual(z2_statistic(L, v, e, A), -paper, places=12)


class Z2TestTests(unittest.TestCase):
    def test_size_under_h0(self):
        rng = np.random.default_rng(2)
        T, reps = 250, 2000
        dists = [LocScale(0.0, 1.0)] * T
        v, e = np.full(T, VAR), np.full(T, ES)
        rejections = sum(acerbi_szekely_z2(rng.standard_normal(T), v, e, dists, A, seed=r, m_sim=199).decision
                         == REJECT for r in range(reps))
        rate = rejections / reps
        self.assertLess(abs(rate - 0.05), 3 * np.sqrt(0.05 * 0.95 / reps), msg=f"rate={rate}")

    def test_power_against_underestimation(self):
        rng = np.random.default_rng(3)
        T, reps = 250, 100
        dists = [LocScale(0.0, 1.0)] * T
        v, e = np.full(T, VAR), np.full(T, ES)
        # true losses have 1.5 x the model's volatility: ES is underestimated
        rej = sum(acerbi_szekely_z2(1.5 * rng.standard_normal(T), v, e, dists, A, seed=r, m_sim=199).decision
                  == REJECT for r in range(reps))
        self.assertGreater(rej / reps, 0.8)
        # overestimation is not rejected (one-sided)
        rej = sum(acerbi_szekely_z2(0.6 * rng.standard_normal(T), v, e, dists, A, seed=r, m_sim=199).decision
                  == REJECT for r in range(reps))
        self.assertEqual(rej, 0)

    def test_fast_path_equals_generic_path(self):
        class Wrapped:
            def __init__(self, d):
                self.d = d

            def ppf(self, u):
                return self.d.ppf(u)

        rng = np.random.default_rng(4)
        T = 120
        dists = [LocScale(0.1 * i / T, 1 + i / T, 5.0 if i % 2 else None) for i in range(T)]
        v = np.array([float(d.ppf(A)) for d in dists])
        e = np.array([float(np.mean(d.ppf(np.linspace(A, 1, 20001)[1:-1]))) for d in dists])
        L = rng.standard_normal(T) * 1.3
        a = acerbi_szekely_z2(L, v, e, dists, A, seed=7, m_sim=500)
        b = acerbi_szekely_z2(L, v, e, [Wrapped(d) for d in dists], A, seed=7, m_sim=500)
        self.assertEqual(a.p_value, b.p_value)
        self.assertAlmostEqual(a.details["mean_z2_h0"], b.details["mean_z2_h0"], places=12)

    def test_frame_with_hs_and_normal(self):
        s = iid_normal(600, 0.0, 0.01, 5)
        for model in (HistoricalSimulation, NormalModel):
            frame = walk_forward(s.losses, model, 250, [A])
            r = z2_from_frame(frame, A, seed=1, m_sim=199)
            self.assertEqual(r.name, "acerbi_szekely_z2")
            self.assertEqual(r.details["m_sim"], 199)
            self.assertTrue(0 < r.p_value <= 1)
        self.assertIsInstance(frame.dists[0], LocScale)
        self.assertIsInstance(walk_forward(s.losses, HistoricalSimulation, 250, [A]).dists[0], Empirical)

    def test_validation(self):
        d = [LocScale(0, 1)] * 3
        with self.assertRaises(ValueError):
            acerbi_szekely_z2([1, 2, 3], [1, 1, 1], [0, 1, 1], d, A, seed=1)
        with self.assertRaises(ValueError):
            acerbi_szekely_z2([1, 2, 3], [1, 1, 1], [1, 1, 1], d[:2], A, seed=1)
        with self.assertRaises(ValueError):
            acerbi_szekely_z2([1, 2, 3], [1, 1, 1], [1, 1, 1], d, A, seed=1, m_sim=10)
        with self.assertRaises(TypeError):
            acerbi_szekely_z2([1, 2, 3], [1, 1, 1], [1, 1, 1], d, A)  # seed is required


if __name__ == "__main__":
    unittest.main()
