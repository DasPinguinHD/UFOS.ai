"""Stage-1 review fixes (2026-10-02) in strategy_platform: daily-bar
freshness on/after early-close days, head fetch on a changed adjustment
basis, windowed provider requests + chunked writes + RequestTooLarge,
Credential Manager blob decoding, /v2 key-name type check, caller errors in
registry.fetch and licence keeping in data_source_set. Offline, temp DBs.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_review_fixes -v
"""
import asyncio
import unittest
from datetime import datetime, timezone

try:
    from test_sp_data_support import FakeBarProvider, TempDb, utc
except ImportError:  # pragma: no cover - run as package
    from tests.test_sp_data_support import FakeBarProvider, TempDb, utc

from strategy_platform import secrets
from strategy_platform.api import routes
from strategy_platform.core import calendars
from strategy_platform.core.errors import (DataSourceUnavailable, InvalidRequest, RequestTooLarge,
                                           SecretStoreUnavailable)
from strategy_platform.data import cache as cache_mod
from strategy_platform.data import pit, registry
from strategy_platform.data.cache import BarCache, is_fresh


def eq(day: str, close: float, symbol: str = "SPY") -> dict:
    """Daily IEX bar dict with ts = New York midnight of ``day`` (EDT or EST)."""
    d = datetime.fromisoformat(day).date()
    ts = datetime(d.year, d.month, d.day, tzinfo=calendars.NY).astimezone(timezone.utc)
    return {"symbol": symbol, "timeframe": "1Day", "ts": ts.isoformat(timespec="seconds"),
            "open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 1000.0,
            "vwap": close, "trade_count": 10, "source": "alpaca",
            "known_at": pit.known_at_iso("equity", "1Day", ts), "fetched_at": "2026-10-02T21:00:00+00:00"}


class _Case(unittest.TestCase):
    def setUp(self):
        self._db = TempDb()
        self.db = self._db.__enter__()
        self.prov = FakeBarProvider()
        self.now = utc(2026, 10, 2, 21, 0)
        self.cache = BarCache(fetch=self.prov.fetch, configured_source=lambda dt: "alpaca", clock=lambda: self.now)

    def tearDown(self):
        self._db.__exit__(None, None, None)

    def get(self, start, symbol="SPY"):
        return self.cache.get_bars(symbol, "1Day", start, require_fresh_through=calendars.last_close_before(self.now))


class EarlyCloseFreshnessTest(_Case):
    """Review item 1: on/after an early-close day (close 13:00 NY) the bar of
    that session itself is required — never the previous session's bar."""

    CASES = [  # (days cached before, early-close day, "now" after its close)
        (["2025-07-01", "2025-07-02"], "2025-07-03", utc(2025, 7, 3, 18, 0)),
        (["2025-12-22", "2025-12-23"], "2025-12-24", utc(2025, 12, 24, 19, 0)),
        (["2026-12-22", "2026-12-23"], "2026-12-24", utc(2026, 12, 24, 19, 0)),
    ]

    def test_previous_session_bar_is_not_fresh(self):
        for before, early, after_close in self.CASES:
            with self.subTest(early=early):
                self.prov.bars["SPY"] = [eq(d, 100.0 + i) for i, d in enumerate(before)]
                start = datetime.fromisoformat(before[0]).replace(tzinfo=timezone.utc)
                self.now = utc(*map(int, before[-1].split("-")), 22, 0)      # cached after that close
                self.get(start)
                self.now = after_close
                with self.assertRaises(DataSourceUnavailable) as cm:   # stale bars never returned as current
                    self.get(start)
                self.assertIn("no 1Day bar for SPY", cm.exception.user_message)
                self.prov.bars["SPY"].append(eq(early, 200.0))
                rows = self.get(start)
                self.assertEqual((rows[-1]["close"], rows[-1]["ts"][:10] <= early), (200.0, True))
                self.assertEqual(rows[-1]["known_at"], calendars.session_bounds(
                    datetime.fromisoformat(early).date())[1].isoformat(timespec="seconds"))

    def test_is_fresh_rule(self):
        rows = [eq("2026-12-23", 1.0)]
        self.assertFalse(is_fresh(rows, utc(2026, 12, 24, 18, 0), "1Day"))      # early close: own bar needed
        self.assertTrue(is_fresh(rows, utc(2026, 12, 23, 21, 0), "1Day"))
        self.assertTrue(is_fresh(rows, utc(2026, 12, 24, 15, 0), "1Day"))      # during Dec 24: Dec 23 is the last
        self.assertTrue(is_fresh([eq("2026-12-24", 1.0)], utc(2026, 12, 24, 18, 0), "1Day"))


class HeadAdjustmentTest(_Case):
    def test_head_fetch_with_changed_basis_refetches_everything(self):
        days = ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-28",
                "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"]
        self.prov.bars["SPY"] = [eq(d, 600.0 + i) for i, d in enumerate(days)]
        self.get(utc(2026, 9, 28))                                    # cached from Sep 28
        # a dividend: every adjusted price changes; the tail is still fresh (no tail fetch)
        self.prov.bars["SPY"] = [eq(d, (600.0 + i) * 0.98) for i, d in enumerate(days)]
        rows = self.get(utc(2026, 9, 21))
        self.assertEqual(len(self.prov.calls), 3)                     # first, head, full refetch
        self.assertAlmostEqual(rows[0]["close"], 600.0 * 0.98)
        self.assertAlmostEqual(rows[-1]["close"], 609.0 * 0.98)       # cached rows replaced, one basis
        stored = self.db.query_bars("SPY", "1Day", None, None, "alpaca")
        self.assertAlmostEqual(stored[-1]["close"], 609.0 * 0.98)


class WindowAndChunkTest(_Case):
    def setUp(self):
        super().setUp()
        self._window = dict(cache_mod.FETCH_WINDOW)
        self._chunk = cache_mod.SAVE_CHUNK_ROWS

    def tearDown(self):
        cache_mod.FETCH_WINDOW.clear()
        cache_mod.FETCH_WINDOW.update(self._window)
        cache_mod.SAVE_CHUNK_ROWS = self._chunk
        super().tearDown()

    def test_long_range_is_fetched_in_windows_and_saved_in_chunks(self):
        from datetime import timedelta
        days = ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-28",
                "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"]
        self.prov.bars["SPY"] = [eq(d, 600.0 + i) for i, d in enumerate(days)]
        cache_mod.FETCH_WINDOW["1Day"] = timedelta(days=4)
        cache_mod.SAVE_CHUNK_ROWS = 3
        saved = []
        real = self.db.upsert_bars

        def spy(rows):
            saved.append(len(rows))
            return real(rows)
        self.db.upsert_bars = spy
        try:
            rows = self.get(utc(2026, 9, 21))
        finally:
            self.db.upsert_bars = real
        self.assertEqual([r["ts"][:10] for r in rows], days)          # nothing lost or doubled at window edges
        self.assertGreaterEqual(len(self.prov.calls), 3)
        for c in self.prov.calls:
            self.assertLessEqual(c[5] - c[4], timedelta(days=4))       # every call within one window
        self.assertTrue(saved and max(saved) <= 3 and sum(saved) == len(days))

    def test_oversize_request_is_user_facing(self):
        self.now = utc(2026, 10, 2, 21, 0)
        with self.assertRaises(RequestTooLarge) as cm:
            self.cache.get_bars("BTC-USD", "1Min", utc(2026, 8, 1))
        self.assertIn("too large for 1Min bars", cm.exception.user_message)
        self.assertIsInstance(cm.exception, InvalidRequest)
        self.assertEqual(self.prov.calls, [])


class SecretsBlobTest(unittest.TestCase):
    def test_decode(self):
        t = secrets.target("fred-api-key")
        self.assertEqual(secrets.decode_blob(b" abc \n", t), "abc")
        self.assertIsNone(secrets.decode_blob(b"   ", t))
        for raw in (b"\xff\xfe\x00k", b"ab\x00cd"):
            with self.assertRaises(SecretStoreUnavailable) as cm:
                secrets.decode_blob(raw, t)
            self.assertEqual(cm.exception.user_message,
                             "FRED API key is stored in an unreadable format — set it again in Settings → Keys.")


class RoutesNameTypeTest(unittest.TestCase):
    def test_non_string_name_is_400(self):
        for name in ([1], {"a": 1}, 5, None):
            status, payload = asyncio.run(routes.V2Api().keys_changed({}, {"name": name, "action": "set"}, {}))
            self.assertEqual(status, 400, name)
            self.assertIn("name", payload["errors"])
        status, _p = asyncio.run(routes.V2Api().keys_changed({}, {"name": "fred-api-key", "action": ["set"]}, {}))
        self.assertEqual(status, 400)

    def test_invalid_request_maps_to_400(self):
        self.assertEqual(routes.user_error(RequestTooLarge("too large"))[0], 400)
        self.assertEqual(routes.user_error(DataSourceUnavailable("alpaca", "news", "x"))[0], 503)


class RegistryCallerErrorTest(unittest.TestCase):
    def setUp(self):
        self._db = TempDb()
        self.db = self._db.__enter__()

    def tearDown(self):
        self._db.__exit__(None, None, None)

    def test_bad_arguments_do_not_mark_the_source(self):
        class Prov:
            data_types = ("equity_bars",)

            def get_bars(self, symbol, timeframe, start, end):
                raise ValueError(f"unknown timeframe {timeframe!r}")
        reg = registry.Registry(providers={"alpaca": Prov}, clock=lambda: utc(2026, 10, 2))
        with self.assertRaises(InvalidRequest) as cm:
            reg.fetch("equity_bars", "get_bars", "SPY", "1Week", utc(2026, 9, 1), utc(2026, 10, 1))
        self.assertIn("unknown timeframe", cm.exception.user_message)
        self.assertIsNone(self.db.data_source_get("equity_bars")["status"])

    def test_same_source_without_licence_keeps_it(self):
        row = self.db.data_source_set("news", "alpaca", licence="personal_use", licence_note="note")
        self.assertEqual(row["licence"], "personal_use")
        row = self.db.data_source_set("news", "alpaca")
        self.assertEqual((row["licence"], row["licence_note"]), ("personal_use", "note"))


if __name__ == "__main__":
    unittest.main()
