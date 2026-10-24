"""SPEC §14.3 (stage 5b): investment committee — role order, structured input without holdout facts
(included + labelled only when the holdout was opened for this version), memo number check with semantic
section keys, cancel between roles, budget blocks, manual vs automatic trigger (job after-hook), insert-only,
routes. No network (TRANSPORT fake), temp hedgefund.db.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_committee -v
"""
import asyncio
import json
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strategy_platform.core import llm  # noqa: E402
from strategy_platform.api import routes  # noqa: E402
from strategy_platform.backtest import runcard  # noqa: E402
from strategy_platform.research import committee, studio  # noqa: E402
from strategy_platform.storage import db  # noqa: E402
import test_sp_validation_jobs as tv  # noqa: E402

COST = 0.002


def call(name, body=routes.NO_BODY, query=None, match=None):
    return asyncio.run(getattr(routes.V2Api(), name)(query or {}, body, match or {}))


def data_of(messages):
    return json.loads(messages[1]["content"].partition("\n")[2])


def role_answer(messages, invented="7.77 %"):
    d = data_of(messages)
    role = d["your_role"]
    fact = d["facts"][0]
    if role != "chair":
        return json.dumps({"summary": f"{role}: {fact['display']} [{fact['id']}].", "points": [f"{role} point"]})
    return json.dumps({"summary": f"Memo: {fact['display']} [{fact['id']}], but also {invented}.",
                       "arguments_for": ["Costs are low."], "arguments_against": ["Gate G4 failed."],
                       "confidence": {"value": 0.3, "reason": "Weak gates."},
                       "sizing": {"percent_of_capital": 2, "rules": ["Halve size after a 10 % drawdown."]},
                       "stop_rules": ["Stop below -15 %."], "open_questions": ["Is the edge regime-specific?"]})



GUARD_OK = json.dumps({"on_topic": True, "reason": "about the strategy"})


def guard_reply(fake, messages):
    """The independent topic check (topic_guard) — answered separately from the scripted answers."""
    from strategy_platform.research import topic_guard
    if messages and messages[0]["content"] == topic_guard.SYSTEM_PROMPT:
        fake.guard_calls.append(messages)
        g = fake.guard
        return g(messages) if callable(g) else g
    return None


class Fake:
    def __init__(self, answer=role_answer, gate=None, guard=GUARD_OK):
        self.answer, self.gate, self.calls, self.guard, self.guard_calls = answer, gate, [], guard, []

    async def __call__(self, messages, model, max_tokens, temperature):
        g = guard_reply(self, messages)
        if g is not None:
            return g, "test/guard", {"cost_usd": 0.0, "prompt_tokens": 1, "completion_tokens": 1}
        self.calls.append(messages)
        if self.gate is not None and len(self.calls) == 2:
            await asyncio.to_thread(self.gate.wait, 10)
        return self.answer(messages), "test/committee", {"cost_usd": COST, "prompt_tokens": 1, "completion_tokens": 1}


class CommitteeCase(tv.Case):
    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, llm, "TRANSPORT", None)
        studio._spend_unrecorded = False
        from strategy_platform.validation import jobs as vjobs
        self.vid = vjobs.ValidationJobs(data_factory=tv.loader).submit_validation(self.sid, None, None)
        self.wait()

    def fake(self, **kw):
        f = Fake(**kw)
        llm.TRANSPORT = f
        return f

    def finish(self, rid, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            r = committee.get_run(rid)
            if r["run"]["status"] != "running":
                return r
            time.sleep(0.05)
        self.fail("committee did not finish")


class CommitteeTest(CommitteeCase):
    def test_roles_memo_check_no_holdout(self):
        f = self.fake()
        status, out = call("committee_create", {"strategy_id": self.sid, "model": "standard"})
        self.assertEqual(status, 202, out)
        r = self.finish(out["run"]["id"])
        self.assertEqual(r["run"]["status"], "done")
        self.assertEqual([m["role"] for m in r["messages"]], list(committee.ROLES))
        self.assertEqual([m["seq"] for m in r["messages"]], [1, 2, 3, 4, 5])
        self.assertEqual(r["run"]["validation_run_id"], self.vid)
        self.assertAlmostEqual(r["run"]["cost_usd"], 5 * COST)
        self.assertTrue(all(m["provenance"]["ai_generated"] and m["provenance"]["role"] == m["role"]
                            for m in r["messages"]))
        self.assertTrue(r["messages"][0]["check"]["ok"], r["messages"][0]["check"])
        self.assertFalse(r["memo_check"]["ok"])
        self.assertEqual([(u["text"], u["section"]) for u in r["memo_check"]["unverified"]],
                         [("7.77 %", "summary"), ("10 %", "sizing.rules.0"), ("-15 %", "stop_rules.0")])
        self.assertEqual(r["memo"]["sizing"]["percent_of_capital"], 2.0)
        d = data_of(f.calls[4])
        self.assertEqual([p["role"] for p in d["previous_roles"]], list(committee.ROLES[:4]))
        self.assertNotIn("holdout", d)
        self.assertFalse(r["run"]["holdout_included"])
        self.assertFalse([x for x in d["facts"] if x["id"].startswith(("gate.H.", "holdout."))])
        self.assertTrue([x for x in d["facts"] if x["id"].startswith("gate.G")])
        self.assertEqual(db.strategy_get(self.sid)["state"], db.strategy_get(self.sid)["state"])   # advisory only
        self.assertEqual(call("committee_list", query={"strategy_id": self.sid})[1]["runs"][0]["status"], "done")

    def test_holdout_included_only_for_this_version(self):
        real = runcard.latest_done

        def fake_latest(strategy_id, version, kind=None):
            if kind == "holdout":
                c = dict(runcard.get(self.vid), kind="holdout", id="h-run")
                return c
            return real(strategy_id, version, kind=kind)
        runcard.latest_done = fake_latest
        self.addCleanup(setattr, runcard, "latest_done", real)
        f = self.fake()
        rid = committee.start(self.sid, None, None, "standard")["id"]
        r = self.finish(rid)
        self.assertTrue(r["run"]["holdout_included"])
        d = data_of(f.calls[0])
        self.assertIn("exact version", d["holdout"]["label"])
        self.assertTrue(all(x["id"].startswith(("holdout.", "gate.H.")) for x in d["holdout"]["facts"]))

    def test_cancel_between_roles(self):
        gate = threading.Event()
        f = self.fake(gate=gate)
        rid = committee.start(self.sid, None, None, "standard")["id"]
        time.sleep(0.3)
        status, out = call("committee_cancel", match={"id": rid})
        self.assertEqual(status, 200, out)
        gate.set()
        r = self.finish(rid)
        self.assertEqual(r["run"]["status"], "cancelled")
        self.assertEqual(len(r["messages"]), 2)
        self.assertEqual(len(f.calls), 2)
        self.assertIn("charged so far", r["run"]["note"])
        self.assertEqual(call("committee_cancel", match={"id": rid})[1]["code"], "not_running")

    def test_budget_blocks_and_estimate(self):
        f = self.fake()
        call("ideation_settings_put", {"monthly_budget_usd": 0, "daily_cap_usd": 2})
        status, out = call("committee_create", {"strategy_id": self.sid, "model": "premium"})
        self.assertEqual((status, out["code"], out["error"]), (402, "budget", studio.OFF_MESSAGE))
        self.assertEqual(db.s5b_select("committee_runs"), [])
        status, est = call("committee_estimate", query={"strategy_id": self.sid, "model": "standard"})
        self.assertEqual((status, est["roles"]), (200, 5))
        self.assertGreater(est["estimate_usd"], 0)
        self.assertEqual(f.calls, [])

    def test_auto_trigger(self):
        f = self.fake()
        self.assertIsNone(committee.on_validation_finished(self.vid))            # default off
        status, s = call("committee_settings_put", {"auto_after_validation": True, "model": "standard"})
        self.assertEqual((status, s), (200, {"auto_after_validation": True, "model": "standard"}))
        rid = committee.on_validation_finished(self.vid)
        r = self.finish(rid)
        self.assertEqual((r["run"]["trigger"], r["run"]["status"]), ("auto", "done"))
        call("ideation_settings_put", {"monthly_budget_usd": 10, "daily_cap_usd": 0})
        rid = committee.on_validation_finished(self.vid)
        r = committee.get_run(rid)["run"]
        self.assertEqual(r["status"], "skipped")
        self.assertTrue(r["note"].startswith("committee skipped: "))
        self.assertIsNone(committee.on_validation_finished("nope"))
        self.assertEqual(len(f.calls), 5)
        self.assertEqual(db.list_audit(action="committee_settings_changed")[0]["detail"]["new"]["auto_after_validation"],
                         True)


class CommitteeGuardTest(CommitteeCase):
    def test_off_topic_role_replaced_run_continues_and_errors(self):
        def guard(messages):
            texts = json.loads(messages[1]["content"].partition("\n")[2])["texts"]
            off = any(t.startswith(("quant", "Memo")) for t in texts)
            return json.dumps({"on_topic": not off, "reason": "x"})
        f = self.fake(guard=guard)
        r = self.finish(committee.start(self.sid, None, None, "standard")["id"])
        self.assertEqual(r["run"]["status"], "done")
        q = r["messages"][1]
        self.assertEqual((q["role"], q["discarded"], q["discarded_reason"], q["check"]),
                         ("quant", True, "topic_check_failed", None))
        self.assertEqual(q["output"], {"summary": committee.DISCARDED_ROLE_TEXT, "points": []})
        self.assertIsNone(r["memo"])                                          # chair discarded too
        self.assertFalse(r["messages"][0]["discarded"])
        self.assertEqual(len(f.guard_calls), 5)
        prev = data_of(f.calls[2])["previous_roles"]
        self.assertEqual(prev[1]["output"]["summary"], committee.DISCARDED_ROLE_TEXT)

        def failing(messages):                                                # review M2: role + charge in the note
            if data_of(messages)["your_role"] == "risk_manager":
                raise ConnectionResetError("reset")
            return role_answer(messages)
        self.fake(answer=failing)
        r = self.finish(committee.start(self.sid, None, None, "standard")["id"])
        self.assertEqual(r["run"]["status"], "failed")
        self.assertIn("risk_manager", r["run"]["note"])
        self.assertIn("charged for this role", r["run"]["note"])

    def test_auto_start_error_becomes_skipped(self):
        from strategy_platform.backtest import explain
        call("committee_settings_put", {"auto_after_validation": True, "model": "premium"})
        real = explain._model_id

        def no_model(tier):
            raise explain.ExplainError(503, "no_model", "No premium model is configured.")
        explain._model_id = no_model
        self.addCleanup(setattr, explain, "_model_id", real)
        rid = committee.on_validation_finished(self.vid)
        r = committee.get_run(rid)["run"]
        self.assertEqual((r["status"], r["note"]), ("skipped", "committee skipped: No premium model is configured."))


class RoutesInsertOnlyTest(CommitteeCase):
    def test_routes_and_insert_only(self):
        for body in ({}, {"strategy_id": self.sid}, {"strategy_id": self.sid, "model": "x"},
                     {"strategy_id": "../x", "model": "standard"}, {"strategy_id": self.sid, "model": "standard", "v": 1},
                     {"strategy_id": self.sid, "model": "standard", "version": 0}):
            self.assertEqual(call("committee_create", body)[0], 400, body)
        self.assertEqual(call("committee_create", {"strategy_id": self.sid, "model": "standard",
                                                   "validation_run_id": "nope"})[0], 404)
        other = __import__("strategy_platform.lifecycle.registry", fromlist=["x"]).create_from_text(
            json.dumps(tv.strategy_spec("no validation")), "user")["id"]
        status, out = call("committee_create", {"strategy_id": other, "model": "standard"})
        self.assertEqual((status, out["code"]), (409, "no_validation"))
        for body in ({}, {"auto_after_validation": "yes", "model": "standard"},
                     {"auto_after_validation": True, "model": "x"}, {"auto_after_validation": True, "model": "standard", "x": 1}):
            self.assertEqual(call("committee_settings_put", body)[0], 400, body)
        self.assertEqual(call("committee_settings_get")[1], {"auto_after_validation": False, "model": "standard"})
        self.assertEqual(call("committee_get", match={"id": "cr-000000000000"})[0], 404)
        self.assertEqual(call("committee_estimate", query={"strategy_id": self.sid})[0], 400)
        self.fake()
        rid = committee.start(self.sid, None, None, "standard")["id"]
        self.finish(rid)
        for t in ("committee_runs", "committee_messages", "committee_events"):
            for sql in (f"UPDATE {t} SET created_at = 'x'", f"DELETE FROM {t}"):
                with self.assertRaises(db.StorageUnavailable, msg=sql):
                    with db._connect() as conn:
                        conn.execute(sql)


if __name__ == "__main__":
    unittest.main()
