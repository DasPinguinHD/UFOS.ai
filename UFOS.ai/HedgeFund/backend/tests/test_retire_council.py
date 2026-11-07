"""retire1 — retiring the Council of AIs and the old engine (SP §22.15, contract
UFOS.ai/docs/tasks/RETIRE_COUNCIL_TASK.md): the marker round trip (absent / retired / cleared / unreadable → fail
closed), POST /v2/council/retire (audit first, working Council orders cancelled, the task stopped, idempotent, refused
in sandbox / before the switch-over) and /v2/council/unretire (task started, Council orders stay OFF), the start-up
plan (marker / unreadable marker + critical alert), every Council write route 410 ``retired`` and every read route
200 with ``retired: true``, the AI budget entry hidden + PUT 409, the OMS / service gates, the kill switch while
retired, the readers (portfolio summary, Overview activity) and NO balance change. Temp databases, fakes — no network.
The /fund/* side (410 ``retired``, /fund/kill + /fund/resume still forwarding) is in test_s10_council_switchover."""
import asyncio
import json
import unittest
from datetime import timedelta
from unittest import mock

try:
    from test_s10_council_service import CouncilCase
except ImportError:  # pragma: no cover
    from tests.test_s10_council_service import CouncilCase

import ai_budgets
from strategy_platform.api import overview_routes as ov
from strategy_platform.api.routes import NO_BODY, V2Api
from strategy_platform.council import retirement
from strategy_platform.council import service as svc_mod
from strategy_platform.council import store as cs
from strategy_platform.execution import accounts, control, oms
from strategy_platform.execution.errors import TradingError
from strategy_platform.portfolio import summary
from strategy_platform.storage import db


def call(name, query=None, body=NO_BODY, match=None):
    return asyncio.run(getattr(V2Api(), name)(query or {}, body, match or {}))


def activity(tmp):
    """GET /v2/overview/activity with the options archive pointed at the temp dir (never the real data folder)."""
    api = V2Api()
    api.data_dir = tmp
    return asyncio.run(api.overview_activity({}, NO_BODY, {}))


def audits(action):
    with db._connect() as conn:
        return [json.loads(r["detail_json"] or "{}") for r in
                conn.execute("SELECT detail_json FROM audit WHERE action = ? ORDER BY id", (action,))]


def working_council_order(cid="cl-work00000001"):
    ts = "2026-10-02T14:59:00+00:00"
    with db._connect() as conn:
        oms._insert(conn, {"client_order_id": cid, "account_id": accounts.COUNCIL, "created_at": ts, "updated_at": ts,
                           "symbol": "BTC-USD", "side": "buy", "qty": 0.01, "origin": "council", "tif": "gtc",
                           "state": "new", "filled_qty": 0.0, "fees": 0.0, "attempts": 0, "allow_reduce": 0,
                           "allocations_json": "[]", "checks_json": "[]"})
    return cid


def money_snapshot():
    """Every ledger row + each account's cash + positions (must be identical before / after retire / un-retire)."""
    with db._connect() as conn:
        rows = [tuple(r) for r in conn.execute("SELECT * FROM sl_entries ORDER BY id")]
    accs = {}
    for a in accounts.all_accounts():
        accs[a.id] = (a.store.cash_balance(), sorted((p["symbol"], p["qty"]) for p in a.store.get_positions()))
    return rows, accs


class RetireCase(CouncilCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(retirement, "_sandbox", return_value=False)
        p.start()
        self.addCleanup(p.stop)
        svc_mod.set_default(self.svc)
        self.addCleanup(svc_mod.set_default, None)

    def retire(self, reason="AI spend for nothing"):
        return call("council_retire", body={"reason": reason})


class MarkerTest(RetireCase):
    def test_round_trip_and_unreadable(self):
        self.assertEqual(retirement.state()["retired"], False)            # absent → not retired
        self.assertEqual(retirement.read_fields(), {"retired": False})
        self.assertIsNone(retirement.write_refusal())
        retirement._write(True, "user", "why", 7)
        st = retirement.state()
        self.assertEqual((st["retired"], st["actor"], st["reason"], st["audit_id"]), (True, "user", "why", 7))
        self.assertTrue(retirement.blocked())
        f = retirement.read_fields()
        self.assertEqual((f["retired"], f["reason"], f["retired_at"]), (True, "why", st["at"]))
        status, body = retirement.write_refusal()
        self.assertEqual((status, body["code"]), (410, "retired"))
        self.assertEqual(body["error"], f"Retired by the user on {st['at'][:10]} — un-retire first")
        retirement._write(False, "user", "back", 8)
        self.assertFalse(retirement.is_retired())
        for bad in ("{not json", json.dumps({"retired": "yes"}), json.dumps([1])):
            db.meta_set(retirement.META_RETIRED, bad)
            with self.assertRaises(retirement.RetirementUnreadable):
                retirement.state()
            self.assertTrue(retirement.blocked())                         # fail closed
            self.assertEqual(retirement.status()[0], "unreadable")
            self.assertEqual(retirement.write_refusal()[0], 503)
            self.assertIsNone(retirement.read_fields()["retired"])
        self.assertEqual(cs.switchover(), "done")                         # never the retirement marker

    def test_startup_plan_and_check(self):
        plan = retirement.startup_plan
        self.assertEqual(plan("done", "active"), {"old_engine": False, "council_task": True})
        self.assertEqual(plan("done", "retired"), {"old_engine": False, "council_task": False})
        self.assertEqual(plan("done", "unreadable"), {"old_engine": False, "council_task": False})
        self.assertEqual(plan("none", "active"), {"old_engine": True, "council_task": False})
        self.assertEqual(plan("reverted", "unreadable"), {"old_engine": False, "council_task": False})
        self.assertEqual(plan("pending", "active"), {"old_engine": False, "council_task": False})
        self.assertEqual(retirement.startup_check(), "active")
        retirement._write(True, "user", "r", 1)
        self.assertEqual(retirement.startup_check(), "retired")
        db.meta_set(retirement.META_RETIRED, "{broken")
        with self.assertLogs(retirement.logger, "CRITICAL"):
            self.assertEqual(retirement.startup_check(), "unreadable")
        with db._connect() as conn:
            kinds = [r["kind"] for r in conn.execute("SELECT kind, severity FROM mon_alerts")]
            sev = conn.execute("SELECT severity FROM mon_alerts WHERE kind = ?",
                               (retirement.ALERT_KIND,)).fetchone()["severity"]
        self.assertIn(retirement.ALERT_KIND, kinds)
        self.assertEqual(sev, "critical")


class RetireFlowTest(RetireCase):
    def test_retire_and_unretire(self):
        self.run_cycle()                                                  # positions + ledger rows exist
        before = money_snapshot()
        cid = working_council_order()

        async def scenario():
            self.svc.start()
            await asyncio.sleep(0)
            running = self.svc.task_running()
            st, body = await V2Api().council_retire({}, {"reason": "AI spend for nothing"}, {})
            return running, st, body, self.svc.task_running()
        running, st, body, after_running = asyncio.run(scenario())
        self.assertTrue(running)
        self.assertEqual(st, 200, body)
        self.assertEqual((body["retired"], body["already"], body["orders_enabled"], body["cancelled_orders"],
                          body["task_stopped"]), (True, False, False, 1, True))
        self.assertFalse(after_running)
        self.assertEqual(oms.get(cid)["state"], "cancelled")
        self.assertFalse(cs.orders_enabled())
        self.assertTrue(retirement.is_retired())
        self.assertEqual(retirement.state()["audit_id"], body["audit_id"])
        self.assertEqual(audits("council_retire")[-1]["reason"], "AI spend for nothing")
        self.assertEqual(cs.switchover(), "done")
        # idempotent: no second audit row
        st, again = self.retire()
        self.assertEqual((st, again["already"]), (200, True))
        self.assertEqual(len(audits("council_retire")), 1)
        # un-retire: the task runs again, Council orders stay OFF
        async def back():
            st, body = await V2Api().council_unretire({}, {"reason": "try again"}, {})
            running = self.svc.task_running()
            await self.svc.stop()
            return st, body, running
        st, body, running = asyncio.run(back())
        self.assertEqual(st, 200, body)
        self.assertEqual((body["retired"], body["task_started"], body["orders_enabled"]), (False, True, False))
        self.assertTrue(running)
        self.assertFalse(retirement.is_retired())
        self.assertFalse(cs.orders_enabled())
        self.assertEqual(len(audits("council_unretire")), 1)
        st, again = call("council_unretire")
        self.assertEqual((st, again["already"]), (200, True))
        self.assertEqual(cs.switchover(), "done")
        # NO balance change: every ledger row, cash and positions identical (the cancelled order never filled)
        self.assertEqual(money_snapshot(), before)

    def test_cycle_in_flight_recorded_as_cancelled_by_retire(self):
        """review retire1b #3: a cycle cancelled by retire() is recorded "cancelled (Council retired)"."""
        started = []

        async def slow_cycle(run_id, now, params, acc, calls):
            started.append(run_id)
            await asyncio.sleep(30)
        self.svc._cycle = slow_cycle

        async def go():
            self.svc._task = asyncio.get_running_loop().create_task(self.svc.run("scheduled"))
            while not started:
                await asyncio.sleep(0.01)
            st, body = await V2Api().council_retire({}, {"reason": "stop it"}, {})
            return st, body
        st, body = asyncio.run(go())
        self.assertEqual(st, 200, body)
        d = cs.run_detail(started[0])
        self.assertEqual(d["status"], "error")
        self.assertEqual(d["summary"]["error"], "cancelled (Council retired)")
        self.assertFalse(self.svc.cycle_in_progress)
        # a plain stop (backend shutdown) keeps the old text
        started.clear()

        async def shutdown():
            self.svc._task = asyncio.get_running_loop().create_task(self.svc.run("scheduled"))
            while not started:
                await asyncio.sleep(0.01)
            await self.svc.stop()
        asyncio.run(shutdown())
        self.assertEqual(cs.run_detail(started[0])["summary"]["error"], "cancelled (backend shutting down)")

    def test_replay_returns_existing_order_while_retired(self):
        cid = working_council_order("cl-replay0000001")
        retirement._write(True, "user", "r", None)
        o, created = oms.place(accounts.get(accounts.COUNCIL), {"symbol": "BTC-USD", "side": "buy", "qty": 0.01,
                                                                "origin": "council", "client_order_id": cid,
                                                                "source_id": accounts.COUNCIL_SOURCE})
        self.assertEqual((o["client_order_id"], created), (cid, False))

    def test_refusals(self):
        with mock.patch.object(retirement, "_sandbox", return_value=True):
            st, body = self.retire()
            self.assertEqual((st, body["code"]), (409, "sandbox"))
            self.assertEqual(call("council_unretire")[0], 409)
        self.assertEqual(call("council_retire", body={"reason": 5})[0], 400)
        self.assertEqual(call("council_retire", body={"x": 1})[0], 400)
        db.meta_set(cs.META_SWITCHOVER, json.dumps({"state": "reverted"}))
        st, body = self.retire()
        self.assertEqual((st, body["code"]), (409, "not_switched_over"))
        db.meta_set(cs.META_SWITCHOVER, json.dumps({"state": "done"}))
        with mock.patch.object(db, "add_audit", side_effect=db.StorageUnavailable("down")):
            st, body = self.retire()
        self.assertEqual((st, body["code"]), (503, "audit_unavailable"))
        self.assertFalse(retirement.is_retired())                         # audit first: nothing changed
        self.assertTrue(cs.orders_enabled())
        db.meta_set(retirement.META_RETIRED, "{broken")
        st, body = call("council_unretire")
        self.assertEqual((st, body["code"]), (503, "retirement_unreadable"))
        st, body = self.retire()                                          # retiring repairs an unreadable marker
        self.assertEqual((st, body["retired"]), (200, True))


class RetiredRoutesTest(RetireCase):
    def setUp(self):
        super().setUp()
        self.run_id = self.run_cycle()["run_id"]
        st, body = self.retire()
        self.assertEqual(st, 200, body)

    def test_write_routes_410(self):
        writes = [("council_run", NO_BODY), ("council_params_put", {"interval_minutes": 20}),
                  ("council_broker_params_put", {"fees_bps": 6.0}), ("council_orders", {"enabled": True}),
                  ("council_switchover_post", NO_BODY), ("council_switchover_retry", NO_BODY),
                  ("council_switchover_abort", NO_BODY), ("council_switchover_revert", NO_BODY)]
        for name, body in writes:
            st, payload = call(name, body=body)
            self.assertEqual((st, payload["code"]), (410, "retired"), name)
            self.assertIn("un-retire first", payload["error"])
        st, payload = call("council_orders", body={"enabled": False})    # OFF stays possible
        self.assertEqual((st, payload["orders_enabled"]), (200, False))
        with self.assertRaises(svc_mod.CouncilError) as cm:              # second net in the service
            self.svc.request_run()
        self.assertEqual(cm.exception.status, 410)

    def test_read_routes_200_retired(self):
        reads = [("council_state", {}), ("council_runs", {}), ("council_run_detail", {"id": str(self.run_id)}),
                 ("council_params_get", {}), ("council_broker_params_get", {})]
        for name, match in reads:
            st, payload = call(name, match=match)
            self.assertEqual(st, 200, (name, payload))
            self.assertEqual(payload["retired"], True, name)
            self.assertEqual(payload["reason"], "AI spend for nothing", name)
            self.assertTrue(payload["retired_at"], name)
        st, payload = call("council_state")
        self.assertEqual((payload["task"], payload["active"], payload["switchover"]), ("retired", True, "done"))
        self.assertEqual(payload["last_run"]["id"], self.run_id)                 # history kept
        self.assertEqual(call("council_runs")[1]["runs"][0]["id"], self.run_id)

    def test_switchover_get_offers_nothing(self):
        st, payload = call("council_switchover_get")
        if st != 200:                                                    # the old store may be absent in this case
            self.skipTest(f"dry run not available here: {payload}")
        self.assertTrue(payload["retired"])
        self.assertFalse(any(payload["actions"].values()))

    def test_ai_budget_hidden_and_put_409(self):
        b = ai_budgets.HedgeFundBudgets(None, provider_modules=())
        st, payload = b.handle_get()
        self.assertEqual(st, 200)
        self.assertNotIn("fund", [e["key"] for e in payload["budgets"]])
        st, payload = b.handle_put({"key": "fund", "limit_usd": 1.0})
        self.assertEqual((st, payload["kind"]), (409, "Retired"))
        self.assertEqual(audits("council_params"), [])                    # nothing written
        self.assertEqual(call("council_unretire")[0], 200)
        st, payload = b.handle_get()
        self.assertIn("fund", [e["key"] for e in payload["budgets"]])

    def test_gates_kill_and_readers(self):
        self.assertEqual(oms._council_blocker(), "the Council is retired")
        self.assertEqual(self.svc.order_gate(accounts.get(accounts.COUNCIL)), "the Council is retired")
        with self.assertRaises(TradingError) as cm:
            oms.place(accounts.get(accounts.COUNCIL), {"symbol": "BTC-USD", "side": "buy", "qty": 0.01,
                                                       "origin": "council", "source_id": accounts.COUNCIL_SOURCE})
        self.assertEqual(cm.exception.code, "retired")
        # the kill switch still works (platform + paper-council); the halves say "retired"
        res = control.kill_all("test kill")
        self.assertTrue(res["platform"]["ok"])
        self.assertTrue(accounts.get(accounts.COUNCIL).halt()["active"])
        self.assertEqual((res["legacy"]["applicable"], res["legacy"]["retired"]), (False, True))
        self.assertIn("retired", res["legacy"]["note"])
        self.assertEqual((res["council"]["ok"], res["council"]["retired"]), (True, True))
        # portfolio summary
        p = summary.summary()
        self.assertEqual((p["council"]["retired"], p["council"]["switchover"]), (True, "done"))
        self.assertEqual((p["old_engine"]["reason"], p["old_engine"]["applicable"]), ("retired", False))
        # Overview activity: old engine + Council read "retired", the Council's past runs stay listed
        st, out = activity(self.tmp)
        self.assertEqual(st, 200)
        self.assertEqual((out["sources"]["old_engine"], out["sources"]["council"]), ("retired", "retired"))
        self.assertTrue(any(i.get("source") == "council" for i in out["items"]) or
                        any("Council run" in (i.get("title") or "") for i in out["items"]))

    def test_no_scheduled_cycle_while_retired(self):
        calls = []

        async def fake_run(trigger="manual", run_id=None):
            calls.append(trigger)
            return {}
        self.svc.run = fake_run
        self.svc.last_start = None

        async def go():
            with mock.patch.object(svc_mod, "STARTUP_DELAY_SECONDS", 0):
                t = asyncio.get_running_loop().create_task(self.svc.run_forever())
                await asyncio.sleep(0.2)
                self.svc._stopping = True
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        asyncio.run(go())
        self.assertEqual(calls, [])
        self.assertIsNone(self.svc.next_due_at)


class NotRetiredUnchangedTest(RetireCase):
    def test_without_marker_behaviour_unchanged(self):
        st, payload = call("council_state")
        self.assertEqual((st, payload["retired"], payload["task"]), (200, False, "idle"))
        self.assertIsNone(oms._council_blocker())
        res = control.kill_all("k")
        self.assertNotIn("retired", res["legacy"])
        self.assertNotIn("retired", res["council"])
        st, out = activity(self.tmp)
        self.assertEqual(out["sources"]["council"], "ok")
        self.assertNotEqual(out["sources"]["old_engine"], "retired")


if __name__ == "__main__":
    unittest.main()
