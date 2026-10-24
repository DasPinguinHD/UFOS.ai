"""SPEC §11.1 (X1): attribution — layer 1 of "Explain". Engine
``symbol_pnl`` recording (reconciles with the equity curve), decomposition /
by-symbol reconciliation within 0.01 % of capital on synthetic data, the
benchmark regimes without look-ahead, determinism, fact table, old cards
(no attribution -> null + reason), the backtest job / API payload and the
validation report's ``gate_facts``. Offline, temp hedgefund.db.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_attribution -v
"""
import asyncio
import json
import math
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402

from strategy_platform.api import routes  # noqa: E402
from strategy_platform.backtest import attribution, engine, jobs, metrics, runcard  # noqa: E402
from strategy_platform.core import calendars  # noqa: E402
from strategy_platform.lifecycle import registry as lc  # noqa: E402
from strategy_platform.storage import db  # noqa: E402
import test_sp_backtest_support as sup  # noqa: E402

DAYS = calendars.trading_days(date(2023, 1, 3), date(2024, 4, 30))
LS = dict(mode="long_short", selection={"type": "threshold", "long_above": 100, "short_below": 50})
CAP = 100000.0
TOL = 1e-4 * CAP            # 0.01 % of the starting capital (SPEC §11.1)


def ls_loader():
    return sup.FakeLoader({"SPY": sup.series(150, days=DAYS), "TLT": sup.series(20, days=DAYS, drift=0.0004),
                           "GLD": sup.series(105, days=DAYS, drift=-0.0008),
                           "QQQ": sup.series(300, days=DAYS, drift=0.0006)},
                          dividends={"SPY": [(DAYS[60], 1.5), (DAYS[150], 1.6)], "TLT": [(DAYS[70], 0.07)],
                                     "GLD": [(DAYS[90], 0.4)]})


def ls_spec():
    return sup.spec(["SPY", "TLT", "GLD", "QQQ"], **LS)


def call(name, body=routes.NO_BODY, query=None, match=None):
    return asyncio.run(getattr(routes.V2Api(), name)(query or {}, body, match or {}))


def fake_result(n_sym=25, n_days=140, seed=3):
    """A hand-built engine-shaped dict with symbol_pnl rows that reconcile by construction."""
    rng = np.random.default_rng(seed)
    dates = [d.isoformat() for d in DAYS[:n_days]]
    syms = [f"S{k:02d}" for k in range(n_sym)]
    rows, equity, bench, longs, shorts = [], [], [], [], []
    e, b = CAP, CAP
    for i, d in enumerate(dates):
        day = 0.0
        lm = sm = 0.0
        for k, s in enumerate(syms):
            mv = (1 if k % 3 else -1) * 3000.0
            pnl = float(rng.normal(2.0 * (k - n_sym / 2), 40.0))
            fees = -0.5 if i % 10 == 0 else 0.0
            mtm = pnl - fees
            rows.append([d, s, pnl, mtm, 0.0, fees, 0.1 if fees else 0.0, 0.0, 0.0, mv])
            day += pnl
            lm += max(mv, 0.0)
            sm += max(-mv, 0.0)
        e += day
        b *= 1.0 + float(rng.normal(0.0004, 0.01))
        equity.append(e)
        bench.append(b)
        longs.append(lm)
        shorts.append(sm)
    return {"engine_version": engine.ENGINE_VERSION, "dates": dates, "equity": equity, "benchmark": bench,
            "long_mv": longs, "short_mv": shorts, "fills": [], "request": {"capital": CAP},
            "range": {"start": dates[0], "end": dates[-1], "calendar": "nyse"},
            "benchmark_info": {"symbol": "SPY"},
            "symbol_pnl": {"fields": list(engine.SYMBOL_PNL_FIELDS), "rows": rows}}


class EngineCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = engine.run(ls_spec(), data=ls_loader())
        cls.metrics = metrics.compute(cls.result, analysis=cls.result.analysis)
        cls.attr = attribution.compute(cls.result, cls.metrics)


class RecordingTest(EngineCase):
    def test_engine_version_and_fields(self):
        self.assertEqual(engine.ENGINE_VERSION, attribution.REQUIRED_ENGINE)
        sp = self.result.symbol_pnl
        self.assertEqual(sp["fields"], list(engine.SYMBOL_PNL_FIELDS))
        self.assertIn("symbol_pnl", self.result.to_dict())
        kinds = {r["kind"] for r in self.result.ledger}
        self.assertTrue({"fill", "fee", "borrow_fee", "dividend", "dividend_short"} <= kinds, kinds)

    def test_symbol_pnl_sums_to_equity_change(self):
        rows = self.result.symbol_pnl["rows"]
        total = math.fsum(r[2] for r in rows)
        last = [e for e in self.result.equity if e is not None][-1]
        self.assertAlmostEqual(total, last - CAP, delta=1e-6)
        for r in rows:          # pnl = mtm + dividends + fees + borrow + funding
            self.assertAlmostEqual(r[2], r[3] + r[4] + r[5] + r[7] + r[8], delta=1e-9)
            self.assertGreaterEqual(r[6], 0.0)
        # sparse + ordered
        keys = [(r[0], r[1]) for r in rows]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(keys), len(set(keys)))


class ReconciliationTest(EngineCase):
    def test_decomposition_reconciles(self):
        dec = self.attr["decomposition"]
        src = dec["sources"]
        last = [e for e in self.result.equity if e is not None][-1]
        self.assertAlmostEqual(dec["total_pnl"], last - CAP, delta=1e-6)
        self.assertLessEqual(abs(src["unattributed"]), TOL)
        explained = (src["price_pnl"] - src["slippage"] - src["fees"] - src["borrow"] - src["funding"]
                     + src["dividends_net"] + src["cash_other"])
        self.assertLessEqual(abs(dec["total_pnl"] - explained), TOL)
        self.assertGreater(src["borrow"], 0.0)
        self.assertNotEqual(src["dividends_net"], 0.0)
        reg = dec["regression"]
        self.assertLessEqual(abs(reg["market_pnl"] + reg["alpha_pnl"] + reg["residual_pnl"]
                                 + reg["outside_regression_pnl"] - dec["total_pnl"]), TOL)
        # the same OLS as the metrics (reused)
        self.assertEqual(reg["beta"], self.metrics["benchmark"]["beta"])
        self.assertEqual(reg["alpha_ann"], self.metrics["benchmark"]["alpha_annual"])
        self.assertLess(abs(reg["residual_cum"]), 1e-9)          # OLS residuals with an intercept sum to 0
        # costs equal the metrics' (fees incl. liquidation fees)
        c = self.metrics["costs"]
        self.assertAlmostEqual(src["slippage"], c["slippage"], delta=1e-6)
        self.assertAlmostEqual(src["fees"], c["fees"] + c["liquidation_fees"], delta=1e-6)
        self.assertAlmostEqual(src["borrow"], c["borrow"], delta=1e-6)

    def test_by_symbol_reconciles(self):
        bs = self.attr["by_symbol"]
        rec = bs["reconciliation"]
        parts = math.fsum(v["pnl"] for v in bs["top"] + bs["bottom"]) + bs["others"]["pnl"]
        self.assertLessEqual(abs(parts + rec["cash_other"] - self.attr["decomposition"]["total_pnl"]), TOL)
        self.assertLessEqual(abs(rec["unattributed"]), TOL)
        for v in bs["top"] + bs["bottom"]:
            self.assertAlmostEqual(v["long_pnl"] + v["short_pnl"] + v["flat_pnl"], v["pnl"], delta=1e-6)
        self.assertTrue(any(v["short_pnl"] != 0 for v in bs["top"] + bs["bottom"]))
        ls = self.attr["long_short"]
        self.assertLessEqual(abs(ls["long_pnl"] + ls["short_pnl"] + ls["flat_pnl"] - rec["symbols_pnl"]), TOL)
        self.assertGreater(ls["avg_short_exposure"], 0.0)

    def test_top_bottom_others_with_many_symbols(self):
        r = fake_result()
        a = attribution.compute(r, None)
        bs = a["by_symbol"]
        self.assertEqual((len(bs["top"]), len(bs["bottom"]), bs["others"]["count"]), (10, 10, 5))
        self.assertEqual([v["pnl"] for v in bs["top"]], sorted([v["pnl"] for v in bs["top"]], reverse=True))
        self.assertEqual([v["pnl"] for v in bs["bottom"]], sorted([v["pnl"] for v in bs["bottom"]]))
        self.assertLess(max(v["pnl"] for v in bs["bottom"]), min(v["pnl"] for v in bs["top"]))
        total = a["decomposition"]["total_pnl"]
        parts = math.fsum(v["pnl"] for v in bs["top"] + bs["bottom"]) + bs["others"]["pnl"]
        self.assertLessEqual(abs(parts - total), TOL)
        self.assertLessEqual(abs(a["decomposition"]["sources"]["unattributed"]), TOL)
        self.assertEqual(a["costs"], {"unavailable": "the run's metrics have no cost block"})

    def test_by_year_and_periods(self):
        years = self.attr["by_year"]["years"]
        self.assertEqual([y["year"] for y in years], [2023, 2024])
        # compounding the yearly returns gives the total return
        g = 1.0
        for y in years:
            g *= 1.0 + y["return"]
            self.assertAlmostEqual(y["excess"], y["return"] - y["benchmark_return"], delta=1e-12)
        self.assertAlmostEqual(g - 1.0, self.attr["decomposition"]["total_pnl"] / CAP, delta=1e-9)
        per = self.attr["periods"]
        idx = {d: i for i, d in enumerate(self.result.dates)}
        for kind in ("best", "worst"):
            starts = [idx[w["start"]] for w in per[kind]]
            self.assertEqual(len(starts), 3)
            for i in starts:
                for j in starts:
                    if i != j:
                        self.assertGreaterEqual(abs(i - j), attribution.PERIOD_WINDOW)
            for w in per[kind]:
                self.assertEqual(idx[w["end"]] - idx[w["start"]], attribution.PERIOD_WINDOW)
        best = [w["strategy_return"] for w in per["best"]]
        self.assertEqual(best, sorted(best, reverse=True))
        self.assertTrue(per["drawdowns"])
        depths = [d["depth"] for d in per["drawdowns"]]
        self.assertEqual(depths, sorted(depths, reverse=True))
        for d in per["drawdowns"]:
            self.assertLessEqual(len(d["contributors"]), 3)
            self.assertTrue(all(c["pnl"] < 0 for c in d["contributors"]))
            self.assertEqual(d["recovered"], d["recovery"] is not None)


class RegimeTest(unittest.TestCase):
    def test_no_look_ahead(self):
        rng = np.random.default_rng(5)
        bench = list(100.0 * np.cumprod(1 + rng.normal(0.0003, 0.012, 400)))
        base = attribution.regime_labels(bench, 252)
        self.assertTrue(all(x == "warm-up" for x in base[:attribution.REGIME_WARMUP + 1]))
        self.assertTrue(set(base[attribution.REGIME_WARMUP + 1:]) <= set(attribution.REGIMES))
        self.assertEqual(len(set(base[attribution.REGIME_WARMUP + 1:])), 4)
        for T in (70, 150, 300):
            changed = bench[:T + 1] + [x * f for x, f in zip(bench[T + 1:], rng.uniform(0.5, 1.5, 400))]
            labels = attribution.regime_labels(changed, 252)
            # the label of day j uses the benchmark up to the close of j-1: days <= T+1 cannot change
            self.assertEqual(labels[:T + 2], base[:T + 2], T)
            self.assertNotEqual(labels, base)

    def test_by_regime_block(self):
        r = fake_result(n_sym=3, n_days=200)
        reg = attribution.compute(r, None)["by_regime"]
        self.assertEqual(reg["warmup_days"], attribution.REGIME_WARMUP)
        self.assertEqual(sum(x["days"] for x in reg["regimes"]), reg["labelled_days"])
        self.assertEqual(reg["labelled_days"] + reg["warmup_days"] + reg["unknown_days"], 199)
        self.assertAlmostEqual(sum(x["share"] for x in reg["regimes"]), 1.0, delta=1e-12)

    def test_short_run_unavailable(self):
        r = fake_result(n_sym=3, n_days=40)
        self.assertIn("unavailable", attribution.compute(r, None)["by_regime"])


class DeterminismAndFactsTest(EngineCase):
    def test_deterministic(self):
        again = engine.run(ls_spec(), data=ls_loader())
        a2 = attribution.compute(again, metrics.compute(again, analysis=again.analysis))
        dump = lambda a: json.dumps(a, sort_keys=True, allow_nan=False)        # noqa: E731
        self.assertEqual(dump(a2), dump(self.attr))
        self.assertEqual(dump(attribution.compute(self.result, self.metrics)), dump(self.attr))

    def test_fact_table(self):
        facts = self.attr["facts"]
        ids = [f["id"] for f in facts]
        self.assertEqual(len(ids), len(set(ids)))
        by = {f["id"]: f for f in facts}
        for fid in ("dec.total_pnl", "dec.alpha_ann", "dec.market_pnl", "dec.beta", "dec.unattributed",
                    "year.2023.excess", "year.2024.return", "regime.up_calm.days", "ls.short.pnl",
                    "period.best1.strategy", "dd.1.depth", "cost.total", "sig.ts.positive_share"):
            self.assertIn(fid, by, fid)
        for f in facts:
            self.assertEqual(set(f), {"id", "label", "value", "unit", "display"})
            self.assertIsInstance(f["display"], str)
            self.assertTrue(f["display"])
            if f["unit"] in ("usd", "fraction") and f["id"] != "run.capital":
                self.assertEqual(f["display"], attribution.display(f["value"], f["unit"]))
        self.assertEqual(by["run.capital"]["display"], "$100,000")
        top = self.attr["by_symbol"]["top"][0]["symbol"]
        self.assertIn(f"sym.{top}.pnl", by)
        self.assertEqual(attribution.display(-1234.5, "usd"), "-$1,234.50")
        self.assertEqual(attribution.display(0.1234, "fraction"), "12.34 %")
        self.assertEqual(attribution.display(-0.00001, "fraction"), "0.00 %")
        self.assertEqual(attribution.display(12.0, "count"), "12")
        self.assertEqual(attribution.display(1.23456, "number", 3), "1.235")
        json.dumps(self.attr, allow_nan=False)

    def test_without_symbol_pnl(self):
        d = self.result.to_dict()
        d["symbol_pnl"] = None
        a = attribution.compute(d, self.metrics)          # current engine, recording off (grid / stress)
        self.assertEqual(a["by_symbol"], {"unavailable": attribution.NOT_RECORDED})
        d["engine_version"] = "sp-backtest/3c.1"
        a = attribution.compute(d, self.metrics)
        self.assertEqual(a["by_symbol"], {"unavailable": attribution.NOT_AVAILABLE_OLD})
        self.assertEqual(a["long_short"], {"unavailable": attribution.NOT_AVAILABLE_OLD})
        self.assertEqual(a["decomposition"]["sources"], {"unavailable": attribution.NOT_AVAILABLE_OLD})
        self.assertIn("regression", a["decomposition"])
        self.assertEqual(a["note"], attribution.NOT_AVAILABLE_OLD)


def _label_numbers(label):
    """Integers in a label that the AI number check would not allow by itself (> 12, not a year)."""
    import re
    return {int(x) for x in re.findall(r"(?<![\w.])(\d+)(?![\w.])", label)
            if int(x) > 12 and not 1990 <= int(x) <= 2100}


class LabelNumbersTest(EngineCase):
    """Review L3: every number named in a fact label (method constants) is itself a fact value."""

    def check(self, facts):
        values = {float(f["value"]) for f in facts if f["unit"] not in ("date", "status", "text")}
        for f in facts:
            for n in _label_numbers(f["label"]):
                self.assertIn(float(n), values, f)

    def test_engine_run_labels(self):
        self.check(self.attr["facts"])
        by = {f["id"]: f for f in self.attr["facts"]}
        for fid, v in (("regime.trend_window", 63), ("regime.vol_window", 21), ("period.window", 63),
                       ("sig.ts.window_weeks", 13), ("run.periods_per_year", 252)):
            self.assertEqual((by[fid]["value"], by[fid]["display"], by[fid]["unit"]), (v, str(v), "count"))

    def test_cross_sectional_and_many_symbol_labels(self):
        r = fake_result()
        m = {"signal_quality": {"mode": "cross_sectional", "symbols": 25, "half_life_days": 7.5,
                                "horizons": [{"horizon": h, "mean_ic": 0.02, "icir": 0.1, "t_stat": 2.0, "days": 100}
                                             for h in (1, 5, 10, 21, 63)]}}
        a = attribution.compute(r, m)
        self.check(a["facts"])
        self.assertIn("sym.top_n", {f["id"] for f in a["facts"]})


class RecordingFlagTest(unittest.TestCase):
    """Review L7: grid / stress runs do not record symbol_pnl; the simulation is the same."""

    def test_flag_off_same_simulation(self):
        on = engine.run(ls_spec(), data=ls_loader())
        off = engine.run(ls_spec(), data=ls_loader(), record_symbol_pnl=False)
        self.assertIsNone(off.symbol_pnl)
        self.assertIsNotNone(on.symbol_pnl)
        self.assertEqual((off.equity, off.fills, off.ledger), (on.equity, on.fills, on.ledger))

    def test_grid_and_stress_runs_carry_no_symbol_pnl(self):
        from strategy_platform.backtest import stress
        from strategy_platform.validation import parallel
        seen = []

        def spy(spec, **kw):
            res = engine.run(spec, **kw)
            seen.append((kw.get("record_symbol_pnl"), res.symbol_pnl))
            return res
        out = parallel.grid_task({"run_engine": spy, "start": DAYS[40].isoformat(), "end": DAYS[-1].isoformat(),
                                  "capital": CAP, "benchmark": "SPY", "data": ls_loader()}, {"spec": ls_spec()})
        self.assertIsNotNone(out["series"])
        base = engine.run(ls_spec(), data=ls_loader())
        sc = stress.synthetic_costs("costs_x2", "Costs x2", ls_spec(), base, 2.0, 2.0, data=ls_loader(),
                                    run_engine=spy)
        self.assertEqual(sc["status"], "done", sc.get("reason"))
        stress.episode("q4", "Q4", DAYS[100], DAYS[160], ls_spec(), capital=CAP, benchmark="SPY",
                       data=ls_loader(), run_engine=spy)
        self.assertEqual(len(seen), 3)
        self.assertEqual(seen, [(False, None)] * 3)


class ApiCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sp-x1-attr-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        db.close()
        db.init(os.path.join(self.tmp, "hedgefund.db"))
        self.addCleanup(db.close)
        s = ls_spec()
        s["name"] = "attribution api"
        self.sid = lc.create_from_text(json.dumps(s), "user")["id"]


class ApiTest(ApiCase):
    def test_old_card_without_attribution(self):
        res = engine.run(ls_spec(), data=ls_loader())
        card = runcard.create(self.sid, None, {})
        status, d = call("backtest_get", match={"id": card["id"]})
        self.assertEqual((d["attribution"], d["attribution_unavailable"]),
                         (None, "The attribution is computed when the run finishes."))
        runcard.mark_running(card["id"])
        runcard.finish(card["id"], res, metrics.compute(res, analysis=res.analysis))      # as before §11
        status, d = call("backtest_get", match={"id": card["id"]})
        self.assertEqual(status, 200)
        self.assertIsNone(d["attribution"])
        self.assertEqual(d["attribution_unavailable"], attribution.NOT_AVAILABLE_OLD)
        self.assertIn("run the backtest again", d["attribution_unavailable"])

    def test_job_stores_attribution(self):
        mgr = jobs.JobManager(data_factory=ls_loader)
        jobs.set_manager(mgr)
        self.addCleanup(jobs.set_manager, None)
        rid = mgr.submit(self.sid, None, {})
        self.assertTrue(mgr.wait(120))
        status, d = call("backtest_get", match={"id": rid})
        self.assertEqual(d["status"], "done", d["error"])
        self.assertIsNone(d["attribution_unavailable"])
        a = d["attribution"]
        self.assertEqual(a["version"], attribution.ATTRIBUTION_VERSION)
        self.assertLessEqual(abs(a["decomposition"]["sources"]["unattributed"]), TOL)
        self.assertTrue(a["facts"])
        # finished card is immutable, attribution included
        with self.assertRaises(Exception):
            with db._connect() as conn:
                conn.execute("UPDATE backtest_runs SET attribution_json = '{}' WHERE id = ?", (rid,))

    def test_running_note_cannot_write_attribution(self):
        card = runcard.create(self.sid, None, {})
        runcard.mark_running(card["id"])
        with self.assertRaises(Exception):
            with db._connect() as conn:
                conn.execute("UPDATE backtest_runs SET attribution_json = '{}' WHERE id = ?", (card["id"],))


class GateFactsTest(unittest.TestCase):
    """The validation report carries gate_facts for EVERY gate; exceptions update their status."""

    def test_gate_facts_cover_every_gate_and_follow_exceptions(self):
        from strategy_platform.validation import report
        gates = [{"gate": g, "name": g + " name", "status": "fail" if g in ("G4", "G6") else "pass",
                  "value": v, "threshold": t, "explanation": "x", "details": det}
                 for g, v, t, det in (
                     ("G1", 2.5, 2.0, {"mode": "cross_sectional", "folds_positive": 4, "best_horizon": 5}),
                     ("G2", 1.9, 2.0, {"alpha_annual": 0.04, "beta": 0.3}),
                     ("G3", 12.0, 5.0, {"half_life_days": 12.0, "avg_holding_days": 5.0}),
                     ("G4", 0.71, 0.95, {}), ("G5", 0.2, 0.5, {}),
                     ("G6", 0.6, 0.7, {"costs": {"multiplier": 2, "sharpe": 0.4},
                                       "episodes": [{"key": "gfc", "label": "GFC", "strategy_dd": 0.2,
                                                     "benchmark_dd": 0.5}]}),
                     ("G7", 120, 100, {"trades_closed": 120, "trades_open": 3}),
                     ("G8", 5e6, 1e6, {"capacity_usd": 5e6, "capital": 1e5}))]
        rep = report.build(gates=gates, mode="cross_sectional",
                           folds=[{"fold": k, "sharpe": 0.5 * k, "mean_ic": 0.01, "total_return": 0.02}
                                  for k in range(1, 6)],
                           grid={"sharpes": [0.1, 0.5, 0.9, 1.2], "configs": [{}, {}, {}, {}], "base_sharpe": 0.9},
                           pbo={"value": 0.2, "configs": 4, "combinations": 252, "overfit_combinations": 50},
                           dsr={"value": 0.71, "sharpe_annual": 1.1, "sr0_annual": 0.8, "n_trials": 3},
                           stress={"scenarios": [{"key": "monte_carlo", "kind": "monte_carlo",
                                                  "label": "Monte-Carlo bootstrap", "status": "done",
                                                  "metrics": {"max_drawdown": {"p5": 0.1, "p50": 0.2, "p95": 0.4},
                                                              "cagr": {"p5": -0.1, "p50": 0.05, "p95": 0.2}}},
                                                 {"key": "costs_x2", "kind": "synthetic", "label": "Costs x2",
                                                  "status": "done",
                                                  "metrics": {"strategy": {"total_return": 0.1, "sharpe": 0.4,
                                                                           "max_drawdown": 0.12}}}]},
                           trial_count=3, thresholds={}, training={}, ic={}, holdout=None)
        gf = rep["gate_facts"]
        self.assertEqual([g["gate"] for g in gf], list(report.TRAINING_GATES))
        ids = {f["id"] for g in gf for f in g["facts"]}
        for g in report.TRAINING_GATES:
            self.assertIn(f"gate.{g}.status", ids)
            self.assertIn(f"gate.{g}.value", ids)
            self.assertIn(f"gate.{g}.threshold", ids)
        for fid in ("val.fold.3.sharpe", "val.grid.sharpe_median", "val.pbo.value", "val.dsr.sr0_annual",
                    "val.stress.gfc.strategy_dd", "val.stress.costs_x2.sharpe", "gate.G8.capacity_usd"):
            self.assertIn(fid, ids)
        allf = [f for g in gf for f in g["facts"]]
        values = {float(f["value"]) for f in allf if f["unit"] not in ("date", "status", "text")}
        for f in allf:                                         # review L3: label numbers are facts too
            for n in _label_numbers(f["label"]):
                self.assertIn(float(n), values, f)
        g6 = next(g for g in gf if g["gate"] == "G6")
        self.assertEqual(next(f for f in g6["facts"] if f["id"] == "gate.G6.value")["display"], "60.00 %")
        merged = report.merge(rep, [{"id": 1, "ts": "t", "gate": "G4", "reason": "r" * 40, "actor": "user"}])
        st = {g["gate"]: g["status"] for g in merged["gate_facts"]}
        self.assertEqual((st["G4"], st["G6"]), ("exception", "fail"))
        fact = next(f for g in merged["gate_facts"] for f in g["facts"] if f["id"] == "gate.G4.status")
        self.assertEqual(fact["display"], "exception")
        # payload merge: base attribution facts + gate facts in one table
        a = attribution.with_gate_facts({"facts": [{"id": "dec.total_pnl", "label": "x", "value": 1.0,
                                                    "unit": "usd", "display": "$1.00"}]}, merged)
        self.assertEqual(len(a["gate_facts"]), 8)
        self.assertIn("gate.G4.status", {f["id"] for f in a["facts"]})
        self.assertIsNone(attribution.with_gate_facts(None, merged))


import test_sp_validation_jobs as tv  # noqa: E402


class ValidationPipelineTest(tv.Case):
    """Real engine: the validation card stores the BASE run's attribution; the detail payload merges the
    report's gate facts (every gate) into it."""

    def test_validation_attribution_and_gate_facts(self):
        from strategy_platform.validation import jobs as vjobs, report
        svc = vjobs.ValidationJobs(data_factory=tv.loader)
        rid = svc.submit_validation(self.sid, None, None)
        self.wait()
        card = runcard.get(rid)
        self.assertEqual(card["status"], "done", card.get("error"))
        a = card["attribution"]
        self.assertEqual(a["range"]["end"], card["range"]["end"])            # the base (training) run
        self.assertLessEqual(abs(a["by_symbol"]["reconciliation"]["unattributed"]), TOL)
        rep = card["metrics"]["validation"]["report"]
        self.assertEqual([g["gate"] for g in rep["gate_facts"]], list(report.TRAINING_GATES))
        status, d = call("validation_get", match={"id": rid})
        self.assertEqual(status, 200)
        self.assertIsNone(d["attribution_unavailable"])
        self.assertEqual([g["gate"] for g in d["attribution"]["gate_facts"]], list(report.TRAINING_GATES))
        ids = {f["id"] for f in d["attribution"]["facts"]}
        self.assertTrue({"dec.total_pnl", "gate.G1.status", "gate.G5.threshold", "val.fold.1.sharpe"} <= ids)
        self.assertEqual(len(ids), len(d["attribution"]["facts"]))


if __name__ == "__main__":
    unittest.main()
