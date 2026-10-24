"""Task B (2026-10-17, contract UFOS.ai/docs/ai-budgets-spec.md): ai_caps.DailyCaps (identical copy in
every backend) + the HedgeFund /ai/budgets aggregator (fund, explain, ideation, loop hook, newsroom caps)
+ the Newsroom cap guard. No network, temp hedgefund.db outside the mount.

Run from HedgeFund/backend:  python -m unittest tests.test_ai_budgets -v
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

import ai_budgets  # noqa: E402
import ai_caps  # noqa: E402
from strategy_platform.storage import db  # noqa: E402

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
ENTRY_FIELDS = {"key", "label", "group", "period", "period_id", "limit_usd", "default_usd", "min", "max", "step",
                "spent_usd", "remaining_usd", "enabled", "status", "message", "editable", "applies", "notes",
                "cost_log"}


class DictKV:
    def __init__(self):
        self.d, self.fail_get, self.fail_set = {}, False, False

    def get(self, k):
        if self.fail_get:
            raise RuntimeError("disk on fire")
        return self.d.get(k)

    def set(self, k, v):
        if self.fail_set:
            raise RuntimeError("read-only")
        self.d[k] = v


def make_caps(kv=None, clock=None, audit=None, available=None):
    kv = kv or DictKV()
    caps = ai_caps.DailyCaps("test-module", "Test Module", [
        ai_caps.CapDef("a", "Feature A", 1.0, 0.1, max_usd=50.0, group="G", cost_log=(("Mod", "feat-a"),)),
        ai_caps.CapDef("b", "Feature B", 0.5, 0.05, max_usd=10.0)],
        kv.get, kv.set, available=available, audit=audit, clock=clock or (lambda: NOW))
    return caps, kv


class CapsCore(unittest.TestCase):
    def test_default_reserve_settle_books_reported_cost(self):
        caps, kv = make_caps()
        self.assertEqual(caps.limit("a"), 1.0)
        res = caps.reserve("a")
        self.assertEqual(caps.settle(res, 0.0123), 0.0123)
        self.assertAlmostEqual(caps.spent("a"), 0.0123)
        self.assertIn("ai_cap_spend:a:2026-10-17", kv.d)
        self.assertEqual(caps.settle(res, 5.0), 0.0)            # settled once only

    def test_missing_cost_books_estimate(self):
        caps, _ = make_caps()
        caps.settle(caps.reserve("a"), None)
        self.assertAlmostEqual(caps.spent("a"), 0.1)
        caps.settle(caps.reserve("a"), float("nan"))
        self.assertAlmostEqual(caps.spent("a"), 0.2)

    def test_reached_counts_in_flight_estimates(self):
        caps, _ = make_caps()
        held = [caps.reserve("a") for _ in range(10)]           # 10 x 0.1 = the whole 1.0
        with self.assertRaises(ai_caps.BudgetBlocked) as cm:
            caps.reserve("a")
        self.assertEqual(cm.exception.reason, "reached")
        self.assertIn("AI budgets (main window → Settings)", cm.exception.message)
        caps.release(held[0])
        caps.release(held[0])                                   # idempotent
        caps.reserve("a")

    def test_reached_by_spend(self):
        caps, kv = make_caps()
        kv.d["ai_cap_spend:a:2026-10-17"] = repr(0.95)
        with self.assertRaises(ai_caps.BudgetBlocked) as cm:
            caps.reserve("a")
        self.assertEqual(cm.exception.payload()["code"], "budget")
        self.assertEqual(cm.exception.payload()["budget_key"], "a")
        self.assertIn("$0.9500 of $1.00", cm.exception.message)
        # the next UTC day starts at 0
        caps2, _ = make_caps(kv, clock=lambda: NOW + timedelta(days=1))
        caps2.reserve("a")

    def test_off(self):
        caps, kv = make_caps()
        kv.d["ai_cap_limit:a"] = "0.0"
        with self.assertRaises(ai_caps.BudgetBlocked) as cm:
            caps.reserve("a")
        self.assertEqual(cm.exception.reason, "off")
        self.assertIn("switched off", cm.exception.message)

    def test_unreadable_and_invalid_fail_closed(self):
        caps, kv = make_caps()
        kv.d["ai_cap_spend:a:2026-10-17"] = "garbage"
        with self.assertRaises(ai_caps.BudgetBlocked) as cm:
            caps.reserve("a")
        self.assertEqual(cm.exception.reason, "unreadable")
        kv.d["ai_cap_spend:a:2026-10-17"] = "-1"
        self.assertRaises(ai_caps.BudgetBlocked, caps.reserve, "a")
        del kv.d["ai_cap_spend:a:2026-10-17"]
        kv.d["ai_cap_limit:a"] = "999"
        with self.assertRaises(ai_caps.BudgetBlocked) as cm:
            caps.reserve("a")
        self.assertEqual(cm.exception.reason, "invalid")
        kv.d.pop("ai_cap_limit:a")
        kv.fail_get = True
        with self.assertRaises(ai_caps.BudgetBlocked) as cm:
            caps.reserve("a")
        self.assertEqual(cm.exception.reason, "store")

    def test_unrecorded_pauses_until_restart(self):
        caps, kv = make_caps()
        res = caps.reserve("a")
        kv.fail_set = True
        with self.assertLogs(level="ERROR"):
            caps.settle(res, 0.01)
        kv.fail_set = False
        for key in ("a", "b"):
            with self.assertRaises(ai_caps.BudgetBlocked) as cm:
                caps.reserve(key)
            self.assertEqual(cm.exception.reason, "unrecorded")
        self.assertEqual(caps.entry("b")["status"], "paused")

    def test_store_unavailable(self):
        caps, _ = make_caps(available=lambda: False)
        with self.assertRaises(ai_caps.BudgetBlocked) as cm:
            caps.reserve("a")
        self.assertEqual(cm.exception.reason, "store")
        self.assertEqual(caps.entry("a")["status"], "error")
        self.assertRaises(ai_caps.StoreUnavailable, caps.put, "a", 2.0)

    def test_exception_charging(self):
        caps, _ = make_caps()

        class APITimeoutError(Exception):
            pass

        res = caps.reserve("a")
        try:
            try:
                raise APITimeoutError("Request timed out")
            except APITimeoutError as inner:
                raise RuntimeError("AI request failed (error): the model did not answer in time") from inner
        except RuntimeError as ex:
            self.assertAlmostEqual(caps.charge_for_exception(res, ex), 0.1)
        res = caps.reserve("a")
        self.assertEqual(caps.charge_for_exception(res, RuntimeError("401 invalid api key")), 0.0)
        self.assertAlmostEqual(caps.spent("a"), 0.1)

    def test_guarded_call(self):
        caps, _ = make_caps()

        async def ok():
            return "text", "model", {"cost_usd": 0.03}

        async def boom():
            raise RuntimeError("provider error")

        self.assertEqual(asyncio.run(ai_caps.guarded_call(caps, "a", None, ok))[0], "text")
        self.assertAlmostEqual(caps.spent("a"), 0.03)
        with self.assertRaises(RuntimeError):
            asyncio.run(ai_caps.guarded_call(caps, "a", None, boom))
        self.assertAlmostEqual(caps.spent("a"), 0.03)
        self.assertEqual(caps._reserved["a"], 0.0)

    def test_payload_shape(self):
        caps, kv = make_caps()
        kv.d["ai_cap_spend:a:2026-10-17"] = "0.25"
        p = caps.payload()
        self.assertEqual((p["schema"], p["module"], p["label"], p["day"], p["month"]),
                         (1, "test-module", "Test Module", "2026-10-17", "2026-10"))
        a = p["budgets"][0]
        self.assertEqual(set(a), ENTRY_FIELDS)
        self.assertEqual((a["limit_usd"], a["spent_usd"], a["remaining_usd"], a["status"], a["period"]),
                         (1.0, 0.25, 0.75, "ok", "day"))
        self.assertEqual(a["cost_log"], [{"module": "Mod", "feature": "feat-a"}])
        json.dumps(p, allow_nan=False)

    def test_put_validation(self):
        caps, kv = make_caps()
        bad = [None, [], {"key": "a"}, {"limit_usd": 1}, {"key": "zzz", "limit_usd": 1},
               {"key": "a", "limit_usd": True}, {"key": "a", "limit_usd": "2"}, {"key": "a", "limit_usd": -1},
               {"key": "a", "limit_usd": 50.01}, {"key": "b", "limit_usd": 11}, {"key": "a", "limit_usd": 1, "x": 1},
               {"key": "a", "limit_usd": float("inf")}, {"key": "a", "limit_usd": 10 ** 400}, {"key": 5, "limit_usd": 1}]
        for body in bad:
            status, payload = caps.handle_put(body)
            self.assertEqual(status, 400, body)
            self.assertIn("errors", payload)
        status, payload = caps.handle_put({"key": "b", "limit_usd": 10})
        self.assertEqual(status, 200)
        self.assertEqual(payload["changed"], {"key": "b", "old": 0.5, "new": 10.0})
        self.assertEqual(kv.d["ai_cap_limit:b"], "10.0")
        self.assertEqual(caps.handle_put({"key": "a", "limit_usd": 0})[0], 200)
        self.assertEqual(caps.entry("a")["status"], "blocked")
        self.assertEqual(caps.handle_put({"key": "a", "limit_usd": 1}, origin="null")[0], 403)
        self.assertEqual(caps.handle_put({"key": "a", "limit_usd": 1}, query={"x": "1"})[0], 400)
        self.assertEqual(caps.handle_get({"x": "1"})[0], 400)
        self.assertEqual(caps.handle_get()[0], 200)

    def test_put_audit_first_and_repair(self):
        seen = []
        caps, kv = make_caps(audit=seen.append)
        kv.d["ai_cap_limit:a"] = "nonsense"
        self.assertEqual(caps.put("a", 2.0)["old"], None)          # repair path
        self.assertEqual(seen[-1]["new"], 2.0)

        def failing(change):
            raise RuntimeError("audit down")

        caps2, kv2 = make_caps(audit=failing)
        status, payload = caps2.handle_put({"key": "a", "limit_usd": 3})
        self.assertEqual(status, 503)
        self.assertNotIn("ai_cap_limit:a", kv2.d)
        kv3 = DictKV()
        caps3, _ = make_caps(kv3)
        kv3.fail_set = True
        self.assertEqual(caps3.handle_put({"key": "a", "limit_usd": 3})[0], 503)


class FakeStore:
    def __init__(self, spent=0.0):
        self.spent = spent

    def ai_spend_today(self):
        return self.spent


class FakeEngine:
    def __init__(self, limit=1.0, spent=0.1, version=3):
        self.limit, self.store, self.calls, self.version = limit, FakeStore(spent), [], version

    def get_params(self):
        return {"version": self.version, "params": {"ai_daily_budget_usd": self.limit}}

    def update_params(self, partial, actor="user"):
        from fund.engine import EngineError
        self.calls.append((partial, actor))
        v = partial["ai_daily_budget_usd"]
        if v == 42.0:
            raise EngineError(503, "The parameter store is unavailable.")
        self.limit = v
        return {"version": 4}


class Aggregator(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-budgets-")
        db.close()
        db.init(os.path.join(self.tmp, "hedgefund.db"))
        ai_budgets._newsroom = None
        ai_budgets._reports = None
        self.engine = FakeEngine()
        self.agg = ai_budgets.HedgeFundBudgets(self.engine, provider_modules=("tests_fake_loop_absent",),
                                               clock=lambda: NOW)

    def tearDown(self):
        db.close()
        ai_budgets._newsroom = None
        ai_budgets._reports = None
        for name in ("tests_fake_loop_ok", "tests_fake_loop_broken"):
            sys.modules.pop(name, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def keys(self, payload):
        return [b["key"] for b in payload["budgets"]]

    def test_get_all_keys(self):
        status, p = self.agg.handle_get()
        self.assertEqual(status, 200)
        self.assertEqual((p["module"], p["label"], p["schema"]), ("hedgefund", "Hedge Fund", 1))
        self.assertEqual(self.keys(p), ["fund", "explain", "ideation_month", "ideation_day", "reports",
                                        "newsroom_analysis", "newsroom_insider_summary"])
        by = {b["key"]: b for b in p["budgets"]}
        for b in p["budgets"]:
            self.assertEqual(set(b), ENTRY_FIELDS, b["key"])
            self.assertEqual(b["status"], "ok", b)
        self.assertEqual((by["fund"]["limit_usd"], by["fund"]["spent_usd"], by["fund"]["applies"]),
                         (1.0, 0.1, "next_cycle"))
        self.assertEqual((by["explain"]["limit_usd"], by["explain"]["max"]), (1.0, 50.0))
        self.assertEqual((by["ideation_month"]["period"], by["ideation_month"]["limit_usd"],
                          by["ideation_month"]["max"]), ("month", 10.0, 200.0))
        self.assertEqual(by["ideation_day"]["limit_usd"], 2.0)
        self.assertEqual((by["newsroom_analysis"]["limit_usd"], by["newsroom_insider_summary"]["limit_usd"]),
                         (5.0, 1.0))
        self.assertIn({"module": "HedgeFund", "feature": "sp-studio"}, by["ideation_day"]["cost_log"])
        json.dumps(p, allow_nan=False)
        self.assertEqual(ai_budgets.PROVIDER_MODULES, ("strategy_platform.research.loop",))

    def test_fund_states(self):
        self.engine.store.spent = None                      # unreadable -> error entry, no stand-in
        by = {b["key"]: b for b in self.agg.payload()["budgets"]}
        self.assertEqual(by["fund"]["status"], "error")
        self.assertIsNone(by["fund"]["spent_usd"])
        self.engine.store.spent, self.engine.limit = 0.0, 0.0
        by = {b["key"]: b for b in self.agg.payload()["budgets"]}
        self.assertEqual(by["fund"]["status"], "blocked")
        self.assertIn("switched off", by["fund"]["message"])
        agg = ai_budgets.HedgeFundBudgets(None, provider_modules=(), clock=lambda: NOW)
        self.assertEqual({b["key"]: b for b in agg.payload()["budgets"]}["fund"]["status"], "error")

    def test_fund_defaults_from_a_disabled_store_are_an_error(self):
        from newsroom import db as ndb
        self.engine.version = 0                              # effective_params() fell back to the defaults
        ndb.close()                                          # store disabled
        fund = {b["key"]: b for b in self.agg.payload()["budgets"]}["fund"]
        self.assertEqual((fund["status"], fund["limit_usd"]), ("error", None))
        self.assertIn("disabled", fund["message"])
        ndb.init(os.path.join(self.tmp, "newsroom.db"))      # readable store, nothing saved yet -> real value
        try:
            fund = {b["key"]: b for b in self.agg.payload()["budgets"]}["fund"]
            self.assertEqual((fund["status"], fund["limit_usd"]), ("ok", 1.0))
        finally:
            ndb.close()

    def test_tiny_limit_rejected(self):
        status, p = self.agg.handle_put({"key": "newsroom_analysis", "limit_usd": 0.0000004})
        self.assertEqual((status, p["errors"]["limit_usd"]), (400, "at least 0.000001, or exactly 0 = off"))
        self.assertEqual(self.agg.handle_put({"key": "explain", "limit_usd": 0.0000004})[0], 400)
        self.assertEqual(self.agg.handle_put({"key": "newsroom_analysis", "limit_usd": 0.000001})[0], 200)
        self.assertEqual(self.agg.handle_put({"key": "newsroom_analysis", "limit_usd": 0})[0], 200)

    def test_put_fund_delegates(self):
        status, p = self.agg.handle_put({"key": "fund", "limit_usd": 2.5})
        self.assertEqual(status, 200)
        self.assertEqual(self.engine.calls[-1], ({"ai_daily_budget_usd": 2.5}, "user"))
        self.assertEqual(p["changed"], {"key": "fund", "old": 1.0, "new": 2.5})
        self.assertEqual(self.agg.handle_put({"key": "fund", "limit_usd": 50.5})[0], 400)
        status, p = self.agg.handle_put({"key": "fund", "limit_usd": 42})
        self.assertEqual((status, p["error"]), (503, "The parameter store is unavailable."))

    def test_put_explain_audited(self):
        from strategy_platform.backtest import explain
        self.assertEqual(self.agg.handle_put({"key": "explain", "limit_usd": 3})[0], 200)
        self.assertEqual(explain.daily_budget_usd(), 3.0)
        self.assertEqual(db.list_audit(5, "explain_settings_changed")[0]["detail"]["new"], {"daily_budget_usd": 3.0})

    def test_put_ideation_keeps_other_value(self):
        from strategy_platform.research import studio
        self.assertEqual(self.agg.handle_put({"key": "ideation_day", "limit_usd": 7})[0], 200)
        self.assertEqual(studio.limits(), (10.0, 7.0))
        self.assertEqual(self.agg.handle_put({"key": "ideation_month", "limit_usd": 150})[0], 200)
        self.assertEqual(studio.limits(), (150.0, 7.0))
        self.assertEqual(len(db.list_audit(5, "ideation_settings_changed")), 2)
        self.assertEqual(self.agg.handle_put({"key": "ideation_month", "limit_usd": 201})[0], 400)

    def test_put_newsroom_audited_and_refused_without_audit(self):
        status, p = self.agg.handle_put({"key": "newsroom_analysis", "limit_usd": 0.5})
        self.assertEqual(status, 200)
        row = db.list_audit(5, "ai_budget_changed")[0]
        self.assertEqual((row["actor"], row["detail"]["key"], row["detail"]["old"], row["detail"]["new"]),
                         ("user", "newsroom_analysis", 5.0, 0.5))
        with mock.patch.object(db, "add_audit", side_effect=db.StorageUnavailable("audit down")):
            status, p = self.agg.handle_put({"key": "newsroom_analysis", "limit_usd": 9})
        self.assertEqual(status, 503)
        self.assertEqual(ai_budgets.newsroom_caps().limit("newsroom_analysis"), 0.5)

    def test_put_refusals(self):
        self.assertEqual(self.agg.handle_put({"key": "fund", "limit_usd": 1}, origin="https://evil.example")[0], 403)
        self.assertEqual(self.agg.handle_put({"key": "nope", "limit_usd": 1})[1]["errors"]["key"],
                         "unknown budget key")
        self.assertEqual(self.agg.handle_put("text")[0], 400)
        self.assertEqual(self.agg.handle_get({"a": "b"})[0], 400)

    def test_loop_hook_present(self):
        mod = types.ModuleType("tests_fake_loop_ok")
        mod.store = {"loop": 3.0}

        def entries(now=None):
            return [ai_budgets._entry("loop", "Discovery loop", "Strategy Platform", "day", "2026-10-17",
                                      mod.store["loop"], 3.0, 0.0, 20.0, 0.1, 0.5, "", (("HedgeFund", "sp-loop"),))]

        def put(key, limit_usd, actor="user"):
            if limit_usd == 13:
                raise ValueError("unlucky number")
            mod.store[key] = limit_usd

        mod.ai_budget_entries, mod.ai_budget_put = entries, put
        sys.modules["tests_fake_loop_ok"] = mod
        agg = ai_budgets.HedgeFundBudgets(self.engine, provider_modules=("tests_fake_loop_ok",), clock=lambda: NOW)
        self.assertIn("loop", self.keys(agg.payload()))
        self.assertEqual(agg.handle_put({"key": "loop", "limit_usd": 4})[0], 200)
        self.assertEqual(mod.store["loop"], 4.0)
        self.assertEqual(agg.handle_put({"key": "loop", "limit_usd": 21})[0], 400)       # entry bounds
        status, p = agg.handle_put({"key": "loop", "limit_usd": 13})
        self.assertEqual((status, p["errors"]["limit_usd"]), (400, "unlucky number"))

    def test_loop_hook_broken_is_an_error_entry(self):
        mod = types.ModuleType("tests_fake_loop_broken")

        def entries(now=None):
            raise RuntimeError("secret internals")

        mod.ai_budget_entries = entries
        sys.modules["tests_fake_loop_broken"] = mod
        agg = ai_budgets.HedgeFundBudgets(self.engine, provider_modules=("tests_fake_loop_broken",),
                                          clock=lambda: NOW)
        with self.assertLogs(level="ERROR"):
            p = agg.payload()
        loop = {b["key"]: b for b in p["budgets"]}["loop"]
        self.assertEqual((loop["status"], loop["message"]), ("error", ai_budgets.READ_FAILED))
        self.assertNotIn("secret", json.dumps(p))
        self.assertEqual(agg.handle_put({"key": "loop", "limit_usd": 1})[0], 400)
        self.assertEqual(len(p["budgets"]), 8)          # the other sources still answer (+ stage-9 reports)

    def test_db_unavailable_entries_error(self):
        db.close()
        p = self.agg.payload()
        by = {b["key"]: b for b in p["budgets"]}
        for k in ("explain", "ideation_month", "newsroom_analysis"):
            self.assertEqual(by[k]["status"], "error", k)
        self.assertEqual(by["fund"]["status"], "ok")

    def test_newsroom_guard(self):
        async def call():
            return "analysis", "m", {"cost_usd": 0.04}

        self.assertEqual(asyncio.run(ai_budgets.guarded_newsroom_call(
            "newsroom_analysis", ai_budgets.analysis_estimate("premium"), call))[0], "analysis")
        self.assertAlmostEqual(ai_budgets.newsroom_caps().spent("newsroom_analysis"), 0.04)
        self.assertEqual(ai_budgets.analysis_estimate("standard"), 0.02)
        ai_budgets.newsroom_caps().put("newsroom_insider_summary", 0.0, actor="user")
        calls = []

        async def never():
            calls.append(1)
            return "x", "m", {}

        with self.assertRaises(ai_caps.BudgetBlocked) as cm:
            asyncio.run(ai_budgets.guarded_newsroom_call("newsroom_insider_summary", None, never))
        self.assertEqual(calls, [])                   # blocked BEFORE the call
        self.assertEqual(cm.exception.payload()["reason"], "off")

    def test_main_wiring(self):
        with open(os.path.join(BACKEND, "main.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertEqual(src.count("ai_budgets.guarded_newsroom_call("), 2)
        self.assertEqual(src.count("except ai_caps.BudgetBlocked as ex:"), 2)
        self.assertIn("ai_budgets.register(app, engine)", src)


class CopiesIdentical(unittest.TestCase):
    def test_ai_caps_copies_identical(self):
        root = os.path.dirname(os.path.dirname(BACKEND))
        def read(path):
            with open(path, "rb") as f:
                return f.read()

        mine = read(os.path.join(BACKEND, "ai_caps.py"))
        for rel in (("Global Monitoring", "backend", "ai_caps.py"), ("LiveStreamAgent", "backend", "ai_caps.py")):
            path = os.path.join(root, *rel)
            self.assertTrue(os.path.exists(path), path)
            self.assertEqual(read(path), mine, path)


if __name__ == "__main__":
    unittest.main()
