"""strategy_platform/data/providers/alpaca.py against a fake alpaca-py SDK:
request parameters (IEX feed, adjustment "all"), crypto symbol mapping,
parsing of the raw JSON, keys read per use from the (memory) credential
store, error mapping (401/403/429/network), SDK hardening (no internal
retries, timeouts). No network, no alpaca-py, no real keys.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_data_alpaca -v
"""
import unittest
from datetime import date

try:
    from test_sp_data_support import KEYS, FakeAPIError, Script, fake_gate, fake_sdk, utc
except ImportError:  # pragma: no cover - run as package
    from tests.test_sp_data_support import KEYS, FakeAPIError, Script, fake_gate, fake_sdk, utc

from strategy_platform import secrets
from strategy_platform.core.errors import DataSourceUnavailable, SecretMissing
from strategy_platform.data.providers import alpaca as A
from strategy_platform.data.providers.base import HistoryNotAvailable, SourceAuthRejected, parse_ts

NOW = utc(2026, 10, 2, 21, 0)
SPY_BARS = {"SPY": [{"t": "2026-09-30T04:00:00Z", "o": 600, "h": 605, "l": 598, "c": 603, "v": 12345, "n": 99,
                     "vw": 601.5},
                    {"t": "2026-10-01T04:00:00Z", "o": 603, "h": 606, "l": 601, "c": 604.5, "v": 2000, "n": 10,
                     "vw": 603.2}]}


class ProviderCase(unittest.TestCase):
    def setUp(self):
        secrets.set_store(secrets.MemoryStore(KEYS))
        self.script = Script()
        self.gate, self.ft = fake_gate()
        self.p = A.AlpacaProvider(gate=self.gate, sdk=fake_sdk(self.script), clock=lambda: NOW)

    def tearDown(self):
        secrets.set_store(None)

    def last_request(self):
        return self.script.calls[-1][1]


class BarsTest(ProviderCase):
    def test_equity_bars_request_and_parsing(self):
        self.script.set("get_stock_bars", SPY_BARS)
        rows = self.p.get_bars("spy", "1Day", utc(2026, 9, 25), NOW)
        req = self.last_request()
        self.assertEqual(type(req).__name__, "StockBarsRequest")
        self.assertEqual(req.kw["symbol_or_symbols"], ["SPY"])
        self.assertEqual((req.kw["feed"], req.kw["adjustment"]), ("iex", "all"))
        self.assertEqual(req.kw["timeframe"], ("TF", 1, "Day"))
        self.assertEqual(len(rows), 2)
        r = rows[1]
        self.assertEqual((r["symbol"], r["timeframe"], r["ts"], r["close"], r["trade_count"], r["source"]),
                         ("SPY", "1Day", "2026-10-01T04:00:00+00:00", 604.5, 10, "alpaca"))
        self.assertEqual(r["known_at"], "2026-10-01T20:00:00+00:00")      # 16:00 New York (EDT)
        self.assertEqual(r["fetched_at"], "2026-10-02T21:00:00+00:00")

    def test_keys_raw_data_and_hardening(self):
        self.script.set("get_stock_bars", SPY_BARS)
        self.p.get_bars("SPY", "1Day", utc(2026, 9, 25), NOW)
        name, client = self.script.clients[-1]
        self.assertEqual(name, "StockHistoricalDataClient")
        self.assertEqual((client.api_key, client.secret_key, client.raw_data), ("test-key-id", "test-secret", True))
        self.assertEqual(client._retry, 0)                                   # the gate is the only retry loop
        client._session.request("GET", "https://data.alpaca.markets/v2/stocks/bars")
        self.assertEqual(client._session.calls,                              # wrapper adds the timeout
                         [("GET", "https://data.alpaca.markets/v2/stocks/bars", {"timeout": A.REQUEST_TIMEOUT_SECONDS})])

    def test_session_timeout_wrapper(self):
        calls = []

        class Sess:
            def request(self, method, url, **kw):
                calls.append(kw)

        class C:
            _retry = 3
            _session = Sess()
        c = A.harden_client(C(), 12.0)
        c._session.request("GET", "https://x")
        c._session.request("GET", "https://x", timeout=3)
        self.assertEqual(calls, [{"timeout": 12.0}, {"timeout": 3}])
        A.harden_client(c, 12.0)                                             # not wrapped twice
        c._session.request("GET", "https://x")
        self.assertEqual(len(calls), 3)

    def test_crypto_symbol_mapping(self):
        self.script.set("get_crypto_bars", {"BTC/USD": [{"t": "2026-10-01T00:00:00Z", "o": 1, "h": 2, "l": 0.5,
                                                         "c": 1.5, "v": 3.25, "n": 7, "vw": 1.2}]})
        rows = self.p.get_bars("BTC-USD", "1Day", utc(2026, 9, 25), NOW)
        req = self.last_request()
        self.assertEqual(type(req).__name__, "CryptoBarsRequest")
        self.assertEqual(req.kw["symbol_or_symbols"], ["BTC/USD"])
        self.assertNotIn("feed", req.kw)
        self.assertEqual(self.script.clients[-1][0], "CryptoHistoricalDataClient")
        self.assertEqual(self.script.clients[-1][1].api_key, "test-key-id")  # keys sent on crypto too
        self.assertEqual((rows[0]["symbol"], rows[0]["volume"]), ("BTC-USD", 3.25))
        self.assertEqual(rows[0]["known_at"], "2026-10-02T00:00:00+00:00")  # start + 24 h

    def test_empty_and_null_series(self):
        self.script.set("get_stock_bars", {})
        self.assertEqual(self.p.get_bars("SPY", "1Day", utc(2026, 9, 25), NOW), [])
        self.script.set("get_stock_bars", {"SPY": None})
        self.assertEqual(self.p.get_bars("SPY", "1Day", utc(2026, 9, 25), NOW), [])

    def test_malformed_bar_is_an_error_not_a_skip(self):
        self.script.set("get_stock_bars", {"SPY": [{"t": "2026-10-01T04:00:00Z", "o": 1, "h": 1, "l": 1}]})
        with self.assertRaises(DataSourceUnavailable) as cm:
            self.p.get_bars("SPY", "1Day", utc(2026, 9, 25), NOW)
        self.assertIn("unexpected data from Alpaca", cm.exception.user_message)

    def test_history_before_2016(self):
        with self.assertRaises(HistoryNotAvailable) as cm:
            self.p.get_bars("SPY", "1Day", utc(2015, 6, 1), NOW)
        self.assertIn("History before 2016 is not available from the configured source", cm.exception.user_message)
        self.assertEqual(self.script.calls, [])


class QuoteTradeTest(ProviderCase):
    def test_latest_quote_iex_with_nanoseconds(self):
        self.script.set("get_stock_latest_quote", {"SPY": {"ap": 604.6, "as": 2, "bp": 604.4, "bs": 3,
                                                           "t": "2026-10-02T19:59:58.123456789Z"}})
        q = self.p.latest_quote("SPY")
        self.assertEqual(self.last_request().kw["feed"], "iex")
        self.assertEqual((q["bid"], q["ask"], q["ts"]), (604.4, 604.6, "2026-10-02T19:59:58.123456+00:00"))

    def test_latest_trade_crypto(self):
        self.script.set("get_crypto_latest_trade", {"BTC/USD": {"p": 61000.5, "s": 0.01, "t": "2026-10-02T20:59:00Z"}})
        t = self.p.latest_trade("BTC-USD")
        self.assertEqual(self.last_request().kw["symbol_or_symbols"], ["BTC/USD"])
        self.assertEqual((t["symbol"], t["price"]), ("BTC-USD", 61000.5))

    def test_missing_symbol_in_answer(self):
        self.script.set("get_stock_latest_trade", {})
        with self.assertRaises(DataSourceUnavailable) as cm:
            self.p.latest_trade("SPY")
        self.assertIn("no trade for SPY", cm.exception.user_message)


class ErrorTest(ProviderCase):
    def test_401_clear_message(self):
        self.script.set("get_stock_bars", FakeAPIError(401, "unauthorized."))
        with self.assertRaises(SourceAuthRejected) as cm:
            self.p.get_bars("SPY", "1Day", utc(2026, 9, 25), NOW)
        self.assertEqual(cm.exception.user_message,
                         "Alpaca rejected the keys — check them in Hedge Fund → Settings → Keys.")
        self.assertEqual(len(self.script.calls), 1)                          # never retried

    def test_403_plan_message(self):
        self.script.set("get_stock_latest_quote", FakeAPIError(403, "subscription does not permit querying recent SIP data"))
        with self.assertRaises(DataSourceUnavailable) as cm:
            self.p.latest_quote("SPY")
        self.assertNotIsInstance(cm.exception, SourceAuthRejected)
        self.assertIn("plan does not include", cm.exception.user_message)

    def test_429_retry_then_failure(self):
        self.script.set("get_stock_bars", FakeAPIError(429, "too many requests"))
        with self.assertRaises(DataSourceUnavailable) as cm:
            self.p.get_bars("SPY", "1Day", utc(2026, 9, 25), NOW)
        self.assertEqual(len(self.script.calls), 3)
        self.assertIn("HTTP 429", cm.exception.user_message)
        self.script.calls.clear()
        self.script.set("get_stock_bars", FakeAPIError(429), FakeAPIError(500), SPY_BARS)
        self.assertEqual(len(self.p.get_bars("SPY", "1Day", utc(2026, 9, 25), NOW)), 2)
        self.assertEqual(len(self.script.calls), 3)

    def test_missing_keys(self):
        secrets.set_store(secrets.MemoryStore({}))
        with self.assertRaises(SecretMissing) as cm:
            self.p.get_bars("SPY", "1Day", utc(2026, 9, 25), NOW)
        self.assertIn("Settings → Keys", cm.exception.user_message)
        self.assertEqual((self.script.calls, self.script.clients, self.gate.calls), ([], [], 0))

    def test_keys_read_per_use(self):
        self.script.set("get_stock_latest_trade", {"SPY": {"p": 1.0, "t": "2026-10-02T19:00:00Z"}})
        self.p.latest_trade("SPY")
        secrets.set_store(secrets.MemoryStore({secrets.target("alpaca-paper-key-id"): "new-id",
                                               secrets.target("alpaca-paper-secret-key"): "new-secret"}))
        self.p.latest_trade("SPY")
        self.assertEqual([c.api_key for _n, c in self.script.clients], ["test-key-id", "new-id"])

    def test_module_has_no_top_level_sdk_import(self):
        with open(A.__file__, encoding="utf-8") as fh:
            top = [l for l in fh.read().splitlines() if l.startswith(("import ", "from "))]
        self.assertFalse(any("alpaca" in l.split()[1] for l in top if not l.startswith("from ."))
                         or any("pandas" in l for l in top))

    def test_sdk_missing_is_a_clear_error(self):
        p = A.AlpacaProvider(gate=self.gate, clock=lambda: NOW)
        try:
            import alpaca  # noqa: F401
            self.skipTest("alpaca-py is installed here")
        except ImportError:
            pass
        with self.assertRaises(DataSourceUnavailable) as cm:
            p.get_bars("SPY", "1Day", utc(2026, 9, 25), NOW)
        self.assertIn("alpaca-py package is not installed", cm.exception.user_message)


class NewsCorporateActionsTest(ProviderCase):
    def test_news(self):
        self.script.set("get_news", {"news": [
            {"id": 1, "headline": "  Old   one ", "source": "benzinga", "url": "https://a.test/1",
             "created_at": "2026-10-01T10:00:00Z", "updated_at": "2026-10-01T10:05:00Z", "symbols": ["BTCUSD"]},
            {"id": 2, "headline": "New", "source": "benzinga", "url": "javascript:alert(1)",
             "created_at": "2026-10-02T10:00:00Z", "symbols": ["BTCUSD"]},
            {"id": 3, "headline": "", "created_at": "2026-10-02T11:00:00Z"}]})
        items = self.p.news(["BTC-USD"], limit=5)
        req = self.last_request()
        self.assertEqual((req.kw["symbols"], req.kw["limit"], req.kw["sort"]), ("BTCUSD", 5, "desc"))
        self.assertEqual([i["headline"] for i in items], ["New", "Old one"])
        self.assertIsNone(items[0]["url"])
        self.assertEqual((items[1]["publisher"], items[1]["created_at"]), ("benzinga", "2026-10-01T10:00:00+00:00"))

    def test_corporate_actions(self):
        self.script.set("get_corporate_actions", {
            "cash_dividends": [{"symbol": "SPY", "rate": 1.75, "special": False, "ex_date": "2026-09-19",
                                "process_date": "2026-10-31", "payable_date": "2026-10-31"}],
            "forward_splits": [{"symbol": "SPY", "new_rate": 2, "old_rate": 1, "ex_date": "2026-09-01",
                                "process_date": "2026-09-01"}]})
        rows = self.p.corporate_actions(["SPY", "BTC-USD"], date(2026, 8, 1), date(2026, 10, 2))
        req = self.last_request()
        self.assertEqual(req.kw["symbols"], ["SPY"])
        self.assertEqual(req.kw["types"], ["cash_dividend", "forward_split", "reverse_split"])
        self.assertEqual((req.kw["start"], req.kw["end"]), (date(2026, 8, 1), date(2026, 10, 2)))
        self.assertEqual([(r["kind"], r["ex_date"]) for r in rows],
                         [("forward_split", "2026-09-01"), ("cash_dividend", "2026-09-19")])
        self.assertEqual(rows[1]["rate"], 1.75)

    def test_crypto_has_no_corporate_actions(self):
        self.assertEqual(self.p.corporate_actions(["BTC-USD"], date(2026, 8, 1), date(2026, 10, 2)), [])
        self.assertEqual(self.script.calls, [])


class ParseTest(unittest.TestCase):
    def test_parse_ts(self):
        self.assertEqual(parse_ts("2026-10-02T19:59:58.123456789Z").microsecond, 123456)
        self.assertEqual(parse_ts("2026-10-02T19:59:58.1Z").microsecond, 100000)
        self.assertEqual(parse_ts("2026-10-02T19:59:58Z").tzinfo is not None, True)
        with self.assertRaises(ValueError):
            parse_ts("")

    def test_news_symbol(self):
        self.assertEqual((A.news_symbol("BTC-USD"), A.news_symbol("spy")), ("BTCUSD", "SPY"))


if __name__ == "__main__":
    unittest.main()
