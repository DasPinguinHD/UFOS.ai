"""fund/market.py: NYSE calendar (re-exported from strategy_platform/core/
calendars.py: holidays, weekends, DST switch weeks, early closes,
trading-day counting) and AlpacaFeed on a fake data layer (bars only when
complete and fresh, quotes from the latest trade without any stand-in price,
dividends None/[] contract, news shape, the old engine running on it).
Offline, stdlib unittest; no yfinance, no alpaca-py, no network.

Run from HedgeFund/backend:  python -m unittest tests.test_fund_market -v
"""
import unittest
from datetime import date, datetime, timedelta, timezone

try:
    from fund_testkit import FRI, PRICES, EngineCase, make_bars
except ImportError:  # pragma: no cover - run as package
    from tests.fund_testkit import FRI, PRICES, EngineCase, make_bars

from strategy_platform.data import market_feed as M
from fund import store
from strategy_platform.execution.paper_broker import PaperBroker
from fund.engine import FundEngine
from strategy_platform.core.types import Bar, Quote
from strategy_platform.core.errors import DataSourceUnavailable


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


# --- AlpacaFeed (fakes for the data layer: no network, no alpaca-py) ------------------------------

def _eq_row(day, close, symbol="SPY"):
    """Daily IEX bar dict as data.cache returns it (ts = NY midnight; known at the close)."""
    from strategy_platform.data import pit
    ts = datetime.fromisoformat(day + "T04:00:00+00:00")
    return {"symbol": symbol, "timeframe": "1Day", "ts": ts.isoformat(timespec="seconds"), "open": close,
            "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 100.0, "vwap": close,
            "trade_count": 5, "source": "alpaca", "known_at": pit.known_at_iso("equity", "1Day", ts),
            "fetched_at": "2026-10-02T21:00:00+00:00"}


def _cr_row(day, close, symbol="BTC-USD"):
    ts = datetime.fromisoformat(day + "T00:00:00+00:00")
    return {"symbol": symbol, "timeframe": "1Day", "ts": ts.isoformat(timespec="seconds"), "open": close,
            "high": close, "low": close, "close": close, "volume": 1.5, "vwap": close, "trade_count": 5,
            "source": "alpaca", "known_at": (ts + timedelta(days=1)).isoformat(timespec="seconds"),
            "fetched_at": "2026-10-02T21:00:00+00:00"}


SEP = ["2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"]


class FakeCache:
    """data.cache.BarCache stand-in: serves rows known at ``end``; records calls."""

    def __init__(self, rows=None):
        self.rows = dict(rows or {})
        self.fail = {}
        self.calls = []

    def get_bars(self, symbol, timeframe, start, end=None, *, require_fresh_through=None):
        self.calls.append((symbol, timeframe, start, end, require_fresh_through))
        if symbol in self.fail:
            raise self.fail[symbol]
        s, e = start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")
        return [r for r in self.rows.get(symbol, []) if s <= r["ts"] <= e and r["known_at"] <= e]


class FakeFetch:
    """registry.fetch stand-in: ``answers[(data_type, op)]`` = value, callable or exception."""

    def __init__(self):
        self.answers = {}
        self.calls = []

    def __call__(self, data_type, op, *args):
        self.calls.append((data_type, op) + args)
        a = self.answers.get((data_type, op))
        if isinstance(a, dict) and args and isinstance(args[0], str) and args[0] in a:
            a = a[args[0]]
        if isinstance(a, BaseException):
            raise a
        return a(*args) if callable(a) else a


def unavailable(why="network error — data.alpaca.markets could not be reached", dt="equity_quotes"):
    return DataSourceUnavailable("alpaca", dt, why)


class AlpacaFeedCase(unittest.TestCase):
    def setUp(self):
        self.now = utc(2026, 10, 2, 14, 0)                     # Fri, session open (10:00 EDT)
        self.cache = FakeCache({"SPY": [_eq_row(d, 600.0 + i) for i, d in enumerate(SEP)],
                                "BTC-USD": [_cr_row(f"2026-09-{d:02d}", 60000.0 + d) for d in range(24, 31)]
                                + [_cr_row("2026-10-01", 61000.0), _cr_row("2026-10-02", 62000.0)]})
        self.fetch = FakeFetch()
        self.feed = M.AlpacaFeed(cache=self.cache, fetch=self.fetch, clock=lambda: self.now)


class AlpacaFeedBarsTest(AlpacaFeedCase):
    def test_no_yfinance_and_no_vendor_import_at_top_level(self):
        with open(M.__file__, encoding="utf-8") as fh:
            top = [l for l in fh.read().splitlines() if l.startswith(("import ", "from "))]
        self.assertFalse(any(w in l for l in top for w in ("yfinance", "pandas", "alpaca")))
        self.assertFalse(hasattr(M, "YFinanceFeed"))

    def test_equity_bars_completed_and_fresh_through_last_close(self):
        self.now = utc(2026, 10, 2, 21, 0)                     # after Friday's close
        bars = self.feed.daily_bars("SPY", days=3)
        self.assertEqual([(b.date, b.close) for b in bars],
                         [("2026-09-30", 604.0), ("2026-10-01", 605.0), ("2026-10-02", 606.0)])
        sym, tf, start, end, fresh = self.cache.calls[-1]
        self.assertEqual((sym, tf, end, fresh), ("SPY", "1Day", self.now, utc(2026, 10, 2, 20, 0)))
        self.assertLess(start, fresh - timedelta(days=3))

    def test_bars_during_the_session_exclude_today(self):
        bars = self.feed.daily_bars("SPY")
        self.assertEqual(bars[-1].date, "2026-10-01")
        self.assertEqual(self.cache.calls[-1][4], utc(2026, 10, 1, 20, 0))

    def test_long_lookback_starts_2016(self):
        self.feed.daily_bars("SPY", days=6000)
        self.assertEqual(self.cache.calls[-1][2], utc(2016, 1, 1))

    def test_crypto_bars_utc_dates_fresh_through_now(self):
        bars = self.feed.daily_bars("BTC-USD", days=2)
        self.assertEqual([b.date for b in bars], ["2026-09-30", "2026-10-01"])    # Oct 2 not complete
        self.assertEqual(self.cache.calls[-1][4], self.now)

    def test_failure_raises_with_reason(self):
        self.cache.fail["SPY"] = unavailable("no 1Day bar for SPY through ... yet", "equity_bars")
        with self.assertRaises(DataSourceUnavailable) as cm:
            self.feed.daily_bars("SPY")
        self.assertIn("no 1Day bar for SPY", cm.exception.user_message)


class AlpacaFeedQuoteTest(AlpacaFeedCase):
    def trade(self, sym, price, ts):
        self.fetch.answers.setdefault(("crypto_quotes" if "-" in sym else "equity_quotes", "latest_trade"), {})[sym] = \
            {"symbol": sym, "price": price, "size": 1.0, "ts": ts.isoformat()}

    def test_equity_quote_from_latest_trade(self):
        self.trade("SPY", 607.25, self.now - timedelta(minutes=1))
        q = self.feed.quote("SPY")
        self.assertEqual((q.symbol, q.price, q.prev_close, q.delayed, q.stale), ("SPY", 607.25, 605.0, False, False))
        self.assertEqual(q.ts, "2026-10-02T13:59:00+00:00")
        self.assertEqual(self.cache.calls[-1][4], utc(2026, 10, 1, 20, 0))       # prev session's bar required

    def test_old_trade_during_session_is_refused(self):
        self.trade("SPY", 607.25, self.now - timedelta(minutes=45))
        with self.assertRaises(DataSourceUnavailable) as cm:
            self.feed.quote("SPY")
        self.assertIn("no IEX trade in SPY for 45 min", cm.exception.user_message)

    def test_no_stand_in_price_when_the_source_fails(self):
        self.feed.daily_bars("SPY")                                # bars are cached ...
        self.fetch.answers[("equity_quotes", "latest_trade")] = unavailable()
        with self.assertRaises(DataSourceUnavailable) as cm:      # ... but never stamped as a quote
            self.feed.quote("SPY")
        self.assertIn("could not be reached", cm.exception.user_message)

    def test_outside_session_last_session_trade_only(self):
        self.now = utc(2026, 10, 3, 15, 0)                        # Saturday
        self.trade("SPY", 606.5, utc(2026, 10, 2, 19, 59))
        q = self.feed.quote("SPY")
        self.assertEqual((q.price, q.prev_close), (606.5, 605.0))
        self.trade("SPY", 606.5, utc(2026, 10, 1, 19, 59))       # Thursday's trade on Saturday
        with self.assertRaises(DataSourceUnavailable):
            self.feed.quote("SPY")

    def test_crypto_quote_and_stale_trade(self):
        self.trade("BTC-USD", 62500.0, self.now - timedelta(minutes=2))
        q = self.feed.quote("BTC-USD")
        self.assertEqual((q.price, q.prev_close, q.delayed), (62500.0, 61000.0, False))
        self.trade("BTC-USD", 62500.0, self.now - timedelta(minutes=M.STALE_QUOTE_MINUTES + 5))
        with self.assertRaises(DataSourceUnavailable) as cm:
            self.feed.quote("BTC-USD")
        self.assertIn("min old — no current price", cm.exception.user_message)

    def test_invalid_price(self):
        self.trade("SPY", 0.0, self.now)
        with self.assertRaises(DataSourceUnavailable):
            self.feed.quote("SPY")


class AlpacaFeedDividendsNewsTest(AlpacaFeedCase):
    def test_dividends(self):
        self.assertEqual(self.feed.dividends("BTC-USD", date(2026, 9, 1)), [])
        self.assertEqual(self.fetch.calls, [])
        self.fetch.answers[("corporate_actions", "corporate_actions")] = [
            {"symbol": "SPY", "kind": "cash_dividend", "ex_date": "2026-09-19", "rate": 1.8},
            {"symbol": "SPY", "kind": "cash_dividend", "ex_date": "2026-09-25", "rate": 0.0},
            {"symbol": "QQQ", "kind": "cash_dividend", "ex_date": "2026-09-20", "rate": 0.5}]
        self.assertEqual(self.feed.dividends("SPY", date(2026, 9, 1)), [(date(2026, 9, 19), 1.8)])
        call = self.fetch.calls[-1]
        self.assertEqual(call[2:], (["SPY"], date(2026, 9, 2), date(2026, 10, 3), ("cash_dividend",)))
        self.assertEqual(self.feed.dividends("SPY", date(2026, 9, 19)), [])     # none = [], not None
        self.assertEqual(len(self.fetch.calls), 1)                              # cached 2 h

    def test_dividend_failure_is_none_and_retried(self):
        self.fetch.answers[("corporate_actions", "corporate_actions")] = unavailable(dt="corporate_actions")
        with self.assertLogs("strategy_platform.data.market_feed", level="WARNING"):
            self.assertIsNone(self.feed.dividends("SPY", date(2026, 9, 1)))
        self.fetch.answers[("corporate_actions", "corporate_actions")] = []
        self.assertEqual(self.feed.dividends("SPY", date(2026, 9, 1)), [])
        self.assertEqual(len(self.fetch.calls), 2)

    def test_news_shape(self):
        self.fetch.answers[("news", "news")] = [
            {"headline": "Old", "publisher": "benzinga", "created_at": "2026-10-01T10:00:00+00:00",
             "url": "https://a.test/1"},
            {"headline": "New", "publisher": "benzinga", "created_at": "2026-10-02T10:00:00+00:00",
             "url": "javascript:x"},
            {"headline": "", "created_at": "2026-10-02T11:00:00+00:00"}]
        news = self.feed.news("SPY", limit=5)
        self.assertEqual(news, [{"title": "New", "publisher": "benzinga", "published_at": "2026-10-02T10:00:00+00:00",
                                 "url": None},
                                {"title": "Old", "publisher": "benzinga", "published_at": "2026-10-01T10:00:00+00:00",
                                 "url": "https://a.test/1"}])
        self.assertEqual(self.fetch.calls[-1], ("news", "news", ["SPY"], None, None, 5))
        self.fetch.answers[("news", "news")] = unavailable(dt="news")
        with self.assertRaises(DataSourceUnavailable):
            self.feed.news("SPY")


class SentimentNewsErrorTest(unittest.TestCase):
    """A failed Alpaca news fetch is not "no headlines": the engine marks it
    with an error item and the sentiment agent says why, without an LLM call."""

    def test_news_error_marker(self):
        import asyncio
        from fund.agents import sentiment
        from strategy_platform.core.types import MarketContext
        calls = []

        async def llm(*a, **kw):
            calls.append(a)
            raise AssertionError("no LLM call expected")
        ctx = MarketContext(now="2026-10-02T14:00:00+00:00", params={"ai_daily_budget_usd": 1.0}, session={},
                            news={"SPY": [{"error": "alpaca is unavailable for news: HTTP 503"}]})
        out = asyncio.run(sentiment.evaluate_batch(ctx, ["SPY"], {}, None, llm=llm))
        sig = out["SPY"]
        self.assertEqual(sig.source, "unavailable")
        self.assertEqual(sig.summary, "News could not be loaded: alpaca is unavailable for news: HTTP 503")
        self.assertEqual(sig.confidence, 0.0)
        self.assertEqual(calls, [])
        self.assertIsNone(sentiment.news_error([{"title": "x", "published_at": None}]))
        self.assertEqual(sentiment.news_error({"error": "x"}), "x")


class AlpacaFeedEngineTest(EngineCase):
    """The old engine on AlpacaFeed (fake data layer): a symbol without data
    is skipped with the source's reason, the run is partial, the others run."""

    def setUp(self):
        super().setUp()
        rows = []
        for b in make_bars(PRICES["SPY"]):
            rows.append(dict(_eq_row(b.date, b.close), open=b.open, high=b.high, low=b.low))
        self.acache = FakeCache({"SPY": rows})
        self.acache.fail["QQQ"] = unavailable("Alpaca server error (HTTP 503) — gave up after 3 attempts",
                                              "equity_bars")
        self.afetch = FakeFetch()
        self.afetch.answers[("equity_quotes", "latest_trade")] = {
            "SPY": {"symbol": "SPY", "price": 500.0, "size": 1.0, "ts": (FRI - timedelta(minutes=1)).isoformat()},
            "QQQ": unavailable("Alpaca server error (HTTP 503) — gave up after 3 attempts")}
        self.afetch.answers[("news", "news")] = []
        self.feed = M.AlpacaFeed(cache=self.acache, fetch=self.afetch, clock=lambda: self.now)
        self.broker = PaperBroker(store, self.feed.quote, clock=lambda: self.now, dividends_fn=self.feed.dividends,
                                  bars_fn=self.feed.daily_bars)
        self.engine = FundEngine(self.feed, self.broker, store, clock=lambda: self.now, startup_delay=0.0)

    def test_cycle_partial_with_reason(self):
        out = self.cycle()
        self.assertEqual(out["status"], "partial", out)
        run = store.get_run(out["run_id"])
        self.assertIn("QQQ", run["run"]["error"])
        self.assertIn("HTTP 503", run["run"]["error"])
        self.assertEqual([r["symbol"] for r in run["recommendations"]], ["SPY"])
        self.assertEqual(run["orders"], [])


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
