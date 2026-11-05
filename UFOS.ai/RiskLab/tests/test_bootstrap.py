"""Stationary block bootstrap, automatic block length, predictive distributions."""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: tests run without installing the package
    sys.path.insert(0, _SRC)

import unittest

import numpy as np
from scipy import stats

from risklab.bootstrap import (BlockLengthError, bootstrap_h_sums, iid_bootstrap_indices, optimal_block_length, resolve_block_len,
                               stationary_bootstrap_indices)
from risklab.dists import Empirical, LocScale
from risklab.models import HistoricalSimulation


def _block_lengths(idx_row, n):
    """Lengths of the blocks in one index path (a block continues while idx[t] = idx[t-1] + 1 mod n)."""
    lengths, cur = [], 1
    for a, b in zip(idx_row[:-1], idx_row[1:]):
        if b == (a + 1) % n:
            cur += 1
        else:
            lengths.append(cur)
            cur = 1
    lengths.append(cur)
    return np.array(lengths)


class StationaryBootstrapTests(unittest.TestCase):
    def test_mean_block_length(self):
        n = 100_000  # large n: a fresh start rarely lands on the next index by chance
        for b in (2.0, 5.0, 12.0):
            idx = stationary_bootstrap_indices(n, 60_000, b, 1, np.random.default_rng(1))
            lengths = _block_lengths(idx[0], n)[:-1]  # the last block is cut off by the path end
            se = lengths.std() / np.sqrt(lengths.size)
            self.assertLess(abs(lengths.mean() - b), 4 * se, msg=f"b={b}")

    def test_wraps_around(self):
        idx = stationary_bootstrap_indices(5, 200, 50.0, 3, np.random.default_rng(2))
        self.assertTrue(np.all((idx >= 0) & (idx < 5)))
        steps = (idx[:, 1:] - idx[:, :-1]) % 5
        self.assertTrue(np.any(idx[:, 1:][steps == 1] == 0))  # 4 -> 0 occurred

    def test_reproducible(self):
        x = np.random.default_rng(3).standard_normal(300)
        a = bootstrap_h_sums(x, 10, 4.0, 1000, np.random.default_rng(9))
        b = bootstrap_h_sums(x, 10, 4.0, 1000, np.random.default_rng(9))
        self.assertEqual(a.tobytes(), b.tobytes())
        self.assertNotEqual(a.tobytes(), bootstrap_h_sums(x, 10, 4.0, 1000, np.random.default_rng(10)).tobytes())

    def test_block_len_one_equals_iid_bootstrap(self):
        a = stationary_bootstrap_indices(250, 10, 1.0, 5000, np.random.default_rng(11))
        b = iid_bootstrap_indices(250, 10, 5000, np.random.default_rng(11))
        np.testing.assert_array_equal(a, b)

    def test_auto_block_length(self):
        rng = np.random.default_rng(12)
        n = 2000
        noise = rng.standard_normal(n)
        ar = np.empty(n)
        ar[0] = noise[0]
        for t in range(1, n):
            ar[t] = 0.7 * ar[t - 1] + noise[t]
        b_ar = optimal_block_length(ar)
        b_wn = optimal_block_length(rng.standard_normal(n))
        self.assertGreater(b_ar["b_sb"], 2 * b_wn["b_sb"])
        self.assertGreater(b_ar["m_hat"], b_wn["m_hat"])
        self.assertLessEqual(b_ar["b_sb"], b_ar["b_max"])
        self.assertGreaterEqual(b_wn["b_sb"], 1.0)
        self.assertEqual(resolve_block_len(ar, "auto"), b_ar["b_sb"])
        # independent reference for the AR(1): b = (2 G^2 / D_SB)^(1/3) n^(1/3) with the true
        # autocovariances R(k) = phi^|k| / (1 - phi^2): G = sum |k| R(k), g0 = sum R(k) (infinite sums)
        phi = 0.7
        k = np.arange(1, 400)
        r0 = 1 / (1 - phi ** 2)
        g_true = 2 * np.sum(k * r0 * phi ** k)
        g0_true = r0 + 2 * np.sum(r0 * phi ** k)
        b_true = (2 * g_true ** 2 / (2 * g0_true ** 2)) ** (1 / 3) * n ** (1 / 3)
        self.assertLess(abs(b_ar["b_sb"] / b_true - 1), 0.35)

    def test_undefined_auto_block_length_raises(self):
        # over-differenced white noise (MA(1) with a unit root): spectral density 0 at frequency 0
        x = np.diff(np.random.default_rng(29).standard_normal(1001))
        with self.assertRaises(BlockLengthError):
            optimal_block_length(x)
        with self.assertRaises(ValueError):  # BlockLengthError is a ValueError
            resolve_block_len(x, "auto")
        self.assertEqual(resolve_block_len(x, 3.0), 3.0)  # an explicit block length still works

    def test_validation(self):
        with self.assertRaises(ValueError):
            stationary_bootstrap_indices(10, 5, 0.5, 1, np.random.default_rng(0))
        with self.assertRaises(ValueError):
            resolve_block_len(np.zeros(20), "optimal")
        with self.assertRaises(ValueError):
            optimal_block_length(np.zeros(5))


class DistributionTests(unittest.TestCase):
    def test_locscale_against_scipy(self):
        u = np.array([0.01, 0.3, 0.5, 0.975, 0.999])
        d = LocScale(0.1, 2.0)
        np.testing.assert_allclose(d.ppf(u), stats.norm.ppf(u, 0.1, 2.0), rtol=1e-12)
        np.testing.assert_allclose(d.cdf(d.ppf(u)), u, rtol=1e-12)
        t = LocScale(-0.1, 0.5, 4.0)
        np.testing.assert_allclose(t.ppf(u), stats.t.ppf(u, 4.0, -0.1, 0.5), rtol=1e-10)
        np.testing.assert_allclose(t.cdf(0.3), stats.t.cdf(0.3, 4.0, -0.1, 0.5), rtol=1e-12)
        self.assertEqual(t.family, ("t", 4.0))
        with self.assertRaises(ValueError):
            LocScale(0.0, -1.0)

    def test_empirical_convention(self):
        x = np.arange(1.0, 11.0)
        e = Empirical(x[::-1])
        np.testing.assert_array_equal(e.ppf([0.05, 0.1, 0.11, 0.9, 0.95, 1.0]), [1, 1, 2, 9, 10, 10])
        h = HistoricalSimulation()
        h.fit(x)
        self.assertEqual(e.quantile(0.9), h.forecast([0.9]).var[0.9])
        s = Empirical(x, loc=1.0, scale=2.0)
        self.assertEqual(float(s.ppf(0.9)), 19.0)


if __name__ == "__main__":
    unittest.main()
