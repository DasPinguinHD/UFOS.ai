"""GARCH(1,1) MLE, EWMA and filtered HS."""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: tests run without installing the package
    sys.path.insert(0, _SRC)

import unittest
from unittest import mock

import numpy as np
from scipy import optimize, stats

from risklab.data import garch11, iid_normal
from risklab.dists import Empirical, LocScale
from risklab.engine import walk_forward
from risklab.models import EWMAModel, FilteredHS, FitError, GarchTModel, HistoricalSimulation, fit_garch11
from risklab.models.garch import (PERSISTENCE_BOUNDS, GarchParams, garch_neg_loglik, variance_path)


def _hand_variance(p, x, s0):
    h = [s0]
    for v in x:
        h.append(p.omega + p.alpha * (v - p.mu) ** 2 + p.beta * h[-1])
    return np.array(h)


class GarchLikelihoodTests(unittest.TestCase):
    def test_gradient(self):
        s = garch11(600, 2e-6, 0.08, 0.9, innovations="t", nu=6.0, seed=1)
        x = (s.losses - s.losses.mean()) / s.losses.std()
        for theta, dist in (([0.05, -3.0, 0.9, 0.1, 7.0], "t"), ([0.05, -3.0, 0.9, 0.1], "normal"),
                            ([-0.1, -1.0, 0.5, 0.6, 3.0], "t")):
            f = lambda q: garch_neg_loglik(q, x, dist)[0]  # noqa: E731
            num = optimize.approx_fprime(np.array(theta), f, 1e-7)
            ana = garch_neg_loglik(np.array(theta), x, dist)[1]
            np.testing.assert_allclose(ana, num, rtol=1e-4, atol=2e-3)

    def test_likelihood_against_scipy(self):
        # independent: the sum of scipy.stats log-densities along a hand-computed variance path
        rng = np.random.default_rng(2)
        x = rng.standard_normal(300)
        mu, w, p, g, nu = 0.1, -2.0, 0.9, 0.15, 6.0
        omega, a, b = np.exp(w), p * g, p * (1 - g)
        h = _hand_variance(GarchParams(mu, omega, a, b, nu), x, 1.0)[:-1]
        ll_t = np.sum(stats.t.logpdf(x - mu, nu, scale=np.sqrt(h * (nu - 2) / nu)))
        ll_n = np.sum(stats.norm.logpdf(x - mu, scale=np.sqrt(h)))
        self.assertAlmostEqual(garch_neg_loglik([mu, w, p, g, nu], x, "t")[0], -ll_t, places=8)
        self.assertAlmostEqual(garch_neg_loglik([mu, w, p, g], x, "normal")[0], -ll_n, places=8)

    def test_variance_path(self):
        p = GarchParams(0.001, 1e-6, 0.1, 0.85, None)
        x = np.array([0.01, -0.02, 0.005, 0.0, 0.03])
        np.testing.assert_allclose(variance_path(p, x, 2e-4), _hand_variance(p, x, 2e-4), rtol=1e-13)


class GarchFitTests(unittest.TestCase):
    def test_recovers_true_parameters(self):
        # n = 20 000; tolerances: alpha, beta +-0.015, nu +-1.0, omega within 35 %, mu within 3e-4
        s = garch11(20_000, 2e-6, 0.08, 0.90, innovations="t", nu=6.0, seed=101)
        p = fit_garch11(s.losses, "t")
        self.assertLess(abs(p.alpha - 0.08), 0.015)
        self.assertLess(abs(p.beta - 0.90), 0.015)
        self.assertLess(abs(p.nu - 6.0), 1.0)
        self.assertLess(abs(p.omega / 2e-6 - 1), 0.35)
        self.assertLess(abs(p.mu), 3e-4)
        q = fit_garch11(garch11(20_000, 2e-6, 0.08, 0.90, seed=202).losses, "normal")
        self.assertIsNone(q.nu)
        self.assertLess(abs(q.alpha - 0.08), 0.015)
        self.assertLess(abs(q.beta - 0.90), 0.015)

    def test_constraints_hold(self):
        # integrated / explosive-looking data and white noise: the fit stays inside the constraints
        rng = np.random.default_rng(3)
        cases = [rng.standard_normal(500) * 0.01,
                 np.cumsum(rng.standard_normal(500)) * 0.001,  # strongly persistent levels
                 garch11(500, 1e-7, 0.12, 0.879, seed=4).losses]
        for x in cases:
            p = fit_garch11(x, "t")
            self.assertGreater(p.omega, 0)
            self.assertGreaterEqual(p.alpha, 0)
            self.assertGreaterEqual(p.beta, 0)
            self.assertLessEqual(p.alpha + p.beta, PERSISTENCE_BOUNDS[1] + 1e-12)
            self.assertLess(p.alpha + p.beta, 1.0)
            self.assertTrue(2.05 - 1e-9 <= p.nu <= 200 + 1e-9)

    def test_fit_error_on_optimiser_failure(self):
        x = garch11(300, 2e-6, 0.08, 0.9, seed=5).losses
        bad = optimize.OptimizeResult(x=np.array([0.0, -3.0, 0.9, 0.1, 6.0]), fun=1.0, success=False,
                                      message="ABNORMAL_TERMINATION_IN_LNSRCH")
        with mock.patch("risklab.models.garch.optimize.minimize", return_value=bad):
            with self.assertRaises(FitError):
                fit_garch11(x, "t")
            with self.assertRaises(FitError):
                GarchTModel().fit(x)

    def test_validation(self):
        with self.assertRaises(ValueError):
            fit_garch11(np.zeros(100))
        with self.assertRaises(ValueError):
            fit_garch11(np.ones(10) * 0.01)
        with self.assertRaises(ValueError):
            fit_garch11(np.random.default_rng(0).standard_normal(100), "laplace")
        with self.assertRaises(ValueError):
            GarchTModel(refit_every=0)

    def test_model_update_and_forecast(self):
        s = garch11(400, 2e-6, 0.08, 0.9, innovations="t", nu=6.0, seed=6)
        m = GarchTModel()
        m.fit(s.losses[:300])
        hand = _hand_variance(m.params, s.losses[:310], float(np.var(s.losses[:300])))
        self.assertAlmostEqual(m.sigma2_next / hand[300], 1.0, places=12)
        for i in range(300, 310):
            m.update(s.losses[i])
        self.assertAlmostEqual(m.sigma2_next / hand[310], 1.0, places=12)
        f = m.forecast([0.99])
        p, sd = m.params, np.sqrt(m.sigma2_next)
        scale = sd * np.sqrt((p.nu - 2) / p.nu)
        self.assertAlmostEqual(f.var[0.99], stats.t.ppf(0.99, p.nu, loc=p.mu, scale=scale), places=12)
        es = stats.t.expect(lambda v: v, args=(p.nu,), loc=p.mu, scale=scale,
                            lb=stats.t.ppf(0.99, p.nu, loc=p.mu, scale=scale), conditional=True)
        self.assertAlmostEqual(f.es[0.99] / es, 1.0, places=6)
        self.assertIsInstance(f.dist, LocScale)
        self.assertAlmostEqual(float(f.dist.ppf(0.99)), f.var[0.99], places=12)


class EWMATests(unittest.TestCase):
    def test_recursion_by_hand(self):
        x = np.array([0.01, -0.02, 0.015, 0.0, -0.01, 0.03])
        m = EWMAModel(lam=0.9, init=3)
        m.fit(x)
        s2 = np.mean(x[:3] ** 2)  # zero-mean (uncentred) initial variance
        s2 = 0.9 * s2 + 0.1 * 0.0 ** 2
        s2 = 0.9 * s2 + 0.1 * 0.01 ** 2
        s2 = 0.9 * s2 + 0.1 * 0.03 ** 2
        self.assertAlmostEqual(m.sigma2_next, s2, places=17)
        m.update(-0.02)
        s2 = 0.9 * s2 + 0.1 * 0.0004
        self.assertAlmostEqual(m.sigma2_next, s2, places=17)
        f = m.forecast([0.99])
        self.assertAlmostEqual(f.var[0.99], np.sqrt(s2) * stats.norm.ppf(0.99), places=14)
        t = EWMAModel(lam=0.9, init=3, dist="t", nu=5.0)
        t.fit(x)
        scale = np.sqrt(t.sigma2_next * 3 / 5)
        self.assertAlmostEqual(t.forecast([0.975]).var[0.975], stats.t.ppf(0.975, 5, scale=scale), places=14)

    def test_validation(self):
        for kw in ({"lam": 1.0}, {"lam": 0.0}, {"dist": "t"}, {"dist": "t", "nu": 2.0}, {"nu": 5.0},
                   {"init": 0}, {"dist": "x"}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                EWMAModel(**kw)
        with self.assertRaises(ValueError):
            EWMAModel(init=30).fit(np.zeros(20))


class FilteredHSTests(unittest.TestCase):
    def test_close_to_hs_on_iid_data(self):
        x = iid_normal(2000, 0.0, 0.01, 7).losses
        f, h = FilteredHS(), HistoricalSimulation()
        f.fit(x)
        h.fit(x)
        for a in (0.975, 0.99):
            rf, rh = f.forecast([a]), h.forecast([a])
            self.assertLess(abs(rf.var[a] / rh.var[a] - 1), 0.08)
            self.assertLess(abs(rf.es[a] / rh.es[a] - 1), 0.08)

    def test_residual_standardisation(self):
        s = garch11(500, 2e-6, 0.08, 0.9, seed=8)
        f = FilteredHS()
        f.fit(s.losses)
        p = f.params
        h = _hand_variance(p, s.losses, float(np.var(s.losses)))
        z = np.sort((s.losses - p.mu) / np.sqrt(h[:-1]))
        np.testing.assert_allclose(f.z, z, rtol=1e-12)
        fc = f.forecast([0.99])
        k = int(np.ceil(500 * 0.99))
        self.assertAlmostEqual(fc.var[0.99], p.mu + np.sqrt(h[-1]) * z[k - 1], places=14)
        self.assertAlmostEqual(fc.es[0.99], p.mu + np.sqrt(h[-1]) * z[z >= z[k - 1]].mean(), places=14)
        self.assertIsInstance(fc.dist, Empirical)
        self.assertAlmostEqual(float(fc.dist.ppf(0.99)), fc.var[0.99], places=14)

    def test_in_engine(self):
        s = garch11(700, 2e-6, 0.08, 0.9, seed=9)
        frame = walk_forward(s.losses, FilteredHS, 500, [0.99])
        self.assertEqual(frame.meta["refit_every"], 20)
        self.assertEqual(len(frame.dists), 200)


if __name__ == "__main__":
    unittest.main()
