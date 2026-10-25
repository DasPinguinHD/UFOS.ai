"""Stage 10 S10-C — the scenario part of the Council parity gate (SP §22.10): each scenario drives the old engine and
the Council in lockstep from the REAL §17.9 migration (``council_parity_testkit``), asserts parity (signals, verdicts,
target weights, executions with the fixed skip-reason mapping, order intents, AI call logs, AI cost, books) AND that
the scenario really happened (its reason text / its orders). Plus the "no wall-clock read" check and the frozen
real-data fixture. Offline: fake market, scripted fake LLM — no network."""
import json
import unittest
from datetime import datetime, timedelta, timezone

try:
    import council_parity_testkit as kit
    from sp_trading_testkit import TradingCase
except ImportError:  # pragma: no cover
    from tests import council_parity_testkit as kit
    from tests.sp_trading_testkit import TradingCase

from fund import store as fund_store
from strategy_platform.core import llm as llm_mod
from strategy_platform.core.llm import LlmUnavailable
from strategy_platform.council import store as cs
from strategy_platform.execution import accounts
from strategy_platform.storage import db

UTC = timezone.utc
START = datetime(2026, 10, 5, 13, 0, tzinfo=UTC)          # Mon 09:00 NY
TICKS = kit.lockstep_ticks(START.date(), 1)               # Mon in-session ticks after the grace


def verdicts(action_of, conf=0.95):
    """A judge policy: ``action_of(symbol) -> LONG|SHORT|FLAT`` with a fixed confidence (sentiment: default)."""
    def policy(feature, syms, h):
        if feature != "fund-judge":
            return None
        return [{"symbol": s, "action": action_of(s), "confidence": conf, "rationale": f"Scenario verdict {s}."}
                for s in syms]
    return policy


class ScenarioCase(TradingCase):
    open_main = False
    now = START

    def harness(self, **kw):
        self.h = kit.Harness(self, **kw)
        return self.h

    def run_ticks(self, ticks, off_window=False, touch=True):
        recs, mism = [], []
        for t in ticks:
            rec = self.h.tick(t, touch=touch)
            recs.append(rec)
            mism += self.h.compare_tick(rec, off_window=off_window)
        return recs, mism

    def assert_parity(self, mism, books=True):
        self.assertEqual(self.h.llm.misses, [])
        self.assertEqual((mism + (self.h.compare_books() if books else []))[:20], [])

    def texts(self, recs):
        """Every Council reason of these ticks: execution reasons, unavailable reasons, signal summaries, errors."""
        out = []
        for r in recs:
            d = cs.run_detail(r["new"]["run_id"])
            out += [x["reason"] or "" for x in d["executions"]]
            out += [x["unavailable_reason"] or "" for x in d["decisions"]]
            out += [x["summary"] or "" for x in d["signals"]]
            out += [json.dumps((d.get("summary") or {}).get("errors") or {})]
        return out

    @staticmethod
    def decision(rec, sym):
        return next(x for x in cs.run_detail(rec["new"]["run_id"])["decisions"] if x["symbol"] == sym)

    def assert_seen(self, recs, needle):
        texts = self.texts(recs)
        self.assertTrue(any(needle in t for t in texts), f"scenario not reached: {needle!r} not in {texts[:12]}")


class JudgeUnavailableTest(ScenarioCase):
    def test_budget_reached(self):
        self.harness(old_params={"ai_daily_budget_usd": 0.002})
        recs, mism = self.run_ticks(TICKS[:3])
        self.assert_parity(mism)
        self.assert_seen(recs, llm_mod.BUDGET_REACHED_MESSAGE)

    def test_unreadable_spend(self):
        self.harness()
        with fund_store._connect() as conn:
            conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = "
                         "excluded.value", (fund_store.META_AI_SPEND, "not json"))
        db.meta_set(cs.META_SPEND, "not json")
        recs, mism = self.run_ticks(TICKS[:2])
        self.assert_parity(mism)
        self.assertEqual(self.h.llm.logs, {"old": [], "new": []})      # fails closed on both paths

    def test_timeout_charges_the_estimate(self):
        self.harness()
        self.h.llm.fail["fund-judge"] = LlmUnavailable("error", "Request timed out.", attempted=True)
        recs, mism = self.run_ticks(TICKS[:2])
        self.assert_parity(mism)
        self.assert_seen(recs, "Request timed out")
        self.assertGreater(recs[0]["new"]["cost_usd"], 0.0)             # the pre-call estimate is booked


class ModelModeTest(ScenarioCase):
    def test_premium_on_flip_incl_failing_premium(self):
        # judge_stale_hours 1 + ticks 60 min apart: every tick re-judges; the scripted verdict flips each time
        self.harness(old_params={"judge_model_mode": "premium_on_flip", "judge_stale_hours": 1})
        premium = llm_mod.models()["premium"]
        state = {"act": "LONG"}
        self.h.llm.policy = verdicts(lambda s: state["act"])
        recs, mism = self.run_ticks(TICKS[0:1])
        state["act"] = "SHORT"                                           # every symbol flips → premium re-judge
        recs2, mism2 = self.run_ticks(TICKS[2:3])
        self.assertIn(premium, {c[1] for c in recs2[0]["calls_new"]})
        state["act"] = "LONG"
        self.h.llm.fail_models[premium] = LlmUnavailable("error", "premium model down")
        recs3, mism3 = self.run_ticks(TICKS[4:5])
        self.assert_parity(mism + mism2 + mism3)
        self.assertIn(premium, {c[1] for c in recs3[0]["calls_new"]})
        self.assert_seen(recs3, "premium model down")


class ShortRulesTest(ScenarioCase):
    def test_not_shortable_and_short_exposure_cap(self):
        self.harness(old_params={"short_exposure_max_pct": 3.0})
        self.h.llm.policy = verdicts(lambda s: "SHORT")
        recs, mism = self.run_ticks(TICKS[:2])
        self.assert_parity(mism)
        self.assertEqual(self.decision(recs[0], "XLE")["action"], "FLAT")      # not shortable: SHORT vetoed
        self.assert_seen(recs, "short exposure limit")

    def test_shorts_off(self):
        self.harness(old_params={"allow_shorts": False})
        self.h.llm.policy = verdicts(lambda s: "SHORT")
        recs, mism = self.run_ticks(TICKS[:1])
        self.assert_parity(mism)
        acts = {x["action"] for x in cs.run_detail(recs[0]["new"]["run_id"])["decisions"] if x["action"]}
        self.assertEqual(acts, {"FLAT"})                                 # every SHORT verdict vetoed


class SizingRulesTest(ScenarioCase):
    def test_min_confidence(self):
        self.harness(old_params={"min_confidence": 90})
        self.h.llm.policy = verdicts(lambda s: "LONG", conf=0.8)
        recs, mism = self.run_ticks(TICKS[:1])
        self.assert_parity(mism)
        self.assert_seen(recs, "below minimum 90%")

    def test_min_hold_days(self):
        self.harness(old_params={"min_hold_days": 3})
        state = {"act": "LONG"}
        self.h.llm.policy = verdicts(lambda s: state["act"])
        recs, mism = self.run_ticks(TICKS[:1])
        state["act"] = "SHORT"
        recs2, mism2 = self.run_ticks(TICKS[1:2])
        self.assert_parity(mism + mism2)
        self.assert_seen(recs2, "hold")

    def test_max_trades_per_day(self):
        self.harness(old_params={"max_trades_per_day": 2})
        self.h.llm.policy = verdicts(lambda s: "LONG")
        recs, mism = self.run_ticks(TICKS[:2])
        self.assert_parity(mism)
        self.assert_seen(recs, "daily trade limit")

    def test_buying_power_partial_fill(self):
        self.harness(old_params={"max_position_pct": 15.0, "crypto_max_position_pct": 15.0,
                                 "initial_margin_pct": 100.0})
        self.h.llm.policy = verdicts(lambda s: "LONG")
        recs, mism = self.run_ticks(TICKS[:1])
        self.assert_parity(mism)
        orders = self.h.new_orders(TICKS[0])
        self.assertGreaterEqual(len(orders), 4)


class ReversalTest(ScenarioCase):
    def test_two_leg_reversal_in_session(self):
        self.harness(old_params={"max_position_pct": 10.0})
        self.h.llm.policy = verdicts(lambda s: {"SPY": "SHORT", "BTC-USD": "SHORT", "TLT": "LONG"}.get(s, "FLAT"))
        recs, mism = self.run_ticks(TICKS[:1])
        self.assert_parity(mism)
        sides = {(o[0], o[1]) for o in self.h.new_orders(TICKS[0])}
        self.assertTrue({("SPY", "sell"), ("SPY", "short")} <= sides, sides)

    def test_crypto_reversal_while_closed(self):
        self.harness()
        self.h.llm.policy = verdicts(lambda s: "SHORT" if s == "BTC-USD" else "FLAT")
        b = kit.calendars.session_bounds(START.date())
        t = b[1] + timedelta(hours=2)
        recs, mism = self.run_ticks([t], off_window=True)
        self.assert_parity(mism, books=False)                            # equity orders queue only on the old path
        sides = {(o[0], o[1]) for o in self.h.new_orders(t)}
        self.assertIn(("BTC-USD", "sell"), sides)


class GateTest(ScenarioCase):
    def test_halted_account(self):
        self.harness()
        fund_store.set_halted(True, "scenario")
        accounts.get(accounts.COUNCIL).set_halt(True, "kill", "scenario")
        recs, mism = self.run_ticks(TICKS[:1])
        self.assert_parity(mism)

    def test_kill_mid_cycle(self):
        self.harness()
        self.h.llm.policy = verdicts(lambda s: "SHORT")

        def kill(path, feature):
            if feature != "fund-judge":
                return
            if path == "old":
                fund_store.set_halted(True, "kill mid-cycle")
            else:
                accounts.get(accounts.COUNCIL).set_halt(True, "kill", "kill mid-cycle")
        self.h.llm.on_call = kill
        recs, mism = self.run_ticks(TICKS[:1])
        self.assert_parity(mism)
        self.assertEqual(self.h.new_orders(TICKS[0]), [])
        self.assert_seen(recs, "the account is halted")

    def test_council_switch_off_mid_cycle(self):
        self.harness()
        self.h.llm.policy = verdicts(lambda s: "SHORT")

        def off(path, feature):
            if feature != "fund-judge":
                return
            if path == "old":
                fund_store.set_autopilot(False)
            else:
                cs.set_orders_enabled(False, "user", "scenario")
        self.h.llm.on_call = off
        recs, mism = self.run_ticks(TICKS[:1])
        self.assert_parity(mism)
        self.assertEqual(self.h.new_orders(TICKS[0]), [])

    def test_dead_man(self):
        self.harness()
        self.h.llm.policy = verdicts(lambda s: "SHORT")
        recs, mism = self.run_ticks(TICKS[:1])
        recs2, mism2 = self.run_ticks(TICKS[1:3], touch=False)          # no client for 60 min > 15 min heartbeat
        self.assert_parity(mism + mism2)
        self.assert_seen(recs2[1:], "auto trading is off")

    def test_drawdown_stop(self):
        self.harness(seed_orders=(("SPY", "buy", 120), ("QQQ", "buy", 100), ("BTC-USD", "buy", 0.5)))
        recs, mism = self.run_ticks(TICKS[:1])
        for s in ("SPY", "QQQ", "BTC-USD"):
            self.h.world.shock[s] = 0.7
        recs2, mism2 = self.run_ticks(TICKS[1:2])
        self.assert_parity(mism + mism2)
        self.assertTrue(fund_store.get_halted()["active"])
        self.assertTrue(accounts.get(accounts.COUNCIL).halt()["active"])

    def test_valuation_paused(self):
        self.harness()
        self.h.world.fail_quotes.add("SPY")                             # a held position without a quote
        recs, mism = self.run_ticks(TICKS[:1])
        self.assert_parity(mism, books=False)
        self.assert_seen(recs, "valuation paused")


class DataUnavailableTest(ScenarioCase):
    def test_calendar_insider_fred_unavailable(self):
        def boom():
            raise RuntimeError("source down")
        self.harness(calendar_fn=boom, insider_fn=boom, macro_status_fn=lambda: {"ok": False, "message": "FRED down"})
        recs, mism = self.run_ticks(TICKS[:2])
        self.assert_parity(mism)
        self.assert_seen(recs, "calendar accessor failed")
        self.assert_seen(recs, "FRED down")


class BudgetDayAndRestartTest(ScenarioCase):
    def test_budget_across_midnight_utc(self):
        self.harness(old_params={"ai_daily_budget_usd": 0.004})
        late = datetime(2026, 10, 5, 23, 40, tzinfo=UTC)
        recs, mism = self.run_ticks([late, late + timedelta(minutes=10), late + timedelta(minutes=40)],
                                    off_window=True)
        self.assert_parity(mism, books=False)
        self.assertEqual(cs.run_detail(recs[2]["new"]["run_id"])["ai_calls"] != [], True)   # a new UTC day

    def test_spend_restored_after_restart(self):
        self.harness(old_params={"ai_daily_budget_usd": 0.003})
        recs, mism = self.run_ticks(TICKS[:2])
        self.h.restart()
        recs2, mism2 = self.run_ticks(TICKS[2:4])
        self.assert_parity(mism + mism2)
        self.assert_seen(recs2, llm_mod.BUDGET_REACHED_MESSAGE)

    def test_restart_mid_window(self):
        self.harness()
        recs, mism = self.run_ticks(TICKS[:3])
        self.h.restart()                                                 # change-detection memory from the DBs
        recs2, mism2 = self.run_ticks(TICKS[3:6])
        self.assert_parity(mism + mism2)


class WallClockTest(ScenarioCase):
    def test_no_wall_clock_read(self):
        self.harness()
        with kit.WallClockSpy() as spy:
            self.assertTrue(spy.probe())                                 # the spy is live
            recs, mism = self.run_ticks(TICKS[:3])
        self.assert_parity(mism)
        self.assertEqual(spy.violations(), {})


class SyntheticBarsTest(ScenarioCase):
    """The committed fixture: SYNTHETIC bars of the real fixture's shape (licence clean-up 2026-10-25)."""
    now = datetime(2026, 10, 26, 13, 0, tzinfo=UTC)                     # Mon after the fixture's last bar

    def test_synthetic_bars_fixture(self):
        self.harness(world_cls=kit.SyntheticBarsWorld, old_params={"watchlist": kit.REAL_SYMBOLS})
        self.assertEqual(len(self.h.world.daily_bars("SPY", 400)), 260)
        recs, mism = self.run_ticks(kit.lockstep_ticks(self.now.date(), 1)[:6])
        self.assert_parity(mism)
        self.assertGreater(len(self.h.llm.logs["new"]), 0)


@unittest.skipUnless(kit.real_bars_available(), kit.REAL_BARS_MISSING)
class RealDataTest(ScenarioCase):
    """The user's own real bars (local, git-ignored) — skipped with the reason when the file is absent."""
    now = datetime(2026, 10, 26, 13, 0, tzinfo=UTC)                     # Mon after the fixture's last bar

    def test_real_data_fixture(self):
        self.harness(world_cls=kit.RealWorld, old_params={"watchlist": kit.REAL_SYMBOLS})
        self.assertEqual(len(self.h.world.daily_bars("SPY", 400)), 260)
        recs, mism = self.run_ticks(kit.lockstep_ticks(self.now.date(), 1)[:6])
        self.assert_parity(mism)
        self.assertGreater(len(self.h.llm.logs["new"]), 0)


if __name__ == "__main__":
    unittest.main()
