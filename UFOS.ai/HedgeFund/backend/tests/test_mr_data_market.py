"""Market Research MR2a — market status (SPEC §7.3: open/pre/after/closed/holiday, early close, next
open/close in UTC + New York, DST, calendar coverage) and the asset list (SPEC §4.2: refresh, search
order, sanitising, 503 when empty and the refresh fails, daily refresh task).

Run from HedgeFund/backend:  python -m unittest tests.test_mr_data_market -v
"""
import asyncio
import logging
import unittest
from datetime import timedelta

try:
    from mr_data_testkit import FakeSources, TempStore, utc
except ImportError:  # pragma: no cover - run as package
    from tests.mr_data_testkit import FakeSources, TempStore, utc

from market_research import assets, limits, market, store
from strategy_platform.core.calendars import CalendarUnavailable
from strategy_platform.core.errors import DataSourceUnavailable, SecretMissing


def setUpModule():
    logging.getLogger("market_research").setLevel(logging.CRITICAL + 1)


def tearDownModule():
    logging.getLogger("market_research").setLevel(logging.NOTSET)


class MarketStatusTest(unittest.TestCase):
    def st(self, *a):
        return market.equities(utc(*a))

    def test_states_through_a_day(self):
        cases = [((2026, 10, 15, 7, 0), "closed", "2026-10-15"),      # 03:00 ET
                 ((2026, 10, 15, 12, 0), "pre_market", "2026-10-15"),
                 ((2026, 10, 15, 14, 0), "open", "2026-10-15"),
                 ((2026, 10, 15, 21, 0), "after_hours", "2026-10-15"),
                 ((2026, 10, 16, 1, 0), "closed", "2026-10-16"),      # 21:00 ET -> next session
                 ((2026, 10, 17, 15, 0), "closed", "2026-10-19"),     # Saturday
                 ((2026, 11, 26, 15, 0), "holiday", "2026-11-27")]    # Thanksgiving
        for t, state, sd in cases:
            with self.subTest(t=t):
                s = self.st(*t)
                self.assertEqual((s["state"], s["session_date"]), (state, sd))

    def test_next_open_close_and_ny_block(self):
        s = self.st(2026, 10, 15, 14, 0)
        self.assertEqual((s["opens_at"], s["closes_at"]), ("2026-10-15T13:30:00+00:00", "2026-10-15T20:00:00+00:00"))
        self.assertEqual((s["next_open_at"], s["next_close_at"]),
                         ("2026-10-16T13:30:00+00:00", "2026-10-15T20:00:00+00:00"))
        self.assertEqual(s["ny"]["next_open_at"], "2026-10-16T09:30:00-04:00")
        self.assertEqual(s["tz"], "America/New_York")
        fri = self.st(2026, 10, 16, 21, 0)                               # Friday after hours
        self.assertEqual((fri["next_open_at"], fri["next_close_at"]),
                         ("2026-10-19T13:30:00+00:00", "2026-10-19T20:00:00+00:00"))
        pre = self.st(2026, 10, 15, 12, 0)
        self.assertEqual(pre["next_open_at"], "2026-10-15T13:30:00+00:00")

    def test_early_close(self):
        s = self.st(2026, 11, 27, 18, 30)                                # 13:30 EST on an early-close day
        self.assertEqual((s["state"], s["early_close"]), ("after_hours", True))
        self.assertEqual(s["closes_at"], "2026-11-27T18:00:00+00:00")
        self.assertEqual(s["extended_closes_at"], "2026-11-27T22:00:00+00:00")   # 17:00 EST
        self.assertEqual(self.st(2026, 11, 27, 22, 30)["state"], "closed")
        self.assertEqual(self.st(2026, 11, 27, 17, 59)["state"], "open")

    def test_dst_offsets(self):
        self.assertEqual(self.st(2026, 3, 6, 15, 0)["opens_at"], "2026-03-06T14:30:00+00:00")
        self.assertEqual(self.st(2026, 3, 9, 15, 0)["opens_at"], "2026-03-09T13:30:00+00:00")   # US DST, EU not yet
        self.assertEqual(self.st(2026, 10, 27, 15, 0)["opens_at"], "2026-10-27T13:30:00+00:00")  # EU back, US not
        self.assertEqual(self.st(2026, 11, 2, 15, 0)["opens_at"], "2026-11-02T14:30:00+00:00")

    def test_crypto_and_full_payload(self):
        p = market.status(utc(2026, 10, 15, 23, 59))
        self.assertEqual(p["crypto"], {"state": "open", "day_start": "2026-10-15T00:00:00+00:00",
                                       "day_end": "2026-10-16T00:00:00+00:00", "tz": "UTC"})
        self.assertEqual(set(p), {"equities", "crypto", "generated_at"})
        self.assertTrue({"state", "session_date", "opens_at", "closes_at", "early_close", "next_open_at",
                         "next_close_at"} <= set(p["equities"]))

    def test_calendar_not_covered(self):
        with self.assertRaises(CalendarUnavailable):
            market.status(utc(2031, 6, 2, 15))           # the calendar covers 2000–2030 (S10-H)


class AssetsTest(unittest.TestCase):
    NOW = utc(2026, 10, 15, 15)

    def test_refresh_normalises_and_sanitises(self):
        with TempStore():
            fs = FakeSources(lambda: self.NOW)
            n = assets.refresh(fs, self.NOW)
            self.assertEqual(n, 7)                                         # OTC + BTC/USDT skipped
            btc = store.asset_get("BTC-USD")
            self.assertEqual((btc["name"], btc["kind"], btc["asset_class"]), ("Bitcoin / US Dollar", "crypto", "crypto"))
            self.assertIsNone(store.asset_get("OTCX"))
            self.assertIsNone(store.asset_get("BTC-USDT"))
            self.assertEqual(store.asset_get("SPY")["kind"], "etf")
            self.assertEqual(store.asset_get("AAPW")["kind"], "etf")        # name says ETF
            self.assertEqual(store.asset_get("AAPL")["kind"], "stock")
            self.assertEqual(assets.refreshed_at(), "2026-10-15T15:00:00+00:00")

    def test_search_order(self):
        with TempStore():
            fs = FakeSources(lambda: self.NOW)
            r = assets.search("aapl", 10, fs, now=self.NOW)
            # exact symbol, then name word prefix ("Roundhill AAPL WeeklyPay ETF"); PAPL does not contain "aapl"
            self.assertEqual([i["symbol"] for i in r["items"]], ["AAPL", "AAPW"])
            r = assets.search("AA", 10, fs, now=self.NOW)
            self.assertEqual([i["symbol"] for i in r["items"]][:3], ["AA", "AAPL", "AAPW"])
            r = assets.search("apple", 10, fs, now=self.NOW)
            self.assertEqual([i["symbol"] for i in r["items"]], ["AAPL", "PAPL"])   # word prefix, then substring
            r = assets.search("btc/usd", 10, fs, now=self.NOW)
            self.assertEqual(r["items"][0]["symbol"], "BTC-USD")
            self.assertEqual(set(r), {"items", "refreshed_at", "source"})
            self.assertEqual(set(r["items"][0]), {"symbol", "name", "asset_class", "kind", "exchange", "tradable"})
            self.assertEqual(len(assets.search("a", 2, fs, now=self.NOW)["items"]), 2)

    def test_empty_table_refresh_failure_and_kept_list(self):
        with TempStore():
            fs = FakeSources(lambda: self.NOW)
            fs.fail["assets"] = SecretMissing("alpaca-paper-key-id", "Alpaca key ID")
            with self.assertRaises(SecretMissing):
                assets.search("AA", 5, fs, now=self.NOW)
            fs.fail.clear()
            assets.refresh(fs, self.NOW)
            fs.assets_rows = []
            with self.assertRaises(DataSourceUnavailable):
                assets.refresh(fs, self.NOW)                               # empty answer: old list kept
            self.assertEqual(store.assets_count(), 7)
            fs.fail["assets"] = DataSourceUnavailable("alpaca", "assets", "down")
            self.assertEqual(len(assets.search("AA", 5, fs, now=self.NOW)["items"]), 3)   # list kept, no refresh

    def test_empty_table_retry_spacing(self):
        with TempStore():
            fs = FakeSources(lambda: self.NOW)
            fs.fail["assets"] = SecretMissing("alpaca-paper-key-id", "Alpaca key ID")
            for _ in range(5):
                with self.assertRaises(SecretMissing) as cm:
                    assets.search("AA", 5, fs, now=self.NOW)
                self.assertIn("enter it in Hedge Fund", cm.exception.user_message)    # the reason is shown
            with self.assertRaises(SecretMissing):
                assets.require("AAPL", fs, now=self.NOW)
            self.assertEqual(fs.calls.count("assets"), 1)                # one refresh, not one per search
            fs.fail.clear()
            assets._failure["at"] -= limits.ASSET_EMPTY_RETRY_SECONDS + 1   # 61 s later
            self.assertEqual(assets.search("AAPL", 5, fs, now=self.NOW)["items"][0]["symbol"], "AAPL")
            self.assertEqual(fs.calls.count("assets"), 2)

    def test_refresh_task_daily_and_retry(self):
        with TempStore():
            fs = FakeSources(lambda: self.NOW)
            fs.fail["assets"] = DataSourceUnavailable("alpaca", "assets", "down")
            sleeps = []

            async def sleep(s):
                sleeps.append(s)
                if len(sleeps) == 1:
                    fs.fail.clear()
                elif len(sleeps) >= 2:
                    raise asyncio.CancelledError()
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(assets.run(lambda: fs, sleep=sleep, now=lambda: self.NOW))
            self.assertEqual(sleeps[0], limits.ASSET_REFRESH_RETRY_SECONDS)
            self.assertEqual(sleeps[1], limits.ASSET_REFRESH_HOURS * 3600.0)
            self.assertEqual(store.assets_count(), 7)
            self.assertEqual(assets.due(self.NOW + timedelta(hours=25)), 0.0)


if __name__ == "__main__":
    unittest.main()
