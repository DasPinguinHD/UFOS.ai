"""Stage 10 S10-C R1 — the Council parity gate (SP §22.10): the old engine and the Council on the platform, started
from the REAL §17.9 migration of one old account, run in lockstep on one injected clock (in-session ticks after the
opening grace) and must produce the same signals, verdicts, target weights, order intents, AI call logs, AI cost and
— after the replay — the same cash, positions and equity. Plus the schedule-equivalence test and the D7 scenario.
``UFOS_PARITY_SESSIONS`` (default 2) sets the synthetic window; the full §22.10 window is 60 sessions (run in its
own process, it exceeds a foreground split). Offline: fake market, scripted fake LLM — no network."""
import os
import unittest
from datetime import date, datetime, timedelta, timezone

try:
    import council_parity_testkit as kit
    from sp_trading_testkit import TradingCase
except ImportError:  # pragma: no cover
    from tests import council_parity_testkit as kit
    from tests.sp_trading_testkit import TradingCase

from strategy_platform.core.llm import LlmUnavailable
from strategy_platform.council import service as svc_mod
from strategy_platform.council import store as cs
from strategy_platform.execution import accounts

UTC = timezone.utc
START = datetime(2026, 10, 5, 13, 0, tzinfo=UTC)          # Mon 09:00 NY (before the open)
SESSIONS = int(os.environ.get("UFOS_PARITY_SESSIONS", "2"))


class ParityCase(TradingCase):
    open_main = False
    now = START

    def setUp(self):
        super().setUp()
        self.h = kit.Harness(self)


class LockstepParityTest(ParityCase):
    def test_lockstep_window(self):
        self.assertEqual(self.h.migration["account"].id, accounts.COUNCIL)
        self.assertEqual(self.h.compare_books(), [], "books differ right after the migration")
        mismatches = []
        ticks = kit.lockstep_ticks(START.date(), SESSIONS)
        for t in ticks:
            rec = self.h.tick(t)
            mismatches += self.h.compare_tick(rec)
        mismatches += self.h.compare_books()
        orders = sum(len(self.h.new_orders(r["t"])) for r in self.h.ticks)
        calls = len(self.h.llm.logs["new"])
        report = (f"parity: {len(ticks)} ticks, {orders} Council orders, {calls} AI calls, "
                  f"{len(self.h.llm.misses)} unscripted, {len(mismatches)} mismatches")
        print("\n" + report)
        self.assertEqual(self.h.llm.misses, [], report)
        self.assertEqual(mismatches[:25], [], report)
        self.assertGreater(orders, 0, "the window produced no Council orders — the comparison would be empty")
        self.assertGreater(calls, 0)


class OffWindowScenarioTest(ParityCase):
    def test_d7_equities_in_the_grace_and_closed(self):
        """Decisions are still compared; the equity targets get "market closed — no order placed" (D7)."""
        b = kit.calendars.session_bounds(START.date())
        for t in (b[0] + timedelta(minutes=2), b[1] + timedelta(hours=2)):
            rec = self.h.tick(t)
            d = cs.run_detail(rec["new"]["run_id"])
            ex = {x["decision_id"]: x for x in d["executions"]}
            for dec in d["decisions"]:
                e = ex.get(dec["id"])
                if dec["symbol"] in kit.EQUITIES and e is not None and e["outcome"] != "skipped":
                    self.assertEqual((e["outcome"], e["reason"]), ("not_executed", svc_mod.MARKET_CLOSED))
            for line in self.h.compare_tick(rec):
                self.assertNotIn("decision", line)
                self.assertNotIn("signal", line)
        self.assertEqual(self.h.llm.misses, [])



class JudgeUnavailableScenarioTest(ParityCase):
    """Scenario (SP §22.10): the judge is unavailable on both paths — no key, then garbage JSON — the same AI calls,
    no new recommendation (FUND D9: positions held), no order, the same unavailable texts."""

    def run_scenario(self, err):
        self.h.llm.fail["fund-judge"] = err
        mism = []
        for t in kit.lockstep_ticks(START.date(), 1)[:2]:
            rec = self.h.tick(t)
            mism += self.h.compare_tick(rec)
            d = cs.run_detail(rec["new"]["run_id"])
            self.assertEqual({x["judge_source"] for x in d["decisions"]}, {"unavailable"})
            self.assertEqual(self.h.new_orders(t), [])
        self.assertEqual(mism + self.h.compare_books(), [])
        self.assertEqual(self.h.llm.misses, [])
        return d

    def test_no_key(self):
        d = self.run_scenario(LlmUnavailable("no_key", "OpenRouter API key is not set — enter it in ⚙ Keys & data."))
        self.assertTrue(all("Keys & data" in x["unavailable_reason"] for x in d["decisions"]))

    def test_garbage_json(self):
        d = self.run_scenario(LlmUnavailable("bad_json", "not JSON", usage={"cost_usd": 0.001}, model="m"))
        self.assertTrue(all("invalid" in x["unavailable_reason"] for x in d["decisions"]))


class BudgetZeroScenarioTest(TradingCase):
    """Scenario: AI budget 0 on both paths — no AI call at all, the cycle runs (old D9 rule / D8)."""
    open_main = False
    now = START

    def test_budget_zero(self):
        h = kit.Harness(self, old_params={"ai_daily_budget_usd": 0.0})
        mism = []
        for t in kit.lockstep_ticks(START.date(), 1)[:2]:
            mism += h.compare_tick(h.tick(t))
        self.assertEqual(mism + h.compare_books(), [])
        self.assertEqual(h.llm.logs, {"old": [], "new": []})

class ScheduleEquivalenceTest(unittest.TestCase):
    def test_new_rule_equals_shifted_old_trace(self):
        """For a clock trace, the Council schedule produces open + grace + 5 s, then last start + interval — the old
        engine's in-session trace (open + 5 s, + interval) shifted by the grace (deliberate difference 2)."""
        day = date(2026, 10, 5)
        b = kit.calendars.session_bounds(day)
        old_trace = []
        t = b[0] + timedelta(seconds=5)
        while t < b[1]:
            old_trace.append(t)
            t += timedelta(minutes=30)
        expected = [x + timedelta(minutes=5) for x in old_trace if x + timedelta(minutes=5) < b[1]]
        got, last, now = [], None, b[0] - timedelta(hours=1)
        while True:
            due = svc_mod.next_due(now, 30, last)
            if due is None or due >= b[1]:
                break
            got.append(due)
            last, now = due, due + timedelta(seconds=1)
        self.assertEqual(got, expected)
        self.assertEqual(kit.lockstep_ticks(day, 1), expected)


if __name__ == "__main__":
    unittest.main()
