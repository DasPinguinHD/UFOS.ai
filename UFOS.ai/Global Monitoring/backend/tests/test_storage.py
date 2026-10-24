"""Tests for the local SQLite storage (storage.py) and its Global Monitoring
glue (persistence.py, localnews.py persistence, EventStore warm start),
2026-09-25. stdlib unittest only, temp DB files; third-party modules are
stubbed so the tests run without the backend's venv.

Run from Global Monitoring/backend:
    python -m unittest discover -s tests -t .
"""
import asyncio
import importlib
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import storage  # noqa: E402
from events import EventStore  # noqa: E402

# persistence imports the real config (retention constants). Other test
# modules may have put a stub "config" into sys.modules, and config's
# load_dotenv() must not read the real backend/.env — import it with a no-op
# dotenv, then put back whatever was there before.
_saved = {k: sys.modules.pop(k) for k in ("config", "dotenv", "persistence") if k in sys.modules}
sys.modules["dotenv"] = types.SimpleNamespace(load_dotenv=lambda *a, **k: None)
try:
    config = importlib.import_module("config")
    persistence = importlib.import_module("persistence")
finally:
    sys.modules.pop("dotenv", None)
    if "config" in _saved:
        sys.modules["config"] = _saved["config"]
    if "dotenv" in _saved:
        sys.modules["dotenv"] = _saved["dotenv"]

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _event(event_id, category, age_hours=0.0, **extra):
    ev = {"id": event_id, "category": category, "lat": 1.0, "lon": 2.0, "title": event_id,
          "summary": "s", "source": "test", "url": None, "severity": None,
          "timestamp": _iso(NOW - timedelta(hours=age_hours))}
    ev.update(extra)
    return ev


def _never_past(event):
    return False


def _past_if_flagged(event):
    return bool(event.get("past"))


class _TempStoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "sub", "gms.db")
        self.store = storage.Storage(self.path)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()


class OpenAndMigrateTests(_TempStoreCase):
    def test_file_created_in_wal_mode_with_schema(self):
        self.assertFalse(self.store.in_memory)
        self.assertTrue(os.path.exists(self.path))
        self.assertEqual(self.store.schema_version(), storage.MIGRATIONS[-1][0])
        conn = sqlite3.connect(self.path)
        try:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        self.assertTrue({"items", "flight_positions", "prices", "ai_outputs", "source_state", "meta"} <= tables)

    def test_reopen_keeps_data_and_is_idempotent(self):
        self.store.upsert_items("news", [("news-1", NOW, {"id": "news-1"})])
        self.store.close()
        self.store = storage.Storage(self.path)
        storage.Storage._migrate(self.store._conn)  # once more: no-op
        self.assertEqual(self.store.schema_version(), storage.MIGRATIONS[-1][0])
        self.assertTrue(self.store.has_item("news", "news-1"))

    def test_default_path_and_env_override(self):
        with mock.patch.dict(os.environ, {storage.DB_ENV: "/x/y.db"}):
            self.assertEqual(storage.default_path(), "/x/y.db")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(storage.DB_ENV, None)
            self.assertEqual(storage.default_path(), os.path.join(BACKEND, "data", "gms.db"))

    def test_meta_roundtrip(self):
        self.assertIsNone(self.store.get_meta("k"))
        self.assertTrue(self.store.set_meta("k", {"a": 1}))
        self.assertTrue(self.store.set_meta("k", {"a": 2}))
        self.assertEqual(self.store.get_meta("k"), {"a": 2})


class UpsertDedupeTests(_TempStoreCase):
    def test_same_id_is_one_row_newest_wins(self):
        self.store.upsert_items("news", [("news-1", NOW - timedelta(hours=1), {"v": 1})])
        self.store.upsert_items("news", [("news-1", NOW, {"v": 2}), ("news-2", NOW, {"v": 3})])
        rows = self.store.load_items("news")
        self.assertEqual([r["item_id"] for r in rows], ["news-1", "news-2"])
        self.assertEqual(rows[0]["payload"], {"v": 2})

    def test_flight_positions_one_row_per_observation(self):
        ev = _event("flight-abc", "flight", icao24="abc", observed_at=_iso(NOW - timedelta(minutes=8)))
        self.assertTrue(persistence.record_event(self.store, ev))
        self.assertTrue(persistence.record_event(self.store, ev))  # same observation polled again
        ev2 = dict(ev, observed_at=_iso(NOW - timedelta(minutes=4)), lat=5.0)
        persistence.record_event(self.store, ev2)
        track = persistence.flight_track(self.store, "ABC", now=NOW)
        self.assertEqual(len(track), 2)
        self.assertEqual(track[-1]["lat"], 5.0)
        latest = self.store.load_flight_positions(latest_only=True)
        self.assertEqual(len(latest), 1)
        self.assertEqual(latest[0]["payload"]["lat"], 5.0)

    def test_market_event_stores_latest_quote_and_price_points(self):
        q1 = _event("markets-^GSPC", "markets", symbol="^GSPC", price=5000.0, quote_time=_iso(NOW - timedelta(minutes=2)))
        persistence.record_event(self.store, q1)
        persistence.record_event(self.store, q1)  # unchanged delayed quote polled again
        persistence.record_event(self.store, dict(q1, price=5001.0, quote_time=_iso(NOW)))
        self.assertEqual([p[1] for p in self.store.load_prices("^GSPC")], [5000.0, 5001.0])
        self.assertEqual(len(self.store.load_items("yahoo")), 1)

    def test_forget_events_deletes_from_db(self):
        persistence.record_event(self.store, _event("disaster-1", "disaster", ends_at=None))
        persistence.record_event(self.store, _event("news-1", "geopolitics"))
        self.assertEqual(persistence.forget_events(self.store, ["disaster-1", "news-1", "flight-x"]), 2)
        self.assertEqual(self.store.load_items("gdacs"), [])
        self.assertEqual(self.store.load_items("news"), [])


class RetentionTests(_TempStoreCase):
    def _fill(self):
        s = self.store
        s.upsert_items("news", [("news-new", NOW - timedelta(days=6), {"id": "news-new"}),
                                ("news-old", NOW - timedelta(days=8), {"id": "news-old"})])
        s.upsert_items("gdacs", [("disaster-cur", NOW - timedelta(hours=1), {"id": "disaster-cur"}),
                                 ("disaster-past", NOW - timedelta(hours=1), {"id": "disaster-past", "past": True}),
                                 ("disaster-old", NOW - timedelta(days=8), {"id": "disaster-old"})])
        s.upsert_items("localnews", [("berlin|germany", NOW - timedelta(hours=23), {"a": 1}),
                                     ("paris|france", NOW - timedelta(hours=25), {"a": 1})])
        s.add_flight_positions([{"icao24": "a", "ts": NOW - timedelta(hours=23), "lat": 1, "lon": 1},
                                {"icao24": "a", "ts": NOW - timedelta(hours=25), "lat": 1, "lon": 1}])
        s.add_prices([("^GSPC", NOW - timedelta(days=700), 1.0, "yahoo"),
                      ("^GSPC", NOW - timedelta(days=740), 1.0, "yahoo")])
        s.save_ai_output("assess", "m", "new", ts=NOW - timedelta(days=89))
        s.save_ai_output("assess", "m", "old", ts=NOW - timedelta(days=91))

    def test_apply_retention_per_data_type(self):
        self._fill()
        counts = persistence.apply_retention(self.store, now=NOW, is_past=_past_if_flagged)
        self.assertEqual(counts, {"news": 1, "yahoo": 0, "localnews": 1, "flight_positions": 1,
                                  "prices": 1, "ai_outputs": 1, "gdacs": 2})
        self.assertEqual([r["item_id"] for r in self.store.load_items("news")], ["news-new"])
        self.assertEqual([r["item_id"] for r in self.store.load_items("gdacs")], ["disaster-cur"])
        self.assertEqual([r["item_id"] for r in self.store.load_items("localnews")], ["berlin|germany"])
        self.assertEqual(len(self.store.load_flight_positions()), 1)
        self.assertEqual(len(self.store.load_prices("^GSPC")), 1)
        self.assertEqual([r["output_text"] for r in self.store.list_ai_outputs("assess")], ["new"])
        self.assertEqual(self.store.get_meta("last_retention_ts"), storage.iso(NOW))

    def test_retention_values_match_the_decisions(self):
        self.assertEqual(config.EVENT_RETENTION_DAYS, 7)
        self.assertEqual(config.FLIGHT_TRACK_RETENTION_HOURS, 24)
        self.assertEqual(config.PRICE_RETENTION_DAYS, 730)
        self.assertEqual(config.LOCALNEWS_RETENTION_HOURS, 24)
        self.assertEqual(config.AI_OUTPUT_RETENTION_DAYS, 90)


class CacheLoadTests(_TempStoreCase):
    def test_warm_start_respects_display_windows_and_limits(self):
        s = self.store
        for ev in (_event("news-recent", "geopolitics", 2, published=_iso(NOW - timedelta(hours=2))),
                   _event("news-3d", "geopolitics", 72, published=_iso(NOW - timedelta(hours=72))),
                   _event("disaster-cur", "disaster", 1),
                   _event("disaster-past", "disaster", 1, past=True),
                   _event("markets-^GSPC", "markets", 0.1, symbol="^GSPC", price=1.0)):
            persistence.record_event(s, ev)
        for i in range(20):  # more aircraft than CATEGORY_LIMITS["flight"]
            persistence.record_event(s, _event(f"flight-{i:02d}", "flight", 0.5, icao24=f"{i:02d}",
                                               observed_at=_iso(NOW - timedelta(minutes=30 - i))))
        persistence.record_event(s, _event("flight-stale", "flight", 5, icao24="stale",
                                           observed_at=_iso(NOW - timedelta(hours=5))))
        events = persistence.warm_start_events(s, now=NOW, is_past=_past_if_flagged)
        ids = {e["id"] for e in events}
        self.assertIn("news-recent", ids)
        self.assertNotIn("news-3d", ids)            # outside NEWS_MAX_AGE_HOURS
        self.assertIn("disaster-cur", ids)
        self.assertNotIn("disaster-past", ids)
        self.assertIn("markets-^GSPC", ids)
        self.assertNotIn("flight-stale", ids)       # older than FLIGHT_CACHE_MAX_AGE_HOURS
        flights = [e for e in events if e["category"] == "flight"]
        self.assertEqual(len(flights), 15)
        self.assertIn("flight-19", ids)              # the newest are kept
        self.assertEqual(events, sorted(events, key=lambda e: e["timestamp"]))

    def test_seeded_event_is_refreshed_once_then_deduplicated(self):
        store = EventStore()
        self.assertEqual(store.seed([_event("markets-x", "markets", 1)]), 1)
        self.assertTrue(store.add(_event("markets-x", "markets", 0)))   # fresh copy replaces cache
        self.assertFalse(store.add(_event("markets-x", "markets", 0)))  # then normal dedupe
        self.assertEqual(len(store.snapshot()), 1)

    def test_end_to_end_restart_and_cached_status(self):
        persistence.record_event(self.store, _event("news-1", "geopolitics", 1, published=_iso(NOW - timedelta(hours=1))))
        self.store.mark_success("news", ts=NOW - timedelta(minutes=5), status={"ok": True})
        self.store.close()
        self.store = storage.Storage(self.path)
        store = EventStore()
        self.assertEqual(store.seed(persistence.warm_start_events(self.store, now=NOW, is_past=_never_past)), 1)
        status = persistence.cached_status(self.store, "news", 1)
        local = (NOW - timedelta(minutes=5)).astimezone().strftime("%H:%M")
        self.assertEqual(status["message"], f"from cache (updated {local}) — refreshing…")
        self.assertTrue(status["ok"])
        self.assertIsNone(persistence.cached_status(self.store, "gdacs", 0))

    def test_assess_reuse_lookup(self):
        self.store.save_ai_output("assess", "m", "text", input_data={"cache_key": "assess:e1"},
                                  provenance={"generated_at": _iso(NOW - timedelta(minutes=10))})
        self.assertIsNotNone(persistence.find_recent_ai_output(self.store, "assess", "assess:e1", 1800, now=NOW))
        self.assertIsNone(persistence.find_recent_ai_output(self.store, "assess", "assess:e1", 300, now=NOW))
        self.assertIsNone(persistence.find_recent_ai_output(self.store, "assess", "assess:e2", 1800, now=NOW))


class CorruptAndFailSoftTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def test_corrupt_file_is_moved_aside_and_replaced(self):
        path = os.path.join(self._tmp.name, "gms.db")
        with open(path, "wb") as f:
            f.write(b"this is not a sqlite database" * 200)
        s = storage.Storage(path)
        try:
            self.assertFalse(s.in_memory)
            self.assertIsNotNone(s.recovered_from)
            self.assertTrue(os.path.basename(s.recovered_from).startswith("gms.db.corrupt-"))
            self.assertTrue(os.path.exists(s.recovered_from))
            self.assertTrue(s.upsert_items("news", [("n", NOW, {"a": 1})]) == 1)
        finally:
            s.close()
        with open(s.recovered_from, "rb") as f:
            self.assertTrue(f.read().startswith(b"this is not"))

    def test_unusable_path_falls_back_to_memory(self):
        s = storage.Storage(self._tmp.name)  # a directory, not a file
        try:
            self.assertTrue(s.in_memory)
            self.assertEqual(s.upsert_items("news", [("n", NOW, {"a": 1})]), 1)
        finally:
            s.close()

    def test_errors_after_open_return_defaults(self):
        s = storage.Storage(os.path.join(self._tmp.name, "gms.db"))
        s.close()  # every later call hits a closed connection
        self.assertEqual(s.upsert_items("news", [("n", NOW, {})]), 0)
        self.assertEqual(s.load_items("news"), [])
        self.assertFalse(s.has_item("news", "n"))
        self.assertEqual(s.add_prices([("x", NOW, 1.0, "y")]), 0)
        self.assertIsNone(s.save_ai_output("assess", "m", "t"))
        self.assertFalse(persistence.record_event(s, _event("news-1", "geopolitics")))
        self.assertEqual(persistence.warm_start_events(s, now=NOW, is_past=_never_past), [])
        self.assertIsInstance(persistence.apply_retention(s, now=NOW, is_past=_never_past), dict)


class LocalNewsPersistenceTests(unittest.TestCase):
    """localnews.py with stubbed aiohttp/config/news_feeds."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        stubs = {
            "aiohttp": types.SimpleNamespace(ClientSession=object, ClientTimeout=lambda **k: None),
            "config": types.SimpleNamespace(LOCALNEWS_MAX_RECORDS=8, LOCALNEWS_CACHE_SECONDS=1800,
                                            LOCALNEWS_ERROR_CACHE_SECONDS=45),
            "news_feeds": types.SimpleNamespace(latest_items=[], HEADERS={}, parse_feed=lambda *a: []),
        }
        self._patch = mock.patch.dict(sys.modules, stubs)
        self._patch.start()
        sys.modules.pop("localnews", None)
        self.localnews = importlib.import_module("localnews")

    def tearDown(self):
        sys.modules.pop("localnews", None)
        self._patch.stop()
        self._tmp.cleanup()

    def test_lookup_survives_restart_without_new_request(self):
        path = os.path.join(self._tmp.name, "gms.db")
        s = storage.Storage(path)
        self.localnews.attach_storage(s)
        calls = []

        async def fake_google(session, city, country):
            calls.append(city)
            return [{"title": "Story - Outlet", "url": "https://x/1", "source": "Outlet",
                     "published": None, "summary": ""}]

        self.localnews._google_news = fake_google
        result = asyncio.run(self.localnews.fetch_local_news(None, "Berlin", "Germany"))
        self.assertEqual(result["articles"][0]["title"], "Story")
        s.close()

        self.localnews._cache.clear()  # "restart": fresh module state, same DB file
        s = storage.Storage(path)
        self.assertEqual(self.localnews.attach_storage(s), 1)
        result = asyncio.run(self.localnews.fetch_local_news(None, "Berlin", "Germany"))
        self.assertEqual(result["articles"][0]["title"], "Story")
        self.assertEqual(calls, ["Berlin"])
        s.close()


if __name__ == "__main__":
    unittest.main()
