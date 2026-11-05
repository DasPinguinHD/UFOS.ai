"""Historical simulation, normal and Student-t models."""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: tests run without installing the package
    sys.path.insert(0, _SRC)

import unittest

import numpy as np
from scipy import optimize, stats

from risklab import analytic as A
from risklab.data import iid_normal, iid_student_t
from risklab.engine import walk_forward
from risklab.models import Forecast, HistoricalSimulation, NormalModel, RiskModel, StudentTModel
from unittest import mock

from risklab.models import FitError
from risklab.models.historical import exceedance_probability, min_window, order_statistic_index
from risklab.models.parametric import NU_BOUNDS, fit_t_mle, t_neg_loglik


class HistoricalSimulationTests(unittest.TestCase):
    def test_order_statistic_convention(self):
        rng = np.random.default_rng(0)
        x = rng.permutation(np.arange(1.0, 11.0))  # 1..10 shuffled
        m = HistoricalSimulation()
        m.fit(x)
        f = m.forecast([0.9])
        # k = ceil(10 * 0.9) = 9 -> the 9th smallest = 9; ES = mean(9, 10)
        self.assertEqual(f.var[0.9], 9.0)
        self.assertEqual(f.es[0.9], 9.5)

    def test_no_interpolation(self):
        x = np.arange(1.0, 21.0)  # n = 20
        m = HistoricalSimulation()
        m.fit(x)
        f = m.forecast([0.92, 0.95])
        self.assertEqual(f.var[0.92], 19.0)  # ceil(18.4) = 19
        self.assertEqual(f.var[0.95], 19.0)  # ceil(19.0) = 19, not 20 (float noise removed)
        self.assertEqual(f.es[0.95], 19.5)

    def test_ties(self):
        x = np.array([0.0, 1.0, 2.0, 5.0, 5.0, 5.0, 5.0, 3.0, 4.0, 6.0])
        m = HistoricalSimulation()
        m.fit(x)
        f = m.forecast([0.9])
        # sorted: 0 1 2 3 4 5 5 5 5 6; k = 9 -> 5; all losses >= 5 are in the tail
        self.assertEqual(f.var[0.9], 5.0)
        self.assertEqual(f.es[0.9], np.mean([5.0, 5.0, 5.0, 5.0, 6.0]))

    def test_window_too_short(self):
        self.assertEqual(min_window(0.99), 100)
        self.assertEqual(min_window(0.975), 40)
        self.assertEqual(order_statistic_index(250, 0.99), 248)
        m = HistoricalSimulation()
        m.fit(np.arange(99.0))
        with self.assertRaises(ValueError) as ctx:
            m.forecast([0.99])
        self.assertIn("at least 100", str(ctx.exception))
        m.fit(np.arange(100.0))
        self.assertEqual(m.forecast([0.99]).var[0.99], 98.0)  # k = 99 -> value 98

    def test_finite_sample_exceedance_probability(self):
        self.assertAlmostEqual(exceedance_probability(250, 0.99), 3 / 251, places=15)  # 1.20 %
        # Monte Carlo: n = 100, alpha = 0.99 -> k = 99, P = 2 / 101 = 1.98 % (not 1 %)
        n, reps = 100, 50_000
        rng = np.random.default_rng(31)
        draws = rng.standard_normal((reps, n + 1))
        var = np.sort(draws[:, :n], axis=1)[:, order_statistic_index(n, 0.99) - 1]
        rate = np.mean(draws[:, n] > var)
        p = exceedance_probability(n, 0.99)
        self.assertAlmostEqual(p, 2 / 101, places=15)
        self.assertLess(abs(rate - p), 4 * np.sqrt(p * (1 - p) / reps))

    def test_bad_method_and_order(self):
        with self.assertRaises(ValueError):
            HistoricalSimulation("kernel")
        with self.assertRaises(RuntimeError):
            HistoricalSimulation().forecast([0.99])


class NormalModelTests(unittest.TestCase):
    def test_formula(self):
        x = iid_normal(300, 0.001, 0.02, 3).losses
        m = NormalModel()
        m.fit(x)
        mu, sd = x.mean(), x.std(ddof=1)
        f = m.forecast([0.99, 0.975])
        self.assertIsInstance(f, Forecast)
        self.assertAlmostEqual(f.var[0.99], mu + sd * stats.norm.ppf(0.99), places=14)
        self.assertAlmostEqual(f.es[0.975], mu + sd * stats.norm.pdf(stats.norm.ppf(0.975)) / 0.025, places=14)

    def test_short_window(self):
        with self.assertRaises(ValueError):
            NormalModel().fit(np.array([1.0]))


class StudentTModelTests(unittest.TestCase):
    def test_gradient(self):
        z = stats.t.rvs(5, size=300, random_state=1)
        for p in ([0.1, -0.2, 6.0], [0.0, 0.3, 3.0]):
            err = optimize.check_grad(lambda q: t_neg_loglik(q, z)[0], lambda q: t_neg_loglik(q, z)[1], p)
            self.assertLess(err, 1e-4)
        self.assertAlmostEqual(t_neg_loglik([0.1, -0.2, 6.0], z)[0],
                               stats.t.nnlf((6.0, 0.1, np.exp(-0.2)), z), places=8)

    def test_mle_matches_scipy_fit(self):
        x = iid_student_t(5000, 0.001, 0.01, 5.0, 4).losses
        loc, scale, nu = fit_t_mle(x)
        nu_s, loc_s, scale_s = stats.t.fit(x)
        self.assertAlmostEqual(nu, nu_s, delta=1e-3 * nu_s)
        self.assertAlmostEqual(loc, loc_s, delta=1e-6)
        self.assertAlmostEqual(scale, scale_s, delta=1e-5 * scale_s)
        # the true parameters: nu 5, scale = sd * sqrt(3 / 5)
        self.assertLess(abs(nu - 5.0), 1.0)

    def test_nu_bounds(self):
        x = iid_normal(2000, 0.0, 0.01, 5).losses  # normal data: nu runs to the upper bound
        _, _, nu = fit_t_mle(x)
        self.assertGreaterEqual(nu, NU_BOUNDS[0])
        self.assertLessEqual(nu, NU_BOUNDS[1])
        heavy = stats.t.rvs(1.5, size=2000, random_state=6)  # infinite variance: nu hits the lower bound
        self.assertAlmostEqual(fit_t_mle(heavy)[2], NU_BOUNDS[0], places=6)

    def test_fixed_nu_and_forecast(self):
        x = iid_student_t(400, 0.0, 0.01, 4.0, 7).losses
        m = StudentTModel(nu=4.0)
        m.fit(x)
        self.assertEqual(m.nu_hat, 4.0)
        f = m.forecast([0.99])
        self.assertAlmostEqual(f.var[0.99], m.loc + m.scale * stats.t.ppf(0.99, 4.0), places=12)
        self.assertAlmostEqual(f.es[0.99], float(A.es_t(0.99, 4.0, m.loc, m.scale)), places=12)
        with self.assertRaises(ValueError):
            StudentTModel(nu=2.0)
        with self.assertRaises(ValueError):
            StudentTModel().fit(np.zeros(10))

    def test_non_convergence_raises(self):
        x = iid_student_t(300, 0.0, 0.01, 5.0, 9).losses
        failed = optimize.OptimizeResult(x=np.array([0.0, 0.0, 5.0]), fun=1.0, success=False,
                                         message="ABNORMAL_TERMINATION_IN_LNSRCH")
        with mock.patch("risklab.models.parametric.optimize.minimize", return_value=failed):
            with self.assertRaises(FitError) as ctx:
                fit_t_mle(x)
            self.assertIn("did not converge", str(ctx.exception))
        failed_fixed = optimize.OptimizeResult(x=np.array([0.0, 0.0]), fun=1.0, success=False, message="ABNORMAL")
        with mock.patch("risklab.models.parametric.optimize.minimize", return_value=failed_fixed):
            with self.assertRaises(FitError):
                StudentTModel(nu=5.0).fit(x)

    def test_protocol(self):
        for m in (HistoricalSimulation(), NormalModel(), StudentTModel()):
            self.assertIsInstance(m, RiskModel)


class EngineEndToEndTests(unittest.TestCase):
    def test_normal_model_coverage(self):
        alpha, window, t = 0.95, 250, 4000
        s = iid_normal(window + t, 0.0, 0.01, 8)
        f = walk_forward(s.losses, NormalModel, window, [alpha])
        rate = f.n_exceed(alpha) / len(f)
        p = 1 - alpha
        sd = np.sqrt(p * (1 - p) / t)
        # 4 binomial standard deviations (estimation error adds a little extra spread)
        self.assertLess(abs(rate - p), 4 * sd, msg=f"rate={rate}")
        self.assertIsNotNone(f.es)

    def test_normal_model_coverage_99(self):
        alpha, window, t = 0.99, 250, 20_000
        s = iid_normal(window + t, 0.0, 0.01, 10)
        f = walk_forward(s.losses, NormalModel, window, [alpha])
        rate = f.n_exceed(alpha) / len(f)
        p = 1 - alpha
        # 4 binomial standard deviations; estimation error with a 250-day window adds about +0.1 point
        self.assertLess(abs(rate - p), 4 * np.sqrt(p * (1 - p) / t), msg=f"rate={rate}")


if __name__ == "__main__":
    unittest.main()
