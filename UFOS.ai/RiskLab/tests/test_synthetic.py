"""Synthetic generators: reproducibility, validation and large-n sanity checks."""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: tests run without installing the package
    sys.path.insert(0, _SRC)

import dataclasses
import unittest

import numpy as np

from risklab.data.synthetic import (SyntheticSeries, garch11, iid_normal, iid_student_t,
                                    regime_switching)

GENERATORS = {
    "normal": lambda seed, n=500: iid_normal(n, 0.0, 0.01, seed),
    "t": lambda seed, n=500: iid_student_t(n, 0.0, 0.01, 5.0, seed),
    "garch_n": lambda seed, n=500: garch11(n, 1e-6, 0.08, 0.9, seed=seed),
    "garch_t": lambda seed, n=500: garch11(n, 1e-6, 0.08, 0.9, innovations="t", nu=6.0, seed=seed),
    "regime": lambda seed, n=500: regime_switching(n, (0.01, 0.03), ((0.98, 0.02), (0.05, 0.95)), seed=seed),
}


class ReproducibilityTests(unittest.TestCase):
    def test_same_seed_identical_bytes(self):
        for name, gen in GENERATORS.items():
            with self.subTest(name):
                a, b = gen(42), gen(42)
                self.assertEqual(a.losses.tobytes(), b.losses.tobytes())
                self.assertEqual(a.sigma_true.tobytes(), b.sigma_true.tobytes())
                self.assertEqual(a.mu_true.tobytes(), b.mu_true.tobytes())
                if a.regime is not None:
                    self.assertEqual(a.regime.tobytes(), b.regime.tobytes())
                self.assertEqual(a.meta, b.meta)

    def test_different_seed_differs(self):
        for name, gen in GENERATORS.items():
            with self.subTest(name):
                self.assertNotEqual(gen(1).losses.tobytes(), gen(2).losses.tobytes())

    def test_generator_object_accepted(self):
        a = iid_normal(100, 0.0, 1.0, np.random.default_rng(7))
        b = iid_normal(100, 0.0, 1.0, 7)
        self.assertEqual(a.losses.tobytes(), b.losses.tobytes())
        self.assertIsNone(a.meta["seed"])
        self.assertEqual(b.meta["seed"], 7)

    def test_shapes_dtypes_and_frozen(self):
        for name, gen in GENERATORS.items():
            with self.subTest(name):
                s = gen(3, n=123)
                self.assertIsInstance(s, SyntheticSeries)
                self.assertEqual(len(s), 123)
                for arr in (s.losses, s.sigma_true, s.mu_true):
                    self.assertEqual(arr.dtype, np.float64)
                    self.assertEqual(arr.shape, (123,))
                    self.assertFalse(arr.flags.writeable)
                self.assertTrue(np.all(s.sigma_true > 0))
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    s.dist = "x"
                with self.assertRaises(ValueError):
                    s.losses[0] = 1.0

    def test_seed_required(self):
        with self.assertRaises(TypeError):
            garch11(10, 1e-6, 0.08, 0.9)
        with self.assertRaises(TypeError):
            regime_switching(10)

    def test_no_quantile_fields(self):
        names = {f.name for f in dataclasses.fields(SyntheticSeries)}
        self.assertEqual(names, {"losses", "sigma_true", "mu_true", "dist", "dist_params", "regime", "meta"})


class ValidationTests(unittest.TestCase):
    def test_bad_arguments(self):
        cases = [
            lambda: iid_normal(0, 0.0, 1.0, 1),
            lambda: iid_normal(10, 0.0, 0.0, 1),
            lambda: iid_normal(10, 0.0, -1.0, 1),
            lambda: iid_normal(10, float("nan"), 1.0, 1),
            lambda: iid_normal(10, 0.0, 1.0, None),
            lambda: iid_normal(10, 0.0, 1.0, -3),
            lambda: iid_normal(10, 0.0, 1.0, 1.5),
            lambda: iid_student_t(10, 0.0, 1.0, 2.0, 1),
            lambda: iid_student_t(10, 0.0, 1.0, 1.5, 1),
            lambda: iid_student_t(10, 0.0, 1.0, None, 1),
            lambda: garch11(10, 1e-6, 0.1, 0.9, seed=1),
            lambda: garch11(10, 1e-6, 0.5, 0.6, seed=1),
            lambda: garch11(10, 0.0, 0.1, 0.8, seed=1),
            lambda: garch11(10, 1e-6, -0.1, 0.8, seed=1),
            lambda: garch11(10, 1e-6, 0.1, 0.0, seed=1),
            lambda: garch11(10, 1e-6, 0.1, 0.8, innovations="t", seed=1),
            lambda: garch11(10, 1e-6, 0.1, 0.8, innovations="t", nu=2.0, seed=1),
            lambda: garch11(10, 1e-6, 0.1, 0.8, innovations="normal", nu=5.0, seed=1),
            lambda: garch11(10, 1e-6, 0.1, 0.8, innovations="laplace", seed=1),
            lambda: garch11(10, 1e-6, 0.1, 0.8, seed=1, burn_in=-1),
            lambda: regime_switching(10, (0.01,), seed=1),
            lambda: regime_switching(10, (0.01, 0.0), seed=1),
            lambda: regime_switching(10, (0.01, 0.02), ((0.9, 0.2), (0.5, 0.5)), seed=1),
            lambda: regime_switching(10, (0.01, 0.02), ((1.1, -0.1), (0.5, 0.5)), seed=1),
            lambda: regime_switching(10, (0.01, 0.02), ((1.0,), (0.5, 0.5)), seed=1),
            lambda: regime_switching(10, (0.01, 0.02), seed=1, initial_state=2),
        ]
        for i, case in enumerate(cases):
            with self.subTest(i=i):
                with self.assertRaises(ValueError):
                    case()


class LargeSampleTests(unittest.TestCase):
    N = 200_000

    def test_normal_moments(self):
        s = iid_normal(self.N, 0.002, 0.01, 11)
        se = 0.01 / np.sqrt(self.N)
        self.assertLess(abs(s.losses.mean() - 0.002), 5 * se)
        self.assertLess(abs(s.losses.std() / 0.01 - 1.0), 0.01)

    def test_scaled_t_moments(self):
        s = iid_student_t(self.N, -0.001, 0.02, 5.0, 12)
        self.assertLess(abs(s.losses.mean() + 0.001), 5 * 0.02 / np.sqrt(self.N))
        self.assertLess(abs(s.losses.std() / 0.02 - 1.0), 0.03)
        self.assertEqual(s.dist_params, {"nu": 5.0})

    def test_garch_unconditional_variance(self):
        omega, a, b = 0.05, 0.08, 0.9
        target = omega / (1 - a - b)
        s = garch11(self.N, omega, a, b, seed=13)
        self.assertLess(abs(s.losses.var() / target - 1.0), 0.1)
        self.assertLess(abs(np.mean(s.sigma_true ** 2) / target - 1.0), 0.1)
        z = s.losses / s.sigma_true
        self.assertLess(abs(z.std() - 1.0), 0.01)

    def test_garch_t_unconditional_variance(self):
        omega, a, b, nu = 0.05, 0.08, 0.9, 8.0
        target = omega / (1 - a - b)
        s = garch11(self.N, omega, a, b, innovations="t", nu=nu, seed=16)
        self.assertEqual(s.dist, "t")
        self.assertEqual(s.dist_params, {"nu": nu})
        # heavier tails than the normal case: wider tolerance on the sample variance
        self.assertLess(abs(s.losses.var() / target - 1.0), 0.15)
        self.assertLess(abs(np.mean(s.sigma_true ** 2) / target - 1.0), 0.15)
        z = s.losses / s.sigma_true
        self.assertLess(abs(z.std() - 1.0), 0.02)

    def test_garch_recursion(self):
        omega, a, b = 0.05, 0.08, 0.9
        s = garch11(1000, omega, a, b, seed=14)
        s2 = s.sigma_true ** 2
        np.testing.assert_allclose(s2[1:], omega + a * s.losses[:-1] ** 2 + b * s2[:-1], rtol=1e-12)

    def test_regime_statistics(self):
        p = ((0.98, 0.02), (0.05, 0.95))
        s = regime_switching(self.N, (0.01, 0.03), p, seed=15)
        r = s.regime
        self.assertEqual(r[0], 0)
        for i in (0, 1):
            frm = r[:-1] == i
            stay = np.mean(r[1:][frm] == i)
            self.assertLess(abs(stay - p[i][i]), 0.01, msg=f"state {i}")
        np.testing.assert_array_equal(s.sigma_true, np.where(r == 0, 0.01, 0.03))
        z = s.losses / s.sigma_true
        self.assertLess(abs(z.std() - 1.0), 0.01)
        self.assertLess(abs(r.mean() - 0.02 / 0.07), 0.03)


if __name__ == "__main__":
    unittest.main()
