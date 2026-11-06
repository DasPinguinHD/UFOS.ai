"""IRRBB measures: closed forms, DV01, key-rate durations, the BCBS shock shapes, the floor, Delta EVE / NII, outlier tests."""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: tests run without installing the package
    sys.path.insert(0, _SRC)

import math
import unittest

import numpy as np

from risklab.rates import curve as C
from risklab.rates import instruments as I
from risklab.rates import irrbb as R

FLAT3 = C.NelsonSiegel(0.03, 0.0, 0.0, 1.0)
CURVE = C.NelsonSiegel(0.03, -0.015, 0.01, 2.0)


class MeasureTests(unittest.TestCase):
    def test_zero_coupon_closed_form(self):
        cf = I.FixedBond(100, 0.0, 7).cashflows()
        m = R.bond_measures(cf, 0.03)
        self.assertAlmostEqual(m["pv"], 100 * math.exp(-0.21), places=10)
        self.assertAlmostEqual(m["macaulay"], 7.0, places=12)
        self.assertAlmostEqual(m["modified"], 7.0, places=12)
        self.assertAlmostEqual(m["convexity"], 49.0, places=10)

    def test_par_bond_annual_compounding(self):
        cf = I.FixedBond(100, 0.05, 10).cashflows()
        m = R.bond_measures(cf, 0.05, m=1)
        self.assertAlmostEqual(m["pv"], 100.0, places=10)
        mac = sum(t * c / 1.05 ** t for t, c in zip(cf.times, cf.amounts)) / 100
        self.assertAlmostEqual(m["macaulay"], mac, places=10)
        self.assertAlmostEqual(m["modified"], mac / 1.05, places=10)
        # second derivative of the annual-yield price, by finite differences
        p = lambda y: sum(c / (1 + y) ** t for t, c in zip(cf.times, cf.amounts))
        h = 1e-4
        self.assertAlmostEqual(m["convexity"], (p(0.05 + h) + p(0.05 - h) - 2 * p(0.05)) / h ** 2 / 100, places=3)

    def test_effective_measures_flat_curve_match_closed_form(self):
        cf = I.FixedBond(100, 0.04, 8).cashflows()
        e = R.effective_measures(cf, FLAT3)
        b = R.bond_measures(cf, 0.03)
        # central differences with a 1 bp bump: relative error of order 1e-7
        self.assertAlmostEqual(e["duration"], b["modified"], delta=1e-6 * b["modified"])
        self.assertAlmostEqual(e["convexity"], b["convexity"], places=2)
        self.assertAlmostEqual(e["dv01"], e["dv01_analytic"], delta=1e-6 * e["dv01"])

    def test_key_rates_sum_to_effective_duration(self):
        for cf in (I.FixedBond(100, 0.03, 12).cashflows(), I.AmortisingLoan(100, 0.04, 25).cashflows(),
                   I.NonMaturityDeposit(100, 0.7, 3.0).cashflows()):
            k = R.key_rate_durations(cf, CURVE)
            d = R.effective_measures(cf, CURVE)["duration"]
            self.assertAlmostEqual(k.sum(), d, delta=1e-6 * abs(d))   # equal up to second-order terms
        t = np.linspace(0, 40, 400)
        np.testing.assert_allclose(sum(R.tent(i)(t) for i in range(len(R.KEY_RATES))), 1.0, atol=1e-12)


class ShockTests(unittest.TestCase):
    def test_shapes(self):
        s = {"parallel": 200.0, "short": 250.0, "long": 100.0}
        self.assertAlmostEqual(float(R.shock("short_up", 0.0, s)), 0.025)
        self.assertAlmostEqual(float(R.shock("short_up", 4.0, s)), 0.025 * math.exp(-1))
        self.assertAlmostEqual(float(R.shock("parallel_down", 9.0, s)), -0.02)
        self.assertAlmostEqual(float(R.shock("steepener", 0.0, s)), -0.65 * 0.025)
        self.assertAlmostEqual(float(R.shock("flattener", 1e6, s)), -0.6 * 0.01, places=12)
        with self.assertRaises(ValueError):
            R.shock("twist", 1.0, s)

    def test_bcbs_worked_example_steepener(self):
        """BCBS d578 SRP31.92 example: -0.65 x 100 bp x 0.417 + 0.9 x 100 bp x (1 - 0.417) = +25.4 bp
        (0.417 = e^{-t/4} at t = 3.5)."""
        s = {"parallel": 100.0, "short": 100.0, "long": 100.0}
        self.assertAlmostEqual(math.exp(-3.5 / 4), 0.417, places=3)
        self.assertAlmostEqual(float(R.shock("steepener", 3.5, s)) / R.BP, 25.4, places=1)

    def test_presets(self):
        self.assertEqual(R.SHOCK_PRESETS["EUR_BCBS2016"], {"parallel": 200.0, "short": 250.0, "long": 100.0})
        self.assertEqual(R.SHOCK_PRESETS["EUR_BCBS2024"], {"parallel": 225.0, "short": 350.0, "long": 200.0})

    def test_floor(self):
        f = R.Floor()
        np.testing.assert_allclose(f(np.array([0.0, 10.0, 50.0, 80.0])), [-0.015, -0.012, 0.0, 0.0], atol=1e-15)
        low = C.NelsonSiegel(0.001, 0.0, 0.0, 1.0)
        down = R.shocked_curve(low, "parallel_down", R.SHOCK_PRESETS["EUR_BCBS2016"])
        np.testing.assert_allclose(down.zero(np.array([0.0, 10.0, 60.0])), [-0.015, -0.012, 0.0], atol=1e-12)
        # an observed rate already below the floor stays (the floor is min(floor, observed))
        neg = C.NelsonSiegel(-0.02, 0.0, 0.0, 1.0)
        d2 = R.shocked_curve(neg, "parallel_down", R.SHOCK_PRESETS["EUR_BCBS2016"])
        self.assertAlmostEqual(float(d2.zero(1.0)), -0.02, places=12)
        nofloor = R.shocked_curve(low, "parallel_down", R.SHOCK_PRESETS["EUR_BCBS2016"], floor=None)
        self.assertAlmostEqual(float(nofloor.zero(60.0)), -0.019, places=12)


class EVENIITests(unittest.TestCase):
    def test_eve_sign_conventions(self):
        asset = I.BalanceSheet([I.FixedBond(100, 0.03, 10)], [], tier1=10)
        liab = I.BalanceSheet([], [I.FixedBond(100, 0.03, 10, side=-1)], tier1=10)
        ra, rl = R.run_scenarios(asset, CURVE), R.run_scenarios(liab, CURVE)
        up = {r.scenario: r for r in ra.rows}["parallel_up"]
        self.assertLess(up.delta_eve, 0)
        self.assertGreater({r.scenario: r for r in rl.rows}["parallel_up"].delta_eve, 0)
        self.assertAlmostEqual(up.eve_base, R.pv(I.FixedBond(100, 0.03, 10).cashflows(), CURVE))

    def test_longer_nmd_maturity_hedges_more(self):
        out = []
        for m in (1.0, 3.0, 5.0):
            bs = I.synthetic_balance_sheet(nmd_avg_maturity=m)
            out.append(R.outlier_tests(R.run_scenarios(bs, CURVE))["eve_ratio"])
        self.assertTrue(out[0] > out[1] > out[2])

    def test_nii_frn_only(self):
        frn = I.FloatingRateNote(100, 0.03, next_reset=0.25, reset_period=0.25)
        bs = I.BalanceSheet([frn], [], tier1=10)
        base = R.as_zero_curve(FLAT3)
        self.assertAlmostEqual(R.nii(bs, base, base), 3.0, places=12)
        up = R.shocked_curve(base, "parallel_up", R.SHOCK_PRESETS["EUR_BCBS2016"])
        # months 3..11 (9 of 12) earn the 200 bp higher rate
        self.assertAlmostEqual(R.nii(bs, base, up) - 3.0, 100 * 0.02 * 9 / 12, places=10)

    def test_outlier_tests_and_assumptions(self):
        res = R.run_scenarios(I.synthetic_balance_sheet(), CURVE)
        o = R.outlier_tests(res)
        self.assertEqual(o["eve_outlier"], o["eve_ratio"] > 0.15)
        self.assertIn(o["nii_worst_scenario"], R.NII_SCENARIOS)
        self.assertEqual({r.scenario for r in res.rows}, set(R.SCENARIOS))
        for key in ("shock sizes", "post-shock floor", "NMD caps", "NII horizon"):
            self.assertIn(key, res.assumptions.items)
        o2 = R.outlier_tests(res, eve_threshold=1.0, nii_threshold=1.0)
        self.assertFalse(o2["eve_outlier"] or o2["nii_outlier"])


if __name__ == "__main__":
    unittest.main()
