"""True conditional VaR / ES of the synthetic generators against Monte-Carlo samples.

Tolerances: the exceedance rate against the true VaR must lie within 4 binomial
standard deviations of 1 - alpha; the mean standardised loss beyond the true
VaR must match the standardised ES within 3 % (n = 400 000, about 4 000 tail
observations at alpha = 0.99).
"""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: tests run without installing the package
    sys.path.insert(0, _SRC)

import unittest

import numpy as np
from scipy import stats

from risklab.data import garch11, iid_normal, iid_student_t, regime_switching, true_var_es

N = 400_000
ALPHAS = (0.975, 0.99)


def _cases():
    return {
        "iid_normal": iid_normal(N, 0.001, 0.01, 21),
        "iid_t": iid_student_t(N, -0.001, 0.02, 5.0, 22),
        "garch_normal": garch11(N, 0.05, 0.08, 0.9, seed=23),
        "garch_t": garch11(N, 0.05, 0.08, 0.9, innovations="t", nu=6.0, seed=24),
        "regime": regime_switching(N, (0.01, 0.03), ((0.98, 0.02), (0.05, 0.95)), seed=25),
    }


class TruthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = _cases()

    def test_exceedance_rate_and_tail_mean(self):
        for name, s in self.cases.items():
            truth = true_var_es(s, ALPHAS)
            for a in ALPHAS:
                with self.subTest(name=name, alpha=a):
                    exc = s.losses > truth.var[a]
                    p = 1 - a
                    self.assertLess(abs(exc.mean() - p), 4 * np.sqrt(p * (1 - p) / N))
                    # ES: compare standardised tails so that days with different sigma are comparable
                    z = (s.losses - s.mu_true) / s.sigma_true
                    es_std = (truth.es[a] - s.mu_true) / s.sigma_true
                    self.assertLess(abs(z[exc].mean() / es_std[0] - 1), 0.03)

    def test_iid_quantile(self):
        s = self.cases["iid_normal"]
        truth = true_var_es(s, [0.99])
        self.assertTrue(np.all(truth.var[0.99] == truth.var[0.99][0]))
        self.assertLess(abs(np.quantile(s.losses, 0.99) / truth.var[0.99][0] - 1), 0.01)
        self.assertAlmostEqual(truth.var[0.99][0], 0.001 + 0.01 * 2.3263478740408408, places=12)

    def test_against_scipy_distributions(self):
        # independent of risklab.analytic: scipy.stats quantiles and numerical tail expectations
        s = self.cases["iid_t"]
        scale = 0.02 * np.sqrt(3.0 / 5.0)  # unit-variance t5 scaled to sd 0.02
        truth = true_var_es(s, [0.99])
        self.assertAlmostEqual(truth.var[0.99][0], stats.t.ppf(0.99, 5, loc=-0.001, scale=scale), places=12)
        es = stats.t.expect(lambda x: x, args=(5,), loc=-0.001, scale=scale,
                            lb=stats.t.ppf(0.99, 5, loc=-0.001, scale=scale), conditional=True)
        self.assertAlmostEqual(truth.es[0.99][0], es, places=8)
        g = self.cases["garch_t"]
        truth = true_var_es(g, [0.99])
        sd_scale = np.sqrt(4.0 / 6.0)
        for i in (0, 1000, 99_999):
            sc = g.sigma_true[i] * sd_scale
            q = stats.t.ppf(0.99, 6, scale=sc)
            self.assertAlmostEqual(truth.var[0.99][i] / q, 1.0, places=10)
            e = stats.t.expect(lambda x: x, args=(6,), scale=sc, lb=q, conditional=True)
            self.assertAlmostEqual(truth.es[0.99][i] / e, 1.0, places=7)
        r = self.cases["regime"]
        truth = true_var_es(r, [0.975])
        np.testing.assert_allclose(truth.var[0.975], stats.norm.ppf(0.975, scale=np.where(r.regime == 0, 0.01, 0.03)),
                                   rtol=1e-12)

    def test_read_only(self):
        truth = true_var_es(self.cases["iid_normal"], [0.99])
        with self.assertRaises(ValueError):
            truth.var[0.99][0] = 0.0


if __name__ == "__main__":
    unittest.main()
