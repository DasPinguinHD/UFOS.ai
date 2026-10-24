"""Market Research MR2a review fixes M3/L6 (SPEC §7.5): Research never delays platform requests beyond one
in-flight request — concurrency slots (at most max(1, limit − 1) for Research; limit 1 → only while no
normal request is active; yields to normal slot waiters), the bar cache's series lock (a waiting research
holder gives way, ``ResearchYield``), and the 60/min cap counts actual HTTP sends (alpaca-py pages).

Run from HedgeFund/backend:  python -m unittest tests.test_mr_data_priority -v
"""
import threading
import time
import unittest
from datetime import datetime, timezone

try:
    from test_sp_data_support import FakeBarProvider, TempDb, equity_row
    from test_mr_data_gate import FakeTime, call, gate, max_in_window
except ImportError:  # pragma: no cover - run as package
    from tests.test_sp_data_support import FakeBarProvider, TempDb, equity_row
    from tests.test_mr_data_gate import FakeTime, call, gate, max_in_window

from strategy_platform.core.clock import FixedClock
from strategy_platform.core.errors import DataSourceUnavailable
from strategy_platform.data import http_gate, network
from strategy_platform.data.cache import BarCache
from strategy_platform.data.providers.alpaca import classify_error, harden_client


class SlotRuleTest(unittest.TestCase):
    def test_research_leaves_one_slot(self):
        lim = network.AdaptiveLimiter("alpaca", adaptive=False, fixed=2)
        g, _ft = gate(concurrency=lim)
        g.research_timeout = 2.0
        self.assertTrue(lim.try_acquire())                 # one research request in flight …
        g._research_in_flight = 1
        with http_gate.research_priority():
            with self.assertRaises(DataSourceUnavailable):  # … a second one may not take the last slot
                call(g)
        self.assertEqual(call(g, lambda: lim.in_flight), 2)  # a normal request gets it at once
        lim.release()
        g._research_in_flight = 0
        with http_gate.research_priority():
            self.assertEqual(call(g, lambda: g._research_in_flight), 1)

    def test_limit_one_waits_for_active_normal_request(self):
        lim = network.AdaptiveLimiter("alpaca", adaptive=False, fixed=1)
        g, ft = gate(concurrency=lim)
        self.assertTrue(lim.try_acquire())                 # a normal request is in flight
        steps = []

        def hook(_s):
            steps.append(lim.in_flight)
            if len(steps) == 8:
                lim.release()                               # … and finishes
        ft.hook = hook
        with http_gate.research_priority():
            self.assertEqual(call(g, lambda: lim.in_flight), 1)
        self.assertEqual(steps[:8], [1] * 8)
        self.assertEqual(lim.in_flight, 0)

    def test_yields_to_normal_slot_waiter(self):
        lim = network.AdaptiveLimiter("alpaca", adaptive=False, fixed=4)
        g, _ft = gate(concurrency=lim)
        g._normal_slot_waiting = 1
        g.research_timeout = 1.0
        with http_gate.research_priority():
            with self.assertRaises(DataSourceUnavailable):
                call(g)
        self.assertEqual(lim.peak, 0)


class SendsTest(unittest.TestCase):
    def test_pages_count_and_cap_holds_on_sends(self):
        g, ft = gate()
        sends = []

        def three_pages():
            for _ in range(3):
                g.note_send()
                sends.append(ft.t)
            return 1
        with http_gate.research_priority():
            for _ in range(50):
                call(g, three_pages)
        self.assertEqual(len(sends), 150)
        self.assertEqual(g.research_calls, 150)
        self.assertLessEqual(max_in_window(sends, 60.0), 60)

    def test_normal_pages_unchanged(self):
        g, ft = gate()
        before = g.bucket._tokens
        call(g, lambda: [g.note_send() for _ in range(3)])
        self.assertAlmostEqual(g.bucket._tokens, before - 1, places=3)
        self.assertEqual(g.research_calls, 0)

    def test_harden_client_reports_every_request(self):
        seen = []

        class Session:
            def request(self, method, url, **kw):
                return (method, kw["timeout"])
        client = type("C", (), {})()
        client._retry, client._session = 3, Session()
        harden_client(client, 5.0, on_send=lambda: seen.append(1))
        client._session.request("GET", "u")
        client._session.request("GET", "u")
        self.assertEqual((seen, client._retry), ([1, 1], 0))

    def test_yield_check_raises_research_yield(self):
        g, _ft = gate()
        g._normal_waiting = 1
        with http_gate.research_priority(), http_gate.research_yield_check(lambda: True):
            with self.assertRaises(http_gate.ResearchYield) as cm:
                call(g)
        self.assertTrue(cm.exception.is_cancellation)
        self.assertEqual(g.research_stats()["used_last_minute"], 0)


class CacheLockTest(unittest.TestCase):
    def test_platform_request_not_blocked_by_waiting_research(self):
        """A research cache fill waits in the gate (platform busy) while holding the SPY lock; a platform
        request for SPY must get through at once (the research holder gives way and retries)."""
        g = http_gate.HttpGate("alpaca")
        g.research_timeout = 20.0
        g._normal_waiting = 1                                  # the platform is busy: Research must wait
        prov = FakeBarProvider()
        prov.bars["SPY"] = [equity_row(d, 400.0 + i) for i, d in
                            enumerate(("2026-10-12", "2026-10-13", "2026-10-14"))]
        fetches = []

        def fetch(data_type, op, *args, **kw):
            fetches.append(http_gate.is_research_priority())
            return g.call(lambda: prov.fetch(data_type, op, *args), data_type=data_type, classify=classify_error)
        with TempDb() as db:
            cache = BarCache(store=db, fetch=fetch, configured_source=lambda dt: "alpaca",
                             clock=FixedClock(datetime(2026, 10, 15, 12, tzinfo=timezone.utc)))
            out = {}

            def research():
                with http_gate.research_priority():
                    out["research"] = cache.get_bars("SPY", "1Day", "2026-10-12", "2026-10-15")
            th = threading.Thread(target=research)
            th.start()
            deadline = time.monotonic() + 5
            while not fetches and time.monotonic() < deadline:
                time.sleep(0.01)
            time.sleep(0.3)                                    # research now waits in the gate, lock held
            t0 = time.monotonic()
            rows = cache.get_bars("SPY", "1Day", "2026-10-12", "2026-10-15")
            latency = time.monotonic() - t0
            self.assertEqual(len(rows), 3)
            self.assertLess(latency, 1.5)                      # one research wait step, not the 20 s gate wait
            g._normal_waiting = 0
            th.join(10)
            self.assertFalse(th.is_alive())
            self.assertEqual(len(out["research"]), 3)
            self.assertEqual(fetches[0], True)


if __name__ == "__main__":
    unittest.main()
