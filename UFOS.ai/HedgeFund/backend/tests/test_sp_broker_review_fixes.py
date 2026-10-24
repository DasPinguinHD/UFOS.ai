"""Stage 8 review fixes 2026-10-18 (SPEC §18.13): verified broker kill (bounded wait for pending cancels, bounded
retries of rejected closes, flatness verified, never a silent success), user actions make one real attempt during
backoff, accept_broker only on a halted account. (The Kraken adapter cases — DuplicateOrderId lookup, wallet scope,
install failure — went with the adapter, stage 10 S10-K.) Fakes, temp DBs, offline."""
import os
import shutil
import sys
import tempfile
import unittest

try:
    from sp_broker_testkit import BrokerCase
except ImportError:  # pragma: no cover
    from tests.sp_broker_testkit import BrokerCase

from strategy_platform import secrets
from strategy_platform.execution import control
from strategy_platform.execution.brokers import common, reconcile, service
from strategy_platform.execution.brokers.common import BrokerNotInstalled
from strategy_platform.execution.errors import BrokerUnavailable, TradingError


class KillVerifiedTest(BrokerCase):
    def setUp(self):
        super().setUp()
        self.slept = []
        orig = common.sleep
        common.sleep = self.slept.append
        self.addCleanup(setattr, common, "sleep", orig)

    def bracket(self, acc, qty=10):
        o = self.bmanual(acc, "SPY", "buy", qty, type="bracket", take_profit={"limit_price": 600.0},
                         stop_loss={"stop_price": 450.0})
        self.assertEqual(o["state"], "filled")
        return o

    def test_pending_cancel_released_then_flat(self):
        acc, _ = self.connect()
        self.bracket(acc)                          # the two legs (sell 10) hold the whole position at the broker
        self.alpaca.cancel_delay = 3               # cancels take a few reads to complete
        out = service.kill(acc, "Kill switch: test", "kill", "user")
        self.assertTrue(out["ok"], out)
        self.assertEqual((out["flat"], out["pending"], out["open_orders_left"], out["not_flat"]), (True, False, 0, []))
        self.assertEqual(self.alpaca.pos, {})
        self.assertTrue(self.slept)                # it waited (bounded) for the pending cancels

    def test_qty_still_held_not_flat_then_completed_by_poller(self):
        acc, _ = self.connect()
        self.bracket(acc)
        self.alpaca.cancel_delay = 10_000          # the cancels never complete within the bound
        out = service.kill(acc, "Kill switch: test", "kill", "user")
        self.assertFalse(out["ok"])
        self.assertEqual((out["flat"], out["pending"]), (False, True))
        self.assertGreater(out["open_orders_left"], 0)
        self.assertEqual(out["attempts"], service.KILL_CLOSE_ATTEMPTS)
        nf = out["not_flat"][0]
        self.assertEqual((nf["symbol"], nf["broker_qty"], nf["close_state"]), ("SPY", 10.0, "rejected"))
        self.assertIn("insufficient qty", nf["reject_reason"])
        self.assertEqual(out["note"], service.NOT_FLAT_NOTE)
        self.assertIn("still open at the broker", out["error"])
        self.assertEqual(acc.halt()["kind"], "kill")
        self.assertEqual(self.alpaca.pos["SPY"][0], 10.0)           # nothing claimed: the broker still holds it
        st = service.broker_status("alpaca-paper")
        self.assertTrue(st["kill"]["pending"])
        self.assertEqual(st["kill"]["not_flat"][0]["symbol"], "SPY")
        with self.assertRaises(TradingError) as cm:
            control.resume("alpaca-paper", "resume please")
        self.assertEqual(cm.exception.code, "kill_pending")
        for o in self.alpaca.orders.values():                       # the cancels complete at the broker
            if o["status"] == "pending_cancel":
                o["cancel_in"] = 1
        self.advance(seconds=20)
        service.poll_once()
        self.assertFalse(service.kill_pending(acc))
        self.assertEqual(self.alpaca.pos, {})
        self.assertFalse(service.broker_status("alpaca-paper")["kill"]["pending"])

    def test_rejected_close_is_retried(self):
        acc, _ = self.connect()
        self.bmanual(acc, "SPY", "buy", 5)
        self.alpaca.reject_closes = 1
        out = service.kill(acc, "Kill switch: test", "kill", "user")
        self.assertTrue(out["ok"], out)
        self.assertEqual((out["flat"], out["attempts"]), (True, 2))
        self.assertEqual([o["state"] for o in out["orders"]], ["rejected", "filled"])
        self.assertEqual(self.alpaca.pos, {})

    def test_queued_close_not_flat_no_duplicate(self):
        acc, _ = self.connect()
        self.bmanual(acc, "SPY", "buy", 5)
        self.alpaca.auto = "none"                   # closes are accepted but not filled (e.g. market closed)
        out = service.kill(acc, "Kill switch: test", "kill", "user")
        self.assertEqual((out["ok"], out["flat"], out["pending"], out["attempts"]), (False, False, True, 1))
        self.assertEqual(out["not_flat"][0]["close_state"], "accepted")
        self.assertEqual(out["error"], "the broker is not flat yet")
        sends = self.alpaca.sends
        self.advance(seconds=20)
        service.poll_once()                         # a close works already: no second one
        self.assertEqual(self.alpaca.sends, sends)
        self.assertTrue(service.kill_pending(acc))
        self.alpaca.fill(out["orders"][0]["client_order_id"])
        self.advance(seconds=20)
        service.poll_once()
        self.assertFalse(service.kill_pending(acc))

    def test_global_kill_reports_not_flat(self):
        acc, _ = self.connect()
        self.bmanual(acc, "SPY", "buy", 2)
        self.alpaca.auto = "none"
        out = control.kill_all("panic", "user")
        b = next(a for a in out["platform"]["accounts"] if a["account_id"] == "alpaca-paper")["broker"]
        self.assertEqual((b["ok"], b["flat"], b["pending"]), (False, False, True))


class UserAttemptAndResolveTest(BrokerCase):
    def test_manual_reconcile_tries_during_backoff(self):
        acc, _ = self.connect()
        self.alpaca.down = True
        self.assertEqual(reconcile.run(acc, "cycle")["status"], "unreachable")
        self.assertIsNotNone(acc.link.blocked())
        self.alpaca.down = False
        out = service.reconcile_now("alpaca-paper")                 # still in backoff: the user action tries
        self.assertEqual(out["reconciliation"]["status"], "ok")
        self.alpaca.down = True
        self.advance(seconds=1)
        n = len(self.alpaca.calls)
        res = service.reconcile_now("alpaca-paper")["reconciliation"]
        self.assertEqual(res["status"], "unreachable")
        self.assertEqual(len(self.alpaca.calls) - n, 1)             # exactly one real attempt, then blocked

    def test_accept_broker_refused_when_not_halted(self):
        acc, _ = self.connect()
        self.alpaca.down = True
        reconcile.run(acc, "cycle")                                 # unreachable, not halted
        with self.assertRaises(TradingError) as cm:
            reconcile.resolve(acc, "accept_broker", "accept anyway", confirm="ACCEPT BROKER VALUES")
        self.assertEqual(cm.exception.code, "nothing_to_resolve")




if __name__ == "__main__":
    unittest.main()
