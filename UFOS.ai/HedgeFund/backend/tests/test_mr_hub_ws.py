"""Market Research MR2b — WebSocket /research/ws (market_research/SPEC.md §4.3, §13 MR2b notes):
auth (no token / ?token= -> 401 before the handler), hello timeout -> 1008, bad messages (5 in a row -> 1008),
strict op/topic validation, subscription limit, visibility = demand, send queue overflow -> 1013, coalescing,
server frame cap (message_too_large), too many clients -> 1013, route registration. aiohttp is stubbed.

Run from HedgeFund/backend:  python -m unittest tests.test_mr_hub_ws -v
"""
import asyncio
import json
import logging
import sys
import types
import unittest

import api_auth
import market_research
from market_research import limits
from market_research.hub import ws as mr_ws
from market_research.hub.hub import MarketDataHub
from market_research.hub.ws import ResearchWsSession, encode

from tests.mr_hub_testkit import FAST, FakeAlpaca, FakeData, keys, wait_for

_LOG = logging.getLogger("market_research")


def setUpModule():
    global _level
    _level = _LOG.level
    _LOG.setLevel(logging.CRITICAL)


def tearDownModule():
    _LOG.setLevel(_level)


def run(coro):
    return asyncio.run(coro)


def make_hub():
    data = FakeData()
    return MarketDataHub(transport=FakeAlpaca(), keys=keys, data=lambda: data, config=FAST,
                         status_fn=lambda: {"schema": 1, "generated_at": "x", "feeds": []})


class FakeWs:
    def __init__(self, block=False):
        self.incoming = asyncio.Queue()
        self.sent, self.raw, self.closed = [], [], None
        self.gate = asyncio.Event()
        if not block:
            self.gate.set()

    def client(self, obj):
        self.incoming.put_nowait(("text", obj if isinstance(obj, str) else json.dumps(obj)))

    def binary(self):
        self.incoming.put_nowait(("binary", None))

    def bye(self):
        self.incoming.put_nowait(("close", None))

    async def receive(self):
        return await self.incoming.get()

    async def send_str(self, text):
        await self.gate.wait()
        self.raw.append(text)
        self.sent.append(json.loads(text))

    async def close(self, code, message=""):
        self.closed = (code, message)

    def of(self, typ, **m):
        return [x for x in self.sent if x["type"] == typ and all(x.get(k) == v for k, v in m.items())]


async def session(hub, ws, body, **kw):
    s = ResearchWsSession(hub, ws, **kw)
    task = asyncio.ensure_future(s.run())
    try:
        await body(s)
        await asyncio.wait_for(task, 2)
    finally:
        task.cancel()
    return s


class ProtocolTest(unittest.TestCase):
    def test_hello_timeout_closes_1008(self):
        hub, ws = make_hub(), FakeWs()
        run(session(hub, ws, lambda s: asyncio.sleep(0), hello_timeout=0.05))
        self.assertEqual(ws.closed, (1008, "hello expected"))
        self.assertEqual(hub.subscribers, [])

    def test_hello_visible_subscribe_and_close(self):
        hub, ws = make_hub(), FakeWs()

        async def body(s):
            ws.client({"op": "hello", "visible": True})
            self.assertTrue(await wait_for(lambda: ws.sent))
            self.assertEqual(ws.sent[0], {"type": "feeds", "data": {"schema": 1, "generated_at": "x", "feeds": []}})
            self.assertEqual(hub.demand()["views"], 1)
            ws.client({"op": "subscribe", "topics": ["trades:aapl", "candle:AAPL:5m:eth", "candle:BTC-USD:1D:utc",
                                                     "news:MSFT", "feeds", "alerts"]})
            self.assertTrue(await wait_for(lambda: ws.of("symbol_status", symbol="AAPL")))
            self.assertEqual(list(s.topics), ["trades:AAPL", "candle:AAPL:5m:eth", "candle:BTC-USD:1D:utc",
                                              "news:MSFT", "feeds", "alerts"])
            self.assertIn("candle:AAPL:5m:eth", hub.candles)
            self.assertEqual(ws.of("symbol_status", symbol="AAPL")[0]["live"], False)   # hub not running
            ws.client({"op": "visible", "visible": False})
            self.assertTrue(await wait_for(lambda: hub.demand()["views"] == 0))
            ws.client({"op": "unsubscribe", "topics": ["candle:AAPL:5m:eth"]})
            self.assertTrue(await wait_for(lambda: "candle:AAPL:5m:eth" not in hub.candles))
            ws.bye()
        run(session(hub, ws, body))
        self.assertEqual(ws.closed, (1000, ""))
        self.assertEqual(hub.subscribers, [])
        self.assertEqual(hub.candles, {})
        self.assertEqual(ws.of("error"), [])

    def test_bad_messages_then_1008(self):
        hub, ws = make_hub(), FakeWs()
        bad = ["not json", {"op": "nope"}, {"op": "hello", "visible": True, "extra": 1},
               {"op": "hello", "visible": "yes"}]

        async def body(s):
            ws.client({"op": "subscribe", "topics": ["feeds"]})           # before hello
            for b in bad:
                ws.client(b)
        run(session(hub, ws, body))
        errs = ws.of("error")
        self.assertEqual(len(errs), limits.WS_BAD_MESSAGE_LIMIT)
        self.assertTrue(all(e["code"] == "bad_message" for e in errs))
        self.assertEqual(errs[0]["message"], "hello must be the first message")
        self.assertEqual(ws.closed, (1008, "repeated bad messages"))

    def test_topic_validation_and_limits(self):
        hub, ws = make_hub(), FakeWs()

        async def body(s):
            ws.client({"op": "hello", "visible": True})
            for topics in (["candle:BTC-USD:1m:rth"], ["candle:AAPL:1m:utc"], ["candle:AAPL:2m:rth"],
                           ["trades:not a symbol"], ["quotes:AAPL"], [], ["x" * 65],
                           ["trades:AAPL"] * (limits.WS_MAX_SUBSCRIPTIONS + 1)):
                ws.client({"op": "subscribe", "topics": topics})
                ws.client({"op": "visible", "visible": True})              # a good message resets the streak
            ws.binary()
            ws.client("x" * (limits.WS_MAX_MESSAGE_BYTES + 1))
            ws.client({"op": "visible", "visible": True})
            ws.client({"op": "subscribe", "topics": [f"trades:S{i:02d}" for i in range(60)]})
            ws.client({"op": "subscribe", "topics": [f"trades:T{i:02d}" for i in range(5)]})
            ws.client({"op": "hello", "visible": True})                      # second hello
            await wait_for(lambda: len(ws.of("error")) >= 12)
            ws.bye()
        run(session(hub, ws, body))
        errs = ws.of("error")
        codes = [e["code"] for e in errs]
        self.assertEqual(codes.count("subscription_limit"), 1)
        self.assertEqual(codes.count("bad_message"), 11)
        self.assertIn("session must be utc for BTC-USD", errs[0]["message"])
        self.assertIn("session must be rth or eth for AAPL", errs[1]["message"])
        self.assertTrue(any("larger than 16384 bytes" in e["message"] for e in errs))
        self.assertTrue(any(e["message"] == "only JSON text frames are accepted" for e in errs))
        self.assertEqual(ws.closed[0], 1000)                                 # never 5 in a row

    def test_queue_overflow_closes_1013_and_coalescing(self):
        hub, ws = make_hub(), FakeWs(block=True)

        async def body(s):
            ws.client({"op": "hello", "visible": True})
            await wait_for(lambda: hub.subscribers)
            for i in range(50):                                           # coalesced: one entry per key
                s.deliver({"type": "candle", "i": i}, "candle:AAPL:1m:rth@t")
            self.assertLessEqual(len(s._queue), 3)
            self.assertEqual([e[1] for e in s._queue if e[0] == "candle:AAPL:1m:rth@t"][0]["i"], 49)
            for i in range(s.queue_max):
                s.deliver({"type": "news", "item": {"id": i}})
            await asyncio.sleep(0.05)
        run(session(hub, ws, body, queue_max=10))
        self.assertEqual(ws.closed, (1013, "send queue full"))
        self.assertEqual(hub.subscribers, [])

    def test_frame_cap(self):
        big = {"type": "feeds", "data": {"x": "a" * (limits.WS_MAX_SERVER_MESSAGE_BYTES + 10)}}
        out = json.loads(encode(big))
        self.assertEqual(out["type"], "error")
        self.assertEqual(out["code"], "message_too_large")
        ok = {"type": "sip_replace", "symbol": "AAPL", "tf": "1m", "session": "rth",
              "bars": [{"t": "2026-10-15T14:00:00+00:00", "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "v": 1.0e9,
                        "vwap": 1.0, "n": 123456, "feed": "sip", "final": True}] * limits.WS_MAX_BARS_PER_MESSAGE,
              "part": 1, "parts": 1}
        text = encode(ok)
        self.assertEqual(json.loads(text)["type"], "sip_replace")             # a full chunk fits the cap
        self.assertLess(len(text.encode("utf-8")), limits.WS_MAX_SERVER_MESSAGE_BYTES)
        self.assertNotIn("NaN", encode({"type": "trade", "price": float("nan")}))   # strict JSON (routes.dumps)


# --- aiohttp stub (the VM has no aiohttp) ----------------------------------------------------------------
class _Resp:
    def __init__(self, text, status, headers=None):
        self.text, self.status, self.headers = text, status, dict(headers or {})


class _StubWsResponse:
    instances = []

    def __init__(self, heartbeat=None, max_msg_size=None):
        self.heartbeat, self.max_msg_size, self.closed, self.close_args = heartbeat, max_msg_size, False, None
        _StubWsResponse.instances.append(self)

    async def prepare(self, request):
        return self

    async def close(self, code=1000, message=b""):
        self.closed, self.close_args = True, (code, message)


def _stub_aiohttp():
    web = types.ModuleType("aiohttp.web")
    web.json_response = lambda data, status=200, dumps=json.dumps, headers=None: _Resp(dumps(data), status, headers)
    web.middleware = lambda f: f
    web.WebSocketResponse = _StubWsResponse

    class HTTPException(Exception):
        pass
    web.HTTPException = HTTPException

    class Router:
        def __init__(self):
            self.routes = {}

        def add_route(self, method, path, handler):
            self.routes[(method, path)] = handler

    class App:
        def __init__(self):
            self.router = Router()
    web.Application = App
    pkg = types.ModuleType("aiohttp")
    pkg.web = web
    pkg.WSMsgType = types.SimpleNamespace(TEXT=1, BINARY=2, ERROR=258, CLOSE=8)
    return pkg, web


class _Request:
    def __init__(self, path="/research/ws", headers=None):
        self.method, self.path, self.path_qs = "GET", path.split("?")[0], path
        self.query, self.headers, self.match_info, self.remote = {}, dict(headers or {}), {}, "127.0.0.1"
        self.headers.setdefault("Host", "127.0.0.1:8867")          # stage 10 L2: Host check on the listening port
        self.transport = _Sock()
        self.can_read_body = False


class _Sock:
    @staticmethod
    def get_extra_info(key):
        return ("127.0.0.1", 8867) if key == "sockname" else None


class Stubbed:
    def __enter__(self):
        pkg, self.web = _stub_aiohttp()
        self.saved = {k: sys.modules.get(k) for k in ("aiohttp", "aiohttp.web")}
        sys.modules["aiohttp"], sys.modules["aiohttp.web"] = pkg, self.web
        return self.web

    def __exit__(self, *exc):
        for k, m in self.saved.items():
            if m is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = m


class AuthAndRouteTest(unittest.TestCase):
    def setUp(self):
        api_auth.init_token()
        self.addCleanup(lambda: setattr(api_auth, "_token", None))

    def test_ws_requires_header_token(self):
        path = mr_ws.WS_PATH
        self.assertNotIn(path, api_auth.PUBLIC_PATHS)
        self.assertEqual(api_auth.check("GET", path, {}, frozenset())[0], 401)
        self.assertEqual(api_auth.check("GET", path + "?token=" + api_auth.token(), {}, frozenset())[0], 401)
        self.assertIsNone(api_auth.check("GET", path, {api_auth.TOKEN_HEADER: api_auth.token()}, frozenset()))
        calls = []

        async def handler(request):
            calls.append(request)
            return _Resp("{}", 101)
        with Stubbed():
            mw = api_auth.http_middleware(frozenset())
            for req in (_Request(path), _Request(path + "?token=" + api_auth.token()),
                        _Request(path, {api_auth.TOKEN_HEADER: "wrong"})):
                resp = run(mw(req, handler))
                self.assertEqual(resp.status, 401)
                self.assertEqual(json.loads(resp.text), {"error": "missing or invalid API token"})
            self.assertEqual(calls, [])                                          # refused before the handler
            resp = run(mw(_Request(path, {api_auth.TOKEN_HEADER: api_auth.token(), "Origin": "https://evil.example"}),
                          handler))
            self.assertEqual(resp.status, 403)
            resp = run(mw(_Request(path, {api_auth.TOKEN_HEADER: api_auth.token()}), handler))
            self.assertEqual(resp.status, 101)
            self.assertEqual(len(calls), 1)

    def test_install_registers_ws_and_too_many_clients(self):
        saved = market_research._service
        self.addCleanup(lambda: setattr(market_research, "_service", saved))
        with Stubbed() as web:
            app = web.Application()
            svc = market_research.install(app)
            self.assertIn(("GET", "/research/ws"), app.router.routes)
            self.assertIn(("GET", "/research/feeds/status"), app.router.routes)
            self.assertIs(svc.hub.status_fn.__self__, svc)
            self.assertIn("market_data_hub", [t["name"] for t in svc.supervisor.status()["tasks"]])
            handler = app.router.routes[("GET", "/research/ws")]
            svc.hub.ws_clients = limits.WS_MAX_CLIENTS
            resp = run(handler(_Request()))
            self.assertEqual(resp.close_args, (1013, b"too many Research connections"))
            self.assertEqual(resp.heartbeat, limits.WS_HEARTBEAT_SECONDS)
            self.assertEqual(svc.hub.ws_clients, limits.WS_MAX_CLIENTS)


if __name__ == "__main__":
    unittest.main()
