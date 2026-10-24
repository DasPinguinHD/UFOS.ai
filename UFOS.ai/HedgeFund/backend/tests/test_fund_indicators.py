"""fund/indicators.py — pure indicator helpers (offline, deterministic).

Run from HedgeFund/backend:  python -m unittest tests.test_fund_indicators -v
"""
import unittest

from fund import indicators as ind
from strategy_platform.core.types import Bar


def _bars(closes, spread=1.0):
    return [Bar(date=f"2026-01-{i + 1:02d}", open=c, high=c + spread / 2, low=c - spread / 2, close=c)
            for i, c in enumerate(closes)]


class IndicatorTest(unittest.TestCase):
    def test_sma(self):
        self.assertEqual(ind.sma([1, 2, 3, 4], 2), 3.5)
        self.assertIsNone(ind.sma([1, 2], 3))
        self.assertEqual(ind.sma_series([1, 2, 3], 2), [None, 1.5, 2.5])

    def test_ema(self):
        self.assertAlmostEqual(ind.ema([5.0] * 30, 10), 5.0)
        # seed = SMA(1,2,3) = 2; alpha = 0.5 -> 0.5*4 + 0.5*2 = 3
        self.assertAlmostEqual(ind.ema([1, 2, 3, 4], 3), 3.0)
        self.assertIsNone(ind.ema([1], 3))

    def test_rsi_extremes(self):
        self.assertEqual(ind.rsi(list(range(1, 30)), 14), 100.0)
        self.assertAlmostEqual(ind.rsi(list(range(30, 1, -1)), 14), 0.0)
        self.assertEqual(ind.rsi([10.0] * 20, 14), 50.0)
        self.assertIsNone(ind.rsi([1, 2, 3], 14))

    def test_rsi_alternating_is_neutral(self):
        vals = [10 + (i % 2) for i in range(60)]
        self.assertAlmostEqual(ind.rsi(vals, 14), 50.0, delta=5.0)

    def test_atr_constant_range(self):
        bars = _bars([100.0] * 30, spread=2.0)
        self.assertAlmostEqual(ind.atr(bars, 14), 2.0)
        self.assertAlmostEqual(ind.atr_pct(bars, 14), 0.02)
        self.assertIsNone(ind.atr(bars[:10], 14))

    def test_atr_uses_gaps(self):
        bars = [Bar("d1", 100, 101, 99, 100), Bar("d2", 110, 111, 109, 110)]
        self.assertEqual(ind.true_ranges(bars), [11.0])

    def test_returns_and_pct_change(self):
        self.assertEqual(ind.returns([100, 110, 99]), [0.10000000000000009, -0.09999999999999998])
        self.assertAlmostEqual(ind.pct_change([100, 105, 110], 2), 0.10)
        self.assertIsNone(ind.pct_change([100], 1))

    def test_max_drawdown(self):
        self.assertAlmostEqual(ind.max_drawdown([100, 120, 90, 130, 65]), 0.5)
        self.assertEqual(ind.max_drawdown([1, 2, 3]), 0.0)
        self.assertEqual(ind.max_drawdown([]), 0.0)

    def test_sharpe_and_stdev(self):
        self.assertIsNone(ind.sharpe([0.01] * 10))
        self.assertGreater(ind.sharpe([0.01, 0.02, 0.0, 0.015]), 0)
        self.assertAlmostEqual(ind.stdev([2, 4, 4, 4, 5, 5, 7, 9]), 2.138, places=3)


if __name__ == "__main__":
    unittest.main()
