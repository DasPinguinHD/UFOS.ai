"""strategy_platform/api/routes.py — the /v2 routes (SPEC §4): handlers
called directly (no aiohttp), route registration with a stubbed aiohttp,
error mapping (UserFacingError -> 503 {error, kind}, validation -> 400
{error, errors}), the data-layer development-order guard, and main.py wiring.
Fake Credential Manager (MemoryStore), temp hedgefund.db, no network.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_api -v
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import types
import unittest

from strategy_platform import secrets
from strategy_platform.api import routes
from strategy_platform.core.errors import DataSourceUnavailable, SecretMissing
from strategy_platform.storage import db

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE_REGISTRY = "sp_test_fake_registry"


def call(name, body=routes.NO_BODY, query=None, match=None):
    return asyncio.run(getattr(routes.V2Api(), name)(query or {}, body, match or {}))


class ApiCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sp-api-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        db.close()
        db.init(os.path.join(self.dir, "hedgefund.db"))
        self.addCleanup(db.close)
        self.store = secrets.MemoryStore({secrets.target("fred-api-key"): "fred-VALUE-123"})
        secrets.set_store(self.store)
        self.addCleanup(secrets.set_store, None)
        saved_module = routes.DATA_LAYER_MODULE
        self.addCleanup(setattr, routes, "DATA_LAYER_MODULE", saved_module)
        self.addCleanup(sys.modules.pop, FAKE_REGISTRY, None)

    def fake_registry(self, test_source):
        mod = types.ModuleType(FAKE_REGISTRY)
        mod.test_source = test_source
        sys.modules[FAKE_REGISTRY] = mod
        routes.DATA_LAYER_MODULE = FAKE_REGISTRY


class KeysTest(ApiCase):
    def test_get_keys_never_returns_values(self):
        status, payload = call("keys")
        self.assertEqual(status, 200)
        by_name = {k["name"]: k for k in payload["keys"]}
        self.assertEqual(set(by_name), set(secrets.SECRETS))
        self.assertTrue(by_name["fred-api-key"]["present"])
        self.assertFalse(by_name["openrouter-api-key"]["present"])
        self.assertNotIn("fred-VALUE-123", json.dumps(payload))

    def test_keys_changed_writes_audit(self):
        status, payload = call("keys_changed", {"name": "fred-api-key", "action": "imported"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        row = db.list_audit(1)[0]
        self.assertEqual((row["actor"], row["action"], row["detail"]), ("user", "key_imported", {"name": "fred-api-key"}))

    def test_keys_changed_validation(self):
        status, payload = call("keys_changed", {"name": "fred-api-key", "action": "set", "value": "secret!"})
        self.assertEqual(status, 400)
        self.assertIn("value", payload["errors"])
        self.assertNotIn("secret!", json.dumps(payload))
        self.assertEqual(call("keys_changed", {"name": "nope", "action": "set"})[1]["errors"].keys(), {"name"})
        self.assertEqual(call("keys_changed", {"name": "fred-api-key", "action": "x"})[1]["errors"].keys(), {"action"})
        self.assertEqual(call("keys_changed", {"name": "fred-api-key", "action": "set", "x": 1})[0], 400)
        self.assertEqual(call("keys_changed")[0], 400)
        self.assertEqual(call("keys_changed", [1])[0], 400)
        self.assertEqual(db.list_audit(), [])

    def test_keys_changed_without_storage_is_503(self):
        db.close()
        status, payload = call("keys_changed", {"name": "fred-api-key", "action": "removed"})
        self.assertEqual(status, 503)
        self.assertEqual(payload["kind"], "StorageUnavailable")
        self.assertIn("hedgefund.db", payload["error"])


class DataSourcesTest(ApiCase):
    def test_list(self):
        self.assertEqual(call("data_sources"), (200, {"sources": []}))
        db.data_source_set("equity_bars", "alpaca", licence="personal_use", licence_note="account holder only")
        db.data_source_set("macro", "fred")
        db.data_source_status_update("equity_bars", False, "Alpaca is unavailable")
        status, payload = call("data_sources")
        self.assertEqual(status, 200)
        eq, macro = payload["sources"]
        self.assertEqual(set(eq), {"data_type", "source", "status", "last_ok_at", "last_error_at", "message",
                                   "licence"})
        self.assertEqual((eq["status"], eq["message"]), ("error", "Alpaca is unavailable"))
        self.assertEqual(eq["licence"], {"terms": "personal_use", "note": "account holder only"})
        self.assertIsNone(macro["licence"])

    def test_storage_failure_is_503(self):
        db.close()
        status, payload = call("data_sources")
        self.assertEqual((status, payload["kind"]), (503, "StorageUnavailable"))


class DataTestTest(ApiCase):
    def test_validation(self):
        self.assertEqual(call("data_test", {"source": "yahoo"})[0], 400)
        self.assertEqual(call("data_test", {"source": "alpaca", "x": 1})[0], 400)
        self.assertEqual(call("data_test")[0], 400)

    def test_data_layer_not_installed(self):
        routes.DATA_LAYER_MODULE = "strategy_platform.data.registry_not_there_for_test"
        status, payload = call("data_test", {"source": "alpaca"})
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], "data layer not installed")

    def test_missing_dependency_inside_registry_is_not_hidden(self):
        def fake_import(name):
            raise ModuleNotFoundError("No module named 'alpaca'", name="alpaca")
        saved = routes._load_registry
        try:
            import importlib
            real = importlib.import_module
            importlib.import_module = lambda name: fake_import(name) if name == routes.DATA_LAYER_MODULE else real(name)
            try:
                with self.assertRaises(ModuleNotFoundError):
                    call("data_test", {"source": "alpaca"})
            finally:
                importlib.import_module = real
        finally:
            routes._load_registry = saved

    def test_result_and_errors(self):
        result = {"ok": False, "checks": [{"name": "SPY daily bar", "ok": False, "message": "key missing"}]}
        self.fake_registry(lambda source: result)
        self.assertEqual(call("data_test", {"source": "alpaca"}), (200, result))

        def raises(source):
            raise DataSourceUnavailable("alpaca", "equity_bars", "HTTP 503")
        self.fake_registry(raises)
        status, payload = call("data_test", {"source": "alpaca"})
        self.assertEqual((status, payload["kind"]), (503, "DataSourceUnavailable"))
        self.assertIn("no other source is used", payload["error"])

        def missing(source):
            raise SecretMissing("alpaca-paper-key-id", "Alpaca paper API key ID")
        self.fake_registry(missing)
        self.assertEqual(call("data_test", {"source": "alpaca"})[1]["kind"], "SecretMissing")

        self.fake_registry(lambda source: ["not", "a", "dict"])
        self.assertEqual(call("data_test", {"source": "alpaca"})[0], 500)


# --- registration with a stubbed aiohttp ------------------------------------------------

class _Resp:
    def __init__(self, text, status):
        self.text, self.status = text, status


def _stub_aiohttp():
    web = types.ModuleType("aiohttp.web")
    web.json_response = lambda data, status=200, dumps=json.dumps: _Resp(dumps(data), status)

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
    return pkg, web


class _Request:
    def __init__(self, text=None):
        self._text = text
        self.can_read_body = text is not None
        self.query = {}
        self.match_info = {}

    async def text(self):
        return self._text or ""


class RegisterTest(ApiCase):
    def test_register_and_wrapper(self):
        pkg, web = _stub_aiohttp()
        saved = {k: sys.modules.get(k) for k in ("aiohttp", "aiohttp.web")}
        sys.modules["aiohttp"], sys.modules["aiohttp.web"] = pkg, web
        try:
            app = web.Application()
            routes.register(app)
        finally:
            for k, m in saved.items():
                if m is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = m
        r = app.router.routes
        self.assertEqual(set(r), {(m, p) for m, p, _n, _b in routes.ROUTES})
        self.assertTrue({("GET", "/v2/settings/keys"), ("POST", "/v2/settings/keys/changed"),
                         ("GET", "/v2/data/sources"), ("POST", "/v2/data/test")} <= set(r))   # + stage-2 routes
        resp = asyncio.run(r[("GET", "/v2/settings/keys")](_Request()))
        self.assertEqual(resp.status, 200)
        resp = asyncio.run(r[("POST", "/v2/settings/keys/changed")](_Request("{bad json")))
        self.assertEqual(resp.status, 400)
        resp = asyncio.run(r[("POST", "/v2/settings/keys/changed")](_Request('{"name": "fred-api-key", "action": "set"}')))
        self.assertEqual((resp.status, json.loads(resp.text)["ok"]), (200, True))

        def boom(source):
            raise RuntimeError("internal detail with secret fred-VALUE-123")
        self.fake_registry(boom)
        resp = asyncio.run(r[("POST", "/v2/data/test")](_Request('{"source": "alpaca"}')))
        self.assertEqual(resp.status, 500)
        self.assertNotIn("fred-VALUE-123", resp.text)
        self.assertEqual(json.loads(routes.dumps({"x": float("nan")})), {"x": None})


class MainWiringTest(unittest.TestCase):
    def test_main_mounts_v2_and_drops_env_keys(self):
        with open(os.path.join(_BACKEND, "main.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("v2_routes.register(app)", src)
        self.assertNotIn("log_ignored_env_secrets", src)          # stage 10 S10-E: no .env at all
        self.assertIn("sp_storage.init", src)
        for name in ("OPENROUTER_API_KEY", "FRED_API_KEY", "SEC_EDGAR_USER_AGENT"):
            self.assertNotIn(name, src.split('"""', 2)[2])   # code after the module docstring


if __name__ == "__main__":
    unittest.main()
