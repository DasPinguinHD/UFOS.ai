"""OS2 surface (SP §21.12): ATM IV and 25Δ RR / BF by interpolation inside one expiry only (no extrapolation),
expected move (straddle and IV-based) on a constructed chain, 30-day IV by total variance, the VIX term-structure
label, realised volatility and VRP — all against values written out by hand."""
import math
import os
import sys
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from os2_testkit import NY, OS2Case, build_chain, ny_ms  # noqa: E402

from strategy_platform.core import calendars  # noqa: E402
from strategy_platform.options_structure import bs, model, quotes, surface  # noqa: E402

D = date(2026, 11, 5)
SLOT = ny_ms(D, 10, 0)


def N(x):
    return 0.5 * math.erfc(-x / math.sqrt(2))


class InterpolationTest(unittest.TestCase):
    def test_atm_iv_in_log_moneyness(self):
        pts = [{"k": -0.02, "iv": 0.24}, {"k": -0.01, "iv": 0.22}, {"k": 0.01, "iv": 0.18}, {"k": 0.03, "iv": 0.17}]
        v, why = surface.atm_iv(pts)
        self.assertIsNone(why)
        self.assertAlmostEqual(v, 0.22 + (0.18 - 0.22) * (0.01 / 0.02))        # = 0.20
        v, why = surface.atm_iv(pts[:2])                                        # only one side → no extrapolation
        self.assertIsNone(v)
        self.assertIn("no extrapolation", why)

    def test_25_delta_by_delta_interpolation(self):
        pts = [{"kind": "put", "delta": -0.40, "iv": 0.22}, {"kind": "put", "delta": -0.20, "iv": 0.26},
               {"kind": "call", "delta": 0.30, "iv": 0.18}, {"kind": "call", "delta": 0.15, "iv": 0.19}]
        p25, _ = surface.delta_iv(pts, "put", 0.25)
        c25, _ = surface.delta_iv(pts, "call", 0.25)
        self.assertAlmostEqual(p25, 0.26 + (0.22 - 0.26) * (0.05 / 0.20))      # 0.25
        self.assertAlmostEqual(c25, 0.19 + (0.18 - 0.19) * (0.10 / 0.15))      # 0.18333…
        v, why = surface.delta_iv([p for p in pts if p["delta"] != -0.20], "put", 0.25)
        self.assertIsNone(v)
        self.assertIn("not bracketed", why)

    def test_iv30_total_variance(self):
        views = [{"available": True, "T": 20 / 365, "atm_iv": 0.20}, {"available": True, "T": 40 / 365, "atm_iv": 0.30},
                 {"available": False, "T": 25 / 365, "atm_iv": 0.9}]
        w20, w40 = 0.04 * 20 / 365, 0.09 * 40 / 365
        want = math.sqrt((w20 + (w40 - w20) * 0.5) / (30 / 365))
        v, why = surface.iv_at_days(views, 30)
        self.assertAlmostEqual(v, want, places=12)
        v, why = surface.iv_at_days(views[:1], 30)
        self.assertIsNone(v)
        self.assertIn("no extrapolation", why)

    def test_vix_label(self):
        f = lambda v, d="2026-11-04": {"date": d, "value": v, "available_ms": 5}   # noqa: E731
        s = surface.vix_structure({"VIXCLS": f(18.0), "VXVCLS": f(20.0)}, "SPY")
        self.assertEqual((s["label"], s["slope"]), ("contango", 2.0))
        self.assertAlmostEqual(s["ratio"], 0.9)
        self.assertEqual(surface.vix_structure({"VIXCLS": f(25.0), "VXVCLS": f(22.0)}, "SPY")["label"], "backwardation")
        self.assertEqual(surface.vix_structure({"VIXCLS": f(20.0), "VXVCLS": f(20.0)}, "SPY")["label"], "flat")
        s = surface.vix_structure({"VIXCLS": f(18.0), "VXVCLS": f(20.0, "2026-11-03")}, "SPY")
        self.assertIsNone(s["label"])
        self.assertIn("different dates", s["reason"])
        self.assertIsNone(surface.vix_structure({"VIXCLS": f(18.0)}, "SPY")["label"])
        s = surface.vix_structure({"VIXCLS": f(18.0), "VXVCLS": f(20.0), "VXNCLS": f(22.0)}, "QQQ")
        self.assertEqual(s["vxn"]["value"], 22.0)

    def test_realised_vol(self):
        closes = [(date(2026, 10, 1) + timedelta(days=i), 100.0 * math.exp(0.01 * (i % 2))) for i in range(22)]
        rets = [math.log(b[1] / a[1]) for a, b in zip(closes, closes[1:])]
        m = sum(rets) / len(rets)
        want = math.sqrt(sum((x - m) ** 2 for x in rets) / (len(rets) - 1) * 252)
        v, why = surface.realised_vol(closes, 21, closes[-1][0])
        self.assertAlmostEqual(v, want, places=12)
        self.assertIsNone(surface.realised_vol(closes, 21, date(2026, 12, 1))[0])         # newest close stale
        self.assertIn("needed", surface.realised_vol(closes[:10], 21, closes[9][0])[1])


class ChainTest(OS2Case):
    def test_expected_move_rr_bf_on_a_constructed_chain(self):
        exp = D + timedelta(days=14)
        run, _ = build_chain(self.arch, D, SLOT, spot=600.0, expiries=[exp], strikes=list(range(570, 631, 5)),
                             rate_pct=0.0, q=0.0, vol_fn=lambda k: 0.2)
        cfg = self.cfg
        ch = quotes.load_chain(self.arch.conn, quotes.run_row(self.arch.conn, run), "SPY", self.acfg, cfg)
        pc = model.price_chain(ch, 0.0, cfg, self.acfg.coverage_min_pct)
        self.assertAlmostEqual(pc.q_of(exp), 0.0, places=8)            # parity-implied q of a q = 0 chain
        view = surface.expiry_view(pc, exp, cfg)
        self.assertAlmostEqual(view["atm_iv"], 0.2, places=6)
        self.assertAlmostEqual(view["rr25"], 0.0, places=6)            # flat smile → RR = BF = 0
        self.assertAlmostEqual(view["bf25"], 0.0, places=6)
        em = surface.expected_move(pc, exp, view)
        T = (datetime.combine(exp, calendars.REGULAR_CLOSE, tzinfo=NY).timestamp() * 1000 - SLOT) / (365.0 * 86400000)
        d1 = 0.5 * 0.2 * math.sqrt(T)                                   # S = K, r = q = 0
        straddle = 2 * 600 * (N(d1) - N(-d1))                           # call + put at the money
        self.assertEqual(em["strike"], 600)
        self.assertAlmostEqual(em["straddle"], straddle, places=6)
        self.assertAlmostEqual(em["one_sigma"], 0.2 * math.sqrt(T) * 600, places=4)
        self.assertAlmostEqual(em["iv_based"], math.sqrt(2 / math.pi) * 0.2 * math.sqrt(T) * 600, places=4)
        self.assertLess(abs(em["difference"] / em["straddle"]), 0.01)  # the model straddle matches — not −20 %
        self.assertAlmostEqual(em["difference"], em["straddle"] - em["iv_based"], places=12)

    def test_skewed_smile_has_negative_rr(self):
        run, _ = build_chain(self.arch, D, SLOT, expiries=[D + timedelta(days=28)],
                             strikes=list(range(560, 641, 5)))
        ch = quotes.load_chain(self.arch.conn, quotes.run_row(self.arch.conn, run), "SPY", self.acfg, self.cfg)
        pc = model.price_chain(ch, 0.0398, self.cfg, self.acfg.coverage_min_pct)
        v = surface.expiry_view(pc, D + timedelta(days=28), self.cfg)
        self.assertLess(v["rr25"], 0)            # puts richer than calls (skew −0.30 in the constructed smile)
        self.assertGreater(v["bf25"], 0)         # convex smile


class ImpliedQTest(OS2Case):
    def test_parity_q_per_expiry_and_no_fallback(self):
        e1, e2 = D + timedelta(days=14), D + timedelta(days=28)
        run, info = build_chain(self.arch, D, SLOT, expiries=[e1, e2], q=0.013)
        ch = quotes.load_chain(self.arch.conn, quotes.run_row(self.arch.conn, run), "SPY", self.acfg, self.cfg)
        pc = model.price_chain(ch, info["rate"], self.cfg, self.acfg.coverage_min_pct)
        for e in (e1, e2):
            self.assertAlmostEqual(pc.q_of(e), 0.013, places=8)        # exact mids → the true q
            self.assertEqual(pc.q_by_expiry[e]["pairs"], 7)             # strikes 582 … 618 (±3 %) with both sides
        # too few pairs → the expiry is unavailable; never the fixed per-symbol yield
        cfg = replace(self.cfg, parity_band_pct=0.5, parity_min_pairs=3)
        pc = model.price_chain(ch, info["rate"], cfg, self.acfg.coverage_min_pct)
        self.assertIsNone(pc.q_of(e1))
        self.assertFalse(pc.expiries[e1]["available_iv"])
        self.assertIn("no implied dividend yield", pc.expiries[e1]["reason_iv"])
        self.assertTrue(all(p.iv is None for p in pc.contracts))

    def test_robust_median(self):
        quotes_ = []
        from strategy_platform.options_structure.quotes import Quote
        S, T, r = 100.0, 0.1, 0.0
        for K, qtrue in ((99.0, 0.01), (100.0, 0.01), (101.0, 0.01), (102.0, 0.50)):   # one outlier pair
            c = bs.price("call", S, K, T, r, qtrue, 0.2)
            p = bs.price("put", S, K, T, r, qtrue, 0.2)
            for kind, v in (("call", c), ("put", p)):
                quotes_.append(Quote("X", kind, K, D, 100, v, v, 1, 1, None, None))
        out = model.implied_q(quotes_, S, T, r, replace(self.cfg, parity_band_pct=3.0, parity_min_pairs=3))
        self.assertAlmostEqual(out["q"], 0.01, places=10)
        self.assertEqual(out["pairs"], 4)


if __name__ == "__main__":
    unittest.main()
