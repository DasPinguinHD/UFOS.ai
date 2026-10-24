"""Stage 4 V1 (SPEC §9.1, §9.5): the holdout lock in the engine / run cards /
jobs / stress tests and the lifecycle rules backtested -> candidate ->
validated. Temp databases, fake data, fixed clocks, a fake
``validation.report`` (V2's module), offline. Part of UFOS.ai (proof of concept)."""
import json
import os
import sys
import types
import unittest
from datetime import date
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import strategy_platform.validation as validation_pkg  # noqa: E402
from strategy_platform.backtest import engine, jobs, runcard, stress  # noqa: E402
from strategy_platform.core.clock import FixedClock  # noqa: E402
from strategy_platform.lifecycle import registry as lc  # noqa: E402
from strategy_platform.storage import db  # noqa: E402
from strategy_platform.validation import families as fam  # noqa: E402
import test_sp_backtest_support as sup  # noqa: E402
from test_sp_families import NOW, SYMS, Case, spec_text  # noqa: E402

# last complete session 2026-03-27 (Fri) -> holdout window from 2024-03-27, training end 2024-03-26:
# inside the fake data (2024-01-02 .. 2024-04-30)
LOCK_NOW = "2026-03-27T21:00:00Z"


def loader():
    return sup.FakeLoader({"AAA": sup.series(50), "BBB": sup.series(80, drift=-0.0005), "SPY": sup.series(400)})


class EngineLockTest(unittest.TestCase):
    def setUp(self):
        fam.set_clock(FixedClock(LOCK_NOW))
        self.spec = sup.spec(SYMS)

    def tearDown(self):
        fam.set_clock(None)

    def test_default_end_is_the_training_end(self):
        res = engine.run(self.spec, data=loader())
        self.assertEqual(res.range["end"], "2024-03-26")
        self.assertIsNone(res.range["end_limited_by"])
        self.assertEqual(res.range["holdout"], {"universe_kind": "equity", "start": "2024-03-27",
                                                "end": "2026-03-27", "training_end": "2024-03-26",
                                                "access": False})
        self.assertEqual(res.dates[-1], "2024-03-26")

    def test_explicit_dates_inside_the_window_are_refused(self):
        with self.assertRaises(fam.HoldoutWindowViolation) as cm:
            engine.run(self.spec, end="2024-04-10", data=loader())
        self.assertIn("(from 2024-03-27)", cm.exception.user_message)
        with self.assertRaises(fam.HoldoutWindowViolation) as cm:
            engine.run(self.spec, start="2024-03-27", data=loader())
        self.assertTrue(cm.exception.user_message.startswith("This start date lies inside"))
        self.assertEqual(engine.run(self.spec, end="2024-03-26", data=loader()).range["end"], "2024-03-26")

    def test_holdout_access_simulates_inside_the_window(self):
        res = engine.run(self.spec, start="2024-03-27", end="2024-04-30", data=loader(), holdout_access=True)
        self.assertEqual((res.range["start"], res.range["end"]), ("2024-03-27", "2024-04-30"))
        self.assertTrue(res.range["holdout"]["access"])
        self.assertEqual(engine.run(self.spec, data=loader(), holdout_access=True).range["end"], "2024-04-30")

    def test_stress_skips_episodes_inside_the_window(self):
        base = engine.run(self.spec, data=loader())
        eps = (("early", "Early", date(2024, 2, 20), date(2024, 3, 15)),
               ("late", "Late", date(2024, 3, 1), date(2024, 4, 20)))
        out = stress.run_stress(self.spec, base, data=loader(), episodes=eps)
        by = {s["key"]: s for s in out["scenarios"]}
        self.assertEqual(by["early"]["status"], "done")
        self.assertEqual((by["late"]["status"], by["late"]["reason"]),
                         ("not_available", "not available: inside the locked holdout window"))
        self.assertEqual(by["late"]["window"], {"start": "2024-03-01", "end": "2024-04-20"})
        self.assertEqual(by["costs_x2"]["status"], "done")
        self.assertEqual(stress.holdout_start_for(self.spec, {"range": {}}), date(2024, 3, 27))


class RunCardLockTest(Case):
    def setUp(self):
        super().setUp()
        fam.set_clock(FixedClock(LOCK_NOW))
        self.sid = lc.create_from_text(spec_text("lock"), "user")["id"]

    def test_create_refuses_dates_inside_the_window_before_storing(self):
        for kind in ("backtest", "validation"):
            with self.assertRaises(fam.HoldoutWindowViolation):
                runcard.create(self.sid, request={"end": "2024-04-10"}, kind=kind)
        self.assertEqual(runcard.list_runs(self.sid), [])
        ok = runcard.create(self.sid, request={"end": "2024-03-26"})
        self.assertEqual(ok["kind"], "backtest")
        hold = runcard.create(self.sid, request={"start": "2024-03-27", "end": "2024-04-30"}, kind="holdout")
        self.assertEqual(hold["kind"], "holdout")

    def test_job_submit_raises_synchronously(self):
        mgr = jobs.JobManager(run_engine=lambda *a, **k: None)
        with self.assertRaises(fam.HoldoutWindowViolation):
            mgr.submit(self.sid, None, {"end": "2024-04-01"})
        self.assertIsNone(mgr.active_run_id())
        self.assertEqual(runcard.list_runs(self.sid), [])


class FinishHoldoutTest(Case):
    """runcard.finish_holdout: the holdout card and its event in ONE transaction (review 2026-10-09)."""

    def setUp(self):
        super().setUp()
        self.sid = lc.create_from_text(spec_text("hold"), "user")["id"]
        self.window = {"start": "2024-10-08", "end": "2026-10-08"}

    def running_holdout(self):
        card = runcard.create(self.sid, kind="holdout")
        runcard.mark_running(card["id"])
        return card["id"]

    def test_card_and_event_together(self):
        rid = self.running_holdout()
        m = dict(self.metrics, holdout={"passed": False})
        card = runcard.finish_holdout(rid, self.result, m, family_event={"window": self.window, "result": "fail"})
        self.assertEqual((card["status"], card["kind"], card["trial_number"]), ("done", "holdout", 1))
        ev = db.holdout_events_list(self.sid)
        self.assertEqual([(e["id"], e["run_id"], e["result"], e["window_end"]) for e in ev],
                         [(card["holdout_event_id"], rid, "fail", "2026-10-08")])
        self.assertEqual(fam.seen_until(self.sid), date(2026, 10, 8))
        self.assertIsNotNone(fam.holdout_failed_at(self.sid))
        self.assertIn("holdout_finished", [a["action"] for a in db.list_audit(limit=3)])

    def test_failure_mid_way_writes_neither(self):
        rid = self.running_holdout()
        real = db._insert_audit

        def boom(conn, *a):          # after the card update and the event insert, before commit
            raise RuntimeError("crash")
        with mock.patch.object(db, "_insert_audit", boom):
            with self.assertRaises(RuntimeError):
                runcard.finish_holdout(rid, self.result, self.metrics,
                                       family_event={"window": self.window, "result": "pass"})
        self.assertIs(db._insert_audit, real)
        self.assertEqual(runcard.get(rid)["status"], "running")
        self.assertIsNone(runcard.get(rid)["trial_number"])
        self.assertEqual(db.holdout_events_list(self.sid), [])
        self.assertIsNone(fam.seen_until(self.sid))

    def test_only_holdout_cards_and_valid_events(self):
        card = runcard.create(self.sid)
        runcard.mark_running(card["id"])
        with self.assertRaises(ValueError):
            runcard.finish_holdout(card["id"], self.result, self.metrics,
                                   family_event={"window": self.window, "result": "pass"})
        rid = self.running_holdout()
        with self.assertRaises(ValueError):
            runcard.finish_holdout(rid, self.result, self.metrics, family_event={"window": self.window,
                                                                                 "result": "maybe"})
        with self.assertRaises(ValueError):
            db.backtest_run_finish(rid, {}, seen_until=None, audit=("engine", "x", None),
                                   holdout_event=fam.holdout_event_row(self.sid, 1, self.window, "pass", "bt-other"))
        self.assertEqual(runcard.get(rid)["status"], "running")


def fake_report():
    mod = types.ModuleType("strategy_platform.validation.report")
    mod.effective_passed = lambda card: ((card.get("metrics") or {}).get("validation") or {}).get("passed") is True
    return mod


class LifecycleTest(Case):
    def setUp(self):
        super().setUp()
        mod = fake_report()
        self.patches = [mock.patch.dict(sys.modules, {"strategy_platform.validation.report": mod}),
                        mock.patch.object(validation_pkg, "report", mod, create=True)]
        for p in self.patches:
            p.start()
        self.sid = lc.create_from_text(spec_text("life"), "user")["id"]

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        super().tearDown()

    def allowed(self, sid=None):
        return {t["to"]: t for t in lc.allowed_transitions(lc.get(sid or self.sid))}

    def kind_card(self, kind, passed, sid=None):
        m = dict(self.metrics, **{kind: {"passed": passed, "version": "test"}})
        return self.done(sid or self.sid, metrics_=m, kind=kind)

    def to_backtested(self, sid=None):
        self.done(sid or self.sid)
        lc.transition(sid or self.sid, "backtested", "backtest ok")

    def test_candidate_needs_the_latest_validation_run_to_pass(self):
        self.assertEqual(self.allowed()["candidate"]["reason_if_not"], lc.ONLY_FROM_BACKTESTED)
        self.to_backtested()
        self.assertEqual(self.allowed()["candidate"]["reason_if_not"],
                         "needs a passing validation run of version 1 — run Validate…")
        good = self.kind_card("validation", True)
        bad = self.kind_card("validation", False)
        why = self.allowed()["candidate"]["reason_if_not"]
        self.assertIn(f"latest validation run of version 1 ({bad['id']}) did not pass", why)
        with self.assertRaises(lc.Conflict):
            lc.transition(self.sid, "candidate", "older run", run_id=good["id"])
        good2 = self.kind_card("validation", True)
        self.assertTrue(self.allowed()["candidate"]["allowed"])
        row = lc.transition(self.sid, "candidate", "all gates passed", run_id=good2["id"])
        self.assertEqual(row["state"], "candidate")
        ev = json.loads(db.strategy_transitions_list(self.sid)[-1]["evidence_json"])
        self.assertEqual(ev, {"run_id": good2["id"], "version": 1, "kind": "validation"})

    def test_validated_needs_a_passing_holdout_run(self):
        self.to_backtested()
        self.kind_card("validation", True)
        lc.transition(self.sid, "candidate", "ok")
        why = self.allowed()["validated"]["reason_if_not"]
        self.assertEqual(why, "needs a passing holdout run of version 1 — the holdout can be opened now "
                              "(once per strategy family)")
        # stage 7 (SPEC §17.2): incubation only from validated, through an incubation run
        self.assertIn("only a validated strategy can start an incubation run",
                      self.allowed()["incubation"]["reason_if_not"])
        failed = self.kind_card("holdout", False)
        self.assertIn(f"holdout run {failed['id']} of version 1 did not pass",
                      self.allowed()["validated"]["reason_if_not"])
        passed = self.kind_card("holdout", True)
        row = lc.transition(self.sid, "validated", "holdout passed", run_id=passed["id"])
        self.assertEqual(row["state"], "validated")
        ev = json.loads(db.strategy_transitions_list(self.sid)[-1]["evidence_json"])
        self.assertEqual(ev["kind"], "holdout")

    def test_contaminated_family_shows_the_eligibility_date(self):
        self.to_backtested()
        self.kind_card("validation", True)
        lc.transition(self.sid, "candidate", "ok")
        fam.record_seen(fam.family_of(self.sid), "2026-03-02")      # clock NOW: window from 2024-10-08
        why = self.allowed()["validated"]["reason_if_not"]
        self.assertIn("the holdout can be opened from 2027-03-02", why)

    def test_failed_family_cannot_move_to_validated_or_incubation(self):
        self.to_backtested()
        self.kind_card("validation", True)
        lc.transition(self.sid, "candidate", "ok")
        dup = lc.duplicate(self.sid)["id"]
        fam.record_holdout_event(self.sid, 1, fam.holdout_window("equity"), "fail", "bt-000000000001")
        self.kind_card("holdout", True)        # even a passing card cannot lift a standing family lock
        for to in ("validated", "incubation"):
            why = self.allowed()[to]["reason_if_not"]
            self.assertEqual(why, "the strategy family failed the holdout on 2026-10-09 — locked until 2027-10-08 "
                                  "(≥ 12 new unseen months), then one re-test on those months only")
        with self.assertRaises(lc.Conflict) as cm:
            lc.transition(self.sid, "validated", "please")
        self.assertIn("locked until 2027-10-08", cm.exception.message)
        self.assertEqual(lc.get(dup)["holdout_failed_until"], "2027-10-08")
        self.assertEqual(self.allowed(dup)["validated"]["reason_if_not"], lc.ONLY_FROM_CANDIDATE)

    def test_new_version_needs_new_evidence(self):
        self.to_backtested()
        self.kind_card("validation", True)
        lc.transition(self.sid, "candidate", "ok")
        lc.new_version(self.sid, spec_text("life v2"), 1)
        self.done(self.sid)
        lc.transition(self.sid, "backtested", "v2 backtest")
        self.assertEqual(self.allowed()["candidate"]["reason_if_not"],
                         "needs a passing validation run of version 2 — run Validate…")

    def test_get_carries_the_chips(self):
        row = lc.get(self.sid)
        self.assertEqual((row["family_id"], row["contaminated"], row["holdout_failed_until"], row["trial_count"]),
                         (self.sid, False, None, 0))
        self.done(self.sid)
        self.assertEqual(lc.get(self.sid)["trial_count"], 1)


class ReportMissingTest(Case):
    def test_candidate_without_v2_report_module_raises(self):
        # no stand-in rule: without validation.report the check fails loudly (V2 provides it)
        sid = lc.create_from_text(spec_text("noreport"), "user")["id"]
        self.done(sid)
        lc.transition(sid, "backtested", "ok")
        m = dict(self.metrics, validation={"passed": True})
        self.done(sid, metrics_=m, kind="validation")
        broken = types.ModuleType("strategy_platform.validation.report")
        with mock.patch.dict(sys.modules, {"strategy_platform.validation.report": broken}), \
                mock.patch.object(validation_pkg, "report", broken, create=True):
            with self.assertRaises(AttributeError):
                lc.allowed_transitions(lc.get(sid))


if __name__ == "__main__":
    unittest.main()
