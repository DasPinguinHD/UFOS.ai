"""strategy/alphas (library v1, SPEC §7.9) + universe.py + summary.py:
every entry validates with 0 errors, passes the causality property, yields
finite weights on synthetic data; metadata format; performance sanity
(63 symbols × 2600 days); universes payload; summary texts. Offline (fake
external feature values; the real registry is only asked for its catalogue).

Run from HedgeFund/backend:  python -m unittest tests.test_sp_alphas -v
"""
import json
import math
import random
import time
import unittest

import numpy as np

try:
    from test_sp_strategy_support import EXT_CATALOGUE, fake_ext_values, synthetic_panel
except ImportError:  # pragma: no cover
    from tests.test_sp_strategy_support import EXT_CATALOGUE, fake_ext_values, synthetic_panel

from strategy_platform.core.instruments import KNOWN_ETFS, instrument
from strategy_platform.strategy import alphas, portfolio, schema, summary, universe, validator
from strategy_platform.strategy.evaluate import evaluate

CLASSIC = {"tsmom_12m", "xsmom_12_1", "dual_momentum", "st_reversal_5d", "high_52w", "sma_50_200", "donchian_55_20",
           "rsi2_reversion", "bollinger_reversion", "low_volatility", "inverse_vol_allocation", "sector_rotation",
           "risk_on_off", "crypto_trend_vol_target", "vol_targeted_trend"}
ALPHA101 = {2, 3, 4, 6, 9, 12, 13, 14, 16, 33, 41, 53, 54, 101}


class LibraryContentTest(unittest.TestCase):
    def test_entries_and_metadata(self):
        keys = alphas.keys()
        self.assertEqual(len(keys), len(set(keys)))
        self.assertTrue(CLASSIC <= set(keys))
        self.assertTrue({f"alpha101_{n:03d}" for n in ALPHA101} <= set(keys))
        self.assertIn("agentic_committee_rules", keys)
        for e in alphas.library():
            self.assertEqual(set(e), {"key", "spec", "metadata"})
            m = e["metadata"]
            self.assertEqual(set(m), {"key", "family", "reference", "expected_holding_days", "notes"})
            self.assertEqual(m["key"], e["key"])
            self.assertIsInstance(m["expected_holding_days"], int)
            self.assertTrue(m["reference"] and m["notes"] and m["family"])
        self.assertEqual(alphas.metadata("tsmom_12m")["family"], "momentum")
        self.assertIsNone(alphas.get("nope"))

    def test_copies_are_independent(self):
        a = alphas.library()
        a[0]["spec"]["name"] = "changed"
        self.assertNotEqual(alphas.library()[0]["spec"]["name"], "changed")

    def test_every_spec_validates_with_zero_errors(self):
        for e in alphas.LIBRARY:
            r = validator.validate(e["spec"], ext_catalogue=EXT_CATALOGUE)
            self.assertEqual(r.errors, [], e["key"])
            self.assertEqual([w for w in r.warnings if w["code"] in ("unused_param", "unused_feature")], [],
                             e["key"])
            # stored form is already complete: defaults change nothing
            self.assertEqual(schema.with_defaults(e["spec"]), e["spec"], e["key"])

    def test_committee_validates_against_the_real_registry(self):
        e = alphas.get("agentic_committee_rules")
        r = validator.validate(e["spec"])       # strategy.features.catalogue() (B2) — catalogue only, no load
        self.assertEqual(r.errors, [])
        self.assertEqual(r.facts["required_ext"], ["calendar.event_risk", "macro.regime_score"])

    def test_council_rules_name_says_no_ai(self):
        # user decision 2026-10-06: renamed from "Agentic Committee (rules)", key unchanged
        e = alphas.get("agentic_committee_rules")
        self.assertEqual(e["spec"]["name"], "Council Rules (no AI)")
        self.assertIn("contains no AI", e["spec"]["description"])
        self.assertIn("Council of AIs", e["spec"]["description"])
        self.assertIn("no AI", e["metadata"]["notes"])
        self.assertFalse(any("Agentic Committee" in (x["spec"]["name"] + x["spec"].get("description", ""))
                             for x in alphas.library()))


class LibraryLabelsTest(unittest.TestCase):
    def test_momentum_labels_match_the_papers(self):
        ts = alphas.get("tsmom_12m")
        self.assertEqual(ts["spec"]["name"], "Time-series momentum 12m")
        self.assertNotIn("lag", json.dumps(ts["spec"]["features"]))           # MOP 2012: no skipped month
        xs = alphas.get("xsmom_12_1")
        self.assertIn("Carhart", xs["metadata"]["reference"])
        self.assertIn("12-1", xs["spec"]["name"])
        self.assertIn("lag", json.dumps(xs["spec"]["features"]))

    def test_library_cost_fits_500_symbols(self):
        from strategy_platform.strategy.evaluate import estimate_cost
        from strategy_platform.strategy.limits import MAX_EVAL_CELL_OPS
        for e in alphas.LIBRARY:
            self.assertLess(estimate_cost(e["spec"], (2600, 500)), MAX_EVAL_CELL_OPS, e["key"])


class LibraryCausalityTest(unittest.TestCase):
    def test_causality_and_finite_weights(self):
        rng = random.Random(17)
        p = synthetic_panel(330, nan_rate=0.002, seed=5,
                            symbols=["SPY", "TLT", "GLD", "EFA", "AGG", "XLK", "XLE", "BTCX", "IEF", "DBC"])
        ext = fake_ext_values(p)
        for e in alphas.LIBRARY:
            s = e["spec"]
            full = evaluate(s, p, ext_values=ext)
            self.assertEqual(full.shape, p.shape)
            ts = {len(p) - 1, rng.randrange(0, 60), rng.randrange(260, len(p) - 1)}
            for t in sorted(ts):
                part = evaluate(s, p.head(t + 1), ext_values=ext)
                np.testing.assert_allclose(part[t], full[t], rtol=1e-9, atol=1e-9, equal_nan=True,
                                           err_msg=f"{e['key']} t={t}")
            t = len(p) - 1
            self.assertTrue(np.isfinite(full[t]).any(), e["key"])
            w = portfolio.target_weights(s, full[t], p, t)
            self.assertTrue(all(math.isfinite(v) for v in w.values()), e["key"])
            self.assertLessEqual(sum(abs(v) for v in w.values()), s["portfolio"]["gross_exposure"] + 1e-9)
            self.assertTrue(all(abs(v) <= s["portfolio"]["max_weight"] + 1e-12 for v in w.values()))


class PerformanceTest(unittest.TestCase):
    def test_63_symbols_2600_days(self):
        syms = universe.resolve({"preset": "liquid_etfs_crypto"})
        self.assertEqual(len(syms), 63)
        p = synthetic_panel(2600, symbols=[s.replace("-", "") for s in syms], seed=1)
        ext = fake_ext_values(p)
        for key in ("agentic_committee_rules", "low_volatility", "alpha101_013"):
            t0 = time.perf_counter()
            evaluate(alphas.get(key)["spec"], p, ext_values=ext)
            self.assertLess(time.perf_counter() - t0, 5.0, key)


class UniverseTest(unittest.TestCase):
    def test_preset(self):
        u = universe.universes()
        self.assertEqual([x["key"] for x in u], ["liquid_etfs_crypto"])
        entries = u[0]["symbols"]
        self.assertEqual(len(entries), 63)
        self.assertEqual({e["category"] for e in entries}, set(universe.CATEGORIES))
        for e in entries:
            self.assertEqual(set(e), {"symbol", "category", "asset_class"})
            self.assertEqual(instrument(e["symbol"]).asset_class, e["asset_class"])
            if e["asset_class"] == "equity":
                self.assertIn(e["symbol"], KNOWN_ETFS)
                self.assertEqual(instrument(e["symbol"]).kind, "etf")
        self.assertEqual(universe.resolve({"preset": "liquid_etfs_crypto", "asset_classes": ["crypto"]}),
                         ["BTC-USD", "ETH-USD", "SOL-USD"])
        self.assertEqual(universe.resolve({"symbols": ["spy", "btc-usd"]}), ["SPY", "BTC-USD"])
        self.assertEqual(universe.category_of("TLT"), "bonds")
        with self.assertRaises(ValueError):
            universe.resolve({"preset": "nope"})


class SummaryTest(unittest.TestCase):
    def test_summary_texts(self):
        s = alphas.get("xsmom_12_1")["spec"]
        t = summary.summary_texts(s)
        self.assertEqual(set(t), {"universe_text", "signal_text", "features_text", "portfolio_text",
                                  "rebalance_text", "risk_text"})
        self.assertIn("60 symbols", t["universe_text"])
        self.assertEqual(t["signal_text"], "cs_rank(mom)")
        self.assertEqual(t["features_text"], {"mom": "pct_change(lag(close, $skip), $formation)"})
        self.assertIn("top 10", t["portfolio_text"])
        self.assertIn("Monthly", t["rebalance_text"])
        self.assertEqual(t["risk_text"], "No risk overlays")
        v = summary.summary_texts(alphas.get("vol_targeted_trend")["spec"])
        self.assertIn("volatility target 10% p.a.", v["risk_text"])
        m = summary.summary_texts({"schema": "ufos.strategy/1", "name": "x", "universe": {"symbols": ["SPY"]},
                                   "signal": {"field": "close"}})
        self.assertEqual(m["universe_text"], "1 symbol: SPY")
        self.assertIn("next open", m["rebalance_text"])


if __name__ == "__main__":
    unittest.main()
