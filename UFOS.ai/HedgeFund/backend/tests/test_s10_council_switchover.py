"""Stage 10 S10-C R2 — the switch-over from the old engine to the Council (SP §22.8, §22.8a, §22.2, §22.4, §22.5):
preconditions (sandbox, migration, autopilot, unallocated, backup), the ONE transaction (params copy, heartbeat
setting, spend seed = max(old counter, cost log), legacy import above the high-water mark), the old engine stopped
in-process and refusing to start, the Council orders OFF, kill / resume across the switch-over (kill_all halves,
/fund/kill and /fund/resume aliases with ``via``, every other /fund route 410), the heartbeat from the platform
setting (fail closed) kept alive by the alerts poll, the AI budget on the Council counter, crash recovery
(``pending`` → idempotent) and the revert (old spend seeded, old engine re-created, a second run imports only the
new old ids). Temp databases and a temp cost log only — never the user's files. No network."""
import asyncio
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

try:
    import council_parity_testkit as kit
    from sp_trading_testkit import TradingCase
except ImportError:  # pragma: no cover
    from tests import council_parity_testkit as kit
    from tests.sp_trading_testkit import TradingCase

import ai_budgets
from fund import store as fund_store
from fund.api import FundApi
from fund.engine import FundEngine
from strategy_platform.api.routes import NO_BODY, V2Api
from strategy_platform.core import params as P
from strategy_platform.core.types import OrderRequest
from strategy_platform.council import service as svc_mod
from strategy_platform.council import spend
from strategy_platform.council import store as cs
from strategy_platform.council import switchover as sw
from strategy_platform.execution import accounts, control, migration
from strategy_platform.execution.errors import TradingError
from strategy_platform.execution.paper_broker import PaperBroker
from strategy_platform.storage import db

UTC = timezone.utc
FRI_SEED = datetime(2026, 10, 2, 15, 0, tzinfo=UTC)
MON = datetime(2026, 10, 5, 15, 5, 5, tzinfo=UTC)                 # Mon 11:05 NY


def audits(action):
    with db._connect() as conn:
        return [json.loads(r["detail_json"] or "{}") for r in
                conn.execute("SELECT detail_json FROM audit WHERE action = ? ORDER BY id", (action,))]


class FakeOldEngine:
    """Stands in for a re-created FundEngine (revert) — records start()."""
    def __init__(self):
        self.started, self.cycle_in_progress, self._task = False, False, None
        self.store = fund_store
        self.last_client_seen = None

    def start(self):
        self.started = True

    async def stop(self):
        pass

    def touch_client(self, now=None):
        self.last_client_seen = now


class SwitchoverCase(TradingCase):
    open_main = False
    now = MON

    def setUp(self):
        super().setUp()
        self.cost_log = os.path.join(self.tmp, "ai-cost-log.jsonl")
        patcher = mock.patch.dict(os.environ, {"UFOS_AI_COST_LOG": self.cost_log})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.world = kit.World(self.clock)
        accounts.configure(quote_fn=self.world.quote, dividends_fn=self.world.dividends,
                           bars_fn=self.world.daily_bars, clock=self.clock)
        fund_store.save_params(P.merge(None, dict(kit.OLD_PARAMS, autopilot_heartbeat_minutes=20)), "test")
        self.legacy = PaperBroker(fund_store, self.world.quote, clock=self.clock, dividends_fn=self.world.dividends,
                                  bars_fn=self.world.daily_bars)
        self.set_now(FRI_SEED)
        self.legacy.ensure_initialized(FRI_SEED)
        for sym, side, qty in (("SPY", "buy", 40), ("BTC-USD", "buy", 0.2)):
            self.legacy.submit(OrderRequest(symbol=sym, side=side, qty=qty, origin="manual"), FRI_SEED)
        self.legacy.snapshot_equity(FRI_SEED, None, {})
        self.set_now(MON)
        fund_store.set_autopilot(False)
        self.llm = kit.ScriptedLLM()
        self.engine = FundEngine(self.world, self.legacy, fund_store, clock=self.clock, startup_delay=0.0,
                                 llm=self.llm.for_path("old"), macro_fn=lambda: {},
                                 macro_status_fn=lambda: {"ok": True})
        self.engine.platform_migrated_fn = migration.platform_migrated
        control.set_legacy_engine(self.engine)
        sw.configure(None, None)
        self.addCleanup(sw.configure, None, None)
        svc_mod.set_default(svc_mod.CouncilService(self.world, clock=self.clock, llm=self.llm.for_path("new"),
                                                   sandbox_fn=lambda: False))
        self.addCleanup(svc_mod.set_default, None)

    def old_cycles(self, n=2):
        for i in range(n):
            self.set_now(MON + timedelta(minutes=30 * i))
            asyncio.run(self.engine.run_cycle("schedule"))
        self.set_now(MON + timedelta(minutes=30 * n))

    def migrate(self):
        return migration.run(None, "", "user", self.engine)

    def write_cost_log(self, *rows):
        with open(self.cost_log, "a", encoding="utf-8") as fh:
            for feature, usd, ts in rows:
                fh.write(json.dumps({"ts": ts, "module": "HedgeFund", "feature": feature, "cost_usd": usd}) + "\n")

    def run_r2(self, reason="go"):
        return asyncio.run(sw.run(reason, "user", control.legacy_engine()))


class PreconditionTest(SwitchoverCase):
    def test_blockers_and_sandbox(self):
        codes = {b["code"] for b in sw.blockers()}
        self.assertIn("not_migrated", codes)
        self.assertIn("no_account", codes)
        with self.assertRaises(sw.SwitchoverError) as cm:
            self.run_r2()
        self.assertEqual((cm.exception.status, cm.exception.code), (409, "switchover_blocked"))
        self.migrate()
        fund_store.set_autopilot(True)
        self.assertIn("autopilot_on", {b["code"] for b in sw.blockers()})
        fund_store.set_autopilot(False)
        self.assertEqual(sw.blockers(), [])                   # temp DB born at v25: no pre-v25 backup needed
        self.assertEqual(db.meta_get(db.META_CREATED_VERSION), str(db.SCHEMA_VERSION))
        with mock.patch("sandbox_profile.is_sandbox", return_value=True):
            self.assertEqual(sw.dry_run()["blockers"][0]["code"], "sandbox")
            with self.assertRaises(sw.SwitchoverError) as cm:
                self.run_r2()
            self.assertEqual((cm.exception.status, cm.exception.code), (409, "sandbox"))
        self.assertEqual(cs.switchover(), "none")
        self.assertEqual(audits("council_switchover"), [])

    def test_unreadable_spend_refuses_before_anything(self):
        self.migrate()
        with fund_store._connect() as conn:
            conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = "
                         "excluded.value", (fund_store.META_AI_SPEND, "garbage"))
        with self.assertRaises(sw.SwitchoverError) as cm:
            self.run_r2()
        self.assertEqual(cm.exception.code, "spend_unreadable")
        self.assertEqual(cs.switchover(), "none")
        self.assertIs(control.legacy_engine(), self.engine)  # the old engine was not touched


class RunTest(SwitchoverCase):
    def test_switchover(self):
        self.old_cycles(2)
        old_runs = [r for r in fund_store.list_runs(50) if r["trigger"] in ("schedule", "manual", "startup")
                    and not (r.get("summary") or {}).get("risk_watch")]
        old_recs = fund_store.list_recommendations(500)
        self.assertGreater(len(old_recs), 0)
        self.migrate()
        today = self.clock().date().isoformat()
        with fund_store._connect() as conn:
            conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = "
                         "excluded.value", (fund_store.META_AI_SPEND, json.dumps({"day": today, "usd": 0.2})))
        self.write_cost_log(("fund-judge", 0.3, f"{today}T12:00:00+00:00"), ("fund-sentiment", 0.2,
                                                                             f"{today}T13:00:00+00:00"),
                            ("analysis", 5.0, f"{today}T13:00:00+00:00"), ("fund-judge", 9.0, "2026-10-01T10:00:00"))
        dry = sw.dry_run(self.engine)
        self.assertTrue(dry["ok"], dry["blockers"])
        self.assertEqual((dry["spend_seed_usd"], dry["legacy"]["runs"]), (0.5, len(old_runs)))
        out = self.run_r2("user dialog")
        # (4) done, Council orders OFF, old engine stopped + refusing
        self.assertEqual(cs.switchover(), "done")
        self.assertFalse(cs.orders_enabled())
        self.assertIsNone(control.legacy_engine())
        with self.assertRaises(RuntimeError):
            self.engine.start()
        self.assertFalse(fund_store.get_autopilot())
        self.assertEqual([a["stage"] for a in audits("council_switchover")], ["pending", "done"])
        # (3) params copy, heartbeat, spend seed, legacy import + high-water mark
        ver, params = cs.latest_params()
        self.assertEqual(params["watchlist"], kit.OLD_PARAMS["watchlist"])
        self.assertEqual(params["interval_minutes"], 30)
        self.assertEqual(control.heartbeat_minutes(), 20.0)
        self.assertAlmostEqual(spend.spent_today(self.clock()), 0.5)
        legacy = [r for r in cs.runs(200) if r["legacy"]]
        self.assertEqual(len(legacy), len(old_runs))
        with db._connect() as conn:
            n_dec = conn.execute("SELECT COUNT(*) FROM cl_decisions WHERE legacy = 1").fetchone()[0]
            n_ai = conn.execute("SELECT COUNT(*) FROM cl_decisions WHERE legacy = 1 AND judge_source = 'rules' "
                                "AND ai_generated = 1").fetchone()[0]
            n_par = conn.execute("SELECT COUNT(*) FROM cl_params WHERE legacy = 1").fetchone()[0]
        self.assertEqual(n_dec, len(old_recs))
        self.assertEqual(n_ai, 0)                              # rules rows never carry AI provenance
        self.assertEqual(n_par, 1)                             # the one fund_params version the runs used
        hwm = json.loads(db.meta_get(cs.META_HWM))
        self.assertEqual(hwm["runs"], max(r["id"] for r in fund_store.list_runs(50)))
        self.assertEqual(out["legacy"]["recommendations"], len(old_recs))
        # change detection after R2 starts from the newest legacy decisions (cold start)
        self.assertEqual(set(cs.latest_recommendations()), {r["symbol"] for r in old_recs})
        # a second run is refused
        with self.assertRaises(sw.SwitchoverError) as cm:
            self.run_r2()
        self.assertIn("already_switched_over", {b["code"] for b in cm.exception.blockers})

    def test_kill_resume_and_fund_routes_after_r2(self):
        self.migrate()
        self.run_r2()
        cs.set_orders_enabled(True, "user", "test")
        res = control.kill_all("test kill")
        self.assertEqual(res["legacy"], {"ok": True, "applicable": False,
                                         "note": "not applicable — switched over to the Council", "error": None,
                                         "halted": None})
        self.assertEqual((res["council"]["applicable"], res["council"]["ok"], res["council"]["orders_enabled"]),
                         (True, True, False))
        self.assertTrue(accounts.get(accounts.COUNCIL).halt()["active"])
        api = FundApi(None)
        st, body = asyncio.run(api.replaced("state", {}, NO_BODY, {}))
        self.assertEqual((st, body["code"]), (410, "replaced"))
        self.assertEqual(asyncio.run(api.replaced("orders_post", {}, {"symbol": "SPY"}, {}))[0], 410)
        st, body = asyncio.run(api.replaced("kill", {}, {"reason": "old button"}, {}))
        self.assertEqual((st, body["legacy"]["applicable"], body["council"]["applicable"]), (200, False, True))
        self.assertEqual(audits("platform_kill")[-1]["via"], "/fund/kill")
        st, body = asyncio.run(api.replaced("resume", {}, {"reason": "old button"}, {}))
        self.assertEqual(st, 200, body)
        self.assertFalse(accounts.get(accounts.COUNCIL).halt()["active"])
        self.assertFalse(cs.orders_enabled())                   # resume clears the halt only
        self.assertEqual(audits("platform_resume")[-1]["via"], "/fund/resume")
        # before R2 the routes reach the old engine (None = no guard)
        db.meta_set(cs.META_SWITCHOVER, json.dumps({"state": "reverted"}))
        self.assertIsNone(asyncio.run(api.replaced("state", {}, NO_BODY, {})))

    def test_kill_before_r2_reports_both_halves(self):
        res = control.kill_all("pre-R2 kill")
        self.assertEqual(res["council"], {"applicable": False})
        self.assertIn("orders", res["legacy"])
        self.assertTrue(res["legacy"]["ok"])

    def test_ai_budget_reads_the_council_after_r2(self):
        self.migrate()
        self.run_r2()
        src = ai_budgets.FundSource(None)
        e = src.entries(self.clock())[0]
        self.assertEqual(e["limit_usd"], cs.latest_params()[1]["ai_daily_budget_usd"])
        src.put("fund", 2.5)
        self.assertEqual(cs.latest_params()[1]["ai_daily_budget_usd"], 2.5)
        self.assertEqual(audits("council_params")[-1]["via"], "/ai/budgets")


class HeartbeatTest(SwitchoverCase):
    def test_platform_setting_fail_closed_and_alerts_poll(self):
        self.migrate()
        self.run_r2()
        control.set_legacy_engine(None)
        control._last_seen = None
        control._write_autotrade(True, "user")
        t0 = self.clock()
        asyncio.run(V2Api().mon_alerts({}, NO_BODY, {}))          # the app-wide alerts poll touches
        self.set_now(t0 + timedelta(minutes=15))
        self.assertFalse(control.check_heartbeat(self.clock()))
        asyncio.run(V2Api().mon_alerts({}, NO_BODY, {}))
        self.set_now(t0 + timedelta(minutes=34))
        self.assertFalse(control.check_heartbeat(self.clock()))   # 19 min since the last poll < 20
        self.set_now(t0 + timedelta(minutes=40))
        self.assertTrue(control.check_heartbeat(self.clock()))    # no poll for 25 min → off
        self.assertFalse(control.autotrade()["enabled"])
        # unreadable setting → fail closed, and auto trading cannot be switched on
        db.meta_set(control.HEARTBEAT_KEY, "garbage")
        control._write_autotrade(True, "user")
        self.assertIsNone(control.autotrade()["heartbeat_minutes"])
        self.assertTrue(control.check_heartbeat(self.clock()))
        self.assertFalse(control.autotrade()["enabled"])
        self.assertIn("unreadable", audits("autotrade_heartbeat_off")[-1]["reason"])
        with self.assertRaises(TradingError) as cm:
            control.set_autotrade(True, "user")
        self.assertEqual(cm.exception.code, "heartbeat_unreadable")


class RecoverAndRevertTest(SwitchoverCase):
    def test_pending_is_finished_idempotently(self):
        self.old_cycles(1)
        self.migrate()
        db.meta_set(cs.META_SWITCHOVER, json.dumps({"state": "pending", "tx": "crash1"}))
        with self.assertRaises(RuntimeError):
            FundEngine(self.world, self.legacy, fund_store, clock=self.clock).start()   # refuses while pending
        first = sw.apply("user", "crash test")
        again = sw.apply("user", "crash test")
        self.assertTrue(again["already_applied"])
        self.assertEqual(sw.recover(), "done")
        with db._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM cl_params WHERE legacy = 0").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM cl_runs WHERE legacy = 1").fetchone()[0],
                             first["legacy"]["runs"])
        self.assertEqual(sw.recover(), "done")

    def test_revert_then_second_run_imports_only_new_ids(self):
        self.old_cycles(1)
        self.migrate()
        first = self.run_r2()
        n1 = first["legacy"]["runs"]
        made = []
        seen = []
        sw.configure(lambda: made.append(FakeOldEngine()) or made[-1], seen.append)
        today = self.clock().date().isoformat()
        self.write_cost_log(("fund-judge", 0.7, f"{today}T12:00:00+00:00"))
        out = asyncio.run(sw.revert("back", "user"))
        self.assertEqual(cs.switchover(), "reverted")
        self.assertEqual(out["old_spend_seed_usd"], 0.7)
        with fund_store._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'fund.ai_spend'").fetchone()
        self.assertEqual(json.loads(row[0])["usd"], 0.7)
        self.assertTrue(made[0].started)
        self.assertIs(control.legacy_engine(), made[0])
        self.assertEqual(seen, made)
        self.assertEqual(audits("council_revert")[-1]["reason"], "back")
        # the old engine runs again (a real one on the same store), then a second switch-over
        eng = FundEngine(self.world, self.legacy, fund_store, clock=self.clock, startup_delay=0.0,
                         llm=self.llm.for_path("old"), macro_fn=lambda: {}, macro_status_fn=lambda: {"ok": True})
        self.set_now(self.clock() + timedelta(minutes=30))
        asyncio.run(eng.run_cycle("schedule"))
        control.set_legacy_engine(eng)
        second = self.run_r2("again")
        self.assertEqual(second["legacy"]["runs"], 1)              # only the run above the high-water mark
        with db._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM cl_runs WHERE legacy = 1").fetchone()[0], n1 + 1)



class ReviewFixTest(SwitchoverCase):
    """R2 review: M1 (no double import), M2 (pending with last_error → retry / abort, recovery failure visible),
    minors 1–7 (scratchpad/mr/SWITCHOVER_FIX_API.md)."""

    def legacy_counts(self):
        with db._connect() as conn:
            return (conn.execute("SELECT COUNT(*), COUNT(DISTINCT old_id) FROM cl_runs WHERE legacy = 1").fetchone(),
                    conn.execute("SELECT COUNT(*) FROM cl_params WHERE legacy = 0").fetchone()[0])

    def test_m1_two_parallel_runs_import_once(self):
        import threading
        self.old_cycles(2)
        self.migrate()
        gate, results = threading.Barrier(2), []
        real_stop = sw._stop_engine

        async def slow_stop(engine):              # both threads are inside run() at the same time
            await asyncio.sleep(0.3)
            await real_stop(engine)

        def go():
            gate.wait()
            try:
                results.append(asyncio.run(sw.run("parallel", "user", self.engine))["state"])
            except sw.SwitchoverError as ex:
                results.append(ex.code)
        with mock.patch.object(sw, "_stop_engine", slow_stop):
            threads = [threading.Thread(target=go) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        self.assertEqual(sorted(results), ["done", "switchover_running"])
        (n, distinct), n_params = self.legacy_counts()
        self.assertEqual(n, distinct)
        self.assertEqual(n_params, 1)

    def test_m1_db_guards(self):
        self.old_cycles(1)
        self.migrate()
        db.meta_set(cs.META_SWITCHOVER, json.dumps({"state": "pending", "tx": "a1"}))
        self.assertIn("switchover_pending", {b["code"] for b in sw.blockers()})
        real_rows = sw.legacy_rows

        def moved(hwm):                           # another import moves the mark between the read and BEGIN
            rows = real_rows(hwm)
            db.meta_set(cs.META_HWM, json.dumps({"runs": 999, "recommendations": 999}))
            return rows
        with mock.patch.object(sw, "legacy_rows", moved):
            with self.assertRaises(sw.SwitchoverError) as cm:
                sw.apply("user", None, "a1")
        self.assertEqual(cm.exception.code, "concurrent_import")
        self.assertEqual(self.legacy_counts()[0][0], 0)        # nothing written
        with db._connect() as conn:                            # v26: the database refuses a second legacy row
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
            self.assertTrue({"ux_cl_runs_legacy_old_id", "ux_cl_decisions_legacy_old_id"} <= names)
            conn.execute("INSERT INTO cl_runs (old_id, trigger, started_at, legacy) VALUES (7, 'manual', 'x', 1)")
            with self.assertRaises(Exception):
                conn.execute("INSERT INTO cl_runs (old_id, trigger, started_at, legacy) VALUES (7, 'manual', 'y', 1)")

    def test_m2_failure_keeps_pending_then_retry(self):
        self.old_cycles(1)
        self.migrate()
        with mock.patch.object(sw, "apply", side_effect=RuntimeError("disk full")):
            with self.assertRaises(sw.SwitchoverError) as cm:
                self.run_r2()
        self.assertEqual((cm.exception.status, cm.exception.code), (503, "switchover_failed"))
        self.assertIn("disk full", cm.exception.payload()["last_error"])
        info = sw.dry_run()
        self.assertEqual(info["state"], "pending")
        self.assertIn("disk full", info["pending"]["last_error"])
        self.assertFalse(info["pending"]["applied"])
        self.assertEqual(info["actions"], {"run": False, "retry": True, "abort": True, "revert": False})
        from strategy_platform.api.council_routes import state_payload
        self.assertIn("disk full", state_payload()["switchover_error"])
        from strategy_platform.monitoring import alerts
        self.assertIn("council_switchover_failed", {a["kind"] for a in alerts.list_alerts(0, 50, False)["alerts"]})
        with self.assertRaises(sw.SwitchoverError) as cm:      # revert is not the way out of pending
            asyncio.run(sw.revert("x", "user"))
        self.assertEqual(cm.exception.code, "not_switched_over")
        out = asyncio.run(sw.retry("again", "user"))
        self.assertEqual((out["state"], cs.switchover()), ("done", "done"))
        self.assertEqual(self.legacy_counts()[1], 1)
        with self.assertRaises(sw.SwitchoverError) as cm:
            asyncio.run(sw.retry("again", "user"))
        self.assertEqual(cm.exception.code, "not_pending")

    def test_m2_abort_before_and_after_the_transaction(self):
        self.old_cycles(1)
        self.migrate()
        made = []
        sw.configure(lambda: made.append(FakeOldEngine()) or made[-1], None)
        with mock.patch.object(sw, "apply", side_effect=RuntimeError("boom")):
            with self.assertRaises(sw.SwitchoverError):
                self.run_r2()
        out = asyncio.run(sw.abort("give up", "user"))
        self.assertEqual((out["state"], out["applied"], out["old_engine_started"]), ("none", False, True))
        self.assertEqual(cs.switchover(), "none")
        self.assertIs(control.legacy_engine(), made[-1])
        self.assertFalse(fund_store.get_autopilot())
        # now a failure AFTER the transaction committed → abort = a revert (spend seeded, state reverted)
        control.set_legacy_engine(self.engine)
        with mock.patch.object(sw, "_finish", side_effect=RuntimeError("late")):
            with self.assertRaises(sw.SwitchoverError):
                self.run_r2()
        self.assertTrue(sw.pending_info()["applied"])
        out = asyncio.run(sw.abort("give up", "user"))
        self.assertEqual((out["state"], out["applied"]), ("reverted", True))
        self.assertEqual(audits("council_switchover_abort")[-1]["applied"], True)

    def test_m2_recover_failure_is_visible(self):
        self.old_cycles(1)
        self.migrate()
        db.meta_set(cs.META_SWITCHOVER, json.dumps({"state": "pending", "tx": "t9"}))
        with mock.patch.object(sw, "apply", side_effect=RuntimeError("locked")):
            self.assertEqual(sw.recover(), "pending")
        self.assertIn("locked", sw.state_info()["last_error"])
        self.assertEqual(sw.recover(), "done")                 # the next start finishes the same attempt

    def test_minors(self):
        self.migrate()
        # 3: no Council service → blocker
        svc_mod.set_default(None)
        self.assertIn("council_unavailable", {b["code"] for b in sw.blockers()})
        # 6: a *.tmp leftover is not a backup
        folder = os.path.join(self.tmp, "backups")
        os.makedirs(folder, exist_ok=True)
        open(os.path.join(folder, "hedgefund.db.pre-v25-20261024T000000Z.tmp"), "w").close()
        db.meta_set(db.META_CREATED_VERSION, "24")
        self.assertIn("no_backup", {b["code"] for b in sw.blockers()})
        open(os.path.join(folder, "hedgefund.db.pre-v25-20261024T000000Z"), "w").close()
        self.assertNotIn("no_backup", {b["code"] for b in sw.blockers()})
        # 4: kill_all while pending → no false failure
        db.meta_set(cs.META_SWITCHOVER, json.dumps({"state": "pending", "tx": "k"}))
        res = control.kill_all("pending kill")
        self.assertEqual((res["legacy"]["applicable"], res["council"]["applicable"]), (False, True))
        # 7: revert refused in sandbox; 2: revert of an already reverted state is not an error
        db.meta_set(cs.META_SWITCHOVER, json.dumps({"state": "reverted"}))
        with mock.patch("sandbox_profile.is_sandbox", return_value=True):
            with self.assertRaises(sw.SwitchoverError) as cm:
                asyncio.run(sw.revert("x", "user"))
            self.assertEqual(cm.exception.code, "sandbox")
        self.assertTrue(asyncio.run(sw.revert("x", "user"))["already"])
        # 5: a failing /fund/kill alias never reports a fake ok
        db.meta_set(cs.META_SWITCHOVER, json.dumps({"state": "done"}))
        with mock.patch.object(control, "kill_all", side_effect=RuntimeError("x")):
            st, body = asyncio.run(FundApi(None).replaced("kill", {}, {}, {}))
        self.assertEqual((st, body["legacy"]["ok"], body["council"]["ok"]), (200, False, False))

    def test_legacy_duplicates_block_and_alert(self):
        """A database that got duplicates before v26 (index skipped): critical alert + blocker, visible in the dry run."""
        self.migrate()
        with db._connect() as conn:
            conn.execute("DROP INDEX ux_cl_runs_legacy_old_id")          # as on a database migrated with duplicates
            for ts in ("a", "b", "c"):
                conn.execute("INSERT INTO cl_runs (old_id, trigger, started_at, legacy) VALUES (5, 'manual', ?, 1)",
                             (ts,))
        b = [x for x in sw.dry_run()["blockers"] if x["code"] == "legacy_duplicates"]
        self.assertEqual((b[0]["table"], b[0]["rows"]), ("cl_runs", 2))
        self.assertFalse(sw.dry_run()["actions"]["run"])
        with self.assertRaises(sw.SwitchoverError) as cm:
            self.run_r2()
        self.assertIn("legacy_duplicates", {x["code"] for x in cm.exception.blockers})
        sw.recover()                                                    # the start check alerts too (deduped)
        from strategy_platform.monitoring import alerts
        dup = [a for a in alerts.list_alerts(0, 50, False)["alerts"] if a["kind"] == "council_legacy_duplicates"]
        self.assertEqual(len(dup), 1)
        self.assertIn("cl_runs", dup[0]["body"])

    def test_minor1_engine_starts_no_new_cycle_while_stopping(self):
        eng = FakeOldEngine()
        eng._stopping = False
        asyncio.run(sw._stop_engine(eng))
        self.assertTrue(eng._stopping)
        self.assertTrue(eng._switchover_refused)

if __name__ == "__main__":
    unittest.main()
