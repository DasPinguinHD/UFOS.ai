"""Stage 8b Binance Spot demo adapter (SPEC §18.14) against a fake ccxt exchange (no network): demo mode only
(enable_demo_trading, never sandbox), the URL guard (spot APIs cut to the demo host, every request asserted, sapi /
futures impossible), keys (HMAC only, missing, rejected — never in messages), symbols / USDT, Binance filters named
before sending, long only, the UFOS_ client-id prefix (lookups, prefix-limited cancel_all), fees (quote / base net /
BNB not booked), retries and ambiguous sends (looked up, never re-sent blindly), Kraken-style diagnostics."""
import json
import os
import re
import unittest

try:
    from sp_binance_testkit import State, make_binance
    from sp_broker_testkit import KEYS
except ImportError:  # pragma: no cover
    from tests.sp_binance_testkit import State, make_binance
    from tests.sp_broker_testkit import KEYS

from strategy_platform import secrets
from strategy_platform.core.errors import SecretMissing
from strategy_platform.execution.brokers import binance_demo, live_guard
from strategy_platform.execution.brokers.common import BrokerAuthRejected, BrokerFormatError, BrokerSetupError
from strategy_platform.execution.errors import BrokerUnavailable

BROKERS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "strategy_platform",
                           "execution")


def order(cid="run-000000000001", symbol="BTC-USD", side="buy", qty=0.001, **kw):
    o = {"client_order_id": cid, "symbol": symbol, "side": side, "qty": qty, "origin": "manual", "type": "market"}
    o.update(kw)
    return o


class Base(unittest.TestCase):
    def setUp(self):
        self.store = secrets.MemoryStore(dict(KEYS))
        secrets.set_store(self.store)
        self.addCleanup(secrets.set_store, None)
        self.symbols = {}
        self.st, self.b = make_binance(symbol_lookup=lambda cid: self.symbols.get(cid))

    def hosts(self):
        return {re.sub(r"^https://([^/]+)/.*$", r"\1", u) for _m, u in self.st.sent}


class EndpointTest(Base):
    def test_demo_mode_spot_only(self):
        ex = self.b.exchange()
        self.assertTrue(ex.options["enableDemoTrading"])
        self.assertFalse(ex.isSandboxModeEnabled)
        self.assertEqual(set(ex.urls["api"]), {"public", "private", "v1"})       # sapi / futures cut away
        self.assertEqual({u.split("/")[2] for u in ex.urls["api"].values()}, {"demo-api.binance.com"})
        self.assertEqual(ex.options["defaultType"], "spot")
        self.assertEqual(ex.options["fetchMarkets"], {"types": ["spot"]})
        self.assertEqual(self.b.check_endpoint(), "https://demo-api.binance.com")
        self.assertEqual(self.b.usdt_markets(), 3)                     # BTC, ETH, SOL /USDT spot (not USDC, not perp)
        self.b.account()
        self.assertEqual(self.hosts(), {"demo-api.binance.com"})

    def test_every_request_is_asserted(self):
        ex = self.b.exchange()
        ex.urls["api"]["private"] = "https://api.binance.com/api/v3"            # changed after construction
        n = len(self.st.sent)
        with self.assertRaises(live_guard.LiveTradingDisabled) as cm:
            ex.fetch_balance()
        self.assertEqual(cm.exception.user_message, live_guard.LIVE_DISABLED)
        self.assertEqual(len(self.st.sent), n)                                  # nothing was sent
        with self.assertRaises(Exception) as cm2:                               # sapi: no URL at all (ccxt sign)
            ex._req("sapi", "capital/config/getall")
        self.assertEqual(type(cm2.exception).__name__, "NotSupported")

    def test_sandbox_or_missing_demo_urls_refused(self):
        st, b = make_binance()
        cls = b._sdk.binance

        class Sandboxed(cls):
            def __init__(self, cfg):
                super().__init__(cfg)
                self.isSandboxModeEnabled = True
        b._sdk.binance = Sandboxed
        with self.assertRaises(live_guard.LiveTradingDisabled):
            b.exchange()

        class NoDemo(cls):
            def enable_demo_trading(self, enable):
                pass                                                            # stays on the live URLs
        b._sdk.binance = NoDemo
        with self.assertRaises(live_guard.LiveTradingDisabled):
            b.exchange()
        self.assertEqual(st.sent, [])

    def test_guard_hosts(self):
        ok = ("https://demo-api.binance.com/api/v3/account", "wss://demo-ws-api.binance.com/ws-api/v3",
              "wss://demo-stream.binance.com/ws")
        for u in ok:
            live_guard.assert_paper_url("binance_demo", u)
        for u in ("https://api.binance.com/api/v3/account", "https://testnet.binance.vision/api/v3/account",
                  "https://demo-fapi.binance.com/fapi/v1/order", "http://demo-api.binance.com/api/v3/time",
                  "wss://stream.binance.com/ws", "https://demo-api.binance.com.evil.example/api",
                  "wss://demo-api.binance.com/ws"):
            with self.assertRaises(live_guard.LiveTradingDisabled):
                live_guard.assert_paper_url("binance_demo", u)

    def test_source_scan_has_no_live_binance_host(self):
        # live hosts only (testnet.binance.vision is named in a user hint, never used — the guard allows the demo only)
        pat = re.compile(r"(?<![\w.-])(api|stream|ws-api)\.binance\.com")
        hits = []
        for root, _d, files in os.walk(BROKERS_DIR):
            for f in files:
                if f.endswith(".py"):
                    with open(os.path.join(root, f), encoding="utf-8") as fh:
                        for n, line in enumerate(fh, 1):
                            if pat.search(line) and "LIVE_HOSTS" not in line:
                                hits.append((f, n))
        self.assertEqual(hits, [])


class KeysTest(Base):
    def test_hmac_only_missing_and_rejected(self):
        self.store.values[secrets.target("binance-demo-api-secret")] = ("-----BEGIN PRIVATE KEY-----\nMC4CAQ\n"
                                                                        "-----END PRIVATE KEY-----")
        with self.assertRaises(BrokerSetupError) as cm:
            self.b.account()
        self.assertIn("HMAC keys only", cm.exception.user_message)
        self.assertNotIn("MC4CAQ", cm.exception.user_message)
        self.assertEqual(self.st.sent, [])
        self.store.values.pop(secrets.target("binance-demo-api-secret"))
        with self.assertRaises(SecretMissing):
            self.b.account()
        self.store.values[secrets.target("binance-demo-api-secret")] = "BSFAKE"
        self.st.auth_bad = True
        with self.assertRaises(BrokerAuthRejected) as cm:
            self.b.account()
        self.assertNotIn("BSFAKE", cm.exception.user_message)
        self.assertNotIn("BFAKE", cm.exception.user_message)
        a = self.b.submit(order())
        self.assertEqual(a["status"], "rejected")
        self.assertIn("rejected the API keys", a["reject_reason"])


class SymbolsAndAccountTest(Base):
    def test_mapping_and_usdt(self):
        self.assertEqual((binance_demo.ccxt_symbol("BTC-USD"), binance_demo.binance_id("ETH-USD")),
                         ("BTC/USDT", "ETHUSDT"))
        self.assertIsNone(binance_demo.platform_symbol("BTC/USDC"))
        self.assertIsNone(binance_demo.platform_symbol("BTC/USDT:USDT"))
        with self.assertRaises(ValueError):
            binance_demo.ccxt_symbol("SPY")
        a = self.b.account()
        self.assertEqual((a["currency"], a["currency_note"], a["cash_basis"], a["equity"]),
                         ("USDT", "USDT — not converted", 5000.0, None))
        self.assertEqual([x["asset"] for x in a["balances"]], ["BNB", "BTC", "ETH", "USDT"])
        self.assertEqual(self.b.positions(), [])                       # no baseline yet: nothing is the platform's
        i = self.b.instrument("BTC-USD")
        self.assertEqual((i["broker_symbol"], i["quote"], i["shortable"], i["min_notional"], i["qty_step"]),
                         ("BTCUSDT", "USDT", False, 5.0, 0.00001))
        self.assertFalse(self.b.instrument("DOGE-USD")["tradable"])

    def test_positions_are_baseline_deltas(self):
        self.b.bind_baseline(lambda: {"USDT": 5000.0, "BTC": 0.05, "ETH": 1.0, "BNB": 2.0})
        self.assertEqual(self.b.positions(), [])
        self.st.bal("BTC")[0] += 0.002
        self.st.bal("ETH")[0] -= 0.5
        self.st.bal("BNB")[0] -= 0.01
        pos = {p["symbol"]: p for p in self.b.positions()}
        self.assertAlmostEqual(pos["BTC-USD"]["qty"], 0.002)
        self.assertIsNone(pos["BTC-USD"]["kill_skip"])
        self.assertIn("below the starting balance", pos["ETH-USD"]["kill_skip"])
        self.assertIn("BNB", pos["BNB-USD"]["kill_skip"] or "")
        self.st.bal("BTC")[0] -= 0.00199                              # 0.00001 BTC left: dust (NOTIONAL 5 USDT)
        p = next(x for x in self.b.positions() if x["symbol"] == "BTC-USD")
        self.assertIn("dust", p["kill_skip"])


class OrdersTest(Base):
    def test_market_buy_fee_in_base_booked_net(self):
        a = self.b.submit(order(qty=0.0012345))                       # rounded DOWN to the LOT_SIZE step
        self.assertEqual(a["status"], "filled")
        o = next(iter(self.st.orders.values()))
        self.assertEqual((o["clientOrderId"], o["amount"]), ("UFOS_run-000000000001", 0.00123))
        self.assertAlmostEqual(a["filled_qty"], 0.00123)               # the order is filled …
        self.assertAlmostEqual(a["fee_base_qty"], 0.00123 * 0.001)     # … the commission was taken from the BTC
        self.assertAlmostEqual(a["fees"], 0.00123 * 0.001 * 60000.0)
        self.assertEqual((a["client_order_id"], a["currency"]), ("run-000000000001", "USDT"))
        self.symbols["run-000000000001"] = "BTC-USD"
        again = self.b.submit(order(qty=0.0012345))                    # idempotent: looked up, not re-sent
        self.assertEqual(again["broker_order_id"], a["broker_order_id"])
        self.assertEqual(len(self.st.orders), 1)

    def test_fee_in_quote_and_bnb(self):
        self.st.buy_fee_asset = "quote"
        a = self.b.submit(order(qty=0.001))
        self.assertAlmostEqual(a["filled_qty"], 0.001)
        self.assertAlmostEqual(a["fees"], 0.06)
        self.st.buy_fee_asset = "BNB"
        a = self.b.submit(order(cid="run-000000000002", qty=0.001))
        self.assertEqual(a["fees"], 0.0)
        self.assertIn("BNB", a["fee_note"])

    def test_filters_named_before_sending(self):
        cases = [(order(qty=0.000001), "LOT_SIZE"), (order(qty=60.0), "MARKET_LOT_SIZE"),
                 (order(qty=0.00005), "NOTIONAL"),
                 (order(type="limit", limit_price=50000.005, qty=0.001, tif="gtc"), "PRICE_FILTER"),
                 (order(type="limit", limit_price=0.001, qty=1.0, tif="gtc"), "PRICE_FILTER")]
        for o, name in cases:
            a = self.b.submit(o)
            self.assertEqual(a["status"], "rejected", o)
            self.assertTrue(a["reject_reason"].startswith(name), a["reject_reason"])
        self.assertEqual(self.st.orders, {})
        self.st.prices["BTC/USDT"] = None                              # no price: refused, no stand-in
        self.b._prices.clear()                                         # (the last price is cached 60 s)
        a = self.b.submit(order(qty=0.001))
        self.assertIn("NOTIONAL: the demo's price", a["reject_reason"])
        self.assertEqual(self.st.orders, {})

    def test_long_only_and_shapes(self):
        for o in (order(side="short"), order(side="cover"), order(symbol="SPY"), order(type="stop", stop_price=1.0),
                  order(type="limit", qty=0.001), order(qty=None, notional=100.0),
                  order(cid="x" * 40)):
            a = self.b.submit(o)
            self.assertEqual(a["status"], "rejected", o)
        self.assertIn("long only", self.b.submit(order(side="short"))["reject_reason"])
        self.assertEqual(self.st.orders, {})
        self.assertFalse(binance_demo.CAPABILITIES["shorting"])

    def test_limit_rests_then_cancel_and_prefix_limited_cancel_all(self):
        a = self.b.submit(order(type="limit", limit_price=50000.0, qty=0.001, tif="gtc"))
        self.assertEqual(a["status"], "accepted")
        self.st.add_foreign()
        oo = self.b.open_orders()
        self.assertEqual([o["client_order_id"] for o in oo], ["run-000000000001"])
        self.assertEqual(self.b.last_foreign_open, 1)
        self.assertEqual(self.b.cancel_all(), 1)
        self.assertEqual(self.b.last_untouched, 1)
        st = {o["clientOrderId"]: o["status"] for o in self.st.orders.values()}
        self.assertEqual(st, {"UFOS_run-000000000001": "canceled", "web_abc123": "open"})
        self.assertAlmostEqual(self.st.bal("USDT")[1], 0.001 * 50000.0)  # only the foreign lock remains
        self.symbols["run-000000000001"] = "BTC-USD"
        self.assertFalse(self.b.cancel("run-000000000001"))           # already cancelled

    def test_lost_answer_looked_up_never_resent(self):
        self.st.drop_after_send = 1
        a = self.b.submit(order(qty=0.001))
        self.assertEqual(a["status"], "filled")                       # found by origClientOrderId
        self.assertEqual(len(self.st.orders), 1)
        self.st.down = True
        with self.assertRaises(BrokerUnavailable):
            self.b.submit(order(cid="run-000000000009", qty=0.001))
        self.assertEqual(len(self.st.orders), 1)

    def test_transient_reads_retried(self):
        n = {"k": 0}
        ex = self.b.exchange()
        orig = ex.fetch_balance

        def flaky(params=None):
            n["k"] += 1
            if n["k"] == 1:
                raise type("RequestTimeout", (Exception,), {})("timed out")
            return orig(params)
        ex.fetch_balance = flaky
        self.assertEqual(self.b.account()["cash_basis"], 5000.0)
        self.assertEqual(n["k"], 2)


class DiagnosticsTest(Base):
    def test_html_answer_is_a_format_error_with_metadata(self):
        ex = self.b.exchange()

        class Resp:
            status_code, url, history = 200, "https://demo-api.binance.com/en", [object()]
            headers = {"Content-Type": "text/html; charset=utf-8", "Server": "cloudflare"}

        class Session:
            def request(self, *a, **kw):
                return Resp()
        ex.session = Session()
        ex.fetch = type(ex).fetch.__get__(ex)                           # re-harden with a session
        from strategy_platform.execution.brokers import ccxt_support

        def raw_fetch(url, method="GET", headers=None, body=None):
            ex.session.request(method, url)
            return "<!doctype html><html>captcha id=SECRET</html>"
        ex.fetch = raw_fetch
        ccxt_support.harden_exchange(ex, "binance_demo", binance_demo.FORMAT_MSG, "Binance demo")
        with self.assertRaises(BrokerFormatError) as cm:
            self.b.balances()
        msg = cm.exception.user_message
        self.assertIn("unexpected response shape: str", msg)
        self.assertIn("HTTP 200, text/html, server cloudflare, redirected 1× to demo-api.binance.com/en", msg)
        self.assertIn("captcha", msg)
        self.assertNotIn("SECRET", msg)
        self.assertLessEqual(len(msg), 300)

    def test_bad_balance_shape(self):
        ex = self.b.exchange()
        ex.fetch_balance = lambda params=None: ["not", "a", "balance"]
        with self.assertRaises(BrokerFormatError) as cm:
            self.b.account()
        self.assertIn("list, length 3", cm.exception.user_message)


if __name__ == "__main__":
    unittest.main()
