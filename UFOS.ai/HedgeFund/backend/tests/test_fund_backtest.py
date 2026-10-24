"""fund/backtest.py — rule-only backtest on synthetic bars (no look-ahead,
fees, shorts, benchmarks, weekly points), the 2016 history start of the
configured source (Alpaca) and the macro-history builder.
Offline and deterministic.

Run from HedgeFund/backend:  python -m unittest tests.test_fund_backtest -v
"""
import random
import unittest
from datetime import date, timedelta

from fund.backtest import HISTORY_ERROR, build_macro_history, check_history_start, macro_context_at, run_backtest
from strategy_platform.core.types import Bar


def gen(seed, n, drift, start=date(2020, 1, 1), vol=0.008):
    r, p, d, out = random.Random(seed), 100.0, start, []
    while len(out) < n:
        d += timedelta(days=1)
        if d.weekday() >= 5:
            continue
        o = p * (1 + r.gauss(0, 0.002))
        p = o * (1 + drift + r.gauss(0, vol))
        out.append(Bar(d.isoformat(), o, max(o, p) * 1.003, min(o, p) * 0.997, p))
    return out


BARS = {"UP": gen(1, 700, 0.0015), "DOWN": gen(2, 700, -0.0015)}
START = BARS["UP"][300].date
P_BASE = {"min_confidence": 55, "agent_weights": {"technical": 1.0, "macro": 0.0}}


class HistoryStartTest(unittest.TestCase):
    """Alpaca (the configured source) has equity history only since 2016."""

    def test_start_before_2016_is_an_error(self):
        early = {"UP": gen(1, 700, 0.0015, start=date(2014, 1, 1))}
        r = run_backtest(early, [], P_BASE, start="2015-06-01")
        self.assertEqual(r["error"], HISTORY_ERROR)
        self.assertEqual(HISTORY_ERROR, "history before 2016 is not available from the configured source")
        self.assertEqual(check_history_start("2015-12-31"), HISTORY_ERROR)
        self.assertIsNone(check_history_start(date(2016, 1, 1)))
        self.assertIsNone(check_history_start(None))
        self.assertNotIn("error", run_backtest(BARS, [], P_BASE, start=START))


class BacktestTest(unittest.TestCase):
    def test_structure_and_trend_following(self):
        r = run_backtest(BARS, [], P_BASE, start=START)
        for k in ("symbols", "start", "end", "portfolio", "benchmark", "per_symbol", "equity", "notes"):
            self.assertIn(k, r)
        port = r["portfolio"]
        for k in ("cagr_pct", "total_return_pct", "max_drawdown_pct", "sharpe", "trades", "hit_rate_pct",
                  "exposure_pct"):
            self.assertIn(k, port)
        self.assertEqual(r["start"], START)
        self.assertGreater(port["trades"], 0)
        self.assertGreater(port["total_return_pct"], 0)
        by = {p["symbol"]: p for p in r["per_symbol"]}
        self.assertGreater(by["UP"]["days_long"], by["UP"]["days_short"])
        self.assertGreater(by["DOWN"]["days_short"], 0)
        self.assertGreater(by["DOWN"]["borrow_fees"], 0)
        self.assertGreater(r["benchmark"]["buy_and_hold"]["UP"]["total_return_pct"], 0)
        self.assertLess(r["benchmark"]["buy_and_hold"]["DOWN"]["total_return_pct"], 0)
        self.assertIn("total_return_pct", r["benchmark"]["equal_weight"])
        pt = r["equity"][0]
        self.assertEqual(set(pt), {"date", "equity", "benchmark"})
        self.assertEqual(r["equity"][-1]["date"], r["end"])
        self.assertTrue(any("not investment advice" in n for n in r["notes"]))

    def test_deterministic(self):
        self.assertEqual(run_backtest(BARS, [], P_BASE, start=START), run_backtest(BARS, [], P_BASE, start=START))

    def test_no_look_ahead(self):
        cut = BARS["UP"][500].date
        full = run_backtest(BARS, [], P_BASE, start=START)
        changed = {s: [b if b.date <= cut else Bar(b.date, b.open * 3, b.high * 3, b.low * 3, b.close * 3)
                       for b in bars] for s, bars in BARS.items()}
        alt = run_backtest(changed, [], P_BASE, start=START)
        a = {p["date"]: p["equity"] for p in full["equity"] if p["date"] <= cut}
        b = {p["date"]: p["equity"] for p in alt["equity"] if p["date"] <= cut}
        self.assertTrue(a)
        self.assertEqual(a, b)

    def test_no_shorts(self):
        r = run_backtest(BARS, [], dict(P_BASE, allow_shorts=False), start=START)
        self.assertTrue(all(p["days_short"] == 0 for p in r["per_symbol"]))

    def test_fees_cost_money(self):
        cheap = run_backtest(BARS, [], dict(P_BASE, fees_bps=0.0), start=START)["portfolio"]["final_equity"]
        dear = run_backtest(BARS, [], dict(P_BASE, fees_bps=50.0), start=START)["portfolio"]["final_equity"]
        self.assertLess(dear, cheap)

    def test_empty_input(self):
        self.assertIn("error", run_backtest({}, [], None))


class MacroHistoryTest(unittest.TestCase):
    def macro_latest(self):
        trail = [{"date": "2026-05-01", "x": 0.5, "y": -0.2, "quadrant": "goldilocks"},
                 {"date": "2026-06-01", "x": 0.3, "y": 0.05, "quadrant": "reflation"},
                 {"date": "2026-07-01", "x": 0.2, "y": 0.3, "quadrant": "reflation"}]
        return {"macro-regime": {"macro": {"regime": {"quadrant": "reflation", "extended": {"trail_long": trail}}}},
                "macro-DGS10": {"macro": {"long_history": [{"date": "2026-06-01", "value": 4.4},
                                                           {"date": "2026-07-01", "value": 5.1}]}}}

    def test_build_and_lookup(self):
        hist = build_macro_history(self.macro_latest())
        self.assertEqual([h["available_from"] for h in hist], ["2026-07-01", "2026-08-01", "2026-09-01"])
        self.assertTrue(hist[1]["borderline"])
        self.assertEqual(hist[2]["readings"], {"DGS10": 5.1})
        self.assertFalse(macro_context_at(hist, "2026-06-30")["available"])
        ctx = macro_context_at(hist, "2026-08-15")
        self.assertEqual(ctx["regime"]["quadrant"], "reflation")
        self.assertEqual(ctx["regime"]["date"], "2026-06-01")
        self.assertEqual(ctx["readings"]["DGS10"]["value"], 4.4)
        self.assertEqual(macro_context_at(hist, "2026-09-02")["regime"]["months_in_regime"], 2)
        self.assertEqual(build_macro_history({}), [])

    def test_macro_agent_in_backtest(self):
        months = []
        d = date(2019, 1, 1)
        while d < date(2023, 1, 1):
            months.append({"date": d.isoformat(), "quadrant": "slowdown"})
            d = date(d.year + (d.month // 12), d.month % 12 + 1, 1)
        p = {"min_confidence": 55, "agent_weights": {"technical": 0.0, "macro": 1.0}}
        r = run_backtest({"TLT": BARS["UP"]}, months, p, start=START)
        by = {x["symbol"]: x for x in r["per_symbol"]}
        self.assertGreater(by["TLT"]["days_long"], 0)       # slowdown: govt bonds tended to rise
        self.assertFalse(any("No macro regime history" in n for n in r["notes"]))


if __name__ == "__main__":
    unittest.main()
