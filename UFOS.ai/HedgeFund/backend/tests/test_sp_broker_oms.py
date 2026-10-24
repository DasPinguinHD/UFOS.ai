"""Stage 8 OMS on broker accounts (SPEC §18.1, §18.4, §18.6): connect flow, order lifecycle with partial fills and
exact delta booking into the mirror, rejects, reconnect without duplicate orders (lookup before resend), link
backoff ("waiting for broker (attempt n)"), async cancel, bracket legs, venue and size rules. Fakes, temp DBs."""
import math
import unittest

try:
    from sp_broker_testkit import BrokerCase
except ImportError:  # pragma: no cover
    from tests.sp_broker_testkit import BrokerCase

from strategy_platform.execution import accounts, oms
from strategy_platform.execution.brokers import mirror, reconcile, service
from strategy_platform.execution.errors import BrokerUnavailable, TradingError
from strategy_platform.portfolio import ledger
from strategy_platform.storage import db


def invariant(test, acc):
    rows = ledger.entries(acc.id)
    test.assertAlmostEqual(math.fsum(r["amount"] for r in rows), acc.store.cash_balance(), places=6)
    held = {p["symbol"]: p["qty"] for p in acc.store.get_positions()}
    sums = {}
    for r in rows:
        if r["symbol"]:
            sums[r["symbol"]] = sums.get(r["symbol"], 0.0) + r["qty"]
    for s, q in held.items():
        test.assertAlmostEqual(sums.get(s, 0.0), q, places=7)


class ConnectTest(BrokerCase):
    def test_connect_flow(self):
        # fix1023: a wrong / missing confirm phrase is no longer refused (accepted and ignored)
        self.alpaca.pos["SPY"] = [3.0, 400.0]
        with self.assertRaises(TradingError) as cm:
            service.connect("alpaca-paper", "CONNECT ALPACA-PAPER", "first connect")
        self.assertEqual(cm.exception.code, "not_flat")
        self.alpaca.pos.clear()
        self.alpaca.cash = 500.0
        with self.assertRaises(TradingError) as cm:
            service.connect("alpaca-paper", "CONNECT ALPACA-PAPER", "first connect")
        self.assertEqual(cm.exception.code, "too_small")
        self.alpaca.cash = 100000.0
        acc, out = self.connect()
        self.assertEqual(out["reconciliation"]["status"], "ok")
        self.assertEqual(acc.store.cash_balance(), 100000.0)
        self.assertEqual(acc.broker.account({}).equity, 100000.0)
        self.assertIn("alpaca-paper", [a.id for a in accounts.all_accounts()])
        st = out["broker"]
        self.assertEqual(st["connection"]["status"], "connected")
        self.assertEqual(st["endpoint"], "https://paper-api.alpaca.markets")
        self.assertFalse(st["live"])
        with self.assertRaises(TradingError) as cm:
            self.connect()
        self.assertEqual(cm.exception.code, "already_connected")
        with db._connect() as c:
            acts = [r["action"] for r in c.execute("SELECT action FROM audit").fetchall()]
        self.assertIn("broker_connect", acts)
        self.assertIn("broker_test", acts)

    def test_missing_keys_and_sdk(self):          # S10-K: on alpaca-paper (was the retired kraken-demo)
        self.store.values.clear()
        t = service.test("alpaca-paper")
        self.assertFalse(t["ok"])
        keys = next(c for c in t["checks"] if c["name"] == "Keys present")
        self.assertIn("Alpaca paper API key ID", keys["message"])
        self.assertTrue(all(c["message"].startswith("Skipped") for c in t["checks"][2:]))
        self.alpaca.installed = False
        t = service.test("alpaca-paper")
        self.assertIn("not installed", t["checks"][0]["message"])

    def test_disconnect_rules(self):
        acc, _ = self.connect()
        self.bmanual(acc, "SPY", "buy", 2)
        with self.assertRaises(TradingError) as cm:
            service.disconnect("alpaca-paper", "done testing")
        self.assertEqual(cm.exception.code, "not_flat")
        self.bmanual(acc, "SPY", "sell", 2)
        out = service.disconnect("alpaca-paper", "done testing")
        self.assertEqual(out["broker"]["connection"]["status"], "disconnected")
        self.assertNotIn("alpaca-paper", [a.id for a in accounts.all_accounts()])
        acc2, out = self.connect()          # reconnect: the mirror continues, reconciliation ok
        self.assertEqual(out["reconciliation"]["status"], "ok")


class LifecycleTest(BrokerCase):
    def test_fill_books_mirror_and_subledger(self):
        acc, _ = self.connect()
        o = self.bmanual(acc, "SPY", "buy", 10)
        self.assertEqual(o["state"], "filled")
        self.assertEqual(self.alpaca.sends, 1)
        p = acc.store.get_position("SPY")
        self.assertEqual((p["qty"], p["avg_price"]), (10.0, 500.0))
        self.assertAlmostEqual(acc.store.cash_balance(), 95000.0)
        self.assertEqual(ledger.holdings(acc.id), {"_unallocated": {"SPY": 10.0}})
        invariant(self, acc)
        self.assertEqual(reconcile.run(acc, "manual")["status"], "ok")

    def test_partial_fills_exact_delta_price(self):
        acc, _ = self.connect()
        self.alpaca.auto = "none"
        o = self.bmanual(acc, "SPY", "buy", 10)
        self.assertEqual(o["state"], "accepted")
        self.alpaca.fill(o["client_order_id"], qty=4, price=500.0)
        oms.sync(acc)
        o = oms.get(o["client_order_id"])
        self.assertEqual((o["state"], o["filled_qty"]), ("partially_filled", 4.0))
        self.alpaca.fill(o["client_order_id"], qty=6, price=510.0)
        oms.sync(acc)
        oms.sync(acc)                     # idempotent: nothing booked twice
        o = oms.get(o["client_order_id"])
        self.assertEqual(o["state"], "filled")
        rows = [r for r in acc.store.all_orders()]
        self.assertEqual([(r["qty"], round(r["fill_price"], 6)) for r in rows], [(4.0, 500.0), (6.0, 510.0)])
        self.assertEqual(rows[1]["client_order_id"], o["client_order_id"] + "~2")
        p = acc.store.get_position("SPY")
        self.assertAlmostEqual(p["avg_price"], 506.0)
        ledger.attribute(acc)
        self.assertEqual(ledger.holdings(acc.id), {"_unallocated": {"SPY": 10.0}})
        invariant(self, acc)
        self.assertAlmostEqual(acc.store.cash_balance(), self.alpaca.cash)

    def test_partial_then_cancel(self):
        acc, _ = self.connect()
        self.alpaca.auto = "partial"
        o = self.bmanual(acc, "SPY", "buy", 10)
        self.assertEqual((o["state"], o["filled_qty"]), ("partially_filled", 5.0))
        o = oms.cancel(acc, o["client_order_id"])
        self.assertEqual(o["state"], "cancelled")
        self.assertEqual(o["filled_qty"], 5.0)
        self.assertEqual(acc.store.get_position("SPY")["qty"], 5.0)
        self.assertEqual(reconcile.run(acc, "manual")["status"], "ok")

    def test_reject_by_broker(self):
        acc, _ = self.connect()
        self.alpaca.reject_next = "Alpaca refused: pattern day trader protection (equity below $25,000) — x"
        o = self.bmanual(acc, "SPY", "buy", 1)
        self.assertEqual(o["state"], "rejected")
        self.assertIn("pattern day trader", o["reject_reason"])
        self.assertEqual(acc.store.get_positions(), [])

    def test_reconnect_without_duplicates(self):
        acc, _ = self.connect()
        self.alpaca.drop_after_send = 1          # the order reaches the broker, the answer is lost
        o = self.bmanual(acc, "SPY", "buy", 3)
        self.assertEqual(o["state"], "submitted")                # unknown outcome: kept, never re-sent blindly
        self.assertEqual(acc.link.view()["state"], "waiting")
        n_calls = len(self.alpaca.calls)
        with self.assertRaises(BrokerUnavailable):
            oms.sync(acc)                                        # still backing off: no broker call at all
        self.assertEqual(len(self.alpaca.calls), n_calls)
        self.assertEqual(self.alpaca.sends, 1)
        self.advance(seconds=20)
        oms.sync(acc)                                            # lookup by client id finds it -> synced
        self.assertEqual(oms.get(o["client_order_id"])["state"], "filled")
        self.assertEqual(self.alpaca.sends, 1)                   # exactly one order at the broker
        self.assertEqual(acc.store.get_position("SPY")["qty"], 3.0)
        self.assertEqual(acc.link.view()["state"], "ok")

    def test_broker_down_backoff_then_one_send(self):
        acc, _ = self.connect()
        self.alpaca.down = True
        o = self.bmanual(acc, "SPY", "buy", 2)
        self.assertEqual(o["state"], "submitted")                # stored, the send failed: retried later
        lk = acc.link.view()
        self.assertEqual((lk["state"], lk["attempt"]), ("waiting", 1))
        self.assertIn("Waiting for broker (attempt 1)", lk["message"])
        o2 = self.bmanual(acc, "QQQ", "buy", 1)                  # while waiting: new orders refused (broker_link)
        self.assertEqual(o2["state"], "rejected")
        self.assertEqual(next(c for c in o2["checks"] if not c["ok"])["check"], "broker_link")
        self.assertIn("Waiting for broker", o2["reject_reason"])
        self.advance(seconds=20)
        with self.assertRaises(Exception):
            oms.sync(acc)                                        # still down: attempt 2, longer backoff
        self.assertEqual(acc.link.view()["attempt"], 2)
        self.assertEqual(self.alpaca.sends, 0)
        self.alpaca.down = False
        self.advance(seconds=40)
        oms.sync(acc)                                            # lookup: unknown + young -> ONE send, same id
        self.assertEqual(oms.get(o["client_order_id"])["state"], "filled")
        self.assertEqual(self.alpaca.sends, 1)
        oms.sync(acc)
        self.assertEqual(self.alpaca.sends, 1)

    def test_submitted_resend_after_restart(self):
        acc, _ = self.connect()
        self.alpaca.down = False
        # simulate a crash between "submitted" and the broker answer: the order sits in submitted
        self.alpaca.auto = "none"
        real_submit = self.alpaca.submit
        calls = {"n": 0}

        def flaky(order):
            calls["n"] += 1
            raise mirror.unavailable("lost (fake)", TimeoutError("x"), connection=True)
        self.alpaca.submit = flaky
        o = self.bmanual(acc, "SPY", "buy", 2)
        self.assertEqual(o["state"], "submitted")
        self.alpaca.submit = real_submit
        acc.link.ok()
        oms.sync(acc)                                # unknown at the broker + young -> re-sent with the SAME id
        o2 = oms.get(o["client_order_id"])
        self.assertEqual(o2["state"], "accepted")
        self.assertEqual(self.alpaca.sends, 1)
        oms.sync(acc)
        self.assertEqual(self.alpaca.sends, 1)       # never twice

    def test_async_cancel_and_late_fill(self):
        acc, _ = self.connect()
        self.alpaca.auto = "none"
        o = self.bmanual(acc, "SPY", "buy", 4, type="limit", limit_price=490.0)
        self.assertEqual((o["state"], o["order_type"], o["limit_price"]), ("accepted", "limit", 490.0))
        self.alpaca.fill(o["client_order_id"], qty=4, price=490.0)       # fills just before the cancel arrives
        o = oms.cancel(acc, o["client_order_id"])
        self.assertEqual(o["state"], "filled")
        self.assertEqual(acc.store.get_position("SPY")["qty"], 4.0)

    def test_bracket_legs_imported_and_booked(self):
        acc, _ = self.connect()
        o = self.bmanual(acc, "SPY", "buy", 5, type="bracket", take_profit={"limit_price": 550.0},
                         stop_loss={"stop_price": 450.0})
        self.assertEqual(o["state"], "filled")
        self.assertEqual(o["legs"], [o["client_order_id"] + ".tp", o["client_order_id"] + ".sl"])
        tp = oms.get(o["client_order_id"] + ".tp")
        self.assertEqual((tp["state"], tp["parent_client_order_id"], tp["side"]), ("accepted", o["client_order_id"],
                                                                                   "sell"))
        self.assertEqual(reconcile.run(acc, "manual")["status"], "ok")      # legs are known open orders
        tp_id = tp["broker_order_id"]
        self.alpaca.fill(tp_id, price=550.0)
        self.alpaca.orders[oms.get(o["client_order_id"] + ".sl")["broker_order_id"]]["status"] = "cancelled"
        oms.sync(acc)
        self.assertEqual(oms.get(o["client_order_id"] + ".tp")["state"], "filled")
        self.assertEqual(oms.get(o["client_order_id"] + ".sl")["state"], "cancelled")
        self.assertEqual(acc.store.get_positions(), [])
        ledger.attribute(acc)
        invariant(self, acc)
        self.assertEqual(reconcile.run(acc, "manual")["status"], "ok")

    def test_internal_paper_market_only(self):
        with self.assertRaises(ValueError):
            oms.place(self.acc, {"symbol": "SPY", "side": "buy", "qty": 1, "origin": "manual", "type": "limit",
                                 "limit_price": 1.0})


class VenueTest(BrokerCase):
    def test_venue_and_side_rules(self):
        acc, _ = self.connect()
        o = self.bmanual(acc, "BTC-USD", "buy", 0.01)
        self.assertEqual(o["state"], "rejected")
        self.assertIn("binance-demo", o["reject_reason"])          # the crypto venue since 2026-10-19 (§18.14)
        o = self.bmanual(acc, "SPY", "sell", 1)
        self.assertEqual(o["state"], "rejected")
        self.assertIn("exceeds the long position", o["reject_reason"])



if __name__ == "__main__":
    unittest.main()
