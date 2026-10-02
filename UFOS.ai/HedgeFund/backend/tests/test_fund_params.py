"""fund/params.py: defaults, limits, validation edge cases, merge, borrow rates.
Stdlib unittest, offline. Run from HedgeFund/backend:
python -m unittest tests.test_fund_params -v
"""
import copy
import unittest

from fund import params as P


class DefaultsTest(unittest.TestCase):
    def test_defaults_are_valid_and_complete(self):
        clean, errors = P.validate(P.defaults())
        self.assertEqual(errors, {})
        self.assertEqual(clean, P.DEFAULTS)
        self.assertEqual(set(P.LIMITS), set(P.DEFAULTS))

    def test_defaults_copy_is_deep(self):
        d = P.defaults()
        d["watchlist"].append("XXX")
        d["agent_weights"]["macro"] = 0
        self.assertNotIn("XXX", P.DEFAULTS["watchlist"])
        self.assertEqual(P.DEFAULTS["agent_weights"]["macro"], 1.0)

    def test_etf_symbols(self):
        for s in ("SPY", "XLRE", "UUP", "VNQ", "DIA"):
            self.assertIn(s, P.ETF_SYMBOLS)
        self.assertEqual(len(P.ETF_SYMBOLS), 28)   # SPEC §3 list


class ValidateTest(unittest.TestCase):
    def test_unknown_key_and_non_object(self):
        clean, errors = P.validate({"foo": 1, "fees_bps": 3})
        self.assertIn("foo", errors)
        self.assertEqual(clean, {})               # nothing applied when any error
        self.assertIn("_", P.validate([1, 2])[1])

    def test_numbers(self):
        self.assertEqual(P.validate({"interval_minutes": 5.0})[0], {"interval_minutes": 5})
        self.assertIsInstance(P.validate({"interval_minutes": 5.0})[0]["interval_minutes"], int)
        self.assertIn("interval_minutes", P.validate({"interval_minutes": 5.5})[1])
        self.assertIn("interval_minutes", P.validate({"interval_minutes": 0})[1])
        self.assertIn("interval_minutes", P.validate({"interval_minutes": 61})[1])
        self.assertIn("interval_minutes", P.validate({"interval_minutes": True})[1])   # bool is not a number
        self.assertIn("fees_bps", P.validate({"fees_bps": "5"})[1])
        self.assertIn("fees_bps", P.validate({"fees_bps": float("nan")})[1])
        self.assertIn("fees_bps", P.validate({"fees_bps": float("inf")})[1])
        self.assertEqual(P.validate({"fees_bps": 0})[0], {"fees_bps": 0.0})
        self.assertEqual(P.validate({"ai_daily_budget_usd": 50})[0], {"ai_daily_budget_usd": 50.0})
        self.assertIn("min_confidence", P.validate({"min_confidence": 96})[1])
        self.assertIn("change_threshold", P.validate({"change_threshold": 0.001})[1])

    def test_bool_and_enum(self):
        self.assertEqual(P.validate({"allow_shorts": False})[0], {"allow_shorts": False})
        self.assertIn("allow_shorts", P.validate({"allow_shorts": 0})[1])
        self.assertEqual(P.validate({"judge_model_mode": "premium_on_flip"})[0]["judge_model_mode"], "premium_on_flip")
        self.assertIn("judge_model_mode", P.validate({"judge_model_mode": "gpt"})[1])

    def test_watchlist(self):
        clean, errors = P.validate({"watchlist": [" spy", "SPY", "brk.b", "qqq"]})
        self.assertEqual(errors, {})
        self.assertEqual(clean["watchlist"], ["SPY", "BRK.B", "QQQ"])
        self.assertIn("watchlist", P.validate({"watchlist": []})[1])
        self.assertIn("watchlist", P.validate({"watchlist": ["1ABC"]})[1])
        self.assertIn("watchlist", P.validate({"watchlist": ["TOOLONGSYMBOL"]})[1])
        self.assertIn("watchlist", P.validate({"watchlist": ["SPY; DROP"]})[1])
        self.assertIn("watchlist", P.validate({"watchlist": "SPY"})[1])
        self.assertIn("watchlist", P.validate({"watchlist": [f"S{i}" for i in range(26)]})[1])
        self.assertEqual(P.validate({"watchlist": [f"S{i}" for i in range(25)]})[1], {})
        # duplicates don't count toward the limit
        self.assertEqual(P.validate({"watchlist": ["SPY"] * 30})[0]["watchlist"], ["SPY"])

    def test_not_shortable_may_be_empty(self):
        self.assertEqual(P.validate({"not_shortable": []})[0], {"not_shortable": []})
        self.assertEqual(P.validate({"not_shortable": ["gme", "GME"]})[0], {"not_shortable": ["GME"]})
        self.assertIn("not_shortable", P.validate({"not_shortable": ["$$"]})[1])

    def test_margin_cross_field(self):
        self.assertIn("maintenance_margin_pct",
                      P.validate({"maintenance_margin_pct": 60, "initial_margin_pct": 50})[1])
        # against the defaults (initial 50)
        self.assertIn("maintenance_margin_pct", P.validate({"maintenance_margin_pct": 55})[1])
        self.assertIn("initial_margin_pct", P.validate({"initial_margin_pct": 25})[1])   # maint default 30
        # against a given current version
        cur = P.merge(None, {"initial_margin_pct": 80})
        self.assertEqual(P.validate({"maintenance_margin_pct": 70}, cur)[1], {})
        self.assertEqual(P.validate({"maintenance_margin_pct": 50, "initial_margin_pct": 50})[1], {})

    def test_agent_weights(self):
        self.assertEqual(P.validate({"agent_weights": {"macro": 0}})[0], {"agent_weights": {"macro": 0.0}})
        self.assertIn("agent_weights", P.validate({"agent_weights": {"risk": 1}})[1])
        self.assertIn("agent_weights", P.validate({"agent_weights": {"macro": 1.5}})[1])
        self.assertIn("agent_weights", P.validate({"agent_weights": {"macro": True}})[1])
        self.assertIn("agent_weights", P.validate({"agent_weights": [1, 2]})[1])

    def test_borrow_rate_overrides(self):
        clean, errors = P.validate({"borrow_rate_overrides": {"gme": 25, "SPY": 0.5}})
        self.assertEqual(errors, {})
        self.assertEqual(clean["borrow_rate_overrides"], {"GME": 25.0, "SPY": 0.5})
        self.assertIn("borrow_rate_overrides", P.validate({"borrow_rate_overrides": {"GME": 101}})[1])
        self.assertIn("borrow_rate_overrides", P.validate({"borrow_rate_overrides": {"G M E": 1}})[1])
        self.assertIn("borrow_rate_overrides", P.validate({"borrow_rate_overrides": {"GME": "x"}})[1])


class MergeTest(unittest.TestCase):
    def test_merge_layers_and_does_not_mutate(self):
        cur = P.merge(None, {"fees_bps": 7.0, "agent_weights": {"sentiment": 0.0}})
        before = copy.deepcopy(cur)
        new = P.merge(cur, {"agent_weights": {"macro": 0.25}, "watchlist": ["SPY"]})
        self.assertEqual(cur, before)
        self.assertEqual(new["fees_bps"], 7.0)
        self.assertEqual(new["agent_weights"], {"technical": 1.0, "macro": 0.25, "sentiment": 0.0})
        self.assertEqual(new["watchlist"], ["SPY"])
        self.assertEqual(new["min_confidence"], 75)

    def test_merge_drops_unknown_and_fills_missing(self):
        merged = P.merge({"old_param": 1, "fees_bps": 1.0}, None)
        self.assertNotIn("old_param", merged)
        self.assertEqual(set(merged), set(P.DEFAULTS))


class BorrowRateTest(unittest.TestCase):
    def test_override_then_etf_then_stock(self):
        p = P.merge(None, {"borrow_rate_overrides": {"SPY": 1.25}})
        self.assertEqual(P.borrow_rate_for("SPY", p), 1.25)
        self.assertEqual(P.borrow_rate_for("qqq", p), 0.3)
        self.assertEqual(P.borrow_rate_for("AAPL", p), 3.0)
        self.assertTrue(P.is_etf("xlf"))
        self.assertFalse(P.is_etf("AAPL"))


class ReviewFixesParamsTest(unittest.TestCase):
    """Review 2026-09-26: huge JSON numbers are field errors (never an
    OverflowError -> 500); the heartbeat parameter."""

    def test_huge_numbers_are_field_errors(self):
        huge = 10 ** 400
        for body, key in (({"max_position_pct": huge}, "max_position_pct"),
                          ({"max_trades_per_day": huge}, "max_trades_per_day"),
                          ({"ai_daily_budget_usd": -huge}, "ai_daily_budget_usd"),
                          ({"agent_weights": {"technical": huge}}, "agent_weights"),
                          ({"borrow_rate_overrides": {"SPY": huge}}, "borrow_rate_overrides"),
                          ({"starting_capital": huge}, "starting_capital")):
            clean, errors = P.validate(body)
            self.assertEqual(clean, {}, body)
            self.assertIn(key, errors, body)

    def test_autopilot_heartbeat_minutes(self):
        self.assertEqual(P.DEFAULTS["autopilot_heartbeat_minutes"], 15)
        lim = P.LIMITS["autopilot_heartbeat_minutes"]
        self.assertEqual((lim["type"], lim["min"], lim["max"]), ("int", 5, 240))
        self.assertEqual(P.validate({"autopilot_heartbeat_minutes": 30})[0], {"autopilot_heartbeat_minutes": 30})
        for bad in (4, 241, 7.5, True, "15"):
            self.assertIn("autopilot_heartbeat_minutes", P.validate({"autopilot_heartbeat_minutes": bad})[1], bad)
        self.assertEqual(P.merge({"watchlist": ["SPY"]}, None)["autopilot_heartbeat_minutes"], 15)   # old versions


if __name__ == "__main__":
    unittest.main()
