"""load_sdk imports alpaca-py once, under a lock (Python 3.14 import deadlock, 2026-10-03)."""
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from strategy_platform.data.providers import alpaca as prov


class LoadSdkLockTest(unittest.TestCase):
    def setUp(self):
        prov._SDK = None

    def tearDown(self):
        prov._SDK = None

    def test_concurrent_callers_import_once(self):
        calls = []
        gate = threading.Event()

        def fake_import(data_type):
            calls.append(data_type)
            gate.wait(0.2)
            return SimpleNamespace(marker=True)

        with mock.patch.object(prov, "_import_sdk", side_effect=fake_import):
            results = []
            threads = [threading.Thread(target=lambda: results.append(prov.load_sdk("crypto_quotes")))
                       for _ in range(8)]
            for t in threads:
                t.start()
            gate.set()
            for t in threads:
                t.join(5)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(results), 8)
        self.assertTrue(all(r is results[0] for r in results))

    def test_import_error_is_not_cached(self):
        with mock.patch.object(prov, "_import_sdk", side_effect=prov.DataSourceUnavailable("alpaca", "x", "missing")):
            with self.assertRaises(prov.DataSourceUnavailable):
                prov.load_sdk()
        self.assertIsNone(prov._SDK)


if __name__ == "__main__":
    unittest.main()
