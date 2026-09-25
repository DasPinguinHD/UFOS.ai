"""Offline tests for the "Analysis & Reasoning" overview (newsroom/overview.py):
tile contract, ids and order, insider sentiment without Rule 10b5-1 trades,
templates, and the compact analysis context. Stdlib unittest only; no network,
no .env.

Run from HedgeFund/backend:  python -m unittest tests.test_overview -v

aiohttp, config and the newsroom package are stubbed so the modules under
test load without the backend's dependencies or its package __init__.
"""
import importlib.util
import os
import sys
import types
import unittest
from datetime import date

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_NEWSROOM = os.path.join(_BACKEND, "newsroom")


def _load_module():
    names = ("aiohttp", "config", "newsroom", "newsroom.store", "newsroom.macro_rules", "newsroom.macro_derived",
             "newsroom.fred_collector", "newsroom.insider_summary", "newsroom.overview")
    saved = {name: sys.modules.get(name) for name in names}
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
        store.make_event = lambda **kw: dict(kw)
        sys.modules["newsroom.store"] = store

        mod = None
        for name in ("macro_rules", "macro_derived", "fred_collector", "insider_summary", "overview"):
            spec = importlib.util.spec_from_file_location(f"newsroom.{name}", os.path.join(_NEWSROOM, name + ".py"))
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
        return mod
    finally:
        for name, m in saved.items():
            if m is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = m


ov = _load_module()
TODAY = date(2026, 9, 25)
TILE_KEYS = {"id", "group", "title", "value", "trend", "change", "level", "subtitle", "as_of", "facts"}


def macro_event(sid, group, unit, value, previous, date_, level="ok", reason="Within its usual range.", **extra):
    m = {"series_id": sid, "group": group, "unit": unit, "value": value, "date": date_,
         "previous": previous, "previous_date": "2026-07-01", "change": round(value - previous, 4),
         "note": f"How to read {sid}. Second sentence.", "signal": {"level": level, "reason": reason, "rules": {}}}
    m.update(extra)
    return {"id": f"macro-{sid}", "title": f"Label {sid}", "macro": m}


def macro_latest():
    evs = [
        # deliberately inserted out of SERIES order
        macro_event("CPIAUCSL", "us", "%", 3.1, 2.9, "2026-08-01", "watch", "inflation 3.1% — above the Fed's ~2% goal",
                    context={"text": "70th percentile since 2000"},
                    next_release={"date": "2026-10-15", "release": "Consumer Price Index"}),
        macro_event("FEDFUNDS", "us", "%", 4.33, 4.33, "2026-08-01"),
        macro_event("T10Y2Y", "us", "pp", 0.55, 0.5, "2026-09-24"),
        macro_event("ICSA", "us", "K", 231, 226, "2026-09-20"),
        macro_event("VIXCLS", "us", "pts", 16.2, 17.0, "2026-09-24"),
        macro_event("ECBDFR", "europe", "%", 2.0, 2.0, "2026-09-24"),
        macro_event("DGS10", "us", "%", 4.1, 4.2, "2026-09-24"),
        macro_event("SAHM_RULE", "derived", "pp", 0.2, 0.17, "2026-08-01", formula="3-month avg ..."),
        macro_event("REAL_POLICY_RATE", "derived", "pp", 1.4, 1.5, "2026-07-01", formula="Fed funds − core PCE"),
    ]
    out = {e["id"]: e for e in evs}
    out["macro-regime"] = {"id": "macro-regime", "macro": {"series_id": "REGIME", "group": "regime", "regime": {
        "label": "Reflation", "level": "watch", "quadrant": "reflation",
        "description": "Growth and inflation are both accelerating — risk of overheating. More text.",
        "growth_text": "Growth accelerating: +0.30 pp in 6 months", "inflation_text": "Inflation rising: +0.20 pp",
        "months_in_regime": 3, "borderline": False, "date": "2026-07-01"}}}
    return out


def filing(owner, issuer, ticker, code, value, shares, day, planned=False, roles=("Director",), cik=None):
    return {"category": "insider", "filed_at": f"{day}T12:00:00Z", "details": {
        "issuer_name": issuer, "issuer_ticker": ticker, "issuer_cik": ticker + "-cik",
        "owner_name": owner, "owner_cik": cik or owner + "-cik", "owner_roles": list(roles),
        "plan_10b5_1": planned,
        "transactions": [{"code": code, "derivative": False, "date": day, "shares": shares,
                          "price": value / shares, "value_usd": value, "acquired_disposed": "A" if code == "P" else "D"}]}}


def insiders():
    return [
        filing("Alice", "Acme Corp", "ACME", "P", 500_000, 10_000, "2026-09-20"),
        filing("Bob", "Acme Corp", "ACME", "P", 250_000, 5_000, "2026-09-22", roles=("CEO",)),
        filing("Carol", "Beta Inc", "BETA", "S", 2_000_000, 20_000, "2026-09-21"),
        # planned sale — must not count anywhere
        filing("Dan", "Gamma Ltd", "GAMA", "S", 90_000_000, 900_000, "2026-09-21", planned=True),
        # routine grant — ignored
        filing("Eve", "Beta Inc", "BETA", "A", 0.01, 1, "2026-09-21"),
    ]


def calendar():
    ev = []
    for d, sid, imp, kind in [("2026-09-20", "CPIAUCSL", "high", "release"),   # past
                              ("2026-10-02", "PAYEMS", "high", "release"),
                              ("2026-10-15", "CPIAUCSL", "high", "release"),
                              ("2026-10-03", "ICSA", "medium", "release"),
                              ("2026-10-21", "", "high", "fomc"),
                              ("2026-10-30", "PCEPILFE", "high", "release")]:
        ev.append({"date": d, "time_hint": "08:30 ET", "kind": kind, "series_id": sid, "importance": imp,
                   "name": "FOMC rate decision" if kind == "fomc" else f"Release {sid}",
                   "why": f"Why {sid or 'FOMC'} matters. More.", "last_display": "" if kind == "fomc" else "1.0 (Aug 2026)",
                   "sep": False})
    return {"events": ev, "window_days": 45}


class OverviewTest(unittest.TestCase):
    def setUp(self):
        self.data = ov.build_overview(macro_latest(), insiders(), calendar(), today=TODAY)
        self.tiles = {t["id"]: t for t in self.data["tiles"]}

    def test_contract_keys(self):
        self.assertEqual(set(self.data), {"tiles", "templates", "generated_at"})
        for t in self.data["tiles"]:
            self.assertEqual(set(t), TILE_KEYS, t["id"])
            self.assertTrue(all(isinstance(t[k], str) for k in TILE_KEYS), t["id"])
            self.assertIn(t["trend"], ("up", "down", "flat", "none"))
            self.assertIn(t["level"], ("ok", "watch", "alert", "info"))
            self.assertIn(t["group"], ("Macro Regime", "US Macro", "Euro Area", "Derived", "Insider Trades", "Calendar"))
        for tpl in self.data["templates"]:
            self.assertEqual(set(tpl), {"id", "label", "description", "tile_ids"})

    def test_ids_and_order(self):
        ids = [t["id"] for t in self.data["tiles"]]
        self.assertEqual(ids[:8], ["regime", "macro:FEDFUNDS", "macro:DGS10", "macro:T10Y2Y", "macro:CPIAUCSL",
                                   "macro:ICSA", "macro:VIXCLS", "macro:ECBDFR"])
        self.assertEqual(ids[8:10], ["derived:REAL_POLICY_RATE", "derived:SAHM_RULE"])  # DERIVED order
        groups = [t["group"] for t in self.data["tiles"]]
        order = ["Macro Regime", "US Macro", "Euro Area", "Derived", "Insider Trades", "Calendar"]
        self.assertEqual(groups, sorted(groups, key=order.index))
        self.assertIn("insider:sentiment", ids)
        self.assertIn("insider:cluster:1", ids)
        self.assertEqual([i for i in ids if i.startswith("calendar:")],
                         ["calendar:2026-10-02:PAYEMS", "calendar:2026-10-15:CPIAUCSL",
                          "calendar:2026-10-21:fomc", "calendar:2026-10-30:PCEPILFE"])

    def test_formatting(self):
        self.assertEqual(self.tiles["macro:CPIAUCSL"]["value"], "3.10%")
        self.assertEqual(self.tiles["macro:CPIAUCSL"]["change"], "+0.20 pp vs. prev.")
        self.assertEqual(self.tiles["macro:CPIAUCSL"]["trend"], "up")
        self.assertEqual(self.tiles["macro:CPIAUCSL"]["as_of"], "Aug 2026")
        self.assertEqual(self.tiles["macro:CPIAUCSL"]["level"], "watch")
        self.assertIn("above the Fed", self.tiles["macro:CPIAUCSL"]["subtitle"])
        self.assertIn("Next release 2026-10-15", self.tiles["macro:CPIAUCSL"]["facts"])
        self.assertEqual(self.tiles["macro:ICSA"]["value"], "231K")
        self.assertEqual(self.tiles["macro:T10Y2Y"]["value"], "0.55 pp")
        self.assertEqual(self.tiles["macro:T10Y2Y"]["as_of"], "2026-09-24")
        self.assertEqual(self.tiles["macro:FEDFUNDS"]["trend"], "flat")
        self.assertEqual(self.tiles["regime"]["value"], "Reflation")
        self.assertEqual(self.tiles["regime"]["level"], "watch")
        cal = self.tiles["calendar:2026-10-02:PAYEMS"]
        self.assertEqual((cal["value"], cal["as_of"], cal["level"]), ("Oct 2", "in 7 days · Oct 2", "info"))
        self.assertTrue(cal["subtitle"].startswith("Why PAYEMS"))

    def test_sentiment_excludes_10b5_1(self):
        s = self.tiles["insider:sentiment"]
        self.assertEqual(s["value"], "2 buys / 1 sell")
        self.assertNotIn("90", s["change"])  # the $90M planned sale is not in the totals
        self.assertIn("$750K bought", s["change"])
        self.assertIn("$2.0M sold", s["change"])
        self.assertIn("1 filing(s) under Rule 10b5-1", s["facts"])
        tops = [t for i, t in self.tiles.items() if i.startswith("insider:top:")]
        self.assertTrue(all("GAMA" not in t["title"] for t in tops))
        self.assertEqual(self.tiles["insider:top:1"]["title"], "Alice (Director) bought ACME")
        self.assertEqual(self.tiles["insider:cluster:1"]["title"], "Cluster buy: ACME")

    def test_sentiment_level(self):
        many = [filing(f"B{i}", "Acme", "ACME", "P", 1000, 10, "2026-09-20") for i in range(3)]
        many += [filing(f"S{i}", "Acme", "ACME", "S", 1000, 10, "2026-09-20") for i in range(10)]
        t = {x["id"]: x for x in ov.build_overview({}, many, {}, today=TODAY)["tiles"]}
        self.assertEqual(t["insider:sentiment"]["level"], "info")  # 3 buys vs 10 sells: normal
        many += [filing(f"B{i}", "Acme", "ACME", "P", 1000, 10, "2026-09-21") for i in range(3, 5)]
        t = {x["id"]: x for x in ov.build_overview({}, many, {}, today=TODAY)["tiles"]}
        self.assertEqual(t["insider:sentiment"]["level"], "watch")  # 5 distinct buyers

    def test_templates_only_existing_ids(self):
        ids = set(self.tiles)
        tpls = {t["id"]: t for t in self.data["templates"]}
        for tpl in tpls.values():
            self.assertGreaterEqual(len(tpl["tile_ids"]), 2)
            self.assertTrue(set(tpl["tile_ids"]) <= ids, tpl["id"])
        self.assertIn("calendar:2026-10-15:CPIAUCSL", tpls["inflation"]["tile_ids"])
        self.assertIn("calendar:2026-10-21:fomc", tpls["rates"]["tile_ids"])
        self.assertEqual(tpls["insiders"]["tile_ids"][-2:], ["macro:VIXCLS", "regime"])
        # europe: ECBDFR, FEDFUNDS, DGS10, CPIAUCSL exist
        self.assertEqual(tpls["europe"]["tile_ids"], ["macro:ECBDFR", "macro:FEDFUNDS", "macro:DGS10", "macro:CPIAUCSL"])

    def test_templates_dropped_below_two(self):
        only = {k: v for k, v in macro_latest().items() if k == "macro-ECBDFR"}
        data = ov.build_overview(only, [], {}, today=TODAY)
        self.assertEqual([t["id"] for t in data["tiles"]], ["macro:ECBDFR"])
        self.assertEqual(data["templates"], [])

    def test_empty_inputs(self):
        data = ov.build_overview({}, [], {}, today=TODAY)
        self.assertEqual((data["tiles"], data["templates"]), ([], []))

    def test_context(self):
        sel = ["regime", "macro:CPIAUCSL", "insider:sentiment", "calendar:2026-10-15:CPIAUCSL", "nope"]
        text = ov.build_analysis_context(self.tiles, sel, calendar()["events"], today=TODAY)
        self.assertIn("SELECTED ITEMS (4):", text)
        self.assertIn("[US Macro] Label CPIAUCSL: 3.10% (+0.20 pp vs. prev.)", text)
        self.assertIn("UPCOMING KEY DATES (always included, next 30 days):", text)
        self.assertIn("2026-10-02", text)
        self.assertIn("FOMC rate decision", text)
        self.assertNotIn("2026-09-20 (", text.split("UPCOMING")[1])   # past event excluded
        self.assertNotIn("Release ICSA", text)                         # medium importance excluded
        self.assertNotIn("2026-10-30", text.split("UPCOMING")[1])     # beyond 30 days
        self.assertIn("SCENARIOS (NEXT 3–12 MONTHS)", text)
        self.assertLess(len(text), 6000)

    def test_context_compact_with_many_tiles(self):
        text = ov.build_analysis_context(self.tiles, list(self.tiles), calendar()["events"], today=TODAY)
        self.assertNotIn("About:", text)
        self.assertLess(len(text), 9000)


if __name__ == "__main__":
    unittest.main()
