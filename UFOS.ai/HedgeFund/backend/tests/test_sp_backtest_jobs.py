"""Stage 3e: backtest jobs (SPEC §8.5 / §8.11) — one worker at a time,
run card life cycle, progress, cancellation, failures (user message vs the
generic text), crash recovery, and the full happy path with the real engine,
metrics and stress runner on a synthetic universe (fake loader). Temp
hedgefund.db, offline. Part of UFOS.ai (proof of concept).

Run from HedgeFund/backend:  python -m unittest tests.test_sp_backtest_jobs -v
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strategy_platform.backtest import engine, jobs, runcard  # noqa: E402
from strategy_platform.core.errors import DataSourceUnavailable  # noqa: E402
from strategy_platform.lifecycle import registry as lc  # noqa: E402
from strategy_platform.storage import db  # noqa: E402
import test_sp_backtest_support as sup  # noqa: E402

SYMS = ["AAA", "BBB"]
WAIT = 60


def loader():
    return sup.FakeLoader({"AAA": sup.series(50), "BBB": sup.series(80, drift=-0.0005), "SPY": sup.series(400)})


class BlockingEngine:
    """Fake engine: reports progress, then waits until released or cancelled."""

    def __init__(self, result=None, error=None):
        self.release = threading.Event()
        self.started = threading.Event()
        self.result = result
        self.error = error
        self.calls = []

    def __call__(self, spec, *, start=None, end=None, capital=None, benchmark=None, progress=None, cancel=None,
                 data=None, **kw):
        self.calls.append({"start": start, "end": end, "capital": capital, "benchmark": benchmark, **kw})
        progress("data", 40.0, "loading AAA (1/2)")
        self.started.set()
        while not self.release.wait(0.01):
            if cancel():
                raise engine.BacktestCancelled()
        if self.error is not None:
            raise self.error
        return self.result


class Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = engine.run(sup.spec(SYMS), data=loader())

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sp3e-jobs-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        db.close()
        db.init(os.path.join(self.tmp, "hedgefund.db"))
        self.addCleanup(db.close)
        s = sup.spec(SYMS)
        s["name"] = "jobs test"
        self.sid = lc.create_from_text(json.dumps(s), "user")["id"]

    def wait_done(self, mgr):
        self.assertTrue(mgr.wait(WAIT), "the job did not finish")


class JobTest(Case):
    def test_happy_path_real_engine_and_stress(self):
        mgr = jobs.JobManager(data_factory=loader)
        rid = mgr.submit(self.sid, None, {"stress": True, "capital": 50000.0})
        self.wait_done(mgr)
        card = runcard.get(rid)
        self.assertEqual(card["status"], "done", card["error"])
        self.assertIsNone(card["error"])
        self.assertEqual(card["request"]["capital"], 50000.0)
        self.assertEqual(card["engine_version"], engine.ENGINE_VERSION)
        self.assertEqual(len(card["curve"]["dates"]), len(card["curve"]["equity"]))
        self.assertIn("performance", card["metrics"])
        keys = [s["key"] for s in card["stress"]["scenarios"]]
        self.assertIn("costs_x2", keys)
        self.assertIn("monte_carlo", keys)
        self.assertTrue(any(w["code"] == "survivorship" for w in card["warnings"]))
        self.assertIsNone(mgr.progress(rid))
        self.assertIsNone(mgr.active_run_id())

    def test_without_stress_has_no_stress_section(self):
        mgr = jobs.JobManager(data_factory=loader)
        rid = mgr.submit(self.sid, 1, {})
        self.wait_done(mgr)
        card = runcard.get(rid)
        self.assertEqual(card["status"], "done", card["error"])
        self.assertIsNone(card["stress"])

    def test_progress_and_busy(self):
        fake = BlockingEngine(result=self.result)
        mgr = jobs.JobManager(run_engine=fake, data_factory=loader)
        rid = mgr.submit(self.sid, None, {"start": "2024-02-01", "benchmark": "QQQ"})
        self.assertTrue(fake.started.wait(WAIT))
        self.assertEqual(runcard.get(rid)["status"], "running")
        p = mgr.progress(rid)
        self.assertEqual((p["phase"], p["percent"], p["message"]), ("data", 40.0, "loading AAA (1/2)"))
        self.assertIn("updated_at", p)
        with self.assertRaises(jobs.JobBusy) as cm:
            mgr.submit(self.sid, None, {})
        self.assertEqual(cm.exception.run_id, rid)
        self.assertEqual(str(cm.exception), "A backtest is already running.")
        fake.release.set()
        self.wait_done(mgr)
        self.assertEqual(runcard.get(rid)["status"], "done")
        self.assertEqual(fake.calls[0]["start"], "2024-02-01")
        self.assertEqual(fake.calls[0]["benchmark"], "QQQ")
        self.assertEqual(fake.calls[0]["capital"], engine.DEFAULT_CAPITAL)
        self.assertEqual(len(runcard.list_runs(self.sid)), 1)     # the refused submit created no card

    def test_cancel(self):
        fake = BlockingEngine(result=self.result)
        mgr = jobs.JobManager(run_engine=fake, data_factory=loader)
        rid = mgr.submit(self.sid, None, {})
        self.assertTrue(fake.started.wait(WAIT))
        self.assertFalse(mgr.cancel("bt-unknown"))
        self.assertTrue(mgr.cancel(rid))
        self.wait_done(mgr)
        card = runcard.get(rid)
        self.assertEqual(card["status"], "cancelled")
        self.assertEqual(card["error"], jobs.CANCELLED_MESSAGE)
        # the next job may start
        fake2 = BlockingEngine(result=self.result)
        fake2.release.set()
        mgr2 = jobs.JobManager(run_engine=fake2, data_factory=loader)
        self.assertEqual(runcard.get(mgr2.submit(self.sid, None, {}))["status"] in ("queued", "running", "done"),
                         True)
        self.wait_done(mgr2)

    def test_cancel_honoured_by_the_real_engine(self):
        mgr = jobs.JobManager(data_factory=loader)
        orig = jobs.JobManager._set_progress

        def cancel_on_simulate(self_, job, phase, percent, message):
            orig(self_, job, phase, percent, message)
            if phase == "simulate":
                job.cancel_flag.set()
        jobs.JobManager._set_progress = cancel_on_simulate
        self.addCleanup(setattr, jobs.JobManager, "_set_progress", orig)
        rid = mgr.submit(self.sid, None, {})
        self.wait_done(mgr)
        self.assertEqual(runcard.get(rid)["status"], "cancelled")

    def test_user_facing_failure_keeps_message(self):
        err = DataSourceUnavailable("tiingo", "equity_volume", "HTTP 503")
        fake = BlockingEngine(error=err)
        fake.release.set()
        mgr = jobs.JobManager(run_engine=fake, data_factory=loader)
        rid = mgr.submit(self.sid, None, {})
        self.wait_done(mgr)
        card = runcard.get(rid)
        self.assertEqual(card["status"], "failed")
        self.assertEqual(card["error"], err.user_message)

    def test_unexpected_failure_generic_message(self):
        fake = BlockingEngine(error=RuntimeError("secret internals /path/x.py line 3"))
        fake.release.set()
        mgr = jobs.JobManager(run_engine=fake, data_factory=loader)
        with self.assertLogs("strategy_platform.backtest.jobs", "ERROR") as logs:
            rid = mgr.submit(self.sid, None, {})
            self.wait_done(mgr)
        card = runcard.get(rid)
        self.assertEqual(card["status"], "failed")
        self.assertEqual(card["error"], "Backtest failed — see backend.log")
        self.assertIn("secret internals", "\n".join(logs.output))

    def test_metrics_failure_ends_card(self):
        mgr = jobs.JobManager(run_engine=lambda *a, **k: self.result, data_factory=loader,
                              compute_metrics=lambda r: (_ for _ in ()).throw(ValueError("boom")))
        with self.assertLogs("strategy_platform.backtest.jobs", "ERROR"):
            rid = mgr.submit(self.sid, None, {})
            self.wait_done(mgr)
        self.assertEqual(runcard.get(rid)["error"], jobs.GENERIC_FAILURE)

    def test_unknown_strategy(self):
        mgr = jobs.JobManager(data_factory=loader)
        with self.assertRaises(runcard.NotFound):
            mgr.submit("nope", None, {})
        with self.assertRaises(runcard.NotFound):
            mgr.submit(self.sid, 7, {})
        self.assertIsNone(mgr.active_run_id())

    def test_recover_interrupted(self):
        queued = runcard.create(self.sid, request={})["id"]
        running = runcard.create(self.sid, request={})["id"]
        runcard.mark_running(running)
        done = runcard.create(self.sid, request={})["id"]
        runcard.mark_running(done)
        runcard.finish(done, self.result, {"performance": {}})
        with self.assertLogs("strategy_platform.backtest.jobs", "WARNING"):
            ids = jobs.recover_interrupted()
        self.assertEqual(sorted(ids), sorted([queued, running]))
        for rid in (queued, running):
            card = runcard.get(rid)
            self.assertEqual((card["status"], card["error"]), ("failed", "Backend stopped during the run"))
        self.assertEqual(runcard.get(done)["status"], "done")
        self.assertEqual(jobs.recover_interrupted(), [])

    def test_manager_singleton(self):
        jobs.set_manager(None)
        self.addCleanup(jobs.set_manager, None)
        self.assertIs(jobs.manager(), jobs.manager())


if __name__ == "__main__":
    unittest.main()
