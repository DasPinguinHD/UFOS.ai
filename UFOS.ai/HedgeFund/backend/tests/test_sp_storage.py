"""strategy_platform/storage/db.py — hedgefund.db (SPEC §5): migrations, WAL,
corrupt-file quarantine, reopen after a failed open, and the meta / audit /
data_sources / bars helpers. Failures raise StorageUnavailable, "not found"
is None. Temp databases only (outside the repo), never the real file.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_storage -v
"""
import glob
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone

from strategy_platform.core.errors import UserFacingError
from strategy_platform.storage import db


def bar(ts="2026-10-01", **kw):
    row = dict(symbol="SPY", timeframe="1Day", ts=ts, open=100.0, high=101.0, low=99.0, close=100.5,
               volume=1000.0, vwap=100.2, trade_count=42, source="alpaca", known_at="2026-10-01T20:00:00Z",
               fetched_at="2026-10-02T00:00:00Z")
    row.update(kw)
    return row


class TempDb(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sp-storage-")
        self.path = os.path.join(self.dir, "data", "hedgefund.db")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.addCleanup(db.close)
        db.close()
        self.assertEqual(db.init(self.path), db.SCHEMA_VERSION)


class OpenTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sp-storage-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.addCleanup(db.close)
        db.close()

    def test_not_opened_raises(self):
        with self.assertRaises(db.StorageUnavailable) as cm:
            db.meta_get("x")
        self.assertIsInstance(cm.exception, UserFacingError)
        self.assertIn("hedgefund.db", cm.exception.user_message)

    def test_default_path(self):
        self.assertTrue(db.default_path().endswith(os.path.join("backend", "data", "hedgefund.db")))

    def test_init_wal_schema_and_idempotent(self):
        path = os.path.join(self.dir, "h.db")
        self.assertEqual(db.init(path), db.SCHEMA_VERSION)
        self.assertEqual(db.init(path), db.SCHEMA_VERSION)
        conn = sqlite3.connect(path)
        try:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        self.assertTrue(set(db.TABLES) <= tables)
        info = db.storage_info()
        self.assertEqual((info["schema_version"], info["rows"]["bars"]), (db.SCHEMA_VERSION, 0))
        self.assertEqual(info["path"], os.path.abspath(path))

    def test_corrupt_file_is_quarantined(self):
        path = os.path.join(self.dir, "h.db")
        with open(path, "wb") as fh:
            fh.write(b"this is not a sqlite database" * 100)
        self.assertEqual(db.init(path), db.SCHEMA_VERSION)
        self.assertEqual(len(glob.glob(path + ".corrupt-*")), 1)
        self.assertIsNone(db.meta_get("anything"))

    def test_newer_schema_is_refused_not_quarantined(self):
        path = os.path.join(self.dir, "h.db")
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA user_version = 99")
        conn.commit()
        conn.close()
        with self.assertRaises(db.StorageUnavailable):
            db.init(path)
        self.assertEqual(glob.glob(path + ".corrupt-*"), [])
        self.assertFalse(db.is_open())

    def test_failed_open_is_retried_later(self):
        blocker = os.path.join(self.dir, "blocker")
        with open(blocker, "w") as fh:
            fh.write("x")
        path = os.path.join(blocker, "sub", "h.db")            # parent is a file: cannot be created
        with self.assertRaises(db.StorageUnavailable):
            db.init(path)
        with self.assertRaises(db.StorageUnavailable):        # within the reopen interval: no retry
            db.meta_get("k")
        os.remove(blocker)
        saved = db.REOPEN_INTERVAL_SECONDS
        db.REOPEN_INTERVAL_SECONDS = 0
        try:
            db.meta_set("k", "v")                              # reopened on use
        finally:
            db.REOPEN_INTERVAL_SECONDS = saved
        self.assertEqual(db.meta_get("k"), "v")
        self.assertEqual(db.db_path(), os.path.abspath(path))


class MetaAuditTest(TempDb):
    def test_meta(self):
        self.assertIsNone(db.meta_get("a"))
        db.meta_set("a", 5)
        self.assertEqual(db.meta_get("a"), "5")
        db.meta_set("a", "six")
        self.assertEqual(db.meta_get("a"), "six")
        db.meta_set("a", None)
        self.assertIsNone(db.meta_get("a"))
        with self.assertRaises(ValueError):
            db.meta_get("")

    def test_audit(self):
        first = db.add_audit("user", "key_set", {"name": "fred-api-key"})
        second = db.add_audit("system", "startup")
        self.assertEqual(second, first + 1)
        rows = db.list_audit()
        self.assertEqual([r["action"] for r in rows], ["startup", "key_set"])
        self.assertEqual(rows[1]["detail"], {"name": "fred-api-key"})
        self.assertIsNone(rows[0]["detail"])
        self.assertTrue(rows[0]["ts"].endswith("+00:00"))
        self.assertEqual(len(db.list_audit(action="key_set")), 1)
        with self.assertRaises(ValueError):
            db.add_audit("hacker", "x")
        with self.assertRaises(ValueError):
            db.add_audit("user", "")


class DataSourcesTest(TempDb):
    def test_set_get_status(self):
        self.assertIsNone(db.data_source_get("equity_bars"))
        self.assertIsNone(db.data_source_status_update("equity_bars", True, "ok"))
        row = db.data_source_set("equity_bars", "alpaca", licence="personal_use", licence_note="account holder")
        self.assertEqual((row["source"], row["status"], row["licence"]), ("alpaca", None, "personal_use"))
        row = db.data_source_status_update("equity_bars", False, "Alpaca rejected the key",
                                           at="2026-10-02T12:00:00Z")
        self.assertEqual((row["status"], row["last_error_at"], row["message"]),
                         ("error", "2026-10-02T12:00:00+00:00", "Alpaca rejected the key"))
        row = db.data_source_status_update("equity_bars", True)
        self.assertEqual(row["status"], "ok")
        self.assertIsNotNone(row["last_ok_at"])
        self.assertEqual(row["last_error_at"], "2026-10-02T12:00:00+00:00")   # history kept
        # same source again: status kept; another source: status cleared
        self.assertEqual(db.data_source_set("equity_bars", "alpaca")["status"], "ok")
        row = db.data_source_set("equity_bars", "other")
        self.assertEqual((row["source"], row["status"], row["last_ok_at"]), ("other", None, None))
        db.data_source_set("macro", "fred", licence="commercial_ok")
        self.assertEqual([r["data_type"] for r in db.data_sources_list()], ["equity_bars", "macro"])

    def test_validation(self):
        with self.assertRaises(ValueError):
            db.data_source_set("equity_bars", "alpaca", licence="free-for-all")
        db.data_source_set("equity_bars", "alpaca")
        with self.assertRaises(ValueError):
            db.data_source_status_update("equity_bars", "yes")
        with self.assertRaises(ValueError):
            db.data_source_status_update("equity_bars", True, at="not a time")


class BarsTest(TempDb):
    def test_upsert_query_latest(self):
        self.assertIsNone(db.latest_bar_ts("SPY", "1Day", "alpaca"))
        self.assertEqual(db.query_bars("SPY", "1Day", None, None, "alpaca"), [])
        n = db.upsert_bars([bar("2026-09-30", known_at="2026-09-30T20:00:00Z"), bar("2026-10-01"),
                            bar("2026-10-01", source="other", close=1.0),
                            bar(datetime(2026, 10, 2, tzinfo=timezone.utc), symbol="spy",
                                known_at="2026-10-02T20:00:00Z", vwap=None, trade_count=None, volume=None)])
        self.assertEqual(n, 4)
        rows = db.query_bars("SPY", "1Day", None, None, "alpaca")
        self.assertEqual([r["ts"] for r in rows], ["2026-09-30T00:00:00+00:00", "2026-10-01T00:00:00+00:00",
                                                   "2026-10-02T00:00:00+00:00"])
        self.assertEqual(set(rows[0]), set(db.BAR_COLUMNS))
        self.assertIsNone(rows[2]["vwap"])
        self.assertEqual(db.latest_bar_ts("spy", "1Day", "alpaca"), "2026-10-02T00:00:00+00:00")
        # one source only, inclusive range, point in time
        self.assertEqual(db.query_bars("SPY", "1Day", None, None, "other")[0]["close"], 1.0)
        self.assertEqual(len(db.query_bars("SPY", "1Day", "2026-10-01", "2026-10-01T00:00:00+00:00", "alpaca")), 1)
        self.assertEqual(len(db.query_bars("SPY", "1Day", "2026-10-01", None, "alpaca", known_by="2026-10-01T21:00:00Z")), 1)
        self.assertEqual(db.query_bars("SPY", "1Hour", None, None, "alpaca"), [])
        # upsert replaces the same key
        db.upsert_bars([bar("2026-10-01", close=200.0)])
        self.assertEqual(db.query_bars("SPY", "1Day", "2026-10-01", "2026-10-01", "alpaca")[0]["close"], 200.0)
        self.assertEqual(db.upsert_bars([]), 0)

    def test_invalid_rows_write_nothing(self):
        bad_rows = [
            [bar(), bar(close=float("nan"))],
            [bar(), bar(open="x")],
            [bar(), bar(ts="yesterday")],
            [bar(), bar(source="")],
            [bar(), bar(extra=1)],
            [bar(), bar(trade_count=1.5)],
            [bar(), bar(high=None)],
            [bar(), "not a dict"],
        ]
        for rows in bad_rows:
            with self.assertRaises(ValueError):
                db.upsert_bars(rows)
        self.assertEqual(db.query_bars("SPY", "1Day", None, None, "alpaca"), [])
        with self.assertRaises(ValueError):
            db.query_bars("SPY", "1Day", "garbage", None, "alpaca")
        with self.assertRaises(ValueError):
            db.query_bars("SPY", "1Day", None, None, None)        # source is required (no mixing)

    def test_sqlite_failure_raises_storage_unavailable(self):
        conn = sqlite3.connect(self.path)
        conn.execute("DROP TABLE bars")
        conn.commit()
        conn.close()
        with self.assertRaises(db.StorageUnavailable) as cm:
            db.query_bars("SPY", "1Day", None, None, "alpaca")
        self.assertIn("database operation failed", cm.exception.user_message)
        with self.assertRaises(db.StorageUnavailable):
            db.upsert_bars([bar()])


if __name__ == "__main__":
    unittest.main()
