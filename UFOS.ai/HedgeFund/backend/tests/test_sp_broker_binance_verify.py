"""Stage 8b verify tool (tools/verify_binance_demo.py, SPEC §18.14 item 11) on the fake ccxt exchange (offline):
read-only by default (no POST / DELETE), the URL guard self-test, keys missing → exit 2 and never printed, the
optional round trip only with --place-test-order (UFOSV_ id, placed far below the market, cancelled, confirmed),
refused (exit 4, nothing sent) while platform orders work or that cannot be checked."""
import os
import sys
import unittest

try:
    from sp_binance_testkit import make_binance
    from sp_broker_testkit import KEYS
except ImportError:  # pragma: no cover
    from tests.sp_binance_testkit import make_binance
    from tests.sp_broker_testkit import KEYS

from strategy_platform import secrets

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import verify_binance_demo as tool  # noqa: E402


class VerifyToolTest(unittest.TestCase):
    def setUp(self):
        self.store = secrets.MemoryStore(dict(KEYS))
        secrets.set_store(self.store)
        self.addCleanup(secrets.set_store, None)
        self.st, self.b = make_binance()
        self.lines = []

    def run_tool(self, argv=(), working=lambda: 0):
        return tool.run(list(argv), adapter=self.b, out=self.lines.append, sleep=lambda s: None,
                        secrets_module=secrets, working_fn=working)

    def test_read_only_default(self):
        self.assertEqual(self.run_tool(), 0, self.lines)
        self.assertEqual(self.lines[-1], "RESULT: OK")
        self.assertEqual({m for m, _u in self.st.sent}, {"GET"})
        self.assertTrue(any(l.startswith("OK 3 URL guard") for l in self.lines))
        self.assertTrue(any("6 balances: BNB 2, BTC 0.05, ETH 1, USDT 5000" in l for l in self.lines), self.lines)
        self.assertEqual(self.st.orders, {})
        text = "\n".join(self.lines)
        self.assertNotIn("BFAKE", text)
        self.assertNotIn("BSFAKE", text)

    def test_round_trip_only_with_flag(self):
        self.assertEqual(self.run_tool(["--place-test-order"]), 0, self.lines)
        (o,) = self.st.orders.values()
        self.assertTrue(o["clientOrderId"].startswith("UFOSV_"))
        self.assertFalse(o["clientOrderId"].startswith("UFOS_"))
        self.assertEqual((o["type"], o["side"], o["status"], o["filled"]), ("limit", "buy", "canceled", 0.0))
        self.assertLessEqual(o["price"], 30000.0)
        self.assertGreaterEqual(o["price"] * o["amount"], 5.0)
        self.assertTrue(any(l.startswith("OK 10 cancelled") for l in self.lines))

    def test_round_trip_refused(self):
        for working in (lambda: 2, lambda: None):
            self.lines.clear()
            self.assertEqual(self.run_tool(["--place-test-order"], working), 4, self.lines)
        self.assertEqual(self.st.orders, {})
        self.assertEqual({m for m, _u in self.st.sent}, {"GET"})

    def test_keys_missing_and_bad_args(self):
        self.assertEqual(self.run_tool(["--live"]), 1)
        self.store.values.pop(secrets.target("binance-demo-api-secret"))
        self.lines.clear()
        self.assertEqual(self.run_tool(), 2)
        self.assertIn("missing binance-demo-api-secret", self.lines[0])
        self.assertEqual(self.st.sent, [])


if __name__ == "__main__":
    unittest.main()
