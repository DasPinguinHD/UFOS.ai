"""strategy_platform/data/registry.py + licences.py on a temp
hedgefund.db: one configured source per data type (seeded with licence),
status after every success/failure with the user message, test_source
checks (ok, missing keys -> remaining checks skipped, 401), no fallback.
Offline, fake alpaca SDK.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_data_registry -v
"""
import unittest

try:
    from test_sp_data_support import KEYS, FakeAPIError, Script, TempDb, fake_gate, fake_sdk, utc
except ImportError:  # pragma: no cover - run as package
    from tests.test_sp_data_support import KEYS, FakeAPIError, Script, TempDb, fake_gate, fake_sdk, utc

from strategy_platform import secrets
from strategy_platform.core.errors import DataSourceUnavailable
from strategy_platform.data import licences, registry
from strategy_platform.data.providers.alpaca import AUTH_MESSAGE, AlpacaProvider
from strategy_platform.data.providers.base import HistoryNotAvailable

NOW = utc(2026, 10, 2, 21, 0)
SPY = {"SPY": [{"t": "2026-10-01T04:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 1, "n": 1, "vw": 1}]}
BTC = {"BTC/USD": [{"t": "2026-10-01T00:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 1, "n": 1, "vw": 1}]}
QUOTE = {"SPY": {"ap": 2.0, "as": 1, "bp": 1.9, "bs": 1, "t": "2026-10-02T19:59:59Z"}}


class RegistryCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDb()
        self.db = self._tmp.__enter__()
        secrets.set_store(secrets.MemoryStore(KEYS))
        self.script = Script()
        self.gate, _ft = fake_gate()
        self.reg = registry.Registry(providers={"alpaca": lambda: AlpacaProvider(
            gate=self.gate, sdk=fake_sdk(self.script), clock=lambda: NOW)}, clock=lambda: NOW)

    def tearDown(self):
        secrets.set_store(None)
        self._tmp.__exit__(None, None, None)


class ConfigTest(RegistryCase):
    def test_defaults_seeded_with_licence(self):
        self.assertEqual(self.reg.configured_source("equity_bars"), "alpaca")
        row = self.db.data_source_get("equity_bars")
        self.assertEqual((row["source"], row["licence"]), ("alpaca", "personal_use"))
        self.assertEqual(self.reg.configured_source("macro"), "fred")
        self.assertEqual(self.reg.configured_source("insider"), "sec_edgar")
        self.assertEqual(self.db.data_source_get("macro")["licence"], "unknown")

    def test_one_source_per_type(self):
        with self.assertRaises(ValueError):
            self.reg.set_source("equity_bars", "yfinance")
        with self.assertRaises(ValueError):
            self.reg.configured_source("options")
        self.assertEqual(self.reg.set_source("news", "alpaca")["source"], "alpaca")
        with self.assertRaises(ValueError):                    # served by the newsroom collector
            self.reg.get_provider("macro")
        self.assertIsInstance(self.reg.get_provider("crypto_quotes"), AlpacaProvider)

    def test_sources_listing(self):
        out = self.reg.sources()
        self.assertEqual([s["data_type"] for s in out], list(registry.DEFAULT_SOURCES))
        self.assertEqual(out[0]["licence"]["terms"], "personal_use")
        self.assertEqual(len(self.reg.ensure_configured()), len(registry.DEFAULT_SOURCES))
        self.assertEqual(len(self.db.data_sources_list()), len(registry.DEFAULT_SOURCES))
        self.assertEqual(set(out[0]), {"data_type", "source", "status", "last_ok_at", "last_error_at", "message",
                                       "licence"})

    def test_licences(self):
        self.assertEqual(licences.licence_for("alpaca")["licence"], "personal_use")
        self.assertEqual(licences.licence_for("sec_edgar")["licence"], "commercial_ok")
        self.assertEqual(licences.licence_for("nope")["licence"], "unknown")
        self.assertTrue(all(v["licence"] in licences.TERMS for v in licences.LICENCES.values()))


class FetchStatusTest(RegistryCase):
    def test_status_after_success_and_failure(self):
        self.script.set("get_stock_bars", SPY, FakeAPIError(401))
        self.reg.fetch("equity_bars", "get_bars", "SPY", "1Day", utc(2026, 9, 25), NOW)
        row = self.db.data_source_get("equity_bars")
        self.assertEqual((row["status"], row["last_ok_at"], row["message"]), ("ok", "2026-10-02T21:00:00+00:00", None))
        with self.assertRaises(DataSourceUnavailable):
            self.reg.fetch("equity_bars", "get_bars", "SPY", "1Day", utc(2026, 9, 25), NOW)
        row = self.db.data_source_get("equity_bars")
        self.assertEqual((row["status"], row["message"]), ("error", AUTH_MESSAGE))
        self.assertEqual(row["last_ok_at"], "2026-10-02T21:00:00+00:00")       # kept

    def test_request_errors_leave_status_alone(self):
        with self.assertRaises(HistoryNotAvailable):
            self.reg.fetch("equity_bars", "get_bars", "SPY", "1Day", utc(2010, 1, 1), NOW)
        self.assertIsNone(self.db.data_source_get("equity_bars")["status"])

    def test_unexpected_exception_becomes_user_facing(self):
        class Broken:
            data_types = ("news",)

            def news(self, *a):
                raise RuntimeError("boom")
        reg = registry.Registry(providers={"alpaca": Broken}, clock=lambda: NOW)
        with self.assertRaises(DataSourceUnavailable) as cm:
            reg.fetch("news", "news", ["SPY"])
        self.assertIn("unexpected error (RuntimeError)", cm.exception.user_message)
        self.assertEqual(self.db.data_source_get("news")["status"], "error")


class TestSourceTest(RegistryCase):
    def test_all_checks_ok(self):
        self.script.set("get_stock_bars", SPY)
        self.script.set("get_crypto_bars", BTC)
        self.script.set("get_stock_latest_quote", QUOTE)
        out = self.reg.test_source("alpaca")
        self.assertTrue(out["ok"], out)
        self.assertEqual([c["name"] for c in out["checks"]],
                         ["1 daily bar SPY", "1 daily bar BTC/USD", "Latest SPY quote"])
        self.assertIn("SPY: daily bar 2026-10-01", out["checks"][0]["message"])
        self.assertEqual(self.db.data_source_get("crypto_bars")["status"], "ok")

    def test_missing_keys_skip_the_rest(self):
        secrets.set_store(secrets.MemoryStore({}))
        out = self.reg.test_source("alpaca")
        self.assertFalse(out["ok"])
        self.assertIn("is not set — enter it in Hedge Fund → Settings → Keys", out["checks"][0]["message"])
        self.assertTrue(all(c["message"].startswith("Skipped — ") for c in out["checks"][1:]))
        self.assertEqual(self.script.calls, [])

    def test_rejected_keys_and_partial_failure(self):
        self.script.set("get_stock_bars", FakeAPIError(403, "forbidden"))
        out = self.reg.test_source("alpaca")
        self.assertEqual(out["checks"][0]["message"], AUTH_MESSAGE)
        self.assertEqual(len(self.script.calls), 1)
        self.script.calls.clear()
        self.script.set("get_stock_bars", {"SPY": []})                         # no bar
        self.script.set("get_crypto_bars", BTC)
        self.script.set("get_stock_latest_quote", QUOTE)
        out = self.reg.test_source("alpaca")
        self.assertEqual([c["ok"] for c in out["checks"]], [False, True, True])
        self.assertIn("no daily bar for SPY", out["checks"][0]["message"])

    def test_unknown_source(self):
        with self.assertRaises(ValueError):
            self.reg.test_source("yfinance")


if __name__ == "__main__":
    unittest.main()
