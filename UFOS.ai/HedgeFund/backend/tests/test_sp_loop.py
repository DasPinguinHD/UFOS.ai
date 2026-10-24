"""SPEC §16 (stage 6): discovery loop — orchestrator, trials, resume, cancel, budget, topic guard, routes. Real
JobManager + real engine on the validation tests' small synthetic universe; fake LLM transport (no network).

Run from HedgeFund/backend:  python -m unittest tests.test_sp_loop -v
"""
import json
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strategy_platform.api import routes  # noqa: E402
from strategy_platform.backtest import jobs, runcard  # noqa: E402
from strategy_platform.data import network  # noqa: E402
from strategy_platform.discovery import budget, loop  # noqa: E402
from strategy_platform.storage import db  # noqa: E402
from strategy_platform.validation import families, jobs as vjobs  # noqa: E402
import test_sp_loop_support as ls  # noqa: E402
import test_sp_validation_jobs as tv  # noqa: E402

call = ls.call


class StubValidations:
    """vjobs.service() stand-in: records submit_validation; submit_holdout must never be called."""

    def __init__(self):
        self.calls = []

    def submit_validation(self, strategy_id, version, history_from, *, auto_resume=False):
        self.calls.append((strategy_id, version, history_from))
        return f"bt-stubval{len(self.calls):04d}"

    def submit_holdout(self, *a, **k):
        raise AssertionError("the loop must never open the holdout")


class LoopTest(ls.LoopCase):
    def stub_validations(self):
        stub = StubValidations()
        vjobs.set_service(stub)
        self.addCleanup(vjobs.set_service, None)
        return stub

    def test_full_loop_trials_family_memory_payload(self):
        stub = self.stub_validations()
        f = self.fake()
        lid = self.start(variants=3, generations=2, search_points=1)
        out = self.wait_loop(lid)
        self.assertEqual(out["status"], "done", out["status_reason"])
        fam = families.family_of(self.sid)
        # every backtest of the loop is one trial of the seed's family (no family bypass)
        self.assertEqual(out["counts"]["backtests_started"], 9)
        self.assertEqual(families.trial_count(fam), 9)
        self.assertEqual(out["trials"]["trials_now"], 9)
        runs = {x["run_id"] for g in out["generations"] for v in g["variants"] for x in v["versions"]}
        for rid in runs:
            card = runcard.get(rid)
            self.assertEqual((card["kind"], card["status"]), ("backtest", "done"))
            self.assertEqual(db.strategy_get(card["strategy_id"])["family_id"], fam)
            self.assertLess(card["range"]["end"], card["range"]["holdout"]["start"])       # never holdout data
            self.assertFalse(card["range"]["holdout"]["access"])
            self.assertEqual({k: v for k, v in card["request"].items() if v is not None}, {})
        # the user's seed strategy is untouched; loop strategies are drafts with AI provenance
        self.assertEqual(db.strategy_get(self.sid)["current_version"], 1)
        for g in out["generations"]:
            for v in g["variants"]:
                row = db.strategy_get(v["strategy_id"])
                if v["origin"] == "seed" or v.get("carried_from"):
                    continue
                self.assertEqual((row["origin"], row["state"]), ("loop", "draft"))
                env = json.loads(db.strategy_version_get(row["id"], 1)["envelope_json"])
                self.assertIs(env["ai_generated"], True)
                self.assertEqual(env["ai_provenance"]["loop_id"], lid)
                self.assertEqual(v["topic_check"], "passed")
                self.assertTrue(v["refinement"]["output"]["reply"])
                self.assertEqual(v["refinement"]["provenance"]["label"], "AI-generated content (EU AI Act Art. 50)")
                self.assertEqual(len(v["versions"]), 2)                                   # base + 1 search point
                self.assertEqual(v["versions"][1]["kind"], "search")
        g2 = out["generations"][1]["variants"]
        self.assertEqual([v["origin"] for v in g2], ["elite", "refined", "child"])
        self.assertEqual(g2[0]["carried_from"], out["generations"][0]["selection"]["elites"][0])
        diag = g2[1]["diagnosis"]
        self.assertEqual(diag["output"]["summary"], "The net Sharpe is [score.sharpe] after costs.")
        self.assertTrue(diag["check"]["ok"], diag["check"])
        # LLM calls: 2 mutations + 2 × (diagnosis + refinement) + 4 topic checks; prompts hold no holdout ids
        self.assertEqual({k: len(v) for k, v in f.calls.items()}, {"guard": 4, "diagnosis": 2, "refine": 4, "seed": 0})
        for m in f.calls["diagnosis"] + f.calls["refine"]:
            self.assertNotIn("gate.H.", m[1]["content"])
            self.assertNotIn('"holdout.', m[1]["content"])
        # memory: run facts via the finish hook + one loop_result fact
        kinds = {x["kind"] for x in db.memory_facts_list(family_id=fam, limit=500)}
        self.assertIn("loop_result", kinds)
        self.assertIn("metrics", kinds)
        # approval mode: finalists proposed; approve -> validation (never holdout); payload is strict JSON
        self.assertFalse(out["config"]["autonomous"])
        fin = out["finalists"]
        self.assertTrue(fin)
        self.assertEqual(fin[0]["state"], "proposed")
        routes.dumps(out)
        status, after = call("loop_finalist_approve", match={"id": lid, "variant_id": fin[0]["variant_id"]})
        self.assertEqual(status, 200, after)
        self.assertEqual(stub.calls, [(fin[0]["strategy_id"], fin[0]["version"], None)])
        self.assertEqual(after["finalists"][0]["validation_run_id"], "bt-stubval0001")
        status, again = call("loop_finalist_approve", match={"id": lid, "variant_id": fin[0]["variant_id"]})
        self.assertEqual((status, again["code"]), (409, "decided"))
        self.assertEqual(out["ai_interaction_notice"], loop.AI_INTERACTION_NOTICE)
        self.assertIn("never opens the holdout", out["holdout_note"])
        status, lst = call("loops_list")
        self.assertEqual((status, lst["loops"][0]["id"]), (200, lid))

    def test_autonomous_mode_validates_finalists_itself(self):
        stub = self.stub_validations()
        self.set_budget(autonomous=True)
        from strategy_platform.discovery import selection
        orig = selection.finalists
        # random data rarely passes the pre-checks; the finalist rule itself is tested in test_sp_loop_units
        selection.finalists = lambda pop, n, d: [v["variant_id"] for v in sorted(pop, key=selection.rank_key)
                                                 if v.get("score") is not None][:1]
        self.addCleanup(setattr, selection, "finalists", orig)
        self.fake()
        lid = self.start(variants=2, generations=1, search_points=0)
        out = self.wait_loop(lid)
        self.assertEqual(out["status"], "done", out["status_reason"])
        self.assertTrue(out["config"]["autonomous"])
        fins = out["finalists"]
        self.assertTrue(fins)
        self.assertEqual(stub.calls, [(x["strategy_id"], x["version"], None) for x in fins])
        self.assertTrue(all(x["validation_run_id"] for x in fins))
        status, r = call("loop_finalist_approve", match={"id": lid, "variant_id": fins[0]["variant_id"]})
        self.assertEqual((status, r["code"]), (409, "autonomous"))
        # a passing validation = "passed_gates" (never the lifecycle state "validated"); the loop changes no state
        from strategy_platform.validation import report
        orig_get, orig_passed = runcard.get, report.effective_passed
        runcard.get = lambda rid: ({"id": rid, "status": "done", "kind": "validation"} if rid.startswith("bt-stubval")
                                   else orig_get(rid))
        report.effective_passed = lambda card: True
        try:
            f0 = loop.get_loop(lid)["finalists"][0]
        finally:
            runcard.get, report.effective_passed = orig_get, orig_passed
        self.assertEqual((f0["state"], f0["passed"], f0["eligible_state"]), ("passed_gates", True, "candidate"))
        self.assertEqual(db.strategy_get(f0["strategy_id"])["state"], "draft")

    def test_reused_seed_card_is_no_new_trial(self):
        rid = self.mgr.submit(self.sid, None, {})
        self.mgr.wait(tv.WAIT)
        self.assertEqual(runcard.get(rid)["status"], "done")
        self.fake()
        lid = self.start(variants=2, generations=1, search_points=0)
        out = self.wait_loop(lid)
        seed = out["generations"][0]["variants"][0]
        self.assertEqual((seed["versions"][0]["run_id"], seed["versions"][0]["reused"]), (rid, True))
        self.assertEqual(out["counts"]["backtests_started"], 1)
        self.assertEqual(families.trial_count(families.family_of(self.sid)), 2)

    def test_reused_seed_card_with_explicit_defaults_and_stress(self):
        # click-test 2026-10-22: the WPF sends benchmark / capital explicitly and asks for stress scenarios — the
        # base run is the same, so the loop reuses it (no new trial)
        rid = self.mgr.submit(self.sid, None, {"benchmark": "SPY", "capital": 100000.0, "stress": True})
        self.mgr.wait(tv.WAIT)
        self.assertEqual(runcard.get(rid)["status"], "done")
        self.fake()
        lid = self.start(variants=2, generations=1, search_points=0)
        out = self.wait_loop(lid)
        seed = out["generations"][0]["variants"][0]
        self.assertEqual((seed["versions"][0]["run_id"], seed["versions"][0]["reused"]), (rid, True))
        self.assertEqual(families.trial_count(families.family_of(self.sid)), 2)

    def test_estimate_counts_a_reusable_seed_card(self):
        before = loop.estimate(self.sid, 2, 1, 0, "standard", False)
        self.assertFalse(before["seed_reused"])
        self.assertEqual(before["backtests_planned"], loop.planned_backtests(2, 1, 0, False))
        rid = self.mgr.submit(self.sid, None, {"benchmark": "SPY", "capital": 100000.0})
        self.mgr.wait(tv.WAIT)
        self.assertEqual(runcard.get(rid)["status"], "done")
        after = loop.estimate(self.sid, 2, 1, 0, "standard", False)
        self.assertTrue(after["seed_reused"])
        self.assertEqual(after["backtests_planned"], before["backtests_planned"] - 1)
        self.assertEqual(after["backtests_planned"], loop.planned_backtests(2, 1, 0, True))
        self.assertFalse(loop.estimate(self.sid, 2, 1, 0, "standard", False, "2000")["seed_reused"])
        status, out = call("loop_estimate", query={"strategy_id": self.sid, "variants": "2", "generations": "1",
                                                   "search_points": "0"})
        self.assertEqual((status, out["seed_reused"], out["backtests_planned"]), (200, True, after["backtests_planned"]))

    def test_seed_card_with_other_capital_is_not_reused(self):
        rid = self.mgr.submit(self.sid, None, {"capital": 50000.0})
        self.mgr.wait(tv.WAIT)
        self.fake()
        lid = self.start(variants=2, generations=1, search_points=0)
        out = self.wait_loop(lid)
        seed = out["generations"][0]["variants"][0]
        self.assertNotEqual(seed["versions"][0]["run_id"], rid)

    def test_logic_duplicate_is_never_backtested(self):
        self.fake(refine=lambda m, n: ls.smooth_spec(1, name="same logic as the seed"))
        lid = self.start(variants=2, generations=1, search_points=1)
        out = self.wait_loop(lid)
        v = out["generations"][0]["variants"][1]
        self.assertEqual(v["state"], "duplicate")
        self.assertIn("not backtested", v["reason"])
        self.assertEqual(v["versions"], [])
        self.assertEqual(families.trial_count(families.family_of(self.sid)), 1)       # the seed only

    def test_topic_check_failure_hides_texts_and_skips_the_variant(self):
        self.fake(guard=json.dumps({"on_topic": False, "reason": "philosophy"}))
        lid = self.start(variants=2, generations=1, search_points=0)
        out = self.wait_loop(lid)
        v = out["generations"][0]["variants"][1]
        self.assertEqual((v["state"], v["topic_check"]), ("failed", "failed"))
        self.assertEqual(v["reason"], loop.TOPIC_FAILED)
        self.assertIsNone(v["refinement"]["output"])
        self.assertEqual((v["refinement"]["discarded"], v["refinement"]["discarded_reason"]),
                         (True, "topic_check_failed"))
        self.assertIsNone(v["strategy_id"])
        self.assertNotIn("philosophy", json.dumps(out))                 # the guard's reason stays in the audit

    def test_off_topic_and_invalid_refinements_are_discarded(self):
        answers = [ls.draft(None, reply="", kind="off_topic"), ls.draft({"schema": "ufos.strategy/1", "name": "bad"})]
        self.fake(refine=lambda m, n: answers[min(n, len(answers)) - 1] if n <= 1 else
                  ls.draft({"schema": "ufos.strategy/1", "name": "still bad"}))
        lid = self.start(variants=3, generations=1, search_points=0)
        out = self.wait_loop(lid)
        a, b = out["generations"][0]["variants"][1:]
        self.assertEqual(a["state"], "failed")
        self.assertIn("off_topic", a["reason"])
        self.assertEqual(b["state"], "failed")
        self.assertIn("validator", b["reason"])
        self.assertEqual(b["refinement"]["discarded_reason"], "draft_invalid")
        self.assertEqual(b["refinement"]["output"]["validation"]["ok"], False)        # only the validator card
        self.assertEqual(set(b["refinement"]["output"]), {"validation"})              # no AI spec texts (M3)

    def test_injection_in_the_parent_spec_stays_quoted_data(self):
        s = json.loads(db.strategy_version_get(self.sid, 1)["spec_json"])
        s["description"] = ls.INJECTION
        from strategy_platform.lifecycle import registry as lc
        sid = lc.create_from_text(json.dumps(s), "user")["id"]
        f = self.fake()
        lid = self.start(seed={"kind": "strategy", "strategy_id": sid}, variants=2, generations=1, search_points=0)
        self.wait_loop(lid)
        msg = f.calls["refine"][0]
        self.assertNotIn(ls.INJECTION, msg[0]["content"])
        self.assertEqual(ls.data_of(msg)["parent_spec"]["description"], ls.INJECTION)

    def test_budget_off_refuses_the_start(self):
        self.set_budget(monthly_usd=0.0)
        self.fake()
        status, out = call("loop_create", {"seed": {"kind": "strategy", "strategy_id": self.sid}})
        self.assertEqual((status, out["code"]), (402, "budget"))
        self.assertEqual(out["error"], budget.OFF_MESSAGE)
        self.assertEqual(db.s5b_select("loop_runs"), [])

    def test_day_cap_pauses_and_resume_continues_without_recomputing(self):
        spec = json.loads(db.strategy_version_get(self.sid, 1)["spec_json"])
        est = loop._first_call_estimate("standard", spec)
        self.set_budget(daily_usd=round(est * 1.6, 6))
        f = self.fake(cost=round(est * 0.8, 6))
        lid = self.start(variants=3, generations=1, search_points=0)
        out = self.wait_loop(lid)
        self.assertEqual(out["status"], "paused_budget")
        self.assertEqual(out["status_reason"], budget.DAY_REACHED_MESSAGE)
        self.assertTrue(out["can"]["resume"])
        n_refine, trials = len(f.calls["refine"]), families.trial_count(families.family_of(self.sid))
        self.set_budget(daily_usd=50.0)
        status, r = call("loop_resume", match={"id": lid})
        self.assertEqual(status, 200, r)
        out = self.wait_loop(lid)
        self.assertEqual(out["status"], "done", out["status_reason"])
        self.assertEqual(len(f.calls["refine"]), n_refine + 1)          # only the blocked step is called again
        self.assertEqual(families.trial_count(families.family_of(self.sid)), out["counts"]["backtests_started"])
        self.assertGreater(out["counts"]["backtests_started"], trials)
        evs = [e["event"] for e in out["events"]]
        self.assertEqual(evs.count("running"), 2)

    def test_run_cap_stops_creating_variants(self):
        spec = json.loads(db.strategy_version_get(self.sid, 1)["spec_json"])
        est = loop._first_call_estimate("standard", spec)
        self.set_budget(per_run_usd=round(est * 1.6, 6))
        self.fake(cost=round(est * 0.8, 6))
        lid = self.start(variants=3, generations=2, search_points=0)
        out = self.wait_loop(lid)
        self.assertEqual(out["status"], "done", out["status_reason"])
        self.assertTrue(out["run_cap_reached"])
        self.assertEqual(out["done_note"], loop.RUN_CAP_NOTE)
        self.assertEqual(len(out["generations"]), 1)
        states = [v["state"] for v in out["generations"][0]["variants"]]
        self.assertIn("failed", states)
        self.assertIn("scored", states)

    def test_cancel_during_a_backtest(self):
        self.fake()
        lid = self.start(variants=3, generations=2, search_points=1)
        end = time.time() + 60
        while time.time() < end and (loop._progress.get(lid) or {}).get("phase") != "backtest":
            time.sleep(0.01)
        status, r = call("loop_cancel", match={"id": lid})
        self.assertEqual(status, 200, r)
        out = self.wait_loop(lid)
        self.assertEqual(out["status"], "cancelled")
        self.assertFalse(out["active"])
        self.assertIsNone(self.mgr.active_run_id())
        status, r = call("loop_cancel", match={"id": lid})
        self.assertEqual((status, r["code"]), (409, "not_running"))
        status, r = call("loop_resume", match={"id": lid})
        self.assertEqual((status, r["code"]), (409, "not_resumable"))

    def test_pause_and_resume(self):
        f = self.fake(delay=0.2)
        lid = self.start(variants=3, generations=1, search_points=0)
        time.sleep(0.1)
        status, r = call("loop_pause", match={"id": lid})
        self.assertEqual(status, 200, r)
        out = self.wait_loop(lid)
        self.assertEqual(out["status"], "paused")
        status, r = call("loop_resume", match={"id": lid})
        self.assertEqual(status, 200, r)
        out = self.wait_loop(lid)
        self.assertEqual(out["status"], "done", out["status_reason"])
        self.assertEqual(len(f.calls["refine"]), 2)

    def test_crash_recovery_resumes_from_the_events(self):
        class Crash(BaseException):
            pass

        orig = loop._Runner.select
        state = {"n": 0}

        def crashing(self_, g):
            state["n"] += 1
            if state["n"] == 1:
                raise Crash()                       # like a killed backend: no status event is written
            return orig(self_, g)

        loop._Runner.select = crashing
        self.addCleanup(setattr, loop._Runner, "select", orig)
        old_hook = threading.excepthook
        threading.excepthook = lambda args: None
        self.addCleanup(setattr, threading, "excepthook", old_hook)
        f = self.fake()
        lid = self.start(variants=3, generations=2, search_points=0)
        out = self.wait_loop(lid)
        self.assertEqual(out["persisted_status"], "running")
        n_refine, started = len(f.calls["refine"]), out["counts"]["backtests_started"]
        self.assertEqual(n_refine, 2)
        self.assertEqual(loop.recover(), [lid])                 # interrupted + auto_resume (default true)
        out = self.wait_loop(lid)
        self.assertEqual(out["status"], "done", out["status_reason"])
        evs = [e["event"] for e in out["events"]]
        self.assertIn("interrupted", evs)
        self.assertEqual(len(f.calls["refine"]), n_refine + 2)  # generation 1 not recomputed; generation 2 runs
        self.assertEqual(out["counts"]["backtests_started"], started + 2)
        self.assertEqual(families.trial_count(families.family_of(self.sid)), out["counts"]["backtests_started"])

    def test_connection_loss_waits_and_resumes_the_run(self):
        import test_sp_resume as tr
        flaky = tr.FlakyLoader(tv.PRICES, fail=1, when=lambda s, a, b: s == "AAA")
        flaky_used = {"n": 0}

        def factory():
            flaky_used["n"] += 1
            return flaky if flaky_used["n"] == 2 else tv.loader()   # the 2nd backtest loses the connection once
        self.mgr._data_factory = factory
        probe, nxt = network.probe, network.next_probe_in
        network.probe = lambda source: True
        network.next_probe_in = lambda source: 0.0
        self.addCleanup(setattr, network, "probe", probe)
        self.addCleanup(setattr, network, "next_probe_in", nxt)
        old = loop.PROBE_INTERVAL_SECONDS
        loop.PROBE_INTERVAL_SECONDS = 0.05
        self.addCleanup(setattr, loop, "PROBE_INTERVAL_SECONDS", old)
        self.fake()
        lid = self.start(variants=2, generations=1, search_points=0, auto_resume=False)
        out = self.wait_loop(lid)
        self.assertEqual(out["status"], "done", out["status_reason"])
        resumed = [e for e in out["events"] if e["event"] == "backtest_started" and e["data"].get("resumed_from")]
        self.assertEqual(len(resumed), 1)
        new = runcard.get(resumed[0]["data"]["run_id"])
        self.assertEqual((new["status"], new["resumed_from"]), ("done", resumed[0]["data"]["resumed_from"]))
        self.assertEqual(families.trial_count(families.family_of(self.sid)), 2)      # the chain is ONE trial

    def test_one_loop_at_a_time_and_busy_409_names_the_loop(self):
        self.fake(delay=0.3)
        lid = self.start(variants=3, generations=1, search_points=0)
        status, r = call("loop_create", {"seed": {"kind": "strategy", "strategy_id": self.sid}})
        self.assertEqual((status, r["code"], r["loop_id"]), (409, "busy", lid))
        end = time.time() + 60
        while time.time() < end and (loop._progress.get(lid) or {}).get("phase") != "backtest":
            time.sleep(0.01)
        status, r = call("backtest_create", {"strategy_id": self.sid})
        if status == 409:                                # the loop's job holds the slot
            self.assertEqual(r.get("loop_id"), lid)
        self.wait_loop(lid)


class Crash(BaseException):
    """Like a killed backend: no status event is written."""


class ReviewFixTest(ls.LoopCase):
    def quiet_thread_errors(self):
        old = threading.excepthook
        threading.excepthook = lambda args: None
        self.addCleanup(setattr, threading, "excepthook", old)

    def crash_on_select(self):
        orig = loop._Runner.select
        state = {"n": 0}

        def crashing(self_, g):
            state["n"] += 1
            if state["n"] == 1:
                raise Crash()
            return orig(self_, g)
        loop._Runner.select = crashing
        self.addCleanup(setattr, loop._Runner, "select", orig)
        self.quiet_thread_errors()

    def test_m1_cancel_survives_a_restart(self):
        self.crash_on_select()
        self.fake()
        lid = self.start(variants=2, generations=2, search_points=0)
        out = self.wait_loop(lid)
        self.assertEqual(out["persisted_status"], "running")
        loop._event(lid, "cancel_requested")              # the cancel arrived, then the backend stopped
        self.assertEqual(loop.recover(), [])
        out = loop.get_loop(lid)
        self.assertEqual(out["status"], "cancelled")
        self.assertFalse(loop.is_active(lid))
        status, r = call("loop_resume", match={"id": lid})
        self.assertEqual((status, r["code"]), (409, "not_resumable"))

    def test_m1_resume_refused_after_a_cancel_request(self):
        self.fake()
        lid = self.start(variants=2, generations=1, search_points=0)
        self.wait_loop(lid)
        st = loop.State(lid)
        self.assertEqual(st.status, "done")
        lid2 = "lr-" + "a" * 12
        db.s5b_insert("loop_runs", {"id": lid2, "seed_kind": "strategy", "seed_json": json.dumps(st.seed_in),
                                    "config_json": json.dumps(st.config)})
        for ev in ("created", "running", "cancel_requested", "paused"):
            loop._event(lid2, ev)
        status, r = call("loop_resume", match={"id": lid2})
        self.assertEqual((status, r["code"]), (409, "not_resumable"))
        self.assertIn("cancelled", r["error"])

    def test_m4_conflict_and_start_failure_fail_the_variant_not_the_loop(self):
        from strategy_platform.core.errors import UserFacingError
        from strategy_platform.lifecycle import registry as lc
        orig_new = lc.new_loop_version

        def conflict(*a, **k):
            raise lc.Conflict("The strategy was changed meanwhile (current version 3).")
        lc.new_loop_version = conflict
        self.addCleanup(setattr, lc, "new_loop_version", orig_new)
        orig_submit = self.mgr.submit
        calls = {"n": 0, "fail_at": 2}

        def submit(sid, version, request, **kw):
            calls["n"] += 1
            if calls["n"] == calls["fail_at"]:                      # this backtest cannot start
                raise UserFacingError("Tiingo key missing.")
            return orig_submit(sid, version, request, **kw)
        self.mgr.submit = submit
        self.fake()
        lid = self.start(variants=3, generations=1, search_points=1)
        out = self.wait_loop(lid)
        self.assertEqual(out["status"], "done", out["status_reason"])
        a, b = out["generations"][0]["variants"][1:]
        states = sorted([(a["state"], a["reason"]), (b["state"], b["reason"])])
        self.assertTrue(all(x[0] == "failed" for x in states), states)
        reasons = " | ".join(x[1] for x in states)
        self.assertIn("changed meanwhile", reasons)
        codes = {e["data"].get("code") for e in out["events"] if e["event"] == "variant_failed"}
        self.assertEqual(codes, {"conflict"})                      # search point creation conflicted twice
        # start failure: a separate run without the conflict patch
        lc.new_loop_version = orig_new
        calls.update(n=0, fail_at=1)                               # the seed card is reused: 1st submit = mutation
        lid = self.start(variants=2, generations=1, search_points=0)
        out = self.wait_loop(lid)
        self.assertEqual(out["status"], "done", out["status_reason"])
        v = out["generations"][0]["variants"][1]
        self.assertEqual(v["state"], "failed")
        self.assertIn("Tiingo key missing", v["reason"])
        self.assertEqual(v["versions"][0]["run_status"], "not_run")

    def test_m4_resume_refused_falls_back_to_a_fresh_backtest(self):
        import test_sp_resume as tr
        flaky = tr.FlakyLoader(tv.PRICES, fail=1, when=lambda s, a, b: s == "AAA")
        used = {"n": 0}

        def factory():
            used["n"] += 1
            return flaky if used["n"] == 2 else tv.loader()
        self.mgr._data_factory = factory
        probe, nxt, resume = network.probe, network.next_probe_in, jobs.resume
        network.probe = lambda source: True
        network.next_probe_in = lambda source: 0.0

        def refused(run_id, **kw):
            raise runcard.ResumeRefused("The basis changed (test) — start a new run.")
        jobs.resume = refused
        for mod, name, val in ((network, "probe", probe), (network, "next_probe_in", nxt), (jobs, "resume", resume)):
            self.addCleanup(setattr, mod, name, val)
        old = loop.PROBE_INTERVAL_SECONDS
        loop.PROBE_INTERVAL_SECONDS = 0.05
        self.addCleanup(setattr, loop, "PROBE_INTERVAL_SECONDS", old)
        self.fake()
        lid = self.start(variants=2, generations=1, search_points=0, auto_resume=False)
        out = self.wait_loop(lid)
        self.assertEqual(out["status"], "done", out["status_reason"])
        v = out["generations"][0]["variants"][1]
        self.assertEqual(v["state"], "scored")
        started = [e for e in out["events"] if e["event"] == "backtest_started" and e["variant_id"] == v["id"]]
        self.assertEqual(len(started), 2)                           # the failed run + a fresh one
        self.assertEqual(families.trial_count(families.family_of(self.sid)), 2)

    def test_l5_budget_block_in_the_topic_check_pauses_and_resume_reuses_the_answer(self):
        def refine(m, n):
            budget.apply({"daily_usd": 0.0001})          # the cap drops while the refinement is on its way
            return ls.smooth_spec(n + 1)
        f = self.fake(refine=refine)
        lid = self.start(variants=2, generations=1, search_points=0)
        out = self.wait_loop(lid)
        self.assertEqual((out["status"], out["status_reason"]), ("paused_budget", budget.DAY_REACHED_MESSAGE))
        v = out["generations"][0]["variants"][1]
        self.assertEqual(v["state"], "ai")
        self.assertTrue(v["refinement"]["pending"])
        self.assertIsNone(v["refinement"]["output"])
        self.assertEqual((len(f.calls["refine"]), len(f.calls["guard"])), (1, 0))
        self.set_budget(daily_usd=50.0)
        call("loop_resume", match={"id": lid})
        out = self.wait_loop(lid)
        self.assertEqual(out["status"], "done", out["status_reason"])
        self.assertEqual((len(f.calls["refine"]), len(f.calls["guard"])), (1, 1))   # the stored answer is reused
        v = out["generations"][0]["variants"][1]
        self.assertEqual((v["state"], v["topic_check"]), ("scored", "passed"))

    def test_l5_run_cap_in_the_topic_check_ends_the_loop_early(self):
        def refine(m, n):
            budget.apply({"per_run_usd": 0.0001})
            return ls.smooth_spec(n + 1)
        self.fake(refine=refine)
        lid = self.start(variants=2, generations=2, search_points=0)
        out = self.wait_loop(lid)
        self.assertEqual(out["status"], "done", out["status_reason"])
        self.assertTrue(out["run_cap_reached"])
        v = out["generations"][0]["variants"][1]
        self.assertEqual(v["state"], "failed")
        self.assertIn("Per-run AI cap", v["reason"])
        self.assertEqual(v["refinement"]["discarded_reason"], "not_topic_checked")   # L6: finalised, not shown

    def test_l6_diagnosis_of_a_failed_variant_is_finalised(self):
        self.fake(refine=lambda m, n: ls.smooth_spec(n + 1) if n == 1 else ls.draft(None, reply="", kind="off_topic"))
        lid = self.start(variants=2, generations=2, search_points=0)
        out = self.wait_loop(lid)
        self.assertEqual(out["status"], "done", out["status_reason"])
        child = out["generations"][1]["variants"][1]
        self.assertEqual(child["state"], "failed")
        d = child["diagnosis"]
        self.assertEqual((d["pending"], d["topic_check"], d["discarded"], d["discarded_reason"], d["output"]),
                         (False, "skipped", True, "not_topic_checked", None))
        self.assertIsNotNone(d["cost_usd"])

    def test_l7_projected_trials_include_autonomous_validations(self):
        self.set_budget(autonomous=True)
        status, e = call("loop_estimate", query={"strategy_id": self.sid, "variants": "6", "generations": "4"})
        self.assertEqual(status, 200, e)
        self.assertEqual(e["trials"]["validations_planned"], 2)
        self.assertEqual(e["trials"]["trials_projected"], e["trials"]["trials_now"] + 35 + 2)

    def test_l8_planning_is_atomic_and_a_crash_cannot_drop_the_elites(self):
        orig = db.s5b_insert_many
        state = {"n": 0}

        def crashing(rows, audit=None):
            state["n"] += 1
            if state["n"] == 2:                                    # generation 2: fail INSIDE the transaction
                try:
                    orig(list(rows) + [("loop_events", {"bogus": 1})])
                except ValueError:
                    raise Crash()
            return orig(rows, audit)
        db.s5b_insert_many = crashing
        self.addCleanup(setattr, db, "s5b_insert_many", orig)
        self.quiet_thread_errors()
        self.fake()
        lid = self.start(variants=3, generations=2, search_points=0)
        out = self.wait_loop(lid)
        self.assertEqual(out["persisted_status"], "running")
        self.assertEqual(len(out["generations"]), 1)
        self.assertEqual([r for r in db.s5b_select("loop_variants", {"loop_id": lid}) if r["generation"] == 2], [])
        self.assertEqual(loop.recover(), [lid])
        out = self.wait_loop(lid)
        self.assertEqual(out["status"], "done", out["status_reason"])
        g2 = out["generations"][1]["variants"]
        self.assertEqual([v["origin"] for v in g2], ["elite", "refined", "child"])
        self.assertEqual((g2[0]["state"], g2[0]["carried_from"]), ("scored", out["generations"][0]["selection"]["elites"][0]))


class RoutesTest(ls.LoopCase):
    def test_validation_errors_and_not_found(self):
        for body in ({}, {"seed": {"kind": "x"}}, {"seed": {"kind": "idea", "text": ""}}, {"seed": [], "x": 1},
                     {"seed": {"kind": "strategy", "strategy_id": "s-1"}, "variants": 99}):
            status, r = call("loop_create", body)
            self.assertEqual(status, 400, (body, r))
            self.assertIn("errors", r)
        status, r = call("loop_create", {"seed": {"kind": "strategy", "strategy_id": "s-000000000000"}})
        self.assertEqual((status, r["code"]), (404, "not_found"))
        status, r = call("loop_create", {"seed": {"kind": "paper", "paper_id": "p-000000000000"}})
        self.assertEqual((status, r["code"]), (404, "not_found"))
        status, r = call("loop_create", {"seed": {"kind": "strategy", "strategy_id": self.sid}}, query={"x": "1"})
        self.assertEqual(status, 400)
        for name in ("loop_get", "loop_cancel", "loop_pause", "loop_resume"):
            status, r = call(name, match={"id": "lr-000000000000"})
            self.assertEqual((status, r["code"]), (404, "not_found"), name)
            status, r = call(name, match={"id": "../etc"})
            self.assertEqual(status, 404, name)
        status, r = call("loop_get", query={"events_after": "abc"}, match={"id": "lr-000000000000"})
        self.assertEqual(status, 400)
        status, r = call("loop_finalist_reject", {"reason": "x" * 501}, match={"id": "lr-000000000000",
                                                                               "variant_id": "lv-000000000000"})
        self.assertEqual(status, 400)
        status, r = call("loops_list", query={"limit": "0"})
        self.assertEqual(status, 400)

    def test_settings_and_estimate(self):
        status, s = call("loop_settings_get")
        self.assertEqual(status, 200, s)
        self.assertEqual((s["autonomous"], [x["value"] for x in s["budget"]["limits"]]), (False, [2.0, 10.0, 1.0]))
        for bad in ({}, {"autonomous": "yes"}, {"monthly_usd": -1}, {"daily_usd": 201}, {"foo": 1},
                    {"per_run_usd": True}):
            status, r = call("loop_settings_put", bad)
            self.assertEqual(status, 400, bad)
        s = self.set_budget(autonomous=True, per_run_usd=0.5)
        self.assertTrue(s["autonomous"])
        self.assertEqual(s["budget"]["limits"][2]["value"], 0.5)
        actions = [r["action"] for r in db.list_audit(20)]
        self.assertIn("loop_settings_changed", actions)
        self.assertIn("loop_budget_changed", actions)
        status, e = call("loop_estimate", query={"strategy_id": self.sid, "variants": "6", "generations": "4"})
        self.assertEqual(status, 200, e)
        self.assertEqual((e["backtests_planned"], e["ai_calls_planned"]), (35, 46))
        self.assertGreater(e["estimate_usd"], 0)
        self.assertEqual(e["trials"]["validations_planned"], 2)                    # autonomous was switched on
        self.assertEqual(e["trials"]["trials_projected"], e["trials"]["trials_now"] + 35 + 2)
        self.assertGreater(e["trials"]["sr0_projected"], e["trials"]["sr0_now"])
        status, e = call("loop_estimate", query={"variants": "9"})
        self.assertEqual(status, 400)

    def test_routes_registered_behind_auth(self):
        import api_auth
        names = {n for _m, _p, n, _b in routes.ROUTES}
        for n in ("loop_create", "loops_list", "loop_estimate", "loop_get", "loop_cancel", "loop_pause", "loop_resume",
                  "loop_finalist_approve", "loop_finalist_reject", "loop_settings_get", "loop_settings_put"):
            self.assertIn(n, names)
        for m, p, n, _b in routes.ROUTES:
            if "/loops" in p or p.endswith("/settings/loop"):
                path = p.replace("{id}", "lr-1").replace("{variant_id}", "lv-1")
                self.assertIsNotNone(api_auth.check(m, path, {}, frozenset()), p)

    def test_insert_only_tables(self):
        import sqlite3
        self.fake()
        lid = self.start(variants=2, generations=1, search_points=0)
        self.wait_loop(lid)
        with db._connect() as conn:
            for t in ("loop_runs", "loop_variants", "loop_events", "loop_ai_messages"):
                with self.assertRaises(sqlite3.DatabaseError, msg=t):
                    conn.execute(f"UPDATE {t} SET created_at = 'x'")
                with self.assertRaises(sqlite3.DatabaseError, msg=t):
                    conn.execute(f"DELETE FROM {t}")


if __name__ == "__main__":
    unittest.main()
