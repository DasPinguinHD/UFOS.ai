"""strategy_platform/data/http_gate.py: token bucket (rate, burst,
thread safety) and bounded retries only for transient errors of the same
source. Offline, no real sleeping.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_data_gate -v
"""
import threading
import unittest

try:
    from test_sp_data_support import ConnectionError_, FakeAPIError, ReadTimeout, fake_gate
except ImportError:  # pragma: no cover - run as package
    from tests.test_sp_data_support import ConnectionError_, FakeAPIError, ReadTimeout, fake_gate

from strategy_platform.core.errors import DataSourceUnavailable, SecretMissing
from strategy_platform.data import http_gate
from strategy_platform.data.providers.alpaca import AUTH_MESSAGE, classify_error
from strategy_platform.data.providers.base import SourceAuthRejected


class TokenBucketTest(unittest.TestCase):
    def test_burst_then_rate(self):
        gate, ft = fake_gate(rate_per_minute=180, capacity=10)
        b = gate.bucket
        for _ in range(10):
            self.assertEqual(b.try_acquire(), 0.0)
        wait = b.try_acquire()
        self.assertAlmostEqual(wait, 1 / 3, places=6)            # 180/min = 3/s
        self.assertTrue(b.acquire(5.0))                           # sleeps 1/3 s (fake)
        self.assertAlmostEqual(sum(ft.sleeps), 1 / 3, places=6)

    def test_at_most_rate_plus_burst_per_minute(self):
        gate, ft = fake_gate(rate_per_minute=180, capacity=10)
        n = 0
        start = ft.t
        while ft.t - start < 60.0:
            if gate.bucket.acquire(1.0):
                n += 1
        self.assertLessEqual(n, 191)
        self.assertGreaterEqual(n, 189)

    def test_acquire_times_out(self):
        gate, _ft = fake_gate(rate_per_minute=1, capacity=1)
        self.assertTrue(gate.bucket.acquire(0.0))
        self.assertFalse(gate.bucket.acquire(0.5))

    def test_thread_safe(self):
        gate, _ft = fake_gate(rate_per_minute=60, capacity=10)   # time frozen: exactly 10 tokens
        got = []
        lock = threading.Lock()

        def worker():
            for _ in range(5):
                if gate.bucket.try_acquire() == 0.0:
                    with lock:
                        got.append(1)
        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(got), 10)

    def test_defaults(self):
        # 150 + burst 10 for the backend; the Options Structure archive has its own 30/min (SPEC §21.1, 2026-10-23)
        self.assertEqual(http_gate.DEFAULT_RATES_PER_MINUTE["alpaca"], 150)
        self.assertLessEqual(http_gate.DEFAULT_RATES_PER_MINUTE["alpaca"] + http_gate.DEFAULT_CAPACITY + 30, 190)
        http_gate.set_gate("alpaca", None)
        g = http_gate.gate_for("alpaca")
        self.assertIs(g, http_gate.gate_for("alpaca"))
        self.assertEqual(g.bucket.rate_per_minute, 150)
        http_gate.set_gate("alpaca", None)


class RetryTest(unittest.TestCase):
    def run_gate(self, answers, **kw):
        gate, ft = fake_gate(**kw)
        seq = list(answers)
        calls = []

        def fn():
            calls.append(1)
            item = seq.pop(0) if len(seq) > 1 else seq[0]
            if isinstance(item, BaseException):
                raise item
            return item
        return gate, ft, calls, (lambda: gate.call(fn, data_type="equity_bars", classify=classify_error))

    def test_429_retried_then_ok(self):
        gate, ft, calls, go = self.run_gate([FakeAPIError(429), FakeAPIError(503), "data"])
        self.assertEqual(go(), "data")
        self.assertEqual(len(calls), 3)
        self.assertEqual(ft.sleeps[:2], [1.0, 3.0])
        self.assertEqual(gate.retries, 2)

    def test_429_exhausted_raises_unavailable(self):
        _g, _ft, calls, go = self.run_gate([FakeAPIError(429)])
        with self.assertRaises(DataSourceUnavailable) as cm:
            go()
        self.assertEqual(len(calls), 3)                           # bounded
        self.assertIn("HTTP 429", cm.exception.user_message)
        self.assertIn("gave up after 3 attempts", cm.exception.user_message)
        self.assertIn("no other source is used", cm.exception.user_message)

    def test_retry_after_is_honoured_but_capped(self):
        _g, ft, _calls, go = self.run_gate([FakeAPIError(429, headers={"Retry-After": "7"}), "ok"])
        self.assertEqual(go(), "ok")
        self.assertIn(7.0, ft.sleeps)
        _g, ft, _calls, go = self.run_gate([FakeAPIError(429, headers={"Retry-After": "600"}), "ok"])
        go()
        self.assertIn(http_gate.MAX_RETRY_AFTER_SECONDS, ft.sleeps)

    def test_auth_error_not_retried(self):
        _g, _ft, calls, go = self.run_gate([FakeAPIError(401, "unauthorized")])
        with self.assertRaises(SourceAuthRejected) as cm:
            go()
        self.assertEqual(len(calls), 1)
        self.assertEqual(cm.exception.user_message, AUTH_MESSAGE)
        self.assertIn("Settings → Keys", str(cm.exception))

    def test_timeout_retried_network_error_not(self):
        _g, _ft, calls, go = self.run_gate([ReadTimeout("read timed out"), "ok"])
        self.assertEqual(go(), "ok")
        self.assertEqual(len(calls), 2)
        _g, _ft, calls, go = self.run_gate([ConnectionError_("dns")])
        with self.assertRaises(DataSourceUnavailable) as cm:
            go()
        self.assertEqual(len(calls), 1)
        self.assertIn("could not be reached", cm.exception.user_message)

    def test_other_4xx_not_retried(self):
        _g, _ft, calls, go = self.run_gate([FakeAPIError(422, "invalid symbol")])
        with self.assertRaises(DataSourceUnavailable) as cm:
            go()
        self.assertEqual(len(calls), 1)
        self.assertIn("HTTP 422: invalid symbol", cm.exception.user_message)

    def test_user_facing_error_passes_through(self):
        _g, _ft, calls, go = self.run_gate([SecretMissing("alpaca-paper-key-id", "Alpaca paper API key ID")])
        with self.assertRaises(SecretMissing):
            go()
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
