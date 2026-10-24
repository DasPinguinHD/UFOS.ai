"""Stage 10 S10-C R1 — the Council service (SP §22.1, §22.2, §22.5): one council cycle on paper-council (agents →
judge → sizing → order step through the OMS, origin ``council``), the recording in ``cl_*`` (AI provenance,
submit-time outcomes, AI calls booked with the spend counter in one transaction), the gates (switch-over, auto
trading, Council orders, sandbox), D7 (equities outside the window → "market closed — no order placed", crypto
traded), D8 (an exhausted budget runs the cycle without AI judgements), the schedule (open + grace + 5 s, last
start + interval, nothing outside the session), "Run now" (503 / 409 / orders per class) and D11 (stale Council
crypto orders expire after 60 min). Temp databases, fake feed, fake LLM — no network."""
import asyncio
import json
import unittest
from datetime import date, datetime, timedelta, timezone

try:
    from sp_trading_testkit import TradingCase
    from test_s10_council_core import open_council, switched_over
except ImportError:  # pragma: no cover
    from tests.sp_trading_testkit import TradingCase
    from tests.test_s10_council_core import open_council, switched_over

from strategy_platform.core import llm as llm_mod
from strategy_platform.core.types import Bar
from strategy_platform.council import params as cp
from strategy_platform.council import service as svc_mod
from strategy_platform.council import spend
from strategy_platform.council import store as cs
from strategy_platform.data.market_feed import FakeFeed
from strategy_platform.execution import accounts, control, oms
from strategy_platform.portfolio import ledger
from strategy_platform.storage import db

UTC = timezone.utc
FRI_1100 = datetime(2026, 10, 2, 15, 0, tzinfo=UTC)        # Friday 11:00 NY (EDT) — after the opening grace
FRI_0932 = datetime(2026, 10, 2, 13, 32, tzinfo=UTC)       # Friday 09:32 NY — inside the opening grace
SAT = datetime(2026, 10, 3, 15, 0, tzinfo=UTC)
PRICES = {"SPY": 500.0, "BTC-USD": 60000.0}


def make_bars(last_close, n=300, end=date(2026, 10, 1)):
    start = last_close / 1.001 ** (n - 1)
    d0 = end - timedelta(days=n - 1)
    return [Bar(date=(d0 + timedelta(days=i)).isoformat(), open=start * 1.001 ** i * 0.999,
                high=start * 1.001 ** i * 1.005, low=start * 1.001 ** i * 0.995, close=start * 1.001 ** i, volume=1e6)
            for i in range(n)]


class FakeLLM:
    def __init__(self, action="LONG", confidence=0.9, cost=0.002):
        self.action, self.confidence, self.cost, self.calls = action, confidence, cost, []

    async def __call__(self, messages, model, max_tokens, feature, feature_label=None, temperature=0.2):
        self.calls.append(feature)
        data = [{"symbol": s, "action": self.action, "confidence": self.confidence,
                 "rationale": "Trend and macro agree."} for s in PRICES]
        return data, json.dumps(data), model, {"cost_usd": self.cost, "prompt_tokens": 50, "completion_tokens": 10}


class CouncilCase(TradingCase):
    now = FRI_1100

    def setUp(self):
        super().setUp()
        self.quotes.prices.update(PRICES)
        self.council = open_council(100000.0)
        ledger.attribute(self.council)                 # the deposit → the council-of-ais sub-ledger
        switched_over()
        control._write_autotrade(True, "user")
        control.touch()
        params = dict(cp.defaults(), watchlist=list(PRICES), min_hold_days=0,
                      agent_weights={"technical": 1.0, "macro": 1.0, "sentiment": 0.0})
        cs.add_params(params, "system", "test", "test")
        self.feed = FakeFeed(bars={s: make_bars(p) for s, p in PRICES.items()}, quotes=dict(PRICES))
        self.llm = FakeLLM()
        self.svc = svc_mod.CouncilService(self.feed, clock=self.clock, llm=self.llm, sandbox_fn=lambda: False)

    def run_cycle(self):
        return asyncio.run(self.svc.run("manual"))

    def council_orders(self):
        return [o for o in oms.list_orders(accounts.COUNCIL, limit=100) if o["origin"] == "council"]


class CycleTest(CouncilCase):
    def test_cycle_trades_and_records(self):
        out = self.run_cycle()
        self.assertIn(out["status"], ("ok", "partial"))
        self.assertEqual(out["judge_mode"], "llm")
        orders = self.council_orders()
        self.assertEqual({o["symbol"] for o in orders}, {"SPY", "BTC-USD"})
        self.assertTrue(all(o["client_order_id"].startswith("cl-") and o["state"] == "filled" for o in orders))
        held = {p["symbol"] for p in self.council.store.get_positions()}
        self.assertEqual(held, {"SPY", "BTC-USD"})
        d = cs.run_detail(out["run_id"])
        self.assertEqual({x["judge_source"] for x in d["decisions"]}, {"llm"})
        self.assertTrue(all(x["ai_generated"] == 1 and x["provenance"] for x in d["decisions"]))
        self.assertEqual({x["outcome"] for x in d["executions"]}, {"submitted"})
        self.assertEqual({x["agent"] for x in d["signals"]}, {"technical", "macro", "sentiment", "risk"})
        self.assertTrue(all(x["ai_generated"] == 0 for x in d["signals"]))      # sentiment off (weight 0)
        self.assertEqual(len(d["ai_calls"]), 1)
        call = d["ai_calls"][0]
        self.assertEqual((call["feature"], call["status"], round(call["cost_usd"], 6)), ("judge", "ok", 0.002))
        self.assertAlmostEqual(spend.spent_today(self.clock()), 0.002)          # ONE counter, booked with the row
        self.assertEqual(d["summary"]["label"], svc_mod.LABEL)
        self.assertEqual({p["symbol"] for p in d["positions"]}, set())          # state the run decided on: flat
        # second cycle: no material change → carried, no new orders
        out2 = self.run_cycle()
        d2 = cs.run_detail(out2["run_id"])
        self.assertEqual({x["judge_source"] for x in d2["decisions"]}, {"carried"})
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(len(self.council_orders()), 2)

    def test_d7_equities_outside_the_window(self):
        for t in (SAT, FRI_0932):
            self.set_now(t)
            out = self.run_cycle()
            ex = {x["decision_id"]: x for x in cs.run_detail(out["run_id"])["executions"]}
            dec = {x["symbol"]: x for x in cs.run_detail(out["run_id"])["decisions"]}
            self.assertEqual(ex[dec["SPY"]["id"]]["reason"], svc_mod.MARKET_CLOSED)
            self.assertEqual(ex[dec["SPY"]["id"]]["outcome"], "not_executed")
        self.assertEqual({o["symbol"] for o in self.council_orders()}, {"BTC-USD"})     # crypto 24/7

    def test_gates(self):
        cs.set_orders_enabled(False, "user", "test")
        out = self.run_cycle()
        reasons = {x["reason"] for x in cs.run_detail(out["run_id"])["executions"]}
        self.assertEqual(reasons, {"Council orders are off"})
        cs.set_orders_enabled(True, "user", "test")
        control._write_autotrade(False, "user")
        self.llm.action = "SHORT"
        out = self.run_cycle()
        self.assertTrue({x["reason"] for x in cs.run_detail(out["run_id"])["executions"]} <= {"auto trading is off"})
        control._write_autotrade(True, "user")
        svc = svc_mod.CouncilService(self.feed, clock=self.clock, llm=self.llm, sandbox_fn=lambda: True)
        out = asyncio.run(svc.run("manual"))
        self.assertTrue({x["reason"] for x in cs.run_detail(out["run_id"])["executions"]}
                        <= {"sandbox mode — the Council only analyses"})
        self.assertEqual(self.council_orders(), [])

    def test_d8_budget_exhausted_runs_without_ai(self):
        cs.add_params(dict(cs.latest_params()[1], ai_daily_budget_usd=0.0), "user", "test", "user")
        out = self.run_cycle()
        self.assertEqual(out["status"], "partial")
        d = cs.run_detail(out["run_id"])
        self.assertEqual({x["judge_source"] for x in d["decisions"]}, {"unavailable"})
        self.assertTrue(all(x["unavailable_reason"] for x in d["decisions"]))
        self.assertEqual(self.llm.calls, [])
        self.assertEqual(self.council_orders(), [])

    def test_unallocated_capital_blocks_orders(self):
        with db._connect() as conn:
            ledger.insert_entries(conn, accounts.COUNCIL, [{"source_id": accounts.UNALLOCATED, "kind": "capital",
                                                            "amount": 100.0}])
        out = self.run_cycle()
        self.assertIn("unallocated", out["summary"]["errors"])
        self.assertEqual(self.council_orders(), [])


class ScheduleTest(unittest.TestCase):
    def test_next_due(self):
        open_ = datetime(2026, 10, 2, 13, 30, tzinfo=UTC)               # Fri 09:30 NY
        first = open_ + timedelta(minutes=5, seconds=5)
        self.assertEqual(svc_mod.next_due(open_ - timedelta(hours=3), 15), first)
        self.assertEqual(svc_mod.next_due(first + timedelta(seconds=1), 15, first), first + timedelta(minutes=15))
        late = first + timedelta(hours=1)                               # overdue: runs once at once (no catch-up)
        self.assertEqual(svc_mod.next_due(late, 15, first), first + timedelta(minutes=15))
        after_close = datetime(2026, 10, 2, 20, 30, tzinfo=UTC)
        self.assertEqual(svc_mod.next_due(after_close, 15, first), datetime(2026, 10, 5, 13, 35, 5, tzinfo=UTC))
        self.assertEqual(svc_mod.next_due(SAT, 15), datetime(2026, 10, 5, 13, 35, 5, tzinfo=UTC))
        mid = datetime(2026, 10, 2, 16, 0, tzinfo=UTC)                   # in the session, no cycle yet today
        self.assertEqual(svc_mod.next_due(mid, 15), mid)
        last_today = datetime(2026, 10, 2, 19, 50, tzinfo=UTC)          # 15:50 NY + 15 min = after the close
        self.assertEqual(svc_mod.next_due(last_today + timedelta(minutes=1), 15, last_today),
                         datetime(2026, 10, 5, 13, 35, 5, tzinfo=UTC))

    def test_window(self):
        self.assertFalse(svc_mod.equity_window_open(FRI_0932))
        self.assertTrue(svc_mod.equity_window_open(FRI_1100))
        self.assertFalse(svc_mod.equity_window_open(SAT))


class RunNowAndExpiryTest(CouncilCase):
    def test_run_now(self):
        r = self.svc.request_run()
        self.assertEqual(r["orders"], {"equity": True, "crypto": True})
        self.assertTrue(r["started"])
        self.assertEqual(cs.run_detail(r["run_id"])["status"], "running")      # the row exists before the answer
        with self.assertRaises(svc_mod.CouncilError) as cm:
            self.svc.request_run()
        self.assertEqual(cm.exception.status, 409)
        out = asyncio.run(self.svc.run("manual", self.svc._manual_pending))      # what the task does next
        self.svc._manual_pending = False
        self.assertEqual(out["run_id"], r["run_id"])
        self.assertIn(cs.run_detail(r["run_id"])["status"], ("ok", "partial"))
        self.assertEqual([x["id"] for x in cs.runs(10)], [r["run_id"]])          # no second row
        self.set_now(SAT)
        r2 = self.svc.request_run()
        self.assertEqual(r2["orders"], {"equity": False, "crypto": True})
        self.svc._manual_pending = False
        self.assertEqual(cs.sweep_unfinished(self.clock()), 1)                  # backend stopped before pickup
        self.assertEqual(cs.run_detail(r2["run_id"])["status"], "error")
        self.assertEqual(cs.run_detail(r2["run_id"])["summary"]["error"], cs.INTERRUPTED)
        self.assertEqual(cs.sweep_unfinished(self.clock()), 0)
        db.meta_set(cs.META_SWITCHOVER, json.dumps({"state": "none"}))
        with self.assertRaises(svc_mod.CouncilError) as cm:
            self.svc.request_run()
        self.assertEqual((cm.exception.status, cm.exception.code), (503, "not_active"))

    def test_stale_crypto_orders_expire_after_60_min(self):
        ts = (self.clock() - timedelta(minutes=61)).isoformat(timespec="seconds")
        with db._connect() as conn:
            oms._insert(conn, {"client_order_id": "cl-stale0000001", "account_id": accounts.COUNCIL,
                               "created_at": ts, "updated_at": ts, "symbol": "BTC-USD", "side": "buy", "qty": 0.01,
                               "origin": "council", "tif": "gtc", "state": "accepted", "filled_qty": 0.0,
                               "fees": 0.0, "attempts": 1, "allow_reduce": 0, "allocations_json": "[]",
                               "checks_json": "[]"})
        self.assertEqual(self.svc._expire_stale_crypto(self.clock()), 1)
        self.assertEqual(oms.get("cl-stale0000001")["state"], "expired")


if __name__ == "__main__":
    unittest.main()
