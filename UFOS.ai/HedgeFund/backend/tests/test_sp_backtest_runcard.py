"""Stage 3d: backtest run cards (storage v5) and the lifecycle rule
draft -> backtested (SPEC §8.4 / §8.10). Temp database, fake data, offline.
Part of UFOS.ai (proof of concept)."""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strategy_platform.backtest import engine, metrics, runcard, stress  # noqa: E402
from strategy_platform.lifecycle import registry as lc  # noqa: E402
from strategy_platform.storage import db  # noqa: E402
import test_sp_backtest_support as sup  # noqa: E402

SYMS = ["AAA", "BBB"]


def spec_text(name="bt card"):
    s = sup.spec(SYMS)
    s["name"] = name
    return json.dumps(s)


def engine_result():
    prices = {"AAA": sup.series(50), "BBB": sup.series(80, drift=-0.0005), "SPY": sup.series(400)}
    return engine.run(sup.spec(SYMS), data=sup.FakeLoader(prices))


class Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = engine_result()
        cls.metrics = metrics.compute(cls.result)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sp3d-")
        self.path = os.path.join(self.tmp, "hedgefund.db")
        db.init(self.path)
        self.sid = lc.create_from_text(spec_text(), "user")["id"]

    def tearDown(self):
        db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def raw(self, sql, args=()):
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(sql, args)
            conn.commit()
        finally:
            conn.close()

    def done_card(self, sid=None):
        card = runcard.create(sid or self.sid, request={"capital": 100000})
        runcard.mark_running(card["id"])
        return runcard.finish(card["id"], self.result, self.metrics)


class RunCardTest(Case):
    def test_schema_v5(self):
        self.assertGreaterEqual(db.SCHEMA_VERSION, 5)
        self.assertIn("backtest_runs", db.storage_info()["rows"])

    def test_life_cycle_and_payload(self):
        card = runcard.create(self.sid, request={"start": "2024-01-02", "stress": True})
        self.assertEqual(card["status"], "queued")
        self.assertTrue(card["id"].startswith("bt-"))
        self.assertEqual(card["version"], 1)
        self.assertEqual(card["request"], {"start": "2024-01-02", "end": None, "benchmark": None,
                                           "capital": None, "stress": True,
                                           "history_from": None})
        self.assertEqual(card["seed"], stress.MC_SEED)
        self.assertIsNone(card["trial_number"])
        self.assertEqual(card["spec"]["name"], "bt card")
        self.assertEqual(card["content_hash"], db.strategy_version_get(self.sid, 1)["content_hash"])
        self.assertEqual(runcard.mark_running(card["id"])["status"], "running")
        done = runcard.finish(card["id"], self.result, self.metrics, stress={"seed": 1, "scenarios": []})
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["engine_version"], engine.ENGINE_VERSION)
        self.assertEqual(len(done["curve"]["dates"]), len(self.result.dates))
        self.assertEqual(set(done["curve"]), {"dates", "equity", "benchmark", "gross_exposure", "net_exposure",
                                              "drawdown_pct"})
        self.assertEqual(done["warnings"][0]["code"], "survivorship")
        self.assertEqual(done["licences"]["alpaca"]["licence"], "personal_use")
        self.assertIn("tiingo", done["licences"])
        self.assertEqual(done["trades"]["fills"], len(self.result.fills))
        self.assertEqual(done["metrics"]["performance"]["cagr"], self.metrics["performance"]["cagr"])
        self.assertEqual(done["cost_model"]["benchmark"], "SPY")
        self.assertEqual(done["stress"], {"seed": 1, "scenarios": []})
        self.assertIsNotNone(done["finished_at"])
        self.assertEqual(set(done["data"]), {"AAA", "BBB", "SPY"})
        actions = [a["action"] for a in db.list_audit(limit=10)]
        self.assertIn("backtest_finished", actions)
        self.assertIn("backtest_queued", actions)

    def test_finished_card_is_immutable(self):
        done = self.done_card()
        with self.assertRaises(runcard.StatusConflict):
            runcard.fail(done["id"], "late")
        with self.assertRaises(runcard.StatusConflict):
            runcard.finish(done["id"], self.result, self.metrics)
        for sql in ("UPDATE backtest_runs SET metrics_json = '{}' WHERE id = ?",
                    "UPDATE backtest_runs SET status = 'failed' WHERE id = ?",
                    "DELETE FROM backtest_runs WHERE id = ?"):
            with self.assertRaises(sqlite3.DatabaseError) as cm:
                self.raw(sql, (done["id"],))
            self.assertRegex(str(cm.exception), "immutable|insert-only")
        self.assertEqual(runcard.get(done["id"])["metrics"], done["metrics"])

    def test_only_status_transitions_while_open(self):
        card = runcard.create(self.sid)
        rid = card["id"]
        with self.assertRaises(sqlite3.DatabaseError):                     # same status: not a transition
            self.raw("UPDATE backtest_runs SET metrics_json = '{}' WHERE id = ?", (rid,))
        with self.assertRaises(sqlite3.DatabaseError):                     # queued -> done is not allowed
            self.raw("UPDATE backtest_runs SET status = 'done' WHERE id = ?", (rid,))
        with self.assertRaises(sqlite3.DatabaseError):                     # identity columns fixed
            self.raw("UPDATE backtest_runs SET status = 'running', version = 9 WHERE id = ?", (rid,))
        with self.assertRaises(sqlite3.DatabaseError):
            self.raw("DELETE FROM backtest_runs WHERE id = ?", (rid,))
        with self.assertRaises(ValueError):
            db.backtest_run_transition(rid, "queued", "done")
        with self.assertRaises(ValueError):
            db.backtest_run_transition(rid, "queued", "running", {"spec_json": "{}"})
        with self.assertRaises(runcard.StatusConflict):
            runcard.finish(rid, self.result, self.metrics)                  # not running yet
        self.assertEqual(runcard.cancel(rid)["status"], "cancelled")
        self.assertEqual(runcard.get(rid)["error"], "The backtest was cancelled.")
        with self.assertRaises(runcard.StatusConflict):
            runcard.mark_running(rid)
        other = runcard.create(self.sid)
        runcard.mark_running(other["id"])
        failed = runcard.fail(other["id"], "Tiingo API key is not set.")
        self.assertEqual((failed["status"], failed["error"]), ("failed", "Tiingo API key is not set."))

    def test_reads_and_errors(self):
        self.assertIsNone(runcard.get("bt-000000000000"))
        self.assertEqual(runcard.list_runs(self.sid), [])
        self.assertIsNone(runcard.latest_done(self.sid, 1))
        with self.assertRaises(runcard.NotFound):
            runcard.create("s-000000000000")
        with self.assertRaises(runcard.NotFound):
            runcard.create(self.sid, 7)
        with self.assertRaises(ValueError):
            runcard.create(self.sid, request={"bogus": 1})
        with self.assertRaises(ValueError):
            db.backtest_runs_list(limit=0)
        with self.assertRaises(ValueError):
            db.backtest_runs_list(status="weird")
        with self.assertRaises(runcard.StatusConflict):
            runcard.mark_running("bt-000000000000")
        a = runcard.create(self.sid)
        done = self.done_card()
        rows = runcard.list_runs(self.sid)
        self.assertEqual({r["id"] for r in rows}, {a["id"], done["id"]})
        row = next(r for r in rows if r["id"] == done["id"])
        self.assertEqual(set(row["key_metrics"]), set(metrics.SUMMARY_KEYS))
        self.assertNotIn("curve", row)
        self.assertIsNone(next(r for r in rows if r["id"] == a["id"])["key_metrics"])
        self.assertEqual(runcard.latest_done(self.sid, 1)["id"], done["id"])
        self.assertEqual(runcard.list_runs(self.sid, status="queued")[0]["id"], a["id"])

    def test_storage_unavailable(self):
        db.close()
        with self.assertRaises(db.StorageUnavailable):
            runcard.get("bt-000000000000")
        db.init(self.path)


class LifecycleRuleTest(Case):
    def allowed(self):
        return {t["to"]: t for t in lc.allowed_transitions(lc.get(self.sid))}

    def test_draft_to_backtested_needs_a_done_run_of_the_current_version(self):
        a = self.allowed()["backtested"]
        self.assertFalse(a["allowed"])
        self.assertEqual(a["reason_if_not"], "needs a finished backtest of version 1")
        with self.assertRaises(lc.Conflict) as cm:
            lc.transition(self.sid, "backtested", "looks good")
        self.assertIn("needs a finished backtest of version 1", cm.exception.message)
        failed = runcard.create(self.sid)
        runcard.fail(failed["id"], "boom")
        self.assertFalse(self.allowed()["backtested"]["allowed"])
        done = self.done_card()
        self.assertTrue(self.allowed()["backtested"]["allowed"])
        self.assertFalse(self.allowed()["candidate"]["allowed"])
        row = lc.transition(self.sid, "backtested", "backtest finished")
        self.assertEqual(row["state"], "backtested")
        t = db.strategy_transitions_list(self.sid)[-1]
        self.assertEqual(json.loads(t["evidence_json"]), {"run_id": done["id"], "version": 1})

    def test_new_version_needs_a_new_run(self):
        self.done_card()
        lc.transition(self.sid, "backtested", "ok")
        lc.new_version(self.sid, spec_text("bt card v2"), 1)
        row = lc.get(self.sid)
        self.assertEqual((row["state"], row["current_version"]), ("draft", 2))
        self.assertEqual(self.allowed()["backtested"]["reason_if_not"], "needs a finished backtest of version 2")

    def test_explicit_run_id_must_match(self):
        done = self.done_card()
        other = lc.create_from_text(spec_text("other"), "user")["id"]
        with self.assertRaises(lc.Conflict):
            lc.transition(other, "backtested", "borrowed evidence", run_id=done["id"])
        row = lc.transition(self.sid, "backtested", "explicit", run_id=done["id"])
        self.assertEqual(row["state"], "backtested")

    def test_only_from_draft(self):
        self.done_card()
        lc.transition(self.sid, "idea", "later")
        self.assertIn("only a draft", self.allowed()["backtested"]["reason_if_not"])


if __name__ == "__main__":
    unittest.main()
