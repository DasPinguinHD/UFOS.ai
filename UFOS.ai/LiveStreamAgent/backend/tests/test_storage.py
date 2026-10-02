"""Tests for the LiveStreamAgent local SQLite storage (storage.py) and its use
in main.py (verdict history restore, quote/price persistence), 2026-09-25.
stdlib unittest only, temp DB files; main.py's third-party imports are stubbed.

Run from LiveStreamAgent/backend:
    python -m unittest discover -s tests -t .
"""
import importlib
import json
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

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def _import_main():
    """main.py with its heavy/third-party imports stubbed (no venv needed)."""
    class _Web(types.SimpleNamespace):
        pass
    web = _Web(Response=object, json_response=lambda *a, **k: None, Application=object,
               AppRunner=object, TCPSite=object)
    stubs = {
        "aiohttp": types.SimpleNamespace(web=web),
        "aiohttp.web": web,
        "audio_stream": types.SimpleNamespace(pcm_chunks=lambda *a: None),
        "broadcast": types.SimpleNamespace(Broadcaster=object),
        "config": types.SimpleNamespace(YOUTUBE_URL="", VERDICT_INTERVAL_SECONDS=90, VERDICT_MAX_REASON_CHARS=720,
                                        VERDICT_INITIAL_DELAY_SECONDS=0, MARKET_DATA_POLL_SECONDS=15,
                                        HTTP_HOST="127.0.0.1", DEFAULT_ALLOWED_ORIGINS=()),
        "llm_analyst": types.SimpleNamespace(LLMAnalyst=object),
        "logger": types.SimpleNamespace(log_event=lambda *a, **k: None),
        "market_data": types.SimpleNamespace(MarketDataFeed=object, REFRESH_SOURCE="market"),
        "transcription": types.SimpleNamespace(transcribe_stream=None),
    }
    with mock.patch.dict(sys.modules, stubs):
        sys.modules.pop("main", None)
        module = importlib.import_module("main")
    sys.modules.pop("main", None)
    return module


class _TempStoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "sub", "livestream.db")
        self.store = storage.Storage(self.path)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()


class OpenAndMigrateTests(_TempStoreCase):
    def test_file_created_in_wal_mode_with_schema(self):
        self.assertFalse(self.store.in_memory)
        self.assertEqual(self.store.schema_version(), storage.MIGRATIONS[-1][0])
        conn = sqlite3.connect(self.path)
        try:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        self.assertTrue({"items", "prices", "ai_outputs", "source_state", "meta"} <= tables)

    def test_default_path_and_env_override(self):
        with mock.patch.dict(os.environ, {storage.DB_ENV: "/x/y.db"}):
            self.assertEqual(storage.default_path(), "/x/y.db")
        with mock.patch.dict(os.environ, {}):
            os.environ.pop(storage.DB_ENV, None)
            self.assertEqual(storage.default_path(), os.path.join(BACKEND, "data", "livestream.db"))

    def test_reopen_keeps_data(self):
        self.store.save_ai_output("verdict", "m", "{}", ts=NOW)
        self.store.close()
        self.store = storage.Storage(self.path)
        storage.Storage._migrate(self.store._conn)  # once more: no-op
        self.assertEqual(len(self.store.list_ai_outputs(feature="verdict")), 1)


class UpsertDedupeTests(_TempStoreCase):
    def test_items_same_id_one_row_newest_wins(self):
        self.store.upsert_items(storage.SOURCE_QUOTE, [{"item_id": "QQQ", "item_ts": NOW, "payload": {"p": 1}}])
        self.store.upsert_items(storage.SOURCE_QUOTE, [{"item_id": "QQQ", "item_ts": NOW, "payload": {"p": 2}},
                                                       {"item_id": "", "payload": {}}])
        rows = self.store.load_items(storage.SOURCE_QUOTE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"], {"p": 2})

    def test_prices_one_row_per_symbol_ts_source(self):
        self.store.add_prices([("QQQ", NOW, 1.0, "yahoo"), ("QQQ", NOW, 1.5, "yahoo"),
                               ("QQQ", NOW + timedelta(minutes=5), 2.0, "yahoo"), ("QQQ", NOW, None, "yahoo")])
        self.assertEqual([p[1] for p in self.store.load_prices("QQQ")], [1.5, 2.0])

    def test_meta_roundtrip(self):
        self.assertEqual(self.store.get_meta("k", "d"), "d")
        self.store.set_meta("k", [1, 2])
        self.assertEqual(self.store.get_meta("k"), [1, 2])


class RetentionTests(_TempStoreCase):
    def test_run_retention_per_data_type(self):
        s = self.store
        s.save_ai_output("verdict", "m", "new", ts=NOW - timedelta(days=89))
        s.save_ai_output("analysis", "m", "old", ts=NOW - timedelta(days=91))
        s.upsert_items(storage.SOURCE_TRANSCRIPT, [{"item_id": "a", "item_ts": NOW - timedelta(days=6), "payload": {}},
                                                   {"item_id": "b", "item_ts": NOW - timedelta(days=8), "payload": {}}])
        s.upsert_items(storage.SOURCE_QUOTE, [{"item_id": "QQQ", "item_ts": NOW - timedelta(days=3), "payload": {}}])
        s.add_prices([("QQQ", NOW - timedelta(days=700), 1.0, "yahoo"),
                      ("QQQ", NOW - timedelta(days=740), 1.0, "yahoo")])
        counts = s.run_retention(now=NOW)
        self.assertEqual(counts, {"ai_outputs": 1, "prices": 1, "items:transcript": 1, "items:quote": 1})
        self.assertEqual([r["output_text"] for r in s.list_ai_outputs()], ["new"])
        self.assertEqual([r["item_id"] for r in s.load_items(storage.SOURCE_TRANSCRIPT)], ["a"])
        self.assertEqual(len(s.load_prices("QQQ")), 1)
        self.assertEqual(s.get_meta("last_retention_ts"), storage.iso(NOW))

    def test_retention_values_match_the_decisions(self):
        self.assertEqual(storage.AI_OUTPUT_RETENTION_DAYS, 90)
        self.assertEqual(storage.TRANSCRIPT_RETENTION_DAYS, 7)
        self.assertEqual(storage.PRICE_RETENTION_DAYS, 730)


class CacheLoadTests(_TempStoreCase):
    def test_verdict_history_restored_newest_first_in_broadcast_shape(self):
        main = _import_main()
        for i in range(3):
            prov = {"generated_at": (NOW - timedelta(minutes=10 - i)).isoformat(), "model": "m"}
            self.store.save_ai_output("verdict", "m", json.dumps({"verdict": f"V{i}", "ticker": "QQQ",
                                                                  "confidence": 50, "reason": "r"}),
                                      input_data={"transcript": "t"}, provenance=prov, usage={"cost_usd": 0.001})
        self.store.save_ai_output("analysis", "m", json.dumps({"summary": "x"}), ts=NOW)
        self.store.save_ai_output("verdict", "m", "not json", ts=NOW)
        self.store.close()
        self.store = storage.Storage(self.path)  # "restart"
        history = main._load_verdict_history(self.store, 500)
        self.assertEqual([v["verdict"] for v in history], ["V2", "V1", "V0"])
        self.assertEqual(history[0]["provenance"]["model"], "m")
        self.assertEqual(history[0]["usage"], {"cost_usd": 0.001})

    def test_store_quotes_writes_latest_quote_and_throttled_price_points(self):
        main = _import_main()
        main._last_price_point.clear()
        payload = {"timestamp": NOW.isoformat(), "quotes": {"QQQ": {"price": 1.0, "change_percent": 0.1},
                                                            "TLT": {"price": None, "change_percent": None}}}
        main._store_quotes(self.store, payload)
        main._store_quotes(self.store, dict(payload, timestamp=(NOW + timedelta(minutes=1)).isoformat()))
        self.assertEqual(len(self.store.load_prices("QQQ")), 1)          # throttled
        self.assertEqual(len(self.store.load_items(storage.SOURCE_QUOTE)), 1)
        self.assertEqual(self.store.get_state("quotes")["info"], {"symbols": 1, "missing": 1})


class CorruptAndFailSoftTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def test_corrupt_file_is_moved_aside_and_replaced(self):
        path = os.path.join(self._tmp.name, "livestream.db")
        with open(path, "wb") as f:
            f.write(b"garbage, not sqlite" * 300)
        s = storage.Storage(path)
        try:
            self.assertFalse(s.in_memory)
            self.assertTrue(os.path.basename(s.recovered_from).startswith("livestream.db.corrupt-"))
            self.assertTrue(os.path.exists(s.recovered_from))
            self.assertIsNotNone(s.save_ai_output("verdict", "m", "{}"))
        finally:
            s.close()

    def test_unusable_path_falls_back_to_memory(self):
        s = storage.Storage(self._tmp.name)  # a directory
        try:
            self.assertTrue(s.in_memory)
            self.assertIsNotNone(s.save_ai_output("verdict", "m", "{}"))
        finally:
            s.close()

    def test_errors_after_open_return_defaults(self):
        s = storage.Storage(os.path.join(self._tmp.name, "livestream.db"))
        s.close()
        self.assertIsNone(s.save_ai_output("verdict", "m", "{}"))
        self.assertEqual(s.list_ai_outputs(), [])
        self.assertEqual(s.upsert_items("quote", [{"item_id": "x", "payload": {}}]), 0)
        self.assertEqual(s.add_prices([("x", NOW, 1.0, "y")]), 0)
        self.assertIsInstance(s.run_retention(now=NOW), dict)


if __name__ == "__main__":
    unittest.main()
