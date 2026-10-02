"""fund/market.py: NYSE calendar (holidays, weekends, DST switch weeks, early
closes, trading-day counting), news parsing for old/new yfinance shapes, and
YFinanceFeed against a fake yfinance module (lazy import, bar cache in the
prices table, quote fallback, never raising). Offline, stdlib unittest.

Run from HedgeFund/backend:  python -m unittest tests.test_fund_market -v
"""
import os
import sys
import tempfile
import types
import unittest
from datetime import date, datetime, timedelta, timezone

from fund import market as M
from fund import store
from fund.types import Bar, Quote
from newsroom import db


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


class CalendarTest(unittest.TestCase):
    def test_holiday_lists(self):
        for year in (2026, 2027):
            days = [d for d in M.NYSE_HOLIDAYS if d.year == year]
            self.assertEqual(len(days), 10, year)
            self.assertTrue(all(d.weekday() < 5 for d in days))
        for d in ("2026-04-03", "2026-07-03", "2026-11-26", "2027-03-26", "2027-06-18", "2027-07-05",
                  "2027-12-24"):
            self.assertFalse(M.is_trading_day(d), d)
        self.assertTrue(M.is_trading_day("2027-12-31"))       # Jan 1 2028 is a Saturday: no closure
        self.assertTrue(M.is_trading_day(date(2026, 9, 28)))
        self.assertFalse(M.is_trading_day("2026-09-26"))       # Saturday
        self.assertFalse(M.is_trading_day("2026-09-27"))       # Sunday

    def test_regular_session_and_weekend(self):
        s = M.session_state(utc(2026, 9, 28, 14, 0))          # Mon 10:00 EDT
        self.assertEqual((s["open"], s["session"], s["trading_day"]), (True, "regular", "2026-09-28"))
        self.assertEqual(s["next_close"], "2026-09-28T20:00:00+00:00")
        self.assertEqual(s["next_open"], "2026-09-29T13:30:00+00:00")
        s = M.session_state(utc(2026, 9, 26, 15, 0))          # Saturday
        self.assertEqual((s["open"], s["next_close"], s["trading_day"]), (False, None, "2026-09-28"))
        self.assertEqual(s["next_open"], "2026-09-28T13:30:00+00:00")
        s = M.session_state(utc(2026, 9, 28, 12, 0))          # before the open: today
        self.assertEqual((s["open"], s["trading_day"]), (False, "2026-09-28"))
        s = M.session_state(utc(2026, 9, 28, 20, 0))          # exactly at the close: closed
        self.assertEqual((s["open"], s["trading_day"]), (False, "2026-09-29"))
        self.assertTrue(M.is_open(utc(2026, 9, 28, 13, 30)))  # exactly at the open
        self.assertEqual(M.trading_day(utc(2026, 9, 25, 21, 0)), "2026-09-28")   # Fri after close -> Mon

    def test_holiday_session(self):
        s = M.session_state(utc(2026, 7, 3, 15, 0))           # Independence Day observed
        self.assertFalse(s["open"])
        self.assertEqual(s["trading_day"], "2026-07-06")
        s = M.session_state(utc(2026, 9, 4, 21, 0))           # Friday before Labor Day, after close
        self.assertEqual(s["next_open"], "2026-09-08T13:30:00+00:00")

    def test_dst_switch_weeks(self):
        # DST starts Sun 2026-03-08: open 14:30 UTC on Fri Mar 6, 13:30 UTC on Mon Mar 9
        self.assertFalse(M.is_open(utc(2026, 3, 6, 13, 45)))
        self.assertTrue(M.is_open(utc(2026, 3, 6, 14, 45)))
        self.assertTrue(M.is_open(utc(2026, 3, 9, 13, 45)))
        self.assertEqual(M.session_state(utc(2026, 3, 7, 12))["next_open"], "2026-03-09T13:30:00+00:00")
        # DST ends Sun 2026-11-01: close 20:00 UTC on Fri Oct 30, 21:00 UTC on Mon Nov 2
        self.assertFalse(M.is_open(utc(2026, 10, 30, 20, 30)))
        self.assertTrue(M.is_open(utc(2026, 11, 2, 20, 30)))
        self.assertEqual(M.session_bounds("2026-11-02")[0], utc(2026, 11, 2, 14, 30))

    def test_early_closes(self):
        self.assertTrue(M.is_open(utc(2026, 11, 27, 17, 30)))     # 12:30 EST
        s = M.session_state(utc(2026, 11, 27, 18, 30))            # 13:30 EST: closed early
        self.assertFalse(s["open"])
        self.assertEqual(s["next_open"], "2026-11-30T14:30:00+00:00")
        self.assertEqual(M.session_state(utc(2026, 12, 24, 15))["next_close"], "2026-12-24T18:00:00+00:00")
        self.assertEqual(M.session_bounds("2027-11-26")[1], utc(2027, 11, 26, 18, 0))
        self.assertEqual(M.session_bounds("2026-07-02")[1], utc(2026, 7, 2, 20, 0))   # full day
        self.assertIsNone(M.session_bounds("2026-12-25"))

    def test_trading_days_between(self):
        self.assertEqual(M.trading_days_between("2026-09-25", "2026-09-28"), 1)   # Fri -> Mon
        self.assertEqual(M.trading_days_between("2026-09-28", "2026-09-28"), 0)
        self.assertEqual(M.trading_days_between("2026-09-28", "2026-09-25"), 0)
        self.assertEqual(M.trading_days_between("2026-09-04", "2026-09-08"), 1)   # Labor Day
        self.assertEqual(M.trading_days_between("2026-12-21", "2027-01-04"), 8)
        # datetimes are converted to NY dates: Tue 01:00 UTC = Mon evening NY
        self.assertEqual(M.trading_days_between(utc(2026, 9, 28, 14), utc(2026, 9, 29, 1)), 0)
        self.assertEqual(M.trading_days_between("2026-09-28T14:00:00+00:00", "2026-09-29T14:00:00+00:00"), 1)
        self.assertEqual(M.next_trading_day("2026-12-24"), date(2026, 12, 28))
        self.assertEqual(M.previous_trading_day("2026-09-08"), date(2026, 9, 4))

    def test_last_close_before(self):
        self.assertEqual(M.last_close_before(utc(2026, 9, 26, 12)), utc(2026, 9, 25, 20))
        self.assertEqual(M.last_close_before(utc(2026, 9, 28, 19)), utc(2026, 9, 25, 20))
        self.assertEqual(M.last_close_before(utc(2026, 9, 28, 20, 1)), utc(2026, 9, 28, 20))


class NewsParseTest(unittest.TestCase):
    def test_old_shape(self):
        item = {"title": "  Stocks   rally ", "publisher": "Reuters", "link": "https://x.test/a",
                "providerPublishTime": 1790000000}
        out = M.parse_news_item(item)
        self.assertEqual(out["title"], "Stocks rally")
        self.assertEqual(out["publisher"], "Reuters")
        self.assertEqual(out["url"], "https://x.test/a")
        self.assertTrue(out["published_at"].endswith("+00:00"))

    def test_new_shape_and_unsafe_url(self):
        item = {"id": "1", "content": {"title": "Fed holds", "pubDate": "2026-09-25T12:00:00Z",
                                       "provider": {"displayName": "Yahoo"},
                                       "canonicalUrl": {"url": "javascript:alert(1)"},
                                       "clickThroughUrl": {"url": "https://y.test/b"}}}
        out = M.parse_news_item(item)
        self.assertEqual(out, {"title": "Fed holds", "publisher": "Yahoo",
                               "published_at": "2026-09-25T12:00:00+00:00", "url": "https://y.test/b"})
        self.assertIsNone(M.parse_news_item({"content": {"title": ""}}))
        self.assertIsNone(M.parse_news_item("junk"))
        self.assertIsNone(M.parse_news_item({"title": "x", "link": "ftp://z"})["url"])


class _FakeDF:
    def __init__(self, rows):
        self.rows = rows

    def __len__(self):
        return len(self.rows)

    def iterrows(self):
        for d, row in self.rows:
            yield datetime.fromisoformat(d), row


class _FakeTicker:
    fail = False
    div_fail = False
    calls = []

    def __init__(self, symbol):
        self.symbol = symbol

    def history(self, **kw):
        _FakeTicker.calls.append(("history", self.symbol))
        if _FakeTicker.fail:
            raise RuntimeError("rate limited")
        return _FakeDF([("2026-09-24", {"Open": 1, "High": 2, "Low": 0.5, "Close": 1.5, "Volume": 10}),
                        ("2026-09-25", {"Open": 1.5, "High": 2, "Low": 1, "Close": 1.8, "Volume": float("nan")}),
                        ("2026-09-26", {"Open": 1, "High": 1, "Low": 1, "Close": float("nan"), "Volume": 0})])

    @property
    def fast_info(self):
        if _FakeTicker.fail:
            raise RuntimeError("down")
        return {"last_price": 1.95, "previous_close": 1.8}

    @property
    def dividends(self):
        _FakeTicker.calls.append(("dividends", self.symbol))
        if _FakeTicker.div_fail:
            raise RuntimeError("rate limited")
        return {datetime(2026, 6, 20): 0.4, datetime(2026, 9, 19): 0.5}

    @property
    def news(self):
        return [{"title": "old", "providerPublishTime": 1700000000, "link": "https://a"},
                {"content": {"title": "new", "pubDate": "2026-09-25T12:00:00Z"}}]


class YFinanceFeedTest(unittest.TestCase):
    def setUp(self):
        self._saved = sys.modules.get("yfinance")
        yf = types.ModuleType("yfinance")
        yf.Ticker = _FakeTicker
        sys.modules["yfinance"] = yf
        _FakeTicker.fail = False
        _FakeTicker.div_fail = False
        _FakeTicker.calls = []
        self._tmp = tempfile.TemporaryDirectory()
        self.assertTrue(db.init(os.path.join(self._tmp.name, "n.db"), legacy=""))
        self.now = utc(2026, 9, 28, 14, 0)                      # session open
        self.feed = M.YFinanceFeed(store, clock=lambda: self.now)

    def tearDown(self):
        db.close()
        self._tmp.cleanup()
        if self._saved is None:
            sys.modules.pop("yfinance", None)
        else:
            sys.modules["yfinance"] = self._saved

    def test_module_does_not_import_yfinance_at_top_level(self):
        with open(M.__file__, encoding="utf-8") as fh:
            top = [l for l in fh.read().splitlines() if l.startswith(("import ", "from "))]
        self.assertFalse(any("yfinance" in l or "pandas" in l for l in top))

    def test_bars_cached_and_refreshed(self):
        bars = self.feed.daily_bars("SPY", days=10)
        self.assertEqual([(b.date, b.close, b.volume) for b in bars], [("2026-09-24", 1.5, 10.0), ("2026-09-25", 1.8, 0.0)])
        self.feed.daily_bars("SPY", days=10)                     # < 30 min later: cache
        self.assertEqual(len(_FakeTicker.calls), 1)
        self.now += timedelta(minutes=31)
        self.feed.daily_bars("SPY", days=10)
        self.assertEqual(len(_FakeTicker.calls), 2)
        self.feed.daily_bars("SPY", days=50)                      # more history requested: refetch
        self.assertEqual(len(_FakeTicker.calls), 3)
        self.now = utc(2026, 9, 28, 21)                           # after the close: once more, then cached
        self.feed.daily_bars("SPY", days=50)
        self.now = utc(2026, 9, 29, 2)
        self.feed.daily_bars("SPY", days=50)
        self.assertEqual(len(_FakeTicker.calls), 4)

    def test_failures_never_raise(self):
        _FakeTicker.fail = True
        self.assertEqual(self.feed.daily_bars("QQQ"), [])
        self.assertIsNone(self.feed.quote("QQQ"))
        with self.assertLogs("fund.market", level="WARNING") as cm:
            M.YFinanceFeed(store, clock=lambda: self.now).quote("XLF")
        self.assertEqual(len(cm.output), 1)

    def test_quote_and_fallback(self):
        q = self.feed.quote("SPY")
        self.assertEqual((q.price, q.prev_close, q.delayed), (1.95, 1.8, True))
        self.feed.daily_bars("SPY")
        _FakeTicker.fail = True
        q = self.feed.quote("SPY")                                # last cached close
        self.assertEqual((q.price, q.prev_close), (1.8, 1.5))

    def test_dividends_and_news(self):
        self.assertEqual(self.feed.dividends("SPY", date(2026, 7, 1)), [(date(2026, 9, 19), 0.5)])
        self.assertEqual(self.feed.dividends("SPY", date(2026, 9, 19)), [])     # none = [], not None
        news = self.feed.news("SPY", limit=5)
        self.assertEqual([n["title"] for n in news], ["new", "old"])

    def test_dividend_failure_is_none_and_retried(self):
        _FakeTicker.div_fail = True
        self.assertIsNone(self.feed.dividends("SPY", date(2026, 7, 1)))       # unknown, not "no dividends"
        _FakeTicker.div_fail = False
        self.assertEqual(self.feed.dividends("SPY", date(2026, 7, 1)), [(date(2026, 9, 19), 0.5)])  # not cached
        self.assertEqual([c for c in _FakeTicker.calls if c[0] == "dividends"], [("dividends", "SPY")] * 2)
        self.feed.dividends("SPY", date(2026, 7, 1))                          # cached now
        self.assertEqual(len([c for c in _FakeTicker.calls if c[0] == "dividends"]), 2)

    def test_without_store(self):
        feed = M.YFinanceFeed(None, clock=lambda: self.now)
        self.assertEqual(len(feed.daily_bars("SPY", days=5)), 2)
        feed.daily_bars("SPY", days=5)
        self.assertEqual(len(_FakeTicker.calls), 1)


class FakeFeedTest(unittest.TestCase):
    def test_fake_feed(self):
        f = M.FakeFeed(bars={"SPY": [Bar("2026-09-25", 1, 1, 1, 1)]}, quotes={"SPY": 600.0},
                       divs={"SPY": [("2026-09-19", 1.8)]})
        self.assertEqual(f.quote("SPY").price, 600.0)
        self.assertIsNone(f.quote("QQQ"))
        self.assertIsInstance(f.quote("SPY"), Quote)
        self.assertEqual(f.dividends("SPY", date(2026, 9, 1)), [(date(2026, 9, 19), 1.8)])
        self.assertEqual(f.dividends("SPY", date(2026, 9, 19)), [])
        f.div_errors.add("SPY")
        self.assertIsNone(f.dividends("SPY", date(2026, 9, 1)))
        self.assertEqual(len(f.daily_bars("SPY")), 1)


class ConfigBaseUrlTest(unittest.TestCase):
    """config.safe_openrouter_base_url: https only (the API key travels with
    every request). Lives here because config.py has no test module of its
    own; skipped without python-dotenv."""

    def test_https_only(self):
        try:
            import config
        except ImportError:
            self.skipTest("python-dotenv not installed")
        ok = "https://openrouter.ai/api/v1"
        self.assertEqual(config.safe_openrouter_base_url("https://proxy.example.com/v1"),
                         "https://proxy.example.com/v1")
        self.assertEqual(config.safe_openrouter_base_url(""), ok)
        self.assertEqual(config.safe_openrouter_base_url(None), ok)
        for bad in ("http://openrouter.ai/api/v1", "ftp://x", "openrouter.ai/api/v1", "https://"):
            with self.assertLogs("config", level="WARNING"):
                self.assertEqual(config.safe_openrouter_base_url(bad), ok)
        self.assertTrue(config.OPENROUTER_BASE_URL.startswith("https://"))


if __name__ == "__main__":
    unittest.main()
