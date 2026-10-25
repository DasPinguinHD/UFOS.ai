"""Offline tests for the SQLite store additions of 2026-09-25 evening
(newsroom/db.py): data/newsroom.db path + one-time legacy copy, schema v2
(trading tables), corrupt-file quarantine, per-type retention (insider 5 days,
AI 90 days, calendar event date + 1 day, prices 2 years), dedupe by stable id,
cache load after a restart and the storage info for GET /newsroom/status.
Stdlib unittest; every test uses a temp directory — the real database is
never touched.

Run from HedgeFund/backend:  python -m unittest tests.test_storage -v
"""
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_db():
    # db.py is stdlib-only; load it under a private name so no package state is shared.
    spec = importlib.util.spec_from_file_location("_storage_test_db", os.path.join(_BACKEND, "newsroom", "db.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


db = _load_db()
NOW = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
TRADING = ("prices", "signals", "orders", "positions", "equity_curve")


def filing(acc, filed_at, ticker="ABC", details=True):
    return {"id": f"insider-{acc}", "category": "insider", "title": f"T {acc}", "summary": "s",
            "timestamp": filed_at, "filed_at": filed_at,
            "details": {"issuer_ticker": ticker, "issuer_name": "Abc Inc", "owner_name": "Doe Jane",
                        "transactions": []} if details else None}


class StorageCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved_env = os.environ.pop(db.ENV_PATH, None)
        self.path = os.path.join(self._tmp.name, "data", "newsroom.db")
        self.assertTrue(db.init(self.path, legacy=""))

    def tearDown(self):
        db.close()
        if self._saved_env is not None:
            os.environ[db.ENV_PATH] = self._saved_env
        self._tmp.cleanup()

    def raw(self, sql, params=()):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def reopen(self):
        db.close()
        self.assertTrue(db.init(self.path, legacy=""))


class SchemaTest(StorageCase):
    def test_schema_version_and_trading_tables(self):
        self.assertEqual(db.SCHEMA_VERSION, 4)            # v4 (2026-10-24): disclosure tables (INSIDER-RET)
        self.assertEqual(self.raw("PRAGMA user_version")[0][0], 4)
        tables = {r[0] for r in self.raw("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue(set(db.TABLES) <= tables)
        cols = {t: [r[1] for r in self.raw(f"PRAGMA table_info({t})")] for t in TRADING}
        self.assertEqual(cols["prices"], ["symbol", "ts", "open", "high", "low", "close", "volume", "source"])
        self.assertEqual(cols["signals"], ["id", "ts", "source", "symbol", "kind", "value", "level", "payload_json"])
        self.assertEqual(cols["orders"], ["id", "ts", "symbol", "side", "qty", "order_type", "limit_price", "status",
                                          "broker", "broker_order_id", "mode", "rationale", "provenance_json",
                                          # v3 (fund/SPEC.md §2)
                                          "run_id", "recommendation_id", "origin", "fill_price", "fees",
                                          "filled_at", "reject_reason", "notional"])
        self.assertEqual(cols["positions"], ["symbol", "qty", "avg_price", "updated_at",
                                             "opened_at", "realized_pnl", "borrow_rate_pct", "last_price"])
        self.assertEqual(cols["equity_curve"], ["ts", "equity", "cash", "mode",
                                                "long_mv", "short_mv", "benchmark", "drawdown_pct"])

    def test_orders_mode_defaults_to_paper_and_is_checked(self):
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("INSERT INTO orders(ts, symbol, side, qty, order_type, status) "
                         "VALUES ('2026-09-25T12:00:00+00:00', 'SPY', 'buy', 1, 'market', 'new')")
            self.assertEqual(conn.execute("SELECT mode FROM orders").fetchone()[0], "paper")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO orders(ts, symbol, side, qty, order_type, status, mode) "
                             "VALUES ('x', 'SPY', 'buy', 1, 'market', 'new', 'yolo')")
        finally:
            conn.close()

    def test_v1_database_is_upgraded_in_place(self):
        db.close()
        v1 = os.path.join(self._tmp.name, "v1.db")
        conn = sqlite3.connect(v1)
        for sql in db.MIGRATIONS[0][1]:
            conn.execute(sql)
        conn.execute("PRAGMA user_version = 1")
        conn.execute("INSERT INTO meta(key, value) VALUES ('k', 'v')")
        conn.commit()
        conn.close()
        self.assertTrue(db.init(v1, legacy=""))
        self.assertEqual(db.meta_get("k"), "v")
        self.assertEqual(db.storage_info()["schema_version"], db.SCHEMA_VERSION)
        self.assertEqual(db.count_rows("equity_curve"), 0)


class DedupeAndCacheTest(StorageCase):
    def test_insider_upsert_dedupes_and_survives_restart(self):
        db.upsert_insider(filing("0001", "2026-09-24T10:00:00-04:00"), now="2026-09-25T11:00:00+00:00")
        db.upsert_insider(filing("0001", "2026-09-24T10:00:00-04:00", details=False), now="2026-09-25T11:02:00+00:00")
        db.upsert_insider(filing("0002", "2026-09-25T09:00:00+00:00"), now="2026-09-25T11:02:00+00:00")
        self.assertEqual(db.count_rows("insider_filings"), 2)
        self.reopen()   # backend restart: cache load
        stored = db.load_insider("2026-09-20T00:00:00+00:00")
        self.assertEqual([e["id"] for e in stored], ["insider-0002", "insider-0001"])
        self.assertIsNotNone(stored[1]["details"])

    def test_calendar_and_macro_and_meta_survive_restart(self):
        db.save_calendar({"events": [{"date": "2026-09-30", "title": "CPI"}], "updated": "u", "window_days": 45})
        db.replace_observations("DGS10", "lin", "native", [{"date": "2026-09-24", "value": 4.1}])
        db.replace_observations("DGS10", "lin", "native", [{"date": "2026-09-24", "value": 4.2}])  # same key: upsert
        db.meta_set(db.last_ok_key("fred"), "2026-09-25T10:00:00+00:00")
        self.reopen()
        self.assertEqual(db.load_calendar()["events"][0]["title"], "CPI")
        self.assertEqual(db.load_observations("native"), {("DGS10", "lin", "native"): [{"date": "2026-09-24", "value": 4.2}]})
        self.assertEqual(db.meta_get("last_ok:fred"), "2026-09-25T10:00:00+00:00")


class RetentionTest(StorageCase):
    def test_prune_per_data_type(self):
        db.upsert_insider_many([filing("keep", "2026-09-21T13:00:00+00:00"),     # 4 days
                                filing("drop", "2026-09-19T12:00:00+00:00")],    # 6 days
                               now="2026-09-25T11:00:00+00:00")
        # no parsable filed_at -> first_seen (insert time) is used
        db.upsert_insider(filing("nofiled", "garbage"), now="2026-09-01T00:00:00+00:00")
        db.insert_ai_result("analysis", "m", "standard", {"tiles_used": 2}, "new", created_at="2026-07-01T00:00:00+00:00")
        db.insert_ai_result("analysis", "m", "standard", {"tiles_used": 2}, "old", created_at="2026-06-20T00:00:00+00:00")
        db.save_calendar({"events": [{"date": "2026-09-23", "title": "past"},
                                     {"date": "2026-09-24", "title": "yesterday"},
                                     {"date": "2026-09-26", "title": "future"}], "updated": "u"})
        conn = sqlite3.connect(self.path)
        conn.executemany("INSERT INTO prices(symbol, ts, close) VALUES (?, ?, ?)",
                         [("SPY", "2024-09-01T00:00:00+00:00", 1.0), ("SPY", "2026-09-24T00:00:00+00:00", 2.0)])
        conn.execute("INSERT INTO signals(ts, source, kind) VALUES ('2000-01-01', 'x', 'y')")
        conn.execute("INSERT INTO equity_curve(ts, equity) VALUES ('2000-01-01', 1)")
        conn.commit()
        conn.close()

        res = db.prune(now=NOW)
        self.assertEqual(res, {"insider": 2, "ai_results": 1, "calendar_events": 1, "prices": 1})
        self.assertEqual([r[0] for r in self.raw("SELECT accession FROM insider_filings")], ["keep"])
        self.assertEqual([i["output_text"] for i in db.ai_history()], ["new"])
        self.assertEqual([e["title"] for e in db.load_calendar()["events"]], ["yesterday", "future"])
        self.assertEqual(db.count_rows("prices"), 1)
        self.assertEqual(db.count_rows("signals"), 1)        # not pruned (yet)
        self.assertEqual(db.count_rows("equity_curve"), 1)
        self.assertEqual(db.prune(now=NOW), {"insider": 0, "ai_results": 0, "calendar_events": 0, "prices": 0})


class RecoveryTest(StorageCase):
    def test_corrupt_file_is_quarantined_and_replaced(self):
        db.meta_set("k", "v")
        db.close()
        for suffix in ("-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.remove(self.path + suffix)
        with open(self.path, "wb") as fh:
            fh.write(b"this is definitely not an sqlite database" * 100)
        self.assertTrue(db.init(self.path, legacy=""))
        self.assertEqual(db.storage_info()["schema_version"], db.SCHEMA_VERSION)
        self.assertIsNone(db.meta_get("k"))               # fresh database
        moved = [f for f in os.listdir(os.path.dirname(self.path)) if ".corrupt-" in f]
        self.assertEqual(len(moved), 1)
        self.assertTrue(moved[0].startswith("newsroom.db.corrupt-"))

    def test_unopenable_path_is_not_renamed_and_disables_store(self):
        db.close()
        folder = os.path.join(self._tmp.name, "a_dir.db")
        os.makedirs(folder)                                # a directory where the file should be
        self.assertFalse(db.init(folder, legacy=""))
        self.assertFalse(db.is_enabled())
        self.assertTrue(os.path.isdir(folder))
        self.assertIsNone(db.storage_info())
        self.assertEqual(db.upsert_insider(filing("x", "2026-09-25T00:00:00+00:00")), 0)  # no raise

    def test_legacy_database_is_copied_once(self):
        db.close()
        legacy = os.path.join(self._tmp.name, "lad", "hedgefund.sqlite")
        self.assertTrue(db.init(legacy, legacy=""))
        db.upsert_insider(filing("L1", "2026-09-24T00:00:00+00:00"))
        db.close()
        target = os.path.join(self._tmp.name, "new", "data", "newsroom.db")
        self.assertTrue(db.init(target, legacy=legacy))
        self.assertEqual(db.count_rows("insider_filings"), 1)
        db.upsert_insider(filing("N1", "2026-09-24T00:00:00+00:00"))
        db.close()
        self.assertTrue(db.init(target, legacy=legacy))   # target exists: no second copy
        self.assertEqual(db.count_rows("insider_filings"), 2)
        self.assertTrue(os.path.exists(legacy))            # legacy left in place


class StorageInfoTest(StorageCase):
    def test_storage_info(self):
        db.upsert_insider(filing("0001", "2026-09-24T00:00:00+00:00"))
        info = db.storage_info()
        self.assertEqual(info["path"], self.path)
        self.assertGreater(info["size_bytes"], 0)
        self.assertEqual(set(info["rows"]), set(db.TABLES))
        self.assertEqual(info["rows"]["insider_filings"], 1)


if __name__ == "__main__":
    unittest.main()
