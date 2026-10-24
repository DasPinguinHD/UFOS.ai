"""Market Research MR2a — the REST routes (SPEC §4.1/§4.2): validation (unknown params, tf, session for
crypto, limit, before, 30-day bound, symbol), status codes (404 SymbolNotCovered, 503 with the
user message for missing keys, no internals), exact top-level shapes, strict JSON, every route registered
and running in the Research pool under research priority.

Run from HedgeFund/backend:  python -m unittest tests.test_mr_data_api -v
"""
import asyncio
import json
import logging
import unittest

try:
    from mr_data_testkit import FakeSources, TempStore, utc
    from test_mr_shell import StubbedAiohttp, _Request
except ImportError:  # pragma: no cover - run as package
    from tests.mr_data_testkit import FakeSources, TempStore, utc
    from tests.test_mr_shell import StubbedAiohttp, _Request

from market_research import ResearchService, api as mr_api
from market_research.assets import CRYPTO_NOT_COVERED
from market_research.data import DataService
from market_research.supervisor import ResearchSupervisor
from strategy_platform.core.errors import SecretMissing
from strategy_platform.data import http_gate

NOW = utc(2026, 10, 15, 15, 0)


def setUpModule():
    logging.getLogger("market_research").setLevel(logging.CRITICAL + 1)


def tearDownModule():
    logging.getLogger("market_research").setLevel(logging.NOTSET)


def run(c):
    return asyncio.run(c)


class Q(dict):
    """aiohttp MultiDict-like query (getall)."""

    def __init__(self, pairs=()):
        super().__init__()
        self.pairs = list(pairs)
        for k, v in self.pairs:
            self.setdefault(k, v)

    def getall(self, k, *default):
        # aiohttp semantics: a missing key raises KeyError unless a default is given
        vals = [v for kk, v in self.pairs if kk == k]
        if not vals:
            if default:
                return default[0]
            raise KeyError(k)
        return vals


class ApiCase(unittest.TestCase):
    def setUp(self):
        self._ts = TempStore()
        self._ts.__enter__()
        self.fs = FakeSources(lambda: NOW)
        self.svc = ResearchService(supervisor=ResearchSupervisor(),
                                   data=DataService(sources=self.fs, now=lambda: NOW))
        self.api = mr_api.ResearchApi(self.svc)

    def tearDown(self):
        self._ts.__exit__(None, None, None)

    def bars(self, sym, *pairs):
        return run(self.api.symbol_bars(Q(pairs), None, {"symbol": sym}))


class ValidationTest(ApiCase):
    def test_bars_params(self):
        cases = [(("tf", "2m"),), (("tf", "1D"), ("x", "1")), (("tf", "1D"), ("limit", "0")),
                 (("tf", "1D"), ("limit", "2001")), (("tf", "5m"), ("limit", "5001")), (("tf", "1D"), ("limit", "1e3")),
                 (("tf", "1D"), ("session", "pre")), (("tf", "1D"), ("before", "yesterday")),
                 (("tf", "1D"), ("tf", "1W")), ()]
        for pairs in cases:
            with self.subTest(pairs=pairs):
                status, p = self.bars("AAPL", *pairs)
                self.assertEqual(status, 400)
                self.assertEqual(set(p), {"error", "errors"})
        status, p = self.bars("BTC-USD", ("tf", "1H"), ("session", "rth"))
        self.assertEqual((status, list(p["errors"])), (400, ["session"]))
        status, p = self.bars("NOT A SYMBOL", ("tf", "1D"))
        self.assertEqual((status, list(p["errors"])), (400, ["symbol"]))
        status, p = self.bars("AAPL", ("tf", "5m"), ("before", "2026-09-01T15:00:00Z"))
        self.assertEqual(status, 400)
        self.assertEqual(p["error"], "Intraday history older than 30 days is not loaded in Research.")

    def test_assets_and_market_params(self):
        for q in (Q(), Q([("q", "")]), Q([("q", "a" * 33)]), Q([("q", "a;drop")]), Q([("q", "a"), ("limit", "51")]),
                  Q([("q", "a"), ("foo", "1")])):
            with self.subTest(q=q.pairs):
                self.assertEqual(run(self.api.assets(q, None, {}))[0], 400)
        self.assertEqual(run(self.api.market_status(Q([("x", "1")]), None, {}))[0], 400)
        self.assertEqual(run(self.api.symbol_snapshot(Q([("x", "1")]), None, {"symbol": "AAPL"}))[0], 400)
        self.assertEqual(run(self.api.symbol_stats(Q([("x", "1")]), None, {"symbol": "AAPL"}))[0], 400)


class ShapesTest(ApiCase):
    def test_exact_top_level_shapes(self):
        st, p = run(self.api.assets(Q([("q", "aa"), ("limit", "2")]), None, {}))
        self.assertEqual((st, set(p), len(p["items"])), (200, {"items", "refreshed_at", "source"}, 2))
        st, p = run(self.api.market_status(Q(), None, {}))
        self.assertEqual((st, set(p)), (200, {"equities", "crypto", "generated_at"}))
        st, p = run(self.api.symbol_snapshot(Q(), None, {"symbol": "aapl"}))
        self.assertEqual(st, 200)
        self.assertEqual(set(p), {"symbol", "name", "exchange", "asset_class", "kind", "last", "prev_close", "change",
                                  "change_pct", "day_low", "day_high", "volume", "market"})
        self.assertEqual(p["symbol"], "AAPL")
        st, p = self.bars("AAPL", ("tf", "15m"), ("session", "eth"), ("limit", "10"))
        self.assertEqual(st, 200)
        self.assertEqual(set(p), {"symbol", "tf", "session", "bars", "has_more_left", "sip_through", "tz"})
        self.assertEqual(set(p["bars"][0]), {"t", "o", "h", "l", "c", "v", "vwap", "n", "feed", "final", "s"})
        self.assertIn(p["bars"][0]["s"], ("pre", "rth", "post"))
        self.assertIsInstance(p["bars"][0]["t"], str)
        self.assertTrue(p["bars"][0]["t"].endswith("+00:00"))
        st, p = run(self.api.symbol_stats(Q(), None, {"symbol": "AAPL"}))
        self.assertEqual(set(p), {"open", "vwap", "avg_volume_30d", "exchange", "performance", "range_52w", "notes"})
        self.assertEqual(set(p["performance"]), {"1W", "1M", "3M", "6M", "YTD", "1Y"})
        json.loads(mr_api.dumps(p))

    def test_not_covered_404(self):
        st, p = run(self.api.symbol_snapshot(Q(), None, {"symbol": "DOGE-USD"}))
        self.assertEqual((st, p), (404, {"error": CRYPTO_NOT_COVERED, "kind": "SymbolNotCovered"}))
        st, p = self.bars("ZZZZ", ("tf", "1D"))
        self.assertEqual((st, p["kind"]), (404, "SymbolNotCovered"))

    def test_runs_under_research_priority(self):
        seen = []
        orig = self.fs.latest_trade

        def trade(sym):
            seen.append(http_gate.is_research_priority())
            return orig(sym)
        self.fs.latest_trade = trade
        run(self.api.symbol_snapshot(Q(), None, {"symbol": "AAPL"}))
        self.assertEqual(seen, [True])


class RegisteredTest(ApiCase):
    def _call(self, routes, path, query=None, match=None):
        req = _Request(path, query)
        req.query = Q(list((query or {}).items()))
        req.match_info = match or {}
        return run(routes[("GET", path)](req))

    def test_routes_registered_and_503_for_missing_keys(self):
        with StubbedAiohttp() as web:
            app = web.Application()
            mr_api.register(app, self.svc)
            routes = app.router.routes
            for path in ("/research/assets", "/research/market/status", "/research/symbols/{symbol}/snapshot",
                         "/research/symbols/{symbol}/bars", "/research/symbols/{symbol}/stats"):
                self.assertIn(("GET", path), routes)
            self.fs.fail["daily"] = SecretMissing("alpaca-paper-key-id", "Alpaca key ID")
            resp = self._call(routes, "/research/symbols/{symbol}/bars", {"tf": "1D"}, {"symbol": "AAPL"})
            self.assertEqual(resp.status, 503)
            body = json.loads(resp.text if hasattr(resp, "text") else resp.body)
            self.assertEqual(body["kind"], "SecretMissing")
            self.assertIn("enter it in Hedge Fund", body["error"])
            self.fs.latest_trade = lambda s: 1 / 0                      # unexpected bug -> generic 500
            self.fs.fail.clear()
            resp = self._call(routes, "/research/symbols/{symbol}/snapshot", None, {"symbol": "AAPL"})
            self.assertEqual(resp.status, 500)
            body = json.loads(resp.text if hasattr(resp, "text") else resp.body)
            self.assertEqual(body, {"error": "internal error in Market Research (see backend.log)"})


if __name__ == "__main__":
    unittest.main()
