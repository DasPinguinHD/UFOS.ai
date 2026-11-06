"""Reverse DCF: round trips, closed-form implied growth / rate, bracketing errors; base rates and the v0.4 parity."""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: tests run without installing the package
    sys.path.insert(0, _SRC)

import importlib.util
import unittest
from pathlib import Path

import numpy as np

from risklab.valuation import base_rates as BR
from risklab.valuation import dcf as D
from risklab.valuation import reverse as R

BASE = D.DCFInputs(fcf0=100.0, discount_rate=0.09, terminal_growth=0.02, growth=0.05, years=10, net_debt=300.0,
                   other_claims=50.0, shares=100.0)


class RoundTripTests(unittest.TestCase):
    def test_value_to_growth_to_value(self):
        for g in (-0.10, 0.0, 0.04, 0.13, 0.30):
            for fade in (False, True):
                inp = BASE.replace(growth=g, fade=fade)
                price = D.value_per_share(inp)
                g_hat = R.implied_growth(price, BASE, "fade_start" if fade else "constant")
                self.assertAlmostEqual(g_hat, g, places=9)
                self.assertAlmostEqual(D.value_per_share(inp.replace(growth=g_hat)), price, places=8)

    def test_value_to_rate_to_value(self):
        for r in (0.05, 0.09, 0.15):
            price = D.value_per_share(BASE.replace(discount_rate=r))
            self.assertAlmostEqual(R.implied_discount_rate(price, BASE), r, places=9)

    def test_gordon_closed_form(self):
        # one-year explicit period with g = g_T: P = FCF0 (1 + g) / (r - g)  ->  g = (r P - FCF0) / (P + FCF0)
        inp = D.DCFInputs(fcf0=10.0, discount_rate=0.10, terminal_growth=0.03, growth=0.03, years=1)
        price = 10.0 * 1.03 / 0.07
        self.assertAlmostEqual(R.implied_discount_rate(price, inp), 0.10, places=10)
        # implied r for a perpetuity: r = FCF1 / P + g
        p2 = 200.0
        self.assertAlmostEqual(R.implied_discount_rate(p2, inp), 10.0 * 1.03 / p2 + 0.03, places=10)


class BracketTests(unittest.TestCase):
    def test_no_root_in_bracket(self):
        hi_price = D.value_per_share(BASE.replace(growth=0.5))
        with self.assertRaises(R.NoRootInBracket) as cm:
            R.implied_growth(hi_price, BASE, lo=-0.5, hi=0.2)
        e = cm.exception
        self.assertLess(e.v_hi, hi_price)
        self.assertIn("widen the bracket", str(e))
        with self.assertRaises(R.NoRootInBracket):
            R.implied_discount_rate(1.0, BASE, hi=0.2)       # price too low for any r <= 20 %

    def test_input_errors(self):
        with self.assertRaises(D.DCFInputError):
            R.implied_growth(10.0, BASE.replace(fcf0=-5.0))
        with self.assertRaises(ValueError):
            R.implied_growth(10.0, BASE, mode="linear")
        with self.assertRaises(ValueError):
            R.implied_growth(10.0, BASE, lo=0.2, hi=0.1)
        with self.assertRaises(ValueError):
            R.implied_growth(10.0, BASE, lo=-1.0)
        with self.assertRaises(D.DCFInputError):
            R.implied_discount_rate(10.0, BASE, lo=0.01)      # below terminal growth

    def test_bracket_end_is_root(self):
        price = D.value_per_share(BASE.replace(growth=0.2))
        self.assertEqual(R.implied_growth(price, BASE, lo=-0.5, hi=0.2), 0.2)


def _load_survivorship():
    path = Path(__file__).resolve().parents[1] / "studies" / "survivorship.py"
    spec = importlib.util.spec_from_file_location("survivorship_for_parity", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class BaseRateTests(unittest.TestCase):
    def test_parity_with_the_v04_study(self):
        """The universe draws exactly what studies/survivorship.py draws (same parameters, same seed)."""
        sv = _load_survivorship()
        for kappa in (0.0, 1.0, 2.0):
            cfg = sv.SurvivorConfig(n_firms=500)
            alive0, _cagr, path0 = sv.simulate_cohort(cfg, cfg.h0, kappa, np.random.default_rng(11))
            u = BR.GrowthUniverse(n_firms=500, kappa=kappa)
            alive1, path1 = BR.simulate_cohort(u, np.random.default_rng(11))
            np.testing.assert_array_equal(alive0, alive1)
            np.testing.assert_array_equal(path0, path1)
            for h in (0.0, 0.1):
                naive, true = sv.base_rates(alive0, _cagr, h)
                r = BR.rates(alive1, path1, h)
                self.assertEqual(r["survivors_only"], naive)
                self.assertEqual(r["full_cohort"], true)
                self.assertEqual(r["counterfactual"], sv.counterfactual_rate(path0, h))

    def test_rates_by_hand(self):
        alive = np.array([True, True, False, True, False])
        cagr = np.array([0.12, 0.02, 0.20, 0.10, -0.05])
        r = BR.rates(alive, cagr, 0.10)
        self.assertEqual(r["survivors_only"], 2 / 3)
        self.assertEqual(r["full_cohort"], 2 / 5)
        self.assertEqual(r["counterfactual"], 3 / 5)

    def test_no_exits_all_equal(self):
        coh = BR.simulate_cohorts(BR.GrowthUniverse(n_firms=300, h0=0.0), 3, 5)
        b = coh.base_rates(0.08)
        self.assertEqual(b["survival_rate"], 1.0)
        self.assertAlmostEqual(b["survivors_only"], b["full_cohort"], places=15)
        self.assertAlmostEqual(b["counterfactual"], b["full_cohort"], places=15)


if __name__ == "__main__":
    unittest.main()
