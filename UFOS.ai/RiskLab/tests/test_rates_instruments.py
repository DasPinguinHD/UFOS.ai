"""Instruments: cash flows against closed forms, repricing, the NMD replicating portfolio and its caps, the bank."""
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


class InstrumentTests(unittest.TestCase):
    def test_fixed_bond_cashflows(self):
        cf = I.FixedBond(100, 0.04, 3, freq=2).cashflows()
        np.testing.assert_allclose(cf.times, [0.5, 1, 1.5, 2, 2.5, 3])
        np.testing.assert_allclose(cf.amounts, [2, 2, 2, 2, 2, 102])
        self.assertEqual(I.FixedBond(100, 0.04, 3, side=-1).cashflows().amounts[-1], -104)

    def test_annuity_closed_form(self):
        loan = I.AmortisingLoan(1000, 0.06, 2, freq=12)
        i, n = 0.005, 24
        self.assertAlmostEqual(loan.payment(), 1000 * i / (1 - (1 + i) ** -n), places=10)
        self.assertAlmostEqual(loan.balances()[-1], 0.0, places=8)
        # PV of the payments at the loan's own rate (monthly compounding) is the notional
        y = 12 * math.log1p(i)
        self.assertAlmostEqual(R.pv(loan.cashflows(), C.NelsonSiegel(y, 0, 0, 1)), 1000, places=8)
        F = loan.repriced_fraction(np.array([0, 1 / 12, 1.0, 2.0]))
        self.assertEqual(F[0], 0.0)
        self.assertAlmostEqual(F[-1], 1.0)
        self.assertTrue(np.all(np.diff(F) > 0))

    def test_frn_prices_at_par_at_reset(self):
        flat = C.NelsonSiegel(0.03, 0, 0, 1)
        frn = I.FloatingRateNote(100, current_coupon=0.03, next_reset=0.25, reset_period=0.25)
        cf = frn.cashflows()
        self.assertEqual(cf.times.tolist(), [0.25])
        self.assertAlmostEqual(R.pv(cf, flat), 100 * 1.0075 * math.exp(-0.0075), places=10)
        with self.assertRaises(ValueError):
            I.FloatingRateNote(100, 0.03, next_reset=0.5, reset_period=0.25)

    def test_term_deposit(self):
        td = I.TermDeposit(100, 0.02, 2)
        self.assertAlmostEqual(td.cashflows().amounts[0], -100 * 1.02 ** 2)


class NMDTests(unittest.TestCase):
    def test_replicating_portfolio(self):
        d = I.NonMaturityDeposit(1000, core_share=0.6, avg_maturity=3.0, rate_=0.0)
        cf = d.cashflows()
        self.assertAlmostEqual(-cf.amounts.sum(), 1000, places=9)
        self.assertAlmostEqual(-cf.amounts[0], 400, places=9)
        self.assertAlmostEqual(cf.times[0], 1 / 365)
        core_t, core_a = cf.times[1:], -cf.amounts[1:]
        self.assertAlmostEqual(core_a.sum(), 600, places=9)
        self.assertAlmostEqual(float(np.average(core_t, weights=core_a)), 3.0 + 1 / 24, places=9)  # uniform tranches
        self.assertAlmostEqual(core_t[-1], 6.0)

    def test_caps(self):
        cap = I.NMD_CAPS["retail_transactional"]
        I.NonMaturityDeposit(100, cap["core_share"], cap["avg_maturity"])
        with self.assertRaises(ValueError):
            I.NonMaturityDeposit(100, 0.95, 3.0)
        with self.assertRaises(ValueError):
            I.NonMaturityDeposit(100, 0.5, 4.6, category="retail_non_transactional")
        I.NonMaturityDeposit(100, 0.95, 8.0, enforce_caps=False)
        self.assertIn("ASSUMPTION", I.NMD_CAPS_SOURCE)

    def test_pass_through_and_floor(self):
        base = C.NelsonSiegel(0.01, 0, 0, 1)
        up = C.NelsonSiegel(0.03, 0, 0, 1)
        down = C.NelsonSiegel(-0.01, 0, 0, 1)
        d = I.NonMaturityDeposit(100, 0.5, 2.0, rate_=0.002, beta=0.4)
        self.assertAlmostEqual(d.new_rate(base, up), 0.002 + 0.4 * 0.02)
        self.assertEqual(d.new_rate(base, down), 0.0)


class BalanceSheetTests(unittest.TestCase):
    def test_reference_bank(self):
        bs = I.synthetic_balance_sheet()
        t = bs.totals()
        self.assertAlmostEqual(t["assets"], 1000.0)
        self.assertAlmostEqual(t["equity"], 100.0)
        self.assertAlmostEqual(t["tier1"], 90.0)
        self.assertTrue(all(x.side == 1 for x in bs.assets) and all(x.side == -1 for x in bs.liabilities))

    def test_seeded_banks_and_overrides(self):
        a, b = I.synthetic_balance_sheet(5), I.synthetic_balance_sheet(5)
        self.assertEqual([x.cashflows().amounts.sum() for x in a.instruments],
                         [x.cashflows().amounts.sum() for x in b.instruments])
        bs = I.synthetic_balance_sheet(nmd_core_share=0.8, nmd_avg_maturity=4.0)
        nmd = [x for x in bs.liabilities if isinstance(x, I.NonMaturityDeposit)][0]
        self.assertEqual((nmd.core_share, nmd.avg_maturity), (0.8, 4.0))
        with self.assertRaises(ValueError):
            I.synthetic_balance_sheet(nmd_core_share=0.95)


if __name__ == "__main__":
    unittest.main()
