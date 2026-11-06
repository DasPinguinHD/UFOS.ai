"""IRB: the risk-weight function against published illustrative values and a hand computation; floors and bounds;
the Vasicek quantile against Monte Carlo for a large portfolio; the granularity gap."""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: tests run without installing the package
    sys.path.insert(0, _SRC)

import math
import unittest
from statistics import NormalDist

import numpy as np

from risklab.credit import irb as I

N = NormalDist()


def k_by_hand(pd, lgd, m):
    w = (1 - math.exp(-50 * pd)) / (1 - math.exp(-50))
    r = 0.12 * w + 0.24 * (1 - w)
    b = (0.11852 - 0.05478 * math.log(pd)) ** 2
    q = N.cdf((N.inv_cdf(pd) + math.sqrt(r) * N.inv_cdf(0.999)) / math.sqrt(1 - r))
    return (lgd * q - pd * lgd) * (1 + (m - 2.5) * b) / (1 - 1.5 * b)


class IRBFormulaTests(unittest.TestCase):
    def test_published_illustrative_risk_weights(self):
        """Basel II illustrative corporate risk weights (LGD 45 %, M = 2.5 years), as recalled:
        PD 0.03 % -> 14.44 %, 0.10 % -> 29.65 %, 1.00 % -> 92.32 %."""
        for pd, rw in ((0.0003, 14.44), (0.001, 29.65), (0.01, 92.32)):
            self.assertAlmostEqual(100 * float(I.risk_weight(pd, 0.45, 2.5, pd_floor=0.0)), rw, places=2)

    def test_hand_computation(self):
        for pd, lgd, m in ((0.002, 0.45, 2.5), (0.03, 0.35, 4.0), (0.15, 0.6, 1.0)):
            self.assertAlmostEqual(float(I.capital_k(pd, lgd, m, pd_floor=0.0)), k_by_hand(pd, lgd, m), places=10)

    def test_floors_bounds_and_scaling(self):
        self.assertEqual(float(I.capital_k(0.0001, 0.45)), float(I.capital_k(0.0005, 0.45)))   # PD floor 0.05 %
        self.assertEqual(float(I.capital_k(0.01, 0.45, 0.2)), float(I.capital_k(0.01, 0.45, 1.0)))
        self.assertEqual(float(I.capital_k(0.01, 0.45, 9.0)), float(I.capital_k(0.01, 0.45, 5.0)))
        self.assertAlmostEqual(float(I.risk_weight(0.01, 0.45, scaling=1.06)),
                               1.06 * float(I.risk_weight(0.01, 0.45)), places=12)
        self.assertAlmostEqual(float(I.correlation(1e-9)), 0.24, places=6)
        self.assertAlmostEqual(float(I.correlation(1.0)), 0.12, places=12)
        with self.assertRaises(ValueError):
            I.capital_k(1.0, 0.45)
        self.assertIn("CRE32.4", I.IRB_SOURCES["pd_floor"])
        self.assertIn("CRE32.46", I.IRB_SOURCES["maturity"])
        self.assertIn("CRE99", I.IRB_SOURCES["formula"])
        self.assertNotIn("ASSUMPTION", "".join(I.IRB_SOURCES.values()))

    def test_maturity_one_is_the_vasicek_quantile(self):
        pd, lgd = 0.02, 0.4
        r = float(I.correlation(pd))
        self.assertAlmostEqual(float(I.capital_k(pd, lgd, 1.0, pd_floor=0)),
                               lgd * (float(I.vasicek_quantile(pd, r)) - pd), places=14)


class MonteCarloTests(unittest.TestCase):
    def test_vasicek_quantile_vs_large_portfolio(self):
        pd, rho = 0.01, 0.15
        L = I.simulate_losses([I.Bucket(20000, 1.0, 1.0, pd)], 200_000, seed=3, rho=rho) / 20000
        q, lo, hi = I.quantile_ci(L, 0.999)
        v = float(I.vasicek_quantile(pd, rho))
        self.assertLess(lo - 0.002, v)
        self.assertGreater(hi + 0.002, v)        # within the order-statistic interval (+ granularity slack)
        self.assertAlmostEqual(float(L.mean()), pd, delta=4 * L.std() / math.sqrt(L.size))

    def test_concentration_gap(self):
        gran = [I.Bucket(5000, 2.0, 0.45, 0.01)]
        conc = [I.Bucket(10, 1000.0, 0.45, 0.01)]
        out = []
        for b in (gran, conc):
            L = I.simulate_losses(b, 100_000, seed=8)
            out.append((I.quantile_ci(L)[0] - I.expected_loss(b)) / I.portfolio_k(b, m=1.0, pd_floor=0))
        self.assertLess(abs(out[0] - 1.0), 0.05)
        self.assertGreater(out[1], 1.5)


if __name__ == "__main__":
    unittest.main()
