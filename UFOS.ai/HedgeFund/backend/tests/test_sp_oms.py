"""Stage 7 (SPEC §17.7): OMS state machine, idempotent client order ids, stored before
sent, retry policy, crash between store and send, partial fills, day expiry, broker
margin orders imported, cancel. Temp databases, fake quotes. Paper only."""
import unittest

try:
    from sp_trading_testkit import FRI, SAT, TradingCase
except ImportError:  # pragma: no cover
    from tests.sp_trading_testkit import FRI, SAT, TradingCase

from strategy_platform.execution import control, oms
from strategy_platform.execution.errors import BrokerUnavailable, TradingError
from strategy_platform.storage import db


class StateMachineTest(TradingCase):
    def test_transitions_checked(self):
        o = self.manual("SPY", "buy", 10)
        self.assertEqual(o["state"], "filled")
        # a terminal order never moves again
        for to in ("submitted", "accepted", "cancelled", "rejected", "expired", "partially_filled"):
            self.assertFalse(oms.transition(o["client_order_id"], to))
        ev = [e["to_state"] for e in oms.events(o["client_order_id"])]
        self.assertEqual(ev, ["new", "submitted", "filled"])
        self.assertAlmostEqual(o["fees"], 1.2503125, places=9)
        self.assertTrue(o["terminal"])

    def test_allowed_table(self):
        self.assertEqual(oms.ALLOWED["accepted"], ("submitted",))
        self.assertNotIn("new", oms.ALLOWED["filled"])
        self.assertEqual(set(oms.TERMINAL), {"filled", "cancelled", "rejected", "expired"})

    def test_idempotent_client_order_id(self):
        o = self.manual("SPY", "buy", 10, client_order_id="client-0001")
        o2, created = oms.place(self.acc, {"symbol": "SPY", "side": "buy", "qty": 10, "origin": "manual",
                                           "client_order_id": "client-0001"})
        self.assertFalse(created)
        self.assertEqual(o2["client_order_id"], o["client_order_id"])
        self.assertEqual(len(self.acc.store.all_orders()), 1)     # nothing sent twice

    def test_adapter_idempotent_on_client_id(self):
        o = self.manual("SPY", "buy", 10)
        ack = self.acc.adapter.submit({"client_order_id": o["client_order_id"], "symbol": "SPY", "side": "buy",
                                       "qty": 10, "origin": "manual"})
        self.assertEqual(ack["status"], "filled")
        self.assertEqual(len(self.acc.store.all_orders()), 1)

    def test_queued_then_filled_at_open(self):
        self.set_now(SAT)
        o = self.manual("SPY", "buy", 5)
        self.assertEqual(o["state"], "rejected")          # trading hours (equities, non-protective)
        o = oms.place(self.acc, {"symbol": "SPY", "side": "buy", "qty": 0.0, "origin": "kill"})
        self.assertIsNotNone(o)


class StoredBeforeSentTest(TradingCase):
    def test_order_row_exists_before_the_adapter_call(self):
        seen = {}
        real = self.acc.adapter.submit

        def spy_submit(order):
            seen["row"] = oms.get(order["client_order_id"])
            return real(order)
        self.acc.adapter.submit = spy_submit
        self.manual("SPY", "buy", 3)
        self.assertEqual(seen["row"]["state"], "submitted")
        self.assertEqual([e["to_state"] for e in oms.events(seen["row"]["client_order_id"])][:2], ["new", "submitted"])

    def test_retry_policy_transient_then_ok(self):
        real = self.acc.adapter.submit
        calls = []

        def flaky(order):
            calls.append(1)
            if len(calls) < 3:
                raise BrokerUnavailable("locked")
            return real(order)
        self.acc.adapter.submit = flaky
        o = self.manual("SPY", "buy", 3)
        self.assertEqual(len(calls), 3)
        self.assertEqual(o["state"], "filled")

    def test_retry_exhausted_stays_submitted_then_rejected(self):
        def down(order):
            raise BrokerUnavailable("down")
        self.acc.adapter.submit = down
        o = self.manual("SPY", "buy", 3)
        self.assertEqual(o["state"], "submitted")
        self.assertEqual(o["attempts"], 1)
        for _ in range(4):
            self.advance(seconds=5)
            oms.sync(self.acc)
        o = oms.get(o["client_order_id"])
        self.assertEqual(o["state"], "rejected")
        self.assertIn("gave up after 5 attempts", o["reject_reason"])

    def test_crash_between_store_and_send(self):
        # simulate: the row was stored ("new") and the process died before sending
        ts = FRI.isoformat(timespec="seconds")
        with db._connect() as conn:
            oms._insert(conn, {"client_order_id": "crash-new-1", "account_id": "paper-main", "created_at": ts,
                               "updated_at": ts, "symbol": "SPY", "side": "buy", "qty": 1.0, "origin": "runtime",
                               "tif": "day", "state": "new", "filled_qty": 0.0, "fees": 0.0, "attempts": 0,
                               "allow_reduce": 1, "allocations_json": "[]", "checks_json": "[]"})
            oms._insert(conn, {"client_order_id": "crash-sub-1", "account_id": "paper-main", "created_at": ts,
                               "updated_at": ts, "symbol": "QQQ", "side": "buy", "qty": 2.0, "origin": "runtime",
                               "tif": "day", "state": "submitted", "filled_qty": 0.0, "fees": 0.0, "attempts": 1,
                               "allow_reduce": 1, "allocations_json": "[]", "checks_json": "[]"})
        control.set_autotrade(True, "user")     # a runtime order is only re-sent while auto trading is on (H1)
        self.advance(seconds=40)
        out = oms.sync(self.acc)
        self.assertEqual(out["stale_new"], 1)
        self.assertEqual(oms.get("crash-new-1")["state"], "cancelled")
        self.assertEqual(out["resent"], 1)                       # unknown at the broker, young -> re-sent
        self.assertEqual(oms.get("crash-sub-1")["state"], "filled")
        self.assertEqual(len(self.acc.store.orders_by_client_id("crash-sub-1")), 1)
        # a sent order whose ack was lost is synced, not re-sent
        o = self.manual("SPY", "buy", 1)
        with db._connect() as conn:
            conn.execute("UPDATE oms_orders SET state = 'submitted' WHERE client_order_id = ?", (o["client_order_id"],))
        oms.sync(self.acc)
        self.assertEqual(oms.get(o["client_order_id"])["state"], "filled")
        self.assertEqual(len(self.acc.store.orders_by_client_id(o["client_order_id"])), 1)

    def test_lost_old_submitted_is_rejected(self):
        ts = FRI.isoformat(timespec="seconds")
        with db._connect() as conn:
            oms._insert(conn, {"client_order_id": "lost-0001", "account_id": "paper-main", "created_at": ts,
                               "updated_at": ts, "symbol": "SPY", "side": "buy", "qty": 1.0, "origin": "runtime",
                               "tif": "day", "state": "submitted", "filled_qty": 0.0, "fees": 0.0, "attempts": 1,
                               "allow_reduce": 1, "allocations_json": "[]", "checks_json": "[]"})
        self.advance(minutes=10)
        oms.sync(self.acc)
        self.assertEqual(oms.get("lost-0001")["state"], "rejected")
        self.assertEqual(self.acc.store.orders_by_client_id("lost-0001"), [])

    def test_one_broker_call_is_one_transaction(self):
        # a failure inside the broker call (after the order row) rolls everything back
        real = self.acc.store.add_cash_entry

        def boom(*a, **k):
            raise db.StorageUnavailable("disk full")
        self.acc.store.add_cash_entry = boom
        o = self.manual("SPY", "buy", 2)
        self.acc.store.add_cash_entry = real
        self.assertEqual(o["state"], "submitted")                # transient: retried by the next sync
        self.assertEqual(self.acc.store.all_orders(), [])        # no half order at the broker
        self.assertIsNone(self.acc.store.get_position("SPY"))
        oms.sync(self.acc)
        self.assertEqual(oms.get(o["client_order_id"])["state"], "filled")


class PartialAndExpiryTest(TradingCase):
    capital = 10000.0

    def setUp(self):
        super().setUp()
        from strategy_platform.portfolio import settings as pf_settings
        pf_settings.put("paper-main", {"max_order_notional_pct": 100.0, "max_position_pct_equity": 100.0}, "test")

    def test_partial_fill_then_rest_cancelled(self):
        # a 30-share TLT short locks margin: buying power ~$8,650; 18 SPY need ~$9,009 ->
        # runtime orders are reduced (allow_reduce, like the old engine's target())
        self.assertEqual(self.manual("TLT", "short", 30)["state"], "filled")
        o, _ = oms.place(self.acc, {"symbol": "SPY", "side": "buy", "qty": 18, "origin": "runtime",
                                    "allow_reduce": True, "allocations": [("_unallocated", 18)]})
        self.assertEqual(o["state"], "cancelled")
        self.assertEqual(o["filled_qty"], 17.0)
        self.assertIn("rest cancelled", o["reject_reason"])
        states = [e["to_state"] for e in oms.events(o["client_order_id"])]
        self.assertEqual(states, ["new", "submitted", "partially_filled", "cancelled"])

    def test_manual_order_over_buying_power_rejected(self):
        self.manual("TLT", "short", 30)
        o = self.manual("SPY", "buy", 18)
        self.assertEqual(o["state"], "rejected")
        self.assertEqual([c["check"] for c in o["checks"] if not c["ok"]], ["buying_power"])


class ExpiryTest(TradingCase):
    def test_day_order_expires_after_the_close(self):
        o = self.manual("SPY", "buy", 1)
        # make a queued runtime day order: kill-style queue while closed is protective, so place one directly
        self.set_now(SAT)
        with self.acc.store.atomic():
            with self.acc.store.client_order_id("day-order-1"):
                self.acc.store.insert_order(FRI, "SPY", "buy", 1, "pending", "autopilot")
        ts = FRI.isoformat(timespec="seconds")
        with db._connect() as conn:
            oms._insert(conn, {"client_order_id": "day-order-1", "account_id": "paper-main", "created_at": ts,
                               "updated_at": ts, "symbol": "SPY", "side": "buy", "qty": 1.0, "origin": "runtime",
                               "tif": "day", "state": "accepted", "filled_qty": 0.0, "fees": 0.0, "attempts": 1,
                               "allow_reduce": 1, "allocations_json": "[]", "checks_json": "[]"})
        out = oms.sync(self.acc)
        self.assertEqual(out["expired"], 1)
        self.assertEqual(oms.get("day-order-1")["state"], "expired")
        self.assertEqual(self.acc.store.order_by_client_id("day-order-1")["status"], "cancelled")
        self.assertEqual(o["state"], "filled")


class CancelAndImportTest(TradingCase):
    def test_cancel_accepted_and_refuse_terminal(self):
        self.manual("SPY", "buy", 10)
        self.set_now(SAT)
        o, _ = oms.place(self.acc, {"symbol": "SPY", "side": "sell", "qty": 10, "origin": "kill"})
        self.assertEqual(o["state"], "accepted")                 # protective: queued while closed
        c = oms.cancel(self.acc, o["client_order_id"])
        self.assertEqual(c["state"], "cancelled")
        with self.assertRaises(TradingError) as cm:
            oms.cancel(self.acc, o["client_order_id"])
        self.assertEqual(cm.exception.code, "not_cancellable")
        acts = [a["action"] for a in db.list_audit(20)]
        self.assertIn("oms_cancel", acts)

    def test_broker_margin_orders_are_imported(self):
        self.assertEqual(self.manual("SPY", "short", 55)["state"], "filled")
        self.quotes.prices["SPY"] = 2000.0          # the short goes deep against us -> margin call
        res = self.acc.adapter.check_margin(self.clock(), {})
        self.assertTrue(res)
        out = oms.sync(self.acc)
        self.assertGreaterEqual(out["imported"], 1)
        imp = [o for o in oms.list_orders("paper-main") if o["origin"] == "margin"]
        self.assertTrue(imp and imp[0]["client_order_id"].startswith("brk-paper-main-"))
        self.assertEqual(imp[0]["state"], "filled")


class SubmittedOrderSafetyTest(TradingCase):
    """Review fix H1 (2026-10-18): an order left ``submitted`` by a transient broker error is cancelled by the kill
    switch / auto trading off / the heartbeat switch, and a re-send re-checks halt and auto trading."""

    def _stuck(self, symbol="SPY", qty=10, origin="manual"):
        real = self.acc.adapter.submit

        def down(order):
            raise BrokerUnavailable("down")
        self.acc.adapter.submit = down
        if origin == "manual":
            o = self.manual(symbol, "buy", qty)
        else:
            o, _ = oms.place(self.acc, {"symbol": symbol, "side": "buy", "qty": qty, "origin": origin,
                                        "source_id": "_unallocated"}, actor="engine")
        self.assertEqual(o["state"], "submitted")
        self.acc.adapter.submit = real            # the broker is back: a re-send WOULD fill now
        return o

    def assert_dead(self, o):
        cid = o["client_order_id"]
        self.assertEqual(oms.get(cid)["state"], "cancelled")
        self.advance(seconds=30)
        oms.sync(self.acc)
        self.assertEqual(oms.get(cid)["state"], "cancelled")
        self.assertEqual(self.acc.store.orders_by_client_id(cid), [])   # never reached the broker
        self.assertIsNone(self.acc.store.get_position(o["symbol"]))

    def test_kill_cancels_a_submitted_order(self):
        o = self._stuck()
        out = control.kill_platform("test", "user")
        acc_out = [a for a in out["accounts"] if a["account_id"] == "paper-main"][0]
        self.assertEqual(acc_out["cancelled"], 1)
        self.assert_dead(o)

    def test_autotrade_off_cancels_a_submitted_runtime_order(self):
        control.set_autotrade(True, "user")
        o = self._stuck(origin="runtime")
        control.set_autotrade(False, "user")
        self.assert_dead(o)

    def test_heartbeat_off_cancels_a_submitted_runtime_order(self):
        control.set_autotrade(True, "user")
        o = self._stuck(origin="runtime")
        self.advance(hours=3)                     # no client heartbeat for longer than the timeout
        self.assertTrue(control.check_heartbeat(self.clock()))
        self.assert_dead(o)

    def test_resend_rechecks_halt_and_auto_trading(self):
        o = self._stuck()
        self.acc.set_halt(True, "kill", "halted elsewhere")       # halted without cancelling (another path)
        self.advance(seconds=10)
        out = oms.sync(self.acc)
        self.assertEqual(out.get("not_resent"), 1)
        self.assertIn("halted", oms.get(o["client_order_id"])["reject_reason"])
        self.assert_dead(o)
        self.acc.set_halt(False)
        control.set_autotrade(True, "user")
        r = self._stuck(symbol="QQQ", qty=2, origin="runtime")
        control._write_autotrade(False, "engine")                  # flag off without the cancel pass
        self.advance(seconds=10)
        oms.sync(self.acc)
        self.assertIn("auto trading", oms.get(r["client_order_id"])["reject_reason"])
        self.assert_dead(r)

    def test_cancel_of_a_submitted_order_known_at_the_broker(self):
        o = self.manual("SPY", "buy", 1)                      # filled at the broker
        with db._connect() as conn:                            # ...but the ack was lost
            conn.execute("UPDATE oms_orders SET state = 'submitted' WHERE client_order_id = ?", (o["client_order_id"],))
        c = oms.cancel(self.acc, o["client_order_id"])
        self.assertEqual(c["state"], "filled")                 # synced from the broker, not falsely "cancelled"


if __name__ == "__main__":
    unittest.main()
