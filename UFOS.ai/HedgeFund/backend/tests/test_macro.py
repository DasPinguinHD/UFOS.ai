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
        m = derived.to_macro("SAHM_RULE", out["SAHM_RULE"], formula=out.formulas.get("SAHM_RULE"))["macro"]
        self.assertEqual(m["signal"]["level"], "alert")
        self.assertIn("SAHMREALTIME", m["formula"])

    def test_sahm_fallback(self):
        out = derived.compute({"UNRATE": monthly([3.7] * 36 + [4.3] * 12)}, [], None)
        m = derived.to_macro("SAHM_RULE", out["SAHM_RULE"], formula=out.formulas.get("SAHM_RULE"))["macro"]
        self.assertIn("own calculation", m["formula"])

    def test_real_policy_rate_uses_core_pce(self):
        full = {"FEDFUNDS": monthly([4.33] * 48), "PCEPILFE": monthly([2.8] * 47), "CPIAUCSL": monthly([3.0] * 48)}
        out = derived.compute(full, [])
        m = derived.to_macro("REAL_POLICY_RATE", out["REAL_POLICY_RATE"], formula=out.formulas.get("REAL_POLICY_RATE"))["macro"]
        self.assertAlmostEqual(m["value"], 1.53)
        self.assertIn("core PCE", m["formula"])

    def test_formula_does_not_depend_on_call_order(self):
        # Regression (Copilot review 2026-09-25): the formula text used to live in a
        # module-level dict, so whichever compute() call ran last decided it.
        official = derived.compute({"UNRATE": monthly([4.0] * 48)}, [], monthly([0.1] * 30 + [0.2]))
        fallback = derived.compute({"UNRATE": monthly([3.7] * 36 + [4.3] * 12)}, [], None)
        m = derived.to_macro("SAHM_RULE", official["SAHM_RULE"], formula=official.formulas.get("SAHM_RULE"))["macro"]
        self.assertIn("SAHMREALTIME", m["formula"])
        self.assertNotIn("own calculation", m["formula"])
        self.assertIn("own calculation", fallback.formulas["SAHM_RULE"])

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


class ExpandedRegimeTest(unittest.TestCase):
    """Expanded map, second pass: episodes, history, component sparklines, asset table."""

    def setUp(self):
        n = 320  # 2000-01 .. 2026-08
        self.pay = monthly([2.0 + (-6.0 if 110 <= i <= 113 else 0.0) + 0.01 * i for i in range(n)])
        self.ind = monthly([1.0 + 0.5 * ((i // 18) % 2) for i in range(n)])
        self.pce = monthly([2.0 + 0.3 * ((i // 30) % 3) for i in range(n)])
        self.ext = derived.regime_extended({"PAYEMS": self.pay, "INDPRO": self.ind, "PCEPILFE": self.pce})

    def test_history_matches_trail(self):
        hist, trail = self.ext["history"], self.ext["trail_long"]
        self.assertEqual(len(hist), len(trail))
        self.assertEqual([h["quadrant"] for h in hist], [t["quadrant"] for t in trail])
        self.assertTrue(all(set(h) == {"date", "quadrant"} for h in hist))

    def test_episodes_only_for_months_in_data(self):
        eps = self.ext["episodes"]
        labels = [e["label"] for e in eps]
        self.assertIn("GFC trough (Mar 2009)", labels)
        self.assertIn("COVID shock (Apr 2020)", labels)
        trail = {p["date"]: p for p in self.ext["trail_long"]}
        for e in eps:
            self.assertEqual((e["x"], e["y"], e["quadrant"]),
                             (trail[e["date"]]["x"], trail[e["date"]]["y"], trail[e["date"]]["quadrant"]))
        # A short history (from 2015) has no 2001/2005/2009 markers
        short = derived.episodes([p for p in self.ext["trail_long"] if p["date"] >= "2015-01-01"])
        self.assertEqual([e["date"] for e in short], ["2020-04-01", "2022-06-01"])

    def test_component_series(self):
        cs = self.ext["component_series"]
        for key in ("payrolls_yoy", "indpro_yoy", "inflation_yoy", "growth_momentum", "inflation_momentum"):
            self.assertEqual(len(cs[key]), derived.COMPONENT_SERIES_MONTHS, key)
            self.assertEqual(cs[key][-1]["date"], "2026-08-01", key)
        self.assertEqual(cs["inflation_name"], "core PCE")
        # the momentum series are exactly the map's axes
        last = self.ext["trail_long"][-1]
        self.assertAlmostEqual(cs["growth_momentum"][-1]["value"], last["x"], places=2)
        self.assertAlmostEqual(cs["inflation_momentum"][-1]["value"], last["y"], places=2)
        cpi_only = derived.regime_extended({"PAYEMS": self.pay, "INDPRO": self.ind, "CPIAUCSL": self.pce})
        self.assertEqual(cpi_only["component_series"]["inflation_name"], "CPI")

    def test_asset_tendencies_table(self):
        t = self.ext["asset_tendencies"]
        self.assertEqual([r["quadrant"] for r in t["rows"]], ["goldilocks", "reflation", "stagflation", "slowdown"])
        for r in t["rows"]:
            self.assertEqual([c["asset"] for c in r["cells"]], t["columns"])
            self.assertTrue(all(c["direction"] in ("up", "down", "mixed") for c in r["cells"]))
        self.assertIn("not a forecast", t["disclaimer"])
        self.assertIn("not investment advice", t["disclaimer"])

    def test_missing_inputs_give_empty_fields(self):
        ext = derived.regime_extended({})
        self.assertEqual(ext["episodes"], [])
        self.assertEqual(ext["history"], [])
        self.assertEqual(ext["component_series"]["payrolls_yoy"], [])
        self.assertEqual(ext["component_series"]["growth_momentum"], [])


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


def reset_fred_cache():
    fred._OBS.clear()
    fred._LAST_FULL.clear()
    fred._NEXT_RELEASE.clear()
    fred._RELEASE_IDS.clear()


class PollTest(unittest.TestCase):
    """One full FRED poll against a fake API (observations are served from
    2000 on and filtered by observation_start, like FRED does)."""

    def setUp(self):
        reset_fred_cache()
        self.calls = []
        SAHM = fred.SAHM_SERIES

        async def fake_obs(session, sid, units, start):
            self.calls.append((sid, units, start))
            fred._requests += 1
            if sid == "USREC":
                data = monthly([1.0 if 96 <= i <= 113 else 0.0 for i in range(320)])
            else:
                base = {"T10Y2Y": 0.3, SAHM: -0.05}.get(sid, 3.0)
                data = monthly([base + 0.001 * i for i in range(320)])
            return [p for p in data if p["date"] >= start]

        async def fake_eurostat(session, since_month):
            fred._requests += 1
            return [p for p in monthly([1.0 + 0.01 * i for i in range(320)]) if p["date"][:7] >= since_month]

        async def fake_json(session, endpoint, params):
            fred._requests += 1
            if endpoint == "series/release":
                return {"releases": [{"id": hash(params["series_id"]) % 1000, "name": "Rel " + params["series_id"]}]}
            if endpoint == "releases/dates":
                return {"release_dates": [{"release_id": rid, "date": "2099-01-15"} for rid, _ in fred._RELEASE_IDS.values()]}
            return {"release_dates": []}

        fred._fetch_observations, fred._fetch_eurostat_ip, fred._get_json = fake_obs, fake_eurostat, fake_json

    def tearDown(self):
        reset_fred_cache()

    def _poll(self):
        events, statuses = [], []

        async def status(ok, msg):
            statuses.append((ok, msg))

        asyncio.run(fred._poll_once(None, events.append, status))
        return {e["id"]: e for e in events}, statuses

    def test_full_poll_with_fake_api(self):
        latest, statuses = self._poll()
        self.assertTrue(statuses[-1][0], statuses)
        cpi = latest["macro-CPIAUCSL"]["macro"]
        self.assertEqual(len(cpi["long_history"]), 320)
        self.assertEqual(len(cpi["history"]), fred.HISTORY_POINTS)
        self.assertIn("percentile", cpi["context"]["text"])
        self.assertEqual(cpi["next_release"]["date"], "2099-01-15")
        self.assertNotIn("next_release", latest["macro-VIXCLS"]["macro"])  # daily series
        self.assertEqual(latest["macro-ECBDFR"]["macro"]["group"], "europe")
        self.assertIn("macro-derived-SAHM_RULE", latest)
        self.assertTrue(latest["macro-derived-REAL_POLICY_RATE"]["macro"]["long_history"])
        reg = latest["macro-regime"]["macro"]["regime"]
        self.assertIn("extended", reg)
        self.assertEqual(reg["extended"]["recessions"], [{"start": "2008-01", "end": "2009-06"}])
        self.assertIsNotNone(reg["extended"]["euro"])
        self.assertTrue(reg["extended"]["history"])
        self.assertEqual(len(reg["extended"]["component_series"]["payrolls_yoy"]), derived.COMPONENT_SERIES_MONTHS)
        self.assertIn("asset_tendencies", reg["extended"])

    def test_second_poll_is_incremental(self):
        self._poll()
        first = list(self.calls)
        self.assertTrue(all(start == fred.LONG_HISTORY_START for _, _, start in first))
        self.assertEqual(len(first), len(fred.fetch_specs()))
        self.calls.clear()
        self._poll()
        # latest fake month 2026-08-01 minus 400 days
        self.assertTrue(all(start == "2025-06-27" for _, _, start in self.calls), self.calls[:3])
        # release ids are cached: the second poll only needs releases/dates once
        self.assertEqual(fred._requests, len(fred.fetch_specs()) + 1 + 1)


if __name__ == "__main__":
    unittest.main()
