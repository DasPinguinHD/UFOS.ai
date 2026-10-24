"""Fix 2026-10-18 (SPEC §18.13), generic part: a broker answer in an unexpected format is described by a SAFE shape
(type, top-level keys or length, first character — never values); format / setup errors are not connection
problems: link "error" with retry "manual", no "waiting for broker", no automatic retries. (The Kraken /accounts
parsing tests went with the Kraken demo adapter, stage 10 S10-K; binance-demo: test_sp_broker_binance.) Offline."""
import unittest

from strategy_platform.execution.brokers.common import Link, shape_of


class ShapeTest(unittest.TestCase):
    def test_shape_of_is_safe(self):
        self.assertEqual(shape_of({"a b": 1, "c": {"x": "secret"}}), "dict, keys: a?b, c")
        self.assertEqual(shape_of(""), "str, length 0, starts with other (empty)")
        self.assertEqual(shape_of(b"abc"), "bytes, length 3, starts with letter/digit")
        self.assertEqual(shape_of(None), "NoneType")


class NotRetryableLinkTest(unittest.TestCase):
    def test_link_manual_only(self):
        lk = Link("x")
        lk.fail("manual", "The broker answered in an unexpected format — not a connection problem: …")
        v = lk.view()
        self.assertEqual((v["state"], v["retry"], v["next_retry_at"], v["attempt"]), ("error", "manual", None, 0))
        self.assertNotIn("Waiting", v["message"])
        self.assertIn("not retried automatically", lk.blocked())
        lk.ok()
        self.assertIsNone(lk.blocked())


if __name__ == "__main__":
    unittest.main()
