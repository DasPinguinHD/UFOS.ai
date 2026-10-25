"""fund/store.py + schema v3 (newsroom/db.py): v2 -> v3 migration on an
existing database, every store function, meta state helpers, fund retention
and the disabled-store no-op behaviour. Stdlib unittest; temp databases only.

Run from HedgeFund/backend:  python -m unittest tests.test_fund_store -v
"""
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone

from fund import store
from strategy_platform.core.types import AgentSignal, Bar
from newsroom import db


class FundStoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "newsroom.db")
        self.assertTrue(db.init(self.path, legacy=""))

    def tearDown(self):
        db.close()
        self._tmp.cleanup()

    def raw(self, sql, params=()):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()


class MigrationV3Test(FundStoreCase):
    def test_v2_database_is_upgraded_in_place(self):
        db.close()
        v2 = os.path.join(self._tmp.name, "v2.db")
        conn = sqlite3.connect(v2)
        for version, statements in db.MIGRATIONS[:2]:
            for sql in statements:
                conn.execute(sql)
        conn.execute("PRAGMA user_version = 2")
        conn.execute("INSERT INTO orders(ts, symbol, side, qty, order_type, status) "
                     "VALUES ('2026-09-25T12:00:00+00:00', 'SPY', 'buy', 3, 'market', 'filled')")
        conn.execute("INSERT INTO positions(symbol, qty, avg_price, updated_at) VALUES ('SPY', 3, 600, 'x')")
        conn.execute("INSERT INTO equity_curve(ts, equity, cash) VALUES ('2026-09-25T12:00:00+00:00', 1, 1)")
        conn.commit()
        conn.close()
        self.assertTrue(db.init(v2, legacy=""))
        self.assertEqual(db.storage_info()["schema_version"], db.SCHEMA_VERSION)   # v2 -> current (v4)
        conn = sqlite3.connect(v2)
        try:
            cols = {t: {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
                    for t in ("orders", "positions", "equity_curve")}
            self.assertTrue({"run_id", "recommendation_id", "origin", "fill_price", "fees", "filled_at",
                             "reject_reason", "notional"} <= cols["orders"])
            self.assertTrue({"opened_at", "realized_pnl", "borrow_rate_pct", "last_price"} <= cols["positions"])
            self.assertTrue({"long_mv", "short_mv", "benchmark", "drawdown_pct"} <= cols["equity_curve"])
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue(set(db.TABLES) <= tables)
            # old rows kept; realized_pnl default applies to existing rows
            self.assertEqual(conn.execute("SELECT qty, realized_pnl FROM positions").fetchone(), (3.0, 0.0))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 1)
            # re-running the v3 steps (user_version forced back) must not fail on ADD COLUMN
            conn.execute("PRAGMA user_version = 2")
            conn.commit()
            self.assertEqual(db.migrate(conn), db.SCHEMA_VERSION)       # v3 + v4 steps re-run cleanly
        finally:
            conn.close()
        self.assertEqual(store.get_positions()[0]["symbol"], "SPY")

    def test_paper_mode_check_still_enforced(self):
        oid = store.insert_order("2026-09-28T14:00:00+00:00", "SPY", "buy", 1, "filled", "manual")
        self.assertEqual(self.raw("SELECT mode, broker, order_type FROM orders WHERE id=?", (oid,))[0],
                         ("paper", "paper", "market"))


class ParamsRunsTest(FundStoreCase):
    def test_params_versions(self):
        self.assertIsNone(store.latest_params())
        self.assertEqual(store.effective_params()[0], 0)
        v1 = store.save_params({"fees_bps": 3.0}, "user", "first")
        v2 = store.save_params({"fees_bps": 4.0}, "user")
        self.assertEqual((v1, v2), (1, 2))
        latest = store.latest_params()
        self.assertEqual((latest["version"], latest["params"], latest["actor"]), (2, {"fees_bps": 4.0}, "user"))
        self.assertEqual(store.get_params(1)["note"], "first")
        self.assertIsNone(store.get_params(9))
        version, full = store.effective_params()
        self.assertEqual((version, full["fees_bps"], full["min_confidence"]), (2, 4.0, 75))

    def test_runs_scores_recs_orders(self):
        pv = store.save_params({}, "system")
        rid = store.create_run("2026-09-28T14:00:00+00:00", pv, "startup", True)
        self.assertEqual(store.last_run()["status"], "running")
        sig = AgentSignal(agent="technical", symbol="SPY", score=0.4, confidence=0.7, summary="up",
                          details={"sma50": 1.0})
        self.assertEqual(store.save_agent_scores(rid, [sig, {"agent": "macro", "symbol": "SPY", "score": -0.1,
                                                             "confidence": 0.5, "summary": "m", "source": "rules"}]), 2)
        store.save_agent_scores(rid, [sig])        # replace, no duplicate
        scores = store.agent_scores(rid)
        self.assertEqual([s["agent"] for s in scores], ["macro", "technical"])
        self.assertEqual(scores[1]["details"], {"sma50": 1.0})
        self.assertEqual(len(store.agent_scores(rid, "QQQ")), 0)

        r1 = store.save_recommendation(rid, "2026-09-28T14:00:01+00:00", "SPY", "LONG", 0.8, 0.04, 0.3, "why",
                                       "llm", "m1", {"ai": True}, {"cost_usd": 0.001})
        r2 = store.save_recommendation(rid, "2026-09-28T14:05:00+00:00", "SPY", "LONG", 0.8, 0.04, source="carried")
        r3 = store.save_recommendation(rid, "2026-09-28T14:05:00+00:00", "QQQ", "FLAT", 0.2, 0.0)
        self.assertEqual(store.get_recommendation(r1)["status"], "superseded")
        self.assertEqual(store.get_recommendation(r1)["provenance"], {"ai": True})
        self.assertEqual(store.last_judged_recommendation("SPY")["id"], r1)       # carried is skipped
        latest = store.latest_recommendations()
        self.assertEqual((latest["SPY"]["id"], latest["QQQ"]["id"]), (r2, r3))
        oid = store.insert_order("2026-09-28T14:05:02+00:00", "SPY", "buy", 5, "filled", "autopilot",
                                 run_id=rid, recommendation_id=r2, fill_price=600.3, fees=1.5, notional=3001.5,
                                 filled_at="2026-09-28T14:05:02+00:00")
        self.assertTrue(store.update_recommendation_status(r2, "executed", order_id=oid))
        self.assertEqual(store.get_recommendation(r2)["order_id"], oid)
        self.assertTrue(store.finish_run(rid, "ok", "2026-09-28T14:00:30+00:00", "mixed", ["m1", "m2"], 0.0021,
                                         summary={"flips": ["SPY"]}))
        runs = store.list_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual((runs[0]["recommendations"], runs[0]["orders"], runs[0]["flips"], runs[0]["models"]),
                         (3, 1, ["SPY"], "m1, m2"))
        detail = store.get_run(rid)
        self.assertEqual(detail["params"]["version"], pv)
        self.assertEqual(len(detail["agents"]), 2)
        self.assertEqual(len(detail["recommendations"]), 3)
        self.assertEqual(detail["orders"][0]["id"], oid)
        self.assertIsNone(store.get_run(999))

    def test_orders(self):
        a = store.insert_order("2026-09-28T13:00:00+00:00", "SPY", "buy", 2, "pending", "autopilot", notional=1200)
        b = store.insert_order("2026-09-29T02:00:00+00:00", "SPY", "sell", 1, "pending", "autopilot")  # NY Sep 28 22:00
        c = store.insert_order("2026-09-29T14:00:00+00:00", "QQQ", "short", 1, "filled", "manual")
        store.insert_order("2026-09-28T15:00:00+00:00", "QQQ", "short", 1, "rejected", "autopilot",
                           reject_reason="x")
        self.assertEqual([o["id"] for o in store.pending_orders()], [a, b])
        self.assertEqual([o["id"] for o in store.pending_orders("QQQ")], [])
        # a: Mon 09:00 EDT (before the open) -> Monday's session; b: Mon 22:00 EDT -> Tuesday's session
        self.assertEqual([o["id"] for o in store.orders_on_trading_day("2026-09-28", "autopilot")], [a])
        self.assertEqual([o["id"] for o in store.orders_on_trading_day("2026-09-29")], [b, c])
        self.assertEqual([o["id"] for o in store.orders_on_trading_day("2026-09-29", "manual")], [c])
        # queued on Saturday -> counts for Monday; filled at Monday's open -> still Monday
        w = store.insert_order("2026-10-03T15:00:00+00:00", "SPY", "buy", 1, "pending", "autopilot")
        self.assertEqual([o["id"] for o in store.orders_on_trading_day("2026-10-05", "autopilot")], [w])
        store.update_order(w, status="filled", filled_at="2026-10-05T13:30:00+00:00")
        self.assertEqual([o["id"] for o in store.orders_on_trading_day("2026-10-05")], [w])
        self.assertEqual(store.orders_on_trading_day("2026-10-02"), [])
        self.assertTrue(store.update_order(a, status="filled", fill_price=600.0, fees=0.6,
                                           filled_at="2026-09-28T13:30:00+00:00"))
        self.assertEqual(store.get_order(a)["status"], "filled")
        self.assertFalse(store.update_order(a, mode="live"))         # not updatable -> refused
        self.assertEqual(store.get_order(a)["status"], "filled")
        self.assertEqual([o["id"] for o in store.list_orders(3)], [w, c, b])


class LedgerPositionsEquityTest(FundStoreCase):
    def test_cash_positions_equity(self):
        self.assertEqual(store.cash_balance(), 0.0)
        store.add_cash_entry("2026-09-28T13:00:00+00:00", "deposit", 100000)
        store.add_cash_entry("2026-09-28T14:00:00+00:00", "fee", -1.25, "SPY")
        self.assertAlmostEqual(store.cash_balance(), 99998.75)
        self.assertEqual(store.cash_entries(kind="fee")[0]["symbol"], "SPY")
        store.upsert_position("SPY", -10, 600, "2026-09-28T14:00:00+00:00", "2026-09-28T14:00:00+00:00", -1.25, 0.3, 601)
        store.upsert_position("QQQ", 0, 500, "2026-09-28T14:00:00+00:00")
        self.assertEqual([p["symbol"] for p in store.get_positions()], ["SPY"])
        self.assertEqual(len(store.get_positions(include_flat=True)), 2)
        store.set_last_prices({"SPY": 605.5})
        self.assertEqual(store.get_position("SPY")["last_price"], 605.5)
        self.assertTrue(store.delete_position("QQQ"))
        self.assertEqual(store.clear_positions(), 1)
        store.add_equity_point("2026-09-27T20:00:00+00:00", 100000, 100000, 0, 0)
        store.add_equity_point("2026-09-28T20:00:00+00:00", 100100, 90000, 10100, 0, 650.0, 0.0)
        self.assertEqual(len(store.equity_points()), 2)
        self.assertEqual(store.equity_points(since="2026-09-28T00:00:00+00:00")[0]["benchmark"], 650.0)
        self.assertEqual(store.equity_point_before("2026-09-28T04:00:00+00:00")["equity"], 100000)
        self.assertIsNone(store.equity_point_before("2026-09-01T00:00:00+00:00"))

    def test_bars_cache(self):
        store.save_bars("SPY", [Bar("2026-09-24", 1, 2, 0.5, 1.5, 10), Bar("2026-09-25", 1.5, 2, 1, 1.8, 11)])
        store.save_bars("SPY", [Bar("2026-09-25", 1.5, 2, 1, 1.9, 11)])   # revision replaces
        bars = store.load_bars("SPY")
        self.assertEqual([(b.date, b.close) for b in bars], [("2026-09-24", 1.5), ("2026-09-25", 1.9)])
        self.assertEqual(len(store.load_bars("SPY", limit=1)), 1)
        self.assertIsNone(store.bars_fetched("SPY"))
        store.set_bars_fetched("SPY", "2026-09-25T20:00:00+00:00", 400)
        self.assertEqual(store.bars_fetched("SPY"), {"at": "2026-09-25T20:00:00+00:00", "days": 400})


class EventsAuditBacktestMetaTest(FundStoreCase):
    def test_events_audit_backtests(self):
        e1 = store.add_event("flip", "SPY LONG -> SHORT", "SPY", "body", ts="2026-09-28T14:00:00+00:00")
        e2 = store.add_event("halt", "Halted")
        self.assertEqual([e["id"] for e in store.events_after(0)], [e1, e2])
        self.assertEqual([e["id"] for e in store.events_after(e1)], [e2])
        store.add_audit("user", "params.update", {"fees_bps": 3})
        store.add_audit("engine", "halt", None)
        audit = store.list_audit()
        self.assertEqual([a["action"] for a in audit], ["halt", "params.update"])
        self.assertEqual(audit[1]["detail"], {"fees_bps": 3})
        bt = store.save_backtest({"symbols": ["SPY"]}, {"portfolio": {"cagr_pct": 4.2}, "equity": []})
        self.assertEqual(store.list_backtests()[0]["summary"], {"cagr_pct": 4.2})
        self.assertEqual(store.get_backtest(bt)["request"], {"symbols": ["SPY"]})

    def test_meta_state(self):
        self.assertFalse(store.get_autopilot())
        store.set_autopilot(True)
        self.assertTrue(store.get_autopilot())
        self.assertEqual(store.get_halted(), {"active": False, "reason": None, "at": None})
        store.set_halted(True, "kill switch", "2026-09-28T14:00:00+00:00")
        self.assertEqual(store.get_halted(), {"active": True, "reason": "kill switch", "at": "2026-09-28T14:00:00+00:00"})
        store.set_halted(False)
        self.assertFalse(store.get_halted()["active"])
        for getter, setter, value in ((store.get_peak_equity, store.set_peak_equity, 101234.5),
                                      (store.get_starting_capital, store.set_starting_capital, 100000.0)):
            self.assertIsNone(getter())
            setter(value)
            self.assertEqual(getter(), value)
        store.set_reset_at("2026-09-28T14:00:00Z")
        self.assertEqual(store.get_reset_at(), "2026-09-28T14:00:00+00:00")
        store.set_last_accrual_date("2026-09-28")
        store.set_last_dividend_date("2026-09-27")
        self.assertEqual((store.get_last_accrual_date(), store.get_last_dividend_date()), ("2026-09-28", "2026-09-27"))
        self.assertEqual(db.meta_get("fund.autopilot"), "1")

    def test_ai_spend_per_utc_day(self):
        d1 = datetime(2026, 9, 28, 23, 50, tzinfo=timezone.utc)
        d2 = datetime(2026, 9, 29, 0, 5, tzinfo=timezone.utc)
        self.assertEqual(store.ai_spend_today(d1), 0.0)
        store.add_ai_spend(0.01, d1)
        self.assertAlmostEqual(store.add_ai_spend(0.02, d1), 0.03)
        self.assertAlmostEqual(store.ai_spend_today(d1), 0.03)
        self.assertEqual(store.ai_spend_today(d2), 0.0)            # new UTC day
        self.assertAlmostEqual(store.add_ai_spend(0.005, d2), 0.005)
        store.add_ai_spend(-5, d2)                                  # negative ignored
        self.assertAlmostEqual(store.ai_spend_today(d2), 0.005)


class RetentionTest(FundStoreCase):
    def test_fund_runs_scores_events_pruned(self):
        """Old runs without recommendations/orders are pruned with their
        scores; runs behind kept recommendations or orders stay (decision
        inputs), whatever their age."""
        now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
        old_rec = store.create_run("2026-06-01T00:00:00+00:00", 0, "schedule", False)
        old_order = store.create_run("2026-06-02T00:00:00+00:00", 0, "schedule", False)
        old_empty = store.create_run("2026-06-03T00:00:00+00:00", 0, "schedule", False)
        new = store.create_run("2026-09-01T00:00:00+00:00", 0, "schedule", False)
        for rid in (old_rec, old_order, old_empty, new):
            store.save_agent_scores(rid, [{"agent": "technical", "symbol": "SPY", "score": 0, "confidence": 0,
                                           "summary": "", "source": "rules"}])
        for rid in (old_rec, new):
            store.save_recommendation(rid, "2026-06-01T00:00:00+00:00", "SPY", "FLAT", 0, 0)
        store.insert_order("2026-06-02T00:00:00+00:00", "SPY", "buy", 1, "rejected", "manual", run_id=old_order)
        store.add_event("order", "old", ts="2026-08-20T00:00:00+00:00")
        store.add_event("order", "new", ts="2026-09-20T00:00:00+00:00")
        res = db.prune(now=now)
        self.assertEqual((res["fund_runs"], res["fund_agent_scores"], res["fund_events"]), (1, 1, 1))
        self.assertEqual(sorted(r["id"] for r in store.list_runs()), sorted([old_rec, old_order, new]))
        self.assertEqual(len(store.agent_scores(old_empty)), 0)
        self.assertEqual(len(store.agent_scores(old_rec)), 1)      # decision inputs kept
        self.assertEqual(len(store.agent_scores(old_order)), 1)
        self.assertEqual(len(store.list_recommendations()), 2)     # recommendations are kept
        self.assertEqual([e["title"] for e in store.events_after(0)], ["new"])
        self.assertEqual(db.prune(now=now), {"insider": 0, "ai_results": 0, "calendar_events": 0, "prices": 0})


class _Broken:
    """Stands in for db._connect: every connection attempt fails (a
    transient SQLite error such as 'database is locked')."""

    def __call__(self):
        raise sqlite3.OperationalError("database is locked")


class FailClosedTest(FundStoreCase):
    """Review fixes: reads that feed money / budget / audit decisions return
    None on a SQLite error instead of a neutral-looking default."""

    def broken(self):
        saved = store._connect
        store._connect = _Broken()
        self.addCleanup(setattr, store, "_connect", saved)

    def test_cash_and_snapshot_none_on_error(self):
        store.add_cash_entry("2026-10-02T14:00:00+00:00", "deposit", 1000.0)
        store.upsert_position("SPY", 2, 500.0)
        snap = store.account_snapshot()
        self.assertEqual((snap["cash"], [p["symbol"] for p in snap["positions"]], snap["ledger_rows"]),
                         (1000.0, ["SPY"], 1))
        self.broken()
        self.assertIsNone(store.cash_balance())
        self.assertIsNone(store.account_snapshot())

    def test_ai_spend_fails_closed(self):
        d = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
        self.assertEqual(store.ai_spend_today(d), 0.0)             # nothing booked = 0, known
        self.assertAlmostEqual(store.add_ai_spend(0.5, d), 0.5)
        db.meta_set(store.META_AI_SPEND, "garbage")
        self.assertIsNone(store.ai_spend_today(d))                  # unreadable value = unknown
        self.broken()
        self.assertIsNone(store.ai_spend_today(d))
        self.assertIsNone(store.add_ai_spend(0.1, d))

    def test_add_audit_returns_id_or_none(self):
        a = store.add_audit("user", "kill", {"reason": "test"})
        b = store.add_audit("user", "resume")
        self.assertIsInstance(a, int)
        self.assertEqual(b, a + 1)
        self.broken()
        self.assertIsNone(store.add_audit("user", "kill"))


class RecommendationSupersedeTest(FundStoreCase):
    def test_open_and_skipped_are_superseded(self):
        run = store.create_run("2026-10-02T14:00:00+00:00", 0, "manual", True)
        a = store.save_recommendation(run, "2026-10-02T14:00:00+00:00", "SPY", "LONG", 0.8, 0.05)
        store.update_recommendation_status(a, "skipped", "min hold")
        b = store.save_recommendation(run, "2026-10-02T14:00:00+00:00", "QQQ", "LONG", 0.8, 0.05)
        store.update_recommendation_status(b, "executed", None, 7)
        c = store.save_recommendation(run, "2026-10-02T14:15:00+00:00", "SPY", "SHORT", 0.7, -0.05)
        d = store.save_recommendation(run, "2026-10-02T14:15:00+00:00", "QQQ", "FLAT", 0.2, 0.0)
        status = {r["id"]: r["status"] for r in store.list_recommendations()}
        self.assertEqual(status, {a: "superseded", b: "executed", c: "open", d: "open"})
        self.assertEqual(store.latest_recommendation_id("SPY"), c)
        self.assertIsNone(store.latest_recommendation_id("IWM"))


class DividendLedgerHelpersTest(FundStoreCase):
    def test_booked_keys_and_net_fills(self):
        store.add_cash_entry("2026-10-05T14:00:00+00:00", "dividend", 15.0, symbol="SPY",
                             note="ex 2026-10-05: 1.5/share x 10")
        store.add_cash_entry("2026-10-05T14:00:00+00:00", "dividend_short", -2.0, symbol="XLF",
                             note="ex 2026-09-20: 0.2/share x -10")
        store.add_cash_entry("2026-10-05T14:00:00+00:00", "fee", -1.0, symbol="SPY", note="ex 2026-10-05: no")
        self.assertEqual(store.booked_dividend_keys(), {("SPY", "2026-10-05"), ("XLF", "2026-09-20")})
        self.assertEqual(store.booked_dividend_keys("2026-10-01"), {("SPY", "2026-10-05")})
        store.insert_order("2026-10-02T14:00:00+00:00", "SPY", "buy", 10, "filled", "manual",
                           filled_at="2026-10-02T14:00:00+00:00")
        store.insert_order("2026-10-05T14:00:00+00:00", "SPY", "sell", 4, "filled", "manual",
                           filled_at="2026-10-05T14:00:00+00:00")
        store.insert_order("2026-10-05T15:00:00+00:00", "SPY", "buy", 9, "rejected", "manual")
        self.assertEqual(store.net_filled_qty_since("SPY", "2026-10-05T04:00:00+00:00"), -4.0)
        self.assertEqual(store.net_filled_qty_since("SPY", "2026-10-01T04:00:00+00:00"), 6.0)
        self.assertEqual(store.net_filled_qty_since("QQQ", "2026-10-01T04:00:00+00:00"), 0.0)


class DisabledStoreTest(unittest.TestCase):
    def test_everything_is_a_no_op(self):
        db.close()
        self.assertIsNone(store.latest_params())
        self.assertEqual(store.effective_params()[0], 0)
        self.assertIsNone(store.save_params({}, "user"))
        self.assertIsNone(store.create_run("2026-09-28T14:00:00+00:00", 0, "manual", False))
        self.assertEqual(store.list_runs(), [])
        self.assertIsNone(store.cash_balance())                    # unknown, never a fake 0
        self.assertIsNone(store.account_snapshot())
        self.assertIsNone(store.booked_dividend_keys())
        self.assertIsNone(store.ai_spend_today())
        self.assertIsNone(store.add_audit("user", "x"))
        self.assertEqual(store.get_positions(), [])
        self.assertIsNone(store.insert_order("x", "SPY", "buy", 1, "pending", "manual"))
        self.assertEqual(store.events_after(0), [])
        self.assertFalse(store.get_autopilot())
        self.assertFalse(store.get_halted()["active"])
        self.assertIsNone(store.add_ai_spend(1.0))
        self.assertEqual(store.load_bars("SPY"), [])


if __name__ == "__main__":
    unittest.main()
