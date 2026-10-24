"""SPEC §16 (stage 6) — discovery loop units without the engine: scoring (penalties, pre-checks, holdout
guard, projected G4), selection (k/c, correlation duplicates, finalists), bounded parameter search, loop budget
(limits, blocks, per-run cap, the central budget window hook), diagnosis schema, config validation.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_loop_units -v
"""
import json
import math
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strategy_platform.discovery import budget, loop, prompts as lp, scoring, search, selection  # noqa: E402
from strategy_platform.research import loop as hook  # noqa: E402
from strategy_platform.storage import db  # noqa: E402
import test_sp_loop_support as ls  # noqa: E402

TH = {"g1_ic_t": 2.0, "g7_min_trades": 100, "g8_capacity_multiple": 10.0}


def card(**kw):
    m = {"performance": {"sharpe": 1.2, "max_drawdown": 0.10, "trades_closed": 150, "total_return": 0.3, "cagr": 0.1},
         "exposure": {"turnover_annual": 10.0},
         "costs": {"pct_of_gross_pnl": 20.0, "gross_pnl": 1000.0},
         "benchmark": {"alpha_annual": 0.05},
         "signal_quality": {"mode": "cross_sectional", "horizons": [{"horizon": 1, "t_stat": 1.0},
                                                                     {"horizon": 5, "t_stat": 3.0},
                                                                     {"horizon": 63, "t_stat": 9.0}]},
         "capacity": {"capital": 100000.0, "capacity_usd": 5e6}}
    for k, v in kw.items():
        grp, _, key = k.partition("__")
        m[grp][key] = v
    return {"id": "bt-1", "kind": "backtest", "spec": {"universe": {"symbols": ["AAA", "BBB"]}},
            "request": {"capital": 100000.0}, "metrics": m,
            "range": {"start": "2020-01-02", "end": "2024-10-07",
                      "holdout": {"start": "2024-10-08", "end": "2026-10-08", "access": False}}}


class ScoringTest(unittest.TestCase):
    def test_clean_card_scores_its_sharpe(self):
        sc = scoring.score(card(), TH)
        self.assertEqual(sc["score"], 1.2)
        self.assertTrue(sc["checks_passed"])
        self.assertEqual([c["key"] for c in sc["checks"]], list(scoring.CHECKS))
        sig = next(c for c in sc["checks"] if c["key"] == "signal")
        self.assertEqual(sig["value"], 3.0)          # horizon 63 is not a pre-check horizon

    def test_penalties(self):
        sc = scoring.score(card(exposure__turnover_annual=62.0, performance__max_drawdown=0.30,
                                costs__pct_of_gross_pnl=90.0), TH)
        self.assertAlmostEqual(sc["penalties"]["turnover"], 0.5)
        self.assertAlmostEqual(sc["penalties"]["drawdown"], 0.2)
        self.assertAlmostEqual(sc["penalties"]["costs"], 0.2)
        self.assertAlmostEqual(sc["score"], 1.2 - 0.9)
        capped = scoring.score(card(exposure__turnover_annual=1000.0, costs__pct_of_gross_pnl=10000.0), TH)
        self.assertEqual((capped["penalties"]["turnover"], capped["penalties"]["costs"]), (1.0, 1.0))

    def test_failed_checks_cost_a_quarter_each_and_missing_values_fail(self):
        sc = scoring.score(card(performance__trades_closed=20, benchmark__alpha_annual=None,
                                capacity__capacity_usd=None), TH)
        self.assertEqual(sorted(sc["failed_checks"]), ["alpha", "capacity", "trades"])
        self.assertAlmostEqual(sc["score"], 1.2 - 0.75)
        self.assertFalse(sc["checks_passed"])

    def test_no_sharpe_no_score(self):
        sc = scoring.score(card(performance__sharpe=None), TH)
        self.assertIsNone(sc["score"])
        self.assertIn("not scorable", sc["reason"])

    def test_holdout_guard(self):
        for bad in ({"kind": "holdout"}, {"kind": "validation"}):
            c = dict(card(), **bad)
            with self.assertRaises(scoring.HoldoutDataRefused):
                scoring.score(c, TH)
        c = card()
        c["range"]["holdout"]["access"] = True
        with self.assertRaises(scoring.HoldoutDataRefused):
            scoring.score(c, TH)
        c = card()
        c["range"]["end"] = "2024-10-08"            # inside the window
        with self.assertRaises(scoring.HoldoutDataRefused):
            scoring.score(c, TH)

    def test_score_facts_have_ids_and_displays(self):
        f = scoring.score_facts(scoring.score(card(), TH))
        ids = {x["id"] for x in f}
        self.assertIn("score.sharpe", ids)
        self.assertIn("score.check.trades.status", ids)
        self.assertTrue(all(isinstance(x["display"], str) for x in f))


def pop(vid, slot, score, returns=None, checks=True):
    return {"variant_id": vid, "slot": slot, "score": score, "sharpe": score, "returns": returns or {},
            "checks_passed": checks}


class SelectionTest(unittest.TestCase):
    def test_k_and_c(self):
        self.assertEqual([(n, selection.k_elites(n), selection.c_culled(n)) for n in (2, 3, 6, 8)],
                         [(2, 1, 1), (3, 1, 1), (6, 2, 2), (8, 3, 2)])

    def test_rank_elites_refine_cull(self):
        p = [pop("a", 1, 0.5), pop("b", 2, 1.5), pop("c", 3, None), pop("d", 4, -0.2), pop("e", 5, 0.9),
             pop("f", 6, 0.1)]
        r = selection.select(p, 6)
        self.assertEqual(r["ranking"], ["b", "e", "a", "f", "d", "c"])
        self.assertEqual(r["elites"], ["b", "e"])
        self.assertEqual(sorted(r["culled"]), ["c", "d"])          # the unscorable first, then the worst
        self.assertEqual(r["refine"], ["a", "f"])

    def test_correlated_duplicate_is_culled_and_never_finalist(self):
        base = {f"2020-{1 + i // 28:02d}-{1 + i % 28:02d}": math.sin(i) / 100 for i in range(100)}
        twin = {d: v * 2 for d, v in base.items()}                  # correlation 1
        other = {d: math.cos(i * 3.1) / 100 for i, d in enumerate(base)}
        p = [pop("a", 1, 1.0, base), pop("b", 2, 0.9, twin), pop("c", 3, 0.5, other)]
        r = selection.select(p, 3)
        self.assertIn("b", r["duplicates"])
        self.assertEqual(r["duplicates"]["b"]["of"], "a")
        self.assertIn("b", r["culled"])
        self.assertIn("duplicate of a", r["reasons"]["b"])
        self.assertEqual(selection.finalists(p, 3, r["duplicates"]), ["a"])
        short = {d: v for d, v in list(base.items())[:30]}
        self.assertIsNone(selection.correlation(base, short))        # < 60 common days: no verdict

    def test_finalists_need_positive_score_and_checks(self):
        p = [pop("a", 1, 2.0, checks=False), pop("b", 2, -0.1), pop("c", 3, 0.4)]
        self.assertEqual(selection.finalists(p, 3, {}), ["c"])
        self.assertEqual(selection.finalists([pop("x", 1, -1.0)], 3, {}), [])


class SearchTest(unittest.TestCase):
    def test_candidates_deterministic_bounded_and_unique(self):
        spec = ls.smooth_spec(1)
        spec["parameters"]["zz"] = {"value": 3, "min": 1, "max": 9, "step": 2, "integer": True}   # unused
        c = search.candidates(spec, 3)
        self.assertEqual([(x["parameter"], x["value"]) for x in c], [("lookback", 5), ("lookback", 15)])
        self.assertEqual(search.candidates(spec, 1)[0]["value"], 5)
        self.assertEqual(search.candidates(spec, 0), [])
        excl = {search.logic_hash(c[0]["spec"])}
        self.assertEqual([x["value"] for x in search.candidates(spec, 3, exclude=excl)], [15])
        edge = ls.smooth_spec(1, lookback=20)
        self.assertEqual([x["value"] for x in search.candidates(edge, 3)], [15])     # clamped at max
        self.assertEqual(search.logic_hash(ls.smooth_spec(1, name="x")), search.logic_hash(ls.smooth_spec(1, name="y")))


class TempDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sp6-units-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        db.close()
        db.init(os.path.join(self.tmp, "hedgefund.db"))
        self.addCleanup(db.close)
        budget._spend_unrecorded = False
        self.addCleanup(setattr, budget, "_spend_unrecorded", False)


class BudgetTest(TempDb):
    def test_defaults_and_blocks(self):
        self.assertEqual(budget.limits(), {"monthly_usd": 10.0, "daily_usd": 2.0, "per_run_usd": 1.0})
        b = budget.LoopBudget("lr-aaaaaaaaaaaa")
        self.assertIsNone(b.check(0.5))
        b.add(0.9)
        self.assertEqual(b.check(0.2), budget.RUN_REACHED_MESSAGE)
        self.assertIsNone(budget.LoopBudget("lr-bbbbbbbbbbbb").check(0.2))       # per run
        budget.apply({"daily_usd": 1.0})
        self.assertEqual(budget.LoopBudget("lr-bbbbbbbbbbbb").check(0.2), budget.DAY_REACHED_MESSAGE)
        budget.apply({"monthly_usd": 0.0})
        self.assertEqual(b.check(0.0), budget.OFF_MESSAGE)
        budget.apply({"monthly_usd": 0.95, "daily_usd": 5.0, "per_run_usd": 5.0})
        self.assertEqual(budget.LoopBudget("lr-cccccccccccc").check(0.1), budget.MONTH_REACHED_MESSAGE)
        rows = [r for r in db.list_audit(20) if r["action"] == "loop_budget_changed"]
        self.assertEqual(len(rows), 3)

    def test_limit_change_applies_immediately_and_fail_closed(self):
        b = budget.LoopBudget("lr-aaaaaaaaaaaa")
        budget.apply({"per_run_usd": 0.0})
        self.assertEqual(b.check(0.01), budget.RUN_OFF_MESSAGE)
        db.meta_set(db.LOOP_DAILY_KEY, "garbage")
        self.assertIn("invalid", b.check(0.01))
        db.meta_set(db.LOOP_DAILY_KEY, "2.0")
        budget.apply({"per_run_usd": 1.0})
        db.meta_set(db.LOOP_SPEND_DAY_PREFIX + b.day, "nan")
        self.assertEqual(b.check(0.01), budget.SPEND_UNREADABLE_MESSAGE)

    def test_spend_write_failure_pauses(self):
        b = budget.LoopBudget("lr-aaaaaaaaaaaa")
        orig = db.loop_spend_add
        db.loop_spend_add = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk"))
        try:
            b.add(0.01)
        finally:
            db.loop_spend_add = orig
        self.assertEqual(b.check(0.0), budget.SPEND_UNRECORDED_MESSAGE)
        self.assertEqual(budget.paused_reason(), budget.SPEND_UNRECORDED_MESSAGE)

    def test_central_window_hook(self):
        entries = hook.ai_budget_entries()
        self.assertEqual([e["key"] for e in entries], ["loop", "loop_month", "loop_run"])
        for e in entries:
            self.assertTrue({"key", "label", "group", "period", "period_id", "limit_usd", "default_usd", "min", "max",
                             "step", "spent_usd", "remaining_usd", "enabled", "status", "message", "editable",
                             "applies", "notes", "cost_log"} <= set(e))
            self.assertEqual(e["status"], "ok")
            json.dumps(e, allow_nan=False)
        hook.ai_budget_put("loop_month", 3.5)
        self.assertEqual(budget.limits()["monthly_usd"], 3.5)
        with self.assertRaises(ValueError):
            hook.ai_budget_put("loop", 500.0)
        with self.assertRaises(ValueError):
            hook.ai_budget_put("fund", 1.0)
        hook.ai_budget_put("loop", 0.0)
        e = {x["key"]: x for x in hook.ai_budget_entries()}
        self.assertEqual((e["loop"]["status"], e["loop"]["enabled"]), ("blocked", False))


class MidnightAndProjectionTest(TempDb):
    def test_m2_budget_follows_the_utc_day(self):
        from datetime import datetime, timezone
        clock = {"now": datetime(2026, 10, 17, 23, 59, tzinfo=timezone.utc)}
        orig = budget.utc_now
        budget.utc_now = lambda: clock["now"]
        self.addCleanup(setattr, budget, "utc_now", orig)
        budget.apply({"daily_usd": 1.0, "per_run_usd": 50.0, "monthly_usd": 50.0})
        b = budget.LoopBudget("lr-aaaaaaaaaaaa")
        b.add(0.9)
        self.assertEqual(b.check(0.2), budget.DAY_REACHED_MESSAGE)
        clock["now"] = datetime(2026, 10, 18, 0, 1, tzinfo=timezone.utc)
        self.assertIsNone(b.check(0.2))                              # the new day has its own cap
        b.add(0.3)
        self.assertEqual(db.loop_spend_get("2026-10", "2026-10-18")[1], 0.3)
        self.assertAlmostEqual(db.loop_spend_get("2026-10", "2026-10-17")[1], 0.9)
        clock["now"] = datetime(2026, 11, 1, 0, 1, tzinfo=timezone.utc)
        b.add(0.1)
        self.assertEqual(db.loop_spend_get("2026-11", "2026-11-01")[0], 0.1)

    def test_l7_projection_counts_validations_and_skips_holdout_cards(self):
        out = scoring.projected_g4(None, 5, 2)
        self.assertEqual((out["trials_projected"], out["validations_planned"]), (7, 2))
        rows = [{"id": "a", "kind": "backtest", "metrics_json": json.dumps({"performance": {"sharpe": 0.5, "years": 4.0}})},
                {"id": "b", "kind": "backtest", "metrics_json": json.dumps({"performance": {"sharpe": 0.6, "years": 4.0}})},
                {"id": "h", "kind": "holdout", "metrics_json": json.dumps({"performance": {"sharpe": 9.0, "years": 0.1}})}]
        orig, otc = db.family_done_runs, scoring.__dict__.get("x")
        db.family_done_runs = lambda fam: rows
        from strategy_platform.validation import families
        tc = families.trial_count
        families.trial_count = lambda fam: 3
        try:
            p = scoring.projected_g4("s-fam", 0)
        finally:
            db.family_done_runs = orig
            families.trial_count = tc
        self.assertEqual((p["years"], p["variance_source"], p["trial_variance"]), (4.0, "minimum", 0.25))


class SchemaTest(unittest.TestCase):
    def test_diagnosis_schema(self):
        ok = lp.validate_diagnosis(json.loads(ls.DIAG))
        self.assertEqual(set(ok), {"summary", "weaknesses", "ideas"})
        for bad in ({"summary": ""}, {"summary": "x", "extra": 1}, {"summary": "x" * 601},
                    {"summary": "x", "weaknesses": ["y"] * 6}, {"summary": "x", "ideas": ["z" * 201]}, []):
            with self.assertRaises(ValueError):
                lp.validate_diagnosis(bad)

    def test_config_validation(self):
        self.assertEqual(loop.check_config({"seed": {"kind": "idea", "text": "momentum"}}), {})
        e = loop.check_config({"seed": {"kind": "strategy"}, "variants": 9, "generations": 0, "search_points": 4,
                               "model": "x", "history_from": "1999", "auto_resume": "yes", "foo": 1})
        self.assertEqual(set(e), {"seed.strategy_id", "variants", "generations", "search_points", "model",
                                  "history_from", "auto_resume", "foo"})
        self.assertIn("seed", loop.check_config({}))
        self.assertIn("seed.kind", loop.check_config({"seed": {"kind": "holdout"}}))
        self.assertIn("seed.text", loop.check_config({"seed": {"kind": "idea", "text": "x" * 4001}}))
        self.assertIn("variants", loop.check_config({"seed": {"kind": "idea", "text": "x"}, "variants": True}))

    def test_planned_counts(self):
        self.assertEqual(loop.planned_backtests(6, 4, 1), 35)
        self.assertEqual(loop.planned_backtests(6, 4, 1, seed_reused=True), 34)
        self.assertEqual(loop.planned_backtests(8, 5, 3), 109)
        self.assertLessEqual(loop.planned_backtests(8, 5, 3), loop.MAX_BACKTESTS)
        self.assertEqual(loop.planned_ai_calls(6, 4, False), 46)


if __name__ == "__main__":
    unittest.main()
