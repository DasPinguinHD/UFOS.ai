"""Market Research MR1 shell (market_research/SPEC.md §1, §5, §6, §10 MR1):
supervisor isolation (a crashing Research task → Research unavailable with
the reason, bounded restarts, while other app tasks — a dummy paper engine —
keep running), store migrations / WAL / corrupt-file recovery, the
GET /research/feeds/status shape, auth (401 without token) and main.py wiring.
Temp databases outside the repo; no network; aiohttp is stubbed.

Run from HedgeFund/backend:  python -m unittest tests.test_mr_shell -v
"""
import asyncio
import contextvars
import glob
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import api_auth
from market_research import ResearchService, api as mr_api, feeds as mr_feeds, limits, maintenance
from market_research import executor as mr_executor
from market_research import store
from market_research.hub import hub as mr_hub
from market_research.supervisor import ResearchSupervisor, RestartPolicy, describe_error
from strategy_platform.core.errors import UserFacingError

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MR_LOGGER = logging.getLogger("market_research")


def setUpModule():
    # crashes are logged with stacks on purpose; keep the test output readable
    _MR_LOGGER.setLevel(logging.CRITICAL + 1)


def tearDownModule():
    _MR_LOGGER.setLevel(logging.NOTSET)
FAST = RestartPolicy(delays=(0.01, 0.01, 0.01), max_restarts=2, window_seconds=60, healthy_after_seconds=60)
OK_STORE = lambda: {"state": "ok", "reason": None, "file": "market_research.db", "schema_version": 1}  # noqa: E731


def run(coro):
    return asyncio.run(coro)


async def wait_for(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.005)
    return pred()


# --- supervisor ------------------------------------------------------------------------------

class SupervisorTest(unittest.TestCase):
    def test_crashing_task_marks_unavailable_and_engine_keeps_running(self):
        async def scenario():
            ticks = {"n": 0}

            async def engine():          # stands in for the paper engine / kill switch / other app tasks
                while True:
                    ticks["n"] += 1
                    await asyncio.sleep(0.002)

            engine_task = asyncio.create_task(engine())
            calls = {"n": 0}

            async def crashing():
                calls["n"] += 1
                await asyncio.sleep(0)
                raise RuntimeError("internal detail secret-XYZ")

            sup = ResearchSupervisor(FAST, store_status=OK_STORE)
            sup.add("crashy", "Crashing task", crashing)
            sup.start()
            self.assertTrue(await wait_for(lambda: sup.tasks_status()[0]["state"] == "failed"))
            status = sup.status()
            before = ticks["n"]
            await asyncio.sleep(0.05)
            self.assertGreater(ticks["n"], before)                 # engine still running
            self.assertFalse(engine_task.done())
            engine_task.cancel()
            return status, calls["n"]

        status, calls = run(scenario())
        self.assertEqual(calls, 3)                                  # first run + max_restarts (2)
        self.assertEqual(status["research"]["state"], "unavailable")
        reason = status["research"]["reason"]
        self.assertIn('"crashy"', reason)
        self.assertIn("restart the HedgeFund backend", reason)
        self.assertIn("Trading is not affected.", reason)
        self.assertNotIn("secret-XYZ", json.dumps(status))          # raw exception text never exposed
        task = status["tasks"][0]
        self.assertEqual((task["state"], task["restarts"], task["max_restarts"]), ("failed", 2, 2))
        self.assertEqual(task["last_error"], "RuntimeError — see backend.log")
        self.assertIsNotNone(task["last_error_at"])
        self.assertLessEqual(len(reason), limits.MAX_TEXT_CHARS)

    def test_restarting_state_reason_and_recovery(self):
        async def scenario():
            gate = asyncio.Event()
            calls = {"n": 0}

            async def flaky():
                calls["n"] += 1
                if calls["n"] == 1:
                    raise UserFacingError("Alpaca keys are missing — enter them in Settings.")
                await gate.wait()
                await asyncio.sleep(3600)

            sup = ResearchSupervisor(RestartPolicy(delays=(0.2,), max_restarts=3, window_seconds=60,
                                                   healthy_after_seconds=60), store_status=OK_STORE)
            sup.add("flaky", "Flaky", flaky)
            sup.start()
            self.assertTrue(await wait_for(lambda: sup.tasks_status()[0]["state"] == "restarting"))
            during = sup.status()
            self.assertTrue(await wait_for(lambda: sup.tasks_status()[0]["state"] == "running" and calls["n"] == 2))
            after = sup.status()
            await sup.stop(1.0)
            return during, after, sup.status()

        during, after, stopped = run(scenario())
        self.assertEqual(during["research"]["state"], "unavailable")
        self.assertIn("Alpaca keys are missing", during["research"]["reason"])     # user_message is shown
        self.assertIn("restarting in", during["research"]["reason"])
        self.assertIn("attempt 1 of 3", during["research"]["reason"])
        self.assertIsNotNone(during["tasks"][0]["next_restart_at"])
        self.assertEqual(after["research"]["state"], "ok")
        self.assertEqual(after["tasks"][0]["restarts"], 1)
        self.assertEqual(stopped["research"]["state"], "stopped")
        self.assertEqual(stopped["tasks"][0]["state"], "stopped")

    def test_returning_task_counts_as_crash(self):
        async def scenario():
            async def returns():
                return None
            sup = ResearchSupervisor(FAST, store_status=OK_STORE)
            sup.add("quits", "Quits", returns)
            sup.start()
            await wait_for(lambda: sup.tasks_status()[0]["state"] == "failed")
            return sup.status()
        st = run(scenario())
        self.assertEqual(st["tasks"][0]["last_error"], "ended unexpectedly")
        self.assertEqual(st["research"]["state"], "unavailable")

    def test_healthy_run_resets_crash_count(self):
        clock = {"t": 0.0}

        async def scenario():
            calls = {"n": 0}

            async def task():
                calls["n"] += 1
                clock["t"] += 100.0           # every run "lasts" 100 s ≥ healthy_after
                raise RuntimeError("x")

            policy = RestartPolicy(delays=(0.0,), max_restarts=1, window_seconds=1000, healthy_after_seconds=50)
            sup = ResearchSupervisor(policy, store_status=OK_STORE, monotonic=lambda: clock["t"])
            sup.add("long_runs", "Long runs", task)
            sup.start()
            await wait_for(lambda: calls["n"] >= 6)
            st = sup.tasks_status()[0]
            await sup.stop(0.5)
            return st

        st = run(scenario())
        self.assertNotEqual(st["state"], "failed")
        self.assertGreaterEqual(st["restarts"], 5)

    def test_stop_is_bounded_even_if_a_task_ignores_cancel(self):
        async def scenario():
            async def stubborn():
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    await asyncio.sleep(0.3)     # slow cleanup, longer than the stop timeout, then returns
            sup = ResearchSupervisor(FAST, store_status=OK_STORE)
            sup.add("stubborn", "Stubborn", stubborn)
            sup.start()
            await asyncio.sleep(0.01)
            t0 = time.monotonic()
            await sup.stop(0.1)
            elapsed = time.monotonic() - t0
            st = sup.status()
            await asyncio.sleep(0.4)            # the task returns while stopping: no restart
            return elapsed, st, sup.tasks_status()[0]
        elapsed, st, task = run(scenario())
        self.assertLess(elapsed, 0.25)
        self.assertEqual(st["research"]["state"], "stopped")
        self.assertEqual((task["state"], task["restarts"]), ("stopped", 0))

    def test_start_without_loop_never_raises(self):
        sup = ResearchSupervisor(FAST, store_status=OK_STORE)
        sup.add("t", "T", lambda: asyncio.sleep(1))
        sup.start()                                   # no running loop here
        st = sup.status()
        self.assertEqual(st["research"]["state"], "unavailable")
        self.assertIn("Research could not start", st["research"]["reason"])

    def test_states_before_start_and_store_states(self):
        store_state = {"v": {"state": "not_opened", "reason": None, "file": "market_research.db",
                             "schema_version": None}}
        sup = ResearchSupervisor(FAST, store_status=lambda: store_state["v"])
        self.assertEqual(sup.status()["research"]["state"], "starting")

        async def scenario():
            sup.add("idle", "Idle", lambda: asyncio.sleep(3600))
            sup.start()
            await asyncio.sleep(0.01)
            a = sup.status()
            store_state["v"] = {"state": "unavailable", "reason": "The Research database is unavailable: x.",
                                "file": "market_research.db"}
            b = sup.status()
            store_state["v"] = OK_STORE()
            c = sup.status()
            await sup.stop(0.5)
            return a, b, c
        a, b, c = run(scenario())
        self.assertEqual(a["research"]["state"], "starting")             # store not opened yet
        self.assertEqual((b["research"]["state"], b["store"]["state"]), ("unavailable", "unavailable"))
        self.assertIn("Research database", b["research"]["reason"])
        self.assertIsNone(b["store"]["schema_version"])
        self.assertEqual(c["research"]["state"], "ok")
        self.assertIsNone(c["research"]["reason"])

    def test_store_status_failure_is_reported_not_raised(self):
        def boom():
            raise RuntimeError("db internals")
        sup = ResearchSupervisor(FAST, store_status=boom)
        st = sup.status()
        self.assertEqual(st["store"]["state"], "unavailable")
        self.assertNotIn("db internals", json.dumps(st))

    def test_add_validation(self):
        sup = ResearchSupervisor(FAST, store_status=OK_STORE)
        for bad in ("", "Bad", "x" * 41, "a-b", None):
            with self.assertRaises(ValueError):
                sup.add(bad, "L", lambda: asyncio.sleep(0))
        sup.add("ok_name", "L", lambda: asyncio.sleep(0))
        with self.assertRaises(ValueError):
            sup.add("ok_name", "L", lambda: asyncio.sleep(0))

    def test_system_exit_in_a_task_is_a_crash_not_a_process_exit(self):
        async def scenario():
            ticks = {"n": 0}

            async def engine():
                while True:
                    ticks["n"] += 1
                    await asyncio.sleep(0.002)
            eng = asyncio.create_task(engine())

            async def exits():
                raise SystemExit(3)
            sup = ResearchSupervisor(FAST, store_status=OK_STORE)
            sup.add("exits", "Exits", exits)
            sup.start()
            self.assertTrue(await wait_for(lambda: sup.tasks_status()[0]["state"] == "failed"))
            before = ticks["n"]
            await asyncio.sleep(0.03)
            alive = ticks["n"] > before and not eng.done()
            eng.cancel()
            return sup.status(), alive
        st, alive = run(scenario())                    # no SystemExit reaches asyncio.run
        self.assertTrue(alive)
        self.assertEqual(st["tasks"][0]["last_error"], "SystemExit — see backend.log")
        self.assertEqual(st["research"]["state"], "unavailable")

    def test_describe_error(self):
        self.assertEqual(describe_error(ValueError("secret")), "ValueError — see backend.log")
        self.assertEqual(describe_error(UserFacingError("Shown text.", detail="hidden")), "Shown text.")


# --- store -----------------------------------------------------------------------------------

class StoreCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="mr-store-")
        self.path = os.path.join(self.dir, "data", "market_research.db")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.addCleanup(store.close)
        store.close()


class StoreTest(StoreCase):
    def test_default_path(self):
        self.assertTrue(store.default_path().endswith(os.path.join("backend", "data", "market_research.db")))

    def test_not_opened(self):
        self.assertEqual(store.status()["state"], "not_opened")
        with self.assertRaises(store.StoreUnavailable) as cm:
            store.meta_get("x")
        self.assertIsInstance(cm.exception, UserFacingError)
        self.assertIn("market_research.db", cm.exception.user_message)
        self.assertIn("trading is not affected", cm.exception.user_message)

    def test_init_wal_migrations_idempotent(self):
        self.assertEqual(store.init(self.path), store.SCHEMA_VERSION)
        store.meta_set("k", "v")
        self.assertEqual(store.init(self.path), store.SCHEMA_VERSION)       # again: no error, data kept
        self.assertEqual(store.meta_get("k"), "v")
        conn = sqlite3.connect(self.path)
        try:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], store.SCHEMA_VERSION)
            store.migrate(conn)                                                # direct re-run is a no-op
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], store.SCHEMA_VERSION)
        finally:
            conn.close()
        st = store.status()
        self.assertEqual(st, {"state": "ok", "reason": None, "file": "market_research.db",
                              "schema_version": store.SCHEMA_VERSION})
        info = store.storage_info()
        self.assertEqual(info["file"], "market_research.db")
        self.assertNotIn(self.dir, json.dumps(st) + json.dumps(info))       # file name only, no path
        self.assertTrue({"meta", "audit"} <= set(info["rows"]))

    def test_corrupt_file_renamed_and_recreated(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "wb") as fh:
            fh.write(b"this is not a sqlite database" * 100)
        self.assertEqual(store.init(self.path), store.SCHEMA_VERSION)
        moved = glob.glob(self.path + ".corrupt-*")
        self.assertEqual(len(moved), 1)
        with open(moved[0], "rb") as fh:
            self.assertTrue(fh.read().startswith(b"this is not"))
        store.meta_set("fresh", 1)
        self.assertEqual(store.meta_get("fresh"), "1")

    def test_newer_schema_refused_file_untouched(self):
        os.makedirs(os.path.dirname(self.path))
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA user_version = 99")
        conn.commit()
        conn.close()
        with self.assertRaises(store.StoreUnavailable) as cm:
            store.init(self.path)
        self.assertIn("newer version", cm.exception.user_message)
        self.assertEqual(glob.glob(self.path + ".corrupt-*"), [])
        st = store.status()
        self.assertEqual(st["state"], "unavailable")
        self.assertIn("newer version", st["reason"])

    def test_newer_schema_file_is_byte_identical_afterwards(self):
        os.makedirs(os.path.dirname(self.path))
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TABLE t(x)")
        conn.execute("PRAGMA user_version = 99")
        conn.commit()
        conn.close()
        with open(self.path, "rb") as fh:
            before = hashlib.sha256(fh.read()).hexdigest()
        with self.assertRaises(store.StoreUnavailable):
            store.init(self.path)
        with open(self.path, "rb") as fh:
            self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), before)
        self.assertFalse(os.path.exists(self.path + "-wal"))
        conn = sqlite3.connect(self.path)
        try:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")   # WAL never set
        finally:
            conn.close()

    def test_quarantine_is_all_or_nothing(self):
        os.makedirs(os.path.dirname(self.path))
        for suffix, data in (("", b"main"), ("-wal", b"wal junk"), ("-shm", b"shm junk")):
            with open(self.path + suffix, "wb") as fh:
                fh.write(data)
        real_replace = os.replace
        target = os.path.abspath(self.path)

        def replace(src, dst):
            if os.path.abspath(src) == target:          # the main file is held open by another process
                raise PermissionError(13, "file in use")
            return real_replace(src, dst)
        with mock.patch.object(store.os, "replace", side_effect=replace):
            with self.assertRaises(PermissionError):
                store._quarantine(self.path)
        for suffix, data in (("", b"main"), ("-wal", b"wal junk"), ("-shm", b"shm junk")):
            with open(self.path + suffix, "rb") as fh:
                self.assertEqual(fh.read(), data, suffix)              # everything moved back
        self.assertEqual(glob.glob(self.path + ".corrupt-*"), [])
        moved = store._quarantine(self.path)                           # unlocked: all three move
        self.assertEqual(sorted(glob.glob(self.path + ".corrupt-*")),
                         sorted([moved, moved + "-shm", moved + "-wal"]))

    def test_locked_corrupt_file_is_not_recreated(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "wb") as fh:
            fh.write(b"not a database" * 100)
        with mock.patch.object(store.os, "replace", side_effect=PermissionError(13, "file in use")):
            with self.assertRaises(store.StoreUnavailable):
                store.init(self.path)
        with open(self.path, "rb") as fh:
            self.assertTrue(fh.read().startswith(b"not a database"))  # untouched, not replaced by a fresh DB
        self.assertEqual(store.status()["state"], "unavailable")

    def test_unopenable_path(self):
        blocker = os.path.join(self.dir, "data")
        with open(blocker, "w") as fh:            # a FILE where the data directory should be
            fh.write("x")
        with self.assertRaises(store.StoreUnavailable):
            store.init(self.path)
        self.assertEqual(store.status()["state"], "unavailable")
        self.assertTrue(os.path.isfile(blocker))  # nothing renamed

    def test_audit_and_prune(self):
        store.init(self.path)
        a = store.add_audit("system", "test", {"x": 1})
        self.assertEqual(store.list_audit()[0]["id"], a)
        conn = sqlite3.connect(self.path)
        conn.execute("INSERT INTO audit(ts, actor, action) VALUES('2020-01-01T00:00:00+00:00', 'system', 'old')")
        conn.commit()
        conn.close()
        deleted = store.prune(datetime(2026, 10, 16, tzinfo=timezone.utc))
        self.assertEqual({k: v for k, v in deleted.items() if v}, {"audit": 1})   # MR2a: v2 tables report 0
        self.assertEqual([r["action"] for r in store.list_audit()], ["test"])
        with self.assertRaises(ValueError):
            store.add_audit("hacker", "x")
        with self.assertRaises(ValueError):
            store.list_audit(0)
        with self.assertRaises(ValueError):
            store.add_audit("system", "x", {"bad": float("nan")})


class MaintenanceTest(StoreCase):
    def test_opens_and_prunes_then_sleeps_a_day(self):
        sleeps = []

        async def fake_sleep(s):
            sleeps.append(s)
            raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            run(maintenance.run(self.path, sleep=fake_sleep))
        self.assertTrue(store.is_open())
        self.assertEqual(sleeps, [limits.MAINTENANCE_INTERVAL_SECONDS])

    def test_unavailable_store_retries_without_crashing(self):
        with open(os.path.join(self.dir, "data"), "w") as fh:
            fh.write("x")
        sleeps = []

        async def fake_sleep(s):
            sleeps.append(s)
            if len(sleeps) >= 2:
                raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            run(maintenance.run(self.path, sleep=fake_sleep))
        self.assertEqual(sleeps, [limits.STORE_REOPEN_SECONDS] * 2)
        self.assertEqual(store.status()["state"], "unavailable")


# --- Research executor (review H1) ------------------------------------------------------------

class _NoDefaultExecutor(ThreadPoolExecutor):
    def submit(self, *a, **k):
        raise AssertionError("Research used asyncio's default executor")


class ExecutorTest(StoreCase):
    def test_package_never_uses_the_default_executor(self):
        import ast
        pkg = os.path.join(_BACKEND, "market_research")
        for name in sorted(os.listdir(pkg)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(pkg, name), encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    attr = node.func.attr
                    self.assertNotEqual(attr, "to_thread", name)
                    if attr == "run_in_executor":
                        self.assertEqual(name, "executor.py")
                        first = node.args[0] if node.args else None
                        self.assertFalse(isinstance(first, ast.Constant) and first.value is None, name)
                if isinstance(node, ast.Name) and node.id == "ThreadPoolExecutor":
                    self.assertEqual(name, "executor.py")

    def test_maintenance_runs_in_the_research_pool(self):
        sleeps = []

        async def fake_sleep(sec):
            sleeps.append(sec)
            raise asyncio.CancelledError

        async def scenario():
            asyncio.get_running_loop().set_default_executor(_NoDefaultExecutor())
            await maintenance.run(self.path, sleep=fake_sleep)
        with self.assertRaises(asyncio.CancelledError):
            run(scenario())
        self.assertTrue(store.is_open())

    def test_pool_bounded_named_and_context_copied(self):
        var = contextvars.ContextVar("mr_test_var", default="unset")
        self.addCleanup(mr_executor.shutdown)

        async def scenario():
            var.set("research")
            import threading
            return await mr_executor.run_blocking(lambda: (var.get(), threading.current_thread().name))
        value, thread = run(scenario())
        self.assertEqual(value, "research")
        self.assertTrue(thread.startswith("research"))
        self.assertEqual(mr_executor.executor()._max_workers, limits.RESEARCH_EXECUTOR_WORKERS)
        self.assertLessEqual(limits.RESEARCH_EXECUTOR_WORKERS, 4)
        self.assertLessEqual(limits.RESEARCH_ACQUIRE_TIMEOUT_SECONDS, 30)


# --- API -------------------------------------------------------------------------------------

TOP_KEYS = {"schema", "generated_at", "research", "store", "tasks", "feeds", "live_count", "feed_count",
            "demand", "budget"}
FEED_KEYS = {"id", "label", "feed_label", "state", "message", "attempt", "connected_since", "last_message_at",
             "symbols", "symbol_limit"}
TASK_KEYS = {"name", "label", "state", "restarts", "max_restarts", "last_error", "last_error_at",
             "next_restart_at"}


class _Resp:
    def __init__(self, text, status, headers=None):
        self.text, self.status, self.headers = text, status, dict(headers or {})


def _stub_aiohttp():
    web = types.ModuleType("aiohttp.web")
    web.json_response = lambda data, status=200, dumps=json.dumps, headers=None: _Resp(dumps(data), status, headers)
    web.middleware = lambda f: f

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
    return pkg, web


class _Request:
    def __init__(self, path="/research/feeds/status", query=None, headers=None):
        self.method = "GET"
        self.path = path
        self.query = dict(query or {})
        self.path_qs = path + ("?" + "&".join(f"{k}={v}" for k, v in self.query.items()) if self.query else "")
        self.headers = dict(headers or {})
        self.headers.setdefault("Host", "127.0.0.1:8867")          # stage 10 L2: Host check on the listening port
        self.transport = _Sock()
        self.match_info = {}
        self.remote = "127.0.0.1"
        self.can_read_body = False


class _Sock:
    @staticmethod
    def get_extra_info(key):
        return ("127.0.0.1", 8867) if key == "sockname" else None


class StubbedAiohttp:
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


def _service():
    sup = ResearchSupervisor(FAST, store_status=OK_STORE)
    return ResearchService(supervisor=sup)


class ApiTest(unittest.TestCase):
    def test_payload_shape_mr1(self):
        svc = _service()
        p = mr_api.status_payload(svc.supervisor, svc.feeds,
                                  now=datetime(2026, 10, 16, 12, 0, 5, tzinfo=timezone.utc))
        self.assertEqual(set(p), TOP_KEYS)
        self.assertEqual(p["schema"], 1)
        self.assertEqual(p["generated_at"], "2026-10-16T12:00:05+00:00")
        self.assertEqual(set(p["research"]), {"state", "reason", "since"})
        self.assertEqual(p["research"]["state"], "starting")             # supervisor not started
        self.assertEqual(set(p["store"]), {"state", "reason", "file", "schema_version"})
        names = [t["name"] for t in p["tasks"]]
        self.assertEqual(names[0], "store_maintenance")
        self.assertIn("asset_refresh", names)                              # MR2a
        self.assertIn("market_data_hub", names)                            # MR2b
        self.assertEqual(set(p["tasks"][0]), TASK_KEYS)
        self.assertEqual(p["tasks"][0]["state"], "pending")
        self.assertEqual([f["id"] for f in p["feeds"]], ["stocks", "crypto", "news"])
        for f in p["feeds"]:
            self.assertEqual(set(f), FEED_KEYS)
            self.assertEqual(f["state"], "off")
            self.assertEqual(f["message"], mr_hub.NOT_RUNNING_MESSAGE)    # MR2b: hub task not started
        self.assertEqual([f["symbol_limit"] for f in p["feeds"]], [30, None, None])
        self.assertEqual((p["live_count"], p["feed_count"]), (0, 3))
        self.assertEqual(p["demand"], {"views": 0, "alerts": 0, "streams_wanted": False,
                                       "note": mr_feeds.DEMAND_NOTE})
        b = p["budget"]                                                     # MR2a: measured by the gate
        self.assertEqual({k: b[k] for k in ("source", "shared_per_minute", "research_max_per_minute")},
                         {"source": "alpaca", "shared_per_minute": 150, "research_max_per_minute": 60})
        self.assertIsInstance(b["research_used_last_minute"], int)
        self.assertTrue(b["waiting_seconds"] is None or isinstance(b["waiting_seconds"], float))
        json.loads(mr_api.dumps(p))                                         # strict JSON

    def test_handler_rejects_query_params(self):
        api = mr_api.ResearchApi(_service())
        status, payload = run(api.feeds_status({"x": "1"}, None, {}))
        self.assertEqual(status, 400)
        self.assertEqual(payload["errors"], {"x": "unknown parameter"})
        status, payload = run(api.feeds_status({}, None, {}))
        self.assertEqual(status, 200)

    def test_register_route_and_generic_500(self):
        svc = _service()
        with StubbedAiohttp() as web:
            app = web.Application()
            mr_api.register(app, svc)
            routes = app.router.routes
            self.assertEqual(set(routes), {(m, p) for m, p, _n, _b in mr_api.ROUTES})
            self.assertIn(("GET", "/research/feeds/status"), routes)
            for _m, path in routes:
                self.assertTrue(path.startswith("/research/"))
            resp = run(routes[("GET", "/research/feeds/status")](_Request()))
            self.assertEqual(resp.status, 200)
            self.assertEqual(set(json.loads(resp.text)), TOP_KEYS)

            def boom():
                raise RuntimeError("internal secret detail")
            svc.supervisor.status = boom
            resp = run(routes[("GET", "/research/feeds/status")](_Request()))
            self.assertEqual(resp.status, 500)
            self.assertNotIn("secret", resp.text)
            self.assertEqual(json.loads(resp.text), {"error": mr_api.INTERNAL_ERROR})

    def test_route_requires_token(self):
        api_auth.init_token()
        self.addCleanup(lambda: setattr(api_auth, "_token", None))
        path = "/research/feeds/status"
        self.assertNotIn(path, api_auth.PUBLIC_PATHS)
        self.assertEqual(api_auth.check("GET", path, {}, frozenset())[0], 401)
        self.assertEqual(api_auth.check("GET", path + "?token=" + api_auth.token(), {}, frozenset())[0], 401)
        self.assertIsNone(api_auth.check("GET", path, {api_auth.TOKEN_HEADER: api_auth.token()}, frozenset()))
        # the same through the real middleware wrapping the registered route
        svc = _service()
        with StubbedAiohttp() as web:
            app = web.Application()
            mr_api.register(app, svc)
            handler = app.router.routes[("GET", path)]
            mw = api_auth.http_middleware(frozenset())
            resp = run(mw(_Request(path), handler))
            self.assertEqual(resp.status, 401)
            self.assertEqual(json.loads(resp.text), {"error": "missing or invalid API token"})
            resp = run(mw(_Request(path, headers={api_auth.TOKEN_HEADER: "wrong"}), handler))
            self.assertEqual(resp.status, 401)
            resp = run(mw(_Request(path, headers={api_auth.TOKEN_HEADER: api_auth.token()}), handler))
            self.assertEqual(resp.status, 200)
            resp = run(mw(_Request(path, headers={api_auth.TOKEN_HEADER: api_auth.token(),
                                                  "Origin": "https://evil.example"}), handler))
            self.assertEqual(resp.status, 403)

    def test_service_lifecycle_with_real_store(self):
        d = tempfile.mkdtemp(prefix="mr-svc-")
        self.addCleanup(shutil.rmtree, d, True)
        self.addCleanup(store.close)
        store.close()

        async def scenario():
            svc = ResearchService(store_path=os.path.join(d, "data", "market_research.db"))
            svc.start()
            ok = await wait_for(lambda: svc.status_payload()["research"]["state"] == "ok")
            p = svc.status_payload()
            await svc.shutdown(1.0)
            return ok, p, svc.status_payload()

        ok, p, after = run(scenario())
        self.assertTrue(ok)
        self.assertEqual(p["store"], {"state": "ok", "reason": None, "file": "market_research.db",
                                      "schema_version": store.SCHEMA_VERSION})
        self.assertEqual(p["tasks"][0]["state"], "running")
        self.assertEqual(after["research"]["state"], "stopped")


class MainWiringTest(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(_BACKEND, "main.py"), encoding="utf-8") as fh:
            self.src = fh.read()

    def test_install_guarded_and_ordered(self):
        s = self.src
        self.assertIn("research = _install_research(app)", s)
        self.assertIn('app.router.add_route("*", "/research/{tail:.*}", _research_unavailable)', s)
        self.assertIn('"kind": "ResearchUnavailable"', s)
        self.assertLess(s.index("v2_routes.register(app)"), s.index("research = _install_research(app)"))
        self.assertLess(s.index("research = _install_research(app)"), s.index("await runner.setup()"))
        self.assertLess(s.index("    engine.start()\n"), s.index("research.start()"))
        self.assertIn("await research.shutdown()", s)
        self.assertLess(s.index("await research.shutdown()"), s.index("call_later(0.3, _exit_now)"))
        # Research tasks are owned by the supervisor, never part of main's gather()
        gather_block = s[s.index("tasks = [asyncio.create_task(_run_collector_forever"):]
        self.assertNotIn("research", gather_block.split("async with websockets.serve")[0])
        # the package is imported lazily inside the guard, never at module top
        top = s.split("def _install_research", 1)[0]
        self.assertNotIn("import market_research", top)

    def test_install_failure_leaves_503_stub(self):
        import importlib
        saved = sys.modules.get("market_research")
        with StubbedAiohttp() as web:
            # simulate a broken package: install raises
            broken = types.ModuleType("market_research")

            def bad_install(app):
                raise ImportError("broken")
            broken.install = bad_install
            sys.modules["market_research"] = broken
            try:
                spec_src = self.src.split("def _install_research(app):", 1)[1].split("\n\n\nasync def main", 1)[0]
                ns = {"web": web, "logger": logging.getLogger("t"),
                      "RESEARCH_LOAD_ERROR": "Market Research failed to load — see backend.log. "
                                             "Trading is not affected."}
                exec("def _install_research(app):" + spec_src, ns)
                app = web.Application()
                with self.assertLogs("t", level="ERROR"):
                    self.assertIsNone(ns["_install_research"](app))
                handler = app.router.routes[("*", "/research/{tail:.*}")]
                resp = run(handler(_Request()))
                self.assertEqual(resp.status, 503)
                self.assertEqual(json.loads(resp.text)["kind"], "ResearchUnavailable")
            finally:
                if saved is None:
                    sys.modules.pop("market_research", None)
                else:
                    sys.modules["market_research"] = saved
                importlib.invalidate_caches()


class SpecTest(unittest.TestCase):
    def test_spec_documents_status_shape(self):
        with open(os.path.join(_BACKEND, "market_research", "SPEC.md"), encoding="utf-8") as fh:
            spec = fh.read()
        self.assertIn("GET /research/feeds/status", spec)
        for key in TOP_KEYS | FEED_KEYS | TASK_KEYS:
            self.assertIn(f'"{key}"', spec)


if __name__ == "__main__":
    unittest.main()
