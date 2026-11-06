"""OS2 point-in-time rules and the regime job (SP §21.12): OI before T+1 08:00 or of another session is ignored;
outputs carry max(available_at) of their inputs and the provisional flag; a gap → unavailable with the reason (no
fill); the rate must be archived by the slot; the job is idempotent and resumable, never stores a crashed
computation; the archive is opened read-only; the OS2 store is insert-only; no H1 / H2 statistics anywhere."""
import json
import math
import os
import sqlite3
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from os2_testkit import OS2Case, build_chain, ny_ms, occ  # noqa: E402

from strategy_platform.core import calendars  # noqa: E402
from strategy_platform.options_structure import config as scfg  # noqa: E402
from strategy_platform.options_structure import quotes, regime, store  # noqa: E402

D = date(2026, 11, 5)
SLOT = ny_ms(D, 10, 0)
PREV = calendars.previous_trading_day(D)


def closes(n=30, last=None):
    out, d = [], last or PREV
    for i in range(n):
        out.append((d, 600.0 * math.exp(0.005 * math.sin(i)), ny_ms(d, 16, 30)))
        d = calendars.previous_trading_day(d)
    return sorted(out)


class RegimeCase(OS2Case):
    def setUp(self):
        super().setUp()
        self.arch.fred("DTB3", PREV, 4.0, ny_ms(D, 8, 46))
        self.arch.fred("VIXCLS", PREV, 18.0, ny_ms(D, 8, 46))
        self.arch.fred("VXVCLS", PREV, 20.0, ny_ms(D, 8, 46))
        self.sconn = store.open_rw(os.path.join(self.tmp, "options_structure.db"))
        self.addCleanup(self.sconn.close)

    def ctx(self, cfg=None, bars=None):
        return regime.Context(cfg or self.cfg, self.acfg, bars_fn=lambda s, t: bars if bars is not None else closes(),
                              fomc=([date(2026, 12, 9)], None), cpi=([date(2026, 11, 13)], 0, None), clock_ms=lambda: 7)

    def parts(self, run, ctx=None, symbol="SPY"):
        rows = regime.compute(self.arch.conn, self.sconn, quotes.run_row(self.arch.conn, run), symbol,
                              ctx or self.ctx())
        return {r["part"]: r for r in rows}


class PointInTimeTest(RegimeCase):
    def test_full_computation_carries_max_available_and_provisional(self):
        run, info = build_chain(self.arch, D, SLOT, oi_fn=lambda k, K, e: 3000 if k == "put" else 1000)
        p = self.parts(run)
        self.assertEqual(set(p), set(store.PARTS))
        reg = p["regime"]
        self.assertEqual(reg["status"], "partial")                       # the flip is outside ±5 % → a reason
        self.assertEqual(reg["payload"]["gex_sign"], -1)
        self.assertEqual(reg["payload"]["gex_status"], "ok")
        self.assertEqual(len(reg["payload"]["gex_included_expiries"]), 5)
        self.assertEqual(reg["valid_for_ms"], SLOT)
        # max of: chain rows (slot − 2 s), OI (08:00 floor), rate / VIX (08:46), bars (prev 16:30)
        self.assertEqual(reg["available_ms"], SLOT - 2000)
        self.assertTrue(reg["provisional"])
        self.assertIn("archive.quote_max_age_s", reg["payload"]["provisional_keys"])
        self.assertNotIn("archive.rate_series", reg["payload"]["provisional_keys"])     # DTB3 confirmed 2026-11-06
        self.assertEqual(reg["payload"]["oi"]["newest_valid_for"], PREV.isoformat())
        self.assertEqual(reg["payload"]["oi"]["age_sessions"], 1)
        self.assertEqual(reg["payload"]["oi"]["coverage_pct"], 100.0)
        dq = reg["payload"]["dividend_yield"]
        self.assertEqual(dq["fixed_cross_check"], 0.012)
        self.assertTrue(all(abs(v["q"] - 0.012) < 1e-6 for v in dq["by_expiry"].values()))
        self.assertEqual(reg["payload"]["vix_label"], "contango")
        self.assertIsNotNone(reg["payload"]["em_position"])
        self.assertIn(info["oi_run"], reg["source_runs"])
        self.assertIn(run, reg["source_runs"])
        self.assertAlmostEqual(reg["payload"]["rate"]["value"], -math.log(1 - 0.04 * 91 / 360) * 365 / 91)
        text = json.dumps([r["payload"] for r in p.values()]).lower()
        for banned in ("h1", "h2", "r_first", "r_last", "hit_rate", "regression"):
            self.assertNotIn(f'"{banned}', text)                         # no pre-registration statistics

    def test_late_chain_rows_raise_available_at(self):
        run, _ = build_chain(self.arch, D, SLOT, oi=1000)
        self.arch.conn.execute("INSERT INTO os_chain_snapshots (run_id, symbol, spot, bid, ask, quote_t_ms, iv, "
                               "available_ms, observed_ms) VALUES (?, 'SPY261231C00600000', 600, 1, 1.1, ?, 0.2, ?, ?)",
                               (run, SLOT - 1000, SLOT + 60000, SLOT + 60000))
        self.assertEqual(self.parts(run)["quality"]["available_ms"], SLOT + 60000)

    def test_oi_before_the_floor_or_of_another_session_is_ignored(self):
        # OI of the previous session stamped 07:59 (before the 08:00 floor): never used
        run, _ = build_chain(self.arch, D, SLOT, oi=1000, oi_available_ms=ny_ms(D, 7, 59))
        p = self.parts(run)
        self.assertEqual(p["gex"]["status"], "unavailable")
        self.assertEqual(p["quality"]["payload"]["oi"]["with_oi"], 0)
        self.assertIn("no open interest", json.dumps(p["quality"]["payload"]["oi"]["missing"]))

    def test_oi_two_sessions_back_is_used_and_stated(self):
        # user decision 2026-11-06 (SP §21.3): the newest OI with valid_for ≤ the previous session — in practice D−2
        older = calendars.previous_trading_day(PREV)
        run, _ = build_chain(self.arch, D, SLOT, oi=1000, oi_valid_for=older, oi_available_ms=ny_ms(D, 8, 30))
        p = self.parts(run)
        self.assertEqual(p["gex"]["status"], "ok")
        oi = p["regime"]["payload"]["oi"]
        self.assertEqual((oi["max_valid_for"], oi["newest_valid_for"], oi["age_sessions"]),
                         (PREV.isoformat(), older.isoformat(), 2))
        self.assertEqual(p["gex"]["available_ms"], SLOT - 2000)

    def test_newer_oi_wins_per_contract_and_today_is_never_used(self):
        # fresh window D−1 / D−2 (user decision 2026-11-06 b, os2d): D−3 OI is NOT used (no fresh OI, contributes 0)
        older = calendars.previous_trading_day(PREV)
        d3 = calendars.previous_trading_day(older)
        e0 = D + timedelta(days=7)
        only_d3 = occ("SPY", e0, "put", 600)
        run, info = build_chain(self.arch, D, SLOT, oi_valid_for=older, oi_available_ms=ny_ms(PREV, 8, 0),
                                oi_fn=lambda k, K, e: None if (k, K, e) == ("put", 600, e0) else 1000)
        sym = self.arch.conn.execute("SELECT symbol FROM os_open_interest WHERE symbol != ? LIMIT 1",
                                     (only_d3,)).fetchone()[0]
        self.arch.oi(info["oi_run"], sym, 7, PREV, ny_ms(D, 8, 0))       # D−1 for ONE contract: newer → used
        self.arch.oi(info["oi_run"], sym, 9, D, ny_ms(D, 9, 0))          # "today" (valid_for > D−1): never
        self.arch.oi(info["oi_run"], only_d3, 5000, d3, ny_ms(older, 8, 0))    # only D−3 for another: not fresh
        ch = quotes.load_chain(self.arch.conn, quotes.run_row(self.arch.conn, run), "SPY", self.acfg, self.cfg)
        q = {x.occ: x for x in ch.quotes}
        self.assertEqual((q[sym].oi, q[sym].oi_valid_for, q[sym].oi_age_sessions), (7, PREV.isoformat(), 1))
        self.assertIsNone(q[only_d3].oi)
        self.assertEqual(q[only_d3].oi_age_sessions, 3)
        self.assertIn("no fresh OI", q[only_d3].oi_reason)
        s = quotes.oi_summary(ch, self.cfg.oi_max_age_sessions)
        self.assertEqual(s["valid_for_counts"], {older.isoformat(): s["with_oi"] - 1, PREV.isoformat(): 1})
        self.assertEqual(s["no_fresh_oi"], 1)
        self.assertEqual(s["age_distribution"], {"1": 1, "2": s["with_oi"] - 1, "3": 1})
        self.assertIn("D−1 … D−2", s["rule"])

    def test_oi_three_sessions_back_only_is_no_fresh_oi(self):
        d3 = calendars.previous_trading_day(calendars.previous_trading_day(PREV))
        run, _ = build_chain(self.arch, D, SLOT, oi=1000, oi_valid_for=d3, oi_available_ms=ny_ms(D, 8, 0))
        p = self.parts(run)
        self.assertEqual(p["gex"]["status"], "unavailable")              # every contract contributes 0
        oi = p["quality"]["payload"]["oi"]
        self.assertEqual((oi["with_oi"], oi["no_fresh_oi"]), (0, oi["contracts"]))
        self.assertEqual(oi["age_distribution"], {"3": oi["contracts"]})
        self.assertIn("no fresh OI", json.dumps(oi["missing"]))
        # with the window widened to … it is refused: the OS2 key may not exceed the archive's fresh window
        self.write_override("options_structure_config.json", {"oi_max_age_sessions": 3})
        with self.assertRaises(ValueError):
            scfg.load(self.tmp)

    def test_evaluability_excluded_oi_above_15_pct_makes_the_regime_unavailable(self):
        e0 = D + timedelta(days=7)
        stale = lambda exp, K: 301000 if exp == e0 else 30000            # noqa: E731 — the 7-day expiry fails
        # equal OI on 5 expiries → the excluded one carries 20 % > 15 % → not evaluable
        run, _ = build_chain(self.arch, D, SLOT, oi_fn=lambda k, K, e: 3000 if k == "put" else 1000,
                             quote_age_fn=stale)
        p = self.parts(run)
        self.assertEqual(p["gex"]["status"], "partial")
        ev = p["gex"]["payload"]["total"]["evaluable"]
        self.assertFalse(ev["ok"])
        self.assertAlmostEqual(ev["excluded_oi_pct"], 20.0)
        reg = p["regime"]
        self.assertEqual(reg["status"], "unavailable")
        self.assertIsNone(reg["payload"]["gex_sign"])
        self.assertTrue(any(r.startswith("GEX partial: excluded OI 20.0 % > 15 %") for r in reg["reasons"]))
        self.assertEqual(reg["payload"]["gex_excluded"]["oi_share_pct"], 20.0)
        self.assertFalse(reg["payload"]["gex_evaluable"]["ok"])

    def test_evaluability_excluded_oi_at_most_15_pct_keeps_the_regime(self):
        e0 = D + timedelta(days=7)
        stale = lambda exp, K: 301000 if exp == e0 else 30000            # noqa: E731
        run, _ = build_chain(self.arch, D, SLOT, quote_age_fn=stale,
                             oi_fn=lambda k, K, e: 100 if e == e0 else (3000 if k == "put" else 1000))
        reg = self.parts(run)["regime"]
        ev = reg["payload"]["gex_evaluable"]
        self.assertTrue(ev["ok"])
        self.assertLess(ev["excluded_oi_pct"], 15.0)
        self.assertEqual(reg["payload"]["gex_status"], "partial")
        self.assertEqual(reg["payload"]["gex_sign"], -1)
        self.assertNotEqual(reg["status"], "unavailable")

    def test_implied_q_flag_is_shown_and_the_expiry_stays_in_use(self):
        run, _ = build_chain(self.arch, D, SLOT, oi=1000, q=0.08)       # |q| 8 % > 5 % → flagged, still used
        p = self.parts(run)
        qual = p["quality"]["payload"]
        self.assertEqual(len(qual["q_flagged_expiries"]), 5)
        e = qual["expiries"][0]
        self.assertTrue(e["available_iv"])
        self.assertTrue(e["q_flags"][0].startswith("abs_q"))
        self.assertEqual(len(p["regime"]["payload"]["dividend_yield"]["flagged_expiries"]), 5)
        self.assertEqual(p["gex"]["status"], "ok")
        run2, _ = build_chain(self.arch, D, ny_ms(D, 10, 15), oi=1000, label="10:15")
        self.assertEqual(self.parts(run2)["quality"]["payload"]["q_flagged_expiries"], [])

    def test_missing_oi_is_left_out_not_zero(self):
        run, _ = build_chain(self.arch, D, SLOT, oi_fn=lambda k, K, e: None if K < 600 else 1000)
        p = self.parts(run)
        self.assertEqual(p["gex"]["status"], "ok")                       # OI coverage does not gate
        self.assertLess(p["regime"]["payload"]["oi"]["coverage_pct"], 100.0)
        cov = p["gex"]["payload"]["total"]["oi_coverage"]
        self.assertIn("OI reported for", p["regime"]["payload"]["oi"]["text"])
        self.assertLess(cov["share_pct"], 100.0)
        self.assertIn("no open interest", json.dumps(p["quality"]["payload"]["oi"]["missing"]))

    def test_oi_published_after_the_slot_is_not_used(self):
        run, _ = build_chain(self.arch, D, SLOT, oi=1000, oi_available_ms=SLOT + 1)
        self.assertEqual(self.parts(run)["gex"]["status"], "unavailable")

    def test_stale_quotes_make_the_chain_unavailable_never_filled(self):
        run, _ = build_chain(self.arch, D, SLOT, oi=1000, quote_age_ms=301000)        # > 300 s at the slot
        p = self.parts(run)
        self.assertEqual(p["quality"]["payload"]["flags"]["stale"], p["quality"]["payload"]["contracts"])
        for part in ("gex", "regime", "surface"):
            self.assertEqual(p[part]["status"], "unavailable", part)
        self.assertTrue(any("no archived expiry passes the quote-coverage rule" in r for r in p["regime"]["reasons"]))

    def test_no_rate_archived_by_the_slot(self):
        run, _ = build_chain(self.arch, D, SLOT, oi=1000)
        self.arch.conn.execute("INSERT INTO os_fred (series_id, date, value, available_ms, observed_ms) VALUES "
                               "('DTB3', ?, 4.1, ?, ?)", (D.isoformat(), SLOT + 1, SLOT + 1))
        # only the NEWER row is after the slot; the older one (PREV) still counts → available
        self.assertEqual(self.parts(run)["gex"]["status"], "ok")
        self.setUp_without_rate()                                        # a second archive without any DTB3 row
        run, _ = build_chain(self.arch, D, SLOT, oi=1000)
        p = self.parts(run, self.ctx())
        self.assertEqual(p["gex"]["status"], "unavailable")
        self.assertIn("risk-free rate unavailable", " ".join(p["regime"]["reasons"]))
        self.assertEqual(p["quality"]["status"], "ok")                   # the quality view is still stored

    def setUp_without_rate(self):
        from os2_testkit import Archive
        self.arch = Archive(os.path.join(self.tmp, "archive2.db"))
        self.addCleanup(self.arch.conn.close)


class StoreAndJobTest(RegimeCase):
    def test_archive_opened_read_only(self):
        conn = quotes.open_archive_ro(self.arch.path)
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("INSERT INTO os_gaps (slot_ms, kind, what, recorded_ms) VALUES (1, 'x', 'y', 1)")
        self.assertIsNone(quotes.open_archive_ro(os.path.join(self.tmp, "missing.db")))

    def test_store_is_insert_only(self):
        run, _ = build_chain(self.arch, D, SLOT, oi=1000)
        ctx = self.ctx()
        regime.run_pending(self.arch.conn, self.sconn, ctx)
        for t in store.TABLES:
            for sql in (f"UPDATE {t} SET rowid = rowid", f"DELETE FROM {t}"):
                with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                    self.sconn.execute(sql)
        with self.assertRaises(sqlite3.DatabaseError):
            self.sconn.execute("INSERT OR REPLACE INTO os2_assumptions (hash, json, first_ms) VALUES (?, '{}', 1)",
                               (ctx.ahash,))

    def test_job_idempotent_and_resumable(self):
        r1, _ = build_chain(self.arch, D, SLOT, oi=1000)
        r2, _ = build_chain(self.arch, D, ny_ms(D, 10, 15), oi=1000, label="10:15")
        self.arch.run("manual", ny_ms(D, 10, 20), label="manual")              # never counts for a slot
        self.arch.run("intraday", ny_ms(D, 10, 30), status="failed", label="10:30")
        ctx = self.ctx()
        res = regime.run_pending(self.arch.conn, self.sconn, ctx, limit=1)    # QQQ + SPY of r1 … one at a time
        self.assertEqual(res["processed"], 1)
        res = regime.run_pending(self.arch.conn, self.sconn, ctx)
        self.assertEqual(res["processed"], 3)
        self.assertEqual(regime.run_pending(self.arch.conn, self.sconn, ctx)["processed"], 0)
        n = self.sconn.execute("SELECT COUNT(*) FROM os2_results").fetchone()[0]
        self.assertEqual(n, 2 * 2 * 4)                                         # 2 runs × 2 symbols × 4 parts
        qqq = store.latest(self.sconn, "QQQ", "regime", ny_ms(D, 11, 0), scfg.MODEL_VERSION, ctx.ahash)
        self.assertEqual(qqq["status"], "unavailable")                        # no QQQ chain in the run
        # a changed assumption → a new hash → recomputed, old rows kept
        from dataclasses import replace
        ctx2 = self.ctx(replace(self.cfg, spread_max_pct=40.0))
        self.assertNotEqual(ctx2.ahash, ctx.ahash)
        self.assertEqual(regime.run_pending(self.arch.conn, self.sconn, ctx2)["processed"], 4)

    def test_crashing_computation_is_not_stored_and_retried(self):
        build_chain(self.arch, D, SLOT, oi=1000)
        ctx = self.ctx(bars=None)
        orig = regime.compute
        calls = {"n": 0}

        def boom(*a, **k):
            calls["n"] += 1
            raise RuntimeError("synthetic failure")
        regime.compute = boom
        self.addCleanup(setattr, regime, "compute", orig)
        regime._attempts.clear()
        self.addCleanup(regime._attempts.clear)
        with self.assertLogs("strategy_platform.options_structure.regime", level="ERROR"):
            res = regime.run_pending(self.arch.conn, self.sconn, ctx)
        self.assertEqual((res["processed"], res["failed"]), (0, 2))
        self.assertEqual(self.sconn.execute("SELECT COUNT(*) FROM os2_results").fetchone()[0], 0)
        with self.assertLogs("strategy_platform.options_structure.regime", level="ERROR"):
            regime.run_pending(self.arch.conn, self.sconn, ctx)
            regime.run_pending(self.arch.conn, self.sconn, ctx)
        res = regime.run_pending(self.arch.conn, self.sconn, ctx)              # 3 attempts → skipped this process
        self.assertEqual(res["skipped"], 2)
        regime.compute = orig
        regime._attempts.clear()
        self.assertEqual(regime.run_pending(self.arch.conn, self.sconn, ctx)["processed"], 2)

    def test_rr_percentile_needs_history(self):
        from dataclasses import replace
        cfg = replace(self.cfg, rr_history_min=2)
        for i, hh in enumerate((10, 11, 12)):
            build_chain(self.arch, D, ny_ms(D, hh, 0), oi=1000, label=f"{hh}:00")
        regime.run_pending(self.arch.conn, self.sconn, self.ctx(cfg))
        rows = store.history(self.sconn, "SPY", "surface", 0, ny_ms(D, 13, 0), scfg.MODEL_VERSION, self.ctx(cfg).ahash)
        pct = [r["payload"]["rr_percentile"] for r in rows]
        self.assertEqual([p["n"] for p in pct], [0, 1, 2])
        self.assertIsNone(pct[0]["value"])
        self.assertIsNotNone(pct[2]["value"])


class ConfigTest(OS2Case):
    def test_override_validation(self):
        self.write_override("options_structure_config.json", {"dividend_yield": {"SPY": 0.013}, "spread_max_pct": 30})
        c = scfg.load(self.tmp)
        self.assertEqual((c.dividend_yield["SPY"], c.dividend_yield["QQQ"], c.spread_max_pct), (0.013, 0.006, 30.0))
        for bad, word in (({"spread_max_pct": 0}, "spread_max_pct"), ({"nope": 1}, "unknown key"),
                          ({"dividend_yield": {"SPY": 0.5}}, "dividend_yield"), ({"flip_points": 100}, "odd"),
                          ({"rv_sessions": 2.5}, "whole number"), ({"dividend_yield": {"IWM": 0.01}}, "keys")):
            self.write_override("options_structure_config.json", bad)
            with self.assertRaises(ValueError) as cm:
                scfg.load(self.tmp)
            self.assertIn(word, str(cm.exception))
        with open(os.path.join(self.tmp, "options_structure_config.json"), "w") as fh:
            fh.write('{"iv_max": NaN}')
        with self.assertRaises(ValueError):
            scfg.load(self.tmp)

    def test_assumptions_hash_covers_archive_numbers(self):
        from dataclasses import replace
        h = scfg.assumptions_hash(self.cfg, self.acfg)
        self.assertNotEqual(h, scfg.assumptions_hash(self.cfg, replace(self.acfg, coverage_min_pct=70.0)))
        self.assertNotEqual(h, scfg.assumptions_hash(replace(self.cfg, dividend_yield={"SPY": 0.0, "QQQ": 0.006}),
                                                     self.acfg))
        prov, keys = scfg.provisional(self.cfg, self.acfg)
        self.assertTrue(prov)
        self.assertIn("parity_min_pairs", keys)
        self.assertNotIn("dividend_yield", keys)                         # a cross-check only since 2026-11-06
        self.assertIn("q_flag_abs_pct", keys)
        self.assertNotIn("max_excluded_oi_pct", keys)                    # user decisions 2026-11-06 b: not provisional
        self.assertNotIn("oi_max_age_sessions", keys)
        for k, v in (("max_excluded_oi_pct", 20.0), ("oi_max_age_sessions", 1), ("q_flag_abs_pct", 6.0)):
            self.assertNotEqual(h, scfg.assumptions_hash(replace(self.cfg, **{k: v}), self.acfg), k)
        from strategy_platform.options_archive.config import OI_FRESH_SESSIONS
        self.assertEqual(self.cfg.oi_max_age_sessions, OI_FRESH_SESSIONS)  # the same window as the collector

    def test_confirmation_mechanism(self):
        self.write_override("options_structure_config.json", {"spread_max_pct": 40,
                                                               "confirmed": {"spread_max_pct": 40, "rr_history_min": 20},
                                                               "confirmed_note": "user 2026-11-07"})
        cfg = scfg.load(self.tmp)
        conf = scfg.load_confirmed(self.tmp, cfg)
        prov, keys = scfg.provisional(cfg, self.acfg, conf)
        self.assertNotIn("spread_max_pct", keys)
        self.assertNotIn("rr_history_min", keys)
        self.assertIn("parity_band_pct", keys)
        # confirmations do not change the hash (the history continues)
        self.assertEqual(scfg.assumptions_hash(cfg, self.acfg),
                         scfg.assumptions_hash(scfg.Config(spread_max_pct=40.0), self.acfg))
        # a later change of the value makes the key provisional again
        self.write_override("options_structure_config.json", {"spread_max_pct": 45, "confirmed": {"spread_max_pct": 40}})
        cfg = scfg.load(self.tmp)
        self.assertIn("spread_max_pct", scfg.provisional(cfg, self.acfg, scfg.load_confirmed(self.tmp, cfg))[1])
        for bad, word in (({"confirmed": {"iv_max": 5}}, "only"), ({"confirmed": {"spread_max_pct": "x"}}, "number"),
                          ({"confirmed": {"parity_min_pairs": 2.5}}, "whole"), ({"confirmed": [1]}, "object"),
                          ({"confirmed_note": "x" * 201}, "confirmed_note")):
            self.write_override("options_structure_config.json", bad)
            with self.assertRaises(ValueError) as cm:
                scfg.load_confirmed(self.tmp)
            self.assertIn(word, str(cm.exception))


if __name__ == "__main__":
    unittest.main()
