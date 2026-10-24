"""Daily AI caps of the Global Monitoring backend (task B, 2026-10-17; contract
UFOS.ai/docs/ai-budgets-spec.md §3/§4): ai_caps.DailyCaps over the real gms.db storage (temp file),
fail-closed cases, the /ai/budgets payload + PUT, and the main.py wiring (checked statically — main.py
needs aiohttp/openai, which the tests do not install). No network.

Run from Global Monitoring/backend:
    python -m unittest discover -s tests -t .
"""
import ast
import asyncio
import os
import shutil
import sys
import tempfile
import unittest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import ai_caps  # noqa: E402
import storage  # noqa: E402

MAIN = os.path.join(BACKEND, "main.py")


def _main_source() -> str:
    with open(MAIN, encoding="utf-8") as f:
        return f.read()


def _main_caps():
    """AI_CAPS / MODULE_ID / MODULE_LABEL of main.py, evaluated without importing main.py."""
    tree = ast.parse(_main_source())
    ns = {"ai_caps": ai_caps}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) in ("AI_CAPS", "MODULE_ID", "MODULE_LABEL")
                                                for t in node.targets):
            exec(compile(ast.Module(body=[node], type_ignores=[]), MAIN, "exec"), ns)
    return ns


def _function_source(name: str) -> str:
    src = _main_source()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError(name)


class GmsCaps(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gms-ai-caps-")
        self.path = os.path.join(self.tmp, "gms.db")
        self.store = storage.Storage(self.path)
        self.ns = _main_caps()

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def caps(self, store=None):
        kv_get, kv_set, kv_add, available = ai_caps.storage_kv(store or self.store)
        return ai_caps.DailyCaps(self.ns["MODULE_ID"], self.ns["MODULE_LABEL"], self.ns["AI_CAPS"], kv_get, kv_set,
                                 kv_add=kv_add, available=available)

    def test_defaults_from_main(self):
        caps = {c.key: c for c in self.ns["AI_CAPS"]}
        self.assertEqual(set(caps), {"assess", "localnews_summary"})
        self.assertEqual((caps["assess"].default_usd, caps["assess"].max_usd, caps["assess"].estimate_usd),
                         (1.0, 50.0, 0.005))
        self.assertEqual(caps["localnews_summary"].default_usd, 1.0)
        self.assertEqual(caps["assess"].cost_log, (("Global Monitoring", "assess"),))
        self.assertEqual((self.ns["MODULE_ID"], self.ns["MODULE_LABEL"]), ("global-monitoring", "Global Monitoring"))

    def test_spend_and_limit_persist_in_gms_db(self):
        caps = self.caps()
        caps.settle(caps.reserve("assess"), 0.25)
        self.assertEqual(caps.handle_put({"key": "assess", "limit_usd": 0.3})[0], 200)
        self.store.close()
        self.store = storage.Storage(self.path)          # backend restart
        caps2 = self.caps()
        self.assertAlmostEqual(caps2.spent("assess"), 0.25)
        self.assertEqual(caps2.limit("assess"), 0.3)
        with self.assertRaises(ai_caps.BudgetBlocked) as cm:   # 0.25 + 0.005 + ... 0.3: one more fits
            for _ in range(20):
                caps2.reserve("assess")
        self.assertEqual(cm.exception.reason, "reached")
        p = caps2.payload()
        self.assertEqual(p["module"], "global-monitoring")
        self.assertEqual([b["key"] for b in p["budgets"]], ["assess", "localnews_summary"])
        self.assertEqual(self.store.get_meta("ai_cap_limit:assess"), 0.3)   # readable by the meta API too

    def test_in_memory_store_refuses(self):
        self.store.in_memory = True                        # the storage's in-memory stand-in after a bad file
        caps = self.caps()
        with self.assertRaises(ai_caps.BudgetBlocked) as cm:
            caps.reserve("assess")
        self.assertEqual(cm.exception.reason, "store")
        self.assertEqual(caps.handle_put({"key": "assess", "limit_usd": 2})[0], 503)
        self.assertEqual(caps.payload()["budgets"][0]["status"], "error")

    def test_broken_connection_fails_closed(self):
        caps = self.caps()
        self.store._conn.close()                           # every SQL call fails now
        with self.assertLogs(level="WARNING"):
            with self.assertRaises(ai_caps.BudgetBlocked) as cm:
                caps.reserve("assess")
        self.assertEqual(cm.exception.reason, "store")

    def test_booking_failure_pauses(self):
        caps = self.caps()
        res = caps.reserve("localnews_summary")
        self.store._conn.close()
        with self.assertLogs(level="ERROR"):
            caps.settle(res, 0.01)
        self.store = storage.Storage(self.path)
        kv = ai_caps.storage_kv(self.store)
        caps._get, caps._set, caps._add = kv[0], kv[1], kv[2]
        with self.assertRaises(ai_caps.BudgetBlocked) as cm:
            caps.reserve("assess")
        self.assertEqual(cm.exception.reason, "unrecorded")

    def test_guarded_call_books(self):
        caps = self.caps()

        async def call():
            return "text", "model", {"cost_usd": 0.001}

        asyncio.run(ai_caps.guarded_call(caps, "assess", None, call))
        self.assertAlmostEqual(caps.spent("assess"), 0.001)


class MainWiring(unittest.TestCase):
    def test_assess_checks_after_cache_before_context_and_call(self):
        src = _function_source("_assess")
        cache = src.index("find_recent_ai_output")
        check = src.index('budgets.status("assess")')
        context = src.index("assess_context.build_context")
        call = src.index('ai_caps.guarded_call(')
        self.assertTrue(cache < check < context < call)
        self.assertIn("status=402", src)
        self.assertLess(src.index("except ai_caps.BudgetBlocked"), src.rindex("except Exception as ex"))

    def test_localnews_summary_guarded(self):
        src = _function_source("_localnews_summary")
        self.assertLess(src.index('budgets.status("localnews_summary")'), src.index("fetch_local_news("))
        self.assertIn('ai_caps.guarded_call(\n                budgets, "localnews_summary"', src)

    def test_routes_and_origin_refusal(self):
        src = _main_source()
        self.assertIn('add_get("/ai/budgets", _ai_budgets_get)', src)
        self.assertIn('add_put("/ai/budgets", _ai_budgets_put)', src)
        put = _function_source("_ai_budgets_put")
        self.assertIn("ai_caps.ORIGIN_REFUSED", put)
        self.assertIn("ai_caps.read_json_body", put)


class ReadBody(unittest.TestCase):
    class _Content:
        def __init__(self, raw):
            self.raw = raw

        async def read(self, n):
            return self.raw[:n]

    class _Req:
        def __init__(self, raw, length=None):
            self.content = ReadBody._Content(raw)
            self.content_length = length

    def test_body_limits(self):
        run = asyncio.run
        self.assertEqual(run(ai_caps.read_json_body(self._Req(b'{"key": "assess", "limit_usd": 1}'))),
                         ({"key": "assess", "limit_usd": 1}, None))
        self.assertEqual(run(ai_caps.read_json_body(self._Req(b"x" * 10, length=5000)))[1][0], 400)
        self.assertEqual(run(ai_caps.read_json_body(self._Req(b" " * 5000)))[1][0], 400)
        self.assertEqual(run(ai_caps.read_json_body(self._Req(b"{nope")))[1][1]["error"], "invalid JSON body")
        self.assertEqual(run(ai_caps.read_json_body(self._Req(b""))), (None, None))


if __name__ == "__main__":
    unittest.main()
