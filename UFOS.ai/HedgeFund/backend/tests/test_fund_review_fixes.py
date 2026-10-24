"""Stage-1 review fixes + user decisions of 2026-10-02 in the fund package:
prev_close after early closes, strict bars (no repaired OHLC), stored risk
card required for manual accept, strict AI budget param in sentiment, NO
valuation at the stored price (valuation paused), macro agent unavailable
while FRED fails, carried recommendations keep their original time.
Paper trading only. Offline.

Run from HedgeFund/backend:  python -m unittest tests.test_fund_review_fixes -v
"""
import asyncio
import unittest
from datetime import datetime, timedelta, timezone

try:
    from fund_testkit import EngineCase
    from test_fund_market import FakeCache, FakeFetch
    from test_sp_review_fixes import eq
except ImportError:  # pragma: no cover - run as package
    from tests.fund_testkit import EngineCase
    from tests.test_fund_market import FakeCache, FakeFetch
    from tests.test_sp_review_fixes import eq

from strategy_platform.data import market_feed as M
from fund import store
from fund.agents import sentiment
from fund.engine import EngineError
from strategy_platform.core.types import MarketContext
from strategy_platform.core.errors import DataSourceUnavailable


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


class PrevCloseAfterEarlyCloseTest(unittest.TestCase):
    CASES = [  # (bars before, early-close session, trade time on the next session)
        (["2025-07-01", "2025-07-02"], "2025-07-03", utc(2025, 7, 7, 15, 0)),
        (["2025-12-22", "2025-12-23"], "2025-12-24", utc(2025, 12, 26, 16, 0)),
        (["2026-12-22", "2026-12-23"], "2026-12-24", utc(2026, 12, 28, 16, 0)),
    ]

    def test_prev_close_is_the_early_close_session(self):
        for before, early, trade_at in self.CASES:
            with self.subTest(early=early):
                cache = FakeCache({"SPY": [eq(d, 100.0 + i) for i, d in enumerate(before)]})
                fetch = FakeFetch()
                fetch.answers[("equity_quotes", "latest_trade")] = {
                    "SPY": {"symbol": "SPY", "price": 150.0, "size": 1, "ts": trade_at.isoformat()}}
                feed = M.AlpacaFeed(cache=cache, fetch=fetch, clock=lambda: trade_at + timedelta(minutes=1))
                with self.assertRaises(DataSourceUnavailable) as cm:      # never the session before
                    feed.quote("SPY")
                self.assertIn(f"previous session {early}", cm.exception.user_message)
                cache.rows["SPY"].append(eq(early, 140.0))
                self.assertEqual(feed.quote("SPY").prev_close, 140.0)


class StrictBarsTest(unittest.TestCase):
    def feed(self, rows):
        return M.AlpacaFeed(cache=FakeCache({"SPY": rows}), fetch=FakeFetch(), clock=lambda: utc(2026, 10, 2, 21, 0))

    def test_malformed_bar_fails_the_symbol(self):
        for field, value in (("close", None), ("open", 0.0), ("high", float("nan")), ("low", -1.0)):
            with self.subTest(field=field):
                rows = [eq("2026-10-01", 100.0), dict(eq("2026-10-02", 101.0), **{field: value})]
                with self.assertRaises(DataSourceUnavailable) as cm:
                    self.feed(rows).daily_bars("SPY")
                self.assertIn("Alpaca returned a malformed bar for SPY on 2026-10-02", cm.exception.user_message)

    def test_validation_error_fails_and_missing_volume_stays_none(self):
        bad = dict(eq("2026-10-02", 101.0), high=90.0, low=95.0)          # high below low
        with self.assertRaises(DataSourceUnavailable) as cm:
            self.feed([eq("2026-10-01", 100.0), bad]).daily_bars("SPY")
        self.assertIn("malformed bar for SPY", cm.exception.user_message)
        feed = self.feed([eq("2026-10-01", 100.0), dict(eq("2026-10-02", 101.0), volume=None)])
        bars = feed.daily_bars("SPY")
        self.assertIsNone(bars[-1].volume)                                 # not replaced by 0
        self.assertIn("SPY", feed.findings)


class SentimentBudgetParamTest(unittest.TestCase):
    def test_missing_budget_param_is_unavailable_without_default(self):
        calls = []

        async def llm(*a, **kw):
            calls.append(a)
            raise AssertionError("no LLM call expected")
        now = "2026-10-02T14:00:00+00:00"
        ctx = MarketContext(now=now, params={"agent_weights": {"sentiment": 1.0}}, session={},
                            news={"SPY": [{"title": "SPY up", "publisher": "W", "published_at": now}]})
        out = asyncio.run(sentiment.evaluate_batch(ctx, ["SPY"], {}, None, llm=llm))
        self.assertEqual((out["SPY"].source, out["SPY"].confidence), ("unavailable", 0.0))
        self.assertIn("AI budget setting is missing", out["SPY"].summary)
        self.assertEqual(calls, [])


class RiskCardRequiredTest(EngineCase):
    def test_missing_risk_card_or_multiplier_is_409(self):
        with self.assertRaises(EngineError) as cm:
            self.engine._risk_from_scores(None, "SPY")
        self.assertEqual(cm.exception.status, 409)
        orig = store.agent_scores
        store.agent_scores = lambda run_id, symbol=None: [{"agent": "risk", "details": {"veto": False}}]
        try:
            with self.assertRaises(EngineError) as cm:
                self.engine._risk_from_scores(1, "SPY")
        finally:
            store.agent_scores = orig
        self.assertIn("no size multiplier", cm.exception.message)


class ValuationPausedTest(EngineCase):
    """User decision 2026-10-02: no valuation at the stored price."""

    def test_missing_quote_pauses_valuation_snapshot_and_orders(self):
        asyncio.run(self.engine.manual_order({"symbol": "SPY", "side": "buy", "qty": 3}))
        self.cycle()
        points = len(store.equity_points())
        self.engine.set_autopilot(True)
        del self.feed.quotes["SPY"]
        self.fake.calls.clear()
        out = self.cycle()
        self.assertEqual(out["status"], "partial")
        run = store.get_run(out["run_id"])
        self.assertEqual(run["run"]["summary"]["valuation_paused"], "valuation paused: no current quote for SPY")
        self.assertIn("valuation paused: no current quote for SPY", out["error"])
        self.assertEqual(len(store.equity_points()), points)                    # no equity snapshot
        self.assertEqual([o for o in store.list_orders() if o["origin"] == "autopilot"], [])
        for r in run["recommendations"]:                                         # QQQ: analysed, not traded
            self.assertEqual((r["status"], r["skip_reason"]), ("skipped", "valuation paused: no current quote for SPY"))
        st = self.engine.state()
        self.assertEqual(st["account"]["valuation_paused"], "valuation paused: no current quote for SPY")
        self.assertIsNone(st["account"]["equity"])
        pos = {p["symbol"]: p for p in st["positions"]}["SPY"]
        self.assertEqual((pos["qty"], pos["last_price"], pos["market_value"], pos["weight"]), (3, None, None, None))
        rec = store.latest_recommendations()["QQQ"]
        with self.assertRaises(EngineError) as cm:                               # manual accept refused too
            asyncio.run(self.engine.execute_recommendation(rec["id"]))
        self.assertEqual(cm.exception.status, 503)
        self.assertIn("valuation paused: no current quote for SPY", cm.exception.message)
        # the quote is back: valuation resumes
        self.feed.quotes["SPY"] = 500.0
        self.now = self.now + timedelta(minutes=5)
        out = self.cycle()
        self.assertIsNone(store.get_run(out["run_id"])["run"]["summary"]["valuation_paused"])
        self.assertIsNone(self.engine.state()["account"]["valuation_paused"])
        self.assertEqual(len(store.equity_points()), points + 1)

    def test_broker_never_marks_at_the_stored_price(self):
        from strategy_platform.execution.paper_broker import ValuationPaused
        asyncio.run(self.engine.manual_order({"symbol": "SPY", "side": "buy", "qty": 3}))
        with self.assertRaises(ValuationPaused):
            self.broker.account({"SPY": None})
        views = self.broker.positions({"SPY": None}, allow_unvalued=True)
        self.assertEqual((views[0].last_price, views[0].market_value, views[0].unrealized_pnl), (None, None, None))
        # a closing order still queues (the kill switch keeps working)
        r = self.broker.liquidate_all("kill-switch", prices={"SPY": None})
        self.assertEqual([x.status for x in r], ["pending"])


class MacroUnavailableTest(EngineCase):
    def test_fred_failing_makes_the_macro_agent_unavailable(self):
        self.engine._macro_status_fn = lambda: {"ok": False, "message": "FRED API key is not set — enter it in "
                                                                        "Hedge Fund → Settings → Keys."}
        self.engine._macro_fn = lambda: {"macro-DGS10": {"id": "macro-DGS10"}}     # stored data is NOT used
        out = self.cycle()
        macro = [a for a in store.get_run(out["run_id"])["agents"] if a["agent"] == "macro"]
        self.assertTrue(macro)
        for a in macro:
            self.assertEqual((a["source"], a["confidence"]), ("unavailable", 0.0))
            self.assertTrue(a["summary"].startswith("Macro data unavailable: FRED API key is not set"))
        self.engine._macro_status_fn = lambda: {"ok": True, "message": "ok"}
        out = self.cycle()
        macro = [a for a in store.get_run(out["run_id"])["agents"] if a["agent"] == "macro"]
        self.assertTrue(all(a["source"] != "unavailable" for a in macro))


class CarriedTimeTest(EngineCase):
    def test_carried_recommendation_shows_its_original_time(self):
        self.cycle()
        first = store.latest_recommendations()["SPY"]
        self.now = self.now + timedelta(minutes=5)
        self.cycle()
        st = {s["symbol"]: s for s in self.engine.state()["symbols"]}
        rec = st["SPY"]["recommendation"]
        self.assertEqual(rec["source"], "carried")
        self.assertEqual(rec["judged_at"], first["provenance"]["generated_at"])
        self.assertNotEqual(rec["ts"], first["ts"])


# --- user decisions on the last pending fallbacks (2026-10-02) ------------------------------

class PremiumFlipNoStandInTest(unittest.TestCase):
    def test_failed_premium_call_gives_no_recommendation_for_the_flip(self):
        from fund import judge as J
        from strategy_platform.core import llm as llm_mod
        from strategy_platform.core import params as P
        from strategy_platform.core.llm import Budget, LlmUnavailable
        from strategy_platform.core.types import AgentSignal, JudgeUnavailable, PositionView, RiskAssessment
        models = llm_mod.models()

        def sig(agent, score, sym):
            return AgentSignal(agent=agent, symbol=sym, score=score, confidence=0.9, summary=agent)
        signals = {s: {"technical": sig("technical", v, s), "macro": sig("macro", v, s)}
                   for s, v in (("SPY", 0.6), ("TLT", -0.5))}
        judged = {"id": 1, "ts": "2026-09-28T14:50:00+00:00", "action": "LONG", "confidence": 0.8,
                  "rationale": "r", "source": "llm", "model": models["standard"]}
        previous = {s: J.previous_state({**judged, "id": i}, params_changed=True) for i, s in enumerate(["SPY", "TLT"])}
        calls = []

        async def llm(messages, model, max_tokens, feature, label, temperature=0.2):
            calls.append(model)
            if model == models["premium"]:
                raise LlmUnavailable("error", "timeout", attempted=True)
            out = [{"symbol": "SPY", "action": "LONG", "confidence": 0.8, "rationale": "x"},
                   {"symbol": "TLT", "action": "SHORT", "confidence": 0.7, "rationale": "y"}]
            return out, "", model, {"cost_usd": 0.01, "prompt_tokens": 10, "completion_tokens": 5}
        ctx = MarketContext(now="2026-09-28T15:00:00+00:00", params=P.defaults(), session={},
                            positions={"TLT": PositionView("TLT", 10, 90.0, 90.0)})
        out = asyncio.run(J.judge(ctx, signals, {s: RiskAssessment(s) for s in signals}, previous,
                                  P.merge(None, {"judge_model_mode": "premium_on_flip"}), Budget(5.0), llm=llm))
        self.assertEqual(calls, [models["standard"], models["premium"]])
        self.assertEqual(out["SPY"].source, "llm")
        self.assertIsInstance(out["TLT"], JudgeUnavailable)            # no standard-answer stand-in
        self.assertIn("Premium AI judge unavailable for this flip", out["TLT"].message)
        self.assertIsNotNone(out["TLT"].usage)


class ManualAcceptNeedsBarsTest(EngineCase):
    def test_no_daily_bars_is_409(self):
        self.cycle()
        rec = store.latest_recommendations()["SPY"]
        self.feed.bars["SPY"] = []
        with self.assertRaises(EngineError) as cm:
            asyncio.run(self.engine.execute_recommendation(rec["id"]))
        self.assertEqual((cm.exception.status, cm.exception.message),
                         (409, "No daily bars for SPY — the position size can't be volatility-scaled; try again "
                               "when market data is available."))
        self.assertEqual(store.list_orders(), [])


class CalendarUnavailableTest(EngineCase):
    def test_failing_calendar_makes_risk_cards_unavailable_and_blocks_orders(self):
        def broken():
            raise RuntimeError("calendar cache gone")
        self.engine._calendar_fn = broken
        self.engine.set_autopilot(True)
        out = self.cycle()
        self.assertEqual(out["status"], "partial")
        self.assertIn("calendar accessor failed", out["error"])
        run = store.get_run(out["run_id"])
        risk = [a for a in run["agents"] if a["agent"] == "risk"]
        self.assertTrue(risk and all(a["source"] == "unavailable" for a in risk))
        self.assertTrue(all("unavailable: calendar data could not be loaded" in a["summary"] for a in risk))
        self.assertEqual(store.list_orders(), [])
        for r in run["recommendations"]:
            self.assertEqual(r["status"], "skipped")
            self.assertIn("risk card unavailable: calendar data could not be loaded", r["skip_reason"])
        with self.assertRaises(EngineError) as cm:                        # manual accept refused as well
            asyncio.run(self.engine.execute_recommendation(run["recommendations"][0]["id"]))
        self.assertEqual(cm.exception.status, 409)


class InsiderUnavailableTest(unittest.TestCase):
    def run_batch(self, ctx):
        prompts = []

        async def llm(messages, model, max_tokens, feature, label, temperature=0.2):
            prompts.append(messages[-1]["content"])
            return ([{"symbol": "SPY", "score": 0.3, "confidence": 0.5, "summary": "Upbeat."}], "", model,
                    {"cost_usd": 0.001})
        return asyncio.run(sentiment.evaluate_batch(ctx, ["SPY"], {}, None, llm=llm)), prompts

    def ctx(self, **kw):
        now = "2026-10-02T14:00:00+00:00"
        return MarketContext(now=now, params={"ai_daily_budget_usd": 1.0, "agent_weights": {"sentiment": 1.0}},
                             session={}, news={"SPY": [{"title": "SPY up", "publisher": "W", "published_at": now}]},
                             **kw)

    def test_accessor_error_is_said_not_hidden(self):
        out, prompts = self.run_batch(self.ctx(insider_error="insider accessor failed (RuntimeError)"))
        self.assertIn("INSIDER DATA UNAVAILABLE: insider accessor failed", prompts[0])
        self.assertTrue(out["SPY"].summary.endswith("(insider data unavailable)"))
        self.assertEqual(out["SPY"].details["insider_unavailable"], "insider accessor failed (RuntimeError)")

    def test_failing_cluster_detection_is_said_not_hidden(self):
        import newsroom.insider_summary as ins
        orig = ins.find_clusters

        def boom(events):
            raise ValueError("bad event")
        ins.find_clusters = boom
        try:
            events = [{"details": {"issuer_ticker": "SPY", "transactions": []}}]
            with self.assertRaises(sentiment.InsiderDataUnavailable):
                sentiment.insider_flags(events, "SPY")
            out, prompts = self.run_batch(self.ctx(insider=events))
        finally:
            ins.find_clusters = orig
        self.assertIn("INSIDER DATA UNAVAILABLE: insider cluster detection failed (ValueError)", prompts[0])
        self.assertIn("insider data unavailable", out["SPY"].summary)


if __name__ == "__main__":
    unittest.main()
