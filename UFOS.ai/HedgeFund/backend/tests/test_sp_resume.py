"""SPEC §10.3/§10.4 (R2): resumable runs — storage v8 (run card columns,
insert-only checkpoints, running-card notes), tamper-evident checkpoints
(mismatch -> recompute + warning), resume only on an identical basis (each
mismatch -> 409), a resumed chain counts as ONE trial, holdout runs are never
resumable, the auto-resume state machine (3 connection failures then success
-> done with resume_count 3; 6 failures -> failed after 5 automatic resumes;
cancel while waiting), crash recovery, and the routes (auto_resume, resume,
list/detail fields, GET/PUT /v2/settings/network). Temp hedgefund.db,
offline (fake loader, fake probe).

Run from HedgeFund/backend:  python -m unittest tests.test_sp_resume -v
"""
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strategy_platform.api import routes  # noqa: E402
from strategy_platform.backtest import jobs, runcard  # noqa: E402
from strategy_platform.core.clock import FixedClock  # noqa: E402
from strategy_platform.core.errors import DataSourceUnavailable  # noqa: E402
from strategy_platform.data import network  # noqa: E402
from strategy_platform.lifecycle import registry as lc  # noqa: E402
from strategy_platform.storage import db  # noqa: E402
from strategy_platform.validation import families, parallel  # noqa: E402
from strategy_platform.validation import jobs as vjobs  # noqa: E402
import test_sp_backtest_support as bsup  # noqa: E402
import test_sp_parallel as tpar  # noqa: E402
import test_sp_validation_support as sup  # noqa: E402

WAIT = 170
BT_PRICES = {s: bsup.series(100.0 + 10 * i) for i, s in enumerate(["AAA", "BBB", "SPY"])}


def call(name, body=routes.NO_BODY, query=None, match=None):
    return asyncio.run(getattr(routes.V2Api(), name)(query or {}, body, match or {}))


def conn_error(source="alpaca_sip"):
    return network.mark_connection(DataSourceUnavailable(source, "equity_bars_history", "the read timed out"))


class FlakyLoader(bsup.FakeLoader):
    """FakeLoader whose history() raises a connection-kind error ``fail`` times when ``when`` matches."""
    max_workers = 1                          # sequential loading: one failure per attempt

    def __init__(self, prices, *, fail=0, when=None, source="alpaca_sip", factor=1.0):
        super().__init__(prices)
        self.fail_left = fail
        self.when = when or (lambda symbol, start, end: True)
        self.source = source
        self.factor = factor
        self.lock = threading.Lock()

    def history(self, symbol, adjustment, start, end=None, *, progress=None):
        with self.lock:
            hit = self.fail_left > 0 and self.when(symbol, start, end)
            if hit:
                self.fail_left -= 1
        if hit:
            raise conn_error(self.source)
        rows, info = super().history(symbol, adjustment, start, end, progress=progress)
        if self.factor != 1.0 and symbol == "AAA":
            rows = [dict(r, close=r["close"] * self.factor) for r in rows]
        return rows, info


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sp-r2-resume-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        db.close()
        db.init(os.path.join(self.tmp, "hedgefund.db"))
        self.addCleanup(db.close)
        network.reset()
        self.addCleanup(network.reset)
        families.set_clock(FixedClock(sup.NOW))
        self.addCleanup(families.set_clock, None)
        self.sid = lc.create_from_text(json.dumps(bsup.spec(["AAA", "BBB"])), "user")["id"]
        self.probes = []
        self.probe_result = True
        self.loader = FlakyLoader(BT_PRICES)
        self.mgr = jobs.JobManager(data_factory=lambda: self.loader, probe=self._probe, probe_interval=0.01)
        jobs.set_manager(self.mgr)
        self.addCleanup(jobs.set_manager, None)
        self.addCleanup(self.mgr.wait, WAIT)

    def _probe(self, source):
        rid = self.mgr.active_run_id()
        self.probes.append({"source": source, "waiting": self.mgr.is_waiting(rid),
                            "progress": self.mgr.progress(rid),
                            "list_status": routes._bt_list(None, 5)[0]["status"],
                            "card_error_kind": runcard.get(rid)["error_kind"]})
        return self.probe_result if not callable(self.probe_result) else self.probe_result()

    def wait(self):
        self.assertTrue(self.mgr.wait(WAIT), "the job did not finish")

    def failed_backtest(self, **kw):
        """A backtest that fails once with a connection error (no auto-resume) -> its card."""
        self.loader.fail_left = 1
        rid = self.mgr.submit(self.sid, None, {}, **kw)
        self.wait()
        card = runcard.get(rid)
        self.assertEqual((card["status"], card["error_kind"]), ("failed", "connection"), card["error"])
        return card


class StorageTest(Case):
    def test_v8_schema_and_triggers(self):
        self.assertGreaterEqual(db.SCHEMA_VERSION, 8)        # v9: attribution_json (SPEC §11.1)
        self.assertIn("run_checkpoints", db.TABLES)
        card = runcard.create(self.sid, None, {}, auto_resume=True)
        self.assertEqual((card["resumed_from"], card["resume_count"], card["error_kind"], card["auto_resume"]),
                         (None, 0, None, True))
        cid = db.checkpoint_insert(card["id"], "x", "{}", "0" * 64)
        raw = sqlite3.connect(db.db_path(), isolation_level=None)
        self.addCleanup(raw.close)
        for sql in ("UPDATE run_checkpoints SET payload_json = '[]' WHERE id = ?",
                    "DELETE FROM run_checkpoints WHERE id = ?"):
            with self.assertRaises(sqlite3.DatabaseError):
                raw.execute(sql, (cid,))
        runcard.mark_running(card["id"])
        self.assertTrue(runcard.mark_waiting(card["id"]))
        self.assertEqual(runcard.get(card["id"])["error_kind"], "connection")
        self.assertTrue(runcard.mark_resumed(card["id"]))
        c2 = runcard.get(card["id"])
        self.assertEqual((c2["error_kind"], c2["resume_count"], c2["status"]), (None, 1, "running"))
        with self.assertRaises(sqlite3.DatabaseError):          # only resume_count / error_kind while running
            raw.execute("UPDATE backtest_runs SET metrics_json = '{}' WHERE id = ?", (card["id"],))
        with self.assertRaises(sqlite3.DatabaseError):          # resume_count never goes down
            raw.execute("UPDATE backtest_runs SET resume_count = 0 WHERE id = ?", (card["id"],))
        with self.assertRaises(sqlite3.DatabaseError):          # resumed_from is identity
            raw.execute("UPDATE backtest_runs SET resumed_from = 'bt-x' WHERE id = ?", (card["id"],))


class CheckpointTest(Case):
    def test_tampered_checkpoint_is_ignored_with_warning(self):
        card = runcard.create(self.sid, None, {})
        st = runcard.CheckpointStore(card["id"])
        st.put("grid:abc", {"series": {"2024-01-02": 0.01}, "reason": None})
        st2 = runcard.CheckpointStore(card["id"])
        self.assertEqual(st2.get("grid:abc"), {"reason": None, "series": {"2024-01-02": 0.01}})
        self.assertEqual(st2.warnings, [])
        text = '{"reason":null,"series":{"2024-01-02":0.99}}'           # forged payload, stale digest
        db.checkpoint_insert(card["id"], "grid:abc", text, runcard.CheckpointStore.digest(card["id"], "grid:abc", "x"))
        st3 = runcard.CheckpointStore(card["id"])
        self.assertIsNone(st3.get("grid:abc"))
        self.assertEqual([w["code"] for w in st3.warnings], ["checkpoint_mismatch"])
        self.assertEqual(st3.warnings[0]["step"], "grid:abc")
        # a row copied to another run / step does not verify either (run id + step are hashed)
        other = runcard.create(self.sid, None, {})
        good = st2._rows["grid:abc"]
        db.checkpoint_insert(other["id"], "grid:abc", good["payload_json"], good["sha256"])
        self.assertIsNone(runcard.CheckpointStore(other["id"]).get("grid:abc"))

    def test_validation_resume_reuses_checkpoints_and_recomputes_tampered_step(self):
        sid = lc.create_from_text(json.dumps(tpar.spec()), "user")["id"]
        self.loader = FlakyLoader(tpar.PRICES, fail=1, when=lambda s, a, b: b is not None and b < date(2023, 1, 1))
        svc = vjobs.ValidationJobs(data_factory=lambda: self.loader)
        vjobs.set_service(svc)                                     # jobs.resume dispatches to the service
        self.addCleanup(vjobs.set_service, None)
        rid = svc.submit_validation(sid, None, None)
        self.wait()
        card = runcard.get(rid)
        self.assertEqual((card["status"], card["error_kind"]), ("failed", "connection"), card["error"])
        self.assertIn("timed out", card["error"])
        self.assertIsNone(card["trial_number"])
        rows = db.checkpoints_list([rid])
        grid_steps = sorted({r["step_key"] for r in rows if r["step_key"].startswith("grid:")})
        self.assertEqual(len(grid_steps), 3)                     # 4 configs, the base reuses the training run
        self.assertIn("basis", {r["step_key"] for r in rows})
        self.assertTrue(any(r["step_key"].startswith("data:") for r in rows))
        bad = grid_steps[0]
        db.checkpoint_insert(rid, bad, '{"reason":null,"series":{}}', "f" * 64)
        new = jobs.resume(rid)
        self.wait()
        c2 = runcard.get(new)
        self.assertEqual((c2["status"], c2["resumed_from"], c2["resume_count"]), ("done", rid, 1), c2["error"])
        codes = [w for w in c2["warnings"] if w.get("code") == "checkpoint_mismatch"]
        self.assertEqual([w["step"] for w in codes], [bad])
        self.assertEqual(families.trial_count(families.family_of(sid)), 1)
        self.assertEqual(c2["trial_number"], 1)
        rep = c2["metrics"]["validation"]["report"]
        ref = tpar.seq_run()["report"]                               # uninterrupted, sequential
        self.assertEqual([c["sharpe"] for c in rep["grid"]["configs"]],
                         [c["sharpe"] for c in tpar.norm(ref)["grid"]["configs"]])
        self.assertEqual(tpar.norm(rep["stress"]), tpar.norm(ref["stress"]))
        own = {r["step_key"] for r in db.checkpoints_list([new])}
        self.assertIn(bad, own)                                    # recomputed -> written for the new run
        self.assertFalse(set(grid_steps[1:]) & own)                # reused from the failed run


class BasisTest(Case):
    def test_window_move_tampered_basis_and_content_hash_are_409(self):
        card = self.failed_backtest()
        self.assertTrue(routes._bt_list(None, 5)[0]["resumable"])
        self.assertIsNone(runcard.resume_refusal(card))
        families.set_clock(FixedClock(sup.NOW + timedelta(days=3)))
        why = runcard.resume_refusal(card)
        self.assertIn("The basis changed (the locked holdout window moved", why)
        self.assertEqual(call("backtest_resume", match={"id": card["id"]}), (409, {"error": why}))
        families.set_clock(FixedClock(sup.NOW))
        db.checkpoint_insert(card["id"], "basis", "{}", "0" * 64)
        self.assertIn("record of the first start is missing or failed its integrity check",
                      runcard.resume_refusal(card))
        # a card whose content hash no longer matches its version
        raw = db.backtest_run_get(card["id"])
        row = {k: raw[k] for k in ("strategy_id", "version", "spec_json", "request_json", "kind")}
        row.update(id="bt-hashmismatch", created_at=raw["created_at"], updated_at=raw["created_at"],
                   content_hash="0" * 64, status="queued")
        db.backtest_run_insert(row, ("engine", "test", {}))
        db.backtest_run_transition("bt-hashmismatch", "queued", "running", {})
        db.backtest_run_transition("bt-hashmismatch", "running", "failed", {"error": "x", "error_kind": "connection"})
        self.assertIn("content hash differs", runcard.resume_refusal(runcard.get("bt-hashmismatch")))

    def test_engine_update_is_409(self):
        from strategy_platform.backtest import engine
        card = self.failed_backtest()
        old = engine.ENGINE_VERSION
        engine.ENGINE_VERSION = old + "-next"
        self.addCleanup(setattr, engine, "ENGINE_VERSION", old)
        why = runcard.resume_refusal(card)
        self.assertIn(f"the engine was updated ({old} → {old}-next)", why)
        self.assertTrue(why.endswith("— start a new run."))
        self.assertEqual(call("backtest_resume", match={"id": card["id"]}), (409, {"error": why}))

    def test_known_at_changes_do_not_break_the_basis(self):
        card = runcard.create(self.sid, None, {})
        rows1 = [{"symbol": "AAA", "ts": "2024-01-02", "close": 1.0, "known_at": "2026-10-09T20:00:00+00:00"}]
        rows2 = [dict(rows1[0], known_at="2026-10-12T20:00:00+00:00", fetched_at="2026-10-12")]

        class L:
            def __init__(self, rows):
                self.rows = rows

            def corporate_actions(self, symbol, start, end=None, *, progress=None):
                return self.rows, {}
        st = runcard.CheckpointStore(card["id"])
        parallel.RecordingLoader(L(rows1), checkpoints=st).corporate_actions("AAA", date(2024, 1, 2), None)
        st2 = runcard.CheckpointStore(card["id"])
        parallel.RecordingLoader(L(rows2), checkpoints=st2).corporate_actions("AAA", date(2024, 1, 2), None)
        with self.assertRaises(runcard.BasisChanged):
            parallel.RecordingLoader(L([dict(rows1[0], close=2.0)]), checkpoints=runcard.CheckpointStore(
                card["id"])).corporate_actions("AAA", date(2024, 1, 2), None)

    def test_request_and_end_mismatch(self):
        card = runcard.create(self.sid, None, {})
        now = runcard.current_basis(card)
        self.assertIsNone(runcard.basis_problem(now, dict(now)))
        self.assertIn("request differs", runcard.basis_problem(dict(now, request={"start": "2020-01-02"}), now))
        self.assertIn("end date resolved", runcard.basis_problem(dict(now, resolved_end="2024-01-02"), now))
        self.assertEqual(now["resolved_end"], "2024-10-07")
        self.assertEqual(now["holdout_window"], {"start": "2024-10-08", "end": "2026-10-08"})

    def test_changed_data_fails_the_resumed_run_with_kind_basis(self):
        self.loader = FlakyLoader(BT_PRICES, fail=1, when=lambda s, a, b: s == "BBB")
        rid = self.mgr.submit(self.sid, None, {})
        self.wait()
        self.assertEqual(runcard.get(rid)["error_kind"], "connection")
        self.loader = FlakyLoader(BT_PRICES, factor=1.01)          # AAA's cached rows now differ
        new = jobs.resume(rid)
        self.wait()
        c2 = runcard.get(new)
        self.assertEqual((c2["status"], c2["error_kind"]), ("failed", "basis"))
        self.assertIn("The basis changed (the loaded data of AAA history (all) differs", c2["error"])
        self.assertIn("Only a run that failed because the connection was lost", runcard.resume_refusal(c2))

    def test_refusals(self):
        card = self.failed_backtest()
        new = jobs.resume(card["id"])
        self.wait()
        self.assertEqual(runcard.get(new)["status"], "done")
        self.assertEqual(runcard.resume_refusal(runcard.get(card["id"])), f"This run was already resumed as {new}.")
        self.assertIn("this run is done", runcard.resume_refusal(runcard.get(new)))
        status, payload = call("backtest_resume", match={"id": new})
        self.assertEqual(status, 409)
        with self.assertRaises(runcard.ResumeRefused):
            runcard.create_resumed(card["id"])
        with self.assertRaises(db.StorageUnavailable):            # DB: one successor per run
            raw = db.backtest_run_get(new)
            db.backtest_run_insert({k: raw[k] for k in ("strategy_id", "version", "content_hash", "spec_json",
                                                         "request_json", "kind")} |
                                   {"id": "bt-fork", "created_at": raw["created_at"], "updated_at": raw["created_at"],
                                    "status": "queued", "resumed_from": card["id"]}, ("engine", "t", {}))

    def test_holdout_never_resumable(self):
        card = runcard.create(self.sid, None, {"start": "2024-10-08", "end": "2026-10-08"}, kind="holdout",
                              auto_resume=True)
        self.assertFalse(card["auto_resume"])
        runcard.mark_running(card["id"])
        runcard.fail(card["id"], "timed out", "connection")
        c = runcard.get(card["id"])
        self.assertFalse(runcard.resumable(c))
        self.assertEqual(runcard.resume_refusal(c), runcard.HOLDOUT_NOT_RESUMABLE)
        status, payload = call("validation_resume", match={"id": card["id"]})
        self.assertEqual((status, payload), (409, {"error": runcard.HOLDOUT_NOT_RESUMABLE}))
        self.assertFalse(call("validation_get", match={"id": card["id"]})[1]["resumable"])


class TrialTest(Case):
    def test_resumed_chain_counts_once(self):
        card = self.failed_backtest()
        fid = families.family_of(self.sid)
        self.assertEqual(families.trial_count(fid), 0)
        status, payload = call("backtest_resume", {"auto_resume": True}, match={"id": card["id"]})
        self.assertEqual(status, 202, payload)
        self.assertEqual(payload["resumed_from"], card["id"])
        self.wait()
        c2 = runcard.get(payload["run_id"])
        self.assertEqual((c2["status"], c2["resumed_from"], c2["resume_count"], c2["trial_number"], c2["auto_resume"]),
                         ("done", card["id"], 1, 1, True))
        self.assertEqual(families.trial_count(fid), 1)
        self.assertIsNone(runcard.get(card["id"])["trial_number"])
        rows = {r["id"]: r for r in call("backtests_list")[1]["runs"]}
        self.assertFalse(rows[card["id"]]["resumable"])            # already resumed
        self.assertEqual(rows[c2["id"]]["resumed_from"], card["id"])
        d = call("backtest_get", match={"id": c2["id"]})[1]
        self.assertEqual((d["resumed_from"], d["resume_count"], d["error_kind"], d["resumable"]),
                         (card["id"], 1, None, False))


class AutoResumeTest(Case):
    def test_three_connection_failures_then_success(self):
        self.loader.fail_left = 3
        rid = self.mgr.submit(self.sid, None, {}, auto_resume=True)
        self.wait()
        card = runcard.get(rid)
        self.assertEqual((card["status"], card["resume_count"], card["error_kind"]), ("done", 3, None), card["error"])
        self.assertEqual(len(self.probes), 3)
        p = self.probes[0]
        self.assertTrue(p["waiting"])
        self.assertEqual(p["progress"]["phase"], "waiting")
        self.assertEqual(p["progress"]["message"], "Connection lost — waiting for Alpaca (attempt 1/5)…")
        self.assertEqual(self.probes[2]["progress"]["message"], "Connection lost — waiting for Alpaca (attempt 3/5)…")
        self.assertEqual(p["list_status"], "waiting")
        self.assertEqual(p["card_error_kind"], "connection")
        self.assertEqual(p["source"], "alpaca")
        self.assertEqual(sum(a["action"] == "backtest_auto_resumed" for a in db.list_audit(limit=100)), 3)

    def test_six_failures_fail_after_five_resumes(self):
        self.loader.fail_left = 6
        rid = self.mgr.submit(self.sid, None, {}, auto_resume=True)
        self.wait()
        card = runcard.get(rid)
        self.assertEqual((card["status"], card["error_kind"], card["resume_count"]), ("failed", "connection", 5))
        self.assertIn("timed out", card["error"])
        self.assertEqual(len(self.probes), 5)
        self.assertTrue(runcard.resumable(card))                  # manual Resume still possible

    def test_unreachable_probe_waits_then_cancel(self):
        self.loader.fail_left = 1
        self.loader.source = "tiingo"
        self.probe_result = False
        rid = self.mgr.submit(self.sid, None, {}, auto_resume=True)
        for _ in range(2000):
            if len(self.probes) >= 3:
                break
            threading.Event().wait(0.01)
        self.assertGreaterEqual(len(self.probes), 3)               # keeps probing, no time limit
        self.assertTrue(self.mgr.is_waiting(rid))
        self.assertIn("waiting for Tiingo (attempt 1/5)", self.mgr.progress(rid)["message"])
        self.assertEqual(call("backtest_get", match={"id": rid})[1]["status"], "waiting")
        status, _ = call("backtest_cancel", match={"id": rid})
        self.assertEqual(status, 202)
        self.wait()
        card = runcard.get(rid)
        self.assertEqual((card["status"], card["error_kind"]), ("cancelled", None))
        self.assertFalse(runcard.resumable(card))

    def test_waiting_stops_when_the_basis_is_gone(self):
        self.loader.fail_left = 1
        self.probe_result = False
        rid = self.mgr.submit(self.sid, None, {}, auto_resume=True)
        for _ in range(2000):
            if self.probes:
                break
            threading.Event().wait(0.01)
        self.assertTrue(self.probes)
        families.set_clock(FixedClock(sup.NOW + timedelta(days=3)))     # the holdout window rolls over
        self.wait()
        card = runcard.get(rid)
        self.assertEqual((card["status"], card["error_kind"]), ("failed", "basis"))
        self.assertIn("The basis changed (the locked holdout window moved", card["error"])

    def test_fred_source_is_named_and_not_probed_as_alpaca(self):
        self.loader.fail_left = 1
        self.loader.source = "fred"
        rid = self.mgr.submit(self.sid, None, {}, auto_resume=True)
        self.wait()
        self.assertEqual(runcard.get(rid)["status"], "done")
        self.assertEqual(self.probes[0]["source"], "fred")
        self.assertEqual(self.probes[0]["progress"]["message"], "Connection lost — waiting for FRED (attempt 1/5)…")
        self.assertEqual(jobs.source_of(conn_error("sec_edgar")), "sec_edgar")
        self.assertEqual(jobs.source_of(conn_error("alpaca_sip")), "alpaca")
        self.assertEqual(jobs.source_of(KeyError("x")), "unknown")
        plain = jobs.JobManager()                   # no injected probe: FRED has none -> next real request
        self.assertTrue(plain._probe_source("fred"))
        self.assertEqual(plain._next_probe_in("sec_edgar"), 0.0)

    def test_without_auto_resume_fails_at_once(self):
        card = self.failed_backtest()
        self.assertEqual(self.probes, [])
        self.assertEqual(card["resume_count"], 0)


class RecoveryTest(Case):
    def test_crash_recovery_kinds(self):
        a = runcard.create(self.sid, None, {})
        b = runcard.create(self.sid, None, {})
        c = runcard.create(self.sid, None, {})
        runcard.mark_running(a["id"])
        runcard.mark_running(b["id"])
        runcard.mark_waiting(b["id"])
        ids = jobs.recover_interrupted()
        self.assertEqual(set(ids), {a["id"], b["id"], c["id"]})
        ca, cb, cc = (runcard.get(x["id"]) for x in (a, b, c))
        self.assertEqual((ca["error_kind"], ca["error"]), ("interrupted", jobs.INTERRUPTED_MESSAGE))
        self.assertEqual((cb["error_kind"], cb["error"]), ("connection", jobs.WAITING_INTERRUPTED_MESSAGE))
        self.assertEqual(cc["error_kind"], "interrupted")
        self.assertFalse(runcard.resumable(ca))
        self.assertTrue(runcard.resumable(cb))


class RouteTest(Case):
    def test_auto_resume_body_and_resume_route_validation(self):
        status, payload = call("backtest_create", {"strategy_id": self.sid, "auto_resume": "yes"})
        self.assertEqual((status, payload["errors"]), (400, {"auto_resume": "expected true/false"}))
        status, payload = call("validation_create", {"strategy_id": self.sid, "auto_resume": 1})
        self.assertEqual(status, 400)
        status, payload = call("backtest_create", {"strategy_id": self.sid, "auto_resume": True})
        self.assertEqual(status, 202, payload)
        self.wait()
        rid = payload["run_id"]
        self.assertTrue(runcard.get(rid)["auto_resume"])
        self.assertEqual(call("backtest_resume", match={"id": "bt-unknown00000"}), (404, {"error": routes.BT_NOT_FOUND}))
        self.assertEqual(call("backtest_resume", {"x": 1}, match={"id": rid})[0], 400)
        self.assertEqual(call("backtest_resume", {"auto_resume": "y"}, match={"id": rid})[0], 400)
        self.assertEqual(call("backtest_resume", query={"a": "1"}, match={"id": rid})[0], 400)
        self.assertEqual(call("validation_resume", match={"id": rid}), (404, {"error": routes.VAL_NOT_FOUND}))
        status, payload = call("backtest_resume", match={"id": rid})
        self.assertEqual(status, 409)
        self.assertIn("this run is done", payload["error"])
        paths = {(m, p) for m, p, _, _ in routes.ROUTES}
        for r in (("POST", "/v2/backtests/{id}/resume"), ("POST", "/v2/validations/{id}/resume"),
                  ("GET", "/v2/settings/network"), ("PUT", "/v2/settings/network")):
            self.assertIn(r, paths)

    def test_network_settings_routes(self):
        status, s = call("network_settings_get")
        self.assertEqual(status, 200, s)
        self.assertEqual(set(s), {"adaptive", "fixed", "current", "min", "max"})
        self.assertEqual((s["adaptive"], s["fixed"], s["min"], s["max"]), (True, 4, 1, 8))
        for body, field in (({"adaptive": "no"}, "adaptive"), ({"adaptive": False, "fixed": 9}, "fixed"),
                            ({"adaptive": False, "fixed": 0}, "fixed"), ({"fixed": 2}, "adaptive"),
                            ({"adaptive": True, "x": 1}, "x"), ({"adaptive": True, "fixed": True}, "fixed")):
            status, payload = call("network_settings_put", body)
            self.assertEqual(status, 400, body)
            self.assertIn(field, payload["errors"])
        self.assertEqual(call("network_settings_get", query={"x": "1"})[0], 400)
        status, s = call("network_settings_put", {"adaptive": False, "fixed": 2})
        self.assertEqual((status, s["adaptive"], s["fixed"]), (200, False, 2))
        self.assertEqual(s["current"], {"alpaca": 2, "tiingo": 2})
        status, s = call("network_settings_put", {"adaptive": True})
        self.assertEqual((s["adaptive"], s["fixed"]), (True, 2))
        audits = [a for a in db.list_audit(limit=50) if a["action"] == "network_settings_changed"]
        self.assertEqual(len(audits), 2)
        detail = audits[-1]["detail"] if audits[-1]["detail"]["new"]["adaptive"] is False else audits[0]["detail"]
        self.assertEqual(detail, {"old": {"adaptive": True, "fixed": 4}, "new": {"adaptive": False, "fixed": 2}})


if __name__ == "__main__":
    unittest.main()
