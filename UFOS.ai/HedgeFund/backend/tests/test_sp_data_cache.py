"""strategy_platform/data/cache.py on a temp hedgefund.db with a fake
provider: cached history served without requests, explicit freshness
(never stale bars as current), unfinished bars never stored, adjustment
changes refetched, head ranges fetched once. Offline.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_data_cache -v
"""
import unittest
from datetime import timedelta

try:
    from test_sp_data_support import FakeBarProvider, TempDb, crypto_row, equity_row, utc
except ImportError:  # pragma: no cover - run as package
    from tests.test_sp_data_support import FakeBarProvider, TempDb, crypto_row, equity_row, utc

from strategy_platform.core import calendars
from strategy_platform.core.errors import DataSourceUnavailable
from strategy_platform.data.cache import BarCache, is_fresh

SEP = ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29",
       "2026-09-30", "2026-10-01", "2026-10-02"]


class CacheCase(unittest.TestCase):
    def setUp(self):
        self._db = TempDb()
        self.db = self._db.__enter__()
        self.now = utc(2026, 10, 2, 21, 0)                    # Fri after the close
        self.prov = FakeBarProvider()
        self.prov.bars["SPY"] = [equity_row(d, 600 + i) for i, d in enumerate(SEP)]
        self.cache = BarCache(fetch=self.prov.fetch, configured_source=lambda dt: "alpaca", clock=lambda: self.now)

    def tearDown(self):
        self._db.__exit__(None, None, None)

    def get(self, symbol="SPY", start=utc(2026, 9, 21), fresh="close"):
        if fresh == "close":
            fresh = calendars.last_close_before(self.now)
        return self.cache.get_bars(symbol, "1Day", start, require_fresh_through=fresh)


class FreshnessTest(CacheCase):
    def test_first_call_fetches_and_stores_then_cache_only(self):
        rows = self.get()
        self.assertEqual([r["ts"][:10] for r in rows], SEP)
        self.assertEqual(len(self.prov.calls), 1)
        self.assertEqual(len(self.db.query_bars("SPY", "1Day", None, None, "alpaca")), 10)
        self.assertEqual(self.get(), rows)
        self.assertEqual(len(self.prov.calls), 1)                 # history is served from hedgefund.db

    def test_new_session_requires_new_bar_and_never_returns_stale(self):
        self.get()
        self.now = utc(2026, 10, 5, 21, 0)                        # Mon after the close: Monday's bar needed
        self.prov.fail = DataSourceUnavailable("alpaca", "equity_bars", "network error")
        with self.assertRaises(DataSourceUnavailable) as cm:
            self.get()
        self.assertIn("network error", cm.exception.user_message)
        self.prov.fail = None                                     # source works but has no Monday bar yet
        with self.assertRaises(DataSourceUnavailable) as cm:
            self.get()
        self.assertIn("no 1Day bar for SPY through 2026-10-05T20:00:00+00:00", cm.exception.user_message)
        self.prov.bars["SPY"].append(equity_row("2026-10-05", 650.0))
        rows = self.get()
        self.assertEqual(rows[-1]["ts"][:10], "2026-10-05")
        start_of_tail = self.prov.calls[-1][4]                    # tail fetch starts at the newest cached bar
        self.assertEqual(start_of_tail, utc(2026, 10, 2, 4, 0))

    def test_weekend_needs_no_request(self):
        self.get()
        self.now = utc(2026, 10, 4, 12, 0)                        # Sunday: Friday's bar is the newest needed
        self.get()
        self.assertEqual(len(self.prov.calls), 1)

    def test_unfinished_bar_never_stored_or_returned(self):
        self.now = utc(2026, 10, 2, 15, 0)                        # Fri during the session
        self.prov.bars["SPY"][-1] = equity_row("2026-10-02", 999.0)   # partial bar, known_at 20:00
        rows = self.get()
        self.assertEqual(rows[-1]["ts"][:10], "2026-10-01")
        self.assertEqual(self.db.latest_bar_ts("SPY", "1Day", "alpaca")[:10], "2026-10-01")

    def test_crypto_fresh_through_now(self):
        self.prov.bars["BTC-USD"] = [crypto_row(f"2026-09-{d:02d}", 60000 + d) for d in range(20, 31)] + \
            [crypto_row("2026-10-01", 61000.0), crypto_row("2026-10-02", 62000.0)]
        rows = self.get("BTC-USD", start=utc(2026, 9, 20), fresh=self.now)
        self.assertEqual(rows[-1]["ts"][:10], "2026-10-01")       # Oct 2 completes at Oct 3 00:00 UTC
        self.now = utc(2026, 10, 3, 1, 0)
        rows = self.get("BTC-USD", start=utc(2026, 9, 20), fresh=self.now)
        self.assertEqual(rows[-1]["ts"][:10], "2026-10-02")
        self.assertEqual(len(self.prov.calls), 2)

    def test_is_fresh(self):
        rows = [equity_row("2026-10-01", 1.0)]
        self.assertTrue(is_fresh(rows, utc(2026, 10, 1, 20), "1Day"))
        self.assertFalse(is_fresh(rows, utc(2026, 10, 2, 20), "1Day"))
        self.assertFalse(is_fresh([], utc(2026, 10, 2, 20), "1Day"))

    def test_bad_arguments(self):
        with self.assertRaises(ValueError):
            self.cache.get_bars("SPY", "1Week", utc(2026, 9, 1))
        with self.assertRaises(ValueError):
            self.cache.get_bars("SPY", "1Day", utc(2026, 9, 1), require_fresh_through=self.now + timedelta(hours=1))


class HistoryTest(CacheCase):
    def test_adjustment_change_refetches_everything(self):
        self.get()
        self.now = utc(2026, 10, 5, 21, 0)
        # a dividend went ex on Monday: every earlier adjusted price changes
        self.prov.bars["SPY"] = [equity_row(d, (600 + i) * 0.99) for i, d in enumerate(SEP)] + \
            [equity_row("2026-10-05", 650.0)]
        rows = self.get()
        self.assertAlmostEqual(rows[0]["close"], 600 * 0.99)
        self.assertEqual(len(self.prov.calls), 3)                 # initial, tail, full refetch
        stored = self.db.query_bars("SPY", "1Day", None, None, "alpaca")
        self.assertAlmostEqual(stored[0]["close"], 600 * 0.99)

    def test_head_fetched_once_and_history_start_remembered(self):
        self.get(start=utc(2026, 9, 28))
        self.assertEqual(len(self.prov.calls), 1)
        rows = self.get(start=utc(2026, 9, 1))                    # earlier history wanted
        self.assertEqual(rows[0]["ts"][:10], "2026-09-21")
        self.assertEqual(len(self.prov.calls), 2)
        self.get(start=utc(2026, 9, 1))                           # the source has nothing before Sep 21
        self.assertEqual(len(self.prov.calls), 2)

    def test_past_range_without_freshness_is_history(self):
        self.get()
        rows = self.cache.get_bars("SPY", "1Day", utc(2026, 9, 22), utc(2026, 9, 26))
        self.assertEqual([r["ts"][:10] for r in rows], SEP[1:5])
        self.assertEqual(len(self.prov.calls), 1)


if __name__ == "__main__":
    unittest.main()
