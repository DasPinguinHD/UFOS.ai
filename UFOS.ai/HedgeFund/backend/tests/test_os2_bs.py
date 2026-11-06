"""OS2 Black–Scholes–Merton (SP §21.12): prices against a textbook closed-form example, put-call parity, greeks
against finite differences, the IV round trip, no-root / bound cases (never clipped), the T-bill rate conversion by
hand, the parity dividend-yield cross-check."""
import math
import unittest

from strategy_platform.options_structure import bs


class PriceTest(unittest.TestCase):
    def test_textbook_example(self):
        # Hull, Options, Futures and Other Derivatives: S=42, K=40, r=10 %, T=0.5, σ=20 %, q=0 → c ≈ 4.76, p ≈ 0.81
        self.assertAlmostEqual(bs.price("call", 42, 40, 0.5, 0.10, 0.0, 0.20), 4.7594, places=3)
        self.assertAlmostEqual(bs.price("put", 42, 40, 0.5, 0.10, 0.0, 0.20), 0.8086, places=3)

    def test_put_call_parity_with_dividends(self):
        for S, K, T, r, q, v in ((600, 590, 0.08, 0.04, 0.012, 0.18), (500, 530, 0.3, 0.05, 0.006, 0.35),
                                 (100, 100, 0.002, 0.0, 0.0, 0.9)):
            c, p = bs.price("call", S, K, T, r, q, v), bs.price("put", S, K, T, r, q, v)
            self.assertAlmostEqual(c - p, S * math.exp(-q * T) - K * math.exp(-r * T), places=9)

    def test_greeks_vs_finite_differences(self):
        S, K, T, r, q, v = 600.0, 610.0, 0.1, 0.04, 0.012, 0.22
        h = 1e-3
        for kind in ("call", "put"):
            g = bs.greeks(kind, S, K, T, r, q, v)
            f = lambda s: bs.price(kind, s, K, T, r, q, v)   # noqa: E731
            self.assertAlmostEqual(g.delta, (f(S + h) - f(S - h)) / (2 * h), places=6)
            self.assertAlmostEqual(g.gamma, (f(S + h) - 2 * f(S) + f(S - h)) / (h * h), places=4)
            self.assertAlmostEqual(g.vega, (bs.price(kind, S, K, T, r, q, v + 1e-5) -
                                            bs.price(kind, S, K, T, r, q, v - 1e-5)) / 2e-5, places=4)
            dt = 1e-6
            self.assertAlmostEqual(g.theta, -(bs.price(kind, S, K, T + dt, r, q, v) -
                                              bs.price(kind, S, K, T - dt, r, q, v)) / (2 * dt), places=2)
            self.assertAlmostEqual(bs.gamma(S, K, T, r, q, v), g.gamma, places=14)
        # call and put gamma are equal
        self.assertAlmostEqual(bs.greeks("call", S, K, T, r, q, v).gamma, bs.greeks("put", S, K, T, r, q, v).gamma,
                               places=14)

    def test_dollar_gamma(self):
        self.assertAlmostEqual(bs.dollar_gamma(0.01, 600.0, 100), 0.01 * 100 * 600 * 600 * 0.01)


class ImpliedVolTest(unittest.TestCase):
    def test_round_trip(self):
        for kind, S, K, T, r, q, v in (("call", 600, 600, 0.05, 0.04, 0.012, 0.15),
                                       ("put", 600, 540, 0.02, 0.04, 0.012, 0.45),
                                       ("call", 500, 560, 0.12, 0.05, 0.006, 0.28),
                                       ("put", 500, 505, 0.0008, 0.05, 0.006, 1.2)):
            p = bs.price(kind, S, K, T, r, q, v)
            self.assertAlmostEqual(bs.implied_vol(kind, p, S, K, T, r, q), v, places=7)

    def test_no_root_cases_are_reasons_never_clipped(self):
        S, K, T, r, q = 600.0, 580.0, 0.05, 0.04, 0.0
        intrinsic = S - K * math.exp(-r * T)
        with self.assertRaises(bs.NoIV) as cm:
            bs.implied_vol("call", intrinsic - 0.5, S, K, T, r, q)       # below intrinsic
        self.assertIn("lower bound", str(cm.exception))
        with self.assertRaises(bs.NoIV) as cm:
            bs.implied_vol("call", S + 1, S, K, T, r, q)                 # above the upper bound S e^{-qT}
        self.assertIn("upper bound", str(cm.exception))
        p = bs.price("call", S, 600, T, r, q, 2.0)
        with self.assertRaises(bs.NoIV) as cm:
            bs.implied_vol("call", p, S, 600, T, r, q, lo=0.01, hi=1.0)  # true σ = 2 outside the bracket
        self.assertIn("no root", str(cm.exception))
        for bad in (0.0, -1.0, float("nan")):
            with self.assertRaises(bs.NoIV):
                bs.implied_vol("call", bad, S, K, T, r, q)
        with self.assertRaises(bs.NoIV):
            bs.implied_vol("call", 5.0, S, K, 0.0, r, q)                 # expired


class RateTest(unittest.TestCase):
    def test_discount_yield_by_hand(self):
        # DTB3 4.00 % (discount, ACT/360), 91 days: price = 1 − 0.04·91/360 = 0.989888…; r = −ln(price)·365/91
        price = 1 - 0.04 * 91 / 360
        self.assertAlmostEqual(bs.rate_from_discount_yield(4.0, 91), -math.log(price) * 365 / 91, places=14)
        self.assertAlmostEqual(bs.rate_from_discount_yield(0.0), 0.0)
        with self.assertRaises(ValueError):
            bs.rate_from_discount_yield(float("nan"))

    def test_parity_dividend_yield(self):
        S, K, T, r, q, v = 600.0, 600.0, 0.1, 0.04, 0.013, 0.2
        c, p = bs.price("call", S, K, T, r, q, v), bs.price("put", S, K, T, r, q, v)
        self.assertAlmostEqual(bs.parity_dividend_yield(c, p, S, K, T, r), q, places=10)


if __name__ == "__main__":
    unittest.main()
