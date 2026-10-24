"""Market Research MR2a — header + key statistics (market_research/SPEC.md §7.4, plan §5.4): prev close,
change, day range (RTH), SIP-delayed volume, open, VWAP, 30-day average volume, performance windows incl.
YTD and 1Y, 52-week range, crypto UTC day, null-with-reason, feed label on every number, unavailable
states (no keys, symbol not covered, no fallback).

Run from HedgeFund/backend:  python -m unittest tests.test_mr_data_stats -v
"""
import unittest
from datetime import date, timedelta

try:
    from mr_data_testkit import FakeSources, FailingSources, TempStore, bar_row, utc
except ImportError:  # pragma: no cover - run as package
    from tests.mr_data_testkit import FakeSources, FailingSources, TempStore, bar_row, utc

from market_research.assets import CRYPTO_NOT_COVERED, SymbolNotCovered
from market_research.bars import aggregate as agg
from market_research.data import DataService
from market_research.stats.keystats import months_back
from strategy_platform.core import calendars
from strategy_platform.core.clock import iso
from strategy_platform.core.errors import SecretMissing

NOW = utc(2026, 10, 15, 15, 0)          # Thursday 11:00 EDT, sip_through 10:45


def svc(now=NOW, cls=FakeSources, **kw):
    fs = cls(lambda: now, **kw)
    return DataService(sources=fs, now=lambda: now), fs


def nums(obj, path=""):
    if isinstance(obj, dict):
        if "feed_label" in obj:
            yield path, obj
        for k, v in obj.items():
            yield from nums(v, f"{path}.{k}")


def rth_rows(day, frm=None, to=None):
    w = agg.session_window(day, "rth")
    t, out = frm or w[0], []
    while t < (to or w[1]):
        out.append(bar_row(t))
        t += timedelta(minutes=1)
    return out


class EquitySnapshotTest(unittest.TestCase):
    def test_header_values_and_feeds(self):
        with TempStore():
            ds, fs = svc()
            fs.trade["AAPL"] = 120.0
            fs.daily_overrides[("AAPL", date(2026, 10, 14))] = {"close": 100.0}
            s = ds.snapshot("AAPL")
            self.assertEqual((s["name"], s["exchange"], s["kind"]), ("Apple Inc. Common Stock", "NASDAQ", "stock"))
            self.assertEqual((s["last"]["value"], s["last"]["feed"]), (120.0, "iex"))
            self.assertEqual((s["prev_close"]["value"], s["prev_close"]["feed"]), (100.0, "sip_daily"))
            self.assertEqual(s["prev_close"]["as_of"], "2026-10-14T20:00:00+00:00")
            self.assertAlmostEqual(s["change"]["value"], 20.0)
            self.assertAlmostEqual(s["change_pct"]["value"], 20.0)
            self.assertEqual(s["change"]["feed"], "computed")
            day = date(2026, 10, 15)
            sip = rth_rows(day, to=utc(2026, 10, 15, 14, 45))
            tail = rth_rows(day, frm=utc(2026, 10, 15, 14, 45), to=NOW)
            both = sip + tail
            self.assertEqual(s["day_low"]["value"], min(r["low"] for r in both))
            self.assertEqual(s["day_high"]["value"], max(r["high"] for r in both))
            self.assertEqual(s["day_low"]["feed_label"], "SIP + IEX live tail (regular session)")
            self.assertEqual(s["volume"]["value"], 10.0 * len(sip))          # SIP only, tail excluded
            self.assertEqual((s["volume"]["feed"], s["volume"]["feed_label"]), ("sip_delayed", "SIP · 15 min delayed"))
            self.assertEqual(s["volume"]["as_of"], "2026-10-15T14:45:00+00:00")
            self.assertEqual(s["market"]["state"], "open")
            for path, n in nums(s):
                self.assertTrue(n["feed"] and n["feed_label"], path)
                self.assertEqual(set(n), {"value", "feed", "feed_label", "as_of", "reason"}, path)

    def test_pre_market_nulls_with_reason(self):
        with TempStore():
            ds, _fs = svc(utc(2026, 10, 15, 12, 0))                       # 08:00 ET
            s = ds.snapshot("AAPL")
            for k in ("day_low", "day_high", "volume"):
                self.assertIsNone(s[k]["value"])
                self.assertIn("not opened yet", s[k]["reason"])
            self.assertEqual(s["market"]["state"], "pre_market")
            self.assertIsNotNone(s["prev_close"]["value"])

    def test_first_15_minutes_volume_reason_and_open_from_tail(self):
        with TempStore():
            ds, _fs = svc(utc(2026, 10, 15, 13, 40))                      # 09:40 ET: SIP only through 09:25
            s = ds.snapshot("AAPL")
            self.assertIsNone(s["volume"]["value"])
            self.assertIn("09:45", s["volume"]["reason"])
            st = ds.stats("AAPL")
            self.assertEqual(st["open"]["feed"], "iex_tail")
            self.assertEqual(st["open"]["value"], bar_row(utc(2026, 10, 15, 13, 30))["open"])

    def test_weekend_values_at_close(self):
        with TempStore():
            ds, _fs = svc(utc(2026, 10, 17, 16, 0))                       # Saturday
            s = ds.snapshot("AAPL")
            self.assertEqual(s["market"]["state"], "closed")
            self.assertEqual(s["volume"]["as_of"], "2026-10-16T20:00:00+00:00")    # Friday's close
            self.assertEqual(s["volume"]["value"], 390 * 10.0)
            self.assertEqual(s["prev_close"]["as_of"], "2026-10-15T20:00:00+00:00")


class EquityStatsTest(unittest.TestCase):
    def test_definitions(self):
        with TempStore():
            ds, fs = svc()
            fs.trade["AAPL"] = 110.0
            ov = fs.daily_overrides
            ov[("AAPL", date(2026, 10, 8))] = {"close": 100.0}            # ref − 7 d = 10-08 (Thu)
            ov[("AAPL", date(2026, 9, 15))] = {"close": 50.0}             # 1M
            ov[("AAPL", date(2025, 12, 31))] = {"close": 55.0}            # YTD: last close of 2025
            ov[("AAPL", date(2025, 10, 15))] = {"close": 220.0}           # 1Y
            ov[("AAPL", date(2026, 3, 2))] = {"low": 1.0}                 # 52W low inside
            ov[("AAPL", date(2025, 10, 10))] = {"high": 999.0}            # outside the 52 weeks
            ov[("AAPL", date(2026, 5, 4))] = {"high": 500.0}
            st = ds.stats("AAPL")
            p = st["performance"]
            self.assertEqual(p["1W"]["base_date"], "2026-10-08")
            self.assertAlmostEqual(p["1W"]["pct"]["value"], 10.0)
            self.assertEqual(p["1M"]["base_date"], "2026-09-15")
            self.assertAlmostEqual(p["1M"]["pct"]["value"], 120.0)
            self.assertEqual(p["YTD"]["base_date"], "2025-12-31")
            self.assertAlmostEqual(p["YTD"]["pct"]["value"], (110 / 55 - 1) * 100)
            self.assertEqual(p["1Y"]["base_date"], "2025-10-15")
            self.assertAlmostEqual(p["1Y"]["pct"]["value"], -50.0)
            self.assertEqual(p["3M"]["base_date"], "2026-07-15")
            self.assertEqual(p["6M"]["base_date"], "2026-04-15")
            self.assertEqual(p["1W"]["pct"]["feed"], "computed")
            self.assertEqual(st["notes"], ["Performance excludes dividends."])
            r = st["range_52w"]
            self.assertEqual((r["low"]["value"], r["high"]["value"]), (1.0, 500.0))
            self.assertEqual((r["from"], r["to"]), ("2025-10-17", "2026-10-14"))   # (ref − 52 weeks, ref]
            self.assertEqual(r["low"]["feed"], "sip_daily")
            # avg volume: last 30 completed sessions (fake volume = 1000 + days since 2016-01-01)
            sessions = calendars.trading_days("2026-08-01", "2026-10-14")[-30:]
            exp = sum(1000.0 + (d - date(2016, 1, 1)).days for d in sessions) / 30
            self.assertAlmostEqual(st["avg_volume_30d"]["value"], exp)
            self.assertEqual(st["avg_volume_30d"]["feed"], "sip_daily")
            # open = first RTH minute (SIP), VWAP over RTH minutes SIP + tail
            self.assertEqual((st["open"]["value"], st["open"]["feed"]),
                             (bar_row(utc(2026, 10, 15, 13, 30))["open"], "sip_delayed"))
            rows = rth_rows(date(2026, 10, 15), to=NOW)
            vols = [10.0 if r["ts"] < "2026-10-15T14:45" else 1.0 for r in rows]
            exp_vwap = sum(r["vwap"] * v for r, v in zip(rows, vols)) / sum(vols)
            self.assertAlmostEqual(st["vwap"]["value"], exp_vwap)
            self.assertEqual(st["exchange"], "NASDAQ")
            daily_calls = [c for c in fs.calls if isinstance(c, tuple) and c[0] == "daily"]
            self.assertEqual(daily_calls[-1][1], "split")                  # split-adjusted, dividends excluded
            for path, n in nums(st):
                self.assertTrue(n["feed"] and n["feed_label"], path)

    def test_months_back_clamps(self):
        self.assertEqual(months_back(date(2026, 3, 31), 1), date(2026, 2, 28))
        self.assertEqual(months_back(date(2026, 1, 15), 3), date(2025, 10, 15))
        self.assertEqual(months_back(date(2024, 2, 29), 12), date(2023, 2, 28))

    def test_short_history_null_with_reason(self):
        with TempStore():
            ds, fs = svc()
            orig = fs.daily
            fs.daily = lambda s, a, b, **kw: orig(s, a, b, **kw)[-10:]     # e.g. a recent IPO
            st = ds.stats("AAPL")
            self.assertIsNone(st["avg_volume_30d"]["value"])
            self.assertIn("Only 10", st["avg_volume_30d"]["reason"])
            self.assertIsNone(st["performance"]["1Y"]["pct"]["value"])
            self.assertIn("No daily close", st["performance"]["1Y"]["pct"]["reason"])
            self.assertIsNone(st["performance"]["1Y"]["base_date"])


class CryptoTest(unittest.TestCase):
    def test_crypto_utc_day(self):
        with TempStore():
            ds, fs = svc()
            fs.trade["BTC-USD"] = 200.0
            s = ds.snapshot("BTC-USD")
            self.assertEqual(s["asset_class"], "crypto")
            self.assertEqual(s["last"]["feed"], "crypto")
            prev_close = bar_row(utc(2026, 10, 14, 23))["close"]           # last hour of the previous UTC day
            self.assertAlmostEqual(s["prev_close"]["value"], prev_close)
            self.assertEqual(s["volume"]["value"], 2.0 * 15 * 60)            # 00:00–15:00 UTC minutes
            self.assertEqual(s["volume"]["feed"], "crypto")
            self.assertEqual(s["market"], {"state": "open", "day_start": "2026-10-15T00:00:00+00:00",
                                           "day_end": "2026-10-16T00:00:00+00:00", "tz": "UTC"})
            st = ds.stats("BTC-USD")
            self.assertEqual(st["open"]["as_of"], "2026-10-15T00:00:00+00:00")
            self.assertEqual(st["avg_volume_30d"]["value"], 24 * 120.0)
            self.assertEqual(st["performance"]["1W"]["base_date"], "2026-10-08")


class UnavailableTest(unittest.TestCase):
    def test_no_keys_message(self):
        with TempStore():
            ex = SecretMissing("alpaca-paper-key-id", "Alpaca key ID")
            ds, _fs = svc(cls=FailingSources, ex=ex)
            for call in (lambda: ds.snapshot("AAPL"), lambda: ds.bars("AAPL", "1D", None, None, None),
                         lambda: ds.search_assets("AA", 5)):
                with self.assertRaises(SecretMissing) as cm:
                    call()
                self.assertIn("enter it in Hedge Fund", cm.exception.user_message)

    def test_symbol_not_covered(self):
        with TempStore():
            ds, _fs = svc()
            with self.assertRaises(SymbolNotCovered) as cm:
                ds.snapshot("DOGE-USD")
            self.assertEqual(cm.exception.user_message, CRYPTO_NOT_COVERED)
            with self.assertRaises(SymbolNotCovered) as cm:
                ds.bars("ZZZZ", "1D", None, None, None)
            self.assertIn("ZZZZ", cm.exception.user_message)
            with self.assertRaises(SymbolNotCovered):
                ds.stats("OTCX")                                           # OTC assets are not listed

    def test_trade_failure_null_not_stand_in(self):
        from strategy_platform.core.errors import DataSourceUnavailable
        with TempStore():
            ds, fs = svc()
            fs.fail["latest_trade"] = DataSourceUnavailable("alpaca", "equity_quotes", "timeout")
            s = ds.snapshot("AAPL")
            self.assertIsNone(s["last"]["value"])
            self.assertIn("timeout", s["last"]["reason"])
            self.assertIsNone(s["change"]["value"])
            self.assertIsNotNone(s["change"]["reason"])


if __name__ == "__main__":
    unittest.main()
