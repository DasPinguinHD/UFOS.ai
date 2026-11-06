"""Sensitivity tools: Sobol indices against the analytic Ishigami and linear-model values (tolerance from the MC
error), tornado ordering on a linear model, two-way tables and their explicit invalid-cell rule."""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: tests run without installing the package
    sys.path.insert(0, _SRC)

import math
import unittest

import numpy as np

from risklab import sensitivity as S


def ishigami_truth(a=7.0, b=0.1):
    """Analytic first / total indices of the Ishigami function (written out independently of the study)."""
    pi = math.pi
    v1 = 0.5 * (1 + b * pi ** 4 / 5) ** 2
    v2 = a ** 2 / 8
    v13 = b ** 2 * pi ** 8 * 8 / 225          # = b^2 pi^8 (1/18 - 1/50)
    v = v1 + v2 + v13
    return np.array([v1 / v, v2 / v, 0.0]), np.array([(v1 + v13) / v, v2 / v, v13 / v])


class SobolTests(unittest.TestCase):
    def test_ishigami_against_analytic(self):
        first, total = ishigami_truth()
        # the total variance in closed form, as a check of the formulas above
        self.assertAlmostEqual(7 ** 2 / 8 + 0.1 * math.pi ** 4 / 5 + 0.01 * math.pi ** 8 / 18 + 0.5,
                               0.5 * (1 + 0.1 * math.pi ** 4 / 5) ** 2 + 49 / 8 + 0.01 * math.pi ** 8 * 8 / 225,
                               places=10)
        res = S.sobol_indices(S.ishigami, S.ISHIGAMI_INPUTS, 2 ** 14, seed=2026, n_boot=300)
        self.assertEqual(res.evaluations, 2 ** 14 * 5)
        for i in range(3):
            self.assertLess(abs(res.first[i] - first[i]), 4 * res.first_se[i] + 0.005, res.names[i])
            self.assertLess(abs(res.total[i] - total[i]), 4 * res.total_se[i] + 0.005, res.names[i])
            self.assertLess(res.first_se[i], 0.03)
            self.assertLess(res.total_se[i], 0.03)

    def test_linear_model(self):
        # y = 1 x1 + 2 x2 + 3 x3 with x ~ U(0, 1): S_i = S_Ti = c_i^2 / sum c^2 (additive)
        c = np.array([1.0, 2.0, 3.0])
        dists = {f"x{i}": S.Uniform(0.0, 1.0) for i in range(3)}
        f = lambda p: sum(c[i] * p[f"x{i}"] for i in range(3)) + 100.0   # noqa: E731  (large level: centring)
        res = S.sobol_indices(f, dists, 4096, seed=3, n_boot=200)
        want = c ** 2 / np.sum(c ** 2)
        for i in range(3):
            self.assertLess(abs(res.first[i] - want[i]), 4 * res.first_se[i] + 0.003)
            self.assertLess(abs(res.total[i] - want[i]), 4 * res.total_se[i] + 0.003)
        self.assertLess(np.max(res.first_se), 0.03)          # centring keeps the level of 100 harmless

    def test_reproducible_and_rowwise(self):
        dists = {"a": S.Uniform(0.0, 1.0), "b": S.Normal(0.0, 1.0)}
        fv = lambda p: p["a"] * np.exp(p["b"])     # noqa: E731
        fs = lambda p: p["a"] * math.exp(p["b"])   # noqa: E731
        r1 = S.sobol_indices(fv, dists, 64, seed=9, n_boot=20)
        r2 = S.sobol_indices(S.rowwise(fs), dists, 64, seed=9, n_boot=20)
        np.testing.assert_allclose(r1.first, r2.first, rtol=1e-12)
        np.testing.assert_allclose(r1.total_se, r2.total_se, rtol=1e-12)
        r3 = S.sobol_indices(fv, dists, 64, seed=9, n_boot=20)
        np.testing.assert_array_equal(r1.total, r3.total)

    def test_discrete_uniform(self):
        d = S.DiscreteUniform(7, 13)
        u = (np.arange(70000) + 0.5) / 70000
        x = d.ppf(u)
        self.assertEqual(sorted(set(x.tolist())), [7.0, 8.0, 9.0, 10.0, 11.0, 12.0, 13.0])
        counts = np.array([np.sum(x == k) for k in range(7, 14)])
        self.assertTrue(np.all(counts == 10000))                  # equally likely
        self.assertEqual(d.ppf(np.array([0.0, 0.999999999]))[[0, 1]].tolist(), [7.0, 13.0])
        with self.assertRaises(ValueError):
            S.DiscreteUniform(3, 3)
        with self.assertRaises(ValueError):
            S.DiscreteUniform(1.5, 3)

    def test_errors(self):
        with self.assertRaises(ValueError):
            S.sobol_indices(lambda p: np.zeros(8), {"a": S.Uniform(0, 1)}, 8, seed=1)      # zero variance
        with self.assertRaises(ValueError):
            S.sobol_indices(lambda p: np.ones(3), {"a": S.Uniform(0, 1)}, 8, seed=1)       # wrong shape
        with self.assertRaises(ValueError):
            S.sobol_indices(lambda p: p["a"] + np.inf, {"a": S.Uniform(0, 1)}, 8, seed=1)  # non-finite
        with self.assertRaises(ValueError):
            S.Uniform(1.0, 1.0)
        with self.assertRaises(ValueError):
            S.Normal(0.0, 0.0)


class TornadoTests(unittest.TestCase):
    def test_linear_ordering(self):
        f = lambda p: 2.0 * p["a"] - 5.0 * p["b"] + 0.5 * p["c"]   # noqa: E731
        base = {"a": 1.0, "b": 1.0, "c": 1.0}
        ranges = {"a": (0.0, 2.0), "b": (0.5, 1.5), "c": (-4.0, 4.0)}   # swings 4, 5, 4
        bars = S.tornado(f, base, ranges)
        self.assertEqual([b.name for b in bars], ["b", "a", "c"])     # ties keep the input order
        self.assertEqual([b.swing for b in bars], [5.0, 4.0, 4.0])
        self.assertEqual(bars[0].y_low, f({**base, "b": 0.5}))
        self.assertEqual(bars[0].base_y, f(base))
        with self.assertRaises(KeyError):
            S.tornado(f, base, {"d": (0, 1)})


class TwoWayTests(unittest.TestCase):
    def test_grid_and_invalid_cells(self):
        def f(p):
            if p["x"] <= p["y"]:
                raise ArithmeticError("invalid")
            return p["x"] * 10 + p["y"]
        t = S.two_way(f, {"x": 0.0, "y": 0.0}, "x", [1.0, 2.0, 3.0], "y", [0.0, 2.0], invalid=(ArithmeticError,))
        self.assertEqual(t.z.shape, (2, 3))
        np.testing.assert_array_equal(t.z[0], [10.0, 20.0, 30.0])
        self.assertTrue(np.isnan(t.z[1, 0]) and np.isnan(t.z[1, 1]))
        self.assertEqual(t.z[1, 2], 32.0)
        self.assertEqual(t.invalid, 2)
        with self.assertRaises(ArithmeticError):                      # not opted in: propagates
            S.two_way(f, {"x": 0.0, "y": 0.0}, "x", [1.0], "y", [2.0])


if __name__ == "__main__":
    unittest.main()
