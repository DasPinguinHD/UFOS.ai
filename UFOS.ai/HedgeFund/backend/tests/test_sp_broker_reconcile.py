"""Stage 8 reconciliation, kill switch per broker, dead-man switch, runtime (SPEC §18.5, §18.6): a mismatch halts the
broker account (no orders at all, even reducing ones), alerts with a clear reason, stays halted until the user
resolves it (recheck / cancel_unknown_orders / accept_broker — no auto-correction); unreachable never halts but
blocks new orders; the kill switch cancels everything at the broker and closes what the BROKER holds (also when the
broker comes back later); the global kill and the dead-man switch cover broker accounts. Fakes, temp DBs."""
import json
import math
import unittest

try:
    from sp_broker_testkit import BrokerCase
except ImportError:  # pragma: no cover
    from tests.sp_broker_testkit import BrokerCase

from strategy_platform.execution import accounts, control, oms
from strategy_platform.execution.brokers import mirror, reconcile, service
from strategy_platform.execution.errors import TradingError
from strategy_platform.portfolio import ledger
from strategy_platform.storage import db


def audits(action):
    with db._connect() as c:
        return [json.loads(r["detail_json"]) for r in c.execute("SELECT detail_json FROM audit WHERE action = ?",
                                                                 (action,)).fetchall()]


class ReconcileTest(BrokerCase):
    def test_position_mismatch_halts_and_blocks_everything(self):
        acc, _ = self.connect()
        self.bmanual(acc, "SPY", "buy", 10)
        self.alpaca.foreign_trade("SPY", 5, 500.0)                 # bought at the broker, outside the platform
        res = reconcile.run(acc, "cycle")
        self.assertEqual(res["status"], "mismatch")
        self.assertTrue(res["halted"])
        self.assertIn("SPY 15 at the broker vs 10 expected", res["reason"])
        self.assertTrue(any(m["kind"] == "cash" for m in res["mismatches"]))
        h = acc.halt()
        self.assertEqual((h["active"], h["kind"]), (True, "reconcile"))
        self.assertEqual(len(audits("broker_reconcile_mismatch")), 1)
        self.assertTrue(any("reconciliation mismatch" in e["title"] for e in acc.store.all_events()))
        o = self.bmanual(acc, "SPY", "sell", 5)                   # even a reducing order is refused
        self.assertEqual(o["state"], "rejected")
        self.assertIn("halted", o["reject_reason"])
        reconcile.run(acc, "cycle")                                # a second mismatch: no second alert
        self.assertEqual(len(audits("broker_reconcile_mismatch")), 1)
        with self.assertRaises(TradingError) as cm:
            control.resume("alpaca-paper", "try resume")
        self.assertEqual(cm.exception.code, "reconcile_mismatch")
        # the user fixes it at the broker (sells the 5 again) -> recheck clears the halt
        self.alpaca.foreign_trade("SPY", -5, 500.0)
        out = reconcile.resolve(acc, "recheck", "sold the stray shares at Alpaca")
        self.assertEqual(out["reconciliation"]["status"], "ok")
        self.assertFalse(acc.halt()["active"])
        self.assertEqual(audits("broker_resolve")[0]["action"], "recheck")

    def test_ok_does_not_auto_clear(self):
        acc, _ = self.connect()
        self.alpaca.foreign_trade("SPY", 1, 500.0)
        self.assertEqual(reconcile.run(acc, "cycle")["status"], "mismatch")
        self.alpaca.foreign_trade("SPY", -1, 500.0)
        res = reconcile.run(acc, "cycle")
        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["halted"])                             # only the user's resolve clears it
        self.assertTrue(acc.halt()["active"])

    def test_unknown_order_and_cancel_unknown(self):
        acc, _ = self.connect()
        oid = self.alpaca.foreign_order("QQQ", "buy", 1)
        res = reconcile.run(acc, "cycle")
        self.assertEqual(res["status"], "mismatch")
        self.assertEqual(res["open_orders"]["unknown_at_broker"][0]["broker_order_id"], oid)
        # fix1023: accept_broker no longer needs a typed confirm phrase (covered in test_fix1023)
        out = reconcile.resolve(acc, "cancel_unknown_orders", "cancel the stray order")
        self.assertEqual(out["reconciliation"]["status"], "ok")
        self.assertEqual(self.alpaca.orders[oid]["status"], "cancelled")
        self.assertFalse(acc.halt()["active"])

    def test_missing_order_detected(self):
        acc, _ = self.connect()
        self.alpaca.auto = "none"
        o = self.bmanual(acc, "SPY", "buy", 2, type="limit", limit_price=400.0)
        self.assertEqual(o["state"], "accepted")
        del self.alpaca.by_cid[o["client_order_id"]]
        self.alpaca.orders.pop(o["broker_order_id"])
        res = reconcile.run(acc, "cycle")
        self.assertEqual(res["status"], "mismatch")
        self.assertEqual(res["open_orders"]["missing_at_broker"][0]["client_order_id"], o["client_order_id"])

    def test_accept_broker_values(self):
        acc, _ = self.connect()
        sid = "s-aaaaaaaaaaaa"
        self.bmanual(acc, "SPY", "buy", 10, source_id=sid, allocations=[(sid, 10)])
        self.alpaca.foreign_trade("SPY", -4, 520.0)               # sold 4 at the broker (e.g. by hand)
        self.alpaca.cash += 3.25                                   # and some unexplained cash (a fee rebate)
        self.assertEqual(reconcile.run(acc, "cycle")["status"], "mismatch")
        out = reconcile.resolve(acc, "accept_broker", "accept Alpaca's numbers", confirm="ACCEPT BROKER VALUES")
        self.assertEqual(out["reconciliation"]["status"], "ok", out["reconciliation"].get("reason"))
        self.assertEqual(acc.store.get_position("SPY")["qty"], 6.0)
        self.assertAlmostEqual(acc.store.cash_balance(), self.alpaca.cash)
        hold = ledger.holdings(acc.id)
        self.assertEqual(hold.get(sid, {}).get("SPY"), 6.0)
        rows = ledger.entries(acc.id)
        self.assertAlmostEqual(math.fsum(r["amount"] for r in rows), acc.store.cash_balance(), places=6)
        self.assertFalse(acc.halt()["active"])
        self.assertEqual(out["resolution"]["detail"]["positions"][0]["to"], 6.0)

    def test_cash_tolerance(self):
        acc, _ = self.connect()
        self.alpaca.cash += 0.5                                    # within max($1, 0.01 % of equity)
        self.assertEqual(reconcile.run(acc, "cycle")["status"], "ok")
        self.alpaca.cash += 20.0
        res = reconcile.run(acc, "cycle")
        self.assertEqual(res["status"], "mismatch")
        self.assertIn("cash 100,020.50 at the broker vs 100,000.00 expected", res["reason"])

    def test_unreachable_never_halts_but_blocks(self):
        acc, _ = self.connect()
        self.alpaca.down = True
        res = reconcile.run(acc, "cycle")
        self.assertEqual(res["status"], "unreachable")
        self.assertFalse(acc.halt()["active"])
        self.assertIn("connection reset", res["error"])
        self.alpaca.down = False
        o = self.bmanual(acc, "SPY", "buy", 1)
        self.assertEqual(o["state"], "rejected")                   # link still backing off / not reconciled
        self.assertEqual(next(c for c in o["checks"] if not c["ok"])["check"], "broker_link")
        self.advance(seconds=20)
        self.assertEqual(reconcile.run(acc, "cycle")["status"], "ok")
        self.assertEqual(self.bmanual(acc, "SPY", "buy", 1)["state"], "filled")

    def test_stale_reconciliation_blocks(self):
        acc, _ = self.connect()
        self.advance(minutes=20)
        o = self.bmanual(acc, "SPY", "buy", 1)
        self.assertEqual(o["state"], "rejected")
        self.assertIn("min old", o["reject_reason"])
        service.poll_once()                                        # the poller reconciles (≥ 5 min old)
        self.assertEqual(self.bmanual(acc, "SPY", "buy", 1)["state"], "filled")

    def test_runtime_housekeeping_reconciles(self):
        acc, _ = self.connect()
        self.alpaca.foreign_trade("SPY", 2, 500.0)
        self.rt.cycle()
        self.assertTrue(acc.halt()["active"])
        self.assertEqual(reconcile.latest(acc.id)["trigger"], "cycle")


class KillTest(BrokerCase):
    def test_kill_cancels_all_and_flattens_broker_positions(self):
        acc, _ = self.connect()
        self.bmanual(acc, "SPY", "buy", 10)
        self.alpaca.auto = "none"
        w = self.bmanual(acc, "QQQ", "buy", 2, type="limit", limit_price=300.0)
        foreign = self.alpaca.foreign_order("SPY", "buy", 1)
        self.alpaca.foreign_trade("QQQ", 3, 400.0)                 # a position the mirror does not know
        self.alpaca.auto = "fill"
        out = service.kill(acc, "Kill switch: test", "kill", "user")
        self.assertTrue(out["ok"], out["error"])
        self.assertEqual(oms.get(w["client_order_id"])["state"], "cancelled")
        self.assertEqual(self.alpaca.orders[foreign]["status"], "cancelled")
        self.assertEqual({o["symbol"] for o in out["orders"]}, {"SPY", "QQQ"})
        self.assertEqual(self.alpaca.pos, {})                      # flat AT THE BROKER
        self.assertEqual(acc.halt()["kind"], "kill")
        self.assertEqual(audits("broker_kill")[0]["account_id"], "alpaca-paper")

    def test_kill_while_unreachable_completes_later(self):
        acc, _ = self.connect()
        self.bmanual(acc, "SPY", "buy", 4)
        self.alpaca.down = True
        out = service.kill(acc, "Kill switch: test", "kill", "user")
        self.assertFalse(out["ok"])
        self.assertIn("closed automatically", out["note"])
        self.assertTrue(service.kill_pending(acc))
        with self.assertRaises(TradingError) as cm:
            control.resume("alpaca-paper", "resume please")
        self.assertEqual(cm.exception.code, "kill_pending")
        self.alpaca.down = False
        self.advance(minutes=1)
        service.poll_once()
        self.assertFalse(service.kill_pending(acc))
        self.assertEqual(self.alpaca.pos, {})

    def test_global_kill_covers_brokers(self):
        acc, _ = self.connect()
        self.bmanual(acc, "SPY", "buy", 3)
        out = control.kill_all("panic", "user")
        ids = [a["account_id"] for a in out["platform"]["accounts"]]
        self.assertIn("alpaca-paper", ids)
        self.assertEqual(self.alpaca.pos, {})
        self.assertEqual(accounts.get("alpaca-paper").halt()["kind"], "kill")

    def test_dead_man_cancels_runtime_orders_at_broker(self):
        acc, _ = self.connect()
        self.alpaca.auto = "none"
        o = self.bmanual(acc, "SPY", "buy", 1, origin="runtime")
        self.assertEqual(o["state"], "accepted")
        control._write_autotrade(True, "user")
        control._last_seen = self.clock()
        self.advance(hours=2)
        self.assertTrue(control.check_heartbeat())
        self.assertEqual(oms.get(o["client_order_id"])["state"], "cancelled")
        self.assertEqual(self.alpaca.by_cid[o["client_order_id"]]["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
