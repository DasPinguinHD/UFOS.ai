"""fix1023 round 3 (sandbox click-test 2026-10-23): the incubation start answers within the allocation wait (the
allocation runs after the commit; a failure is an alert + audit row, never dropped), the double-start protection
stays, GET /v2/incubation?started_after= lets a client confirm a start after a timeout, a pending decision of an
aborted run is shown cancelled and is never executed, empty notes are left out of event data. Temp DBs, offline."""
import asyncio
import json
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

try:
    from sp_trading_testkit import EXPECTED, FRI, TradingCase, make_strategy
except ImportError:  # pragma: no cover
    from tests.sp_trading_testkit import EXPECTED, FRI, TradingCase, make_strategy

from strategy_platform.api import routes
from strategy_platform.api.trading_routes import decisions_of
from strategy_platform.execution.errors import TradingError
from strategy_platform.live import incubation
from strategy_platform.portfolio import allocation
from strategy_platform.storage import db


def call(name, body=None, match=None, query=None):
    status, payload = asyncio.run(getattr(routes.V2Api(), name)(query or {}, routes.NO_BODY if body is None else body,
                                                                 match or {}))
    routes.dumps(payload)
    return status, payload


def alerts_of(kind):
    with db._connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM mon_alerts WHERE kind = ? ORDER BY id", (kind,))]


class Round3Case(TradingCase):
    def setUp(self):
        super().setUp()
        real = incubation.expected_for
        incubation.expected_for = lambda sid, v: dict(EXPECTED)
        self.addCleanup(setattr, incubation, "expected_for", real)
        self.addCleanup(setattr, incubation, "ALLOCATION_WAIT_SECONDS", incubation.ALLOCATION_WAIT_SECONDS)
        self.addCleanup(setattr, allocation, "rebalance", allocation.rebalance)

    def slow_rebalance(self, seconds, fail=None):
        done = threading.Event()
        real = allocation.rebalance

        def fake(acc, reason, actor="engine", now=None):
            time.sleep(seconds)
            try:
                if fail:
                    raise TradingError(503, "valuation_paused", fail)
                return real(acc, reason, actor, now=now)
            finally:
                done.set()
        allocation.rebalance = fake
        return done


class StartLatencyTest(Round3Case):
    def test_slow_allocation_does_not_hold_the_answer(self):
        incubation.ALLOCATION_WAIT_SECONDS = 0.2
        done = self.slow_rebalance(1.0)
        sid = make_strategy()
        t = time.monotonic()
        st, p = call("incubation_create", {"strategy_id": sid, "length_sessions": 20})
        took = time.monotonic() - t
        self.assertEqual(st, 201, p)
        self.assertLess(took, 0.9)                                     # not the 1 s of the allocation
        self.assertEqual(p["allocation"]["status"], "pending")
        self.assertIn("background", p["allocation"]["note"])
        self.assertTrue(done.wait(5))
        time.sleep(0.1)
        self.assertEqual(alerts_of("allocation_failed"), [])           # it succeeded later: no alert
        st, p = call("incubation_create", {"strategy_id": sid, "length_sessions": 20})
        self.assertEqual((st, p["code"]), (409, "already_incubating"))  # the double-start protection stays

    def test_failed_allocation_is_an_alert_and_audit_row(self):
        self.slow_rebalance(0.0, fail="No current quote for BTC-USD — the allocation waits")
        sid = make_strategy()
        out = incubation.start(sid, 20, "", now=FRI)
        self.assertEqual(out["allocation"]["status"], "failed")
        self.assertIn("BTC-USD", out["allocation"]["error"])
        a = alerts_of("allocation_failed")
        self.assertEqual((len(a), a[0]["severity"], a[0]["ref"]), (1, "warning", out["run"]["id"]))
        self.assertEqual(len(db.list_audit(action="incubation_allocation_failed")), 1)

    def test_late_failure_after_the_answer_is_still_recorded(self):
        incubation.ALLOCATION_WAIT_SECONDS = 0.1
        done = self.slow_rebalance(0.5, fail="no quote")
        out = incubation.start(make_strategy(), 20, "", now=FRI)
        self.assertEqual(out["allocation"]["status"], "pending")
        self.assertTrue(done.wait(5))
        for _ in range(50):
            if alerts_of("allocation_failed"):
                break
            time.sleep(0.05)
        self.assertEqual(len(alerts_of("allocation_failed")), 1)

    def test_normal_allocation_done_within_the_wait(self):
        out = incubation.start(make_strategy(), 20, "", now=FRI)
        self.assertEqual(out["allocation"], {"status": "done"})


class StartedAfterTest(Round3Case):
    def test_client_can_confirm_its_start(self):
        sid = make_strategy()
        before = (datetime.now(timezone.utc) - timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rid = incubation.start(sid, 20, "", now=FRI)["run"]["id"]
        st, p = call("incubation_list", query={"strategy_id": sid, "started_after": before})
        self.assertEqual((st, [r["id"] for r in p["runs"]]), (200, [rid]))
        self.assertTrue(p["started_after"].endswith("+00:00"))
        later = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        self.assertEqual(call("incubation_list", query={"strategy_id": sid, "started_after": later})[1]["runs"], [])
        self.assertEqual(call("incubation_list", query={"started_after": "yesterday"})[0], 400)
        self.assertEqual(call("incubation_list", query={"started_after": "2026-10-23T21:00:00"})[0], 400)   # no offset
        self.assertNotIn("started_after", call("incubation_list")[1])


class AbortTest(Round3Case):
    def test_pending_decision_of_an_aborted_run_is_cancelled_and_never_executed(self):
        sid = make_strategy()
        run = incubation.start(sid, 20, "", now=FRI)["run"]
        r = incubation.run_row(run["id"])
        with db._connect() as conn:
            conn.execute("INSERT INTO rt_decisions (id, account_id, source_id, strategy_id, version, incubation_id,"
                         " bar_date, created_at, status, result_json) VALUES ('rd-test0000001', ?, ?, ?, ?, ?,"
                         " '2026-10-23', '2026-10-23T21:00:00+00:00', 'ok', ?)",
                         (r["account_id"], r["source_id"], sid, r["version"], r["id"],
                          json.dumps({"targets": {"SPY": 1.0}, "reason": "strategy rebalance", "force": []})))
        self.assertEqual(decisions_of(r["id"])[0]["status"], "pending")
        incubation.abort(r["id"], "", "user")
        d = decisions_of(r["id"])[0]
        self.assertEqual((d["status"], d["reason"]), ("cancelled", "strategy rebalance"))
        self.assertIsNotNone(d["cancelled_at"])
        self.assertIn("never executed", d["cancelled_note"])
        self.assertNotIn(r["id"], [x["id"] for x in incubation.active_runs()])
        self.rt.cycle(FRI + timedelta(days=3, hours=1))
        with db._connect() as conn:
            execs = [json.loads(e[0]) for e in conn.execute("SELECT decision_ids_json FROM rt_executions")]
        self.assertFalse(any("rd-test0000001" in ids for ids in execs))       # the runtime never acts on it
        ev = [e for e in incubation.run_events(r["id"]) if e["event"] == "aborted"][0]
        self.assertEqual(ev["data"], {"actor": "user"})                       # no empty "reason="

    def test_note_kept_when_given(self):
        rid = incubation.start(make_strategy(), 20, "", now=FRI)["run"]["id"]
        incubation.abort(rid, "wrong length", "user")
        ev = [e for e in incubation.run_events(rid) if e["event"] == "aborted"][0]
        self.assertEqual(ev["data"], {"reason": "wrong length", "actor": "user"})


if __name__ == "__main__":
    unittest.main()
