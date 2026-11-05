"""Kupiec, Christoffersen and the Basel traffic light."""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: tests run without installing the package
    sys.path.insert(0, _SRC)

import math
import unittest

import numpy as np
from scipy import stats

from risklab.backtest import (NOT_REJECTED, REJECT, binomial_exact, christoffersen_cc, christoffersen_ind,
                              exact_size, kupiec_pof, traffic_light, transition_counts, zone_bounds)


def _pmf(x, n, p):
    """Binomial probability by brute force (math.comb), independent of scipy."""
    return math.comb(n, x) * p ** x * (1 - p) ** (n - x)


def _exc(n, ones):
    e = np.zeros(n, dtype=bool)
    e[list(ones)] = True
    return e


class KupiecTests(unittest.TestCase):
    def test_reference_value(self):
        r = kupiec_pof(_exc(250, range(7)), 0.99)
        self.assertAlmostEqual(r.statistic, 5.497, places=3)
        self.assertAlmostEqual(r.p_value, 0.0190, places=4)
        self.assertEqual((r.decision, r.df, r.n_obs, r.n_exceed), (REJECT, 1, 250, 7))

    def test_edges_exact(self):
        r0 = kupiec_pof(np.zeros(250, dtype=bool), 0.99)
        self.assertEqual(r0.statistic, -2 * 250 * math.log(0.99))
        rt = kupiec_pof(np.ones(250, dtype=bool), 0.99)
        self.assertAlmostEqual(rt.statistic, -2 * 250 * math.log(0.01), places=9)
        self.assertEqual(rt.decision, REJECT)

    def test_exact_rate_is_zero(self):
        r = kupiec_pof(_exc(1000, range(10)), 0.99)
        self.assertEqual(r.statistic, 0.0)
        self.assertEqual(r.p_value, 1.0)
        self.assertEqual(r.decision, NOT_REJECTED)

    def test_validation(self):
        for call in (lambda: kupiec_pof([], 0.99), lambda: kupiec_pof([0, 2], 0.99),
                     lambda: kupiec_pof([0, 1], 1.0), lambda: kupiec_pof([0, 1], 0.99, reject_at=0),
                     lambda: kupiec_pof(np.zeros((2, 2)), 0.99)):
            with self.assertRaises(ValueError):
                call()
        self.assertEqual(kupiec_pof([0, 1, 0], 0.9).n_exceed, 1)


class ExactSizeTests(unittest.TestCase):
    def test_kupiec_exact_size(self):
        r250 = exact_size("kupiec_pof", 250, 0.99)
        self.assertEqual(r250["reject_counts"][:3], [0, 7, 8])
        self.assertEqual(r250["reject_counts"], [0] + list(range(7, 251)))
        brute = _pmf(0, 250, 0.01) + sum(_pmf(x, 250, 0.01) for x in range(7, 251))
        self.assertAlmostEqual(r250["size"], brute, places=12)
        self.assertAlmostEqual(r250["size"], 0.0948, places=4)
        self.assertAlmostEqual(_pmf(0, 250, 0.01), 0.081, places=3)
        r1000 = exact_size("kupiec_pof", 1000, 0.99)
        self.assertAlmostEqual(r1000["size"], 0.0551, places=4)
        brute = sum(_pmf(x, 1000, 0.01) for x in r1000["reject_counts"])
        self.assertAlmostEqual(r1000["size"], brute, places=12)

    def test_binomial_exact_size_at_most_nominal(self):
        for n in (50, 100, 250, 500, 1000):
            for level in (0.05, 0.01):
                with self.subTest(n=n, level=level):
                    self.assertLessEqual(exact_size("binomial_exact", n, 0.99, level)["size"], level)

    def test_validation(self):
        with self.assertRaises(ValueError):
            exact_size("christoffersen_ind", 250, 0.99)
        with self.assertRaises(ValueError):
            exact_size("kupiec_pof", 0, 0.99)


class BinomialExactTests(unittest.TestCase):
    def test_p_value_brute_force(self):
        n, p = 250, 0.01
        probs = [_pmf(k, n, p) for k in range(n + 1)]
        for x in (0, 1, 2, 3, 5, 6, 7, 10):
            with self.subTest(x=x):
                # two-sided minimum-likelihood p-value: all counts at most as likely as x
                expected = min(1.0, sum(q for q in probs if q <= probs[x] * (1 + 1e-7)))
                r = binomial_exact(_exc(n, range(x)), 0.99)
                self.assertAlmostEqual(r.p_value, expected, places=10)
                self.assertEqual((r.statistic, r.n_exceed, r.n_obs), (float(x), x, n))

    def test_decisions_at_250(self):
        self.assertEqual(binomial_exact(np.zeros(250, dtype=bool), 0.99).decision, NOT_REJECTED)
        self.assertEqual(kupiec_pof(np.zeros(250, dtype=bool), 0.99).decision, REJECT)  # the asymptotic flaw
        self.assertEqual(binomial_exact(_exc(250, range(5)), 0.99).decision, NOT_REJECTED)
        self.assertEqual(binomial_exact(_exc(250, range(6)), 0.99).decision, REJECT)

    def test_validation(self):
        with self.assertRaises(ValueError):
            binomial_exact([], 0.99)
        with self.assertRaises(ValueError):
            binomial_exact([0, 1], 1.5)


class ChristoffersenTests(unittest.TestCase):
    # 0 0 1 1 0 0 0 1 0 0 : pairs 00 01 11 10 00 00 01 10 00
    SEQ = np.array([0, 0, 1, 1, 0, 0, 0, 1, 0, 0], dtype=bool)

    def test_counts(self):
        self.assertEqual(transition_counts(self.SEQ), {"n00": 4, "n01": 2, "n10": 2, "n11": 1})

    def test_hand_computed_ind(self):
        n00, n01, n10, n11 = 4, 2, 2, 1
        pi01, pi11, pi = n01 / (n00 + n01), n11 / (n10 + n11), (n01 + n11) / 9
        ll0 = (n00 + n10) * math.log(1 - pi) + (n01 + n11) * math.log(pi)
        ll1 = (n00 * math.log(1 - pi01) + n01 * math.log(pi01) + n10 * math.log(1 - pi11)
               + n11 * math.log(pi11))
        lr = -2 * (ll0 - ll1)
        r = christoffersen_ind(self.SEQ)
        self.assertAlmostEqual(r.statistic, lr, places=12)
        self.assertAlmostEqual(r.p_value, stats.chi2.sf(lr, 1), places=12)
        self.assertEqual(r.df, 1)
        self.assertEqual(r.notes, "")

    def test_hand_computed_cc(self):
        a = 0.9
        p = 1 - a
        tail = self.SEQ[1:]  # convention: conditional on the first observation
        x, t = int(tail.sum()), tail.size
        lr_uc = -2 * ((t - x) * math.log(1 - p) + x * math.log(p)
                      - (t - x) * math.log(1 - x / t) - x * math.log(x / t))
        r = christoffersen_cc(self.SEQ, a)
        self.assertAlmostEqual(r.details["lr_uc"], lr_uc, places=12)
        self.assertAlmostEqual(r.statistic, lr_uc + christoffersen_ind(self.SEQ).statistic, places=12)
        self.assertEqual(r.df, 2)
        # LR_cc is the joint LR: iid(p) against the Markov chain on the transitions
        n00, n01, n10, n11 = 4, 2, 2, 1
        ll_p = (n00 + n10) * math.log(1 - p) + (n01 + n11) * math.log(p)
        ll_m = (n00 * math.log(4 / 6) + n01 * math.log(2 / 6) + n10 * math.log(2 / 3) + n11 * math.log(1 / 3))
        self.assertAlmostEqual(r.statistic, -2 * (ll_p - ll_m), places=12)
        self.assertAlmostEqual(r.p_value, stats.chi2.sf(r.statistic, 2), places=12)

    def test_degenerate_no_exceedance(self):
        r = christoffersen_ind(np.zeros(250, dtype=bool))
        self.assertEqual(r.statistic, 0.0)
        self.assertIn("no exceedance", r.notes)
        self.assertIsNone(r.details["pi11"])
        self.assertEqual(r.decision, NOT_REJECTED)

    def test_degenerate_no_transition_out_of_one(self):
        r = christoffersen_ind(_exc(50, [49]))  # the only exceedance is the last day
        self.assertIn("no transition out of state 1", r.notes)
        self.assertIsNone(r.details["pi11"])
        self.assertGreaterEqual(r.statistic, 0.0)
        self.assertTrue(math.isfinite(r.statistic))

    def test_clustered_rejected(self):
        e = _exc(250, range(100, 106))
        self.assertEqual(christoffersen_ind(e).decision, REJECT)
        spread = _exc(250, range(10, 250, 40))
        self.assertEqual(christoffersen_ind(spread).decision, NOT_REJECTED)

    def test_too_short(self):
        with self.assertRaises(ValueError):
            christoffersen_ind([True])
        with self.assertRaises(ValueError):
            christoffersen_cc([True], 0.99)


class TrafficLightTests(unittest.TestCase):
    def test_zone_boundaries_250_99(self):
        self.assertEqual(zone_bounds(250, 0.99), {"yellow_from": 5, "red_from": 10})
        expected = {4: ("green", 0.0), 5: ("yellow", 0.40), 6: ("yellow", 0.50), 7: ("yellow", 0.65),
                    8: ("yellow", 0.75), 9: ("yellow", 0.85), 10: ("red", 1.0), 15: ("red", 1.0), 0: ("green", 0.0)}
        for x, (zone, plus) in expected.items():
            with self.subTest(x=x):
                r = traffic_light(_exc(250, range(x)))
                self.assertEqual(r.decision, zone)
                self.assertEqual(r.details["plus_factor"], plus)
                self.assertEqual(r.n_exceed, x)

    def test_last_window_only(self):
        e = np.concatenate([np.ones(100, dtype=bool), _exc(250, range(3))])
        r = traffic_light(e)
        self.assertEqual((r.n_obs, r.n_exceed, r.decision), (250, 3, "green"))

    def test_other_window(self):
        r = traffic_light(_exc(500, range(9)), 0.99, window=500)
        self.assertIsNone(r.details["plus_factor"])
        self.assertIn("250 days at 99 %", r.notes)
        # independent check: cumulative binomial sums by brute force (math.comb, no scipy)
        for n, a in ((500, 0.99), (250, 0.99), (250, 0.975), (1000, 0.99)):
            with self.subTest(n=n, alpha=a):
                cdf, yellow, red = 0.0, None, None
                for x in range(n + 1):
                    cdf += _pmf(x, n, 1 - a)
                    if yellow is None and cdf >= 0.95:
                        yellow = x
                    if red is None and cdf >= 0.9999:
                        red = x
                        break
                self.assertEqual(zone_bounds(n, a), {"yellow_from": yellow, "red_from": red})

    def test_validation(self):
        with self.assertRaises(ValueError):
            traffic_light(np.zeros(100, dtype=bool))
        with self.assertRaises(ValueError):
            traffic_light(np.zeros(300, dtype=bool), window=0)


if __name__ == "__main__":
    unittest.main()
