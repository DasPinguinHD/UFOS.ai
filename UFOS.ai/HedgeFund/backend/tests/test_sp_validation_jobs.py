"""Stage 4 (V2): validation and holdout jobs on the shared single-job
runner — the full pipeline with the REAL engine, metrics and stress runner
on a small synthetic universe (fake loader): validation card (kind,
metrics_json.validation, grid, folds, stress, trial number), exceptions
(rules, merge, effective_passed -> lifecycle candidate), the holdout job
(confirmation, eligibility, holdout event, once per family, validated),
busy/cancel/failure through the existing JobManager. Temp hedgefund.db,
offline.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_validation_jobs -v
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402

from strategy_platform.backtest import engine, jobs, runcard  # noqa: E402
from strategy_platform.core import calendars  # noqa: E402
from strategy_platform.core.clock import FixedClock  # noqa: E402
from strategy_platform.core.errors import DataSourceUnavailable  # noqa: E402
from strategy_platform.lifecycle import registry as lc  # noqa: E402
from strategy_platform.storage import db  # noqa: E402
from strategy_platform.validation import families, jobs as vjobs, report  # noqa: E402
import test_sp_backtest_support as bsup  # noqa: E402
import test_sp_validation_support as sup  # noqa: E402

SYMS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
WAIT = 170
_DAYS = calendars.trading_days(date(2024, 1, 2), date(2026, 10, 8))


def _prices():
    rng = np.random.default_rng(11)
    out = {}
    for k, s in enumerate(SYMS + ["SPY"]):
        r = rng.normal(0.0004 + 0.0001 * k, 0.012, len(_DAYS))
        c = 50.0 * (1 + k) * np.cumprod(1 + r)
        out[s] = {d: (float(c[i] * 0.999), float(c[i])) for i, d in enumerate(_DAYS)}
    return out


PRICES = _prices()


def loader():
    return bsup.FakeLoader(PRICES)


def strategy_spec(name="validation job test"):
    return {"schema": "ufos.strategy/1", "name": name, "universe": {"symbols": list(SYMS)},
            "parameters": {"lookback": {"value": 10, "min": 5, "max": 20, "step": 5, "integer": True}},
            "signal": {"op": "cs_rank", "args": [{"op": "pct_change", "args": [{"field": "close"},
                                                                                {"param": "lookback"}]}]},
            "portfolio": {"mode": "long_short", "selection": {"type": "top_bottom_n", "n": 2}, "weighting": "equal",
                          "max_weight": 1.0, "gross_exposure": 1.0},
            "rebalance": {"frequency": "weekly"}}


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sp4-vjobs-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        db.close()
        db.init(os.path.join(self.tmp, "hedgefund.db"))
        self.addCleanup(db.close)
        families.set_clock(FixedClock(sup.NOW))
        self.addCleanup(families.set_clock, None)
        self.sid = lc.create_from_text(json.dumps(strategy_spec()), "user")["id"]
        self.mgr = jobs.JobManager(data_factory=loader)
        jobs.set_manager(self.mgr)
        self.addCleanup(jobs.set_manager, None)
        self.addCleanup(self.mgr.wait, WAIT)

    def wait(self):
        self.assertTrue(self.mgr.wait(WAIT), "the job did not finish")


class PipelineTest(Case):
    def test_validation_exceptions_holdout_real_engine(self):
        svc = vjobs.ValidationJobs(data_factory=loader)
        rid = svc.submit_validation(self.sid, None, None)
        self.assertEqual(self.mgr.active_run_id(), rid)
        self.wait()
        card = runcard.get(rid)
        self.assertEqual((card["kind"], card["status"]), ("validation", "done"), card.get("error"))
        self.assertEqual(card["request"]["history_from"], None)
        self.assertTrue(card["request"]["stress"])
        self.assertEqual(card["range"]["end"], "2024-10-07")              # training end = holdout start - 1
        self.assertIsNotNone(card["trial_number"])
        block = card["metrics"]["validation"]
        self.assertEqual(set(block), {"passed", "version", "report"})
        rep = block["report"]
        self.assertEqual(rep["version"], report.REPORT_VERSION)
        self.assertEqual([g["gate"] for g in rep["gates"]], ["G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8"])
        self.assertEqual(rep["grid"]["total"], 4)
        self.assertEqual([c["params"]["lookback"] for c in rep["grid"]["configs"]], [5, 10, 15, 20])
        self.assertTrue(all(c["sharpe"] is not None for c in rep["grid"]["configs"]), rep["grid"]["configs"])
        self.assertEqual(rep["pbo"]["configs"], 4)
        self.assertEqual(len(rep["folds"]), 5)
        self.assertEqual(rep["mode"], "cross_sectional")
        self.assertEqual(rep["thresholds"]["preset"], "standard")
        self.assertEqual(rep["trial_count"], 1)
        keys = [s["key"] for s in card["stress"]["scenarios"]]
        self.assertIn("costs_x2", keys)
        self.assertIn("gfc", keys)
        self.assertNotEqual(next(s for s in card["stress"]["scenarios"] if s["key"] == "costs_x2")["status"],
                            "failed")
        self.assertIsNotNone(rep["holdout"])
        json.dumps(card["metrics"], allow_nan=False)
        self.assertEqual(families.trial_count(families.family_of(self.sid)), 1)

        # exceptions
        failed_gates = [g["gate"] for g in rep["gates"] if g["status"] == "fail"]
        passed_gates = [g["gate"] for g in rep["gates"] if g["status"] == "pass"]
        self.assertTrue(failed_gates, "random data should fail at least one gate")
        self.assertEqual(report.effective_passed(card), False)
        with self.assertRaises(report.ExceptionRefused) as cm:
            report.add_exception(rid, failed_gates[0], "too short")
        self.assertEqual((cm.exception.status, cm.exception.field), (400, "reason"))
        with self.assertRaises(report.ExceptionRefused) as cm:
            report.add_exception(rid, "H", "x" * 40)
        self.assertEqual(cm.exception.status, 400)
        with self.assertRaises(report.ExceptionRefused) as cm:
            report.add_exception("bt-000000000000", "G1", "x" * 40)
        self.assertEqual(cm.exception.status, 404)
        if passed_gates:
            with self.assertRaises(report.ExceptionRefused) as cm:
                report.add_exception(rid, passed_gates[0], "x" * 40)
            self.assertIn("did not fail", cm.exception.message)
        reason = "Accepted for the calibration test: synthetic random-walk data."
        merged = report.add_exception(rid, failed_gates[0], reason)
        g = next(x for x in merged["gates"] if x["gate"] == failed_gates[0])
        self.assertEqual((g["status"], g["exception"]["reason"]), ("exception", reason))
        with self.assertRaises(report.ExceptionRefused) as cm:
            report.add_exception(rid, failed_gates[0], reason)
        self.assertEqual(cm.exception.status, 409)
        for gate in failed_gates[1:]:
            report.add_exception(rid, gate, reason)
        card = runcard.get(rid)
        self.assertEqual(card["metrics"]["validation"]["passed"], False)       # the card itself is unchanged
        self.assertTrue(report.effective_passed(card))
        self.assertTrue(any(a["action"] == "validation_exception" for a in db.list_audit(limit=200)))

        # lifecycle: draft -> backtested (the validation card is a finished run) -> candidate
        lc.transition(self.sid, "backtested", "validation run as evidence", run_id=rid)
        lc.transition(self.sid, "candidate", "all gates pass or excepted")
        self.assertEqual(lc.get(self.sid)["state"], "candidate")

        # holdout: wrong confirmation, then the real run on the eligible window
        with self.assertRaises(ValueError):
            svc.submit_holdout(self.sid, "wrong name")
        elig = families.holdout_eligibility(self.sid)
        self.assertTrue(elig["eligible"])
        self.assertEqual(elig["window"], {"start": "2024-10-08", "end": "2026-10-08"})
        hid = svc.submit_holdout(self.sid, "validation job test")
        self.wait()
        hcard = runcard.get(hid)
        self.assertEqual((hcard["kind"], hcard["status"]), ("holdout", "done"), hcard.get("error"))
        self.assertEqual(hcard["range"]["end"], "2026-10-08")
        self.assertGreaterEqual(hcard["range"]["start"], "2024-10-08")
        hb = hcard["metrics"]["holdout"]
        self.assertEqual(set(hb), {"passed", "version", "report"})
        self.assertEqual([g["gate"] for g in hb["report"]["gates"]], ["H"])
        self.assertEqual(hb["report"]["validation_run_id"], rid)
        events = db.holdout_events_list(families.family_of(self.sid))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["run_id"], hid)
        self.assertEqual(events[0]["result"], "pass" if hb["passed"] else "fail")
        after = families.holdout_eligibility(self.sid)
        self.assertFalse(after["eligible"])
        with self.assertRaises(vjobs.HoldoutRefused):
            svc.submit_holdout(self.sid, "validation job test")
        if hb["passed"]:
            lc.transition(self.sid, "validated", "holdout passed")
            self.assertEqual(lc.get(self.sid)["state"], "validated")
        else:
            self.assertIsNotNone(after["holdout_failed_at"])


class RunnerTest(Case):
    def test_busy_and_cancel_share_the_backtest_runner(self):
        from test_sp_backtest_jobs import BlockingEngine
        eng = BlockingEngine()
        self.addCleanup(eng.release.set)            # runs before the manager wait (LIFO)
        svc = vjobs.ValidationJobs(run_engine=eng, data_factory=loader)
        rid = svc.submit_validation(self.sid, None, "2016")
        self.assertTrue(eng.started.wait(10))
        self.assertEqual(eng.calls[0]["end"], date(2024, 10, 7))
        self.assertEqual(eng.calls[0]["history_from"], "2016")
        with self.assertRaises(jobs.JobBusy):
            self.mgr.submit(self.sid, None, {})
        with self.assertRaises(jobs.JobBusy):
            svc.submit_validation(self.sid, None, None)
        p = self.mgr.progress(rid)
        self.assertEqual(p["phase"], "data")
        self.assertTrue(self.mgr.cancel(rid))
        self.wait()
        card = runcard.get(rid)
        self.assertEqual((card["status"], card["error"]), ("cancelled", vjobs.CANCELLED_VALIDATION))
        self.assertIsNone(self.mgr.active_run_id())

    def test_failures(self):
        def broken(spec, **kw):
            raise DataSourceUnavailable("alpaca_sip", "equity_bars_history", "SIP refused")
        rid = vjobs.ValidationJobs(run_engine=broken, data_factory=loader).submit_validation(self.sid, None, None)
        self.wait()
        card = runcard.get(rid)
        self.assertEqual(card["status"], "failed")
        self.assertIn("SIP refused", card["error"])

        def bug(spec, **kw):
            raise RuntimeError("boom")
        rid = vjobs.ValidationJobs(run_engine=bug, data_factory=loader).submit_validation(self.sid, None, None)
        self.wait()
        self.assertEqual(runcard.get(rid)["error"], vjobs.VALIDATION_FAILURE)
        self.assertEqual(len(runcard.list_runs(self.sid, kind="validation", status="failed")), 2)

    def test_uses_only_the_public_submit_work(self):
        calls = []

        class StubManager:                       # no _lock/_job/_Job: only the public API exists
            def submit_work(self, create_card, work, *, label, generic_msg, cancelled_msg):
                calls.append((label, generic_msg, cancelled_msg))
                return "bt-stub"
        svc = vjobs.ValidationJobs(manager=StubManager())
        self.assertEqual(svc.submit_validation(self.sid, None, None), "bt-stub")
        self.assertEqual(calls, [("validation", vjobs.VALIDATION_FAILURE, vjobs.CANCELLED_VALIDATION)])

    def test_holdout_rechecked_under_the_lock(self):
        from unittest import mock
        window = {"start": "2024-10-08", "end": "2026-10-08"}
        pre = {"row": {"name": "validation job test"}, "version": 1,
               "eligibility": {"eligible": True, "window": window, "eligible_from": None},
               "validation": {"id": "bt-aaaaaaaaaaaa", "request": {}, "metrics": {"performance": {"sharpe": 1.0}}}}
        svc = vjobs.ValidationJobs(data_factory=loader)
        refused = vjobs.HoldoutRefused("already opened by a parallel request", eligible_from="2027-10-08")
        with mock.patch.object(vjobs.ValidationJobs, "holdout_preconditions", side_effect=[pre, refused]) as m:
            with self.assertRaises(vjobs.HoldoutRefused):
                svc.submit_holdout(self.sid, "validation job test")
        self.assertEqual(m.call_count, 2)                       # outside AND inside the runner's lock
        changed = dict(pre, eligibility={"eligible": True, "window": {"start": "2025-10-08", "end": "2026-10-08"},
                                         "eligible_from": None})
        with mock.patch.object(vjobs.ValidationJobs, "holdout_preconditions", side_effect=[pre, changed]):
            with self.assertRaises(vjobs.HoldoutRefused) as cm:
                svc.submit_holdout(self.sid, "validation job test")
        self.assertIn("changed meanwhile", cm.exception.message)
        self.assertEqual(runcard.list_runs(self.sid), [])
        self.assertIsNone(self.mgr.active_run_id())

    def test_holdout_needs_passing_validation(self):
        with self.assertRaises(vjobs.HoldoutRefused) as cm:
            vjobs.ValidationJobs().submit_holdout(self.sid, "validation job test")
        self.assertIn("passing validation run of version 1", cm.exception.message)
        self.assertEqual(runcard.list_runs(self.sid), [])
        self.assertEqual(db.holdout_events_list(families.family_of(self.sid)), [])


if __name__ == "__main__":
    unittest.main()
