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
    def test_loopback_kept(self):
        with mock.patch.dict(os.environ, {"ALLOW_REMOTE_ACCESS": ""}):
            self.assertEqual(api_auth.bind_host("127.0.0.1", "x"), "127.0.0.1")
            self.assertEqual(api_auth.bind_host("", "x"), "127.0.0.1")

    def test_wildcard_ignored_without_opt_in(self):
        with mock.patch.dict(os.environ, {"ALLOW_REMOTE_ACCESS": "false"}):
            with self.assertLogs(api_auth.logger, "WARNING"):
                self.assertEqual(api_auth.bind_host("0.0.0.0", "x"), "127.0.0.1")

    def test_wildcard_with_opt_in(self):
        with mock.patch.dict(os.environ, {"ALLOW_REMOTE_ACCESS": "true"}):
            with self.assertLogs(api_auth.logger, "WARNING"):
                self.assertEqual(api_auth.bind_host("0.0.0.0", "x"), "0.0.0.0")


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


if __name__ == "__main__":
    unittest.main()
