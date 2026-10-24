"""Stage 8b binance-demo through the broker framework (SPEC §18.14) — the REAL adapter on the fake ccxt exchange,
temp DBs, offline: Test connection (read-only, balances, USDT), the first connect with confirmed starting balances
(no flat rule; 400 / 409 balances_changed / 409 platform_orders_open), positions as baseline deltas, a buy booked
net of the base-asset fee and reconciled exactly, long only (pre-trade block + visible event, routing 409
long_only, incubation eligibility), the prefix-limited kill (foreign orders untouched, dust skipped), the global
kill, and accept_broker re-basing a change made in the demo UI (never a short in the mirror)."""
import asyncio
import unittest
from unittest import mock

try:
    from sp_broker_testkit import BrokerCase, make_strategy
except ImportError:  # pragma: no cover
    from tests.sp_broker_testkit import BrokerCase, make_strategy

from strategy_platform.api import routes
from strategy_platform.execution import accounts, control, oms
from strategy_platform.execution.brokers import mirror, reconcile, service
from strategy_platform.execution.errors import TradingError

BID = "binance-demo"
START = {"USDT": 5000.0, "BTC": 0.05, "ETH": 1.0, "BNB": 2.0}


def call(name, query=None, body=routes.NO_BODY, match=None):
    status, payload = asyncio.run(getattr(routes.V2Api(), name)(query or {}, body, match or {}))
    routes.dumps(payload)
    return status, payload


class BinanceFlowTest(BrokerCase):
    def bconnect(self, balances=None):
        out = service.connect(BID, service.confirm_phrase(BID), "connect the demo", "user",
                              dict(START if balances is None else balances))
        return accounts.get(BID), out

    def test_test_connection_is_read_only(self):
        t = service.test(BID)
        self.assertTrue(t["ok"], t["checks"])
        names = [c["name"] for c in t["checks"]]
        self.assertEqual(names, ["SDK installed", "Keys present", "Paper/demo endpoint", "Server time",
                                 "Markets loaded (exchangeInfo)", "Account readable", "Positions readable",
                                 "Open orders readable"])
        self.assertEqual((t["currency"], t["currency_note"], t["cash_basis"]), ("USDT", "USDT — not converted",
                                                                               5000.0))
        self.assertEqual({b["asset"]: b["total"] for b in t["balances"]}, START)
        self.assertEqual((t["platform_open_orders"], t["foreign_open_orders"], t["positions"]), (0, 0, []))
        self.assertNotIn("order", [p for p in self.bstate.calls if p == "order"])       # nothing sent / looked up
        self.assertEqual({m for m, _u in self.bstate.sent}, {"GET"})
        self.assertEqual(t["endpoint"], "https://demo-api.binance.com")

    def test_first_connect_confirms_the_baseline(self):
        st, p = call("broker_connect", match={"id": BID}, body={"confirm": "CONNECT BINANCE-DEMO", "reason": "go now"})
        self.assertEqual(st, 400)                                                 # starting_balances missing
        self.assertIn("starting_balances", p["errors"]["_"])
        st, p = call("broker_connect", match={"id": BID}, body={"confirm": "CONNECT BINANCE-DEMO", "reason": "go now",
                                                                "starting_balances": {"usdt": 1}})
        self.assertEqual((st, set(p["errors"])), (400, {"starting_balances"}))
        st, p = call("broker_connect", match={"id": "alpaca-paper"},
                     body={"confirm": "CONNECT ALPACA-PAPER", "reason": "go now", "starting_balances": {"USDT": 1}})
        self.assertEqual(p["errors"]["starting_balances"], "only for binance-demo")
        st, p = call("broker_connect", match={"id": BID}, body={"confirm": "CONNECT BINANCE-DEMO", "reason": "go now",
                                                                "starting_balances": dict(START, BTC=0.04)})
        self.assertEqual((st, p["code"]), (409, "balances_changed"))
        self.assertIn("BTC 0.04 confirmed vs 0.05 now", p["error"])
        self.bstate.orders.clear()
        self.bstate._new("BTC/USDT", "limit", "buy", 0.001, 50000.0, "UFOS_man-000000000001")
        with self.assertRaises(TradingError) as cm:
            self.bconnect({"USDT": 5000.0, "BTC": 0.05, "ETH": 1.0, "BNB": 2.0})
        self.assertEqual(cm.exception.code, "platform_orders_open")
        o = next(iter(self.bstate.orders.values()))
        o["clientOrderId"] = "web_manual_1"                                       # a foreign order is fine
        acc, out = self.bconnect()
        self.assertEqual(out["reconciliation"]["status"], "ok", out["reconciliation"])
        self.assertEqual(mirror.baseline(acc.store), START)
        self.assertAlmostEqual(acc.store.cash_balance(), 5000.0)
        b = out["broker"]
        self.assertEqual((b["currency"], b["currency_note"], b["chip"]), ("USDT", "USDT — not converted",
                                                                           "DEMO · SIMULATED FUNDS"))
        self.assertFalse(b["capabilities"]["shorting"])

    def test_buy_booked_net_and_reconciled(self):
        acc, _ = self.bconnect()
        o = self.bmanual(acc, "BTC-USD", "buy", 0.001)
        self.assertEqual(o["state"], "filled", o)
        pos = acc.store.get_position("BTC-USD")
        self.assertAlmostEqual(pos["qty"], 0.000999)                              # net of the BTC commission
        self.assertAlmostEqual(acc.store.cash_balance(), 5000.0 - 60.0, places=6)
        res = reconcile.run(acc, "manual", user=True)
        self.assertEqual(res["status"], "ok", res["mismatches"])
        self.assertEqual(res["cash"]["currency"], "USDT")
        bp = {p["symbol"]: p["qty"] for p in self.binance.positions()}
        self.assertAlmostEqual(bp["BTC-USD"], 0.000999)

    def test_short_and_oversell_blocked_with_event(self):
        acc, _ = self.bconnect()
        o = self.bmanual(acc, "BTC-USD", "short", 0.001)
        self.assertEqual(o["state"], "rejected")
        self.assertIn("long only", o["reject_reason"])
        o = self.bmanual(acc, "ETH-USD", "sell", 0.5)                             # the pre-funded ETH is not ours
        self.assertEqual(o["state"], "rejected")
        self.assertIn("long only", o["reject_reason"])
        ev = [e for e in acc.store.all_events() if e["title"] == "Order blocked: short not possible on binance-demo"]
        self.assertEqual([e["symbol"] for e in ev], ["BTC-USD", "ETH-USD"])
        self.assertEqual([x for x in self.bstate.orders.values()], [])           # nothing reached the broker
        o = self.bmanual(acc, "SPY", "buy", 1)
        self.assertIn("stocks/ETFs are traded on alpaca-paper", o["reject_reason"])

    def test_routing_long_only(self):
        acc, _ = self.bconnect()
        sid = make_strategy(symbols=("BTC-USD", "ETH-USD"), state="validated", name="crypto long")
        out = service.set_route(sid, BID, service.route_phrase(BID), "to the demo")
        self.assertEqual(out["route"]["account_id"], BID)
        self.assertFalse(out["route"]["blocked"])
        sid2 = make_strategy(symbols=("BTC-USD",), state="validated", name="crypto ls")
        with mock.patch.object(service, "strategy_mode", return_value="long_short"):
            with self.assertRaises(TradingError) as cm:
                service.set_route(sid2, BID, service.route_phrase(BID), "to the demo")
            self.assertEqual(cm.exception.code, "long_only")
            e = service._route_entry(sid2)
            self.assertEqual((e["allowed_accounts"], e["refused_accounts"][0]["code"]), (["paper-main"], "long_only"))
            e = service._route_entry(sid)                                         # routed earlier: shown blocked
            self.assertTrue(e["blocked"])
            self.assertIn("long only", e["blocked_reason"])
            from strategy_platform.live import incubation
            el = incubation.eligibility(sid, accounts.now(), BID)
            self.assertEqual((el["eligible"], el["code"]), (False, "long_only"))
        st, p = call("brokers_routing_post", body={"strategy_id": sid, "account_id": "paper-main",
                                             "confirm": "ROUTE TO PAPER-MAIN", "reason": "back"})
        self.assertEqual(st, 200, p)

    def test_kill_prefix_limited_with_dust(self):
        acc, _ = self.bconnect()
        self.bmanual(acc, "BTC-USD", "buy", 0.001)
        self.bstate.add_foreign()
        lim = self.bmanual(acc, "ETH-USD", "buy", 0.01, type="limit", limit_price=2000.0, tif="gtc")
        self.assertEqual(lim["state"], "accepted", lim)
        out = service.kill(acc, "test kill")
        self.assertTrue(out["flat"], out)
        self.assertEqual(out["untouched_orders"], 1)
        self.assertIn("UFOS_", out["untouched_note"])
        self.assertEqual([s["symbol"] for s in out["skipped"]], ["BTC-USD"])     # 0.000009 BTC: dust
        self.assertIn("dust", out["skipped"][0]["reason"])
        st = {o["clientOrderId"]: o["status"] for o in self.bstate.orders.values()}
        self.assertEqual(st["web_abc123"], "open")
        self.assertEqual(oms.get(lim["client_order_id"])["state"], "cancelled")
        self.assertTrue(all(o["clientOrderId"].startswith("UFOS_") for o in self.bstate.orders.values()
                            if o["side"] == "sell"))
        self.assertEqual(reconcile.run(acc, "manual", user=True)["status"], "ok")

    def test_global_kill_includes_binance(self):
        acc, _ = self.bconnect()
        self.bmanual(acc, "BTC-USD", "buy", 0.002)
        out = control.kill_platform("global test")
        self.assertTrue(out["ok"], out)
        self.assertIn(BID, [a["account_id"] for a in out["accounts"]])
        self.assertTrue(acc.halt()["active"])

    def test_ui_change_mismatch_then_accept_rebases(self):
        acc, _ = self.bconnect()
        self.bstate.bal("ETH")[0] -= 0.5                                          # sold in the demo UI
        self.bstate.bal("USDT")[0] += 1500.0
        res = reconcile.run(acc, "manual", user=True)
        self.assertEqual(res["status"], "mismatch")
        kinds = sorted(m["kind"] for m in res["mismatches"])
        self.assertEqual(kinds, ["cash", "position"])
        self.assertIn("outside the platform", next(m for m in res["mismatches"] if m["kind"] == "position")["message"])
        self.assertIn("USDT — not converted", next(m for m in res["mismatches"] if m["kind"] == "cash")["message"])
        self.assertTrue(acc.halt()["active"])
        out = reconcile.resolve(acc, "accept_broker", "sold in the UI", confirm=reconcile.ACCEPT_CONFIRM)
        self.assertEqual(out["reconciliation"]["status"], "ok", out)
        self.assertAlmostEqual(mirror.baseline(acc.store)["ETH"], 0.5)
        self.assertIsNone(acc.store.get_position("ETH-USD"))
        self.assertAlmostEqual(acc.store.cash_balance(), 6500.0)

    def test_storage_v23_br_accounts(self):
        import sqlite3
        from strategy_platform.storage import db
        self.assertGreaterEqual(db.SCHEMA_VERSION, 23)
        self.bconnect()
        with db._connect() as conn:
            ids = [r["id"] for r in conn.execute("SELECT id FROM br_accounts").fetchall()]
            self.assertEqual(ids, [BID])
            with self.assertRaises(sqlite3.DatabaseError):          # still insert-only (triggers recreated)
                conn.execute("UPDATE br_accounts SET label = 'x' WHERE id = ?", (BID,))
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("DELETE FROM br_accounts")
            with self.assertRaises(sqlite3.IntegrityError):         # the CHECK still names the known accounts only
                conn.execute("INSERT INTO br_accounts (id, broker, label, params_json, created_at, created_by) VALUES"
                             " ('binance-live', 'binance_demo', 'x', '{}', 'now', 'user')")


class MigrationV23Test(unittest.TestCase):
    def test_rows_copied_from_v22(self):
        import sqlite3
        from strategy_platform.storage import db
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.row_factory = sqlite3.Row
        for version, statements in db.MIGRATIONS:          # the schema as v22 left it
            if version > 22:
                break
            for sql in statements:
                sql(conn) if callable(sql) else conn.execute(sql)
            conn.execute(f"PRAGMA user_version = {version}")
            conn.commit()
        conn.execute("INSERT INTO br_accounts (id, broker, label, params_json, created_at, created_by) VALUES"
                     " ('kraken-demo', 'kraken_demo', 'Kraken', '{\"a\": 1}', '2026-10-18', 'user')")
        conn.commit()
        self.assertEqual(db.migrate(conn), db.SCHEMA_VERSION)
        rows = [dict(r) for r in conn.execute("SELECT * FROM br_accounts").fetchall()]
        self.assertEqual(rows, [{"id": "kraken-demo", "broker": "kraken_demo", "label": "Kraken",
                                 "params_json": '{"a": 1}', "created_at": "2026-10-18", "created_by": "user"}])
        conn.execute("INSERT INTO br_accounts (id, broker, label, params_json, created_at, created_by) VALUES"
                     " ('binance-demo', 'binance_demo', 'B', '{}', 'now', 'user')")
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("UPDATE br_accounts SET label = 'x'")
        conn.close()


if __name__ == "__main__":
    unittest.main()
