"""Stage 7 acceptance (SPEC §17): the old engine's behaviour reproduced on paper —
(1) ORDER parity: the same orders through the OMS + internal paper adapter and directly
through the old engine's PaperBroker on fund.store give identical fills, cash ledger,
positions and account values (accruals, borrow, crypto perp funding included);
(2) TARGET parity: one strategy source holding the whole account, executed by the runtime
(netting → OMS), equals the old autopilot path ``broker.target()``;
(3) MIGRATION: dry run, blockers, the real run on a TEMP COPY of the old account (never the
real DB), AccountView parity, the coexistence rule (the old engine refuses to trade a
migrated account), crash recovery. Offline, fakes only."""
import asyncio
import json
import os
import shutil
import unittest
from datetime import datetime, timedelta, timezone

try:
    from sp_trading_testkit import EXPECTED, FRI, SAT, TradingCase, make_strategy
except ImportError:  # pragma: no cover
    from tests.sp_trading_testkit import EXPECTED, FRI, SAT, TradingCase, make_strategy

from strategy_platform.core import params as P
from fund import store as fund_store
from strategy_platform.execution.paper_broker import PaperBroker
from fund.engine import EngineError, FundEngine
from strategy_platform.data.market_feed import FakeFeed
from strategy_platform.core.types import OrderRequest
from newsroom import db as newsroom_db
from strategy_platform.execution import accounts, control, migration, oms
from strategy_platform.execution.errors import TradingError
from strategy_platform.portfolio import ledger
from strategy_platform.storage import db

MON = datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)
ORDER_KEYS = ("ts", "symbol", "side", "qty", "status", "fill_price", "fees", "filled_at", "reject_reason", "notional")


class LegacyMixin:
    def legacy_broker(self):
        fund_store.save_params(P.merge(None, {"watchlist": ["SPY", "QQQ", "TLT"]}), "test")
        b = PaperBroker(fund_store, self.quotes, clock=self.clock)
        b.ensure_initialized(self.clock())
        return b

    def assert_books_equal(self, legacy_store, platform_store):
        lo = sorted(legacy_store.list_orders(limit=1000), key=lambda o: o["id"])
        po = platform_store.all_orders()
        self.assertEqual(len(lo), len(po))
        for a, b in zip(lo, po):
            for k in ORDER_KEYS:
                if isinstance(a[k], float):
                    self.assertAlmostEqual(a[k], b[k], places=6, msg=k)
                else:
                    self.assertEqual(a[k], b[k], k)
        ll = sorted(legacy_store.cash_entries(limit=10000), key=lambda r: r["id"])
        pl = platform_store.all_cash_entries()
        self.assertEqual([(r["ts"], r["kind"], r["symbol"], r["note"]) for r in ll],
                         [(r["ts"], r["kind"], r["symbol"], r["note"]) for r in pl])
        for a, b in zip(ll, pl):
            self.assertAlmostEqual(a["amount"], b["amount"], places=6)
        lp = {p["symbol"]: p for p in legacy_store.get_positions()}
        pp = {p["symbol"]: p for p in platform_store.get_positions()}
        self.assertEqual(set(lp), set(pp))
        for s in lp:
            for k in ("qty", "avg_price", "realized_pnl", "borrow_rate_pct", "opened_at"):
                if isinstance(lp[s][k], float):
                    self.assertAlmostEqual(lp[s][k], pp[s][k], places=6, msg=f"{s}.{k}")
                else:
                    self.assertEqual(lp[s][k], pp[s][k], f"{s}.{k}")

    def assert_views_equal(self, a, b):
        da, dbb = a.to_dict(), b.to_dict()
        self.assertEqual(set(da), set(dbb))
        for k in da:
            if isinstance(da[k], float):
                self.assertAlmostEqual(da[k], dbb[k], places=6, msg=k)
            else:
                self.assertEqual(da[k], dbb[k], k)


class OrderParityTest(LegacyMixin, TradingCase):
    def test_same_orders_same_books(self):
        from strategy_platform.portfolio import settings as pf_settings
        pf_settings.put("paper-main", {"duplicate_window_seconds": 10}, "parity test")
        legacy = self.legacy_broker()
        script = [(FRI, "SPY", "buy", 40), (FRI + timedelta(minutes=1), "QQQ", "short", 30),
                  (FRI + timedelta(minutes=2), "BTC-USD", "short", 0.2),
                  (FRI + timedelta(minutes=3), "TLT", "buy", 100),
                  (SAT, "BTC-USD", "cover", 0.1),
                  (MON, "SPY", "sell", 15), (MON + timedelta(minutes=1), "QQQ", "cover", 30),
                  (MON + timedelta(minutes=2), "TLT", "sell", 100)]
        for when, sym, side, qty in script:
            self.set_now(when)
            self.quotes.prices["SPY"] = 500.0 + (when - FRI).total_seconds() / 3600.0
            self.quotes.prices["BTC-USD"] = 60000.0 - (when - FRI).total_seconds() / 60.0
            # accruals first (borrow fee, perp funding), like both engines' cycles
            legacy.daily_accruals(when, None, {})
            self.acc.adapter.daily_accruals(when, {})
            r = legacy.submit(OrderRequest(symbol=sym, side=side, qty=qty, origin="manual"), when)
            o = self.manual(sym, side, qty)
            self.assertEqual(r.status, "filled", f"{sym} {side}")
            self.assertEqual(o["state"], "filled", (sym, side, o["checks"]))
        self.assert_books_equal(fund_store, self.acc.store)
        self.assert_views_equal(legacy.account({}), self.acc.broker.account({}))
        kinds = {r["kind"] for r in self.acc.store.all_cash_entries()}
        self.assertTrue({"deposit", "fill", "fee", "borrow_fee", "funding"} <= kinds, kinds)
        # the sub-ledger mirrors the account exactly
        st = ledger.all_states("paper-main")
        self.assertAlmostEqual(sum(v["cash"] for v in st.values()), self.acc.store.cash_balance(), places=6)


class TargetParityTest(LegacyMixin, TradingCase):
    """One source holding the whole account ≡ the old autopilot's target() (and the backtest's execution)."""

    def setUp(self):
        super().setUp()
        from strategy_platform.portfolio import settings as pf_settings
        pf_settings.put("paper-main", {"max_order_notional_pct": 100.0, "max_position_pct_equity": 100.0,
                                       "max_position_pct_crypto": 100.0, "duplicate_window_seconds": 10}, "test")
        self.sid = make_strategy(("SPY", "QQQ", "TLT"), state="incubation")
        self.run_id = "inc-0123456789ab"
        with db._connect() as conn:
            conn.execute("INSERT INTO incubation_runs (id, strategy_id, version, content_hash, account_id, source_id,"
                         " length_sessions, calendar, start_date, end_date, created_at, created_by, reason,"
                         " expected_json) VALUES (?, ?, 1, 'h', 'paper-main', ?, 60, 'nyse', '2026-10-02', '2026-12-28',"
                         " ?, 'user', 'test', ?)", (self.run_id, self.sid, self.sid,
                                                     FRI.isoformat(timespec="seconds"), json.dumps(EXPECTED)))
            ledger.insert_entries(conn, "paper-main", [
                {"source_id": "_unallocated", "kind": "capital", "amount": -100000.0},
                {"source_id": self.sid, "kind": "capital", "amount": 100000.0}])
        ledger.attribute(self.acc)
        control.set_autotrade(True, "user")

    def decision(self, targets, when, n):
        res = {"targets": targets, "force": [], "reason": "strategy rebalance", "state": {},
               "ref_prices": {}, "adv": {s: 1e12 for s in targets}, "timing": "next_open",
               "window": {"start": (when - timedelta(minutes=1)).isoformat(timespec="seconds"),
                          "end": (when + timedelta(hours=3)).isoformat(timespec="seconds")}}
        with db._connect() as conn:
            conn.execute("INSERT INTO rt_decisions (id, account_id, source_id, strategy_id, version, incubation_id,"
                         " bar_date, created_at, status, result_json) VALUES (?, 'paper-main', ?, ?, 1, ?, ?, ?, 'ok', ?)",
                         (f"rd-{n:012d}", self.sid, self.sid, self.run_id, f"2026-10-{n:02d}",
                          when.isoformat(timespec="seconds"), json.dumps(res)))

    def test_runtime_equals_target(self):
        legacy = self.legacy_broker()
        steps = [(FRI, {"SPY": 0.4, "QQQ": 0.3, "TLT": 0.2}), (MON, {"SPY": 0.1, "QQQ": -0.2, "TLT": 0.5}),
                 (MON + timedelta(days=1), {"SPY": -0.3, "QQQ": 0.25, "TLT": 0.0})]
        for n, (when, targets) in enumerate(steps, start=1):
            self.set_now(when)
            control.touch(when)
            for s in targets:
                self.quotes.prices[s] = {"SPY": 500.0, "QQQ": 400.0, "TLT": 90.0}[s] * (1 + 0.01 * n)
            self.decision(targets, when, n)
            # the backtest's / runtime's order: reductions first (desired vs held at the start equity)
            from strategy_platform.execution.paper_broker import round_qty
            e0 = legacy.account({}).equity
            work = []
            for s in sorted(targets):
                h = float((fund_store.get_position(s) or {}).get("qty") or 0.0)
                raw = targets[s] * e0 / self.quotes.prices[s]
                d = round_qty(s, abs(raw)) * (1 if raw >= 0 else -1)
                if abs(d - h) < 1e-9:
                    continue
                red = d == 0 or (d * h > 0 and abs(d) < abs(h))
                work.append((0 if red else 1, s))
            for _r, s in sorted(work):
                legacy.target(s, targets[s], "autopilot", when)
            self.rt._execute(self.acc, when, {"orders": 0, "errors": {}})
        self.assert_books_equal(fund_store, self.acc.store)
        self.assert_views_equal(legacy.account({}), self.acc.broker.account({}))
        self.assertEqual(ledger.holdings("paper-main").get(self.sid),
                         {p["symbol"]: p["qty"] for p in self.acc.store.get_positions()})


class MigrationTest(LegacyMixin, TradingCase):
    open_main = False

    def setUp(self):
        super().setUp()
        self.legacy = self.legacy_broker()
        for when, sym, side, qty in [(FRI, "SPY", "buy", 40), (FRI, "QQQ", "short", 30),
                                     (FRI, "BTC-USD", "buy", 0.3)]:
            self.legacy.submit(OrderRequest(symbol=sym, side=side, qty=qty, origin="manual"), when)
        self.legacy.snapshot_equity(FRI, None, {})
        self.set_now(MON)
        self.legacy.daily_accruals(MON, None, {})
        self.legacy.snapshot_equity(MON, None, {})
        fund_store.set_autopilot(False)
        self.feed = FakeFeed(bars={}, quotes={"SPY": 500.0})
        self.engine = FundEngine(self.feed, self.legacy, fund_store, clock=self.clock, startup_delay=0.0)
        self.engine.platform_migrated_fn = migration.platform_migrated
        control.set_legacy_engine(self.engine)

    def test_dry_run_then_migrate_with_parity_and_coexistence(self):
        rep = migration.dry_run("user", self.engine)
        self.assertTrue(rep["ok"], rep["blockers"])
        self.assertEqual(rep["legacy"]["positions"][0]["symbol"], "BTC-USD")
        self.assertEqual(rep["target_account_id"], "paper-council")
        self.assertEqual(rep["settings"]["drawdown_kill_pct"], 8.0)        # the old drawdown stop carries over
        self.assertFalse(accounts.exists("paper-council"))                 # a dry run writes nothing
        # fix1023: the typed "MIGRATE" is no longer checked (accepted and ignored)
        before = self.legacy.account({})
        out = migration.run(None, "", "user", self.engine)          # fix1023: no confirm word, no note
        acc = out["account"]
        self.assertEqual(acc.id, "paper-council")
        self.assert_books_equal(fund_store, acc.store)
        self.assert_views_equal(before, acc.broker.account({}))
        self.assert_views_equal(self.legacy.account({}), acc.broker.account({}))
        # the Council sub-ledger holds everything
        st = ledger.all_states("paper-council")
        self.assertEqual(set(st), {"council-of-ais"})
        self.assertEqual({s: v["qty"] for s, v in st["council-of-ais"]["positions"].items()},
                         {p["symbol"]: p["qty"] for p in acc.store.get_positions()})
        self.assertAlmostEqual(st["council-of-ais"]["cash"], acc.store.cash_balance(), places=6)
        self.assertEqual(ledger.attribute(acc), 0)                          # copied rows are not re-attributed
        self.assertEqual(oms.sync(acc)["imported"], 0)                       # nor re-imported as margin orders
        # coexistence: the old engine never trades the migrated account again
        self.assertEqual(fund_store.get_migration_state()["status"], "migrated")
        with self.assertRaises(EngineError) as cm:
            asyncio.run(self.engine.manual_order({"symbol": "SPY", "side": "buy", "qty": 1}))
        self.assertEqual(cm.exception.status, 409)
        self.assertIn("migrated to the Strategy Platform", str(cm.exception.message if hasattr(cm.exception, "message")
                                                               else cm.exception))
        with self.assertRaises(EngineError):
            self.engine.set_autopilot(True)
        with self.assertRaises(EngineError):
            asyncio.run(self.engine.reset(100000))
        watch = asyncio.run(self.engine.run_risk_watch(force=True))
        self.assertEqual(watch["status"], "not_needed")
        self.assertEqual(self.engine.state()["legacy_account"]["status"], "migrated")
        self.assertEqual(migration.legacy_state(self.engine)["owner"], "platform")
        # a second migration is refused
        rep2 = migration.dry_run("user", self.engine)
        self.assertFalse(rep2["ok"])
        self.assertIn("already_migrated", [b["code"] for b in rep2["blockers"]])

    def test_blockers(self):
        fund_store.set_autopilot(True)
        self.legacy.submit(OrderRequest(symbol="SPY", side="buy", qty=1, origin="manual"), SAT)  # queued (closed)
        rep = migration.dry_run("user", self.engine)
        self.assertEqual({b["code"] for b in rep["blockers"]}, {"autopilot_on", "pending_orders"})
        with self.assertRaises(TradingError) as cm:
            migration.run("MIGRATE", "x" * 5, "user", self.engine)
        self.assertEqual(cm.exception.code, "migration_blocked")
        self.assertIsNone(fund_store.get_migration_state())                 # nothing frozen
        self.assertFalse(accounts.exists("paper-council"))

    def test_temp_copy_path_and_crash_recovery(self):
        copy = os.path.join(self.tmp, "copy-of-newsroom.db")
        shutil.copyfile(newsroom_db.db_path(), copy)
        rep = migration.dry_run("user", None, path=copy)
        self.assertTrue(rep["ok"], rep["blockers"])
        self.assertEqual(rep["legacy"]["orders"], 3)
        # crash after the freeze, before the copy: recovery un-freezes
        fund_store.set_migration_state({"status": "migrating", "migration_id": "mig-000000000000", "at": "x"})
        self.assertEqual(migration.recover(), "undone")
        self.assertIsNone(fund_store.get_migration_state())
        # crash after the copy, before the final flag: recovery completes
        out = migration.run("MIGRATE", "copy then crash", "user", self.engine)
        mid = out["migration"]["id"]
        fund_store.set_migration_state({"status": "migrating", "migration_id": mid, "at": "x"})
        self.assertEqual(migration.recover(), "completed")
        self.assertEqual(fund_store.get_migration_state()["status"], "migrated")

    def test_failed_copy_unfreezes(self):
        from strategy_platform.execution import migration as mig
        real = mig._copy

        def boom(*a, **k):
            raise RuntimeError("disk full")
        mig._copy = boom
        try:
            with self.assertRaises(TradingError) as cm:             # review fix M2: a mapped error, not a 500
                migration.run("MIGRATE", "will fail", "user", self.engine)
        finally:
            mig._copy = real
        self.assertEqual((cm.exception.status, cm.exception.code), (503, "migration_failed"))
        self.assertIn("nothing was moved", cm.exception.user_message)
        self.assertIsNone(fund_store.get_migration_state())
        self.assertFalse(accounts.exists("paper-council"))
        self.assertIsNone(self.engine.migration_state())
        self.assertEqual(asyncio.run(self.engine.manual_order({"symbol": "SPY", "side": "buy", "qty": 1}))["status"],
                         "filled")                                          # the old engine still trades

    def test_concurrent_request_is_refused_and_never_unfreezes(self):
        # review fix M2 (2026-10-18)
        from strategy_platform.execution import migration as mig
        # (a) a second request while one runs -> 409 migration_running, nothing frozen
        self.assertTrue(mig._run_lock.acquire(blocking=False))
        try:
            with self.assertRaises(TradingError) as cm:
                migration.run("MIGRATE", "second request", "user", self.engine)
        finally:
            mig._run_lock.release()
        self.assertEqual(cm.exception.code, "migration_blocked")
        self.assertEqual(cm.exception.extra["blockers"][0]["code"], "migration_running")
        self.assertIsNone(fund_store.get_migration_state())
        # (b) the first request wins; a request whose pre-freeze check was stale is refused after the freeze and
        # leaves the account migrated (never reset to active)
        first = migration.run("MIGRATE", "first", "user", self.engine)
        real = mig._blockers
        mig._blockers = lambda legacy, engine=None: []         # the stale pre-lock view of a racing request
        try:
            with self.assertRaises(TradingError) as cm2:
                migration.run("MIGRATE", "racing", "user", self.engine)
        finally:
            mig._blockers = real
        self.assertEqual(cm2.exception.status, 409)
        self.assertIn("already_migrated", [b["code"] for b in cm2.exception.extra["blockers"]])
        st = fund_store.get_migration_state()
        self.assertEqual((st["status"], st["migration_id"]), ("migrated", first["migration"]["id"]))
        self.assertEqual(self.engine.migration_state()["status"], "migrated")
        # (c) even an IntegrityError inside the copy (the record exists) maps to 409 and keeps the freeze
        real_copy, real_done = mig._copy, mig._done_blockers
        calls = []

        def done_once():
            calls.append(1)
            return [] if len(calls) == 1 else real_done()
        import sqlite3

        def dup(*a, **k):
            raise sqlite3.IntegrityError("UNIQUE constraint failed: pf_accounts.id")
        mig._blockers, mig._done_blockers, mig._copy = (lambda legacy, engine=None: []), done_once, dup
        try:
            with self.assertRaises(TradingError) as cm3:
                migration.run("MIGRATE", "racing 2", "user", self.engine)
        finally:
            mig._blockers, mig._done_blockers, mig._copy = real, real_done, real_copy
        self.assertEqual((cm3.exception.status, cm3.exception.code), (409, "migration_blocked"))
        self.assertEqual(fund_store.get_migration_state()["status"], "migrated")
        # recovery with a record of ANOTHER id completes (never un-freezes)
        fund_store.set_migration_state({"status": "migrating", "migration_id": "mig-other0000000", "at": "x"})
        self.assertEqual(migration.recover(), "completed")
        self.assertEqual(fund_store.get_migration_state()["migration_id"], first["migration"]["id"])


if __name__ == "__main__":
    unittest.main()
