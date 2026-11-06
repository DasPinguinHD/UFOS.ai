"""Validation statistics: DSR (paper example, hand computation), PBO / CSCV, Newey-West t (hand code)."""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: tests run without installing the package
    sys.path.insert(0, _SRC)

import math
import unittest
from itertools import combinations

import numpy as np
from scipy import stats as st

from risklab.validation import stats


class DeflatedSharpeTests(unittest.TestCase):
    def test_paper_worked_example(self):
        """Bailey and Lopez de Prado (2014), numerical example: N = 100 trials, V = 1/2 (annualised SR),
        best SR 2.5 a year over 5 years of daily data (T = 1250, 250 days a year), skewness -3,
        kurtosis 10 -> SR0 = 0.1132 per day and DSR = 0.9004."""
        sr0 = stats.expected_max_sharpe(100, 0.5) / math.sqrt(250)
        self.assertAlmostEqual(sr0, 0.1132, places=4)
        self.assertAlmostEqual(stats.psr(2.5 / math.sqrt(250), sr0, 1250, -3.0, 10.0), 0.9004, places=4)

    def test_hand_computation_on_returns(self):
        rng = np.random.default_rng(7)
        r = rng.standard_t(5, 1500) * 0.01 + 0.0006
        trials = [0.3, 0.9, -0.2, 0.5, 1.1, 0.0]
        out = stats.deflated_sharpe(r, 252, 7, trials)
        m, sd = r.mean(), r.std(ddof=1)
        d = r - m
        s0 = math.sqrt(np.mean(d ** 2))
        g3, g4 = np.mean(d ** 3) / s0 ** 3, np.mean(d ** 4) / s0 ** 4
        v = max(np.var(trials, ddof=1), 252 / 1500)
        gam = 0.5772156649015329
        sr0 = math.sqrt(v) * ((1 - gam) * st.norm.ppf(1 - 1 / 7) + gam * st.norm.ppf(1 - 1 / (7 * math.e)))
        sr = m / sd
        z = (sr - sr0 / math.sqrt(252)) * math.sqrt(1499) / math.sqrt(1 - g3 * sr + (g4 - 1) / 4 * sr * sr)
        self.assertAlmostEqual(out["value"], st.norm.cdf(z), places=12)
        self.assertEqual(out["variance_source"], "trials")
        self.assertAlmostEqual(out["sharpe_annual"], sr * math.sqrt(252), places=12)

    def test_variance_floor_and_single_trial(self):
        rng = np.random.default_rng(1)
        r = rng.normal(0.0005, 0.01, 504)
        out = stats.deflated_sharpe(r, 252, 5, [0.10, 0.11, 0.12])
        self.assertEqual(out["variance_source"], "minimum")
        self.assertAlmostEqual(out["trial_variance"], 0.5)          # 1 / years = 1 / 2
        one = stats.deflated_sharpe(r, 252, 1, [])
        self.assertEqual(one["sr0_annual"], 0.0)
        mo = stats.moments(r)
        self.assertAlmostEqual(one["value"], stats.psr(mo["mean"] / mo["std"], 0.0, 504, mo["skew"], mo["kurtosis"]))

    def test_not_computable(self):
        self.assertIsNone(stats.deflated_sharpe([0.01] * 10, 252, 5, [])["value"])
        self.assertIsNone(stats.deflated_sharpe([0.01] * 30, 252, 5, [])["value"])   # zero variance
        self.assertIsNone(stats.deflated_sharpe(np.arange(30) * 1e-3, 252, 0, [])["value"])

    def test_expected_max_sharpe_grows_with_n(self):
        vals = [stats.expected_max_sharpe(n, 1.0) for n in (1, 2, 8, 64, 256)]
        self.assertEqual(vals[0], 0.0)
        self.assertTrue(all(a < b for a, b in zip(vals, vals[1:])))


def _pbo_direct(M, S=10):
    """Hand CSCV: concatenate the blocks, Sharpe with ddof 1, average ranks, logit <= 0 counts."""
    parts = np.array_split(np.arange(M.shape[0]), S)
    lam = []
    for c in combinations(range(S), S // 2):
        i = np.concatenate([parts[b] for b in c])
        o = np.concatenate([parts[b] for b in range(S) if b not in c])
        si = M[i].mean(0) / M[i].std(0, ddof=1)
        so = M[o].mean(0) / M[o].std(0, ddof=1)
        w = st.rankdata(so)[int(np.argmax(si))] / (M.shape[1] + 1)
        lam.append(math.log(w / (1 - w)))
    return sum(1 for x in lam if x <= 0) / len(lam), len(lam)


class PBOTests(unittest.TestCase):
    def test_split_count(self):
        rng = np.random.default_rng(0)
        M = rng.normal(size=(120, 6))
        for S in (2, 4, 6, 10):
            self.assertEqual(stats.pbo_cscv(M, S)["combinations"], math.comb(S, S // 2))

    def test_matches_direct_computation(self):
        rng = np.random.default_rng(3)
        M = rng.normal(0, 0.01, (503, 17)) + np.linspace(0, 0.002, 17)
        val, n = _pbo_direct(M)
        out = stats.pbo_cscv(M)
        self.assertEqual(out["combinations"], n)
        self.assertAlmostEqual(out["value"], val, places=12)

    def test_noise_near_one_half(self):
        rng = np.random.default_rng(11)
        vals = [stats.pbo_cscv(rng.normal(0, 0.01, (1000, 100)))["value"] for _ in range(20)]
        self.assertLess(abs(np.mean(vals) - 0.5), 0.08)

    def test_dominant_configuration_near_zero(self):
        rng = np.random.default_rng(12)
        M = rng.normal(0, 0.01, (1000, 50))
        M[:, 7] += 0.004                    # Sharpe ~ 6 a year: wins every split in and out of sample
        self.assertLess(stats.pbo_cscv(M)["value"], 0.02)

    def test_requirements(self):
        rng = np.random.default_rng(0)
        self.assertIsNone(stats.pbo_cscv(rng.normal(size=(100, 3)))["value"])
        self.assertIsNone(stats.pbo_cscv(rng.normal(size=(49, 5)))["value"])
        self.assertIsNotNone(stats.pbo_cscv(rng.normal(size=(50, 4)))["value"])
        bad = rng.normal(size=(60, 5))
        bad[3, 2] = np.nan
        self.assertIsNone(stats.pbo_cscv(bad)["value"])


def _nw_hand(y, X, L):
    n, k = X.shape
    inv = np.linalg.inv(X.T @ X)
    b = inv @ X.T @ y
    e = y - X @ b
    S = np.zeros((k, k))
    for t in range(n):
        S += e[t] ** 2 * np.outer(X[t], X[t])
    for l in range(1, L + 1):
        w = 1 - l / (L + 1)
        for t in range(l, n):
            G = e[t] * e[t - l] * np.outer(X[t], X[t - l])
            S += w * (G + G.T)
    cov = inv @ S @ inv * n / (n - k)
    return b, np.sqrt(np.diag(cov))


class NeweyWestTests(unittest.TestCase):
    def test_lag_rule(self):
        self.assertEqual(stats.nw_lag(100), 4)
        self.assertEqual(stats.nw_lag(1260), int(math.floor(4 * 12.6 ** (2 / 9))))
        self.assertEqual(stats.nw_lag(2520), 8)
        self.assertEqual(stats.nw_lag(0), 0)

    def test_against_hand_code_known_lag(self):
        rng = np.random.default_rng(5)
        x = rng.normal(size=300)
        y = 0.1 + 0.5 * x + np.convolve(rng.normal(size=302), [1, 0.6, 0.3], "valid")
        X = np.column_stack([np.ones(300), x])
        for L in (0, 3, 7):
            fit = stats.newey_west_ols(y, X, L)
            b, se = _nw_hand(y, X, L)
            np.testing.assert_allclose(fit["coef"], b, rtol=1e-10)
            np.testing.assert_allclose(fit["se"], se, rtol=1e-10)
            self.assertAlmostEqual(fit["t"][0], b[0] / se[0], places=9)

    def test_mean_t_and_alpha(self):
        rng = np.random.default_rng(6)
        x = rng.normal(0.1, 1, 400)
        out = stats.mean_t_newey_west(x, min_lag=10)
        b, se = _nw_hand(x, np.ones((400, 1)), 10)
        self.assertEqual(out["lag"], 10)
        self.assertAlmostEqual(out["value"], b[0] / se[0], places=9)
        rb = rng.normal(0, 0.01, 400)
        rs = 0.0005 + 0.3 * rb + rng.normal(0, 0.005, 400)
        a = stats.alpha_newey_west(rs, rb, 252)
        b, se = _nw_hand(rs, np.column_stack([np.ones(400), rb]), stats.nw_lag(400))
        self.assertAlmostEqual(a["alpha_daily"], b[0], places=12)
        self.assertAlmostEqual(a["value"], b[0] / se[0], places=9)
        self.assertAlmostEqual(a["alpha_annual"], 252 * b[0], places=10)
        self.assertIsNone(stats.alpha_newey_west(rs, np.zeros(400), 252)["value"])
        self.assertIsNone(stats.mean_t_newey_west(x[:10])["value"])


if __name__ == "__main__":
    unittest.main()
