"""Offline tests for the local SQLite store (newsroom/db.py) and what builds
on it: migrations, insider archive + 5-day display window + retention, FRED
incremental fetching with revisions, cards rebuilt from the store at startup,
AI results, and the extended macro-regime contract (recessions, components,
euro-area point from a JSON-stat payload). Stdlib unittest only; every test
uses a temp database via UFOS_HEDGEFUND_DB — the user's real file is never
touched.

Run from HedgeFund/backend:  python -m unittest tests.test_store -v
"""
import asyncio
import importlib.util
import json
import math
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_NEWSROOM = os.path.join(_BACKEND, "newsroom")
_NAMES = ("aiohttp", "config", "newsroom", "newsroom.store", "newsroom.db", "newsroom.macro_rules",
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
        mods = {}
        for name in ("store", "db", "macro_rules", "macro_derived", "fred_collector"):
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
store, db, derived, fred = M["store"], M["db"], M["macro_derived"], M["fred_collector"]
assert fred.store_db is db


def monthly(values, start_year=2000):
    return [{"date": f"{start_year + i // 12}-{i % 12 + 1:02d}-01", "value": v} for i, v in enumerate(values)]


def reset_fred_cache():
    fred._OBS.clear()
    fred._LAST_FULL.clear()
    fred._NEXT_RELEASE.clear()
    fred._RELEASE_IDS.clear()


class TempDbCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved_env = os.environ.get(db.ENV_PATH)
        self.path = os.path.join(self._tmp.name, "nested", "dir", "hedgefund.sqlite")
        os.environ[db.ENV_PATH] = self.path
        self.assertTrue(db.init())
        reset_fred_cache()

    def tearDown(self):
        db.close()
        reset_fred_cache()
        if self._saved_env is None:
            os.environ.pop(db.ENV_PATH, None)
        else:
            os.environ[db.ENV_PATH] = self._saved_env
        self._tmp.cleanup()

    def raw(self, sql, params=()):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()


class MigrationTest(TempDbCase):
    def test_env_override_creates_dirs_and_schema(self):
        self.assertEqual(db.db_path(), self.path)
        self.assertTrue(os.path.exists(self.path))
        self.assertEqual(self.raw("PRAGMA user_version")[0][0], db.SCHEMA_VERSION)
        self.assertEqual(self.raw("PRAGMA journal_mode")[0][0].lower(), "wal")
        tables = {r[0] for r in self.raw("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"insider_filings", "macro_observations", "calendar_snapshot", "ai_results", "meta"} <= tables)

    def test_migrations_idempotent(self):
        db.meta_set("k", "v")
        self.assertTrue(db.init())          # second open: nothing re-applied, data kept
        conn = sqlite3.connect(self.path)
        try:
            self.assertEqual(db.migrate(conn), db.SCHEMA_VERSION)
            conn.execute("PRAGMA user_version = 0")   # force a re-run of every statement
            conn.commit()
            self.assertEqual(db.migrate(conn), db.SCHEMA_VERSION)
        finally:
            conn.close()
        self.assertEqual(db.meta_get("k"), "v")

    def test_default_path_in_backend_data_and_legacy_in_localappdata(self):
        saved = os.environ.pop(db.ENV_PATH)
        old_lad = os.environ.get("LOCALAPPDATA")
        try:
            os.environ["LOCALAPPDATA"] = os.path.join("X:", "lad")
            self.assertEqual(db.default_path(), os.path.join(_BACKEND, "data", "newsroom.db"))
            self.assertEqual(db.legacy_path(), os.path.join("X:", "lad", "UFOS.ai", "data", "hedgefund.sqlite"))
        finally:
            os.environ[db.ENV_PATH] = saved
            if old_lad is None:
                os.environ.pop("LOCALAPPDATA", None)
            else:
                os.environ["LOCALAPPDATA"] = old_lad

    def test_unusable_path_disables_store_without_raising(self):
        blocker = os.path.join(self._tmp.name, "file")
        open(blocker, "w").close()
        self.assertFalse(db.init(os.path.join(blocker, "sub", "x.sqlite")))  # parent is a file
        self.assertFalse(db.is_enabled())
        self.assertEqual(db.load_insider("2000-01-01T00:00:00+00:00"), [])
        self.assertEqual(db.upsert_insider({"id": "insider-1"}), 0)
        self.assertIsNone(db.insert_ai_result("analysis", "m", None, {}, "t"))
        self.assertEqual(db.ai_history(), [])


def filing(acc, filed_at, details=True):
    return {"id": f"insider-{acc}", "category": "insider", "title": f"T {acc}", "summary": "s",
            "timestamp": filed_at, "filed_at": filed_at,
            "details": {"issuer_ticker": "ABC", "issuer_name": "Abc Inc", "owner_name": "Doe Jane",
                        "transactions": []} if details else None}


class InsiderTest(TempDbCase):
    def test_upsert_display_window_and_prune(self):
        now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
        recent = filing("0001", "2026-09-24T10:00:00-04:00")
        edge = filing("0002", "2026-09-20T13:00:00+00:00")     # 5 days minus 1 h
        old = filing("0003", "2026-09-10T10:00:00-04:00")       # outside the 5-day window
        ancient = filing("0004", "2024-09-01T10:00:00-04:00")
        self.assertEqual(db.upsert_insider_many([recent, edge, old, ancient], now="2026-09-25T12:00:00+00:00"), 4)
        # re-poll: known filing -> last_seen updated, first_seen kept; no duplicate row
        db.upsert_insider(filing("0001", recent["filed_at"], details=False), now="2026-09-25T12:02:00+00:00")
        rows = self.raw("SELECT first_seen, last_seen, filed_at, issuer_ticker FROM insider_filings WHERE accession='0001'")
        self.assertEqual(rows, [("2026-09-25T12:00:00+00:00", "2026-09-25T12:02:00+00:00",
                                 "2026-09-24T14:00:00+00:00", "ABC")])
        stored = db.load_insider((now - timedelta(days=5)).isoformat())
        self.assertEqual([e["id"] for e in stored], ["insider-0001", "insider-0002"])
        self.assertIsNotNone(stored[0]["details"])  # the parsed payload survived the detail-less re-poll

        # in-memory display window (same rule main.py uses)
        self.assertFalse(store.filed_before_window(recent, 5, now))
        self.assertFalse(store.filed_before_window(edge, 5, now))
        self.assertTrue(store.filed_before_window(old, 5, now))
        self.assertFalse(store.filed_before_window({"filed_at": "unknown time"}, 5, now))
        es = store.EventStore()
        for e in (old, edge, recent):
            es.add(e)
        es.prune(lambda e: store.filed_before_window(e, 5, now))
        self.assertEqual({e["id"] for e in es.snapshot()}, {"insider-0001", "insider-0002"})

        # retention (default 5 days by filing time): old + ancient removed
        self.assertEqual(db.prune(now=now), {"insider": 2, "ai_results": 0, "calendar_events": 0, "prices": 0})
        self.assertEqual(db.count_rows("insider_filings"), 2)
        self.assertEqual(db.meta_get("last_prune"), "2026-09-25T12:00:00+00:00")


class FredIncrementalTest(TempDbCase):
    KEY = ("CPIAUCSL", "pc1")

    def test_plan_first_full_then_incremental_then_weekly_full(self):
        self.assertEqual(fred.plan_fetch(self.KEY, now=1000.0), (fred.LONG_HISTORY_START, True))
        fred.apply_fetch(self.KEY, monthly([3.0] * 320), fred.LONG_HISTORY_START, True, now=1000.0)
        start, full = fred.plan_fetch(self.KEY, now=1000.0 + 3600)
        self.assertFalse(full)
        self.assertEqual(start, "2025-06-27")  # 2026-08-01 − 400 days
        self.assertEqual(fred.plan_fetch(self.KEY, now=1000.0 + fred.FULL_REFRESH_SECONDS)[1], True)

    def test_revisions_are_upserted_in_memory_and_in_db(self):
        async def run():
            pts = monthly([3.0] * 320)
            fred.apply_fetch(self.KEY, pts, fred.LONG_HISTORY_START, True)
            await fred._persist(self.KEY, pts, fred.LONG_HISTORY_START, True)
            start, full = fred.plan_fetch(self.KEY)
            self.assertFalse(full)
            # FRED revised July, dropped the August value ('.') and added September
            tail = [{"date": p["date"], "value": 3.0} for p in pts if p["date"] >= start and p["date"] < "2026-07-01"]
            tail += [{"date": "2026-07-01", "value": 3.4}, {"date": "2026-09-01", "value": 3.5}]
            fred.apply_fetch(self.KEY, tail, start, False)
            await fred._persist(self.KEY, tail, start, False)
        asyncio.run(run())
        self.assertEqual(fred._OBS[self.KEY]["2026-07-01"], 3.4)
        self.assertNotIn("2026-08-01", fred._OBS[self.KEY])
        rows = dict(self.raw("SELECT date, value FROM macro_observations WHERE series_id='CPIAUCSL' AND units='pc1' "
                             "AND date >= '2026-06-01'"))
        self.assertEqual(rows, {"2026-06-01": 3.0, "2026-07-01": 3.4, "2026-09-01": 3.5})
        self.assertEqual(db.count_rows("macro_observations"), 320)  # 319 kept/revised + 1 new − 1 withdrawn
        self.assertTrue(db.meta_get("obs_full:CPIAUCSL:pc1"))

    def test_empty_answer_never_wipes(self):
        fred.apply_fetch(self.KEY, monthly([3.0] * 30), fred.LONG_HISTORY_START, True)
        fred.apply_fetch(self.KEY, [], fred.LONG_HISTORY_START, True)
        self.assertEqual(len(fred._OBS[self.KEY]), 30)
        db.replace_observations("X", "lin", "native", [], from_date="2000-01-01")
        self.assertEqual(db.count_rows("macro_observations"), 0)

    def test_monthly_average(self):
        pts = [{"date": "2026-01-02", "value": 1.0}, {"date": "2026-01-30", "value": 3.0},
               {"date": "2026-02-02", "value": 5.0}]
        self.assertEqual(fred.monthly_average(pts), [{"date": "2026-01-01", "value": 2.0},
                                                     {"date": "2026-02-01", "value": 5.0}])


class StartupRebuildTest(TempDbCase):
    """A poll fills the store; a 'restarted' process rebuilds every card from it."""

    def test_macro_latest_rebuilt_from_db(self):
        async def fake_obs(session, sid, units, start):
            if sid == "USREC":
                data = monthly([1.0 if 96 <= i <= 113 else 0.0 for i in range(320)])
            else:
                data = monthly([{"T10Y2Y": 0.3, fred.SAHM_SERIES: -0.05}.get(sid, 3.0) + 0.002 * i for i in range(320)])
            return [p for p in data if p["date"] >= start]

        async def fake_eurostat(session, since_month):
            return [p for p in monthly([1.0 + 0.01 * i for i in range(320)]) if p["date"][:7] >= since_month]

        async def fake_json(session, endpoint, params):
            if endpoint == "series/release":
                return {"releases": [{"id": 7, "name": "Rel"}]}
            return {"release_dates": [{"release_id": 7, "date": "2099-01-15"}]}

        saved = fred._fetch_observations, fred._fetch_eurostat_ip, fred._get_json
        fred._fetch_observations, fred._fetch_eurostat_ip, fred._get_json = fake_obs, fake_eurostat, fake_json
        try:
            polled = []

            async def status(ok, msg):
                pass

            asyncio.run(fred._poll_once(None, polled.append, status))
        finally:
            fred._fetch_observations, fred._fetch_eurostat_ip, fred._get_json = saved
        before = {e["id"]: e for e in polled}

        reset_fred_cache()                       # "restart"
        self.assertEqual(fred.build_events()[0], [])
        self.assertEqual(fred.load_state(), len(fred.fetch_specs()) + 1)  # + Eurostat IP
        macro_latest = {e["id"]: e for e in fred.build_events()[0]}
        self.assertEqual(set(macro_latest), set(before))
        for sid in ("CPIAUCSL", "VIXCLS", "ICSA"):
            a, b = macro_latest[f"macro-{sid}"]["macro"], before[f"macro-{sid}"]["macro"]
            self.assertEqual((a["value"], a["date"], a["history"], a["long_history"]),
                             (b["value"], b["date"], b["history"], b["long_history"]))
        self.assertEqual(macro_latest["macro-CPIAUCSL"]["macro"]["next_release"]["date"], "2099-01-15")
        self.assertEqual(fred._RELEASE_IDS["CPIAUCSL"], (7, "Rel"))
        self.assertIn("extended", macro_latest["macro-regime"]["macro"]["regime"])
        # the full-refetch clock survived the restart -> next poll is incremental
        self.assertFalse(fred.plan_fetch(("CPIAUCSL", "pc1"))[1])


class CalendarAndAiTest(TempDbCase):
    def test_calendar_snapshot_roundtrip(self):
        self.assertIsNone(db.load_calendar())
        db.save_calendar({"events": [{"date": "2099-01-01"}], "updated": "x", "window_days": 45})
        db.save_calendar({"events": [{"date": "2099-02-01"}], "updated": "y", "window_days": 45})
        self.assertEqual(db.load_calendar()["updated"], "y")
        self.assertEqual(db.count_rows("calendar_snapshot"), 1)

    def test_ai_results_stored_listed_and_pruned(self):
        prov = {"ai_generated": True, "model": "m1", "generated_at": "2026-09-25T10:00:00+00:00"}
        db.insert_ai_result("insider-summary", "m1", None, {"filings": 12, "table": "..."}, "text A", prov,
                            {"cost_usd": 0.01}, created_at="2025-06-01T10:00:00+00:00")
        db.insert_ai_result("analysis", "m2", "premium", {"tile_ids": ["regime", "macro:DGS10"],
                                                          "titles": ["Macro Regime", "10Y"]},
                            "text B", prov, {"cost_usd": 0.2}, created_at="2026-09-25T10:00:00+00:00")
        db.insert_ai_result("analysis", "m2", "standard", {"tile_ids": []}, "text C", prov, None,
                            created_at="2026-09-25T11:00:00+00:00")
        items = db.ai_history()
        self.assertEqual([i["output_text"] for i in items], ["text C", "text B", "text A"])
        b = db.ai_history(feature="analysis", limit=1)
        self.assertEqual(len(b), 1)
        self.assertEqual(b[0]["output_text"], "text C")
        full_b = db.ai_history(feature="analysis")[1]
        self.assertEqual(full_b["tier"], "premium")
        self.assertEqual(full_b["input_summary"]["titles"], ["Macro Regime", "10Y"])
        self.assertTrue(full_b["provenance"]["ai_generated"])
        self.assertEqual(full_b["usage"]["cost_usd"], 0.2)
        res = db.prune(now=datetime(2026, 9, 25, 12, tzinfo=timezone.utc))
        self.assertEqual(res["ai_results"], 1)  # the 2025-06 summary is older than 90 days
        self.assertEqual(db.count_rows("ai_results"), 2)


def jsonstat(months, values):
    """Minimal Eurostat JSON-stat 2.0 answer (single-category dims except time)."""
    return {
        "version": "2.0", "class": "dataset",
        "id": ["freq", "unit", "s_adj", "nace_r2", "geo", "time"],
        "size": [1, 1, 1, 1, 1, len(months)],
        "value": {str(i): v for i, v in enumerate(values) if v is not None},
        "dimension": {"time": {"category": {"index": {m: i for i, m in enumerate(months)}}},
                      "geo": {"category": {"index": {"EA20": 0}}}},
    }


class RegimeExtendedTest(unittest.TestCase):
    def test_contract_keys_and_types(self):
        n = 320
        pay = monthly([1.5 + 0.5 * math.sin(i / 9) for i in range(n)])
        ind = monthly([1.0 + 1.5 * math.sin(i / 7) for i in range(n)])
        pce = monthly([2.0 + 0.8 * math.cos(i / 11) for i in range(n)])
        usrec = monthly([1.0 if (14 <= i <= 21 or 95 <= i <= 113 or 242 <= i <= 243) else 0.0 for i in range(n)])
        months = [p["date"][:7] for p in monthly([0] * n)]
        payload = jsonstat(months, [0.5 * math.sin(i / 5) for i in range(n - 1)] + [None])
        euro_ip = fred.parse_jsonstat(payload)
        self.assertEqual(len(euro_ip), n - 1)            # the missing last month is skipped
        self.assertEqual(euro_ip[0]["date"], "2000-01-01")
        hicp = monthly([2.0 + 0.3 * i / n for i in range(n)])

        ext = derived.regime_extended({"PAYEMS": pay, "INDPRO": ind, "PCEPILFE": pce}, usrec, euro_ip, hicp)
        self.assertEqual(set(ext), {"trail_long", "recessions", "components", "euro", "phase_behaviour", "note",
                                    "episodes", "history", "component_series", "asset_tendencies"})
        self.assertIsInstance(ext["note"], str)
        # trail since 2000, monthly, same method as the card's trail
        tl = ext["trail_long"]
        self.assertGreater(len(tl), 300)
        self.assertGreaterEqual(tl[0]["date"], "2000-01-01")
        for p in tl:
            self.assertEqual(set(p), {"date", "x", "y", "quadrant"})
            self.assertIsInstance(p["x"], float)
            self.assertIn(p["quadrant"], ("goldilocks", "reflation", "stagflation", "slowdown"))
        card = derived.regime({"PCEPILFE": pce[-48:]}, {"PAYEMS": pay[-48:], "INDPRO": ind[-48:]})
        self.assertEqual(tl[-12:], card["trail"])
        # recessions (FRED USREC)
        self.assertEqual(ext["recessions"], [{"start": "2001-03", "end": "2001-10"},
                                             {"start": "2007-12", "end": "2009-06"},
                                             {"start": "2020-03", "end": "2020-04"}])
        # components
        self.assertEqual([c["name"] for c in ext["components"]],
                         ["Nonfarm payrolls (YoY)", "Industrial production (YoY)", "Core PCE inflation (YoY)"])
        self.assertEqual([c["axis"] for c in ext["components"]], ["growth", "growth", "inflation"])
        for c in ext["components"]:
            self.assertEqual(set(c), {"name", "axis", "latest", "six_months_ago", "change", "unit", "date"})
            self.assertAlmostEqual(c["change"], c["latest"] - c["six_months_ago"], places=2)
            self.assertEqual(c["unit"], "%")
            self.assertEqual(c["date"], "2026-08-01")
        self.assertAlmostEqual(ext["components"][2]["change"], card["y"], places=2)  # same smoothing/momentum
        # euro-area point
        eu = ext["euro"]
        self.assertEqual(set(eu), {"x", "y", "quadrant", "label", "date", "trail", "inputs", "source_url"})
        self.assertEqual(len(eu["trail"]), 12)
        self.assertEqual(eu["date"], "2026-07-01")
        self.assertEqual((eu["x"], eu["y"], eu["quadrant"]), (eu["trail"][-1]["x"], eu["trail"][-1]["y"],
                                                              eu["trail"][-1]["quadrant"]))
        self.assertEqual(eu["label"], derived.REGIMES[eu["quadrant"]][0])
        self.assertIn("eurostat", eu["source_url"])
        # phase behaviour (static)
        pb = ext["phase_behaviour"]
        self.assertEqual(pb["disclaimer"], "Historical tendencies, not a forecast and not investment advice.")
        for q in ("goldilocks", "reflation", "stagflation", "slowdown"):
            self.assertIsInstance(pb[q]["summary"], str)
            self.assertEqual([a["asset"] for a in pb[q]["assets"]],
                             ["Equities", "Government bonds", "Credit", "US dollar", "Commodities", "Sectors"])
            self.assertTrue(all(isinstance(a["tendency"], str) and a["tendency"] for a in pb[q]["assets"]))
        json.dumps(ext)  # JSON-serialisable

    def test_fallbacks(self):
        ext = derived.regime_extended({"PAYEMS": [], "INDPRO": [], "CPIAUCSL": monthly([2.0] * 30)}, [], None, None)
        self.assertEqual(ext["trail_long"], [])
        self.assertIsNone(ext["euro"])
        self.assertEqual([c["name"] for c in ext["components"]], ["CPI inflation (YoY)"])
        self.assertEqual(derived.recessions(monthly([0, 1, 1], 2025)), [{"start": "2025-02", "end": "2025-03"}])
        self.assertEqual(derived.recessions(monthly([1, 1, 0], 1999)), [])  # ended before 2000

    def test_jsonstat_list_values_and_old_labels(self):
        payload = {"id": ["geo", "time"], "size": [1, 2], "value": [1.5, -0.5],
                   "dimension": {"time": {"category": {"index": ["2025M01", "2025M02"]}}}}
        self.assertEqual(fred.parse_jsonstat(payload), [{"date": "2025-01-01", "value": 1.5},
                                                        {"date": "2025-02-01", "value": -0.5}])


if __name__ == "__main__":
    unittest.main()
