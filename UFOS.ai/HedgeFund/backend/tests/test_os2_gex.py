"""OS2 GEX (SP §21.12): a constructed chain with a known answer (gamma written out here, independently of bs.py),
the gamma flip of a two-strike case solved by hand, the three dealer-variant definitions, walls, buckets, the OI rule
of 2026-11-06 (only contracts with OI enter, OI coverage shown, a bucket without any OI is unavailable) and the quote
coverage gating (a bucket with a badly quoted expiry is unavailable — never a partial sum)."""
import math
import os
import sys
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from os2_testkit import NY, OS2Case, build_chain, ny_ms  # noqa: E402

from strategy_platform.core import calendars  # noqa: E402
from strategy_platform.options_structure import gex, model, quotes  # noqa: E402

D = date(2026, 11, 5)
SLOT = ny_ms(D, 10, 0)


def hand_gamma(S, K, T, sigma):
    """r = q = 0: gamma = φ(d1) / (S σ √T), d1 = (ln(S/K) + σ²T/2) / (σ √T)."""
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    return math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi) / (S * sigma * math.sqrt(T))


def years(exp):
    return (datetime.combine(exp, calendars.REGULAR_CLOSE, tzinfo=NY).timestamp() * 1000 - SLOT) / (365.0 * 86400000)


class GexCase(OS2Case):
    def priced(self, run, symbol="SPY", rate=0.0):
        cfg = replace(self.cfg, parity_min_pairs=1)               # two-strike chains: one parity pair is enough
        ch = quotes.load_chain(self.arch.conn, quotes.run_row(self.arch.conn, run), symbol, self.acfg, cfg)
        return model.price_chain(ch, rate, cfg, self.acfg.coverage_min_pct), cfg


class KnownAnswerTest(GexCase):
    def test_single_put_and_variants(self):
        exp = D + timedelta(days=14)
        oi = {("put", 600): 2000, ("call", 600): 0, ("put", 610): 0, ("call", 610): 500}
        run, _ = build_chain(self.arch, D, SLOT, spot=600.0, expiries=[exp], strikes=[600, 610], rate_pct=0.0, q=0.0,
                             vol_fn=lambda k: 0.2, oi_fn=lambda kind, K, e: oi[(kind, K)])
        pc, cfg = self.priced(run)
        g = gex.compute(pc, cfg)
        self.assertTrue(g["available"], g["reasons"])
        T = years(exp)
        put600 = 2000 * hand_gamma(600, 600, T, 0.2) * 100 * 600 ** 2 * 0.01
        call610 = 500 * hand_gamma(600, 610, T, 0.2) * 100 * 600 ** 2 * 0.01
        tot = g["total"]["value"]
        self.assertEqual(g["total"]["status"], "ok")
        self.assertAlmostEqual(tot["A"], call610 - put600, delta=1e-6 * put600)       # calls +, puts −
        self.assertAlmostEqual(tot["B"], -call610 - put600, delta=1e-6 * put600)      # all −
        # C: put 600 with K = S is ITM (not K < S) → +; call 610 OTM (K > S) → −
        self.assertAlmostEqual(tot["C"], put600 - call610, delta=1e-6 * put600)
        self.assertEqual(g["walls"], {"call_wall": 610.0, "put_wall": 600.0})
        rows = {r["strike"]: r for r in g["per_strike"]}
        self.assertAlmostEqual(rows[600.0]["put_gex"], put600, delta=1e-6 * put600)
        self.assertEqual(g["signs"], {"A": -1, "B": -1, "C": 1})
        self.assertFalse(g["agree"])
        self.assertIn("USD per 1 % move", g["convention"])

    def test_variant_definitions(self):
        S = 100.0
        self.assertEqual([gex.dealer_sign("A", k, K, S) for k, K in (("call", 90), ("put", 90), ("call", 110))],
                         [1, -1, 1])
        self.assertEqual({gex.dealer_sign("B", k, K, S) for k in ("call", "put") for K in (90, 100, 110)}, {-1})
        # C: OTM call K > S, OTM put K < S → −; ITM (incl. K = S) → +
        self.assertEqual([gex.dealer_sign("C", "call", K, S) for K in (90, 100, 110)], [1, 1, -1])
        self.assertEqual([gex.dealer_sign("C", "put", K, S) for K in (90, 100, 110)], [-1, 1, 1])
        with self.assertRaises(ValueError):
            gex.dealer_sign("D", "call", 1, 1)


class FlipTest(GexCase):
    def test_two_strike_flip_solved_by_hand(self):
        # A: put 590 (−), call 610 (+), equal OI, same σ and T, r = q = 0 → total GEX(S) = c·S²·[γ(S,610) − γ(S,590)];
        # zero where φ(d1(590)) = φ(d1(610)) ⇔ d1(590) = −d1(610) ⇔ S* = √(590·610) · exp(−σ²T/2)
        exp = D + timedelta(days=14)
        oi = {("put", 590): 1000, ("call", 590): 0, ("put", 610): 0, ("call", 610): 1000}
        run, _ = build_chain(self.arch, D, SLOT, spot=600.0, expiries=[exp], strikes=[590, 610], rate_pct=0.0, q=0.0,
                             vol_fn=lambda k: 0.2, oi_fn=lambda kind, K, e: oi[(kind, K)])
        pc, cfg = self.priced(run)
        g = gex.compute(pc, replace(cfg, flip_points=401))
        T = years(exp)
        s_star = math.sqrt(590 * 610) * math.exp(-0.5 * 0.04 * T)
        self.assertAlmostEqual(g["flip"]["A"]["level"], s_star, delta=0.01)
        self.assertAlmostEqual(g["flip"]["A"]["distance_pct"], 100 * (s_star / 600 - 1), delta=0.002)
        self.assertIsNone(g["flip"]["B"]["level"])                      # all − : never crosses zero
        self.assertIn("no sign change", g["flip"]["B"]["reason"])
        curve = g["flip_curve"]
        self.assertEqual(len(curve["spots"]), 401)
        self.assertAlmostEqual(curve["spots"][200], 600.0)              # the spot is the middle grid point
        self.assertLess(curve["A"][0], 0)                               # below S*: the put dominates
        self.assertGreater(curve["A"][-1], 0)


class CoverageTest(GexCase):
    def test_only_contracts_with_oi_enter_and_coverage_is_shown(self):
        e1, e2 = D + timedelta(days=7), D + timedelta(days=21)
        run, info = build_chain(self.arch, D, SLOT, expiries=[e1, e2], rate_pct=4.0,
                                oi_fn=lambda kind, K, e: None if e == e1 else (100 if K != 600 else None))
        pc, cfg = self.priced(run, rate=info["rate"])
        self.assertEqual(pc.expiries[e1]["with_oi"], 0)
        self.assertTrue(pc.expiries[e1]["available_iv"])                # quotes fine — OI does not gate
        g = gex.compute(pc, cfg)
        self.assertTrue(g["available"], g["reasons"])
        self.assertFalse(g["buckets"]["0-7"]["available"])
        self.assertIn("no contract in this bucket has open interest", g["buckets"]["0-7"]["reason"])
        self.assertTrue(g["buckets"]["8-30"]["available"])
        self.assertEqual(g["total"]["value"], g["buckets"]["8-30"]["total"])   # contracts without OI: contribute 0
        self.assertEqual(g["total"]["status"], "ok")                    # OI does not exclude an expiry
        cov = g["total"]["oi_coverage"]
        self.assertEqual((cov["contracts"], cov["with_oi"]), (44, 20))     # 2 × 11 strikes × C/P; e2 without K = 600
        self.assertAlmostEqual(cov["share_pct"], 100 * 20 / 44, places=2)
        self.assertEqual(cov["text"], "OI reported for 45.5 % of the contracts")

    def test_total_excludes_badly_quoted_expiries_and_says_so(self):
        # user decision 2026-11-06: total GEX = sum over the quote-qualified expiries; the bucket view stays strict
        e1, e2 = D + timedelta(days=7), D + timedelta(days=21)
        run, info = build_chain(self.arch, D, SLOT, expiries=[e1, e2], rate_pct=4.0, oi=100,
                                quote_age_fn=lambda e, K: 400000 if e == e1 else 30000)      # e1: all quotes stale
        pc, cfg = self.priced(run, rate=info["rate"])
        self.assertFalse(pc.expiries[e1]["available_iv"])
        g = gex.compute(pc, cfg)
        t = g["total"]
        self.assertTrue(g["available"])
        self.assertEqual((g["status"], t["status"]), ("partial", "partial"))
        self.assertEqual(t["included"], [e2.isoformat()])
        self.assertEqual([x["expiration"] for x in t["excluded"]], [e1.isoformat()])
        self.assertEqual(t["excluded_text"], "0-7 d: 1 of 1 expiries excluded (0 % usable quotes)")
        self.assertEqual(t["value"], g["buckets"]["8-30"]["total"])
        self.assertIsNone(t["excluded_gamma_share_pct"])              # no gamma for failed quotes → the OI instead
        self.assertEqual(t["excluded_oi"]["contracts_with_oi"], 22)
        self.assertAlmostEqual(t["excluded_oi"]["share_of_total_oi_pct"], 50.0)
        self.assertTrue(any("partial: 0-7 d" in r for r in g["reasons"]))
        self.assertFalse(g["buckets"]["0-7"]["available"])             # the bucket view stays strict
        self.assertIn(e1.isoformat(), g["buckets"]["0-7"]["reason"])
        self.assertEqual(g["buckets"]["31-45"]["reason"], "no archived expiry in this bucket")
        self.assertIsNotNone(g["flip_curve"])

    def test_excluded_gamma_share_where_computable(self):
        e1, e2 = D + timedelta(days=7), D + timedelta(days=21)
        run, info = build_chain(self.arch, D, SLOT, expiries=[e1, e2], rate_pct=4.0, oi=100,
                                quote_age_fn=lambda e, K: 400000 if (e == e1 and K < 600) else 30000)
        pc, cfg = self.priced(run, rate=info["rate"])
        self.assertFalse(pc.expiries[e1]["available_iv"])             # 6 of 11 strikes usable → 54.5 % < 80 %
        g = gex.compute(pc, cfg)
        S, r = pc.chain.spot, pc.rate
        ex = sum(gex.contract_gex(p, S, r) for p in pc.contracts if p.gex_ok and p.q.expiration == e1)
        inc = sum(gex.contract_gex(p, S, r) for p in pc.contracts if p.gex_ok and p.q.expiration == e2)
        self.assertGreater(ex, 0)
        self.assertAlmostEqual(g["total"]["excluded_gamma_share_pct"], round(100 * ex / (ex + inc), 2))

    def test_no_qualified_expiry(self):
        run, info = build_chain(self.arch, D, SLOT, expiries=[D + timedelta(days=7)], rate_pct=4.0, oi=100,
                                quote_age_fn=lambda e, K: 400000)
        pc, cfg = self.priced(run, rate=info["rate"])
        g = gex.compute(pc, cfg)
        self.assertFalse(g["available"])
        self.assertEqual(g["total"]["reason"], "no archived expiry passes the quote-coverage rule")


if __name__ == "__main__":
    unittest.main()
