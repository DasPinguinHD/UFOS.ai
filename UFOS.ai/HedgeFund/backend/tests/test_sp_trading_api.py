"""Stage 7 routes (SPEC §17.10/§17.11): /v2/portfolio/*, /v2/oms/*, /v2/incubation* —
strict input (400 with field errors), the documented payload keys, refusals {error, code},
strict JSON (no NaN), heartbeat touched by GETs. Handlers called directly (no aiohttp).
Temp databases, fakes. Paper only."""
import asyncio
import unittest

try:
    from sp_trading_testkit import EXPECTED, TradingCase, make_strategy
except ImportError:  # pragma: no cover
    from tests.sp_trading_testkit import EXPECTED, TradingCase, make_strategy

from strategy_platform.api import routes
from strategy_platform.execution import control
from strategy_platform.live import incubation
from strategy_platform.storage import db


def call(name, query=None, body=routes.NO_BODY, match=None):
    api = routes.V2Api()
    status, payload = asyncio.run(getattr(api, name)(query or {}, body, match or {}))
    routes.dumps(payload)                        # strict JSON
    return status, payload


class OpenAccountTest(TradingCase):
    open_main = False

    def test_open_once_and_validation(self):
        st, p = call("portfolio_state")
        self.assertEqual(st, 200)
        self.assertEqual(p["accounts"], [])
        self.assertTrue(p["can"]["open_account"])
        self.assertEqual(p["legacy_account"]["status"], "active")
        self.assertEqual(call("portfolio_account_open", body={"starting_capital": 10})[0], 400)
        self.assertEqual(call("portfolio_account_open", body={"capital": 1000})[1]["errors"], {"capital": "unknown field"})
        st, p = call("portfolio_account_open", body={"starting_capital": 50000})
        self.assertEqual(st, 201)
        self.assertEqual(p["account"]["account"]["equity"], 50000.0)
        st, p = call("portfolio_account_open", body={})
        self.assertEqual((st, p["code"]), (409, "account_exists"))
        st, p = call("oms_order_create", body={"account_id": "paper-council", "symbol": "SPY", "side": "buy", "qty": 1})
        self.assertEqual((st, p["code"]), (404, "not_found"))


class ApiTest(TradingCase):
    def test_state_and_account_payloads(self):
        st, p = call("portfolio_state")
        self.assertEqual(set(p), {"now", "paper_only", "notice", "autotrade", "runtime", "legacy_account", "accounts",
                                  "can"})
        a = p["accounts"][0]
        self.assertEqual(set(a), {"id", "label", "broker", "paper", "origin", "opened_at", "account", "valuation",
                                  "halt", "day_loss", "drawdown", "positions_count", "working_orders", "sources",
                                  "broker_link", "reconciliation"})          # + stage 8 (SPEC §18.10)
        self.assertIsNone(a["broker_link"])
        self.assertIsNone(a["reconciliation"])
        self.assertIsNotNone(control.last_seen())                    # GETs are heartbeats
        st, o = call("oms_order_create", body={"account_id": "paper-main", "symbol": "SPY", "side": "buy", "qty": 10,
                                               "client_order_id": "wpf-guid-0001"})
        self.assertEqual((st, o["order"]["state"]), (201, "filled"))
        st, o2 = call("oms_order_create", body={"account_id": "paper-main", "symbol": "SPY", "side": "buy", "qty": 10,
                                                "client_order_id": "wpf-guid-0001"})
        self.assertEqual((st, o2["order"]["client_order_id"]), (200, "wpf-guid-0001"))
        st, p = call("portfolio_account_get", match={"id": "paper-main"})
        self.assertEqual(set(p), {"account", "positions", "working_orders", "sources", "settings"})
        self.assertEqual(p["positions"][0]["by_source"], [{"source_id": "_unallocated", "qty": 10.0}])
        st, p = call("portfolio_sources", {"account_id": "paper-main"})
        s = p["sources"][0]
        for k in ("source_id", "kind", "name", "capital_in", "cash", "equity", "positions", "pnl", "costs", "exposure"):
            self.assertIn(k, s)
        st, p = call("portfolio_source_ledger", {"account_id": "paper-main", "limit": "5"}, match={"source_id": "_unallocated"})
        self.assertEqual(st, 200)
        self.assertLessEqual(len(p["entries"]), 5)
        self.assertEqual(call("portfolio_account_equity", {"since": "bad"}, match={"id": "paper-main"})[0], 400)
        self.assertEqual(call("portfolio_account_get", match={"id": "paper-x"})[0], 404)
        st, p = call("oms_fills", {"account_id": "paper-main"})
        self.assertEqual((st, len(p["fills"])), (200, 1))
        st, p = call("oms_order_get", match={"client_order_id": "wpf-guid-0001"})
        self.assertEqual([e["to_state"] for e in p["events"]], ["new", "submitted", "filled"])

    def test_order_validation_and_cancel(self):
        st, p = call("oms_order_create", body={"account_id": "paper-main", "symbol": "SPY", "side": "buy", "qty": 1.5})
        self.assertEqual(st, 400)
        self.assertIn("whole shares", p["errors"]["qty"])
        st, p = call("oms_order_create", body={"account_id": "paper-main", "symbol": "SPY", "side": "hold", "qty": 1,
                                               "extra": 1})
        self.assertEqual(set(p["errors"]), {"side", "extra"})
        st, p = call("oms_order_create", body={"account_id": "paper-main", "symbol": "SPY", "side": "buy", "qty": 1,
                                               "notional": 100})
        self.assertIn("qty", p["errors"])
        # client-supplied ids: the contract pattern (§17.7) and the engine's prefixes are reserved (audit 2026-10-18)
        for bad in ("short", "has.dot-0001", "x" * 65, "rt-ex1a2b3c4d-SPY-1", "KILL-0123456789", "ddk-0123456789"):
            st, p = call("oms_order_create", body={"account_id": "paper-main", "symbol": "SPY", "side": "buy",
                                                   "qty": 1, "client_order_id": bad})
            self.assertEqual(st, 400, bad)
            self.assertIn("client_order_id", p["errors"], bad)
        st, o = call("oms_order_create", body={"account_id": "paper-main", "symbol": "SPY", "side": "buy", "qty": 1})
        st, p = call("oms_order_cancel", body={}, match={"client_order_id": o["order"]["client_order_id"]})
        self.assertEqual((st, p["code"]), (409, "not_cancellable"))

    def test_settings(self):
        st, p = call("portfolio_settings_get", {"account_id": "paper-main"})
        self.assertEqual(p["settings"]["daily_loss_pct"], 2.0)
        self.assertEqual(p["settings"]["drawdown_kill_pct"], 10.0)
        self.assertEqual(p["bounds"]["daily_loss_pct"]["group"], "post_trade")
        st, p = call("portfolio_settings_put", body={"account_id": "paper-main", "reason": "tighter",
                                                     "changes": {"daily_loss_pct": 50}})
        self.assertEqual(st, 400)
        self.assertIn("daily_loss_pct", p["errors"])
        st, p = call("portfolio_settings_put", body={"account_id": "paper-main", "reason": "tighter",
                                                     "changes": {"daily_loss_pct": 1.5, "drawdown_kill_pct": 8}})
        self.assertEqual((st, p["settings"]["daily_loss_pct"], p["version"]), (200, 1.5, 2))
        self.assertIn("settings_changed", [a["action"] for a in db.list_audit(20)])
        self.assertEqual(call("portfolio_settings_put", body={"account_id": "paper-main", "changes": {"x": 1}})[0], 400)

    def test_controls(self):
        st, p = call("oms_autotrade", body={"enabled": "yes"})
        self.assertEqual(st, 400)
        st, p = call("oms_autotrade", body={"enabled": True})
        self.assertTrue(p["autotrade"]["enabled"])
        st, p = call("oms_kill", body={"reason": "test"})
        self.assertEqual(st, 200)
        self.assertTrue(p["platform"]["ok"])
        self.assertFalse(p["legacy"]["ok"])                         # no old engine wired in this test
        st, p = call("oms_autotrade", body={"enabled": True})
        self.assertEqual((st, p["code"]), (409, "halted"))
        st, p = call("oms_resume", body={"account_id": "paper-main", "reason": "ok again"})
        self.assertEqual(st, 200)
        self.assertFalse(p["account"]["halt"]["active"])
        st, p = call("oms_runtime")
        self.assertEqual(set(p), {"running", "cycle_in_progress", "last_cycle_at", "next_cycle_at", "last_error",
                                  "cycles"})
        self.assertEqual(call("oms_runtime_run", body={})[0], 202)
        self.assertEqual(call("oms_runtime_run", body={"x": 1})[0], 400)

    def test_migration_routes(self):
        st, p = call("portfolio_migration_get")
        self.assertEqual(set(p), {"legacy_account", "migration", "can_migrate"})
        st, p = call("portfolio_migration_dry_run", body={})
        self.assertEqual(st, 200)
        self.assertIn("blockers", p)
        st, p = call("portfolio_migration_run", body={"confirm": "yes", "reason": "r" * 501})
        self.assertEqual(st, 400)
        self.assertEqual(set(p["errors"]), {"reason"})           # fix1023: the confirm word is ignored

    def test_incubation_routes(self):
        real = incubation.expected_for
        incubation.expected_for = lambda sid, v: dict(EXPECTED)
        self.addCleanup(setattr, incubation, "expected_for", real)
        sid = make_strategy()
        st, p = call("incubation_eligibility", {"strategy_id": sid})
        self.assertEqual((st, p["eligible"], p["defaults"]["length_sessions"]), (200, True, 60))
        self.assertEqual(call("incubation_create", body={"strategy_id": sid, "length_sessions": 10, "reason": "x y"})[0],
                         400)
        st, p = call("incubation_create", body={"strategy_id": sid, "length_sessions": 40, "reason": "paper first"})
        self.assertEqual(st, 201, p)
        rid = p["run"]["id"]
        self.assertEqual(p["strategy"]["state"], "incubation")
        st, p = call("incubation_create", body={"strategy_id": sid, "length_sessions": 40, "reason": "again"})
        self.assertEqual((st, p["code"]), (409, "already_incubating"))
        st, p = call("incubation_get", match={"id": rid})
        self.assertEqual(set(p), {"run", "events", "decisions", "decay_events"})         # decay_events: fix1023 r2
        self.assertEqual(p["run"]["length_sessions"], 40)
        st, p = call("incubation_list", {"strategy_id": sid})
        self.assertEqual(len(p["runs"]), 1)
        st, p = call("portfolio_allocation", {"account_id": "paper-main"})
        self.assertEqual(p["allocation"]["weights"][0]["source_id"], sid)
        st, p = call("incubation_abort", body={"reason": "stop"}, match={"id": rid})
        self.assertEqual((st, p["run"]["status"], p["strategy"]["state"]), (200, "aborted", "validated"))
        self.assertEqual(call("incubation_get", match={"id": "inc-zzz"})[0], 404)


if __name__ == "__main__":
    unittest.main()
