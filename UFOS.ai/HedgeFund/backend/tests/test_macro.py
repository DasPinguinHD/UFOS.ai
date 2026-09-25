"""Offline tests for the Macro Indicators section: stability rules (single
source for logic + popup text), derived indicators, history context, regime
map and one full FRED poll with a fake API. Stdlib unittest only; no network.

Run from HedgeFund/backend:  python -m unittest tests.test_macro -v
"""
import asyncio
import importlib.util
import os
import sys
import types
import unittest

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_NEWSROOM = os.path.join(_BACKEND, "newsroom")
_NAMES = ("aiohttp", "config", "newsroom", "newsroom.store", "newsroom.macro_rules",
          "newsroom.macro_derived", "newsroom.fred_collector")


def _load():
    saved = {n: sys.modules.get(n) for n in _NAMES}
    try:
        aiohttp = types.ModuleType("aiohttp")
        aiohttp.ClientSession = object
        aiohttp.ClientTimeout = lambda **kw: None
        sys.modules["aiohttp"] = aiohttp
        config = types.ModuleType("config")
        config.FRED_API_KEY = "test"
        config.FRED_POLL_SECONDS = 60
        sys.modules["config"] = config
        pkg = types.ModuleType("newsroom")
        pkg.__path__ = [_NEWSROOM]
        sys.modules["newsroom"] = pkg
        store = types.ModuleType("newsroom.store")
        store.make_event = lambda **kw: {"id": kw.get("event_id"), **kw}
        sys.modules["newsroom.store"] = store
        mods = {}
        for name in ("macro_rules", "macro_derived", "fred_collector"):
            spec = importlib.util.spec_from_file_location(f"newsroom.{name}", os.path.join(_NEWSROOM, f"{name}.py"))
            mod = importlib.util.module_from_spec(spec)
            sys.modules[f"newsroom.{name}"] = mod
            spec.loader.exec_module(mod)
            mods[name] = mod
        return mods
    finally:
        for n, m in saved.items():
            if m is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = m


M = _load()
rules, derived, fred = M["macro_rules"], M["macro_derived"], M["fred_collector"]


def monthly(values, start_year=2000):
    return [{"date": f"{start_year + i // 12}-{i % 12 + 1:02d}-01", "value": v} for i, v in enumerate(values)]


class RulesTest(unittest.TestCase):
    def test_every_card_series_has_rules_and_text(self):
        for sid, *_ in fred.SERIES:
            d = rules.describe_rules(sid)
            self.assertTrue(d["watch"] and d["alert"], sid)

    def test_levels(self):
        self.assertEqual(rules.stability_signal("DGS10", monthly([4.2] * 24))["level"], "ok")
        self.assertEqual(rules.stability_signal("DGS10", monthly([4.9] * 23 + [5.1]))["level"], "watch")
        self.assertEqual(rules.stability_signal("DGS10", monthly([5.4] * 23 + [5.6]))["level"], "alert")
        self.assertEqual(rules.stability_signal("T10Y2Y", monthly([-0.2] * 24))["level"], "watch")
        self.assertEqual(rules.stability_signal("T10Y2Y", monthly([-0.7] * 24))["level"], "alert")
        self.assertEqual(rules.stability_signal("CPIAUCSL", monthly([0.5] * 24))["level"], "watch")
        self.assertEqual(rules.stability_signal("VIXCLS", monthly([15] * 23 + [31]))["level"], "alert")

    def test_moves(self):
        # 10Y moving +0.6 pp inside the window -> watch even below 5 %
        sig = rules.stability_signal("DGS10", monthly([4.0] * 23 + [4.6]))
        self.assertEqual(sig["level"], "watch")
        self.assertIn("+0.60", sig["reason"])
        # Fed funds cut by 1.33 pp in 12 months -> watch
        self.assertEqual(rules.stability_signal("FEDFUNDS", monthly([5.33] * 12 + [4.0] * 12))["level"], "watch")

    def test_description_matches_thresholds(self):
        d = rules.describe_rules("DGS10")
        self.assertEqual(d["watch"], "≥ 5 % or ±0.5 pp in ~5 weeks")
        self.assertEqual(d["alert"], "≥ 5.5 % or ±0.75 pp in ~5 weeks")
        self.assertIn("inverted within 24 months", rules.describe_rules("CURVE_UNINVERSION")["alert"])


class DerivedTest(unittest.TestCase):
    def test_official_sahm_preferred(self):
        full = {"UNRATE": monthly([4.0] * 48)}
        official = monthly([0.1] * 30 + [0.55])
        out = derived.compute(full, [], official)
        self.assertEqual(out["SAHM_RULE"][-1]["value"], 0.55)
        m = derived.to_macro("SAHM_RULE", out["SAHM_RULE"])["macro"]
        self.assertEqual(m["signal"]["level"], "alert")
        self.assertIn("SAHMREALTIME", m["formula"])

    def test_sahm_fallback(self):
        out = derived.compute({"UNRATE": monthly([3.7] * 36 + [4.3] * 12)}, [], None)
        m = derived.to_macro("SAHM_RULE", out["SAHM_RULE"])["macro"]
        self.assertIn("own calculation", m["formula"])

    def test_real_policy_rate_uses_core_pce(self):
        full = {"FEDFUNDS": monthly([4.33] * 48), "PCEPILFE": monthly([2.8] * 47), "CPIAUCSL": monthly([3.0] * 48)}
        m = derived.to_macro("REAL_POLICY_RATE", derived.compute(full, [])["REAL_POLICY_RATE"])["macro"]
        self.assertAlmostEqual(m["value"], 1.53)
        self.assertIn("core PCE", m["formula"])

    def test_curve_uninversion(self):
        curve = monthly([-0.8] * 14 + [-0.3] * 8 + [0.1, 0.3] * 7)
        m = derived.to_macro("CURVE_UNINVERSION", derived.compute({}, curve)["CURVE_UNINVERSION"])["macro"]
        self.assertEqual(m["signal"]["level"], "alert")

    def test_regime_is_smoothed(self):
        # Industrial production alternating wildly around a flat trend must not flip the quadrant
        pay = monthly([2.0] * 48)
        ind = monthly([1.0 + (0.8 if i % 2 else -0.8) for i in range(48)])
        pce = monthly([2.5 + 0.02 * i for i in range(48)])
        r = derived.regime({"PCEPILFE": pce}, {"PAYEMS": pay, "INDPRO": ind})
        quadrants = {p["quadrant"] for p in r["trail"]}
        self.assertLessEqual(len(quadrants), 2, quadrants)
        self.assertIn("smoothed", r["method"])


class ContextTest(unittest.TestCase):
    def test_percentile_and_highest_since(self):
        hist = monthly([3.0] * 90 + [5.2] + [3.5] * 200 + [5.1])
        ctx = fred.history_context(hist, 5.1)
        self.assertGreaterEqual(ctx["percentile"], 99)
        self.assertIn("highest since Jul 2007", ctx["text"])

    def test_no_extreme_mid_range(self):
        hist = monthly([float(i % 10) for i in range(120)])
        ctx = fred.history_context(hist, 5.0)
        self.assertNotIn("since 20", ctx["text"].split("·")[-1] if "·" in ctx["text"] else "")

    def test_short_history_gives_nothing(self):
        self.assertEqual(fred.history_context(monthly([1.0] * 10), 1.0), {})


class PollTest(unittest.TestCase):
    def test_full_poll_with_fake_api(self):
        async def fake_series(session, sid, units, points_wanted=24, frequency=None):
            base = {"T10Y2Y": 0.3, SAHM: -0.05}.get(sid, 3.0)
            return monthly([base + 0.001 * i for i in range(points_wanted)], 2023)

        async def fake_long(session, sid, units):
            return monthly([3.0 + 0.001 * i for i in range(320)])

        async def fake_json(session, endpoint, params):
            if endpoint == "series/release":
                return {"releases": [{"id": hash(params["series_id"]) % 1000, "name": "Rel " + params["series_id"]}]}
            if endpoint == "releases/dates":
                return {"release_dates": [{"release_id": rid, "date": "2099-01-15"} for rid, _ in fred._RELEASE_IDS.values()]}
            return {"release_dates": []}

        SAHM = fred.SAHM_SERIES
        fred._fetch_series, fred._fetch_long, fred._get_json = fake_series, fake_long, fake_json
        events, statuses = [], []

        async def status(ok, msg):
            statuses.append((ok, msg))

        asyncio.run(fred._poll_once(None, events.append, status))
        latest = {e["id"]: e for e in events}
        self.assertTrue(statuses[-1][0], statuses)
        cpi = latest["macro-CPIAUCSL"]["macro"]
        self.assertEqual(len(cpi["long_history"]), 320)
        self.assertIn("percentile", cpi["context"]["text"])
        self.assertEqual(cpi["next_release"]["date"], "2099-01-15")
        self.assertNotIn("next_release", latest["macro-VIXCLS"]["macro"])  # daily series
        self.assertEqual(latest["macro-ECBDFR"]["macro"]["group"], "europe")
        self.assertIn("macro-derived-SAHM_RULE", latest)
        self.assertTrue(latest["macro-derived-REAL_POLICY_RATE"]["macro"]["long_history"])
        self.assertIn("macro-regime", latest)


if __name__ == "__main__":
    unittest.main()
