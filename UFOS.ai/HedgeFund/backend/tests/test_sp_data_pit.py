"""strategy_platform/data/pit.py: known_at rules (equity daily = session
close incl. early closes and DST, crypto/intraday = bar end) and
bars_as_of (no bar that was not complete at as_of). Offline.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_data_pit -v
"""
import unittest

try:
    from test_sp_data_support import TempDb, crypto_row, equity_row, utc
except ImportError:  # pragma: no cover - run as package
    from tests.test_sp_data_support import TempDb, crypto_row, equity_row, utc

from strategy_platform.core.calendars import CalendarUnavailable
from strategy_platform.data import pit


class KnownAtTest(unittest.TestCase):
    def test_equity_daily_is_the_session_close(self):
        self.assertEqual(pit.known_at("equity", "1Day", "2026-10-01T04:00:00Z"), utc(2026, 10, 1, 20, 0))  # EDT
        self.assertEqual(pit.known_at("equity", "1Day", "2026-12-01T05:00:00Z"), utc(2026, 12, 1, 21, 0))  # EST
        self.assertEqual(pit.known_at("equity", "1Day", "2026-11-27T05:00:00Z"), utc(2026, 11, 27, 18, 0))  # 13:00
        self.assertEqual(pit.known_at("equity", "1Day", "2026-10-03T04:00:00Z"), utc(2026, 10, 3, 20, 0))  # no session

    def test_outside_calendar_raises(self):
        with self.assertRaises(CalendarUnavailable):
            pit.known_at("equity", "1Day", "1999-06-01T04:00:00Z")

    def test_crypto_and_intraday_bar_end(self):
        self.assertEqual(pit.known_at("crypto", "1Day", "2026-10-01T00:00:00Z"), utc(2026, 10, 2, 0, 0))
        self.assertEqual(pit.known_at("crypto", "1Day", "2026-10-01T05:00:00Z"), utc(2026, 10, 2, 5, 0))
        self.assertEqual(pit.known_at("equity", "1Hour", "2026-10-01T14:00:00Z"), utc(2026, 10, 1, 15, 0))
        self.assertEqual(pit.known_at("equity", "1Min", "2026-10-01T14:00:00Z"), utc(2026, 10, 1, 14, 1))
        with self.assertRaises(ValueError):
            pit.known_at("equity", "1Week", "2026-10-01T14:00:00Z")

    def test_filter_known(self):
        rows = [equity_row("2026-09-30", 1.0), equity_row("2026-10-01", 2.0)]
        self.assertEqual(len(pit.filter_known(rows, utc(2026, 10, 1, 19, 59))), 1)
        self.assertEqual(len(pit.filter_known(rows, utc(2026, 10, 1, 20, 0))), 2)


class BarsAsOfTest(unittest.TestCase):
    def test_bars_as_of(self):
        with TempDb() as db:
            db.upsert_bars([equity_row("2026-09-30", 1.0), equity_row("2026-10-01", 2.0),
                            crypto_row("2026-10-01", 3.0)])
            got = pit.bars_as_of("SPY", "1Day", utc(2026, 10, 1, 19, 0), source="alpaca", store=db)
            self.assertEqual([r["close"] for r in got], [1.0])           # Oct 1 not complete before 20:00 UTC
            got = pit.bars_as_of("SPY", "1Day", "2026-10-01T20:00:00Z", source="alpaca", store=db)
            self.assertEqual([r["close"] for r in got], [1.0, 2.0])
            self.assertEqual(pit.bars_as_of("BTC-USD", "1Day", utc(2026, 10, 1, 23), source="alpaca", store=db), [])
            self.assertEqual(len(pit.bars_as_of("btc-usd", "1Day", utc(2026, 10, 2), source="alpaca", store=db)), 1)


if __name__ == "__main__":
    unittest.main()
