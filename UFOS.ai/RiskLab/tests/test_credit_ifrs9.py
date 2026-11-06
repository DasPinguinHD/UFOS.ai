"""IFRS 9: ECL of a single loan by hand, staging edge cases, scenario weighting, the EAD profile."""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: tests run without installing the package
    sys.path.insert(0, _SRC)

import unittest

import numpy as np

from risklab.credit import ifrs9 as F

BASE = (F.Scenario("base", 1.0, ()),)


class ECLTests(unittest.TestCase):
    def test_single_bullet_loan_by_hand(self):
        ln = F.Loan(ead=100, eir=0.05, maturity=3, lgd=0.4, pd_now=0.02, pd_orig=0.02, amortising=False)
        h = 0.02
        e12 = h * 0.4 * 100 / 1.05
        elt = e12 + (1 - h) * h * 0.4 * 100 / 1.05 ** 2 + (1 - h) ** 2 * h * 0.4 * 100 / 1.05 ** 3
        r = F.ecl(ln, BASE)
        self.assertEqual(r["stage"], 1)
        self.assertAlmostEqual(r["ecl"], e12, places=12)
        self.assertAlmostEqual(r["ecl_lifetime"], elt, places=12)

    def test_amortising_ead_profile(self):
        ln = F.Loan(ead=1000, eir=0.1, maturity=3, lgd=0.5, pd_now=0.01, pd_orig=0.01)
        A = 1000 * 0.1 / (1 - 1.1 ** -3)
        np.testing.assert_allclose(F.ead_profile(ln), [1000, 1100 - A, (1100 - A) * 1.1 - A])

    def test_scenarios_weighting(self):
        ln = F.Loan(ead=100, eir=0.0, maturity=1, lgd=1.0, pd_now=0.01, pd_orig=0.01)
        sc = (F.Scenario("up", 0.25, (0.5,)), F.Scenario("base", 0.5, ()), F.Scenario("down", 0.25, (3.0,)))
        self.assertAlmostEqual(F.ecl(ln, sc)["ecl_12m"], 100 * (0.25 * 0.005 + 0.5 * 0.01 + 0.25 * 0.03))
        with self.assertRaises(ValueError):
            F.ecl(ln, (F.Scenario("a", 0.6, ()), F.Scenario("b", 0.6, ())))


class StagingTests(unittest.TestCase):
    def test_edges(self):
        r = F.StagingRules(sicr_multiple=3.0, sicr_abs=0.001)
        mk = lambda pd_now, dpd=0, pd_orig=0.01: F.Loan(10, 0.05, 5, 0.45, pd_now, pd_orig, dpd)
        self.assertEqual(F.stage(mk(0.0299), BASE, r), 1)
        self.assertEqual(F.stage(mk(0.03), BASE, r), 2)                     # exactly the multiple: stage 2
        self.assertEqual(F.stage(mk(0.0012, pd_orig=0.0003), BASE, r), 1)   # x4 but only +9 bp absolute
        self.assertEqual(F.stage(mk(0.01, dpd=29), BASE, r), 1)
        self.assertEqual(F.stage(mk(0.01, dpd=30), BASE, r), 2)
        self.assertEqual(F.stage(mk(0.01, dpd=89), BASE, r), 2)
        self.assertEqual(F.stage(mk(0.01, dpd=90), BASE, r), 3)
        e3 = F.ecl(mk(0.01, dpd=90), BASE, r)
        self.assertEqual(e3["ecl"], 0.45 * 10)

    def test_staging_basis_ignores_the_scenario_overlay(self):
        """pd_now and pd_orig are compared on the same basis: a downside-heavy scenario set changes the ECL,
        not the stage (v0.8 review)."""
        ln = F.Loan(10, 0.05, 5, 0.45, 0.0299, 0.01)
        harsh = (F.Scenario("base", 0.2, ()), F.Scenario("downside", 0.8, (3.0, 2.0)))
        self.assertEqual(F.stage(ln, BASE), 1)
        self.assertEqual(F.stage(ln, harsh), 1)            # weighted PD 0.0598 >= 3 x 1 %, but the basis is pd_now
        self.assertGreater(F.ecl(ln, harsh)["ecl"], F.ecl(ln, BASE)["ecl"])
        self.assertEqual(F.stage(F.Loan(10, 0.05, 5, 0.45, 0.03, 0.01), harsh), 2)
        self.assertIn("same basis", F.IFRS9_SOURCES["staging basis"])

    def test_cliff(self):
        lo = F.ecl(F.Loan(1, 0.05, 5, 0.45, 0.0299, 0.01), BASE)
        hi = F.ecl(F.Loan(1, 0.05, 5, 0.45, 0.0300, 0.01), BASE)
        self.assertEqual((lo["stage"], hi["stage"]), (1, 2))
        self.assertGreater(hi["ecl"] / lo["ecl"], 2.5)     # 5-year amortising loan: lifetime about 2.8 x 12-month

    def test_validation(self):
        with self.assertRaises(ValueError):
            F.Loan(10, 0.05, 0, 0.45, 0.01, 0.01)
        with self.assertRaises(ValueError):
            F.Loan(10, 0.05, 3, 0.45, 0.01, 0.0)


if __name__ == "__main__":
    unittest.main()
