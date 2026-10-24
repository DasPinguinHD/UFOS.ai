"""fund/api.py (paper trading only): the route handlers (FundApi) against a
real engine on a temp store — status codes, validation errors {errors},
audit rows, response shapes the WPF client (Fund/FundClient.cs) reads, no
NaN in JSON; route registration with a stubbed aiohttp; the real aiohttp
app when aiohttp is installed (skipped otherwise); and a static smoke import
of main.py with stubbed third-party modules.

Run from HedgeFund/backend:  python -m unittest tests.test_fund_api -v
"""
import asyncio
import importlib.util
import json
import os
import sys
import types
import unittest

try:
    from fund_testkit import SYMS, EngineCase
except ImportError:  # pragma: no cover
    from tests.fund_testkit import SYMS, EngineCase

from fund import api as fund_api
from strategy_platform.core import params as P
from fund import store

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def call(api, name, query=None, body=fund_api._NO_BODY, match=None):
    return asyncio.run(getattr(api, name)(query or {}, body, match or {}))


class HandlerTest(EngineCase):
    def setUp(self):
        super().setUp()
        self.api = fund_api.FundApi(self.engine)

    def test_state_and_json_is_strict(self):
        status, payload = call(self.api, "state")
        self.assertEqual(status, 200)
        text = fund_api.dumps(payload)
        self.assertIn('"symbols"', text)
        self.assertEqual(json.loads(fund_api.dumps({"x": float("nan")})), {"x": None})

    def test_params_get_shape_for_the_client(self):
        status, p = call(self.api, "params_get")
        self.assertEqual(status, 200)
        self.assertEqual(set(p), {"version", "params", "defaults", "limits", "created_at"})
        for key in ("min_confidence", "max_position_pct", "max_trades_per_day", "drawdown_stop_pct",
                    "interval_minutes", "closed_interval_minutes", "ai_daily_budget_usd", "fees_bps",
                    "short_exposure_max_pct"):
            lim = p["limits"][key]
            self.assertTrue({"min", "max", "step"} <= set(lim), key)
            self.assertLess(lim["min"], lim["max"])
        self.assertEqual(p["params"]["watchlist"], SYMS)
        json.loads(fund_api.dumps(p))

    def test_params_put(self):
        status, p = call(self.api, "params_put", body={"max_position_pct": 99, "nope": 1})
        self.assertEqual(status, 400)
        self.assertEqual(set(p["errors"]), {"max_position_pct", "nope"})
        self.assertEqual(call(self.api, "params_put", body=[1, 2])[0], 400)
        self.assertEqual(call(self.api, "params_put")[0], 400)
        v = store.effective_params()[0]
        status, p = call(self.api, "params_put",
                         body={"agent_weights": {"technical": 0.5, "macro": 1.0, "sentiment": 0.25}})
        self.assertEqual(status, 200)
        self.assertEqual(set(p), {"version", "params", "effective_from"})
        self.assertEqual(p["version"], v + 1)
        self.assertEqual(store.list_audit(1)[0]["action"], "params_update")

    def test_run_autopilot_kill_resume(self):
        self.assertEqual(call(self.api, "run", body={}), (200, {"queued": True}))
        self.assertEqual(store.list_audit(1)[0]["action"], "run_now")
        self.assertEqual(call(self.api, "autopilot", body={})[0], 400)
        self.assertEqual(call(self.api, "autopilot", body={"enabled": "yes"})[0], 400)
        self.assertEqual(call(self.api, "autopilot", body={"enabled": True}), (200, {"autopilot": True}))
        status, k = call(self.api, "kill", body={"reason": "test"})
        self.assertEqual(status, 200)
        self.assertIs(k["halted"], True)
        self.assertIsInstance(k["orders"], list)
        status, err = call(self.api, "autopilot", body={"enabled": True})
        self.assertEqual(status, 409)
        self.assertIn("halted", err["error"])
        self.assertEqual(call(self.api, "resume", body={}), (200, {"halted": False}))
        self.assertEqual(call(self.api, "kill", body={"reason": 5})[0], 400)

    def test_runs_orders_events_audit(self):
        self.cycle()
        status, runs = call(self.api, "runs", {"limit": "30"})
        self.assertEqual(status, 200)
        self.assertTrue({"id", "started_at", "finished_at", "status", "trigger", "params_version", "market_open",
                         "judge_mode", "models", "cost_usd", "recommendations", "orders", "flips"} <= set(runs[0]))
        self.assertEqual(call(self.api, "runs", {"limit": "x"})[0], 400)
        self.assertEqual(call(self.api, "runs", {"limit": "0"})[0], 400)
        status, d = call(self.api, "run_detail", match={"id": str(runs[0]["id"])})
        self.assertEqual(status, 200)
        self.assertEqual(set(d), {"run", "params", "agents", "recommendations", "orders"})
        self.assertEqual(call(self.api, "run_detail", match={"id": "999"})[0], 404)
        self.assertEqual(call(self.api, "run_detail", match={"id": "abc"})[0], 400)

        status, o = call(self.api, "orders_post", body={"symbol": "SPY", "side": "buy", "qty": 2})
        self.assertEqual(status, 200)
        self.assertEqual((o["status"], o["origin"]), ("filled", "manual"))
        self.assertTrue({"order_id", "symbol", "side", "qty", "status", "fill_price", "fees", "reject_reason",
                         "origin", "ts"} <= set(o))
        status, err = call(self.api, "orders_post", body={"symbol": "SPY", "side": "buy"})
        self.assertEqual(status, 400)
        self.assertIn("qty", err["errors"])
        status, orders = call(self.api, "orders", {"limit": "10"})
        self.assertEqual(orders[0]["id"], o["order_id"])
        self.assertTrue({"id", "ts", "side", "qty", "status", "fill_price", "fees", "origin", "reject_reason"}
                        <= set(orders[0]))

        status, evs = call(self.api, "events", {"after_id": "0"})
        self.assertEqual(status, 200)
        self.assertLessEqual(len(evs), 50)
        self.assertEqual(call(self.api, "events", {"after_id": "-1"})[0], 400)
        status, audit = call(self.api, "audit", {"limit": "5"})
        self.assertEqual(audit[0]["action"], "manual_order")

    def test_execute_recommendation(self):
        self.cycle()
        rec = store.latest_recommendations()["SPY"]
        status, res = call(self.api, "execute", body={}, match={"id": str(rec["id"])})
        self.assertEqual(status, 200)
        self.assertEqual(res[0]["origin"], "manual-accept")
        self.assertEqual(call(self.api, "execute", body={}, match={"id": str(rec["id"])})[0], 409)
        self.assertEqual(call(self.api, "execute", body={}, match={"id": "12345"})[0], 404)

    def test_execute_with_failing_data_source_is_503_with_reason(self):
        from strategy_platform.core.errors import DataSourceUnavailable
        self.cycle()
        rec = store.latest_recommendations()["QQQ"]

        def down(sym):
            raise DataSourceUnavailable("alpaca", "quotes", "HTTP 503")
        self.feed.quote = down
        status, err = call(self.api, "execute", body={}, match={"id": str(rec["id"])})
        self.assertEqual(status, 503)
        self.assertIn("alpaca is unavailable for quotes", err["error"])

    def test_state_carries_judge_unavailable_per_symbol(self):
        """D9 (2026-10-02): budget reached -> no recommendation, the reason per symbol."""
        store.save_params(dict(store.effective_params()[1], ai_daily_budget_usd=0.01), "test")
        store.add_ai_spend(0.05)
        self.cycle()
        status, st = call(self.api, "state")
        self.assertEqual(status, 200)
        for row in st["symbols"]:
            self.assertEqual(row["judge_unavailable"], "AI budget reached — raise the limit in Settings to continue.")
            self.assertIsNone(row["data_unavailable"])
            self.assertIsNone(row["recommendation"])
        self.assertEqual(st["engine"]["last_run"]["judge_mode"], "unavailable")

    def test_equity_and_reset(self):
        self.cycle()
        status, eq = call(self.api, "equity")
        self.assertEqual(status, 200)
        self.assertTrue({"ts", "equity", "benchmark_equity"} <= set(eq["points"][0]))
        self.assertEqual(eq["starting_capital"], 100000.0)
        self.assertEqual(call(self.api, "equity", {"since": "yesterday"})[0], 400)
        self.assertEqual(call(self.api, "equity", {"since": "2026-10-01T00:00:00Z"})[0], 200)
        self.assertEqual(call(self.api, "reset", body={"starting_capital": "lots"})[0], 400)
        status, r = call(self.api, "reset", body={"starting_capital": 25000})
        self.assertEqual(status, 200)
        self.assertEqual(r["account"]["equity"], 25000.0)

    def test_backtest_validation_and_run(self):
        self.assertEqual(call(self.api, "backtest", body={"start": "2026-13-01"})[0], 400)
        self.assertEqual(call(self.api, "backtest", body={"start": "2026-09-01", "end": "2026-08-01"})[0], 400)
        self.assertEqual(call(self.api, "backtest", body={"symbols": ["??"]})[0], 400)
        status, res = call(self.api, "backtest", body={"symbols": ["SPY"], "start": "2026-03-01",
                                                       "end": "2026-10-01"})
        self.assertEqual(status, 200, res)
        self.assertTrue({"symbols", "start", "end", "portfolio", "benchmark", "per_symbol", "equity", "notes", "id"}
                        <= set(res))
        json.loads(fund_api.dumps(res))
        status, lst = call(self.api, "backtests")
        self.assertEqual(lst[0]["id"], res["id"])
        self.assertEqual(call(self.api, "backtest_detail", match={"id": str(res["id"])})[0], 200)
        self.assertEqual(store.list_audit(1)[0]["action"], "backtest")
        status, err = call(self.api, "backtest", body={"symbols": ["NODATA"]})
        self.assertEqual(status, 409)

    def test_exports(self):
        self.cycle()
        status, csv_body = call(self.api, "export_orders")
        self.assertEqual(status, 200)
        self.assertEqual(csv_body.filename, "orders.csv")
        self.assertTrue(csv_body.text.startswith("id,ts,symbol,side"))
        status, csv_body = call(self.api, "export_decisions")
        self.assertEqual(len(csv_body.text.strip().splitlines()), 1 + len(SYMS))


# --- stubbed aiohttp: route registration + request adapter ----------------------

class _StubResponse:
    def __init__(self, text="", status=200, content_type="application/json", charset=None, headers=None):
        self.text = text
        self.status = status
        self.content_type = content_type
        self.headers = dict(headers or {})


def _stub_web():
    web = types.ModuleType("aiohttp.web")
    web.Response = _StubResponse

    def json_response(data, status=200, dumps=json.dumps, headers=None):
        return _StubResponse(dumps(data), status, "application/json", headers=headers)
    web.json_response = json_response

    class Router:
        def __init__(self):
            self.routes = {}

        def add_route(self, method, path, handler):
            self.routes[(method, path)] = handler

    class Application:
        def __init__(self, middlewares=()):
            self.router = Router()
    web.Application = Application
    return web


class _StubRequest:
    def __init__(self, body_text=None, query=None, match=None):
        self._text = body_text
        self.can_read_body = body_text is not None
        self.query = query or {}
        self.match_info = match or {}

    async def text(self):
        return self._text or ""


def _drop_module(name):
    """Pop ``name`` from sys.modules and drop the parent package attribute that still points at it."""
    mod = sys.modules.pop(name, None)
    parent, _, child = name.rpartition(".")
    if mod is not None and parent and getattr(sys.modules.get(parent), child, None) is mod:
        delattr(sys.modules[parent], child)


class RegisterTest(EngineCase):
    def test_register_with_stubbed_aiohttp(self):
        web = _stub_web()
        pkg = types.ModuleType("aiohttp")
        pkg.web = web
        saved = {k: sys.modules.get(k) for k in ("aiohttp", "aiohttp.web")}
        sys.modules["aiohttp"], sys.modules["aiohttp.web"] = pkg, web
        try:
            app = web.Application()
            fund_api.register(app, self.engine)
        finally:
            for k, m in saved.items():
                if m is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = m
        routes = app.router.routes
        self.assertEqual(len(routes), len(fund_api.ROUTES))
        for key in [("GET", "/fund/state"), ("PUT", "/fund/params"), ("POST", "/fund/kill"),
                    ("POST", "/fund/recommendations/{id}/execute"), ("GET", "/fund/export/decisions.csv")]:
            self.assertIn(key, routes)
        resp = asyncio.run(routes[("GET", "/fund/state")](_StubRequest()))
        self.assertEqual(resp.status, 200)
        self.assertIn("pending_orders", json.loads(resp.text))
        resp = asyncio.run(routes[("PUT", "/fund/params")](_StubRequest("{not json")))
        self.assertEqual(resp.status, 400)
        resp = asyncio.run(routes[("PUT", "/fund/params")](_StubRequest('{"fees_bps": 7}')))
        self.assertEqual((resp.status, json.loads(resp.text)["params"]["fees_bps"]), (200, 7.0))
        resp = asyncio.run(routes[("POST", "/fund/run")](_StubRequest("")))
        self.assertEqual(json.loads(resp.text), {"queued": True})
        resp = asyncio.run(routes[("GET", "/fund/export/orders.csv")](_StubRequest()))
        self.assertEqual(resp.content_type, "text/csv")
        self.assertIn("orders.csv", resp.headers["Content-Disposition"])


@unittest.skipUnless(importlib.util.find_spec("aiohttp") is not None, "aiohttp not installed")
class AiohttpAppTest(EngineCase):  # pragma: no cover - needs aiohttp
    def test_real_app(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        async def go():
            app = web.Application()
            fund_api.register(app, self.engine)
            async with TestClient(TestServer(app)) as client:
                r = await client.get("/fund/state")
                self.assertEqual(r.status, 200)
                self.assertIn("symbols", await r.json())
                r = await client.put("/fund/params", json={"min_confidence": 10})
                self.assertEqual(r.status, 400)
                self.assertIn("min_confidence", (await r.json())["errors"])
                r = await client.get("/fund/export/orders.csv")
                self.assertEqual(r.content_type, "text/csv")
        asyncio.run(go())


# --- main.py smoke import ------------------------------------------------------

class _AnyModule(types.ModuleType):
    """Stub module: any attribute is a dummy class."""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        cls = type(name, (), {"__init__": lambda self, *a, **k: None})
        setattr(self, name, cls)
        return cls


class MainImportTest(unittest.TestCase):
    def test_main_imports_with_stubbed_dependencies(self):
        before = set(sys.modules)
        saved = {}
        stubs = {}
        for name in ("aiohttp", "aiohttp.web", "websockets", "dotenv", "openai", "logging_setup"):
            saved[name] = sys.modules.get(name)
            stubs[name] = _AnyModule(name)
        stubs["aiohttp"].web = stubs["aiohttp.web"]
        stubs["dotenv"].load_dotenv = lambda *a, **k: False
        stubs["logging_setup"].configure_logging = lambda *a, **k: None
        sys.modules.update(stubs)
        try:
            spec = importlib.util.spec_from_file_location("main_smoke", os.path.join(_BACKEND, "main.py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self.assertTrue(callable(mod.main))
            self.assertTrue(hasattr(mod.fund_api, "register"))
            self.assertTrue(hasattr(mod.fund_engine, "FundEngine"))
            with open(os.path.join(_BACKEND, "main.py"), encoding="utf-8") as fh:
                src = fh.read()
            self.assertIn("fund_api.register(app, engine)", src)
            self.assertLess(src.index("await load_from_store("), src.index("engine.start()"))
        finally:
            for name in set(sys.modules) - before:
                if name.startswith(("newsroom.ai", "main_smoke", "config", "api_auth", "aiohttp", "websockets",
                                    "dotenv", "openai", "logging_setup", "newsroom.fred", "newsroom.sec",
                                    "newsroom.calendar", "newsroom.overview", "newsroom.insider_summary")):
                    _drop_module(name)
            for name, m in saved.items():
                if m is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = m


class ReviewApiTest(EngineCase):
    """Review 2026-09-26: heartbeat on GET state/events, 400 for huge numbers,
    503 without audit log (kill switch still works), 409 superseded."""

    def setUp(self):
        super().setUp()
        self.api = fund_api.FundApi(self.engine)

    def test_state_and_events_are_the_client_heartbeat(self):
        self.assertIsNone(self.engine.last_client_seen)
        status, st = call(self.api, "state")
        self.assertEqual(status, 200)
        self.assertEqual(st["last_client_seen"], st["now"])
        self.engine._last_client_seen = None
        self.assertEqual(call(self.api, "events")[0], 200)
        self.assertIsNotNone(self.engine.last_client_seen)
        self.engine._last_client_seen = None
        self.assertEqual(call(self.api, "params_get")[0], 200)     # other routes are no heartbeat
        self.assertIsNone(self.engine.last_client_seen)

    def test_huge_numbers_are_400(self):
        status, err = call(self.api, "params_put", body={"max_position_pct": 10 ** 400})
        self.assertEqual(status, 400)
        self.assertIn("max_position_pct", err["errors"])
        status, err = call(self.api, "orders_post", body={"symbol": "SPY", "side": "buy", "notional": 10 ** 400})
        self.assertEqual(status, 400)

    def test_audit_unavailable_is_503_but_kill_works(self):
        old = store.add_audit
        store.add_audit = lambda *a, **kw: None
        try:
            status, err = call(self.api, "autopilot", body={"enabled": True})
            self.assertEqual(status, 503)
            self.assertIn("audit log unavailable", err["error"])
            self.assertEqual(call(self.api, "orders_post", body={"symbol": "SPY", "side": "buy", "qty": 1})[0], 503)
            self.assertEqual(call(self.api, "params_put", body={"min_confidence": 80})[0], 503)
            status, k = call(self.api, "kill", body={"reason": "audit down"})
            self.assertEqual(status, 200)
            self.assertIs(k["halted"], True)
        finally:
            store.add_audit = old

    def test_execute_superseded_is_409(self):
        self.cycle()
        old = store.latest_recommendations()["SPY"]
        self.cycle()
        status, err = call(self.api, "execute", body={}, match={"id": str(old["id"])})
        self.assertEqual(status, 409)
        self.assertIn("superseded by a newer recommendation", err["error"])

    def test_second_backtest_while_running_is_409(self):
        self.engine._backtest_running = True
        status, err = call(self.api, "backtest", body={"symbols": ["SPY"]})
        self.assertEqual(status, 409)
        self.assertIn("a backtest is already running", err["error"])


if __name__ == "__main__":
    unittest.main()
