"""DCF core: closed forms (Gordon perpetuity, hand-built cash flows), the growth paths, input errors."""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: tests run without installing the package
    sys.path.insert(0, _SRC)

import unittest

import numpy as np

from risklab.valuation import dcf as D


class ClosedFormTests(unittest.TestCase):
    def test_constant_growth_perpetuity_is_gordon(self):
        # growth = terminal growth: the explicit period changes nothing, EV = FCF0 (1 + g) / (r - g)
        for r, g, n in ((0.09, 0.03, 10), (0.08, 0.0, 1), (0.12, -0.02, 25), (0.07, 0.05, 5)):
            res = D.dcf(D.DCFInputs(fcf0=100.0, discount_rate=r, terminal_growth=g, growth=g, years=n))
            self.assertAlmostEqual(res.enterprise_value, 100.0 * (1 + g) / (r - g), places=9)

    def test_hand_built_two_year_case(self):
        # FCF0 = 50, g = 10 %, 2 years, r = 8 %, g_T = 2 %: written out by hand
        f1, f2 = 55.0, 60.5
        tv = f2 * 1.02 / 0.06
        ev = f1 / 1.08 + f2 / 1.08 ** 2 + tv / 1.08 ** 2
        res = D.dcf(D.DCFInputs(fcf0=50.0, discount_rate=0.08, terminal_growth=0.02, growth=0.10, years=2,
                                net_debt=100.0, other_claims=20.0, shares=4.0))
        np.testing.assert_allclose(res.fcf, [f1, f2])
        self.assertAlmostEqual(res.terminal_value, tv, places=9)
        self.assertAlmostEqual(res.enterprise_value, ev, places=9)
        self.assertAlmostEqual(res.equity_value, ev - 120.0, places=9)
        self.assertAlmostEqual(res.per_share, (ev - 120.0) / 4.0, places=9)
        self.assertAlmostEqual(res.terminal_share, (tv / 1.08 ** 2) / ev, places=12)

    def test_fade_path(self):
        np.testing.assert_allclose(D.growth_path(0.10, 0.02, 5, fade=True), [0.10, 0.08, 0.06, 0.04, 0.02])
        np.testing.assert_allclose(D.growth_path(0.10, 0.02, 3, fade=False), [0.10, 0.10, 0.10])
        inp = D.DCFInputs(100.0, 0.09, 0.02, 0.10, years=5, fade=True)
        cagr = (1.10 * 1.08 * 1.06 * 1.04 * 1.02) ** 0.2 - 1
        self.assertAlmostEqual(D.explicit_cagr(inp), cagr, places=12)
        self.assertAlmostEqual(D.explicit_cagr(inp.replace(fade=False)), 0.10, places=12)

    def test_vector_matches_scalar(self):
        r = np.array([0.08, 0.09, 0.10])
        g = np.array([0.03, 0.06, 0.12])
        v = D.per_share_vec(100.0, r, 0.02, g, 10, False, 300.0, 50.0, 100.0)
        for i in range(3):
            self.assertAlmostEqual(v[i], D.value_per_share(D.DCFInputs(100.0, r[i], 0.02, g[i], 10, False, 300.0,
                                                                         50.0, 100.0)), places=10)

    def test_monotone(self):
        base = D.DCFInputs(100.0, 0.09, 0.02, 0.05)
        vals = [D.value_per_share(base.replace(growth=g)) for g in (0.0, 0.05, 0.10)]
        self.assertTrue(vals[0] < vals[1] < vals[2])
        vals = [D.value_per_share(base.replace(discount_rate=r)) for r in (0.07, 0.09, 0.11)]
        self.assertTrue(vals[0] > vals[1] > vals[2])


class InputErrorTests(unittest.TestCase):
    def test_errors(self):
        with self.assertRaises(D.DCFInputError):
            D.DCFInputs(100.0, 0.03, 0.03, 0.05)                    # r = g_T
        with self.assertRaises(D.DCFInputError):
            D.DCFInputs(100.0, 0.02, 0.03, 0.05)                    # r < g_T
        with self.assertRaises(D.DCFInputError):
            D.DCFInputs(100.0, 0.09, 0.02, 0.05, years=0)
        with self.assertRaises(D.DCFInputError):
            D.DCFInputs(100.0, 0.09, 0.02, 0.05, years=2.5)
        with self.assertRaises(D.DCFInputError):
            D.DCFInputs(100.0, 0.09, 0.02, 0.05, years=1, fade=True)
        with self.assertRaises(D.DCFInputError):
            D.DCFInputs(100.0, 0.09, 0.02, 0.05, shares=0.0)
        with self.assertRaises(D.DCFInputError):
            D.DCFInputs(float("nan"), 0.09, 0.02, 0.05)
        with self.assertRaises(D.DCFInputError):
            D.DCFInputs(100.0, 0.09, 0.02, -1.0)
        with self.assertRaises(D.DCFInputError):
            D.per_share_vec(100.0, np.array([0.09, 0.01]), 0.02, 0.05)  # one bad row is enough

    def test_error_is_value_error(self):
        self.assertTrue(issubclass(D.DCFInputError, ValueError))


if __name__ == "__main__":
    unittest.main()
