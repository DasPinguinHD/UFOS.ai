"""PD model: logistic MLE vs a hand-coded Newton solution, explicit separation errors, AUC vs Mann-Whitney,
CAP, Hosmer-Lemeshow, the binomial and Jeffreys tests, the master scale."""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: tests run without installing the package
    sys.path.insert(0, _SRC)

import math
import unittest

import numpy as np
from scipy import stats

from risklab.credit import pd as P
from risklab.models import FitError

X_TINY = [[0.1], [0.4], [0.5], [0.9], [1.3], [1.4], [1.8], [2.2], [2.5], [3.0], [3.1], [3.6]]
Y_TINY = [0, 0, 1, 0, 0, 1, 0, 1, 1, 0, 1, 1]


def newton_by_hand(x, y, iters=100):
    """Two-parameter Newton-Raphson written out with plain floats."""
    a, b = 0.0, 0.0
    for _ in range(iters):
        ga = gb = haa = hab = hbb = 0.0
        for xi, yi in zip(x, y):
            p = 1 / (1 + math.exp(-(a + b * xi)))
            ga += yi - p
            gb += (yi - p) * xi
            w = p * (1 - p)
            haa += w
            hab += w * xi
            hbb += w * xi * xi
        det = haa * hbb - hab * hab
        a, b = a + (hbb * ga - hab * gb) / det, b + (haa * gb - hab * ga) / det
    return a, b, math.sqrt(hbb / det), math.sqrt(haa / det)


class LogitTests(unittest.TestCase):
    def test_against_hand_newton(self):
        fit = P.fit_logit(X_TINY, Y_TINY)
        a, b, se_a, se_b = newton_by_hand([x[0] for x in X_TINY], Y_TINY)
        self.assertAlmostEqual(fit.coef[0], a, places=8)
        self.assertAlmostEqual(fit.coef[1], b, places=8)
        self.assertAlmostEqual(fit.se[0], se_a, places=8)
        self.assertAlmostEqual(fit.se[1], se_b, places=8)

    def test_recovers_large_sample(self):
        rng = np.random.default_rng(1)
        X = rng.normal(size=(40000, 2))
        p = 1 / (1 + np.exp(-(-2.0 + 0.8 * X[:, 0] - 0.5 * X[:, 1])))
        y = (rng.random(40000) < p).astype(int)
        fit = P.fit_logit(X, y)
        np.testing.assert_allclose(fit.coef, [-2.0, 0.8, -0.5], atol=4 * fit.se.max())

    def test_separation_is_explicit(self):
        with self.assertRaises(P.SeparationError):
            P.fit_logit([[0.0], [1.0], [2.0], [3.0]], [0, 0, 1, 1])
        with self.assertRaises(P.SeparationError):
            P.fit_logit([[0.0], [1.0]], [0, 0])
        self.assertTrue(issubclass(P.SeparationError, FitError))
        with self.assertRaises(P.ConvergenceError):
            P.fit_logit(X_TINY, Y_TINY, max_iter=1)


class DiscriminationTests(unittest.TestCase):
    def test_auc_is_mann_whitney(self):
        rng = np.random.default_rng(2)
        s = np.round(rng.normal(size=300), 1)          # with ties
        y = (rng.random(300) < 0.2).astype(int)
        u = stats.mannwhitneyu(s[y == 1], s[y == 0]).statistic
        self.assertAlmostEqual(P.auc(s, y), u / (y.sum() * (300 - y.sum())), places=12)
        brute = np.mean([(a > b) + 0.5 * (a == b) for a in s[y == 1] for b in s[y == 0]])
        self.assertAlmostEqual(P.auc(s, y), brute, places=12)
        self.assertAlmostEqual(P.gini(s, y), 2 * P.auc(s, y) - 1)

    def test_cap_curve(self):
        x, c = P.cap_curve([0.9, 0.1, 0.8, 0.2], [1, 0, 1, 0])
        np.testing.assert_allclose(c, [0, 0.5, 1, 1, 1])
        np.testing.assert_allclose(x, [0, 0.25, 0.5, 0.75, 1])


class CalibrationTests(unittest.TestCase):
    def test_hosmer_lemeshow_hand(self):
        p = np.array([0.1, 0.1, 0.2, 0.2, 0.3, 0.3, 0.4, 0.4])
        y = np.array([0, 1, 0, 0, 1, 0, 1, 1])
        hl = P.hosmer_lemeshow(p, y, groups=4, fitted_on_sample=True)
        want = sum((O - E) ** 2 / (E * (1 - E / 2)) for O, E in ((1, 0.2), (0, 0.4), (1, 0.6), (2, 0.8)))
        self.assertAlmostEqual(hl["statistic"], want, places=12)
        self.assertEqual(hl["df"], 2)                       # development sample of a fitted model: g - 2
        self.assertAlmostEqual(hl["p_value"], stats.chi2.sf(want, 2), places=14)
        ext = P.hosmer_lemeshow(p, y, groups=4, fitted_on_sample=False)
        self.assertEqual(ext["df"], 4)                      # true PD / out of sample: g
        self.assertEqual(ext["statistic"], hl["statistic"])
        self.assertAlmostEqual(ext["p_value"], stats.chi2.sf(want, 4), places=14)

    def test_hosmer_lemeshow_needs_the_case(self):
        with self.assertRaises(TypeError):                  # no default: every caller states the case
            P.hosmer_lemeshow([0.1, 0.2], [0, 1], groups=2)

    def test_hosmer_lemeshow_size_on_true_pd(self):
        """Independent defaults drawn from the true PD: with df = g the rejection rate is near the 5 % level."""
        rng = np.random.default_rng(7)
        rej = []
        for _ in range(400):
            p = np.exp(rng.normal(np.log(0.03), 0.7, size=4000)).clip(1e-4, 0.5)
            y = rng.random(p.size) < p
            rej.append(P.hosmer_lemeshow(p, y, fitted_on_sample=False)["p_value"] < 0.05)
        rate = float(np.mean(rej))
        self.assertLess(abs(rate - 0.05), 4 * (0.05 * 0.95 / 400) ** 0.5 + 0.01)

    def test_binomial_and_jeffreys(self):
        self.assertAlmostEqual(P.binomial_test(3, 100, 0.01), 1 - sum(stats.binom.pmf(k, 100, 0.01) for k in range(3)))
        self.assertEqual(P.binomial_test(0, 100, 0.01), 1.0)
        self.assertAlmostEqual(P.jeffreys_test(3, 100, 0.01), stats.beta.cdf(0.01, 3.5, 97.5), places=14)
        # more defaults -> smaller p-value
        self.assertLess(P.jeffreys_test(8, 100, 0.01), P.jeffreys_test(2, 100, 0.01))

    def test_master_scale(self):
        g = P.grade_of([0.0, 0.0009, 0.001, 0.05, 0.99, 1.0])
        self.assertEqual(g.tolist(), [1, 1, 2, 7, 9, 9])
        rows = P.grade_table([0.0009, 0.0009, 0.05], [0, 1, 0])
        self.assertEqual([(r["grade"], r["n"], r["defaults"]) for r in rows], [(1, 2, 1), (7, 1, 0)])


if __name__ == "__main__":
    unittest.main()
