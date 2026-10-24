"""Stage 10 S10-C R1 — Council foundations (SP §22.3–§22.5): hedgefund.db v25 tables (insert-only), the Council
params (one source of truth, interval multiples of 5, strict keys, broker override, agent view), the store
round-trip, the side-effect-free ``PaperBroker.plan_target`` (same plan as ``target``), and the origin ``council``
(OMS / broker maps, client-id prefix ``cl-``, re-send gate, cancellation on auto-trading off, kill forces the
Council orders off, backend start forces them off). Temp databases only, no network."""
import json
import sqlite3
import unittest

try:
    from sp_trading_testkit import FRI, TradingCase
except ImportError:  # pragma: no cover
    from tests.sp_trading_testkit import FRI, TradingCase

from strategy_platform.council import params as cp
from strategy_platform.council import store as cs
from strategy_platform.core import params as P
from strategy_platform.execution import accounts, control, oms
from strategy_platform.storage import db


def open_council(capital=100000.0):
    """paper-council as the §17.9 migration would leave it (test shortcut): account row + deposit."""
    from strategy_platform.portfolio import settings as pf_settings
    with db._connect() as conn:
        accounts.insert_account(conn, accounts.COUNCIL, "migrated", accounts.broker_params(capital), "user")
        pf_settings.write(accounts.COUNCIL, pf_settings.defaults(), "system", "test", conn=conn)
    acc = accounts.get(accounts.COUNCIL)
    with acc.broker._lock:
        with acc.store.atomic():
            acc.broker.ensure_initialized(accounts.now(), acc.params)
    return acc


def switched_over(on=True):
    db.meta_set(cs.META_SWITCHOVER, json.dumps({"state": "done"}))
    cs.set_orders_enabled(on, "user", "test")


class V25Test(TradingCase):
    def test_tables_insert_only(self):
        self.assertGreaterEqual(db.SCHEMA_VERSION, 26)      # v26: unique legacy old ids (R2 review M1)
        rid = cs.create_run("manual", FRI, None, "open", False, False)
        cs.finish_run(rid, FRI, "ok", "llm", 0.01, {"x": 1})
        cs.save_signals(rid, [{"symbol": "SPY", "agent": "technical", "score": 0.2, "confidence": 0.5,
                               "summary": "s", "details": {"a": 1}, "source": "rules"}])
        did = cs.add_decision({"run_id": rid, "symbol": "SPY", "judge_source": "llm", "action": "LONG",
                               "confidence": 0.8, "ai_generated": True, "provenance": {"model": "m"}})
        cs.add_execution(did, "submitted", None, FRI, "cl-abc")
        cs.save_positions(rid, [{"symbol": "SPY", "qty": 1.0}])
        cs.add_ai_call(rid, "judge", "m", "h", {"prompt_tokens": 1}, 0.01, "ok", {"p": 1}, FRI)
        cs.add_params(cp.defaults(), "system", "t", "test")
        cs.add_broker_override({"fees_bps": 6.0}, "user", "t")
        with db._connect() as conn:
            for t in ("cl_params", "cl_broker_params", "cl_runs", "cl_run_results", "cl_signals", "cl_decisions",
                      "cl_positions", "cl_executions", "cl_ai_calls"):
                with self.assertRaises(sqlite3.DatabaseError, msg=t):
                    conn.execute(f"UPDATE {t} SET rowid = rowid")
                with self.assertRaises(sqlite3.DatabaseError, msg=t):
                    conn.execute(f"DELETE FROM {t}")
        d = cs.run_detail(rid)
        self.assertEqual((d["status"], d["decisions"][0]["action"], d["executions"][0]["client_order_id"]),
                         ("ok", "LONG", "cl-abc"))
        self.assertEqual(d["decisions"][0]["provenance"], {"model": "m"})
        self.assertEqual(cs.runs(5)[0]["id"], rid)
        self.assertEqual(cs.last_decisions(["SPY"])["SPY"]["action"], "LONG")

    def test_outcomes_are_submit_time_only(self):
        with self.assertRaises(Exception):
            cs.add_execution(None, "expired", None, FRI)


class ParamsTest(unittest.TestCase):
    def test_keys_have_one_owner(self):
        self.assertFalse(set(cp.COUNCIL_KEYS) & set(cp.BROKER_KEYS))
        self.assertFalse(set(cp.COUNCIL_KEYS) & set(cp.DROPPED_KEYS))
        self.assertEqual(set(cp.COUNCIL_KEYS) | set(cp.BROKER_KEYS) | set(cp.DROPPED_KEYS), set(P.DEFAULTS))

    def test_from_old_refuses_the_stored_row(self):
        row = {"version": 3, "created_at": "x", "params": {"watchlist": ["SPY"]}, "actor": "u", "note": None}
        with self.assertRaises(ValueError):
            cp.from_old(row)
        with self.assertRaises(ValueError):
            cp.broker_from_old(row)
        self.assertEqual(cp.from_old(row["params"])[0]["watchlist"], ["SPY"])

    def test_interval_multiples_of_5(self):
        self.assertEqual(cp.defaults()["interval_minutes"], 5)
        for bad in (1, 7, 65, 0, 4.5, True, "5"):
            _c, err = cp.validate({"interval_minutes": bad})
            self.assertIn("interval_minutes", err, bad)
        self.assertEqual(cp.validate({"interval_minutes": 15})[0], {"interval_minutes": 15})
        out, notes = cp.from_old({"interval_minutes": 7, "drawdown_stop_pct": 9.0, "closed_interval_minutes": 30})
        self.assertEqual(out["interval_minutes"], 10)
        self.assertTrue(any("rounded up" in n for n in notes) and any("drawdown_stop_pct" in n for n in notes))
        self.assertNotIn("drawdown_stop_pct", out)

    def test_strict_keys(self):
        _c, err = cp.validate({"fees_bps": 5, "nope": 1})
        self.assertEqual(set(err), {"fees_bps", "nope"})
        self.assertIn("not a Council parameter", err["fees_bps"])
        _c, err = cp.validate_broker({"watchlist": ["SPY"]}, cp.broker_from_old({}))
        self.assertIn("watchlist", err)
        _c, err = cp.validate_broker({"maintenance_margin_pct": 60}, cp.broker_from_old({}))
        self.assertIn("maintenance_margin_pct", err)        # > the effective initial margin 50

    def test_override_and_agent_view(self):
        eff = cp.effective_broker({"fees_bps": 5.0, "allow_shorts": True}, {"fees_bps": 7.5, "watchlist": ["X"]})
        self.assertEqual((eff["fees_bps"], eff["allow_shorts"]), (7.5, True))
        self.assertNotIn("watchlist", eff)
        view = cp.agent_view(cp.defaults(), eff, 12.0)
        self.assertEqual((view["drawdown_stop_pct"], view["fees_bps"], view["interval_minutes"]), (12.0, 7.5, 5))


class PlanTargetTest(TradingCase):
    def test_plan_equals_target(self):
        b = self.acc.broker
        for sym, w in (("SPY", 0.05), ("QQQ", -0.03), ("BTC-USD", 0.02)):
            plan = b.plan_target(sym, w, "autopilot", self.clock(), self.acc.params, {})
            self.assertIsNone(plan["reject"])
            before = len(b.store.get_positions())
            results = b.target(sym, w, "autopilot", self.clock(), self.acc.params, {})
            self.assertEqual([(r.side, r.qty) for r in results], [(s, q) for s, q in plan["legs"]])
            self.assertGreaterEqual(len(b.store.get_positions()), before)
        again = b.plan_target("SPY", 0.05, "autopilot", self.clock(), self.acc.params, {})
        results = b.target("SPY", 0.05, "autopilot", self.clock(), self.acc.params, {})
        self.assertEqual([(r.side, r.qty) for r in results], [(s, q) for s, q in again["legs"]])

    def test_plan_has_no_side_effects(self):
        b = self.acc.broker
        n0 = len(b.store.get_orders() if hasattr(b.store, "get_orders") else oms.list_orders(self.acc.id))
        b.plan_target("SPY", 0.05, "autopilot", self.clock(), self.acc.params, {})
        n1 = len(b.store.get_orders() if hasattr(b.store, "get_orders") else oms.list_orders(self.acc.id))
        self.assertEqual(n0, n1)
        self.assertEqual(b.store.get_positions(), [])


class CouncilOriginTest(TradingCase):
    def setUp(self):
        super().setUp()
        self.council = open_council()

    def place(self, symbol="BTC-USD", qty=0.01):
        o, _ = oms.place(self.council, {"symbol": symbol, "side": "buy", "qty": qty, "origin": "council",
                                        "source_id": accounts.COUNCIL_SOURCE, "allow_reduce": True})
        return o

    def test_origin_known_everywhere(self):
        from strategy_platform.core.types import ORDER_ORIGINS
        from strategy_platform.execution.brokers import mirror, paper
        from strategy_platform.execution.paper_broker import REPLACEABLE_BY
        self.assertIn("council", oms.ORIGINS)
        self.assertIn("council", ORDER_ORIGINS)
        self.assertEqual((paper.BROKER_ORIGIN["council"], paper.OMS_ORIGIN["council"], mirror.BROKER_ORIGIN["council"]),
                         ("council", "council", "council"))
        self.assertEqual(REPLACEABLE_BY["council"], ("council",))
        switched_over()
        o = self.place()
        self.assertTrue(o["client_order_id"].startswith("cl-"))
        self.assertEqual((o["origin"], o["state"]), ("council", "filled"))

    def test_resend_gate(self):
        o = {"origin": "council", "side": "buy", "symbol": "BTC-USD"}
        control._write_autotrade(True, "user")
        self.assertIn("not switched over", oms._resend_blocker(self.council, o, self.clock()))
        switched_over(on=False)
        self.assertIn("switched off", oms._resend_blocker(self.council, o, self.clock()))
        switched_over(on=True)
        self.assertIsNone(oms._resend_blocker(self.council, o, self.clock()))
        control._write_autotrade(False, "user")
        self.assertIn("auto trading", oms._resend_blocker(self.council, o, self.clock()))

    def test_auto_trading_off_cancels_council_orders(self):
        switched_over()
        # off-window equities are rejected by the platform's trading-hours check (D7 option B), so a working Council
        # order is stored directly (state "new": stored, not yet sent)
        ts = self.clock().isoformat(timespec="seconds")
        with db._connect() as conn:
            oms._insert(conn, {"client_order_id": "cl-test00000001", "account_id": self.council.id, "created_at": ts,
                               "updated_at": ts, "symbol": "SPY", "side": "buy", "qty": 1.0, "origin": "council",
                               "tif": "day", "state": "new", "filled_qty": 0.0, "fees": 0.0, "attempts": 0,
                               "allow_reduce": 0, "allocations_json": "[]", "checks_json": "[]"})
        o = oms.get("cl-test00000001")
        self.assertIn(o["state"], oms.WORKING)
        self.assertEqual(control._cancel_runtime_orders("cancelled: auto trading off"), 1)
        self.assertEqual(oms.get(o["client_order_id"])["state"], "cancelled")

    def test_kill_forces_council_orders_off(self):
        switched_over()
        self.assertTrue(cs.orders_enabled())
        control.kill_account(self.council, "test kill", "kill", "user")
        self.assertFalse(cs.orders_enabled())
        self.assertEqual(db.list_audit(5, "council_orders_off")[0]["detail"]["cause"], "kill")
        cs.set_orders_enabled(True, "user", "test")
        control.kill_account(self.council, "dd", "drawdown", "engine")
        self.assertFalse(cs.orders_enabled())

    def test_startup_forces_council_orders_off(self):
        switched_over()
        control.force_off_at_start()
        self.assertFalse(cs.orders_enabled())
        self.assertEqual(db.list_audit(5, "council_orders_off")[0]["detail"]["cause"], "startup")

    def test_store_gates_fail_closed(self):
        db.meta_set(cs.META_ORDERS, "{garbage")
        self.assertFalse(cs.orders_enabled())
        db.meta_set(cs.META_SWITCHOVER, json.dumps({"state": "weird"}))
        with self.assertRaises(db.StorageUnavailable):
            cs.switchover()
        self.assertIn("could not be read", oms._council_blocker())


if __name__ == "__main__":
    unittest.main()
