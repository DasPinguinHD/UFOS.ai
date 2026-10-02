"""fund/engine.py FundEngine (paper trading only): a full cycle with fake
market data and a fake LLM transport, autopilot gating, params versioning
(a running cycle keeps its version), drawdown stop, kill switch / resume,
autopilot forced off at start, AI budget -> rule fallback, closed market ->
pending orders filled at the open, flips, outside position changes,
manual accept, reset, equity benchmark, error handling and the /fund/state
shape (SPEC §11) without NaN.

Run from HedgeFund/backend:  python -m unittest tests.test_fund_engine -v
"""
import asyncio
import json
import unittest
from datetime import timedelta

try:
    from fund_testkit import FRI, MON, SAT, SYMS, EngineCase
except ImportError:  # pragma: no cover - run as package
    from tests.fund_testkit import FRI, MON, SAT, SYMS, EngineCase

from fund import store
from fund.engine import EngineError, FundEngine, json_safe
from fund.types import Quote


class CycleTest(EngineCase):
    def test_full_cycle_produces_run_agents_recommendations(self):
        out = self.cycle("startup")
        self.assertEqual(out["status"], "ok", out)
        run = store.get_run(out["run_id"])
        self.assertEqual(run["run"]["trigger"], "startup")
        self.assertEqual(run["run"]["judge_mode"], "llm")
        self.assertEqual(len(run["agents"]), len(SYMS) * 4)
        self.assertEqual({a["agent"] for a in run["agents"]}, {"technical", "macro", "sentiment", "risk"})
        recs = run["recommendations"]
        self.assertEqual(sorted(r["symbol"] for r in recs), sorted(SYMS))
        for r in recs:
            self.assertEqual((r["action"], r["source"], r["status"]), ("LONG", "llm", "open"))
            self.assertTrue(r["provenance"]["ai_generated"])
            self.assertGreater(r["target_weight"], 0)
            self.assertIsNotNone(r["usage"]["cost_usd"])
        self.assertEqual(run["orders"], [])                       # autopilot is off
        self.assertAlmostEqual(run["run"]["cost_usd"], 0.002)       # sentiment + judge call
        self.assertAlmostEqual(store.ai_spend_today(), 0.002)
        self.assertEqual(self.fake.calls, ["sentiment", "judge"])
        # the cycle's snapshot replaces the opening-deposit point of the same second
        self.assertEqual(store.equity_points()[-1]["benchmark"], 500.0)
        self.assertEqual(store.equity_points()[-1]["ts"], FRI.isoformat())

    def test_benchmark_is_quote_only_when_not_on_the_watchlist(self):
        self.engine.update_params({"watchlist": ["QQQ"]})
        self.cycle()
        self.assertIn(("quote", "SPY"), self.feed.calls)
        self.assertNotIn(("daily_bars", "SPY"), self.feed.calls)
        self.assertEqual(store.equity_points()[-1]["benchmark"], 500.0)
        self.assertEqual(list(store.latest_recommendations()), ["QQQ"])

    def test_second_cycle_carries_unchanged_decisions(self):
        self.cycle()
        self.fake.calls.clear()
        out = self.cycle()
        recs = store.get_run(out["run_id"])["recommendations"]
        self.assertEqual({r["source"] for r in recs}, {"carried"})
        self.assertEqual(self.fake.calls, [])                        # sentiment cached, judge not needed
        self.assertEqual(store.get_run(out["run_id"])["run"]["judge_mode"], "none")

    def test_autopilot_places_orders_only_when_enabled(self):
        self.cycle()
        self.assertEqual(store.list_orders(), [])
        self.engine.set_autopilot(True)
        out = self.cycle()
        orders = store.list_orders()
        self.assertEqual(sorted(o["symbol"] for o in orders), sorted(SYMS))
        self.assertTrue(all(o["origin"] == "autopilot" and o["status"] == "filled" for o in orders))
        self.assertEqual(self.qty("SPY"), 9)          # 5 % x 0.9 of 100k at 500 -> 9 shares
        recs = store.get_run(out["run_id"])["recommendations"]
        self.assertTrue(all(r["status"] == "executed" and r["order_id"] for r in recs))
        kinds = [e["kind"] for e in store.events_after(0)]
        self.assertEqual(kinds.count("order"), 2)
        self.assertIn("autopilot", kinds)
        self.assertEqual(store.get_run(out["run_id"])["run"]["summary"]["orders"], 2)
        # next cycle: target already reached -> nothing new
        out = self.cycle()
        self.assertEqual(len(store.list_orders()), 2)
        recs = store.get_run(out["run_id"])["recommendations"]
        self.assertTrue(all(r["status"] == "skipped" and r["skip_reason"] == "no change needed" for r in recs))

    def test_daily_trade_limit(self):
        store.save_params(dict(store.effective_params()[1], max_trades_per_day=1), "test")
        self.engine.set_autopilot(True)
        out = self.cycle()
        self.assertEqual(len(store.list_orders()), 1)
        recs = store.get_run(out["run_id"])["recommendations"]
        self.assertIn("daily trade limit", [r["skip_reason"] for r in recs])

    def test_params_change_applies_from_next_cycle_only(self):
        v1 = store.effective_params()[0]
        self.engine.set_autopilot(True)
        changed = {}

        def hook(kind):
            if kind == "judge" and not changed:
                changed.update(self.engine.update_params({"max_position_pct": 10.0}))
        self.fake.hook = hook
        out = self.cycle()
        self.assertEqual(changed["version"], v1 + 1)
        self.assertEqual(changed["effective_from"], (FRI + timedelta(minutes=5)).isoformat())
        self.assertEqual(store.get_run(out["run_id"])["run"]["params_version"], v1)
        self.assertEqual(self.qty("SPY"), 9)                          # sized with 5 %, not 10 %
        self.assertTrue(self.engine.state()["pending_params"])
        out2 = self.cycle()
        self.assertEqual(store.get_run(out2["run_id"])["run"]["params_version"], v1 + 1)
        self.assertEqual(self.qty("SPY"), 18)                         # now 10 % x 0.9
        self.assertFalse(self.engine.state()["pending_params"])

    def test_update_params_validation_and_audit(self):
        with self.assertRaises(EngineError) as cm:
            self.engine.update_params({"max_position_pct": 99, "bogus": 1})
        self.assertEqual(cm.exception.status, 400)
        self.assertEqual(set(cm.exception.errors), {"max_position_pct", "bogus"})
        v = store.effective_params()[0]
        same = self.engine.update_params({"fees_bps": 0.0})           # no change -> no new version
        self.assertEqual(same["version"], v)
        res = self.engine.update_params({"agent_weights": {"technical": 0.5, "macro": 1, "sentiment": 0.5}})
        self.assertEqual(res["version"], v + 1)
        self.assertEqual(res["params"]["agent_weights"]["technical"], 0.5)
        self.assertEqual(store.list_audit(1)[0]["action"], "params_update")
        self.assertEqual(store.list_audit(1)[0]["actor"], "user")

    def test_flip_event_after_rejudgement(self):
        self.cycle()
        self.fake.action = "SHORT"
        self.engine.update_params({"agent_weights": {"technical": 0.9, "macro": 1.0, "sentiment": 0.5}})
        out = self.cycle()
        run = store.get_run(out["run_id"])
        self.assertEqual({r["action"] for r in run["recommendations"]}, {"SHORT"})
        self.assertEqual(sorted(run["run"]["summary"]["flips"]), ["QQQ LONG→SHORT", "SPY LONG→SHORT"])
        flips = [e for e in store.events_after(0) if e["kind"] == "flip"]
        self.assertEqual(len(flips), 2)
        self.assertIn("AI-generated", flips[0]["body"])
        self.assertEqual(store.list_runs(1)[0]["flips"], run["run"]["summary"]["flips"])

    def test_position_changed_outside_the_loop_triggers_rejudgement(self):
        self.cycle()
        asyncio.run(self.engine.manual_order({"symbol": "spy", "side": "buy", "qty": 3}))
        self.fake.calls.clear()
        out = self.cycle()
        run = store.get_run(out["run_id"])
        self.assertEqual(run["run"]["summary"]["positions_changed"], ["SPY"])
        src = {r["symbol"]: r["source"] for r in run["recommendations"]}
        self.assertEqual(src, {"SPY": "llm", "QQQ": "carried"})
        self.assertEqual(self.fake.calls, ["judge"])


class BudgetTest(EngineCase):
    extra_params = {"ai_daily_budget_usd": 0.01}

    def test_budget_exhausted_falls_back_to_rules(self):
        store.add_ai_spend(0.02)
        out = self.cycle()
        self.assertEqual(self.fake.calls, [])
        run = store.get_run(out["run_id"])
        self.assertEqual(run["run"]["judge_mode"], "rules")
        for r in run["recommendations"]:
            self.assertEqual(r["source"], "rules")
            self.assertTrue(r["rationale"].startswith("Rule-based (AI budget reached)"), r["rationale"])
            self.assertIsNone(r["provenance"])
        sent = [a for a in run["agents"] if a["agent"] == "sentiment"]
        self.assertTrue(all(a["source"] == "unavailable" for a in sent))
        st = self.engine.state()
        self.assertTrue(st["ai_budget"]["exhausted"])
        self.assertEqual(st["ai_budget"]["limit_usd"], 0.01)

    def test_budget_zero_never_calls_llm(self):
        self.engine.update_params({"ai_daily_budget_usd": 0.0})
        self.cycle()
        self.assertEqual(self.fake.calls, [])
        recs = store.latest_recommendations()
        self.assertTrue(all(r["rationale"].startswith("Rule-based (AI disabled") for r in recs.values()))


class HaltTest(EngineCase):
    def test_drawdown_stop_halts_and_liquidates(self):
        asyncio.run(self.engine.manual_order({"symbol": "SPY", "side": "buy", "qty": 150}))
        self.cycle()                                 # snapshot, peak ~100k (autopilot off: position kept)
        self.engine.set_autopilot(True)
        self.feed.quotes["SPY"] = 400.0              # -15k on 100k -> 15 % drawdown
        out = self.cycle()
        halt = store.get_halted()
        self.assertTrue(halt["active"])
        self.assertIn("Drawdown stop", halt["reason"])
        self.assertFalse(store.get_autopilot())
        self.assertEqual(store.get_positions(), [])
        liq = [o for o in store.list_orders() if o["origin"] == "drawdown-stop"]
        self.assertTrue(liq and all(o["status"] == "filled" for o in liq))
        self.assertTrue(any(e["kind"] == "halt" for e in store.events_after(0)))
        self.assertEqual([a["actor"] for a in store.list_audit(50) if a["action"] == "drawdown_stop"], ["engine"])
        summary = store.get_run(out["run_id"])["run"]["summary"]
        self.assertTrue(summary["drawdown_stop"])
        # halted: recommendations are made, but no autopilot orders
        n = len(store.list_orders())
        self.cycle()
        self.assertEqual(len(store.list_orders()), n)
        # resume rebases the peak: the next cycle must not stop again
        self.engine.resume()
        self.assertAlmostEqual(store.get_peak_equity(), self.broker.account().equity)
        self.cycle()
        self.assertFalse(store.get_halted()["active"])

    def test_kill_resume_and_autopilot_refused_while_halted(self):
        self.engine.set_autopilot(True)
        self.cycle()
        self.assertGreater(self.qty("SPY"), 0)
        res = asyncio.run(self.engine.kill("test"))
        self.assertTrue(res["halted"])
        self.assertEqual({o["origin"] for o in res["orders"]}, {"kill-switch"})
        self.assertEqual(store.get_positions(), [])
        self.assertFalse(store.get_autopilot())
        with self.assertRaises(EngineError) as cm:
            self.engine.set_autopilot(True)
        self.assertEqual(cm.exception.status, 409)
        self.engine.set_autopilot(False)                  # switching off is always allowed
        with self.assertRaises(EngineError):
            asyncio.run(self.engine.manual_order({"symbol": "SPY", "side": "buy", "qty": 1}))
        self.assertEqual(self.engine.resume(), {"halted": False})
        self.assertFalse(store.get_halted()["active"])
        self.assertFalse(store.get_autopilot())           # stays off after resume
        self.assertEqual(self.engine.set_autopilot(True), {"autopilot": True})
        actions = [a["action"] for a in store.list_audit(50) if a["actor"] == "user"]
        for a in ("kill", "resume", "autopilot"):
            self.assertIn(a, actions)
        kinds = [e["kind"] for e in store.events_after(0)]
        self.assertIn("halt", kinds)
        self.assertIn("resume", kinds)

    def test_kill_while_closed_queues_pending(self):
        asyncio.run(self.engine.manual_order({"symbol": "SPY", "side": "buy", "qty": 10}))
        self.now = SAT
        res = asyncio.run(self.engine.kill(None))
        self.assertEqual([(o["side"], o["status"]) for o in res["orders"]], [("sell", "pending")])
        self.assertEqual(self.qty("SPY"), 10)
        self.now = MON
        self.cycle()
        self.assertEqual(self.qty("SPY"), 0)


class LifecycleTest(EngineCase):
    def test_autopilot_forced_off_at_start_and_loop_runs(self):
        store.set_autopilot(True)

        async def go():
            self.engine.start()
            self.assertFalse(store.get_autopilot())
            for _ in range(300):
                await asyncio.sleep(0.01)
                if store.last_run() and not self.engine.cycle_in_progress:
                    break
            self.assertTrue(self.engine.running)
            first = store.last_run()
            self.engine.request_run("user")
            for _ in range(300):
                await asyncio.sleep(0.01)
                if store.last_run()["id"] != first["id"] and not self.engine.cycle_in_progress:
                    break
            await self.engine.stop()
            return first, store.last_run()

        first, second = asyncio.run(go())
        self.assertEqual(first["trigger"], "startup")
        self.assertEqual(second["trigger"], "manual")
        self.assertFalse(self.engine.running)
        self.assertIn("autopilot_forced_off", [a["action"] for a in store.list_audit(20)])

    def test_new_engine_forces_autopilot_off(self):
        self.engine.set_autopilot(True)
        other = FundEngine(self.feed, self.broker, store, clock=lambda: self.now, startup_delay=60)

        async def go():
            other.start()
            await other.stop()
        asyncio.run(go())
        self.assertFalse(store.get_autopilot())

    def test_next_run_schedule(self):
        self.cycle()
        self.assertEqual(self.engine.next_run_at, FRI + timedelta(minutes=5))
        self.now = SAT
        self.cycle()
        # US market closed: no scheduled full cycle before Monday's open (+5 s); the
        # crypto risk watch is due after closed_interval_minutes
        self.assertEqual(self.engine.next_run_at, MON.replace(hour=13, minute=30, second=5))
        self.assertEqual(self.engine.next_risk_watch_at, SAT + timedelta(minutes=30))
        self.now = MON.replace(hour=13, minute=20)      # 09:20 EDT Monday, closed; opens 13:30 UTC
        self.cycle()
        self.assertEqual(self.engine.next_run_at, MON.replace(hour=13, minute=30, second=5))


class ClosedMarketTest(EngineCase):
    def test_closed_market_orders_pending_then_filled_at_open(self):
        self.now = SAT
        self.set_headlines()
        self.engine.set_autopilot(True)
        out = self.cycle()
        self.assertFalse(store.get_run(out["run_id"])["run"]["market_open"])
        pend = store.pending_orders()
        self.assertEqual(sorted(o["symbol"] for o in pend), sorted(SYMS))
        out = self.cycle()                                   # still closed: nothing new, reason says why
        self.assertEqual(len(store.pending_orders()), 2)
        reasons = {r["skip_reason"] for r in store.get_run(out["run_id"])["recommendations"]}
        self.assertEqual(reasons, {"order already pending (executes at the next open)"})
        self.assertEqual(self.qty("SPY"), 0)
        st = self.engine.state()
        self.assertEqual(len(st["pending_orders"]), 2)
        self.assertFalse(st["market"]["open"])
        self.assertTrue(any("pending" in e["title"] for e in store.events_after(0) if e["kind"] == "order"))
        self.now = MON
        self.set_headlines()
        self.engine.touch_client()          # the app is watching (else the dead-man switch cancels them)
        self.cycle()
        self.assertEqual(store.pending_orders(), [])
        self.assertEqual(self.qty("SPY"), 9)
        self.assertTrue(any("at the open" in e["title"] for e in store.events_after(0)))


class ManualAndResetTest(EngineCase):
    def test_execute_recommendation_manual_accept(self):
        self.cycle()
        rec = store.latest_recommendations()["SPY"]
        res = asyncio.run(self.engine.execute_recommendation(rec["id"]))
        self.assertEqual([(r["side"], r["status"], r["origin"]) for r in res], [("buy", "filled", "manual-accept")])
        self.assertEqual(store.get_recommendation(rec["id"])["status"], "executed")
        with self.assertRaises(EngineError) as cm:
            asyncio.run(self.engine.execute_recommendation(rec["id"]))
        self.assertEqual(cm.exception.status, 409)
        with self.assertRaises(EngineError) as cm:
            asyncio.run(self.engine.execute_recommendation(99999))
        self.assertEqual(cm.exception.status, 404)
        self.assertIn("execute_recommendation", [a["action"] for a in store.list_audit(10)])

    def test_manual_order_validation(self):
        with self.assertRaises(EngineError) as cm:
            asyncio.run(self.engine.manual_order({"symbol": "??", "side": "hold", "qty": 1, "notional": 5}))
        self.assertEqual(set(cm.exception.errors), {"symbol", "side", "qty"})
        r = asyncio.run(self.engine.manual_order({"symbol": "QQQ", "side": "buy", "notional": 4000}))
        self.assertEqual((r["status"], r["qty"], r["origin"]), ("filled", 10, "manual"))

    def test_reset(self):
        self.engine.set_autopilot(True)
        self.cycle()
        res = asyncio.run(self.engine.reset(50000))
        self.assertEqual(res["account"]["equity"], 50000.0)
        self.assertEqual(store.get_positions(), [])
        self.assertFalse(store.get_autopilot())
        with self.assertRaises(EngineError) as cm:
            asyncio.run(self.engine.reset(5))
        self.assertEqual(cm.exception.status, 400)
        self.assertEqual(store.list_audit(1)[0]["action"], "reset")

    def test_equity_benchmark_scaled_since_reset(self):
        self.now = FRI - timedelta(minutes=1)
        asyncio.run(self.engine.reset(100000))
        self.now = FRI
        self.cycle()
        self.feed.quotes["SPY"] = 550.0
        self.now = FRI + timedelta(minutes=5)
        self.cycle()
        eq = self.engine.equity()
        self.assertEqual(eq["starting_capital"], 100000.0)
        bench = [p["benchmark_equity"] for p in eq["points"]]
        self.assertEqual(bench[0], None)                          # reset point has no SPY price
        self.assertAlmostEqual(bench[1], 100000.0)
        self.assertAlmostEqual(bench[2], 110000.0)


class ErrorHandlingTest(EngineCase):
    def test_symbol_without_data_makes_run_partial(self):
        self.engine.update_params({"watchlist": ["SPY", "QQQ", "NODATA"]})
        out = self.cycle()
        self.assertEqual(out["status"], "partial")
        self.assertIn("NODATA", out["error"])
        self.assertEqual(sorted(r["symbol"] for r in store.get_run(out["run_id"])["recommendations"]), sorted(SYMS))

    def test_no_market_data_at_all_is_skipped(self):
        self.feed.bars.clear()
        self.feed.quotes.clear()
        out = self.cycle()
        self.assertEqual(out["status"], "skipped")
        self.assertEqual(store.last_run()["status"], "skipped")
        self.assertEqual(self.fake.calls, [])

    def test_feed_exceptions_do_not_crash(self):
        def boom(*a, **k):
            raise RuntimeError("network down")
        self.feed.quote = boom
        self.feed.daily_bars = boom
        out = self.cycle()
        self.assertEqual(out["status"], "skipped")

    def test_crash_marks_run_error_and_next_cycle_works(self):
        orig = self.engine._cycle

        async def crash(*a, **k):
            raise RuntimeError("boom")
        self.engine._cycle = crash
        with self.assertLogs("fund.engine", level="ERROR"):
            out = self.cycle()
        self.assertEqual(out["status"], "error")
        self.assertEqual(store.last_run()["status"], "error")
        self.assertIn("boom", store.last_run()["error"])
        self.assertTrue(any(e["kind"] == "error" for e in store.events_after(0)))
        self.assertFalse(self.engine.cycle_in_progress)
        self.engine._cycle = orig
        self.assertEqual(self.cycle()["status"], "ok")


class StateTest(EngineCase):
    SPEC_TOP = {"now", "market", "engine", "autopilot", "halted", "params_version", "pending_params", "ai_budget",
                "account", "positions", "pending_orders", "symbols"}
    SPEC_ACCOUNT = {"equity", "cash", "long_mv", "short_mv", "buying_power", "day_pnl", "total_pnl", "total_pnl_pct",
                    "peak_equity", "drawdown_pct", "drawdown_stop_pct", "short_exposure_pct", "maintenance_required",
                    "margin_excess", "starting_capital", "reset_at"}
    SPEC_POSITION = {"symbol", "qty", "avg_price", "last_price", "market_value", "weight", "unrealized_pnl",
                     "realized_pnl", "opened_at", "side", "borrow_rate_pct"}
    SPEC_REC = {"id", "action", "confidence", "target_weight", "rule_score", "rationale", "source", "model",
                "provenance", "usage", "status", "skip_reason", "ts", "run_id", "current_weight"}

    def test_state_shape_matches_spec(self):
        st0 = self.engine.state()                       # before any cycle
        self.assertTrue(self.SPEC_TOP <= set(st0))
        self.assertEqual(st0["symbols"][0]["agents"], [])
        self.engine.set_autopilot(True)
        self.cycle()
        st = self.engine.state()
        self.assertTrue(self.SPEC_TOP <= set(st))
        self.assertEqual(set(st["market"]), {"open", "session", "next_open", "next_close", "trading_day",
                                             "delayed_quotes"})
        self.assertTrue({"running", "cycle_in_progress", "next_run_at", "interval_minutes", "last_run"}
                        <= set(st["engine"]))
        self.assertTrue({"id", "started_at", "finished_at", "status", "judge_mode", "cost_usd", "error"}
                        <= set(st["engine"]["last_run"]))
        self.assertEqual(set(st["halted"]), {"active", "reason", "at"})
        self.assertEqual(set(st["ai_budget"]), {"limit_usd", "spent_today_usd", "exhausted", "spend_unreadable"})
        self.assertFalse(st["ai_budget"]["spend_unreadable"])
        self.assertIn("last_client_seen", st)
        self.assertEqual(st["engine"]["autopilot_heartbeat_minutes"], 15)
        self.assertIsNotNone(st["engine"]["autopilot_off_at"])        # autopilot on + client seen
        self.assertFalse(st["account"]["unreadable"])
        self.assertTrue(self.SPEC_ACCOUNT <= set(st["account"]))
        self.assertEqual(len(st["positions"]), 2)
        self.assertTrue(self.SPEC_POSITION <= set(st["positions"][0]))
        self.assertEqual([s["symbol"] for s in st["symbols"]], SYMS)
        sym = st["symbols"][0]
        self.assertEqual(set(sym["quote"]), {"price", "prev_close", "change_pct", "delayed", "ts"})
        self.assertEqual([a["agent"] for a in sym["agents"]], ["technical", "macro", "sentiment", "risk"])
        for a in sym["agents"]:
            self.assertTrue({"agent", "score", "confidence", "summary", "details", "source", "provenance"} <= set(a))
        self.assertEqual(sym["agents"][2]["source"], "llm")
        self.assertTrue(sym["agents"][2]["provenance"]["ai_generated"])
        self.assertTrue(self.SPEC_REC <= set(sym["recommendation"]))
        self.assertAlmostEqual(sym["recommendation"]["current_weight"], st["positions"][1]["weight"])
        self.assertEqual(st["engine"]["interval_minutes"], 5)
        self.assertTrue(st["autopilot"])

    def test_state_reads_agents_from_store_after_restart(self):
        self.cycle()
        fresh = FundEngine(self.feed, self.broker, store, clock=lambda: self.now)
        st = fresh.state()
        self.assertEqual(len(st["symbols"][0]["agents"]), 4)
        self.assertIsNotNone(st["symbols"][0]["recommendation"])

    def test_no_nan_in_state_json(self):
        self.feed.quotes["SPY"] = Quote(symbol="SPY", price=500.0, prev_close=float("nan"))
        self.cycle()
        st = self.engine.state()
        text = json.dumps(st, allow_nan=False)            # raises on NaN/Infinity
        self.assertIn('"change_pct": null', text)
        self.assertEqual(json_safe({"a": float("inf"), "b": [float("-inf"), 1.5]}), {"a": None, "b": [None, 1.5]})


class _Patch:
    """Temporarily replaces an attribute (module function / bound method)."""

    def __init__(self, obj, name, value):
        self.obj, self.name, self.value = obj, name, value

    def __enter__(self):
        self.old = getattr(self.obj, self.name)
        setattr(self.obj, self.name, self.value)
        return self

    def __exit__(self, *exc):
        setattr(self.obj, self.name, self.old)
        return False


class ReviewBudgetAndStateTest(EngineCase):
    """Review 2026-09-26: fail-closed AI budget, unreadable account state."""

    def test_unreadable_ai_spend_means_no_llm_calls(self):
        with _Patch(store, "ai_spend_today", lambda now=None: None):
            out = self.cycle()
            self.assertTrue(self.engine.state()["ai_budget"]["spend_unreadable"])
            self.assertTrue(self.engine.state()["ai_budget"]["exhausted"])
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(store.get_run(out["run_id"])["run"]["judge_mode"], "rules")

    def test_unreadable_account_never_liquidates(self):
        asyncio.run(self.engine.manual_order({"symbol": "SPY", "side": "buy", "qty": 150}))
        self.cycle()                                        # peak ~100k
        self.engine.set_autopilot(True)
        self.feed.quotes["SPY"] = 400.0                     # 15 % drawdown — but the state is unreadable
        margin_calls = []

        def broken(*a, **kw):
            raise RuntimeError("cash ledger unreadable")

        with _Patch(self.broker, "account", broken), \
                _Patch(self.broker, "check_margin", lambda *a, **kw: margin_calls.append(1) or []):
            out = self.cycle()
            st = self.engine.state()
        self.assertEqual(out["status"], "partial")
        self.assertIn("account state unreadable", out["error"])
        self.assertFalse(store.get_halted()["active"])
        self.assertEqual(self.qty("SPY"), 150)
        self.assertEqual(margin_calls, [])
        self.assertEqual([o for o in store.list_orders() if o["origin"] != "manual"], [])
        recs = store.get_run(out["run_id"])["recommendations"]
        self.assertEqual({r["skip_reason"] for r in recs}, {"account state unreadable"})
        self.assertTrue(st["account"]["unreadable"])
        self.assertIsNone(st["account"]["equity"])
        json.dumps(json_safe(st), allow_nan=False)
        # AccountView without equity counts as unreadable too
        import dataclasses
        real = self.broker.account
        with _Patch(self.broker, "account", lambda *a, **kw: dataclasses.replace(real(*a, **kw), equity=None)):
            out = self.cycle()
        self.assertIn("account state unreadable", out["error"])
        self.assertFalse(store.get_halted()["active"])
        # readable again -> the drawdown stop works as before
        self.engine.touch_client()
        self.cycle()
        self.assertTrue(store.get_halted()["active"])


class ReviewBrokerStateUnavailableTest(EngineCase):
    """Integration with broker.StateUnavailable (store read -> None)."""

    def test_unreadable_cash_ledger_skips_trading_and_stops(self):
        asyncio.run(self.engine.manual_order({"symbol": "SPY", "side": "buy", "qty": 150}))
        self.cycle()
        self.engine.set_autopilot(True)
        self.feed.quotes["SPY"] = 400.0
        n_points = len(store.equity_points())
        # the broker reads store.account_snapshot() (newer store) or store.cash_balance() -> None = unreadable
        name = "account_snapshot" if hasattr(store, "account_snapshot") else "cash_balance"
        with _Patch(store, name, lambda *a, **kw: None):
            out = self.cycle()
            st = self.engine.state()
        self.assertEqual(out["status"], "partial")
        self.assertIn("account state unreadable", out["error"])
        self.assertFalse(store.get_halted()["active"])
        self.assertEqual(self.qty("SPY"), 150)
        self.assertEqual([o for o in store.list_orders() if o["origin"] != "manual"], [])
        self.assertEqual(len(store.equity_points()), n_points)          # no snapshot from a broken state
        self.assertTrue(st["account"]["unreadable"])


class ReviewHeartbeatTest(EngineCase):
    """Dead-man switch: autopilot off without client heartbeat."""

    def test_heartbeat_switches_autopilot_off(self):
        self.engine.set_autopilot(True)                     # the user is here: heartbeat starts
        self.now = FRI + timedelta(minutes=14)
        self.assertFalse(self.engine.check_heartbeat())
        self.engine.touch_client()
        self.now = FRI + timedelta(minutes=28)
        self.assertFalse(self.engine.check_heartbeat())
        self.assertTrue(self.engine.state()["autopilot"])
        self.assertEqual(self.engine.state()["last_client_seen"], (FRI + timedelta(minutes=14)).isoformat())
        self.now = FRI + timedelta(minutes=29)
        self.assertTrue(self.engine.check_heartbeat())
        self.assertFalse(store.get_autopilot())
        ev = [e for e in store.events_after(0) if e["kind"] == "autopilot"][-1]
        self.assertIn("no app heartbeat for 15 min", ev["title"])
        self.assertIn("dead-man switch", ev["body"])
        audit = [a for a in store.list_audit(20) if a["action"] == "autopilot_heartbeat_off"]
        self.assertEqual(audit[0]["actor"], "engine")
        self.assertFalse(self.engine.check_heartbeat())     # already off: nothing more
        self.assertIsNone(self.engine.state()["engine"]["autopilot_off_at"])

    def test_cycle_without_client_places_no_orders(self):
        self.engine.set_autopilot(True)
        self.now = FRI + timedelta(minutes=20)
        self.set_headlines()
        self.cycle()
        self.assertFalse(store.get_autopilot())
        self.assertEqual(store.list_orders(), [])

    def test_heartbeat_minutes_param_and_never_seen(self):
        self.engine.update_params({"autopilot_heartbeat_minutes": 60})
        self.engine.set_autopilot(True)
        self.now = FRI + timedelta(minutes=45)
        self.assertFalse(self.engine.check_heartbeat())
        # autopilot on without any client ever seen (e.g. flag set elsewhere) -> off
        eng = FundEngine(self.feed, self.broker, store, clock=lambda: self.now, startup_delay=0.0)
        self.assertTrue(eng.check_heartbeat())
        self.assertFalse(store.get_autopilot())


class ReviewAutopilotOffCancelsTest(EngineCase):
    """Autopilot off (heartbeat or user) cancels queued autopilot orders only."""

    def _queue(self):
        self.now = SAT
        self.set_headlines()
        asyncio.run(self.engine.manual_order({"symbol": "QQQ", "side": "buy", "qty": 3}))   # manual, pending
        self.engine.set_autopilot(True)
        self.cycle()
        pend = store.pending_orders()
        # QQQ: the manual pending order blocks the autopilot ("manual order pending"); SPY: autopilot queued
        self.assertEqual(sorted((o["origin"], o["symbol"]) for o in pend), [("autopilot", "SPY"), ("manual", "QQQ")])

    def _check_only_manual_left(self):
        pend = store.pending_orders()
        self.assertEqual([(o["origin"], o["symbol"]) for o in pend], [("manual", "QQQ")])
        cancelled = [o for o in store.list_orders() if o["status"] == "cancelled"]
        self.assertEqual([(o["origin"], o["symbol"]) for o in cancelled], [("autopilot", "SPY")])

    def test_heartbeat_cancels_queued_autopilot_orders(self):
        self._queue()
        self.now = SAT + timedelta(minutes=20)
        self.assertTrue(self.engine.check_heartbeat())
        self._check_only_manual_left()
        ev = [e for e in store.events_after(0) if e["kind"] == "autopilot"][-1]
        self.assertIn("1 queued autopilot order cancelled (BUY", ev["body"])
        audit = [a for a in store.list_audit(30) if a["action"] == "autopilot_heartbeat_off"][0]
        self.assertEqual(len(audit["detail"]["cancelled_orders"]), 1)

    def test_user_switch_off_cancels_queued_autopilot_orders(self):
        self._queue()
        self.assertEqual(self.engine.set_autopilot(False), {"autopilot": False})
        self._check_only_manual_left()
        ev = [e for e in store.events_after(0) if e["kind"] == "autopilot"][-1]
        self.assertEqual(ev["title"], "Autopilot off")
        self.assertIn("1 queued autopilot order cancelled (BUY", ev["body"])
        audit = [a for a in store.list_audit(30) if a["action"] == "autopilot" and a["actor"] == "user"][0]
        self.assertEqual(len(audit["detail"]["cancelled_orders"]), 1)
        # at the open only the manual order fills
        self.now = MON
        self.set_headlines()
        self.cycle()
        self.assertEqual(self.qty("QQQ"), 3)
        self.assertEqual(self.qty("SPY"), 0)


class ReviewKillRaceTest(EngineCase):
    def test_kill_during_autopilot_placement_is_ordered(self):
        import threading
        import time
        self.engine.set_autopilot(True)
        orig = self.broker.target
        seen = {"thread": None, "halted_during_target": None}

        def target(*a, **kw):
            if seen["thread"] is None:
                seen["thread"] = threading.Thread(target=lambda: asyncio.run(self.engine.kill("race")))
                seen["thread"].start()
                time.sleep(0.3)              # the kill switch has to wait for this placement
                seen["halted_during_target"] = store.get_halted()["active"]
            return orig(*a, **kw)

        with _Patch(self.broker, "target", target):
            out = self.cycle()
        seen["thread"].join(10)
        self.assertFalse(seen["halted_during_target"])
        orders = store.list_orders()
        auto = [o["id"] for o in orders if o["origin"] == "autopilot"]
        kill = [o["id"] for o in orders if o["origin"] == "kill-switch"]
        self.assertEqual(len(auto), 1)                       # the second symbol was not traded after the kill
        self.assertTrue(kill)
        self.assertLess(max(auto), min(kill))                # the liquidation came after the only autopilot order
        self.assertEqual(store.get_positions(), [])
        reasons = [r["skip_reason"] for r in store.get_run(out["run_id"])["recommendations"]]
        self.assertIn("engine halted", reasons)

    def test_manual_buy_refused_after_kill(self):
        asyncio.run(self.engine.kill("test"))
        with self.assertRaises(EngineError) as cm:
            asyncio.run(self.engine.manual_order({"symbol": "SPY", "side": "short", "qty": 1}))
        self.assertEqual(cm.exception.status, 409)


class ReviewAuditTest(EngineCase):
    def test_autopilot_orders_are_audited(self):
        self.engine.set_autopilot(True)
        out = self.cycle()
        rows = [a for a in store.list_audit(50) if a["action"] == "autopilot_order"]
        self.assertEqual(len(rows), 2)
        recs = {r["symbol"]: r["id"] for r in store.get_run(out["run_id"])["recommendations"]}
        for a in rows:
            d = a["detail"]
            self.assertEqual((a["actor"], d["stage"]), ("engine", "placed"))
            self.assertEqual(d["recommendation_id"], recs[d["symbol"]])
            self.assertTrue(d["order_id"] and d["side"] == "buy" and d["qty"] > 0)

    def test_fills_at_the_open_are_audited(self):
        self.now = SAT
        self.set_headlines()
        self.engine.set_autopilot(True)
        self.cycle()
        self.now = MON
        self.set_headlines()
        self.engine.touch_client()
        self.cycle()
        stages = [a["detail"]["stage"] for a in store.list_audit(100) if a["action"] == "autopilot_order"]
        self.assertEqual(stages.count("filled_at_open"), 2)
        self.assertEqual(stages.count("placed"), 2)

    def test_user_actions_refused_without_audit_log(self):
        v0 = store.effective_params()[0]
        with _Patch(store, "add_audit", lambda *a, **kw: None):
            for fn in (lambda: self.engine.set_autopilot(True),
                       lambda: asyncio.run(self.engine.manual_order({"symbol": "SPY", "side": "buy", "qty": 1})),
                       lambda: self.engine.update_params({"min_confidence": 80}),
                       lambda: self.engine.request_run("user"),
                       lambda: asyncio.run(self.engine.reset(50000))):
                with self.assertRaises(EngineError) as cm:
                    fn()
                self.assertEqual(cm.exception.status, 503)
                self.assertIn("audit log unavailable", cm.exception.message)
            self.assertFalse(store.get_autopilot())
            self.assertEqual(store.list_orders(), [])
            self.assertEqual(store.effective_params()[0], v0)
            # the kill switch and switching the autopilot off always work
            self.assertEqual(self.engine.set_autopilot(False), {"autopilot": False})
            res = asyncio.run(self.engine.kill("audit down"))
            self.assertTrue(res["halted"])
            self.assertTrue(store.get_halted()["active"])
            with self.assertRaises(EngineError) as cm:
                self.engine.resume()
            self.assertEqual(cm.exception.status, 503)
            self.assertTrue(store.get_halted()["active"])


class ReviewExecuteAndLimitsTest(EngineCase):
    def test_only_the_latest_recommendation_can_be_executed(self):
        self.cycle()
        old = store.latest_recommendations()["SPY"]
        self.cycle()                                          # carried -> a newer row
        new = store.latest_recommendations()["SPY"]
        self.assertNotEqual(old["id"], new["id"])
        store.update_recommendation_status(old["id"], "skipped", "test")   # even a skipped old one
        with self.assertRaises(EngineError) as cm:
            asyncio.run(self.engine.execute_recommendation(old["id"]))
        self.assertEqual(cm.exception.status, 409)
        self.assertIn("superseded by a newer recommendation", cm.exception.message)
        res = asyncio.run(self.engine.execute_recommendation(new["id"]))
        self.assertEqual(res[0]["origin"], "manual-accept")

    def test_flip_with_one_trade_left_only_closes(self):
        store.save_params(dict(store.effective_params()[1], max_trades_per_day=3), "test")
        self.engine.set_autopilot(True)
        self.cycle()                                          # 2 buys
        self.fake.action = "SHORT"
        self.engine.update_params({"min_confidence": 76})     # forces a re-judgement
        out = self.cycle()
        auto = [o for o in store.list_orders() if o["origin"] == "autopilot" and o["status"] == "filled"]
        self.assertEqual(len(auto), 3)                        # never more than the daily limit
        qtys = sorted(self.qty(s) for s in SYMS)
        self.assertEqual(qtys[0], 0)                          # closed, not reversed
        self.assertGreater(qtys[1], 0)                        # no order left for the other one
        reasons = {r["skip_reason"] for r in store.get_run(out["run_id"])["recommendations"]}
        self.assertEqual(reasons, {"daily trade limit: only the close fits today", "daily trade limit"})

    def test_backtest_caps_and_single_flight(self):
        with self.assertRaises(EngineError) as cm:
            asyncio.run(self.engine.backtest({"symbols": [f"S{i}" for i in range(11)]}))
        self.assertEqual((cm.exception.status, list(cm.exception.errors)), (400, ["symbols"]))
        with self.assertRaises(EngineError) as cm:
            asyncio.run(self.engine.backtest({"symbols": ["SPY"], "start": "2000-01-01", "end": "2026-10-01"}))
        self.assertIn("at most 20 years", cm.exception.errors["start"])
        wl = [f"X{i}" for i in range(12)]
        syms, _s, _e = self.engine._validate_backtest({}, {"watchlist": wl})
        self.assertEqual(syms, wl[:10])
        # one at a time
        import time
        import types as _types
        real = self.feed.daily_bars

        def slow(sym, days=400):
            time.sleep(0.2)
            return real(sym, days)

        self.engine.backtest_feed = _types.SimpleNamespace(daily_bars=slow)
        body = {"symbols": ["SPY"], "start": "2026-03-01", "end": "2026-10-01"}

        async def both():
            return await asyncio.gather(self.engine.backtest(dict(body)), self.engine.backtest(dict(body)),
                                        return_exceptions=True)

        res = asyncio.run(both())
        errs = [r for r in res if isinstance(r, EngineError)]
        oks = [r for r in res if isinstance(r, dict)]
        self.assertEqual(len(oks), 1, res)
        self.assertEqual((errs[0].status, errs[0].message[:31]), (409, "a backtest is already running —"))
        self.assertFalse(self.engine._backtest_running)
        self.assertIn("id", asyncio.run(self.engine.backtest(dict(body))))   # free again

    def test_huge_manual_qty_is_a_400(self):
        with self.assertRaises(EngineError) as cm:
            asyncio.run(self.engine.manual_order({"symbol": "SPY", "side": "buy", "qty": 10 ** 400}))
        self.assertEqual((cm.exception.status, list(cm.exception.errors)), (400, ["qty"]))


if __name__ == "__main__":
    unittest.main()
