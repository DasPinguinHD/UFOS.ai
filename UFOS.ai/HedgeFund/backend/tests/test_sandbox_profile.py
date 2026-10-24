"""Sandbox profile (strategy_platform/SPEC.md "Sandbox profile", user decision 2026-10-23): tools/sandbox.py on / off /
status (consistent copies via the SQLite backup API, marker, refused while the backend answers /health, real DB
hashes compared at off), every database path in data/sandbox, broker accounts hard-blocked (no request, state
"blocked in sandbox"), the old engine's autopilot + scheduled cycles off, AI cost lines tagged, seeding / force-decay
refused in normal mode, the fixture label in the payloads, normal mode unchanged. Temp folders only — the real
data folder is never touched. Offline."""
import asyncio
import contextlib
import glob
import http.server
import importlib.util
import io
import json
import os
import shutil
import socket
import sqlite3
import tempfile
import threading
import unittest
from datetime import timedelta
from unittest import mock

try:
    from sp_broker_testkit import BrokerCase
    from sp_trading_testkit import FRI, TradingCase, make_strategy
except ImportError:  # pragma: no cover
    from tests.sp_broker_testkit import BrokerCase
    from tests.sp_trading_testkit import FRI, TradingCase, make_strategy

import sandbox_profile as sp
from sandbox_profile import SandboxError

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_tool():
    spec = importlib.util.spec_from_file_location("ufos_sandbox_tool", os.path.join(BACKEND, "tools", "sandbox.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tool = _load_tool()


def not_running():
    return False


def make_db(path, rows):
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE t (v TEXT)")
    c.executemany("INSERT INTO t VALUES (?)", [(r,) for r in rows])
    c.commit()
    c.close()


def rows_of(path):
    c = sqlite3.connect(path)
    try:
        return [r[0] for r in c.execute("SELECT v FROM t ORDER BY rowid")]
    finally:
        c.close()


def use_tmp_root(case, marker=None):
    """Points sandbox_profile at a temp data folder (optionally with a marker) for this test."""
    root = tempfile.mkdtemp(prefix="sbx-")
    case.addCleanup(shutil.rmtree, root, True)
    if marker is not None:
        os.makedirs(os.path.join(root, "sandbox"), exist_ok=True)
        with open(os.path.join(root, sp.MARKER_NAME), "w", encoding="utf-8") as f:
            json.dump(marker, f)
    sp.use_root(root)
    case.addCleanup(sp.use_root, None)
    return root


SANDBOX_MARKER = {"mode": "sandbox", "created_at": "2026-10-23T12:00:00+00:00", "files": {}}


class ToolCase(unittest.TestCase):
    def setUp(self):
        self.root = use_tmp_root(self)
        for name in sp.DB_FILES:
            make_db(os.path.join(self.root, name), [name, "real row"])

    def on(self):
        m = tool.cmd_on(self.root, not_running, seed_fn=lambda: {"stub": True})   # seeding: AutoSeedTest
        sp.reset()
        return m


class ToolTest(ToolCase):
    def test_on_makes_consistent_copies_and_marker(self):
        self.assertEqual(sp.mode(), "normal")
        m = self.on()
        self.assertTrue(sp.is_sandbox())
        self.assertEqual(sp.data_dir(), os.path.join(self.root, "sandbox"))
        with open(os.path.join(self.root, sp.MARKER_NAME), encoding="utf-8") as f:
            stored = json.load(f)
        self.assertEqual(stored["created_at"], m["created_at"])
        for name in sp.DB_FILES:
            real, copy = os.path.join(self.root, name), os.path.join(self.root, "sandbox", name)
            self.assertEqual(rows_of(copy), [name, "real row"])
            self.assertEqual(stored["files"][name]["source_sha256"], sp.sha256_file(real))
            self.assertEqual(stored["files"][name]["copy_sha256"], sp.sha256_file(copy))

    def test_on_refused_while_backend_runs_or_twice(self):
        with self.assertRaises(SandboxError) as c:
            tool.cmd_on(self.root, lambda: True)
        self.assertIn("close UFOS.ai first", c.exception.user_message)
        self.assertFalse(os.path.exists(os.path.join(self.root, sp.MARKER_NAME)))
        self.assertFalse(os.path.exists(os.path.join(self.root, "sandbox", "hedgefund.db")))
        self.on()
        with self.assertRaises(SandboxError):
            tool.cmd_on(self.root, not_running)

    def test_existing_sandbox_copy_is_set_aside_never_deleted(self):
        os.makedirs(os.path.join(self.root, "sandbox"))
        make_db(os.path.join(self.root, "sandbox", "hedgefund.db"), ["old sandbox"])
        m = self.on()
        stale = glob.glob(os.path.join(self.root, "sandbox", "hedgefund.db.*.stale"))
        self.assertEqual(len(stale), 1)
        self.assertEqual(rows_of(stale[0]), ["old sandbox"])
        self.assertIn(os.path.basename(stale[0]), m["set_aside"])

    def test_off_real_db_untouched_hash_identical_and_sandbox_kept(self):
        self.on()
        before = {n: sp.sha256_file(os.path.join(self.root, n)) for n in sp.DB_FILES}
        c = sqlite3.connect(sp.db_file("hedgefund.db"))      # the click-test writes the SANDBOX copy only
        c.execute("INSERT INTO t VALUES ('sandbox write')")
        c.commit()
        c.close()
        out = tool.cmd_off(self.root, not_running)
        self.assertTrue(out["identical"])
        self.assertTrue(all(v["identical"] for v in out["comparison"].values()))
        self.assertEqual(before, {n: sp.sha256_file(os.path.join(self.root, n)) for n in sp.DB_FILES})
        self.assertEqual(rows_of(os.path.join(self.root, "hedgefund.db")), ["hedgefund.db", "real row"])
        self.assertFalse(os.path.exists(os.path.join(self.root, sp.MARKER_NAME)))
        stale = glob.glob(os.path.join(self.root, sp.MARKER_NAME + ".*.stale"))
        self.assertEqual(len(stale), 1)
        with open(stale[0], encoding="utf-8") as f:
            self.assertTrue(json.load(f)["off"]["identical"])
        self.assertIn("sandbox write", rows_of(os.path.join(self.root, "sandbox", "hedgefund.db")))
        sp.reset()
        self.assertEqual(sp.mode(), "normal")

    def test_old_marker_with_an_empty_wal_hash_still_compares_identical(self):
        self.on()
        path = os.path.join(self.root, sp.MARKER_NAME)
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
        m["files"]["hedgefund.db"]["source_wal_sha256"] = tool.EMPTY_SHA256          # written by the 2026-10-23 tool
        with open(path, "w", encoding="utf-8") as f:
            json.dump(m, f)
        wal = os.path.join(self.root, "hedgefund.db-wal")
        if os.path.exists(wal):                                  # the empty -wal of on's read-only open goes away
            os.replace(wal, wal + ".gone.stale")
        out = tool.cmd_off(self.root, not_running)
        self.assertTrue(out["identical"], out)
        self.assertEqual(out["comparison"]["hedgefund.db"]["note"], "wal: empty → absent (no data change)")

    def test_empty_and_absent_wal_or_shm_are_the_same_state_but_wal_data_is_a_change(self):
        self.on()
        real = os.path.join(self.root, "newsroom.db")
        open(real + "-wal", "wb").close()                      # an empty -wal / -shm appears (a reader opened it)
        open(real + "-shm", "wb").close()
        self.assertTrue(tool.cmd_status(self.root, not_running)["real_db_unchanged_so_far"]["newsroom.db"])
        out = tool.cmd_off(self.root, not_running)
        self.assertTrue(out["identical"], out)
        self.assertEqual(out["comparison"]["newsroom.db"]["note"], "wal: absent → empty (no data change)")
        sp.reset()
        os.replace(real + "-wal", real + "-wal.x.stale")
        self.on()
        with open(real + "-wal", "wb") as f:                     # a NON-empty WAL difference is a real change
            f.write(b"frame")
        out = tool.cmd_off(self.root, not_running)
        self.assertFalse(out["identical"])
        self.assertFalse(out["comparison"]["newsroom.db"]["identical"])

    def test_off_reports_a_changed_real_db(self):
        self.on()
        make_db(os.path.join(self.root, "other.db"), ["x"])
        c = sqlite3.connect(os.path.join(self.root, "newsroom.db"))      # "the real backend ran in between"
        c.execute("INSERT INTO t VALUES ('real write')")
        c.commit()
        c.close()
        out = tool.cmd_off(self.root, not_running)
        self.assertFalse(out["identical"])
        self.assertFalse(out["comparison"]["newsroom.db"]["identical"])
        self.assertTrue(out["comparison"]["hedgefund.db"]["identical"])

    def test_off_refused_while_backend_runs(self):
        self.on()
        with self.assertRaises(SandboxError):
            tool.cmd_off(self.root, lambda: True)
        self.assertTrue(os.path.exists(os.path.join(self.root, sp.MARKER_NAME)))

    def test_status(self):
        self.assertEqual(tool.cmd_status(self.root, not_running)["mode"], "normal")
        self.on()
        st = tool.cmd_status(self.root, not_running)
        self.assertEqual(st["mode"], "sandbox")
        self.assertTrue(all(st["real_db_unchanged_so_far"].values()))

    def test_broken_marker_or_missing_copy_stops_the_start(self):
        with open(os.path.join(self.root, sp.MARKER_NAME), "w") as f:
            f.write("{not json")
        sp.reset()
        with self.assertRaises(SandboxError):
            sp.startup()
        os.replace(os.path.join(self.root, sp.MARKER_NAME), os.path.join(self.root, "broken.stale"))
        self.on()
        copy = os.path.join(self.root, "sandbox", "hedgefund.db")
        os.replace(copy, copy + ".moved.stale")
        sp.reset()
        with self.assertRaises(SandboxError) as c:
            sp.state()
        self.assertIn("never falls back", c.exception.user_message)

    def test_seed_and_force_decay_refused_in_normal_mode(self):
        from strategy_platform.storage import db
        for argv in (["seed-incubation-test"], ["force-decay", "x"]):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), mock.patch.object(db, "init") as init:
                self.assertEqual(tool.main(argv), tool.EXIT_ERROR)
            self.assertIn("refused in normal mode", err.getvalue())
            init.assert_not_called()                      # not even a database is opened
        import sandbox_fixtures
        with self.assertRaises(SandboxError):
            sandbox_fixtures.seed_incubation_test()
        with self.assertRaises(SandboxError):
            sandbox_fixtures.force_decay("x")


class AutoSeedTest(unittest.TestCase):
    """User 2026-10-24: ``on`` seeds the incubation fixture right after the copy; a failure is reported (sandbox ON)."""

    def setUp(self):
        from strategy_platform.storage import db
        self.root = use_tmp_root(self)
        db.close()
        db.init(os.path.join(self.root, "hedgefund.db"))       # a REAL schema "real" hedgefund.db (temp)
        db.close()
        self.addCleanup(db.close)
        for name in ("newsroom.db", "market_research.db"):
            make_db(os.path.join(self.root, name), [name])

    def test_on_seeds_the_fixture_in_the_sandbox_only(self):
        from strategy_platform.storage import db
        real = os.path.join(self.root, "hedgefund.db")
        m = tool.cmd_on(self.root, not_running)
        self.assertTrue(m["seed"]["ok"], m["seed"])
        r = m["seed"]["result"]
        self.assertEqual((r["name"], r["state"], r["created"]), (sp.FIXTURE_NAME, "validated", True))
        self.assertEqual(os.path.dirname(db.db_path()), os.path.join(self.root, "sandbox"))
        self.assertEqual(sp.sha256_file(real), m["files"]["hedgefund.db"]["source_sha256"])   # real file untouched
        c = sqlite3.connect(real)
        try:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM strategies WHERE name = ?",
                                       (sp.FIXTURE_NAME,)).fetchone()[0], 0)
        finally:
            c.close()
        out = tool.cmd_off(self.root, not_running)
        self.assertTrue(out["identical"], out["comparison"])

    def test_failed_seeding_is_reported_and_the_sandbox_stays_on(self):
        def boom():
            raise SandboxError("the holdout window is not open: test")
        m = tool.cmd_on(self.root, not_running, seed_fn=boom)
        self.assertEqual(m["seed"], {"ok": False, "error": "the holdout window is not open: test"})
        self.assertTrue(os.path.exists(os.path.join(self.root, sp.MARKER_NAME)))
        tool.cmd_off(self.root, not_running)
        err, out = io.StringIO(), io.StringIO()
        with mock.patch.object(tool, "backend_running", lambda: False), \
                mock.patch.object(tool, "cmd_seed", boom), contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(out):
            self.assertEqual(tool.main(["on"]), tool.EXIT_SEED_FAILED)
        self.assertIn("Sandbox mode is ON, but seeding the incubation fixture FAILED", err.getvalue())
        self.assertIn("seed-incubation-test", err.getvalue())
        self.assertTrue(os.path.exists(os.path.join(self.root, sp.MARKER_NAME)))

    def test_venv_guard(self):
        self.assertIsNone(tool.venv_problem(os.path.join(BACKEND, ".venv")))
        msg = tool.venv_problem(os.path.join(self.root, "system-python"))
        self.assertIn(r"run with .venv\Scripts\python.exe tools\sandbox.py", msg)


class HealthCheckTest(unittest.TestCase):
    def test_backend_running_uses_the_health_endpoint(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.assertFalse(tool.backend_running(port))             # connection refused -> not running

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status": "ok"}')

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.assertTrue(tool.backend_running(srv.server_address[1]))


class PathsTest(unittest.TestCase):
    def test_normal_mode_paths_unchanged(self):
        from market_research import store as mr_store
        from newsroom import db as ndb
        from strategy_platform.storage import db
        if os.path.exists(os.path.join(BACKEND, "data", sp.MARKER_NAME)):
            self.skipTest("the real sandbox marker is active on this machine")
        sp.use_root(None)
        self.addCleanup(sp.use_root, None)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ndb.ENV_PATH, None)
            data = os.path.join(BACKEND, "data")
            self.assertEqual(db.default_path(), os.path.join(data, "hedgefund.db"))
            self.assertEqual(ndb.default_path(), os.path.join(data, "newsroom.db"))
            self.assertEqual(mr_store.default_path(), os.path.join(data, "market_research.db"))
            os.environ[ndb.ENV_PATH] = "/elsewhere/x.db"
            self.assertEqual(ndb.default_path(), "/elsewhere/x.db")         # the test override still works
        self.assertEqual(sp.mode_fields(), {"mode": "normal"})
        self.assertEqual(sp.stamp({"status": "ok"}), {"status": "ok", "mode": "normal"})

    def test_sandbox_paths(self):
        from market_research import store as mr_store
        from newsroom import db as ndb
        from strategy_platform.research import papers
        from strategy_platform.storage import db
        root = use_tmp_root(self, SANDBOX_MARKER)
        sdir = os.path.join(root, "sandbox")
        self.assertEqual(db.default_path(), os.path.join(sdir, "hedgefund.db"))
        self.assertEqual(mr_store.default_path(), os.path.join(sdir, "market_research.db"))
        with mock.patch.dict(os.environ, {ndb.ENV_PATH: os.path.join(root, "real-newsroom.db")}):
            self.assertEqual(ndb.default_path(), os.path.join(sdir, "newsroom.db"))     # env override ignored
        with mock.patch.object(db, "db_path", lambda: None):
            self.assertEqual(os.path.dirname(papers.papers_dir()), sdir)
        f = sp.mode_fields()
        self.assertEqual(f["mode"], "sandbox")
        self.assertIn("SANDBOX", f["sandbox"]["banner"])
        self.assertEqual(f["sandbox"]["brokers"], "blocked in sandbox")

    def test_mode_on_the_main_state_payloads(self):
        from fund import api as fund_api
        from strategy_platform.api import routes
        self.assertEqual(fund_api.MODE_PATHS, frozenset({"/fund/state"}))
        for p in ("/v2/portfolio/state", "/v2/oms/runtime", "/v2/incubation", "/v2/monitoring/overview",
                  "/v2/brokers"):
            self.assertIn(p, routes.MODE_PATHS)
            self.assertIn(p, {r[1] for r in routes.ROUTES})
        with open(os.path.join(BACKEND, "main.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("return web.json_response(health_payload())", src)
        self.assertIn("sandbox_profile.startup(logger)", src)
        self.assertIn("engine.sandbox_off_reason = sandbox_profile.ENGINE_OFF", src)

    def test_cost_ledger_lines_tagged_only_in_sandbox(self):
        from newsroom import cost_ledger
        tmp = tempfile.mkdtemp(prefix="sbx-ledger-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "ledger.jsonl")
        with mock.patch.dict(os.environ, {"UFOS_AI_COST_LOG": path}):
            use_tmp_root(self)
            cost_ledger.record("analysis", "Analysis", "m", {"cost_usd": 0.01})
            use_tmp_root(self, SANDBOX_MARKER)
            cost_ledger.record("analysis", "Analysis", "m", {"cost_usd": 0.01})
        with open(path, encoding="utf-8") as f:
            normal, sandbox = [json.loads(x) for x in f]
        self.assertNotIn("mode", normal)
        self.assertEqual(normal["feature_label"], "Analysis")
        self.assertEqual((sandbox["mode"], sandbox["feature_label"]), ("sandbox", "SANDBOX · Analysis"))


def call(name, query=None, body=None, match=None):
    from strategy_platform.api import routes
    status, payload = asyncio.run(getattr(routes.V2Api(), name)(query or {}, routes.NO_BODY if body is None else body,
                                                                 match or {}))
    routes.dumps(payload)
    return status, payload


class BrokerBlockTest(BrokerCase):
    def sandbox(self):
        from strategy_platform.execution import accounts
        from strategy_platform.execution.brokers import mirror
        use_tmp_root(self, SANDBOX_MARKER)
        accounts.reset_cache()
        mirror.reset_links()
        self.alpaca.calls.clear()

    def test_no_broker_request_in_sandbox(self):
        from strategy_platform.execution import accounts, control, oms
        from strategy_platform.execution.brokers import mirror, service
        use_tmp_root(self)                          # connected while normal (the copied history)
        acc, _ = self.connect("alpaca-paper")
        self.sandbox()
        self.assertIsInstance(mirror.remote_adapter("alpaca-paper"), mirror.SandboxBlockedAdapter)
        self.assertIsInstance(mirror.remote_adapter("binance-demo"), mirror.SandboxBlockedAdapter)
        self.assertNotIn("alpaca-paper", [r["id"] for r in accounts.rows()])
        self.rt.cycle()
        service.poll_once()
        service.startup()
        out = control.kill_all("panic", "user")
        self.assertTrue(out["platform"]["ok"], out["platform"])
        self.assertNotIn("alpaca-paper", [a["account_id"] for a in out["platform"]["accounts"]])
        acc = accounts.get("alpaca-paper")
        k = control.kill_account(acc, "kill", "kill", "user")
        self.assertIn("blocked in sandbox", k["sandbox_blocked"])
        o, _ = oms.place(acc, {"symbol": "SPY", "side": "buy", "qty": 1, "origin": "manual"})
        self.assertEqual(o["state"], "rejected")
        self.assertIn("blocked in sandbox", o["reject_reason"])
        with self.assertRaises(Exception):
            acc.adapter.remote_call("account", user=True)
        m = {"id": "alpaca-paper"}
        for name, body, query in (("broker_test", {}, None),
                                  ("broker_connect", {"confirm": "CONNECT ALPACA-PAPER", "reason": "try it"}, None),
                                  ("broker_reconcile", {}, None),
                                  ("broker_kill", {"reason": "x"}, None)):
            st, p = call(name, query=query, body=body, match=m)
            self.assertEqual((st, p.get("code")), (409, "sandbox_blocked"), (name, p))
            self.assertIn("blocked in sandbox", p["error"])
        st, p = call("broker_get", match=m)
        b = p["broker"]
        self.assertEqual(b["state"], "blocked_sandbox")
        self.assertEqual(b["sandbox_blocked"]["label"], "blocked in sandbox")
        self.assertFalse(any(b["can"].values()))
        st, p = call("broker_get", match={"id": "binance-demo"})
        self.assertEqual(p["broker"]["state"], "blocked_sandbox")
        self.assertEqual(self.alpaca.calls, [])                        # not one request reached the broker

    def test_paper_main_and_market_data_still_work(self):
        from strategy_platform.execution.brokers import service
        self.sandbox()
        calls = self.quotes.calls
        o = self.manual("SPY", "buy", 2)                       # the internal paper broker trades
        self.assertEqual(o["state"], "filled")
        self.assertGreater(self.quotes.calls, calls)            # market data (quotes) is still read
        self.assertEqual(service.allowed_accounts("equity"), ["paper-main"])
        self.assertEqual(service.allowed_accounts("crypto"), ["paper-main"])

    def test_normal_mode_unchanged(self):
        from strategy_platform.execution.brokers import mirror, service
        use_tmp_root(self)
        self.assertIs(mirror.remote_adapter("alpaca-paper"), self.alpaca)
        self.assertEqual(service.allowed_accounts("equity"), ["paper-main", "alpaca-paper"])
        st, p = call("broker_get", match={"id": "alpaca-paper"})
        self.assertEqual(p["broker"]["state"], "active")
        self.assertNotIn("sandbox_blocked", p["broker"])


class FixtureTest(TradingCase):
    def test_refused_in_normal_mode_nothing_created(self):
        import sandbox_fixtures
        from strategy_platform.storage import db
        use_tmp_root(self)
        with self.assertRaises(SandboxError):
            sandbox_fixtures.seed_incubation_test()
        with self.assertRaises(SandboxError):
            sandbox_fixtures.force_decay(sp.FIXTURE_NAME)
        with db._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM strategies WHERE name = ?",
                                          (sp.FIXTURE_NAME,)).fetchone()[0], 0)

    def test_seed_labels_reach_the_payloads_then_force_decay_pauses(self):
        import sandbox_fixtures
        from strategy_platform.api import routes
        from strategy_platform.live import incubation
        from strategy_platform.monitoring import decay
        use_tmp_root(self, SANDBOX_MARKER)
        out = sandbox_fixtures.seed_incubation_test()
        sid = out["strategy_id"]
        self.assertEqual((out["state"], out["created"]), ("validated", True))
        self.assertFalse(sandbox_fixtures.seed_incubation_test()["created"])          # idempotent
        note = sp.FIXTURE_NOTE
        det = routes._strategy_detail(sid)
        self.assertEqual((det["strategy"]["fixture"], det["strategy"]["fixture_note"]), (True, note))
        self.assertIn(sp.FIXTURE_TAG, det["strategy"]["tags"])
        self.assertTrue(all(note in t["reason"] for t in det["transitions"][1:]))
        cards = out["run_cards"]
        bt = routes._bt_detail(cards["backtest"], None)
        self.assertEqual((bt["fixture"], bt["fixture_note"]), (True, note))
        self.assertIn("fixture", [w["code"] for w in bt["warnings"]])
        for kind in ("validation", "holdout"):
            v = routes._val_detail(cards[kind], None)
            self.assertTrue(v["passed"])
            self.assertEqual((v["fixture"], v["fixture_note"]), (True, note))
            self.assertEqual(v["report"]["fixture_note"], note)
        self.assertTrue(all(r.get("fixture_note") == note for r in routes._val_list(sid, 10)))
        self.assertTrue(all(r.get("fixture_note") == note for r in routes._bt_list(sid, 10)))
        run = incubation.start(sid, 20, "sandbox click-test", now=FRI)["run"]
        self.assertEqual((run["account_id"], run["fixture_note"]), ("paper-main", note))
        res = sandbox_fixtures.force_decay(sp.FIXTURE_NAME, now=FRI + timedelta(days=3))
        self.assertTrue(res["paused"])
        self.assertIn(run["id"], decay.paused_run_ids())
        view = decay.detail(sid, FRI + timedelta(days=3))["strategy"]
        self.assertEqual(view["decay"]["state"], "paused")
        self.assertTrue(view["decay"]["paused"]["synthetic"])
        self.assertIn("synthetic deviation", view["decay"]["paused"]["reason"])
        self.assertEqual(view["fixture_note"], note)
        self.assertTrue(sandbox_fixtures.force_decay(sid)["already"])

    def test_normal_payloads_carry_no_fixture_keys(self):
        from strategy_platform.api import routes
        use_tmp_root(self)
        sid = make_strategy()
        det = routes._strategy_detail(sid)
        self.assertNotIn("fixture", det["strategy"])
        self.assertNotIn("fixture_note", det["strategy"])


class EngineSandboxTest(unittest.TestCase):
    def test_autopilot_refused_and_no_scheduled_cycles(self):
        try:
            from fund_testkit import EngineCase  # noqa: F401
        except ImportError:  # pragma: no cover
            pass
        from fund.engine import EngineError, FundEngine
        eng = FundEngine.__new__(FundEngine)
        eng.sandbox_off_reason = sp.ENGINE_OFF
        with self.assertRaises(EngineError) as c:
            FundEngine.set_autopilot(eng, True)
        self.assertEqual(c.exception.status, 409)
        self.assertIn("autopilot", str(c.exception.message if hasattr(c.exception, "message") else c.exception))
        with open(os.path.join(BACKEND, "fund", "engine.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("and not self.sandbox_off_reason", src)


if __name__ == "__main__":
    unittest.main()
