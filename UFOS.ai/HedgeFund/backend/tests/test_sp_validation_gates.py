"""Stage 4 (V2): parameter grid (axes, ≤ 64 evenly subsampled with the base
kept, neighbours ±20 %), folds (K = 5 equal segments, per-fold IC strictly
inside the fold), every gate rule incl. the "cannot be computed -> fail"
cases and the G3 not-applicable case, presets / overrides and the stored,
audited gate settings, report merge of exceptions. Temp hedgefund.db,
offline.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_validation_gates -v
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strategy_platform.storage import db  # noqa: E402
from strategy_platform.validation import folds, gates, grid, report  # noqa: E402
import test_sp_validation_support as sup  # noqa: E402

TH = gates.effective("standard")


def spec_with(params, signal_param):
    s = sup.spec_planted()
    s["parameters"] = params
    s["signal"] = {"op": "mul", "args": [{"param": signal_param}, {"field": "close"}]}
    return s


class GridTest(unittest.TestCase):
    def test_axis_values_include_value_and_integers(self):
        self.assertEqual(grid.axis_values({"value": 0.55, "min": 0.0, "max": 1.0, "step": 0.25}),
                         [0.0, 0.25, 0.5, 0.55, 0.75, 1.0])
        self.assertEqual(grid.axis_values({"value": 20, "min": 10, "max": 30, "step": 7, "integer": True}),
                         [10, 17, 20, 24])
        with self.assertRaises(ValueError):
            grid.axis_values({"value": 1, "min": 0, "max": 1e9, "step": 1})

    def test_small_grid_and_neighbours(self):
        s = sup.spec_planted(0.5)
        g = grid.build(s)
        self.assertEqual(g["total"], 11)
        self.assertFalse(g["subsampled"])
        self.assertEqual(g["configs"][g["base_index"]], {"a": 0.5})
        nb = grid.neighbours(g, s)
        self.assertEqual(sorted(g["configs"][i]["a"] for i in nb), [0.4, 0.6])
        # a base value of 0 uses 20 % of the range
        g0 = grid.build(sup.spec_planted(0.0))
        self.assertEqual([g0["configs"][i]["a"] for i in grid.neighbours(g0, sup.spec_planted(0.0))], [0.1, 0.2])

    def test_subsampling_keeps_base(self):
        s = spec_with({"a": {"value": 0.37, "min": 0, "max": 1, "step": 0.01},
                       "b": {"value": 3, "min": 1, "max": 9, "step": 1, "integer": True}}, "a")
        s["signal"] = {"op": "add", "args": [{"op": "mul", "args": [{"param": "a"}, {"field": "close"}]},
                                             {"param": "b"}]}
        g = grid.build(s)
        self.assertEqual(g["total"], 101 * 9)
        self.assertTrue(g["subsampled"])
        self.assertEqual(len(g["configs"]), 64)
        self.assertEqual(g["configs"][g["base_index"]], {"a": 0.37, "b": 3})
        self.assertEqual(len({json.dumps(c, sort_keys=True) for c in g["configs"]}), 64)
        self.assertEqual(g["configs"][0], {"a": 0.0, "b": 1})
        self.assertEqual(g["configs"][-1], {"a": 1.0, "b": 9})

    def test_fixed_parameters(self):
        s = spec_with({"a": {"value": 0.5, "min": 0, "max": 1},                      # no step
                       "u": {"value": 2, "min": 1, "max": 5, "step": 1}}, "a")        # unused
        g = grid.build(s)
        self.assertEqual(g["axes"], {})
        self.assertIn("no step", g["fixed"]["a"]["reason"])
        self.assertEqual(g["fixed"]["u"]["reason"], "unused parameter")
        self.assertEqual(g["configs"], [{}])
        self.assertEqual(grid.spec_with(sup.spec_planted(), {"a": 0.9})["parameters"]["a"]["value"], 0.9)


class FoldTest(unittest.TestCase):
    def test_bounds(self):
        self.assertEqual(folds.fold_bounds(10), [(0, 1), (2, 3), (4, 5), (6, 7), (8, 9)])
        b = folds.fold_bounds(103)
        self.assertEqual(b[0][0], 0)
        self.assertEqual(b[-1][1], 102)
        self.assertLessEqual(max(y - x for x, y in b) - min(y - x for x, y in b), 1)
        with self.assertRaises(ValueError):
            folds.fold_bounds(4)

    def test_fold_metrics_ic_inside_fold(self):
        m = sup.Market(2, first=sup.date(2023, 1, 3), last=sup.date(2024, 6, 28))
        res = m.engine("planted")(sup.spec_planted(1.0))
        ic = folds.daily_ic(res["analysis"], (5,))[5]
        rows = folds.fold_metrics(res, ic=ic, horizon=5, analysis=res["analysis"])
        self.assertEqual(len(rows), 5)
        self.assertEqual(sum(r["days"] for r in rows), len(res["dates"]))
        for r in rows:
            self.assertGreater(r["sharpe"], 0)
            self.assertGreater(r["mean_ic"], 0)
            self.assertEqual(r["ic_days"], r["days"] - 5)          # the last 5 days' windows leave the fold
        self.assertEqual(folds.signal_mode(res["analysis"]), "cross_sectional")
        self.assertEqual(folds.signal_mode({"symbols": ["SPY"]}), "time_series")
        self.assertIsNone(folds.signal_mode(None))


def base_inputs(**over):
    inp = {"mode": "cross_sectional",
           "ic_tests": {1: {"value": 1.0, "mean": 0.01}, 5: {"value": 3.0, "mean": 0.02}, 10: {"value": None,
                                                                                           "reason": "x"}},
           "best_horizon": 5, "folds": [{"mean_ic": 0.02, "sharpe": 1.0}] * 5,
           "alpha": {"value": 3.0, "alpha_daily": 0.0004, "alpha_annual": 0.1}, "benchmark": "SPY",
           "half_life": {"value": 20.0, "reason": None, "fit": {"lambda": 0.03}},
           "holding": {"value": 10.0, "trades": 200}, "dsr": {"value": 0.99, "n_trials": 3, "sr0_annual": 0.5},
           "pbo": {"value": 0.1, "configs": 11}, "grid": {"base_sharpe": 2.0, "neighbours": [0, 2],
                                                         "sharpes": [1.5, 2.0, 1.2], "configs": [{}, {}, {}]},
           "costs": {"multiplier": 2.0, "sharpe": 1.0}, "episodes": [
               {"key": "e", "label": "E", "status": "done", "strategy_dd": 0.1, "benchmark_dd": 0.2},
               {"key": "l", "label": "L", "status": "not_available", "reason": "locked"}],
           "trades_closed": 150, "capacity": {"capacity_usd": 5e6, "capital": 1e5}}
    inp.update(over)
    return inp


def status(inp, th=TH):
    return {g["gate"]: g["status"] for g in gates.evaluate(inp, th)}


class GateTest(unittest.TestCase):
    def test_all_pass_and_shape(self):
        rows = gates.evaluate(base_inputs(), TH)
        self.assertEqual([g["gate"] for g in rows], list(gates.TRAINING_GATES))
        for g in rows:
            self.assertEqual(set(g), {"gate", "name", "status", "value", "threshold", "explanation", "details"})
            self.assertEqual(g["status"], "pass", g)
        json.dumps(rows, allow_nan=False)
        self.assertTrue(report.passed_of(rows))

    def test_g1(self):
        self.assertEqual(status(base_inputs(best_horizon=1))["G1"], "fail")              # t 1.0 < 2
        self.assertEqual(status(base_inputs(folds=[{"mean_ic": 0.02}] * 3 + [{"mean_ic": -0.01}] * 2))["G1"], "fail")
        self.assertEqual(status(base_inputs(folds=[{"mean_ic": 0.02}] * 4 + [{"mean_ic": None}]))["G1"], "pass")
        g = gates.g1(base_inputs(best_horizon=None), TH)
        self.assertEqual(g["status"], "fail")
        self.assertIn("Cannot be computed", g["explanation"])
        ts = base_inputs(mode="time_series", folds=[{"sharpe": 0.5}] * 4 + [{"sharpe": -0.1}])
        self.assertEqual(gates.g1(ts, TH)["value"], 4)
        self.assertEqual(status(ts)["G1"], "pass")
        self.assertEqual(status(ts, gates.effective("strict"))["G1"], "fail")
        self.assertEqual(status(base_inputs(mode=None))["G1"], "fail")

    def test_g2_g3(self):
        self.assertEqual(status(base_inputs(alpha={"value": 1.9, "alpha_daily": 0.001, "alpha_annual": 0.25}))["G2"],
                         "fail")
        self.assertEqual(status(base_inputs(alpha={"value": 2.5, "alpha_daily": -0.001, "alpha_annual": -0.2}))["G2"],
                         "fail")
        self.assertEqual(status(base_inputs(alpha={"value": None, "reason": "too few"}))["G2"], "fail")
        self.assertEqual(status(base_inputs(half_life={"value": 5.0, "fit": {"lambda": 0.1}}))["G3"], "fail")
        flat = {"value": None, "reason": "does not decay", "fit": {"lambda": -0.01}}
        strong_1d = dict(base_inputs()["ic_tests"])
        strong_1d[1] = {"value": 3.5, "mean": 0.03}
        nd = gates.g3(base_inputs(half_life=flat, ic_tests=strong_1d), TH)
        self.assertEqual(nd["status"], "pass")
        self.assertTrue(nd["details"]["does_not_decay"])
        # review 2026-10-09: a flat NOISE IC (not significantly positive at 1 day) must not pass as "no decay"
        for noise in ({1: {"value": 0.4, "mean": 0.002}}, {1: {"value": 2.5, "mean": -0.01}},
                      {1: {"value": None, "mean": None, "reason": "too few"}}, {}):
            g = gates.g3(base_inputs(half_life=flat, ic_tests=noise), TH)
            self.assertEqual(g["status"], "fail", noise)
            self.assertIn("not significantly positive", g["explanation"])
        self.assertEqual(status(base_inputs(half_life={"value": None, "reason": "fewer than 3 lags", "fit": {}}))["G3"],
                         "fail")
        self.assertEqual(status(base_inputs(holding={"value": None, "reason": "no closed trade"}))["G3"], "fail")
        na = gates.g3(base_inputs(mode="time_series"), TH)
        self.assertEqual(na["status"], "pass")
        self.assertIn("Not applicable — single-symbol strategy", na["explanation"])

    def test_g4_g5(self):
        self.assertEqual(status(base_inputs(dsr={"value": 0.94, "n_trials": 3, "sr0_annual": 1.0}))["G4"], "fail")
        self.assertEqual(status(base_inputs(dsr={"value": 0.94, "n_trials": 3, "sr0_annual": 1.0}),
                                gates.effective("lenient"))["G4"], "pass")
        self.assertEqual(status(base_inputs(dsr={"value": None, "reason": "x"}))["G4"], "fail")
        self.assertEqual(status(base_inputs(pbo={"value": 0.31, "configs": 11}))["G5"], "fail")
        few = gates.g5(base_inputs(pbo={"value": None, "configs": 3, "reason": "needs"}), TH)
        self.assertEqual(few["status"], "fail")
        self.assertEqual(few["explanation"], "needs ≥ 4 parameter configurations (add min/max/step to parameters)")
        self.assertEqual(status(base_inputs(pbo={"value": None, "configs": 8, "reason": "a run failed"}))["G5"],
                         "fail")

    def test_g6(self):
        self.assertEqual(status(base_inputs(grid={"base_sharpe": 2.0, "neighbours": [0], "sharpes": [0.9],
                                                  "configs": [{}]}))["G6"], "fail")        # 45 % < 50 %
        self.assertEqual(status(base_inputs(grid={"base_sharpe": 2.0, "neighbours": [], "sharpes": [],
                                                  "configs": []}))["G6"], "fail")
        self.assertEqual(status(base_inputs(grid={"base_sharpe": -1.0, "neighbours": [0], "sharpes": [1.0],
                                                  "configs": [{}]}))["G6"], "fail")
        self.assertEqual(status(base_inputs(grid={"base_sharpe": 2.0, "neighbours": [0], "sharpes": [None],
                                                  "configs": [{}], "reasons": {0: "run failed"}}))["G6"], "fail")
        self.assertEqual(status(base_inputs(costs={"multiplier": 2.0, "sharpe": -0.1}))["G6"], "fail")
        self.assertEqual(status(base_inputs(costs={"multiplier": 2.0, "sharpe": None, "reason": "x"}))["G6"], "fail")
        self.assertEqual(status(base_inputs(episodes=[{"label": "E", "status": "done", "strategy_dd": 0.31,
                                                       "benchmark_dd": 0.2}]))["G6"], "fail")
        g = gates.g6(base_inputs(episodes=[{"label": "L", "status": "not_available"}]), TH)
        self.assertEqual(g["status"], "pass")
        self.assertIn("vacuously", g["details"]["notes"][0])

    def test_g7_g8(self):
        self.assertEqual(status(base_inputs(trades_closed=99))["G7"], "fail")
        self.assertEqual(status(base_inputs(trades_closed=99), gates.effective("lenient"))["G7"], "pass")
        self.assertEqual(status(base_inputs(capacity={"capacity_usd": 9.9e5, "capital": 1e5}))["G8"], "fail")
        # user decision 2026-10-09: crypto-only -> not applicable (pass); mixed/equity keep the estimate rule
        na = gates.g8(base_inputs(universe_kind="crypto", capacity={"capacity_usd": None, "capital": 1e5,
                                                                    "reason": "no filled equity orders"}), TH)
        self.assertEqual(na["status"], "pass")
        self.assertEqual(na["explanation"], "Not applicable — No capacity measurement for crypto (user decision "
                                            "2026-10-03: no volume limit for crypto).")
        self.assertEqual(status(base_inputs(universe_kind="mixed", capacity={"capacity_usd": None, "capital": 1e5,
                                                                             "reason": "x"}))["G8"], "fail")
        self.assertEqual(status(base_inputs(universe_kind="mixed"))["G8"], "pass")
        g8 = gates.g8(base_inputs(capacity={"capacity_usd": None, "at_most": 1e5, "capital": 1e5,
                                            "reason": "the 1 % limit already cut"}), TH)
        self.assertEqual(g8["status"], "fail")
        self.assertIn("already cut", g8["explanation"])

    def test_holdout_gate(self):
        h = gates.holdout_gate(sharpe=1.2, training_sharpe=2.0, mode="cross_sectional", mean_ic=0.01, horizon=5)
        self.assertEqual(h["status"], "pass")
        self.assertFalse(h["details"]["exception_allowed"])
        self.assertEqual(gates.holdout_gate(sharpe=0.9, training_sharpe=2.0, mode="cross_sectional", mean_ic=0.01,
                                            horizon=5)["status"], "fail")
        self.assertEqual(gates.holdout_gate(sharpe=1.2, training_sharpe=2.0, mode="cross_sectional", mean_ic=-0.01,
                                            horizon=5)["status"], "fail")
        self.assertEqual(gates.holdout_gate(sharpe=1.2, training_sharpe=2.0, mode="cross_sectional", mean_ic=None,
                                            horizon=5, ic_reason="x")["status"], "fail")
        self.assertEqual(gates.holdout_gate(sharpe=1.2, training_sharpe=2.0, mode="time_series", mean_ic=None,
                                            horizon=None)["status"], "pass")
        self.assertEqual(gates.holdout_gate(sharpe=-0.1, training_sharpe=-1.0, mode="time_series", mean_ic=None,
                                            horizon=None)["status"], "fail")
        self.assertEqual(gates.holdout_gate(sharpe=None, training_sharpe=2.0, mode="time_series", mean_ic=None,
                                            horizon=None, sharpe_reason="zero variance")["status"], "fail")

    def test_presets(self):
        self.assertEqual(gates.PRESETS["standard"]["g1_ic_t"], 2.0)
        self.assertEqual((gates.PRESETS["lenient"]["g5_pbo_max"], gates.PRESETS["strict"]["g5_pbo_max"]), (0.5, 0.2))
        self.assertEqual(gates.PRESETS["strict"]["g6_cost_multiplier"], 3.0)
        self.assertEqual(gates.PRESETS["strict"]["g7_min_trades"], 200)
        self.assertEqual(gates.effective("standard", {"g7_min_trades": 120.0})["g7_min_trades"], 120)
        self.assertIsInstance(gates.effective("standard", {"g7_min_trades": 120.0})["g7_min_trades"], int)


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sp4-gates-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        db.close()
        db.init(os.path.join(self.tmp, "hedgefund.db"))
        self.addCleanup(db.close)

    def test_default_put_and_audit(self):
        s = gates.get_settings()
        self.assertEqual((s["preset"], s["overrides"], s["updated_at"]), ("standard", {}, None))
        self.assertEqual(s["effective"], gates.PRESETS["standard"])
        out = gates.put_settings({"preset": "strict", "overrides": {"g7_min_trades": 150, "g4_dsr_min": 0.97}})
        self.assertEqual(out["preset"], "strict")
        self.assertEqual(out["effective"]["g7_min_trades"], 150)
        self.assertEqual(out["effective"]["g5_pbo_max"], 0.2)
        self.assertEqual(gates.get_settings()["overrides"], {"g4_dsr_min": 0.97, "g7_min_trades": 150})
        audit = db.list_audit(action="gate_settings_changed")
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["actor"], "user")
        self.assertIsNone(audit[0]["detail"]["old"])
        self.assertEqual(audit[0]["detail"]["new"]["preset"], "strict")

    def test_validation(self):
        for body, field in (([], "_"), ({}, "preset"), ({"preset": "x"}, "preset"), ({"preset": "standard", "z": 1}, "z"),
                            ({"preset": "standard", "overrides": []}, "overrides"),
                            ({"preset": "standard", "overrides": {"nope": 1}}, "overrides.nope"),
                            ({"preset": "standard", "overrides": {"g4_dsr_min": 1.5}}, "overrides.g4_dsr_min"),
                            ({"preset": "standard", "overrides": {"g7_min_trades": 1.5}}, "overrides.g7_min_trades"),
                            ({"preset": "standard", "overrides": {"g1_ic_t": True}}, "overrides.g1_ic_t"),
                            ({"preset": "standard", "overrides": {"g1_ic_t": "2"}}, "overrides.g1_ic_t")):
            self.assertIn(field, gates.check_settings(body), body)
            with self.assertRaises(ValueError):
                gates.put_settings(body if isinstance(body, dict) else {"preset": None})
        self.assertEqual(db.list_audit(action="gate_settings_changed"), [])

    def test_unreadable_settings_raise(self):
        db.meta_set(gates.GATE_SETTINGS_KEY, "{broken")
        with self.assertRaises(db.StorageUnavailable):
            gates.get_settings()
        db.meta_set(gates.GATE_SETTINGS_KEY, json.dumps({"preset": "weird", "overrides": {}}))
        with self.assertRaises(db.StorageUnavailable):
            gates.get_settings()


class ReportMergeTest(unittest.TestCase):
    def test_merge(self):
        rows = gates.evaluate(base_inputs(trades_closed=10, pbo={"value": 0.5, "configs": 11}), TH)
        rep = report.build(gates=rows, mode="cross_sectional", folds=[], grid={}, pbo={}, dsr={}, stress=None,
                           trial_count=2, thresholds={}, training={}, ic={}, holdout=None)
        self.assertFalse(rep["passed"])
        merged = report.merge(rep, [{"id": 1, "ts": "t", "gate": "G7", "reason": "r" * 30, "actor": "user"}])
        self.assertFalse(merged["passed"])
        self.assertEqual(next(g for g in merged["gates"] if g["gate"] == "G7")["status"], "exception")
        merged = report.merge(rep, [{"id": 1, "gate": "G7", "reason": "r" * 30}, {"id": 2, "gate": "G5",
                                                                                   "reason": "s" * 30},
                                    {"id": 3, "gate": "G1", "reason": "t" * 30}])       # G1 passed: ignored
        self.assertTrue(merged["passed"])
        self.assertFalse(merged["passed_raw"])
        self.assertEqual(next(g for g in merged["gates"] if g["gate"] == "G1")["status"], "pass")
        self.assertEqual(len(merged["exceptions"]), 3)
        self.assertFalse(rep["passed"])                     # the stored report is not modified

    def test_missing_gate_is_not_passed(self):
        rows = gates.evaluate(base_inputs(), TH)
        self.assertTrue(report.passed_of(rows))
        self.assertFalse(report.passed_of([g for g in rows if g["gate"] != "G5"]))
        self.assertFalse(report.passed_of([]))
        self.assertFalse(report.passed_of([g for g in rows if g["gate"] != "G8"] +
                                          [{"gate": "H", "status": "pass"}]))


if __name__ == "__main__":
    unittest.main()
