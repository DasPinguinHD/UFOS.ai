"""strategy/validator.py + dsl.py: look-ahead ban, unknown names, cycles,
non-PIT ext, symbols, limits, warnings, facts, catalogue/expr_text. Offline
(fake external-feature catalogue).

Run from HedgeFund/backend:  python -m unittest tests.test_sp_strategy_validator -v
"""
import copy
import json
import unittest

try:
    from test_sp_strategy_support import EXT_CATALOGUE
except ImportError:  # pragma: no cover
    from tests.test_sp_strategy_support import EXT_CATALOGUE

from strategy_platform.strategy import dsl, limits, validator


def op(name, *args):
    return {"op": name, "args": list(args)}


C = {"field": "close"}


def spec(**over):
    s = {
        "schema": "ufos.strategy/1", "name": "Momentum", "description": "d", "tags": ["momentum"],
        "category": "momentum", "universe": {"symbols": ["SPY", "TLT", "GLD"]}, "timeframe": "1Day",
        "parameters": {"lookback": {"value": 252, "min": 126, "max": 378, "step": 21, "integer": True,
                                    "description": "x"}},
        "features": {"mom": op("pct_change", C, {"param": "lookback"})},
        "signal": op("cs_rank", {"ref": "mom"}),
        "portfolio": {"mode": "long_only", "selection": {"type": "top_n", "n": 2}, "weighting": "equal",
                      "vol_lookback": 60, "max_weight": 0.5, "gross_exposure": 1.0},
        "rebalance": {"frequency": "monthly"},
        "risk": {"stop_loss_pct": None, "take_profit_pct": None, "vol_target_annual": None,
                 "max_drawdown_stop_pct": None},
        "execution": {"order_type": "market", "timing": "next_open"},
        "costs": {"model": "paper_broker"},
    }
    s.update(over)
    return s


def check(s):
    return validator.validate(s, ext_catalogue=EXT_CATALOGUE)


class ValidSpecTest(unittest.TestCase):
    def test_valid(self):
        r = check(spec())
        self.assertTrue(r.ok, r.errors)
        self.assertEqual(r.warnings, [])
        f = r.facts
        self.assertEqual(f["max_lookback"], 378)
        self.assertEqual(f["required_fields"], ["close"])
        self.assertEqual(f["required_ext"], [])
        self.assertEqual(f["symbols_count"], 3)
        self.assertEqual(f["asset_classes"], ["equity"])
        self.assertEqual(f["param_count"], 1)
        self.assertGreater(f["node_count"], 30)
        self.assertEqual(set(r.to_dict()), {"ok", "errors", "warnings", "facts"})

    def test_minimal_spec_with_defaults(self):
        r = check({"schema": "ufos.strategy/1", "name": "x", "universe": {"symbols": ["SPY"]}, "signal": C})
        self.assertTrue(r.ok, r.errors)


class LookAheadTest(unittest.TestCase):
    def codes(self, signal, **over):
        return check(spec(signal=signal, features={}, parameters={}, **over)).codes()

    def test_lag_zero_or_negative(self):
        self.assertEqual(self.codes(op("lag", C, 0)), ["look_ahead"])
        self.assertEqual(self.codes(op("lag", C, -1)), ["look_ahead"])
        self.assertEqual(self.codes(op("delta", C, -5)), ["look_ahead"])

    def test_window_zero(self):
        self.assertEqual(self.codes(op("ts_mean", C, 0)), ["look_ahead"])
        self.assertEqual(self.codes(op("returns", 0)), ["look_ahead"])
        self.assertEqual(self.codes(op("ewm_mean", C, 0)), ["look_ahead"])

    def test_param_window_allowing_zero(self):
        s = spec(parameters={"n": {"value": 5, "min": 0, "max": 10, "integer": True}}, features={},
                 signal=op("lag", C, {"param": "n"}))
        self.assertEqual(check(s).codes(), ["look_ahead"])

    def test_same_bar_timing(self):
        for timing in ("same_bar", "close", "open", "now"):
            s = spec(execution={"order_type": "market", "timing": timing})
            self.assertEqual(check(s).codes(), ["bad_timing"], timing)
        self.assertTrue(check(spec(execution={"order_type": "market", "timing": "next_close"})).ok)

    def test_bad_windows(self):
        self.assertEqual(self.codes(op("ts_mean", C, 5.5)), ["bad_window"])
        self.assertEqual(self.codes(op("ts_mean", C, limits.MAX_WINDOW + 1)), ["bad_window"])
        self.assertEqual(self.codes(op("ts_std", C, 1)), ["bad_window"])
        self.assertEqual(self.codes(op("ts_mean", C, C)), ["bad_window"])
        self.assertEqual(self.codes(op("ts_mean", C, True)), ["bad_window"])
        s = spec(parameters={"n": {"value": 5.0, "min": 1.0, "max": 10.0}}, features={},
                 signal=op("ts_mean", C, {"param": "n"}))
        self.assertEqual(check(s).codes(), ["bad_window"])
        s = spec(parameters={"n": {"value": 5, "min": 1, "max": 3000, "integer": True}}, features={},
                 signal=op("ts_mean", C, {"param": "n"}))
        self.assertEqual(check(s).codes(), ["bad_window"])

    def test_constant_args(self):
        self.assertEqual(self.codes(op("pow", C, C)), ["bad_type"])
        self.assertTrue(check(spec(signal=op("pow", C, 2), features={}, parameters={})).ok)


class UnknownNamesTest(unittest.TestCase):
    def test_unknown_op_field_param_ref_ext(self):
        self.assertEqual(check(spec(signal=op("future_close", C))).codes(), ["unknown_op"])
        self.assertEqual(check(spec(signal={"field": "adj_close"})).codes(), ["unknown_field"])
        self.assertEqual(check(spec(signal=op("lag", C, {"param": "nope"}))).codes(), ["unknown_param"])
        self.assertEqual(check(spec(signal={"param": "nope"})).codes(), ["unknown_param"])
        self.assertEqual(check(spec(signal={"ref": "nope"})).codes(), ["unknown_ref"])
        self.assertEqual(check(spec(signal={"ext": "nope.feature"})).codes(), ["unknown_ext"])
        self.assertEqual(check(spec(signal={"param": {"x": 1}})).codes(), ["unknown_param"])

    def test_unknown_and_missing_keys(self):
        s = spec(extra=1)
        r = check(s)
        self.assertEqual(r.codes(), ["unknown_key"])
        self.assertEqual(r.errors[0]["path"], "$.extra")
        s = spec()
        del s["signal"]
        self.assertIn("missing_key", check(s).codes())
        r = check(spec(signal={"op": "neg", "args": [C], "lookahead": True}))
        self.assertEqual(r.codes(), ["unknown_key"])
        r = check(spec(signal={"field": "close", "shift": -1}))
        self.assertEqual(r.codes(), ["unknown_key"])
        s = spec()
        s["portfolio"]["leverage"] = 3
        self.assertEqual(check(s).codes(), ["unknown_key"])
        s = spec()
        s["parameters"]["lookback"]["secret"] = 1
        self.assertEqual(check(s).codes(), ["unknown_key"])

    def test_bad_arity_and_path(self):
        r = check(spec(signal=op("cs_rank", op("sub", C))))
        self.assertEqual(r.codes(), ["bad_arity"])
        self.assertEqual(r.errors[0]["path"], "$.signal.args[0].args")
        r = check(spec(signal=op("cs_rank", op("sub", C, op("lag", C, 0)))))
        self.assertEqual(r.errors[0]["path"], "$.signal.args[0].args[1].args[1]")
        self.assertEqual(check(spec(signal=op("add", *([C] * 17)))).codes(), ["bad_arity"])
        self.assertTrue(check(spec(signal=op("add", C, C, C), features={}, parameters={})).ok)

    def test_ref_cycles(self):
        s = spec(features={"a": {"ref": "b"}, "b": op("neg", {"ref": "a"})}, signal={"ref": "a"}, parameters={})
        self.assertEqual(check(s).codes(), ["ref_cycle"])
        s = spec(features={"a": op("neg", {"ref": "a"})}, signal={"ref": "a"}, parameters={})
        self.assertEqual(check(s).codes(), ["ref_cycle"])
        s = spec(features={"a": C, "b": op("neg", {"ref": "a"}), "c": op("add", {"ref": "a"}, {"ref": "b"})},
                 signal={"ref": "c"}, parameters={})
        self.assertTrue(check(s).ok)

    def test_ext_point_in_time(self):
        self.assertEqual(check(spec(signal={"ext": "test.leaky"})).codes(), ["not_point_in_time"])
        r = check(spec(signal=op("mul", C, {"ext": "calendar.event_risk"}), features={}, parameters={}))
        self.assertTrue(r.ok, r.errors)
        self.assertEqual(r.facts["required_ext"], ["calendar.event_risk"])

    def test_ext_catalogue_from_registry_objects(self):
        class F:
            def __init__(self, name, pit):
                self.name, self.point_in_time = name, pit
        r = validator.validate(spec(signal={"ext": "x.y"}, features={}, parameters={}),
                               ext_catalogue=[F("x.y", False)])
        self.assertEqual(r.codes(), ["not_point_in_time"])


class BlocksTest(unittest.TestCase):
    def test_symbols(self):
        self.assertEqual(check(spec(universe={"symbols": ["SPY", "bad symbol!"]})).codes(), ["bad_symbol"])
        self.assertEqual(check(spec(universe={"symbols": ["SPY", "spy"]})).codes(), ["bad_symbol"])
        many = [f"S{i}" for i in range(limits.MAX_SYMBOLS + 1)]
        self.assertEqual(check(spec(universe={"symbols": many})).codes(), ["too_many_symbols"])
        any_symbol = check(spec(universe={"symbols": ["ZZZZ", "ABC.B", "DOGE-USD", "SPY"]}))
        self.assertTrue(any_symbol.ok, any_symbol.errors)          # not restricted to known symbols
        self.assertEqual(check(spec(universe={"preset": "nope"})).codes(), ["out_of_range"])
        self.assertEqual(check(spec(universe={"preset": "liquid_etfs_crypto", "symbols": ["SPY"]})).codes(),
                         ["bad_type"])
        self.assertEqual(check(spec(universe={})).codes(), ["missing_key"])
        r = check(spec(universe={"preset": "liquid_etfs_crypto", "asset_classes": ["crypto"]}))
        self.assertEqual(r.facts["symbols_count"], 3)

    def test_texts_and_tags(self):
        self.assertEqual(check(spec(name="x" * 81)).codes(), ["text_too_long"])
        self.assertEqual(check(spec(description="x" * 4001)).codes(), ["text_too_long"])
        self.assertEqual(check(spec(tags=["x" * 33])).codes(), ["text_too_long"])
        self.assertEqual(check(spec(tags=["t"] * 17)).codes(), ["limit_exceeded"])
        self.assertEqual(check(spec(name="  ")).codes(), ["out_of_range"])
        s = spec()
        s["parameters"]["lookback"]["description"] = "x" * 301
        self.assertEqual(check(s).codes(), ["text_too_long"])
        s = spec(features={"f" * 41: C, "mom": op("pct_change", C, {"param": "lookback"})})
        self.assertIn("text_too_long", check(s).codes())
        self.assertEqual(check(spec(features={"Bad-Name": C, "mom": op("pct_change", C, {"param": "lookback"})}))
                         .codes(), ["bad_type"])

    def test_counts(self):
        ps = {f"p{i}": {"value": 1, "min": 0, "max": 2} for i in range(limits.MAX_PARAMS + 1)}
        self.assertEqual(check(spec(parameters=ps, features={}, signal=C)).codes(), ["limit_exceeded"])
        fs = {f"f{i}": C for i in range(limits.MAX_FEATURES + 1)}
        self.assertEqual(check(spec(features=fs, signal=C, parameters={})).codes(), ["limit_exceeded"])

    def test_numbers(self):
        r = check(spec(signal=op("mul", C, float("nan")), features={}, parameters={}))
        self.assertEqual(r.codes(), ["bad_type"])
        r = check(spec(signal=op("mul", C, float("inf")), features={}, parameters={}))
        self.assertEqual(r.codes(), ["bad_type"])
        s = spec()
        s["parameters"]["lookback"]["value"] = 400
        self.assertEqual(check(s).codes(), ["out_of_range"])
        s = spec()
        s["parameters"]["lookback"]["min"] = 500
        self.assertEqual(check(s).codes(), ["out_of_range"])
        s = spec()
        s["portfolio"]["max_weight"] = 0
        self.assertEqual(check(s).codes(), ["out_of_range"])
        s = spec()
        s["portfolio"]["selection"] = {"type": "top_n", "n": True}
        self.assertEqual(check(s).codes(), ["bad_type"])

    def test_portfolio_rules(self):
        s = spec()
        s["portfolio"]["selection"] = {"type": "top_bottom_n", "n": 2}
        self.assertEqual(check(s).codes(), ["out_of_range"])
        s["portfolio"]["mode"] = "long_short"
        self.assertTrue(check(s).ok)
        s = spec()
        s["portfolio"]["selection"] = {"type": "threshold", "short_below": 0}
        self.assertIn("missing_key", check(s).codes())
        s = spec()
        s["portfolio"]["selection"] = {"type": "top_n"}
        self.assertEqual(check(s).codes(), ["missing_key"])
        s = spec()
        s["portfolio"]["weighting"] = "inverse_vol"
        s["portfolio"]["vol_lookback"] = None
        self.assertEqual(check(s).codes(), ["missing_key"])
        s = spec(costs={"model": "custom", "fee_bps": 5})
        self.assertEqual(check(s).codes(), ["missing_key"])
        self.assertTrue(check(spec(costs={"model": "custom", "fee_bps": 5, "slippage_bps": 2})).ok)
        self.assertEqual(check(spec(rebalance={"frequency": "hourly"})).codes(), ["out_of_range"])
        self.assertEqual(check(spec(timeframe="1Hour")).codes(), ["out_of_range"])
        self.assertEqual(check(spec(schema="ufos.strategy/2")).codes(), ["out_of_range"])


class WarningsTest(unittest.TestCase):
    def wcodes(self, s):
        r = check(s)
        self.assertTrue(r.ok, r.errors)
        return [w["code"] for w in r.warnings]

    def test_unused(self):
        s = spec(parameters={"lookback": {"value": 20, "min": 10, "max": 30, "integer": True},
                             "extra": {"value": 1, "min": 0, "max": 2}},
                 features={"mom": op("pct_change", C, {"param": "lookback"}), "spare": C})
        self.assertEqual(sorted(self.wcodes(s)), ["unused_feature", "unused_param"])

    def test_mixed_and_crypto_shorts(self):
        s = spec(universe={"symbols": ["SPY", "BTC-USD"]})
        self.assertEqual(self.wcodes(s), ["mixed_calendars"])
        s["portfolio"]["mode"] = "long_short"
        s["portfolio"]["selection"] = {"type": "top_bottom_n", "n": 1}
        self.assertEqual(sorted(self.wcodes(s)), ["crypto_short_selling", "mixed_calendars"])

    def test_survivorship_and_short_history(self):
        s = spec(universe={"preset": "liquid_etfs_crypto", "asset_classes": ["equity"]})
        self.assertEqual(self.wcodes(s), ["survivorship"])
        s = spec(features={}, parameters={}, signal=op("pct_change", C, 800))
        self.assertEqual(self.wcodes(s), ["short_history"])

    def test_lookback_cap(self):
        deep = op("lag", op("lag", op("lag", C, 2520), 2520), 1)
        r = check(spec(features={}, parameters={}, signal=deep))
        self.assertEqual(r.codes(), ["limit_exceeded"])
        self.assertEqual(r.facts["max_lookback"], 5041)
        fs = {f"f{i}": op("lag", {"ref": f"f{i - 1}"} if i else C, 2520) for i in range(60)}
        r = check(spec(features=fs, parameters={}, signal={"ref": "f59"}))
        self.assertIn("limit_exceeded", r.codes())
        ok = op("lag", op("lag", C, 2520), 2520)
        self.assertTrue(check(spec(features={}, parameters={}, signal=ok)).ok)

    def test_lookback_facts(self):
        def lb(sig):
            return check(spec(features={}, parameters={}, signal=sig)).facts["max_lookback"]
        self.assertEqual(lb(op("lag", C, 5)), 5)
        self.assertEqual(lb(op("ts_mean", op("lag", C, 5), 10)), 14)
        self.assertEqual(lb(op("rsi", 14)), 56)
        self.assertEqual(lb(op("ewm_mean", C, 2.5)), 10)
        self.assertEqual(lb(op("ts_corr", op("lag", C, 3), op("returns", 7), 5)), 11)


class TextApiTest(unittest.TestCase):
    def test_validate_text(self):
        r = validator.validate_text(json.dumps(spec()), ext_catalogue=EXT_CATALOGUE)
        self.assertTrue(r.ok, r.errors)
        s, r = validator.parse_and_validate('{"a": 1, "a": 2}')
        self.assertIsNone(s)
        self.assertEqual(r.codes(), ["parse_error"])

    def test_validate_does_not_modify_input(self):
        s = spec()
        before = copy.deepcopy(s)
        check(s)
        self.assertEqual(s, before)

    def test_non_dict(self):
        self.assertEqual(validator.validate([1]).codes(), ["bad_type"])


class DslTest(unittest.TestCase):
    def test_catalogue(self):
        cat = dsl.catalogue()
        names = [c["name"] for c in cat]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(set(names), set(dsl.OPS))
        for required in ("add sub mul div neg abs sign log sqrt pow signed_power min max clip where gt ge lt le eq ne "
                         "and or not is_nan fill_nan lag delta pct_change ts_mean ts_std ts_sum ts_min ts_max "
                         "ts_argmin ts_argmax ts_rank ts_zscore ts_product ts_skew ts_corr ts_cov decay_linear "
                         "ewm_mean returns rsi atr sma cs_rank cs_zscore cs_demean cs_scale cs_winsorize").split():
            self.assertIn(required, names)
        for c in cat:
            self.assertEqual(set(c), {"name", "category", "signature", "description"})
        self.assertEqual(next(c for c in cat if c["name"] == "ts_corr")["signature"], "ts_corr(x, y, n: window)")

    def test_expr_text(self):
        node = op("cs_rank", op("sub", op("pct_change", C, 252), op("pct_change", C, 21)))
        self.assertEqual(dsl.expr_text(node), "cs_rank(pct_change(close, 252) - pct_change(close, 21))")
        self.assertEqual(dsl.expr_text(op("mul", op("add", C, 1), {"param": "k"})), "(close + 1) * $k")
        self.assertEqual(dsl.expr_text(op("sub", C, op("sub", {"ref": "a"}, -2))), "close - (a - (-2))")
        self.assertEqual(dsl.expr_text(op("gt", {"ext": "calendar.event_risk"}, 0.5)), "ext:calendar.event_risk > 0.5")
        self.assertEqual(dsl.expr_text(op("neg", op("add", C, C))), "-(close + close)")
        self.assertEqual(dsl.expr_text({"weird": 1}), "?")


if __name__ == "__main__":
    unittest.main()
