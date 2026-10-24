"""Binance error codes in user messages (live test 2026-10-23, SPEC §18.14): a ccxt AuthenticationError carrying
Binance's JSON body → "rejected the API keys (code -2015: … · likely: …)"; -1022 / -2014 hints; -1021 (ccxt
InvalidNonce, a NetworkError) → not retried, "PC clock out of sync"; the message form without JSON → the code from the
known text; URLs, query strings, signatures, IPs and the key are never shown or logged; ONE redacted log line per
failure; Test connection and a refused send show the code. Offline (fake ccxt exchange)."""
import json
import logging
import unittest

try:
    from sp_binance_testkit import AuthenticationError, NetworkError, make_binance
    from sp_broker_testkit import KEYS, BrokerCase
except ImportError:  # pragma: no cover
    from tests.sp_binance_testkit import AuthenticationError, NetworkError, make_binance
    from tests.sp_broker_testkit import KEYS, BrokerCase

from strategy_platform import secrets
from strategy_platform.execution.brokers import binance_demo, service
from strategy_platform.execution.brokers.common import BrokerAuthRejected, BrokerSetupError

LOGGER = "strategy_platform.execution.brokers.binance_demo"


class InvalidNonce(NetworkError):
    pass


def body(code, msg):
    return "binance " + json.dumps({"code": code, "msg": msg}, separators=(",", ":"))


class ErrorCodesTest(unittest.TestCase):
    def setUp(self):
        secrets.set_store(secrets.MemoryStore(dict(KEYS)))
        self.addCleanup(secrets.set_store, None)
        self.st, self.b = make_binance()
        self.ex = self.b.exchange()
        self.n = 0

    def failing(self, exc):
        def fn(params=None):
            self.n += 1
            raise exc
        self.ex.fetch_balance = fn

    def test_codes_and_hints(self):
        cases = ((-2015, "Invalid API-key, IP, or permissions for action, request ip: 203.0.113.7",
                  "keys from binance.com live or testnet.binance.vision do not work"),
                 (-1022, "Signature for this request is not valid.", "HMAC vs Ed25519/RSA key type"),
                 (-2014, "API-key format invalid.", "likely: key format"))
        for code, msg, hint in cases:
            self.failing(AuthenticationError(body(code, msg)))
            with self.assertLogs(LOGGER, logging.WARNING) as logs:
                with self.assertRaises(BrokerAuthRejected) as cm:
                    self.b.account()
            text = cm.exception.user_message
            self.assertIn(f"code {code}: {msg.split(',')[0]}", text)
            self.assertIn(hint, text)
            self.assertIn(f"code={code}", cm.exception.detail)
            self.assertEqual(len(logs.output), 1)
            self.assertIn(f"code={code}", logs.output[0])
            for secret in ("203.0.113.7", "BFAKE", "BSFAKE"):
                self.assertNotIn(secret, text)
                self.assertNotIn(secret, logs.output[0])
        self.assertEqual(binance_demo.sanitize(cases[0][1]),
                         "Invalid API-key, IP, or permissions for action, request ip: <ip>")

    def test_clock_skew_not_retried(self):
        self.failing(InvalidNonce(body(-1021, "Timestamp for this request was 1000ms ahead of the server's time.")))
        with self.assertRaises(BrokerSetupError) as cm:
            self.b.account()
        self.assertIn("code -1021", cm.exception.user_message)
        self.assertIn("PC clock out of sync", cm.exception.user_message)
        self.assertEqual(self.n, 1)                                    # waiting does not fix a clock

    def test_message_without_json_and_redaction(self):
        self.failing(AuthenticationError("binance Invalid API-key, IP, or permissions for action."))
        with self.assertRaises(BrokerAuthRejected) as cm:
            self.b.account()
        self.assertIn("code -2015", cm.exception.user_message)
        raw = ("binance GET https://demo-api.binance.com/api/v3/account?timestamp=1&signature=" + "ab" * 32 + " "
               + json.dumps({"code": -2015, "msg": "bad key BFAKE signature=" + "cd" * 32 + " see https://x.example/a?b=c "
                                                   "X-MBX-APIKEY: BFAKE token " + "Z" * 40}))
        code, msg = binance_demo.binance_error(AuthenticationError(raw), hide=("BFAKE", "BSFAKE"))
        self.assertEqual(code, -2015)
        for bad in ("BFAKE", "ab" * 32, "cd" * 32, "x.example", "b=c", "Z" * 40, "demo-api.binance.com"):
            self.assertNotIn(bad, msg)
        self.assertLessEqual(len(msg), 160)

    def test_refused_send_shows_the_code(self):
        def refuse(*a, **kw):
            raise AuthenticationError(body(-2015, "Invalid API-key, IP, or permissions for action."))
        self.ex.create_order = refuse
        a = self.b.submit({"client_order_id": "man-000000000001", "symbol": "BTC-USD", "side": "buy", "qty": 0.001,
                           "type": "market", "origin": "manual"})
        self.assertEqual(a["status"], "rejected")
        self.assertIn("code -2015", a["reject_reason"])
        self.assertIn("Enable Reading", a["reject_reason"])


class TestConnectionShowsCodeTest(BrokerCase):
    def test_account_check_message(self):
        ex = self.binance.exchange()

        def bad(params=None):
            ex._req("private", "account")
            raise AuthenticationError(body(-2015, "Invalid API-key, IP, or permissions for action, request ip: "
                                                  "198.51.100.4"))
        ex.fetch_balance = bad
        t = service.test("binance-demo")
        names = {c["name"]: c for c in t["checks"]}
        self.assertTrue(names["Server time"]["ok"] and names["Markets loaded (exchangeInfo)"]["ok"])
        msg = names["Account readable"]["message"]
        self.assertIn("code -2015: Invalid API-key, IP, or permissions for action", msg)
        self.assertIn("missing permission 'Enable Reading'/'Spot trading'", msg)
        self.assertNotIn("198.51.100.4", msg)
        self.assertTrue(names["Positions readable"]["message"].startswith("Skipped"))
        sent = [u for _m, u in self.bstate.sent if u.endswith("/account")]
        self.assertEqual(sent, ["https://demo-api.binance.com/api/v3/account"])   # the demo host, private API


if __name__ == "__main__":
    unittest.main()
