"""Market Research MR2a — Alpaca budget share (SPEC §7.5, Q3): ``http_gate.research_priority()`` —
Research never above 60 requests in any 60 s, never takes the shared reserve, yields to waiting
normal-priority (trading / platform) callers, never holds a concurrency slot while it waits, bounded wait
→ clear message, counters for feeds/status; the context reaches the Research pool. Plus the provider
extension ``AlpacaProvider.assets()`` and ``sources.AlpacaSources`` (15-min SIP rule, end exclusive).

Run from HedgeFund/backend:  python -m unittest tests.test_mr_data_gate -v
"""
import asyncio
import unittest
from datetime import timedelta
from types import SimpleNamespace

from market_research import executor
from market_research.sources import AlpacaSources
from strategy_platform.core.errors import DataSourceUnavailable, SecretMissing
from strategy_platform.data import http_gate, network
from strategy_platform.data.providers.alpaca import AlpacaProvider, classify_error

try:
    from mr_data_testkit import utc
except ImportError:  # pragma: no cover - run as package
    from tests.mr_data_testkit import utc


class FakeTime:
    def __init__(self):
        self.t = 1000.0
        self.hook = None

    def monotonic(self):
        return self.t

    def sleep(self, s):
        if self.hook:
            self.hook(s)
        self.t += s


def gate(**kw):
    ft = FakeTime()
    return http_gate.HttpGate("alpaca", monotonic=ft.monotonic, sleep=ft.sleep, **kw), ft


def call(g, fn=lambda: 1):
    return g.call(fn, data_type="equity_bars", classify=classify_error)


def max_in_window(stamps, sec):
    best, j = 0, 0
    for i, t in enumerate(stamps):
        while t - stamps[j] >= sec:
            j += 1
        best = max(best, i - j + 1)
    return best


class ResearchShareTest(unittest.TestCase):
    def test_never_above_60_per_minute(self):
        g, ft = gate()
        stamps = []
        with http_gate.research_priority():
            for _ in range(150):
                call(g, lambda: stamps.append(ft.t))
        self.assertEqual(len(stamps), 150)
        self.assertLessEqual(max_in_window(stamps, 60.0), 60)
        self.assertGreater(stamps[-1] - stamps[0], 120.0)
        self.assertEqual(g.research_calls, 150)

    def test_yields_to_waiting_normal_caller_then_times_out(self):
        g, ft = gate()
        g._normal_waiting = 1                     # a trading/platform request is waiting for a token
        called = []
        with http_gate.research_priority():
            with self.assertRaises(DataSourceUnavailable) as cm:
                call(g, lambda: called.append(1))
        self.assertEqual(called, [])
        self.assertIn("Research waited too long for the Alpaca rate limit (shared with trading)",
                      cm.exception.user_message)
        self.assertGreaterEqual(ft.t - 1000.0, http_gate.RESEARCH_ACQUIRE_TIMEOUT_SECONDS)
        self.assertEqual(g.research_stats()["used_last_minute"], 0)     # the reservation was released
        g._normal_waiting = 0
        with http_gate.research_priority():
            self.assertEqual(call(g), 1)

    def test_never_takes_the_reserve(self):
        g, ft = gate()
        g.bucket._tokens = float(http_gate.RESEARCH_RESERVE_TOKENS)
        g.bucket._last = ft.t
        with http_gate.research_priority():
            call(g)
        waited = ft.t - 1000.0
        self.assertGreater(waited, 0.3)            # until 4 tokens were there (180/min = 3/s)
        self.assertGreaterEqual(g.bucket._tokens, http_gate.RESEARCH_RESERVE_TOKENS - 1e-6)
        # a normal caller takes the reserve at once
        g.bucket._tokens, g.bucket._last = 1.0, ft.t
        t0 = ft.t
        call(g)
        self.assertEqual(ft.t, t0)

    def test_normal_waiters_are_counted_and_normal_path_unchanged(self):
        g, ft = gate()
        g.bucket._tokens, g.bucket._last = 0.0, ft.t
        seen = []
        ft.hook = lambda s: seen.append(g._normal_waiting)
        call(g)
        self.assertTrue(seen and all(n == 1 for n in seen))
        self.assertEqual(g._normal_waiting, 0)
        self.assertEqual((g.research_calls, g.research_stats()["used_last_minute"]), (0, 0))
        self.assertEqual(g.calls, 1)

    def test_no_concurrency_slot_while_waiting(self):
        lim = network.AdaptiveLimiter("alpaca", adaptive=False, fixed=4)
        g, _ft = gate(concurrency=lim)
        g._normal_waiting = 1
        with http_gate.research_priority():
            with self.assertRaises(DataSourceUnavailable):
                call(g)
        self.assertEqual((lim.in_flight, lim.peak), (0, 0))         # never took a slot while waiting
        g._normal_waiting = 0
        with http_gate.research_priority():
            call(g, lambda: self.assertEqual((lim.in_flight, g._research_in_flight), (1, 1)))
        self.assertEqual((lim.in_flight, g._research_in_flight), (0, 0))

    def test_stats_counters(self):
        g, ft = gate()
        self.assertEqual(g.research_stats(), {"used_last_minute": 0, "waiting_seconds": None})
        with http_gate.research_priority():
            for _ in range(3):
                call(g)
        self.assertEqual(g.research_stats()["used_last_minute"], 3)
        waits = []
        g._normal_waiting = 1

        def hook(_s):
            waits.append(g.research_stats()["waiting_seconds"])
            if ft.t > 1005:
                g._normal_waiting = 0
        ft.hook = hook
        with http_gate.research_priority():
            call(g)
        self.assertTrue(waits and waits[-1] >= 4.0)
        ft.hook = None
        ft.t += 61
        self.assertEqual(g.research_stats(), {"used_last_minute": 0, "waiting_seconds": None})

    def test_flag_reaches_research_pool(self):
        async def go():
            outside = await executor.run_blocking(http_gate.is_research_priority)
            with http_gate.research_priority():
                inside = await executor.run_blocking(http_gate.is_research_priority)
            return outside, inside
        self.assertEqual(asyncio.run(go()), (False, True))
        self.assertFalse(http_gate.is_research_priority())


class ProviderAssetsTest(unittest.TestCase):
    def test_assets_via_trading_api_and_gate(self):
        seen = []

        class TradingClient:
            def __init__(self, api_key=None, secret_key=None, paper=None, raw_data=None):
                seen.append(("client", api_key, paper, raw_data))
                self._retry = 3
                self._session = SimpleNamespace(request=lambda *a, **k: None)

            def get_all_assets(self, req):
                seen.append(("get", req.asset_class))
                if req.asset_class == "crypto":
                    return [{"symbol": "BTC/USD", "name": "Bitcoin\n / US Dollar", "class": "crypto",
                             "exchange": "CRYPTO", "tradable": True, "status": "active"}]
                return [{"symbol": "aapl", "name": "Apple Inc.", "class": "us_equity", "exchange": "NASDAQ",
                         "tradable": True, "status": "active", "attributes": ["has_options"]}, "junk"]
        sdk = SimpleNamespace(TradingClient=TradingClient, AssetClass=lambda v: v,
                              AssetStatus=SimpleNamespace(ACTIVE="active"),
                              GetAssetsRequest=lambda **kw: SimpleNamespace(**kw))
        keys = SimpleNamespace(require_secret=lambda n: {"alpaca-paper-key-id": "kid",
                                                         "alpaca-paper-secret-key": "sec"}[n])
        g, _ft = gate()
        rows = AlpacaProvider(gate=g, trading_sdk=sdk, secrets_module=keys).assets()
        self.assertEqual(seen[0], ("client", "kid", True, True))
        self.assertEqual(g.calls, 2)
        self.assertEqual([(r["symbol"], r["asset_class"], r["name"]) for r in rows],
                         [("AAPL", "us_equity", "Apple Inc."), ("BTC/USD", "crypto", "Bitcoin / US Dollar")])

    def test_assets_without_keys(self):
        def missing(name):
            raise SecretMissing(name, "Alpaca key ID")
        sdk = SimpleNamespace(TradingClient=None, AssetClass=None, AssetStatus=None, GetAssetsRequest=None)
        with self.assertRaises(SecretMissing):
            AlpacaProvider(gate=gate()[0], trading_sdk=sdk,
                           secrets_module=SimpleNamespace(require_secret=missing)).assets()


class Provider30MinTest(unittest.TestCase):
    def test_sip_30min_clamped_and_known_at(self):
        try:
            from test_sp_data_support import Script, fake_sdk
        except ImportError:  # pragma: no cover
            from tests.test_sp_data_support import Script, fake_sdk
        from strategy_platform.core.clock import FixedClock
        from strategy_platform.data.providers.alpaca import AlpacaSipProvider
        sc = Script()
        sc.set("get_stock_bars", {"AAPL": [{"t": "2026-10-15T13:30:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5,
                                            "v": 100, "n": 5, "vw": 1.2}]})
        keys = SimpleNamespace(require_secret=lambda n: "k")
        p = AlpacaSipProvider(gate=gate()[0], sdk=fake_sdk(sc), secrets_module=keys,
                              clock=FixedClock(utc(2026, 10, 15, 15, 0)))
        rows = p.get_bars("AAPL", "30Min", utc(2026, 10, 15, 13), utc(2026, 10, 15, 16))
        req = sc.calls[0][1]
        self.assertEqual(req.timeframe, ("TF", 30, "Min"))
        self.assertEqual(req.end, utc(2026, 10, 15, 14, 45))            # sip_request_end: now − 15 min
        self.assertEqual(req.feed, "sip")
        self.assertEqual(rows[0]["known_at"], "2026-10-15T14:00:00+00:00")
        self.assertEqual(rows[0]["timeframe"], "30Min")


class SourcesTest(unittest.TestCase):
    def test_end_exclusive_and_sip_provider(self):
        asked = []

        class Prov:
            def __init__(self, name):
                self.name = name

            def get_bars(self, symbol, tf, start, end):
                asked.append((self.name, tf, start, end))
                t = start
                out = []
                while t <= end:
                    out.append({"ts": t.isoformat(timespec="seconds")})
                    t += timedelta(minutes=1)
                return out
        src = AlpacaSources(iex=Prov("iex"), sip=Prov("sip"), cache=object())
        rows = src.sip_1m("AAPL", utc(2026, 10, 15, 14, 0), utc(2026, 10, 15, 14, 5))
        self.assertEqual(len(rows), 5)
        self.assertEqual(asked[0][:2], ("sip", "1Min"))
        self.assertEqual(asked[0][3], utc(2026, 10, 15, 14, 4, 59))
        src.iex_1m("AAPL", utc(2026, 10, 15, 14, 0), utc(2026, 10, 15, 14, 1))
        self.assertEqual(asked[1][0], "iex")


if __name__ == "__main__":
    unittest.main()
