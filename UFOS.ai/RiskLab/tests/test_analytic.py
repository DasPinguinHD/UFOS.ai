"""Closed-form VaR / ES against reference values and numerical integration."""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: tests run without installing the package
    sys.path.insert(0, _SRC)

import unittest

import numpy as np
from scipy import integrate, stats

from risklab import analytic as A


class NormalTests(unittest.TestCase):
    def test_reference_values(self):
        self.assertAlmostEqual(float(A.var_normal(0.99)), 2.326348, places=6)
        self.assertAlmostEqual(float(A.es_normal(0.99)), 2.665214, places=6)
        self.assertAlmostEqual(float(A.es_normal(0.975)), 2.337803, places=6)

    def test_location_scale_and_broadcast(self):
        mu, sigma = np.array([0.0, 0.001]), np.array([0.01, 0.02])
        np.testing.assert_allclose(A.var_normal(0.99, mu, sigma), mu + sigma * 2.3263478740408408, rtol=1e-12)
        np.testing.assert_allclose(A.es_normal(0.99, mu, sigma), mu + sigma * 2.6652142203458076, rtol=1e-12)

    def test_es_by_integration(self):
        for a in (0.95, 0.975, 0.99):
            q = stats.norm.ppf(a)
            tail, _ = integrate.quad(lambda x: x * stats.norm.pdf(x), q, np.inf)
            self.assertAlmostEqual(float(A.es_normal(a)), tail / (1 - a), places=8)


class StudentTTests(unittest.TestCase):
    def test_var_by_integration(self):
        for nu in (3.0, 5.0, 12.0):
            for a in (0.975, 0.99):
                v = float(A.var_t(a, nu))
                cdf, _ = integrate.quad(lambda x: stats.t.pdf(x, nu), -np.inf, v)
                self.assertAlmostEqual(cdf, a, places=9, msg=f"nu={nu} a={a}")

    def test_es_by_integration(self):
        for nu in (3.0, 5.0, 12.0):
            for a in (0.975, 0.99):
                v = float(A.var_t(a, nu))
                tail, _ = integrate.quad(lambda x: x * stats.t.pdf(x, nu), v, np.inf)
                self.assertAlmostEqual(float(A.es_t(a, nu)), tail / (1 - a), places=7, msg=f"nu={nu} a={a}")

    def test_location_scale(self):
        self.assertAlmostEqual(float(A.var_t(0.99, 5, 0.1, 2.0)), 0.1 + 2.0 * stats.t.ppf(0.99, 5), places=12)
        self.assertAlmostEqual(float(A.es_t(0.99, 5, 0.1, 2.0)), 0.1 + 2.0 * float(A.es_t(0.99, 5)), places=12)

    def test_standardised_t_has_unit_variance(self):
        nu, sigma = 5.0, 0.01
        scale = float(A.t_scale_from_sd(sigma, nu))
        self.assertAlmostEqual(stats.t.std(nu, scale=scale), sigma, places=14)
        self.assertAlmostEqual(float(A.var_t_std(0.99, nu, 0.0, sigma)), stats.t.ppf(0.99, nu, scale=scale), places=12)
        tail, _ = integrate.quad(lambda x: x * stats.t.pdf(x, nu, scale=scale),
                                 stats.t.ppf(0.99, nu, scale=scale), np.inf)
        self.assertAlmostEqual(float(A.es_t_std(0.99, nu, 0.0, sigma)), tail / 0.01, places=9)

    def test_t_tends_to_normal(self):
        self.assertAlmostEqual(float(A.var_t(0.99, 1e7)), float(A.var_normal(0.99)), places=5)
        self.assertAlmostEqual(float(A.es_t(0.99, 1e7)), float(A.es_normal(0.99)), places=5)

    def test_validation(self):
        for call in (lambda: A.var_normal(1.0), lambda: A.es_normal(0.0), lambda: A.var_normal(0.99, 0, -1),
                     lambda: A.es_t(0.99, 1.0), lambda: A.var_t(0.99, 0.0), lambda: A.var_t_std(0.99, 2.0),
                     lambda: A.es_t(0.99, 5, 0, float("nan"))):
            with self.assertRaises(ValueError):
                call()


if __name__ == "__main__":
    unittest.main()
