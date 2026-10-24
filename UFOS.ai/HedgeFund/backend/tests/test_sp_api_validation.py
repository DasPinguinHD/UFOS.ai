"""Stage 4 (V2): /v2/validations, /v2/strategies/{id}/holdout, /v2/settings/gates
routes (SPEC §9.4, §9.5, V2 notes) — body/query validation, 404/409, the
response shapes, exceptions through the API, the holdout confirmation, the
400 mapping of HoldoutWindowViolation in POST /v2/backtests, the family /
exception chips in the strategy list and detail, route registration. The
jobs run with the fast stand-in engine of test_sp_validation_support (real
engine: test_sp_validation_jobs). Temp hedgefund.db, offline.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_api_validation -v
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strategy_platform.api import routes  # noqa: E402
from strategy_platform.backtest import jobs  # noqa: E402
from strategy_platform.core.clock import FixedClock  # noqa: E402
from strategy_platform.lifecycle import registry as lc  # noqa: E402
from strategy_platform.storage import db  # noqa: E402
from strategy_platform.validation import families, gates, jobs as vjobs  # noqa: E402
import test_sp_validation_support as sup  # noqa: E402

WAIT = 120
DETAIL_KEYS = {"id", "kind", "status", "created_at", "started_at", "finished_at", "error", "strategy_id",
               "strategy_name", "version", "content_hash", "trial_number", "request", "range", "progress",
               "key_metrics", "passed", "report", "warnings",
               "resumed_from", "resume_count", "error_kind", "auto_resume", "resumable",     # SPEC §10.4
               "attribution", "attribution_unavailable"}                                  # SPEC §11.1
LIST_KEYS = {"id", "kind", "created_at", "started_at", "finished_at", "status", "error", "strategy_id",
             "strategy_name", "version", "trial_number", "request", "range", "key_metrics", "passed", "exceptions",
             "resumed_from", "resume_count", "error_kind", "auto_resume", "resumable"}       # SPEC §10.4
HOLDOUT_KEYS = {"eligible", "reason", "window", "unseen_from", "eligible_from", "family_events", "universe_events",
                "mode", "contaminated", "holdout_failed_at", "seen_until", "universe_kind", "holdout_start",
                "version", "training_end", "validation_run_id", "validation_passed", "can_open", "blocked_reason"}
REASON = "Accepted: synthetic calibration data, the trade count limit is raised on purpose."


def call(name, body=routes.NO_BODY, query=None, match=None):
    return asyncio.run(getattr(routes.V2Api(), name)(query or {}, body, match or {}))


class Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.market = sup.Market(1)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sp4-api-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        db.close()
        db.init(os.path.join(self.tmp, "hedgefund.db"))
        self.addCleanup(db.close)
        families.set_clock(FixedClock(sup.NOW))
        self.addCleanup(families.set_clock, None)
        self.sid = lc.create_from_text(json.dumps(sup.spec_planted(name="api planted")), "user")["id"]
        self.mgr = jobs.JobManager()
        jobs.set_manager(self.mgr)
        self.addCleanup(jobs.set_manager, None)
        self.addCleanup(self.mgr.wait, WAIT)
        rs, sc = sup.fake_stress(self.market, "planted", ("2020-02-19", "2020-03-23"))
        vjobs.set_service(vjobs.ValidationJobs(run_engine=self.market.engine("planted"),
                                               compute_metrics=sup.fake_metrics, run_stress=rs, synthetic_costs=sc,
                                               data_factory=lambda: None))
        self.addCleanup(vjobs.set_service, None)

    def wait(self):
        self.assertTrue(self.mgr.wait(WAIT), "the job did not finish")

    def validate(self):
        status, payload = call("validation_create", {"strategy_id": self.sid})
        self.assertEqual(status, 202, payload)
        self.wait()
        return payload["run_id"]


class SettingsRouteTest(Case):
    def test_get_put(self):
        status, s = call("gate_settings_get")
        self.assertEqual(status, 200)
        self.assertEqual((s["preset"], s["overrides"]), ("standard", {}))
        self.assertEqual(set(s), {"preset", "overrides", "effective", "updated_at", "presets", "ranges"})
        self.assertEqual(call("gate_settings_get", query={"x": "1"})[0], 400)
        status, p = call("gate_settings_put", {"preset": "mega"})
        self.assertEqual((status, list(p["errors"])), (400, ["preset"]))
        status, p = call("gate_settings_put", {"preset": "lenient", "overrides": {"g5_pbo_max": 2}})
        self.assertEqual(status, 400)
        self.assertIn("overrides.g5_pbo_max", p["errors"])
        self.assertEqual(call("gate_settings_put", routes.NO_BODY)[0], 400)
        status, p = call("gate_settings_put", {"preset": "lenient", "overrides": {"g7_min_trades": 70}})
        self.assertEqual(status, 200, p)
        self.assertEqual((p["preset"], p["effective"]["g7_min_trades"], p["effective"]["g4_dsr_min"]),
                         ("lenient", 70, 0.90))
        self.assertEqual(len(db.list_audit(action="gate_settings_changed")), 1)


class ValidationRouteTest(Case):
    def test_create_validation(self):
        sid = self.sid
        for body, field in ((routes.NO_BODY, "_"), ({}, "strategy_id"), ({"strategy_id": sid, "x": 1}, "x"),
                            ({"strategy_id": sid, "version": 0}, "version"),
                            ({"strategy_id": sid, "version": True}, "version"),
                            ({"strategy_id": sid, "history_from": "1999"}, "history_from"),
                            ({"strategy_id": "a b"}, "strategy_id")):
            status, payload = call("validation_create", body)
            self.assertEqual(status, 400, body)
            self.assertIn(field, payload["errors"], (body, payload))
        self.assertEqual(call("validation_create", {"strategy_id": "s-000000000000"})[0], 404)
        self.assertEqual(call("validation_create", {"strategy_id": sid, "version": 9})[0], 404)
        self.assertEqual(call("validation_create", {"strategy_id": sid}, query={"a": "b"})[0], 400)

    def test_busy(self):
        from test_sp_backtest_jobs import BlockingEngine
        eng = BlockingEngine()
        self.addCleanup(eng.release.set)
        vjobs.set_service(vjobs.ValidationJobs(run_engine=eng, data_factory=lambda: None))
        status, p = call("validation_create", {"strategy_id": self.sid})
        self.assertEqual(status, 202)
        self.assertTrue(eng.started.wait(10))
        status, busy = call("validation_create", {"strategy_id": self.sid})
        self.assertEqual((status, busy["run_id"]), (409, p["run_id"]))
        self.assertEqual(busy["error"], vjobs.BUSY_MESSAGE)
        self.assertEqual(call("backtest_create", {"strategy_id": self.sid})[0], 409)
        status, d = call("validation_get", match={"id": p["run_id"]})
        self.assertEqual((status, d["status"], d["progress"]["phase"]), (200, "running", "data"))
        self.assertIsNone(d["report"])
        status, c = call("validation_cancel", match={"id": p["run_id"]})
        self.assertEqual((status, c["status"]), (202, "cancelling"))
        self.wait()
        self.assertEqual(call("validation_get", match={"id": p["run_id"]})[1]["status"], "cancelled")

    def test_full_flow(self):
        gates.put_settings({"preset": "standard", "overrides": {"g7_min_trades": 10000}})   # G7 fails on purpose
        rid = self.validate()
        status, d = call("validation_get", match={"id": rid})
        self.assertEqual(status, 200)
        self.assertEqual(set(d), DETAIL_KEYS)
        self.assertEqual((d["kind"], d["status"], d["passed"]), ("validation", "done", False), d["error"])
        self.assertIsNone(d["progress"])
        st = {g["gate"]: g["status"] for g in d["report"]["gates"]}
        self.assertEqual(st, {"G1": "pass", "G2": "pass", "G3": "pass", "G4": "pass", "G5": "pass", "G6": "pass",
                              "G7": "fail", "G8": "pass"})
        self.assertEqual(d["report"]["thresholds"]["effective"]["g7_min_trades"], 10000)
        self.assertEqual(call("validation_get", match={"id": "bt-ffffffffffff"})[0], 404)
        self.assertEqual(call("validation_get", match={"id": "../x"})[0], 404)
        # holdout not possible before the exception
        status, h = call("holdout_get", match={"id": self.sid})
        self.assertEqual(status, 200)
        self.assertEqual(set(h), HOLDOUT_KEYS)
        self.assertEqual((h["eligible"], h["validation_passed"], h["can_open"]), (True, False, False))
        self.assertEqual(h["training_end"], "2024-10-07")
        self.assertIn("passing validation", h["blocked_reason"])
        status, p = call("holdout_open", {"confirm": "api planted"}, match={"id": self.sid})
        self.assertEqual(status, 409, p)
        # exceptions
        for body, code in (({"gate": "G7", "reason": "short"}, 400), ({"gate": "H", "reason": REASON}, 400),
                           ({"gate": "G1", "reason": REASON}, 400), ({"gate": "G9", "reason": REASON}, 400),
                           ({"gate": "G7"}, 400), ({"gate": "G7", "reason": REASON, "x": 1}, 400)):
            status, p = call("validation_exception", body, match={"id": rid})
            self.assertEqual(status, code, (body, p))
            self.assertIn("errors", p)
        self.assertEqual(call("validation_exception", {"gate": "G7", "reason": REASON},
                              match={"id": "bt-ffffffffffff"})[0], 404)
        status, p = call("validation_exception", {"gate": "G7", "reason": REASON}, match={"id": rid})
        self.assertEqual(status, 200, p)
        self.assertEqual(set(p), DETAIL_KEYS)
        self.assertTrue(p["passed"])
        g7 = next(g for g in p["report"]["gates"] if g["gate"] == "G7")
        self.assertEqual((g7["status"], g7["exception"]["reason"]), ("exception", REASON))
        self.assertEqual(p["report"]["exceptions"][0]["gate"], "G7")
        self.assertEqual(call("validation_exception", {"gate": "G7", "reason": REASON}, match={"id": rid})[0], 409)
        # chips
        row = call("strategies_list")[1]["strategies"][0]
        self.assertTrue(row["has_exception"])
        self.assertEqual(row["trial_count"], 1)
        detail = call("strategy_get", match={"id": self.sid})[1]["strategy"]
        self.assertTrue(detail["has_exception"])
        self.assertFalse(detail["contaminated"])
        # lifecycle + holdout
        lc.transition(self.sid, "backtested", "validation run", run_id=rid)
        lc.transition(self.sid, "candidate", "gates pass with one exception")
        status, h = call("holdout_get", match={"id": self.sid})
        self.assertEqual((h["can_open"], h["blocked_reason"], h["validation_run_id"]), (True, None, rid))
        self.assertEqual(h["window"], {"start": "2024-10-08", "end": "2026-10-08"})
        status, p = call("holdout_open", {"confirm": "API planted"}, match={"id": self.sid})
        self.assertEqual(status, 400)
        self.assertIn("confirm", p["errors"])
        self.assertEqual(call("holdout_open", {}, match={"id": self.sid})[0], 400)
        self.assertEqual(call("holdout_open", {"confirm": "x"}, match={"id": "s-000000000000"})[0], 404)
        status, p = call("holdout_open", {"confirm": "api planted"}, match={"id": self.sid})
        self.assertEqual(status, 202, p)
        self.wait()
        status, hd = call("validation_get", match={"id": p["run_id"]})
        self.assertEqual((hd["kind"], hd["status"], hd["passed"]), ("holdout", "done", True), hd["error"])
        self.assertEqual(hd["report"]["gates"][0]["gate"], "H")
        self.assertTrue(self.market.calls[-1]["holdout_access"])
        status, h = call("holdout_get", match={"id": self.sid})
        self.assertEqual((h["eligible"], h["family_events"], h["can_open"]), (False, 1, False))
        self.assertEqual(call("holdout_open", {"confirm": "api planted"}, match={"id": self.sid})[0], 409)
        lc.transition(self.sid, "validated", "holdout passed")
        # list: validation + holdout runs, newest first
        status, lst = call("validations_list", query={"strategy_id": self.sid})
        self.assertEqual(status, 200)
        self.assertEqual([r["kind"] for r in lst["runs"]], ["holdout", "validation"])
        self.assertEqual(set(lst["runs"][0]), LIST_KEYS)
        self.assertEqual((lst["runs"][1]["passed"], lst["runs"][1]["exceptions"]), (True, 1))
        self.assertEqual(call("validations_list", query={"limit": "0"})[0], 400)
        self.assertEqual(call("validations_list", query={"bogus": "1"})[0], 400)
        self.assertEqual(len(call("validations_list", query={"limit": "1"})[1]["runs"]), 1)


class BacktestHoldoutViolationTest(Case):
    def test_end_inside_window_is_400(self):
        status, p = call("backtest_create", {"strategy_id": self.sid, "end": "2025-01-02"})
        self.assertEqual(status, 400, p)
        self.assertEqual(p["kind"], "HoldoutWindowViolation")
        self.assertEqual(p["holdout_start"], "2024-10-08")
        self.assertIn("end", p["errors"])
        self.assertIn("locked holdout window (from 2024-10-08)", p["error"])
        status, p = call("backtest_create", {"strategy_id": self.sid, "start": "2025-01-02", "end": "2025-03-03"})
        self.assertEqual(status, 400)
        self.assertEqual(p["kind"], "HoldoutWindowViolation")


class RegistrationTest(unittest.TestCase):
    def test_routes(self):
        paths = {(m, p): n for m, p, n, _ in routes.ROUTES}
        for m, p in (("POST", "/v2/validations"), ("GET", "/v2/validations"), ("GET", "/v2/validations/{id}"),
                     ("POST", "/v2/validations/{id}/exceptions"), ("POST", "/v2/validations/{id}/cancel"),
                     ("GET", "/v2/strategies/{id}/holdout"), ("POST", "/v2/strategies/{id}/holdout"),
                     ("GET", "/v2/settings/gates"), ("PUT", "/v2/settings/gates")):
            self.assertIn((m, p), paths)
            self.assertTrue(hasattr(routes.V2Api, paths[(m, p)]))


if __name__ == "__main__":
    unittest.main()
