"""api_auth.py — loopback default, session token, origin check (security-by-default rule)."""
import os
import sys
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_auth  # noqa: E402


class BindHostTests(unittest.TestCase):
    def test_always_loopback(self):
        """No remote access since stage 10 (Docker removed): the old switches change nothing."""
        with mock.patch.dict(os.environ, {"ALLOW_REMOTE_ACCESS": "true", "WS_HOST": "0.0.0.0", "HTTP_HOST": "0.0.0.0"}):
            self.assertEqual(api_auth.bind_host("HTTP API"), "127.0.0.1")
            self.assertEqual(api_auth.bind_host(), "127.0.0.1")
        self.assertFalse(hasattr(api_auth, "remote_access_allowed"))


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "data" / "api-token"
        self.token = api_auth.init_token()
        self.assertTrue(api_auth.publish_token(self.path))

    def tearDown(self):
        self.tmp.cleanup()

    def test_token_written_and_random(self):
        self.assertEqual(self.path.read_text(encoding="ascii"), self.token)
        self.assertGreaterEqual(len(self.token), 40)
        self.assertNotEqual(api_auth.init_token(), self.token)  # new token each start

    def test_http_requires_token(self):
        refused = api_auth.check("GET", "/events", {}, frozenset())
        self.assertEqual(refused[0], HTTPStatus.UNAUTHORIZED)
        wrong = api_auth.check("GET", "/events", {api_auth.TOKEN_HEADER: "nope"}, frozenset())
        self.assertEqual(wrong[0], HTTPStatus.UNAUTHORIZED)
        self.assertIsNone(api_auth.check("POST", "/assess", {api_auth.TOKEN_HEADER: self.token}, frozenset()))

    def test_http_query_token_not_accepted(self):
        refused = api_auth.check("GET", "/events?token=" + self.token, {}, frozenset())
        self.assertEqual(refused[0], HTTPStatus.UNAUTHORIZED)

    def test_public_paths_and_preflight(self):
        self.assertIsNone(api_auth.check("GET", "/health", {}, frozenset()))
        self.assertIsNone(api_auth.check("GET", "/version", {}, frozenset()))
        self.assertIsNone(api_auth.check("OPTIONS", "/assess", {"Origin": "null"}, frozenset({"null"})))
        self.assertEqual(api_auth.check("POST", "/shutdown", {}, frozenset())[0], HTTPStatus.UNAUTHORIZED)

    def test_foreign_origin_refused_even_with_token(self):
        headers = {"Origin": "https://evil.example", api_auth.TOKEN_HEADER: self.token}
        self.assertEqual(api_auth.check("POST", "/assess", headers, frozenset({"null"}))[0], HTTPStatus.FORBIDDEN)
        self.assertEqual(api_auth.check("GET", "/health", {"Origin": "https://evil.example"}, frozenset())[0],
                         HTTPStatus.FORBIDDEN)

    def test_websocket_token_via_header_or_query(self):
        self.assertIsNone(api_auth.check("GET", "/?token=" + self.token, {}, frozenset(), websocket=True))
        self.assertIsNone(api_auth.check("GET", "/", {api_auth.TOKEN_HEADER: self.token}, frozenset(), websocket=True))
        self.assertEqual(api_auth.check("GET", "/", {}, frozenset(), websocket=True)[0], HTTPStatus.UNAUTHORIZED)
        self.assertEqual(api_auth.check("GET", "/health", {}, frozenset(), websocket=True)[0], HTTPStatus.UNAUTHORIZED)


class CorsTests(unittest.TestCase):
    def test_only_allowed_origin_echoed(self):
        self.assertEqual(api_auth.cors_headers("https://evil.example", frozenset({"null"})), {})
        self.assertEqual(api_auth.cors_headers(None, frozenset({"null"})), {})
        h = api_auth.cors_headers("null", frozenset({"null"}))
        self.assertEqual(h["Access-Control-Allow-Origin"], "null")
        self.assertIn(api_auth.TOKEN_HEADER, h["Access-Control-Allow-Headers"])

    def test_env_override(self):
        with mock.patch.dict(os.environ, {"CORS_ALLOWED_ORIGINS": "https://a.example, https://b.example"}):
            self.assertEqual(api_auth.allowed_origins(("null",)), frozenset({"https://a.example", "https://b.example"}))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CORS_ALLOWED_ORIGINS", None)
            self.assertEqual(api_auth.allowed_origins(("null",)), frozenset({"null"}))


class _StubWeb:
    """Just enough of aiohttp.web for http_middleware (no aiohttp needed to run these tests)."""
    class HTTPException(Exception):
        headers = {}

    class Response:
        def __init__(self, text="", status=200, headers=None):
            self.text, self.status, self.headers = text, status, dict(headers or {})

    @staticmethod
    def middleware(fn):
        return fn

    @classmethod
    def json_response(cls, data, status=200, headers=None):
        import json as _json
        return cls.Response(_json.dumps(data), status, headers)


class _Transport:
    def __init__(self, port):
        self.port = port

    def get_extra_info(self, key):
        return ("127.0.0.1", self.port) if key == "sockname" and self.port else None


class _Req:
    def __init__(self, path, headers, port=8867):
        self.method, self.path, self.path_qs, self.remote = "GET", path, path, "127.0.0.1"
        self.headers, self.transport = dict(headers), _Transport(port)


class HostCheckTests(unittest.TestCase):
    """Stage 10 security audit L2: Host must be 127.0.0.1:<port> / localhost:<port> (DNS rebinding)."""

    def setUp(self):
        self.token = api_auth.init_token()

    def test_pure_check(self):
        ok = {api_auth.TOKEN_HEADER: self.token}
        for host in ("127.0.0.1:8867", "localhost:8867", "LocalHost:8867"):
            self.assertIsNone(api_auth.check("GET", "/x", dict(ok, Host=host), frozenset(), port=8867), host)
        for host in ("evil.example:8867", "127.0.0.1:9999", "127.0.0.1", "localhost", "[::1]:8867", "", None):
            h = dict(ok) if host is None else dict(ok, Host=host)
            self.assertEqual(api_auth.check("GET", "/x", h, frozenset(), port=8867),
                             (HTTPStatus.FORBIDDEN, api_auth.HOST_REFUSED), host)
        # public paths too, and before the origin / token checks
        self.assertEqual(api_auth.check("GET", "/health", {"Host": "rebind.example:8867"}, frozenset(), port=8867)[1],
                         api_auth.HOST_REFUSED)
        self.assertIsNone(api_auth.check("GET", "/health", {"Host": "127.0.0.1:8867"}, frozenset(), port=8867))
        self.assertIsNone(api_auth.check("GET", "/health", {}, frozenset()))          # no port: pure, no Host check

    def test_middleware_reads_the_port_from_the_socket_and_fails_closed(self):
        calls = []

        async def handler(request):
            calls.append(request)
            return _StubWeb.Response("ok")
        saved = {k: sys.modules.get(k) for k in ("aiohttp", "aiohttp.web")}
        import types
        pkg = types.ModuleType("aiohttp")
        pkg.web = _StubWeb
        sys.modules["aiohttp"], sys.modules["aiohttp.web"] = pkg, _StubWeb
        try:
            mw = api_auth.http_middleware(frozenset())
            import asyncio
            run = asyncio.run
            r = run(mw(_Req("/health", {"Host": "127.0.0.1:8867"}), handler))
            self.assertEqual(r.status, 200)
            r = run(mw(_Req("/health", {"Host": "attacker.example:8867"}), handler))
            self.assertEqual((r.status, r.text), (403, '{"error": "host not allowed"}'))
            r = run(mw(_Req("/health", {"Host": "127.0.0.1:8867"}, port=None), handler))    # port unknown
            self.assertEqual(r.status, 403)
            r = run(mw(_Req("/events", {"Host": "localhost:8867", api_auth.TOKEN_HEADER: self.token}), handler))
            self.assertEqual(r.status, 200)
            self.assertEqual(len(calls), 2)
        finally:
            for k, m in saved.items():
                if m is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = m

    def test_websocket_hook(self):
        class Conn:
            local_address = ("127.0.0.1", 8765)
            remote_address = ("127.0.0.1", 50000)

            def respond(self, status, text):
                return (status, text)

        class Request:
            def __init__(self, host):
                self.path = "/?token=" + api_auth.token()
                self.headers = {"Host": host}
        hook = api_auth.ws_process_request(frozenset())
        self.assertIsNone(hook(Conn(), Request("127.0.0.1:8765")))
        with self.assertLogs(api_auth.logger, "WARNING"):
            self.assertEqual(hook(Conn(), Request("evil.example:8765"))[0], HTTPStatus.FORBIDDEN)
        Conn.local_address = None
        with self.assertLogs(api_auth.logger, "WARNING"):
            self.assertEqual(hook(Conn(), Request("127.0.0.1:8765"))[0], HTTPStatus.FORBIDDEN)   # fail closed


if __name__ == "__main__":
    unittest.main()
