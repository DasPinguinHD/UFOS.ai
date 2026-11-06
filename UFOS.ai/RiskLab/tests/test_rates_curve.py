"""Yield curves: Nelson-Siegel(-Svensson) limits and forwards, compounding, par yields, fitting, synthetic factors."""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: tests run without installing the package
    sys.path.insert(0, _SRC)

import math
import unittest

import numpy as np

from risklab.models import FitError
from risklab.rates import curve as C


class NelsonSiegelTests(unittest.TestCase):
    def setUp(self):
        self.ns = C.NelsonSiegel(0.03, -0.015, 0.01, 2.0)
        self.nss = C.NelsonSiegel(0.03, -0.015, 0.01, 2.0, -0.008, 8.0)

    def test_limits(self):
        for c in (self.ns, self.nss):
            self.assertAlmostEqual(float(c.zero(0.0)), c.b0 + c.b1, places=12)
            self.assertAlmostEqual(float(c.forward(0.0)), c.b0 + c.b1, places=12)
            self.assertAlmostEqual(float(c.zero(1e4)), c.b0, places=4)

    def test_hand_value(self):
        t, tau = 3.0, 2.0
        x = t / tau
        a = (1 - math.exp(-x)) / x
        want = 0.03 - 0.015 * a + 0.01 * (a - math.exp(-x))
        self.assertAlmostEqual(float(self.ns.zero(t)), want, places=14)

    def test_forward_is_derivative_of_z_times_t(self):
        t = np.linspace(0.1, 30, 50)
        for c in (self.ns, self.nss):
            h = 1e-6
            num = (c.zero(t + h) * (t + h) - c.zero(t - h) * (t - h)) / (2 * h)
            np.testing.assert_allclose(c.forward(t), num, atol=1e-8)
            np.testing.assert_allclose(C.as_zero_curve(c).forward(t), c.forward(t), atol=1e-7)

    def test_discount_and_compounding(self):
        t = np.array([0.5, 1, 7, 30])
        np.testing.assert_allclose(self.ns.discount(t), np.exp(-self.ns.zero(t) * t))
        z = np.array([0.01, 0.03, -0.005])
        for m in (1, 2, 12, None):
            np.testing.assert_allclose(C.from_compounded(C.to_compounded(z, m), m), z, atol=1e-15)
        self.assertAlmostEqual(float(C.to_compounded(0.03, 1)), math.exp(0.03) - 1, places=15)

    def test_par_yield_flat_curve(self):
        flat = C.NelsonSiegel(0.03, 0.0, 0.0, 1.0)
        np.testing.assert_allclose(C.par_yield(flat, [1, 5, 10, 30]), math.exp(0.03) - 1, atol=1e-13)
        with self.assertRaises(ValueError):
            C.par_yield(flat, [0])


class FitTests(unittest.TestCase):
    def test_ns_recovers_parameters_from_zero_rates(self):
        true = C.NelsonSiegel(0.025, -0.012, 0.018, 1.7)
        t = np.array([0.25, 0.5, 1, 2, 3, 5, 7, 10, 15, 20, 30])
        fit = C.fit_curve(t, true.zero(t), "zero", "ns")
        for k in ("b0", "b1", "b2", "tau"):
            self.assertAlmostEqual(fit.curve.params()[k], true.params()[k], places=5)
        self.assertLess(fit.rmse, 1e-9)

    def test_nss_fits_par_yields(self):
        true = C.NelsonSiegel(0.03, -0.015, 0.01, 2.0, -0.008, 8.0)
        t = np.arange(1, 31)
        y = C.par_yield(true, t)
        fit = C.fit_curve(t, y, "par", "nss")
        self.assertLess(fit.rmse, 1e-6)
        np.testing.assert_allclose(fit.curve.zero(t), true.zero(t), atol=2e-5)

    def test_fit_failure_is_explicit(self):
        t = np.array([1, 2, 3, 4, 5, 6, 7.0])
        zig = np.array([0.05, -0.05, 0.05, -0.05, 0.05, -0.05, 0.05])
        with self.assertRaises(FitError):
            C.fit_curve(t, zig, "zero", "ns")
        with self.assertRaises(ValueError):
            C.fit_curve([1, 2], [0.01, 0.02])

    def test_user_parameters_in_percent(self):
        u = C.UserCurveParameters(3.0, -1.5, 1.0, 2.0, rates_in_percent=True, source="test")
        self.assertAlmostEqual(float(u.curve().zero(0.0)), 0.015, places=12)


class SyntheticCurveTests(unittest.TestCase):
    def test_dns_deterministic_and_mean_reverting(self):
        a, b = C.dns_factors(2000, seed=3), C.dns_factors(2000, seed=3)
        np.testing.assert_array_equal(a, b)
        self.assertEqual(a.shape, (2000, 3))
        np.testing.assert_allclose(a[0], C.DNS_DEFAULTS["mu"])
        np.testing.assert_allclose(a.mean(axis=0), C.DNS_DEFAULTS["mu"], atol=0.01)
        c = C.dns_curve(a[-1])
        self.assertAlmostEqual(float(c.zero(0.0)), a[-1][0] + a[-1][1], places=12)
        with self.assertRaises(ValueError):
            C.dns_factors(5, seed=1, phi=(1.0, 0.5, 0.5))


if __name__ == "__main__":
    unittest.main()
