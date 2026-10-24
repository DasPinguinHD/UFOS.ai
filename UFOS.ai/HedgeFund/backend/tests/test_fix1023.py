"""Fix batch fix1023 (2026-10-23): confirmation style for professional users (no typed confirm words / checkboxes,
optional notes — the live guard and the evidence overrides keep their checks) and the sandbox click-test findings
(B1 double incubation start, B2 paused numbers + last check, B3 decay events before the first session, B5 an
alert for every pause, B6 the sandbox prices note). Temp DBs, fakes, offline."""
import asyncio
import json
import os
import shutil
import tempfile
import threading
import unittest
from datetime import timedelta

try:
    from sp_broker_testkit import BrokerCase
    from sp_trading_testkit import EXPECTED, FRI, TradingCase, make_strategy
except ImportError:  # pragma: no cover
    from tests.sp_broker_testkit import BrokerCase
    from tests.sp_trading_testkit import EXPECTED, FRI, TradingCase, make_strategy

import sandbox_profile as sp
from strategy_platform.api import routes
from strategy_platform.execution.errors import TradingError
from strategy_platform.live import incubation
from strategy_platform.monitoring import decay
from strategy_platform.storage import db


def call(name, body=None, match=None, query=None):
    status, payload = asyncio.run(getattr(routes.V2Api(), name)(query or {}, routes.NO_BODY if body is None else body,
                                                                 match or {}))
    routes.dumps(payload)
    return status, payload


def sandbox(case):
    root = tempfile.mkdtemp(prefix="fix1023-")
    case.addCleanup(shutil.rmtree, root, True)
    os.makedirs(os.path.join(root, "sandbox"))
    with open(os.path.join(root, sp.MARKER_NAME), "w", encoding="utf-8") as f:
        json.dump({"mode": "sandbox", "created_at": "2026-10-23T12:00:00+00:00", "files": {}}, f)
    sp.use_root(root)
    case.addCleanup(sp.use_root, None)


def decay_alerts(sid):
    with db._connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM mon_alerts WHERE kind = 'decay' AND strategy_id = ?"
                                              " ORDER BY id", (sid,)).fetchall()]


class IncubationCase(TradingCase):
    def setUp(self):
        super().setUp()
        real = incubation.expected_for
        incubation.expected_for = lambda sid, v: dict(EXPECTED)
        self.addCleanup(setattr, incubation, "expected_for", real)


class ConfirmStyleTest(IncubationCase):
    def test_incubation_start_abort_decision_transition_without_notes(self):
        sid = make_strategy()
        st, p = call("incubation_create", {"strategy_id": sid, "length_sessions": 20})
        self.assertEqual(st, 201, p)
        rid = p["run"]["id"]
        self.assertEqual(p["run"]["reason"], "")
        st, p = call("incubation_abort", {}, {"id": rid})
        self.assertEqual(st, 200, p)
        self.assertEqual(db.strategy_get(sid)["state"], "validated")
        st, p = call("strategy_transition", {"to": "retired"}, {"id": sid})
        self.assertEqual((st, p["strategy"]["state"]), (200, "retired"))
        st, p = call("strategy_transition", {"to": "draft", "reason": "back to work"}, {"id": sid})
        self.assertEqual(st, 200)
        self.assertEqual(db.strategy_transitions_list(sid)[-1]["reason"], "back to work")   # a note given is kept

    def test_demote_and_keep_running_without_confirm_word_or_note(self):
        sandbox(self)
        import sandbox_fixtures
        sid = make_strategy()
        incubation.start(sid, 20, "", now=FRI)
        sandbox_fixtures.force_decay(sid, now=FRI)
        st, p = call("mon_decision", {"action": "keep_running"}, {"strategy_id": sid})
        self.assertEqual((st, p["event"]["action"]), (200, "keep_running"), p)
        sandbox_fixtures.force_decay(sid, now=FRI)
        st, p = call("mon_decision", {"action": "demote"}, {"strategy_id": sid})
        self.assertEqual((st, p["event"]["action"]), (200, "demoted"), p)
        self.assertEqual(db.strategy_get(sid)["state"], "validated")

    def test_portfolio_resume_rebalance_without_notes(self):
        from strategy_platform.execution import control
        control.kill_platform("test", "user")
        st, p = call("oms_resume", {"account_id": "paper-main"})
        self.assertEqual(st, 200, p)
        st, p = call("portfolio_allocation_rebalance", {"account_id": "paper-main"})
        self.assertEqual(st, 200, p)
        st, p = call("oms_resume", {"account_id": "paper-main", "reason": "r" * 501})
        self.assertEqual((st, set(p["errors"])), (400, {"reason"}))          # a note is still bounded

    def test_evidence_overrides_keep_their_checks(self):
        sid = make_strategy(state="candidate")
        st, p = call("holdout_open", {}, {"id": sid})
        self.assertEqual(st, 400)
        self.assertIn("confirm", p["errors"])                              # typed strategy name stays
        st, p = call("validation_exception", {"gate": "G1", "reason": "too short"}, {"id": "bt-000000000000"})
        self.assertEqual(st, 400, p)                                       # ≥ 30 characters stays
        self.assertIn("reason", p["errors"])


class BrokerConfirmTest(BrokerCase):
    def test_connect_route_resolve_without_confirm_words(self):
        st, p = call("broker_connect", {}, {"id": "alpaca-paper"})
        self.assertEqual(st, 200, p)
        sid = make_strategy(name="eq one")
        st, p = call("brokers_routing_post", {"strategy_id": sid, "account_id": "alpaca-paper"})
        self.assertEqual((st, p["route"]["account_id"]), (200, "alpaca-paper"), p)
        self.alpaca.foreign_trade("QQQ", 1, 400.0)
        from strategy_platform.execution import accounts
        from strategy_platform.execution.brokers import reconcile
        self.assertEqual(reconcile.run(accounts.get("alpaca-paper"), "cycle")["status"], "mismatch")
        st, p = call("broker_resolve", {"action": "accept_broker"}, {"id": "alpaca-paper"})
        self.assertEqual((st, p["reconciliation"]["status"]), (200, "ok"), p)
        st, p = call("broker_disconnect", {}, {"id": "alpaca-paper"})
        self.assertEqual((st, p["code"]), (409, "routed"), p)             # a business rule (a strategy is routed here)

    def test_live_guard_keeps_its_reason(self):
        st, p = call("broker_live_guard_put", {"live_flag": False, "capital_cap_usd": None}, {"id": "alpaca-paper"})
        self.assertEqual((st, set(p["errors"])), (400, {"reason"}))


class DoubleStartTest(IncubationCase):
    def test_second_start_is_409_never_a_second_run(self):
        sid = make_strategy()
        incubation.start(sid, 20, "", now=FRI)
        with self.assertRaises(TradingError) as c:
            incubation.start(sid, 20, "", now=FRI)
        self.assertEqual((c.exception.status, c.exception.code), (409, "already_incubating"))
        self.assertEqual(len(incubation.run_rows(sid)), 1)

    def test_concurrent_double_submit(self):
        sid = make_strategy()
        out, gate = [], threading.Barrier(2)

        def go():
            gate.wait()
            try:
                out.append(("ok", incubation.start(sid, 20, "", now=FRI)["run"]["id"]))
            except TradingError as ex:
                out.append(("err", ex.code))
        ts = [threading.Thread(target=go) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        self.assertEqual(sorted(k for k, _ in out), ["err", "ok"], out)
        self.assertIn(("err", "already_incubating"), out)
        self.assertEqual(len(incubation.run_rows(sid)), 1)


class MonitorFindingsTest(IncubationCase):
    def test_paused_numbers_last_check_events_and_alert_per_pause(self):
        sandbox(self)
        import sandbox_fixtures
        sid = make_strategy()
        run = incubation.start(sid, 20, "", now=FRI)["run"]
        sandbox_fixtures.force_decay(sid, now=FRI)
        view = decay.detail(sid, FRI)
        dec = view["strategy"]["decay"]
        nums = dec["paused"]["numbers"]
        self.assertEqual((nums["rule"], nums["synthetic"]), ("drawdown", True))
        self.assertGreater(nums["value"], nums["threshold"])                # the triggering values, not live 0 %
        self.assertEqual(dec["paused"]["numbers_note"], "synthetic pause — live numbers not affected")
        self.assertEqual(dec["last_check_source"], "pause")                # B2: the pause counts as the last check
        self.assertIsNotNone(dec["last_check_at"])
        self.assertEqual(view["series"], [])                                # before the first session …
        self.assertEqual([e["action"] for e in view["events"]], ["paused"])  # … the events are listed (B3)
        decay.decide(sid, "keep_running", "", now=FRI)
        sandbox_fixtures.force_decay(sid, now=FRI)                          # re-pause, same check day
        view = decay.detail(sid, FRI)
        self.assertEqual([e["action"] for e in view["events"]], ["paused", "keep_running", "paused"])
        alerts = decay_alerts(sid)
        self.assertEqual(len(alerts), 2, alerts)                            # B5: one CRITICAL alert per pause
        self.assertTrue(all(a["severity"] == "critical" for a in alerts))
        self.assertEqual(len({a["dedupe_key"] for a in alerts}), 2)
        self.assertEqual(run["id"], view["strategy"]["run_id"])

    def test_real_pause_numbers_not_synthetic(self):
        sid = make_strategy()
        run = incubation.start(sid, 20, "", now=FRI)["run"]
        row = incubation.run_row(run["id"])
        a = decay.assess(decay.expected.basis(row), [-0.2], row["calendar"])
        self.assertIn("drawdown", a["triggered"])
        decay._pause(row, dict(a, triggered=["drawdown"]), "2026-10-05", "2026-10-05T21:00:00+00:00")
        p = decay.detail(sid, FRI)["strategy"]["decay"]["paused"]
        self.assertFalse(p["numbers"]["synthetic"])
        self.assertNotIn("numbers_note", p)
        self.assertNotIn("synthetic", p)


class SandboxNoteTest(unittest.TestCase):
    def test_prices_note_only_in_sandbox(self):
        sandbox(self)
        self.assertEqual(sp.mode_fields()["sandbox"]["prices_note"],
                         "prices for copied positions may be unavailable while brokers are blocked")
        d = tempfile.mkdtemp(prefix="fix1023-n-")
        self.addCleanup(shutil.rmtree, d, True)
        sp.use_root(d)
        self.assertEqual(sp.mode_fields(), {"mode": "normal"})


if __name__ == "__main__":
    unittest.main()
