"""24/7 crypto — agents, sizing, backtest and API (fund/SPEC.md §7, §9, §11,
§12): macro mapping (growth-equity tendency ×0.6, own stress rules), crypto
sizing (3 % max position, 3 % vol target, UTC-day min hold), risk ATR
thresholds, the technical 365-bar year, a backtest mixing 7-day crypto and
weekday equity bars, and the API validation of fractional quantities plus
the /fund/state and /fund/params contract fields. Paper trading only;
offline.

Run from HedgeFund/backend:  python -m unittest tests.test_fund_crypto_misc -v
"""
import asyncio
import json
import random
import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

try:
    from crypto_testkit import BTC, ETH, CryptoCase
    from fund_testkit import SAT, make_bars
except ImportError:  # pragma: no cover - run as package
    from tests.crypto_testkit import BTC, ETH, CryptoCase
    from tests.fund_testkit import SAT, make_bars

from fund import api as fund_api
from fund import params as P
from fund import sizing
from fund.agents import macro as macro_agent
from fund.agents import risk as risk_agent
from fund.agents import technical
from fund.backtest import run_backtest
from fund.types import Bar, JudgeDecision, PositionView


class CryptoAgentTest(unittest.TestCase):
    def ctx(self, readings=None, regime="goldilocks"):
        reg = {"quadrant": regime, "label": regime, "date": "2026-08-01", "borderline": False,
               "months_in_regime": 1} if regime else None
        return SimpleNamespace(now="2026-09-27T12:00:00+00:00",
                               macro={"available": True, "regime": reg, "readings": readings or {}})

    def test_macro_maps_crypto_to_scaled_growth_tendency(self):
        growth = macro_agent.evaluate(self.ctx(), "XLK")
        sig = macro_agent.evaluate(self.ctx(), BTC)
        self.assertEqual(sig.details["asset_class"], "crypto")
        self.assertAlmostEqual(growth.score, 0.6)
        self.assertAlmostEqual(sig.score, 0.36)                                  # (0.5 + 0.1) x 0.6
        self.assertAlmostEqual(sig.confidence, round(growth.confidence * 0.6, 4))
        stress = {"DGS10": {"value": 5.1, "date": "2026-09-25", "monthly": False},
                  "SAHM_RULE": {"value": 0.6, "date": "2026-08-01", "monthly": True},
                  "VIXCLS": {"value": 35.0, "date": "2026-09-25", "monthly": False},
                  "T10Y3M": {"value": -0.5, "date": "2026-09-25", "monthly": False}}
        sig = macro_agent.evaluate(self.ctx(stress), BTC)
        self.assertAlmostEqual(sig.details["stress_adjustment"], -0.5)          # 10Y -0.2, Sahm -0.3 only
        self.assertAlmostEqual(sig.score, round(0.36 - 0.5, 4))
        slow = macro_agent.evaluate(self.ctx(regime="slowdown"), BTC)
        self.assertAlmostEqual(slow.score, -0.3)                                 # -0.5 x 0.6
        none = macro_agent.evaluate(self.ctx(stress, regime=None), BTC)
        self.assertLessEqual(none.confidence, 0.3 * 0.6 + 1e-9)

    def test_sizing_uses_crypto_limits(self):
        p = P.defaults()
        d = JudgeDecision(symbol=BTC, action="LONG", confidence=1.0, rationale="", source="rules")
        self.assertAlmostEqual(sizing.target_weight(d, None, 0.02, p), 0.03)       # 3 %, ATR under 3 %
        self.assertAlmostEqual(sizing.target_weight(d, None, 0.06, p), 0.015)      # vol target 3 %
        e = JudgeDecision(symbol="SPY", action="LONG", confidence=1.0, rationale="", source="rules")
        self.assertAlmostEqual(sizing.target_weight(e, None, 0.03, p), 0.025)      # 5 % x 1.5/3
        self.assertEqual(sizing.vol_scaler(0.03, BTC), 1.0)
        self.assertEqual(sizing.vol_scaler(0.03, "SPY"), 0.5)

    def test_min_hold_counts_utc_days_for_crypto(self):
        p = dict(P.defaults(), min_hold_days=1)
        pos = {"weight": 0.02, "opened_at": "2026-10-03T23:00:00+00:00"}       # Saturday night
        now = datetime(2026, 10, 4, 1, 0, tzinfo=timezone.utc)                  # Sunday
        flat = {s: JudgeDecision(symbol=s, action="FLAT", confidence=0.9, rationale="", source="rules")
                for s in (BTC, "SPY")}
        self.assertEqual(sizing.plan_trade(flat[BTC], None, pos, None, p, now, 0)[0], 0.0)
        self.assertIn("min hold", sizing.plan_trade(flat["SPY"], None, pos, None, p, now, 0)[1])
        pos_new = {"weight": 0.02, "opened_at": "2026-10-04T00:30:00+00:00"}
        self.assertIn("UTC days", sizing.plan_trade(flat[BTC], None, pos_new, None, p, now, 0)[1])

    def test_risk_atr_thresholds_and_concentration(self):
        self.assertEqual(risk_agent.atr_thresholds(BTC), (0.09, 0.06))
        self.assertEqual(risk_agent.atr_thresholds("SPY"), (0.05, 0.03))

        def bars(rng):
            return [Bar(date=(date(2026, 1, 1) + timedelta(days=i)).isoformat(), open=100.0,
                        high=100 * (1 + rng / 2), low=100 * (1 - rng / 2), close=100.0) for i in range(80)]
        for rng, mult in ((0.05, 1.0), (0.07, 0.5), (0.10, 0.25)):
            ctx = SimpleNamespace(now="2026-10-03T12:00:00+00:00", halted=False, account=None, positions={},
                                  calendar=[], bars={BTC: bars(rng)}, params=P.defaults())
            self.assertEqual(risk_agent.assess(ctx, BTC, P.defaults()).size_multiplier, mult, rng)
        acct = SimpleNamespace(equity=100000.0, drawdown_pct=0.0, short_exposure_pct=0.0)
        ctx = SimpleNamespace(now="2026-10-03T12:00:00+00:00", halted=False, account=acct, calendar=[], bars={},
                              positions={ETH: PositionView(ETH, 3.0, 2000.0, 2000.0)}, params=P.defaults())
        ra = risk_agent.assess(ctx, BTC, P.defaults())                           # crypto 6 % >= 2 x 3 %
        self.assertTrue(ra.block_increase)
        self.assertFalse(risk_agent.assess(ctx, "SPY", P.defaults()).block_increase)

    def test_technical_uses_365_bars_for_crypto(self):
        b = make_bars(50000.0, 400)
        self.assertEqual(technical.evaluate_bars(b, BTC).details["bars"], 370)
        self.assertEqual(technical.evaluate_bars(b, "SPY").details["bars"], 260)

    def test_position_view_instrument(self):
        self.assertEqual(PositionView(BTC, 0.1, 1.0).instrument, "spot")
        self.assertEqual(PositionView(BTC, -0.1, 1.0).instrument, "perp")
        self.assertEqual(PositionView("SPY", -1, 1.0).instrument, "stock")
        self.assertEqual(PositionView("SPY", 1, 1.0).asset_class, "equity")


def gen(seed, n, drift, weekends, start=date(2021, 1, 1), vol=0.008, p=100.0):
    r, d, out = random.Random(seed), start, []
    while len(out) < n:
        d += timedelta(days=1)
        if not weekends and d.weekday() >= 5:
            continue
        o = p * (1 + r.gauss(0, 0.002))
        p = o * (1 + drift + r.gauss(0, vol))
        out.append(Bar(d.isoformat(), o, max(o, p) * 1.003, min(o, p) * 0.997, p))
    return out


class CryptoBacktestTest(unittest.TestCase):
    def test_mixed_calendar_fractional_qty_crypto_fees_and_funding(self):
        bars = {BTC: gen(1, 1000, 0.002, True, p=30000.0), ETH: gen(2, 1000, -0.002, True, p=2000.0),
                "SPY": gen(3, 700, 0.0015, False)}
        start = bars[BTC][420].date
        params = {"min_confidence": 55, "agent_weights": {"technical": 1.0, "macro": 0.0}, "fees_bps": 0.0,
                  "crypto_fees_bps": 20.0}
        r = run_backtest(bars, [], params, start=start)
        self.assertNotIn("error", r)
        spy_days = sum(1 for b in bars["SPY"] if start <= b.date <= r["end"])
        self.assertGreater(r["days"], spy_days)                               # weekends in the calendar
        by = {p["symbol"]: p for p in r["per_symbol"]}
        self.assertGreater(by[BTC]["trades"], 0)
        self.assertGreater(by[BTC]["days_long"], by[BTC]["days_short"])
        self.assertNotEqual(by[BTC]["final_qty"], int(by[BTC]["final_qty"]))  # fractional
        self.assertGreater(by[BTC]["fees"], 0)
        self.assertEqual(by["SPY"]["fees"], 0)
        self.assertGreater(by["SPY"]["trades"], 0)
        self.assertGreater(by[ETH]["days_short"], 0)
        self.assertGreater(by[ETH]["funding"], 0)                              # + rate: the short receives
        self.assertEqual(by[ETH]["borrow_fees"], 0)
        self.assertEqual(by[BTC]["asset_class"], "crypto")
        self.assertTrue(any(n.startswith("Crypto:") for n in r["notes"]))


def call(api, name, query=None, body=fund_api._NO_BODY, match=None):
    return asyncio.run(getattr(api, name)(query or {}, body, match or {}))


class CryptoApiTest(CryptoCase):
    def setUp(self):
        super().setUp()
        self.api = fund_api.FundApi(self.engine)
        self.now = SAT

    def test_fractional_qty_validation(self):
        status, p = call(self.api, "orders_post", body={"symbol": "SPY", "side": "buy", "qty": 1.5})
        self.assertEqual(status, 400)
        self.assertIn("fractional", p["errors"]["qty"])
        status, p = call(self.api, "orders_post", body={"symbol": "btc-usd", "side": "buy", "qty": 0.01})
        self.assertEqual(status, 200)
        self.assertEqual((p["symbol"], p["qty"], p["status"]), (BTC, 0.01, "filled"))
        status, _p = call(self.api, "orders_post", body={"symbol": BTC, "side": "buy", "qty": 0})
        self.assertEqual(status, 400)
        status, p = call(self.api, "orders_post", body={"symbol": BTC, "side": "buy", "qty": 0.0001})
        self.assertEqual((status, p["status"]), (200, "rejected"))
        self.assertTrue(p["reject_reason"].startswith("size below minimum"))
        status, p = call(self.api, "orders_post", body={"symbol": "SPY", "side": "buy", "qty": 2})
        self.assertEqual((status, p["status"]), (200, "pending"))

    def test_state_and_params_contract_fields(self):
        call(self.api, "orders_post", body={"symbol": BTC, "side": "short", "qty": 0.1})
        status, st = call(self.api, "state")
        self.assertEqual(status, 200)
        for k in ("schedule_mode", "next_run_at", "next_risk_watch_at", "next_session_run_at",
                  "crypto_risk_watch", "risk_watch_interval_minutes"):
            self.assertIn(k, st["engine"])
        for k in ("funding_total", "crypto_margin_excess"):
            self.assertIn(k, st["account"])
        self.assertIsNotNone(st["account"]["crypto_margin_excess"])
        pos = st["positions"][0]
        self.assertEqual((pos["asset_class"], pos["instrument"], pos["qty"]), ("crypto", "perp", -0.1))
        for s in st["symbols"]:
            self.assertIn(s["asset_class"], ("crypto", "equity"))
            self.assertIsInstance(s["tradable_now"], bool)
        status, p = call(self.api, "params_get")
        for k in ("crypto_fees_bps", "crypto_max_position_pct", "crypto_initial_margin_pct",
                  "crypto_maintenance_margin_pct", "crypto_funding_rate_pct_8h", "crypto_liquidation_fee_pct",
                  "crypto_risk_watch"):
            self.assertIn(k, p["limits"])
            self.assertIn(k, p["params"])
        json.loads(fund_api.dumps(st))


if __name__ == "__main__":
    unittest.main()
