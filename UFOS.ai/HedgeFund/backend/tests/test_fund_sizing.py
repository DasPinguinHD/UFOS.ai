"""fund/sizing.py — target weight and the trade decision (SPEC §9). Offline.

Run from HedgeFund/backend:  python -m unittest tests.test_fund_sizing -v
"""
import unittest

from strategy_platform.core import params as P
from fund.sizing import plan_trade, target_weight, vol_scaler
from strategy_platform.core.types import JudgeDecision, PositionView, RiskAssessment

MON = "2026-10-05T15:00:00+00:00"
TUE = "2026-10-06T15:00:00+00:00"


def dec(action, conf, sym="SPY"):
    return JudgeDecision(symbol=sym, action=action, confidence=conf, rationale="", source="rules")


def params(**kw):
    return P.merge(None, kw)


class TargetWeightTest(unittest.TestCase):
    def test_basic_and_vol_scaler(self):
        self.assertAlmostEqual(target_weight(dec("LONG", 0.8), None, 0.01, params()), 0.04)
        self.assertAlmostEqual(target_weight(dec("LONG", 0.8), None, 0.03, params()), 0.02)
        self.assertAlmostEqual(target_weight(dec("SHORT", 0.8), None, None, params()), -0.04)
        self.assertAlmostEqual(vol_scaler(0.0), 1.0)
        rm = RiskAssessment("SPY", size_multiplier=0.5)
        self.assertAlmostEqual(target_weight(dec("LONG", 1.0), rm, 0.01, params(max_position_pct=10)), 0.05)

    def test_flat_and_low_confidence(self):
        self.assertEqual(target_weight(dec("FLAT", 0.1), None, 0.01, params(), current_weight=0.03), 0.0)
        self.assertEqual(target_weight(dec("LONG", 0.6), None, 0.01, params()), 0.0)            # new: not taken
        self.assertEqual(target_weight(dec("LONG", 0.6), None, 0.01, params(), 0.02), 0.02)      # existing kept
        self.assertEqual(target_weight(dec("SHORT", 0.6), None, 0.01, params(), 0.02), 0.02)     # weak reversal: kept
        self.assertAlmostEqual(target_weight(dec("SHORT", 0.9), None, 0.01, params(), 0.02), -0.045)

    def test_short_permissions(self):
        self.assertEqual(target_weight(dec("SHORT", 0.9), RiskAssessment("SPY", veto_short=True), 0.01, params()), 0.0)
        self.assertEqual(target_weight(dec("SHORT", 0.9), None, 0.01, params(allow_shorts=False), -0.03), 0.0)
        self.assertEqual(target_weight(dec("SHORT", 0.9), None, 0.01, params(not_shortable=["SPY"])), 0.0)

    def test_veto_and_block_increase(self):
        veto = RiskAssessment("SPY", veto=True)
        self.assertEqual(target_weight(dec("LONG", 0.9), veto, 0.01, params()), 0.0)
        self.assertAlmostEqual(target_weight(dec("LONG", 0.9), veto, 0.01, params(), 0.06), 0.045)   # may shrink
        block = RiskAssessment("SPY", block_increase=True)
        self.assertAlmostEqual(target_weight(dec("LONG", 0.9), block, 0.01, params(), 0.02), 0.02)
        self.assertEqual(target_weight(dec("SHORT", 0.9), block, 0.01, params(), 0.02), 0.0)


class PlanTradeTest(unittest.TestCase):
    def test_threshold(self):
        self.assertEqual(plan_trade(dec("LONG", 0.8), None, {"weight": 0.035}, 0.01, params(), MON, 0),
                         (None, "no change needed"))
        self.assertEqual(plan_trade(dec("LONG", 0.8), None, None, 0.01, params(), MON, 0), (0.04, None))
        # closing a tiny position is a sign change -> trades
        self.assertEqual(plan_trade(dec("FLAT", 0.2), None, {"weight": 0.004}, 0.01, params(), MON, 0), (0.0, None))
        t, why = plan_trade(dec("LONG", 0.6), None, None, 0.01, params(), MON, 0)
        self.assertIsNone(t)
        self.assertEqual(why, "confidence 60% below minimum 75%")
        self.assertEqual(plan_trade(dec("SHORT", 0.9), RiskAssessment("SPY", veto_short=True), None, 0.01, params(),
                                    MON, 0), (None, "short not allowed"))

    def test_min_hold(self):
        pos = {"weight": 0.04, "opened_at": "2026-10-05T14:00:00+00:00"}
        t, why = plan_trade(dec("FLAT", 0.9), None, pos, 0.01, params(min_hold_days=1), MON, 0)
        self.assertIsNone(t)
        self.assertTrue(why.startswith("min hold"))
        self.assertEqual(plan_trade(dec("FLAT", 0.9), None, pos, 0.01, params(min_hold_days=1), TUE, 0), (0.0, None))
        self.assertEqual(plan_trade(dec("FLAT", 0.9), None, pos, 0.01, params(min_hold_days=1), MON, 0,
                                    enforce_min_hold=False), (0.0, None))
        # increasing is allowed during the hold period
        self.assertEqual(plan_trade(dec("LONG", 1.0), None, {"weight": 0.02, "opened_at": MON}, 0.01,
                                    params(min_hold_days=3), MON, 0), (0.05, None))

    def test_trade_limit(self):
        self.assertEqual(plan_trade(dec("LONG", 0.8), None, None, 0.01, params(max_trades_per_day=2), MON, 2),
                         (None, "daily trade limit"))
        self.assertEqual(plan_trade(dec("LONG", 0.8), None, None, 0.01, params(max_trades_per_day=2), MON, 2,
                                    enforce_trade_limit=False), (0.04, None))

    def test_position_view_needs_equity(self):
        pv = PositionView("SPY", qty=10, avg_price=400.0, last_price=400.0, opened_at="2026-10-01T15:00:00+00:00")
        t, why = plan_trade(dec("SHORT", 0.8), None, pv, 0.01, params(), MON, 0, equity=100000.0)
        self.assertEqual((t, why), (-0.04, None))
        with self.assertRaises(ValueError):
            plan_trade(dec("SHORT", 0.8), None, pv, 0.01, params(), MON, 0)


class ReviewFixesSizingTest(unittest.TestCase):
    """Review 2026-09-26: short cap blocks only increases, min-confidence
    before the short permission, flips need two orders of the daily limit."""

    def test_short_cap_blocks_only_increases(self):
        cap = RiskAssessment("SPY", block_short_increase=True)
        # an existing short is held (not closed) and may shrink, never grow
        self.assertAlmostEqual(target_weight(dec("SHORT", 0.9), cap, 0.01, params(), -0.03), -0.03)
        self.assertAlmostEqual(target_weight(dec("SHORT", 0.9), cap, 0.01, params(), -0.06), -0.045)
        # no new short; a long is closed, not reversed
        self.assertEqual(target_weight(dec("SHORT", 0.9), cap, 0.01, params()), 0.0)
        self.assertEqual(target_weight(dec("SHORT", 0.9), cap, 0.01, params(), 0.03), 0.0)
        # longs are unaffected
        self.assertAlmostEqual(target_weight(dec("LONG", 0.9), cap, 0.01, params()), 0.045)
        self.assertEqual(plan_trade(dec("SHORT", 0.9), cap, None, 0.01, params(), MON, 0),
                         (None, "short exposure cap reached"))
        self.assertEqual(plan_trade(dec("SHORT", 0.9), cap, {"weight": -0.03}, 0.01, params(), MON, 0),
                         (None, "no change needed"))

    def test_weak_short_signal_never_closes_a_long(self):
        for p in (params(not_shortable=["SPY"]), params(allow_shorts=False)):
            self.assertEqual(target_weight(dec("SHORT", 0.6), None, 0.01, p, 0.03), 0.03)
            self.assertEqual(target_weight(dec("SHORT", 0.6), RiskAssessment("SPY", veto_short=True), 0.01, p, 0.03),
                             0.03)
            self.assertEqual(target_weight(dec("SHORT", 0.9), None, 0.01, p, 0.03), 0.0)   # confident: exit

    def test_flip_needs_two_orders(self):
        p = params(max_trades_per_day=4)
        long_pos = {"weight": 0.04}
        self.assertEqual(plan_trade(dec("SHORT", 0.9), None, long_pos, 0.01, p, MON, 2), (-0.045, None))
        self.assertEqual(plan_trade(dec("SHORT", 0.9), None, long_pos, 0.01, p, MON, 3),
                         (0.0, "daily trade limit: only the close fits today"))
        self.assertEqual(plan_trade(dec("SHORT", 0.9), None, long_pos, 0.01, p, MON, 4), (None, "daily trade limit"))
        # a plain close or open needs one order
        self.assertEqual(plan_trade(dec("FLAT", 0.9), None, long_pos, 0.01, p, MON, 3), (0.0, None))
        self.assertEqual(plan_trade(dec("LONG", 0.8), None, None, 0.01, p, MON, 3), (0.04, None))
        # manual accept ignores the limit
        self.assertEqual(plan_trade(dec("SHORT", 0.9), None, long_pos, 0.01, p, MON, 9, enforce_trade_limit=False),
                         (-0.045, None))


if __name__ == "__main__":
    unittest.main()
