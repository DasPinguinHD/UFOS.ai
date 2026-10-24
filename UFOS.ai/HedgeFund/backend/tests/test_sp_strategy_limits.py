"""strategy/limits.py + schema.py: safe parsing limits with stable codes,
canonical JSON and the content hash. Offline.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_strategy_limits -v
"""
import json
import unittest

from strategy_platform.strategy import limits, schema, validator
from strategy_platform.strategy.limits import SpecParseError, parse_spec_text


def nested(depth):
    """JSON text with `depth` nested objects."""
    return '{"a":' * (depth - 1) + "{}" + "}" * (depth - 1)


def base_spec():
    return {"schema": "ufos.strategy/1", "name": "Test", "universe": {"symbols": ["SPY", "TLT"]},
            "signal": {"op": "pct_change", "args": [{"field": "close"}, 20]},
            "portfolio": {"mode": "long_only", "selection": {"type": "top_n", "n": 1}}}


class ParseLimitsTest(unittest.TestCase):
    def code(self, text):
        with self.assertRaises(SpecParseError) as cm:
            parse_spec_text(text)
        return cm.exception.code

    def test_oversize_text(self):
        text = '{"name": "' + "x" * limits.MAX_TEXT_BYTES + '"}'
        self.assertEqual(self.code(text), "limit_exceeded")
        r = validator.validate_text(text)
        self.assertEqual(r.codes(), ["limit_exceeded"])

    def test_multibyte_counts_bytes(self):
        text = '{"d": "' + "é" * (limits.MAX_TEXT_BYTES // 2) + '"}'
        self.assertEqual(self.code(text), "limit_exceeded")

    def test_depth(self):
        self.assertIsInstance(parse_spec_text(nested(32)), dict)
        self.assertEqual(self.code(nested(33)), "limit_exceeded")
        # brackets inside strings do not count
        self.assertIsInstance(parse_spec_text('{"a": "' + "[" * 100 + '"}'), dict)

    def test_huge_depth_never_recurses(self):
        self.assertEqual(self.code("[" * 100000), "limit_exceeded")

    def test_nodes(self):
        ok = json.dumps({"x": [0] * (limits.MAX_NODES - 2)})        # root + list + items = MAX_NODES
        self.assertIsInstance(parse_spec_text(ok), dict)
        self.assertEqual(self.code(json.dumps({"x": [0] * (limits.MAX_NODES - 1)})), "limit_exceeded")

    def test_duplicate_keys(self):
        self.assertEqual(self.code('{"a": 1, "a": 2}'), "parse_error")
        self.assertEqual(self.code('{"b": {"a": 1, "a": 1}}'), "parse_error")

    def test_nan_and_infinity_literals(self):
        for lit in ("NaN", "Infinity", "-Infinity"):
            self.assertEqual(self.code('{"a": %s}' % lit), "parse_error")

    def test_syntax_error_has_line_and_column(self):
        r = validator.validate_text('{\n  "a": 1,\n  "b": \n}')
        self.assertFalse(r.ok)
        e = r.errors[0]
        self.assertEqual(e["code"], "parse_error")
        self.assertEqual(e["line"], 4)
        self.assertIn("column", e)

    def test_non_object_and_non_text(self):
        self.assertEqual(self.code("[1, 2]"), "parse_error")
        self.assertEqual(self.code("3"), "parse_error")
        self.assertEqual(self.code(b"{}"), "parse_error")

    def test_dict_input_limits(self):
        deep = {}
        cur = deep
        for _ in range(40):
            cur["a"] = {}
            cur = cur["a"]
        self.assertEqual(validator.validate(deep).codes(), ["limit_exceeded"])
        self.assertEqual(validator.validate({"x": list(range(3000))}).codes(), ["limit_exceeded"])


class UnicodeTest(unittest.TestCase):
    def test_lone_surrogates_rejected(self):
        for text in ('{"name": "a\\ud800"}', '{"x": ["ok", "\\udfff"]}', '{"\\ud800k": 1}'):
            with self.assertRaises(SpecParseError) as cm:
                parse_spec_text(text)
            self.assertEqual(cm.exception.code, "parse_error")
            self.assertEqual(validator.validate_text(text).codes(), ["parse_error"])
        self.assertEqual(parse_spec_text('{"a": "\\ud83d\\ude00"}'), {"a": "\U0001F600"})   # a valid pair

    def test_dict_input_with_surrogate(self):
        s = base_spec()
        s["name"] = "bad\ud800"
        r = validator.validate(s)
        self.assertEqual(r.codes(), ["bad_type"])
        self.assertEqual(r.errors[0]["path"], "$['name']")


class HashTest(unittest.TestCase):
    def test_stable_across_key_order_and_whitespace(self):
        s = base_spec()
        text1 = json.dumps(s)
        text2 = json.dumps(dict(reversed(list(s.items()))), indent=4)
        self.assertEqual(schema.content_hash(parse_spec_text(text1)), schema.content_hash(parse_spec_text(text2)))
        self.assertEqual(len(schema.content_hash(s)), 64)

    def test_provenance_excluded_and_defaults_included(self):
        s = base_spec()
        h = schema.content_hash(s)
        s2 = dict(s, provenance={"claimed_by": "someone"})
        self.assertEqual(schema.content_hash(s2), h)
        self.assertEqual(schema.content_hash(schema.with_defaults(s)), h)   # stored (default-filled) form
        s3 = dict(s, name="Other")
        self.assertNotEqual(schema.content_hash(s3), h)
        s4 = schema.with_defaults(s)
        s4["rebalance"]["frequency"] = "weekly"
        self.assertNotEqual(schema.content_hash(s4), h)

    def test_canonical_json(self):
        self.assertEqual(schema.canonical_json({"b": 1, "a": [1, {"d": "é", "c": 2}]}),
                         '{"a":[1,{"c":2,"d":"é"}],"b":1}')

    def test_defaults_written(self):
        full = schema.with_defaults(base_spec())
        self.assertEqual(full["execution"], {"order_type": "market", "timing": "next_open"})
        self.assertEqual(full["portfolio"]["weighting"], "equal")
        self.assertEqual(full["portfolio"]["selection"], {"type": "top_n", "n": 1})
        self.assertEqual(full["costs"], {"model": "paper_broker"})
        u = schema.with_defaults(dict(base_spec(), universe={"preset": "liquid_etfs_crypto"}))
        self.assertEqual(u["universe"]["asset_classes"], ["equity", "crypto"])
        self.assertTrue(validator.validate(full, ext_catalogue=[]).ok)

    def test_pretty_json_roundtrip(self):
        s = schema.with_defaults(base_spec())
        self.assertEqual(json.loads(schema.pretty_json(s)), s)
        self.assertTrue(schema.pretty_json(s).startswith('{\n  "schema"'))


if __name__ == "__main__":
    unittest.main()
