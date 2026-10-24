"""strategy_platform/data/providers/tiingo.py + the Tiingo gate config
(SPEC §8.1): parsing, split math (2:1 and 4:1 splits), dividends, missing
key message, key redaction in errors, TLS + header, error mapping, the
hourly/daily request windows with progress events. Offline (fake urlopen).

Run from HedgeFund/backend:  python -m unittest tests.test_sp_data_tiingo -v
"""
import io
import json
import ssl
import unittest
import urllib.error
from datetime import date

try:
    from test_sp_data_support import FakeTime, fake_gate
except ImportError:  # pragma: no cover - run as package
    from tests.test_sp_data_support import FakeTime, fake_gate

from strategy_platform import secrets
from strategy_platform.core.errors import DataSourceUnavailable, SecretMissing
from strategy_platform.data import http_gate, licences, registry
from strategy_platform.data.providers import tiingo
from strategy_platform.data.providers.base import SourceAuthRejected

KEY = "tiingo-SECRET-abc123"


def raw_row(day, close, *, adj=None, volume=10.0, div=0.0, split=1.0):
    adj = close if adj is None else adj
    return {"date": f"{day}T00:00:00.000Z", "open": close, "high": close * 1.01, "low": close * 0.99, "close": close,
            "volume": volume, "adjOpen": adj, "adjHigh": adj * 1.01, "adjLow": adj * 0.99, "adjClose": adj,
            "adjVolume": volume, "divCash": div, "splitFactor": split}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeUrlopen:
    """Records requests; ``script`` items are bytes (body) or exceptions."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    def __call__(self, req, timeout=None, context=None):
        self.calls.append({"url": req.full_url, "headers": dict(req.header_items()), "timeout": timeout,
                           "context": context})
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, BaseException):
            raise item
        return FakeResponse(item)


def http_error(code, body=b"{}"):
    return urllib.error.HTTPError("https://api.tiingo.com/x", code, "err", {}, io.BytesIO(body))


class SplitMathTest(unittest.TestCase):
    def rows(self):
        # true price 100 throughout; a 2:1 split on 01-05 and a 4:1 split on 01-07 (factors on the ex-dates)
        return tiingo.parse_prices([
            raw_row("2015-01-02", 800.0, adj=100.0), raw_row("2015-01-05", 400.0, adj=100.0, split=2.0),
            raw_row("2015-01-06", 400.0, adj=100.0, div=1.0), raw_row("2015-01-07", 100.0, adj=100.0, split=4.0),
            raw_row("2015-01-08", 100.0, adj=99.0)])

    def test_split_series_divides_by_later_split_factors(self):
        s = tiingo.build_series("SPY", self.rows(), "2026-10-03T00:00:00+00:00")
        self.assertEqual([b["close"] for b in s["split"]], [100.0] * 5)
        self.assertEqual([b["volume"] for b in s["split"]], [80.0, 40.0, 40.0, 10.0, 10.0])
        self.assertEqual([b["close"] for b in s["all"]], [100.0, 100.0, 100.0, 100.0, 99.0])
        self.assertTrue(all(b["adjustment"] == "split" for b in s["split"]))
        self.assertTrue(all(b["source"] == "tiingo" and b["vwap"] is None for b in s["split"] + s["all"]))
        self.assertEqual(s["split"][0]["ts"], "2015-01-02T05:00:00+00:00")            # NY midnight (EST)
        self.assertEqual(s["split"][0]["known_at"], "2015-01-02T21:00:00+00:00")      # 16:00 NY, pre-calendar

    def test_dividends_and_splits(self):
        s = tiingo.build_series("SPY", self.rows(), "2026-10-03T00:00:00+00:00")
        self.assertEqual([(d["ex_date"], d["cash_amount"], d["kind"]) for d in s["dividends"]],
                         [("2015-01-06", 1.0, "cash_dividend")])
        self.assertEqual([(d["ex_date"], d["split_ratio"], d["kind"]) for d in s["splits"]],
                         [("2015-01-05", 2.0, "forward_split"), ("2015-01-07", 4.0, "forward_split")])
        self.assertEqual(s["dividends"][0]["known_at"], "2015-01-06T14:30:00+00:00")  # 09:30 NY on the ex-date
        rev = tiingo.build_series("X", tiingo.parse_prices([raw_row("2015-01-02", 1.0),
                                                            raw_row("2015-01-05", 10.0, split=0.1)]), "x")
        self.assertEqual(rev["splits"][0]["kind"], "reverse_split")
        self.assertAlmostEqual(rev["split"][0]["close"], 10.0)

    def test_invalid_rows_are_refused_not_defaulted(self):
        bad = raw_row("2015-01-02", 1.0)
        del bad["splitFactor"]
        for rows in ([bad], [raw_row("2015-01-02", float("nan"))], [raw_row("2015-01-02", 1.0)] * 2, {"a": 1}):
            with self.assertRaises((ValueError, TypeError, KeyError)):
                tiingo.parse_prices(rows)


class ProviderTest(unittest.TestCase):
    def setUp(self):
        secrets.set_store(secrets.MemoryStore({secrets.target("tiingo-api-key"): KEY}))
        self.addCleanup(secrets.set_store, None)
        self.gate, self.ft = fake_gate("tiingo")

    def provider(self, opener):
        return tiingo.TiingoProvider(gate=self.gate, urlopen=opener)

    def test_request_shape_tls_and_parse(self):
        opener = FakeUrlopen(json.dumps([raw_row("2015-01-02", 10.0)]).encode())
        rows = self.provider(opener).daily_prices("BRK.B", date(2000, 1, 3), date(2016, 3, 30))
        self.assertEqual(rows[0]["close"], 10.0)
        call = opener.calls[0]
        self.assertTrue(call["url"].startswith("https://api.tiingo.com/tiingo/daily/brk-b/prices?"))
        self.assertIn("startDate=2000-01-03", call["url"])
        self.assertNotIn(KEY, call["url"])
        self.assertEqual(call["headers"]["Authorization"], "Token " + KEY)
        self.assertIsInstance(call["context"], ssl.SSLContext)
        self.assertEqual(call["context"].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(call["context"].check_hostname)

    def test_missing_key_message(self):
        secrets.set_store(secrets.MemoryStore({}))
        opener = FakeUrlopen(b"[]")
        with self.assertRaises(SecretMissing) as cm:
            self.provider(opener).daily_prices("SPY", date(2010, 1, 4), date(2010, 2, 1))
        self.assertEqual(cm.exception.user_message, "Tiingo API key is not set — enter it in Hedge Fund → Keys & data.")
        self.assertEqual(opener.calls, [])                    # nothing sent without a key

    def test_key_is_redacted_from_errors(self):
        for err in (http_error(400, json.dumps({"detail": f"bad token {KEY}"}).encode()),
                    urllib.error.URLError(f"proxy refused Token {KEY}"),
                    DataSourceUnavailable("tiingo", "equity_bars_pre2016", f"echo {KEY}")):
            with self.assertRaises(DataSourceUnavailable) as cm:
                self.provider(FakeUrlopen(err)).daily_prices("SPY", date(2010, 1, 4), date(2010, 2, 1))
            for text in (cm.exception.user_message, str(cm.exception), cm.exception.detail):
                self.assertNotIn(KEY, text)
        with self.assertRaises(DataSourceUnavailable) as cm:
            self.provider(FakeUrlopen(json.dumps({"detail": f"Error for {KEY}"}).encode())).daily_prices(
                "SPY", date(2010, 1, 4), date(2010, 2, 1))
        self.assertNotIn(KEY, cm.exception.user_message)
        self.assertEqual(tiingo.redact("Authorization: Token abc def", ), "Authorization: Token *** def")

    def test_error_mapping(self):
        with self.assertRaises(SourceAuthRejected) as cm:
            self.provider(FakeUrlopen(http_error(401))).daily_prices("SPY", date(2010, 1, 4), date(2010, 2, 1))
        self.assertEqual(cm.exception.user_message, tiingo.AUTH_MESSAGE)
        with self.assertRaises(tiingo.UnknownSymbol):
            self.provider(FakeUrlopen(http_error(404))).daily_prices("ZZZZ", date(2010, 1, 4), date(2010, 2, 1))
        opener = FakeUrlopen(http_error(429))
        with self.assertRaises(DataSourceUnavailable) as cm:
            self.provider(opener).daily_prices("SPY", date(2010, 1, 4), date(2010, 2, 1))
        self.assertIn("allowance", cm.exception.user_message)
        self.assertEqual(len(opener.calls), 1)                # hourly allowance: no quick retries
        opener = FakeUrlopen(http_error(503))
        with self.assertRaises(DataSourceUnavailable) as cm:
            self.provider(opener).daily_prices("SPY", date(2010, 1, 4), date(2010, 2, 1))
        self.assertEqual(len(opener.calls), 3)                # 5xx retried by the gate, bounded
        with self.assertRaises(DataSourceUnavailable):
            self.provider(FakeUrlopen(b"not json")).daily_prices("SPY", date(2010, 1, 4), date(2010, 2, 1))
        with self.assertRaises(ValueError):
            self.provider(FakeUrlopen(b"[]")).daily_prices("BTC-USD", date(2010, 1, 4), date(2010, 2, 1))


class GateConfigTest(unittest.TestCase):
    def test_default_tiingo_gate_is_conservative(self):
        http_gate.set_gate("tiingo", None)
        self.addCleanup(http_gate.set_gate, "tiingo", None)
        g = http_gate.gate_for("tiingo")
        self.assertEqual(g.limiter.windows, ((40, 3600.0), (800, 86400.0)))   # 80 % of 50/h and 1,000/day
        self.assertLessEqual(g.limiter.windows[0][0], 50)
        self.assertGreaterEqual(g.acquire_timeout, 3600)
        self.assertIsNone(http_gate.gate_for("alpaca").limiter)

    def test_hourly_window_waits_with_progress(self):
        gate, ft = fake_gate("tiingo")
        events = []
        for _ in range(40):
            gate.call(lambda: "ok", data_type="equity_bars_pre2016", classify=lambda e, d: e)
        self.assertLess(ft.t - 1000.0, 120)                   # 40 requests: only the per-minute bucket paces them
        gate.call(lambda: "ok", data_type="equity_bars_pre2016", classify=lambda e, d: e, progress=events.append)
        self.assertGreaterEqual(ft.t - 1000.0, 3600 - 1)      # the 41st waited for the hourly window
        self.assertTrue(events)
        self.assertIn("waiting for Tiingo rate limit (0 requests left this hour", events[0]["message"])
        self.assertTrue(all(e["waiting"] for e in events))

    def test_daily_allowance_used_up_raises_at_once(self):
        ft = FakeTime()
        gate = http_gate.HttpGate("tiingo", windows=((2, 3600.0), (3, 86400.0)), monotonic=ft.monotonic,
                                  sleep=ft.sleep)
        for _ in range(3):
            gate.call(lambda: 1, data_type="equity_bars_pre2016", classify=lambda e, d: e)
        start = ft.t
        with self.assertRaises(DataSourceUnavailable) as cm:
            gate.call(lambda: 1, data_type="equity_bars_pre2016", classify=lambda e, d: e)
        self.assertIn("allowance is used up", cm.exception.user_message)
        self.assertLess(ft.t - start, 10)

    def test_progress_callback_can_cancel(self):
        gate, _ft = fake_gate("tiingo", windows=((1, 3600.0),))
        gate.call(lambda: 1, data_type="equity_bars_pre2016", classify=lambda e, d: e)

        def cancel(_event):
            raise KeyboardInterrupt("cancelled")
        with self.assertRaises(KeyboardInterrupt):
            gate.call(lambda: 1, data_type="equity_bars_pre2016", classify=lambda e, d: e, progress=cancel)


class WiringTest(unittest.TestCase):
    def test_secret_registry_and_licence(self):
        self.assertEqual(secrets.SECRETS["tiingo-api-key"], "Tiingo API key")
        secrets.set_store(secrets.MemoryStore({}))
        self.addCleanup(secrets.set_store, None)
        entry = next(e for e in secrets.status() if e["name"] == "tiingo-api-key")
        self.assertEqual((entry["present"], entry["error"]), (False, None))
        self.assertNotIn("env_names", entry)                      # stage 10 S10-E: no .env fields
        self.assertEqual(registry.DEFAULT_SOURCES["equity_bars_pre2016"], "tiingo")
        self.assertEqual(licences.licence_for("tiingo")["licence"], "personal_use")
        with self.assertRaises(ValueError):
            registry.Registry(store=object()).test_source("tiingo")


if __name__ == "__main__":
    unittest.main()
