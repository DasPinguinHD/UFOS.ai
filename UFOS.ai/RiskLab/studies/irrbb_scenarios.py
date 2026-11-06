"""IRRBB study: Delta EVE / Delta NII of a synthetic bank under the six standardised shock scenarios.

Question: how exposed is a small synthetic retail bank to interest rate risk in the banking
book under the BCBS / EBA standardised scenarios, does it breach the supervisory outlier tests,
and how much do the answers depend on the non-maturity-deposit (NMD) assumptions - the classic
model-risk point of IRRBB? Synthetic data only.

Design (details in :mod:`risklab.rates`):

* base curve: a Nelson-Siegel zero curve with known parameters (:data:`CURVE`; synthetic, a
  plausible upward-sloping EUR curve);
* the reference bank (:func:`risklab.rates.instruments.synthetic_balance_sheet`, EUR millions,
  Tier 1 = 9 % of assets);
* Delta EVE and Delta NII (12 months, constant balance sheet) for the six scenarios, with the EUR
  shock sizes of BCBS 2016 (200 / 250 / 100 bp; the default) and of the 2024 recalibration
  (225 / 350 / 200 bp), and the EBA post-shock floor;
* the supervisory outlier tests: worst EVE decline > 15 % of Tier 1, worst NII decline over
  parallel up / down > 5 % of Tier 1;
* key-rate DV01 profile of assets, liabilities and the net position;
* NMD sensitivity: core share 50-90 % x average maturity 1-5 years of the current-account NMD
  (inside the caps) -> worst Delta EVE / Tier 1;
* duration check: parallel shifts of -400 ... +400 bp (no floor): full revaluation of the net
  EVE against the first-order (DV01) and second-order (with convexity) approximations;
* a population of M synthetic banks (notionals x U(0.7, 1.3), fixed rates +- 50 bp): the
  distribution of the worst Delta EVE / Tier 1 and the outlier share, with Monte-Carlo se.

The reference-bank results are deterministic; only the population part depends on M and the
seed. Run from ``UFOS.ai/RiskLab/``::

    python studies/irrbb_scenarios.py                 # default: M = 200 banks
    python studies/irrbb_scenarios.py --mode full     # M = 2000

Measured run times: default (M = 200) 4 s and full (M = 2000) 32 s in the build container.
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: runs without installing the package
    sys.path.insert(0, _SRC)

import numpy as np

import risklab
from risklab.conventions import make_rng
from risklab.rates import curve as C
from risklab.rates import instruments as I
from risklab.rates import irrbb as R
from risklab.report.export import (code_fingerprint, default_out_dir, save_full_results, table_to_csv,
                                   table_to_json, text_to_file)

STUDY = "irrbb_scenarios"
MODES = {"smoke": 5, "default": 200, "full": 2000}
CURVE = C.NelsonSiegel(0.030, -0.015, 0.010, 2.0)
COLUMNS = ["table", "preset", "scenario", "delta_eve", "delta_eve_tier1", "delta_nii", "delta_nii_tier1",
           "key_tenor", "dv01_assets", "dv01_liabilities", "dv01_net", "core_share", "avg_maturity",
           "worst_eve_tier1", "worst_scenario", "eve_outlier", "shift_bp", "full", "first_order", "second_order",
           "m", "mean", "mean_se", "q05", "q50", "q95", "outlier_share", "outlier_share_se", "nii_outlier_share",
           "nii_outlier_share_se"]


@dataclass
class IRRBBConfig:
    m: int = MODES["default"]
    seed: int = 20261110
    presets: Tuple[str, ...] = ("EUR_BCBS2016", "EUR_BCBS2024")
    core_shares: Tuple[float, ...] = (0.5, 0.6, 0.7, 0.8, 0.9)
    avg_maturities: Tuple[float, ...] = (1.0, 2.0, 3.0, 4.0, 5.0)
    shifts_bp: Tuple[float, ...] = (-400, -300, -200, -100, -50, 50, 100, 200, 300, 400)
    eve_threshold: float = 0.15
    nii_threshold: float = 0.05


def scenario_rows(bs, preset: str) -> Tuple[List[dict], dict, R.IRRBBResult]:
    res = R.run_scenarios(bs, CURVE, preset)
    rows = [{"table": "scenario", "preset": preset, "scenario": r.scenario, "delta_eve": r.delta_eve,
             "delta_eve_tier1": r.delta_eve / bs.tier1, "delta_nii": r.delta_nii,
             "delta_nii_tier1": r.delta_nii / bs.tier1} for r in res.rows]
    return rows, R.outlier_tests(res), res


def key_rate_rows(bs) -> List[dict]:
    def kr(items):
        tot = np.zeros(len(R.KEY_RATES))
        for x in items:
            tot += R.key_rate_durations(x.cashflows(), CURVE, per_unit_pv=False)
        return tot
    a, l = kr(bs.assets), kr(bs.liabilities)
    return [{"table": "key_rate", "key_tenor": k, "dv01_assets": float(a[i]), "dv01_liabilities": float(l[i]),
             "dv01_net": float(a[i] + l[i])} for i, k in enumerate(R.KEY_RATES)]


def nmd_rows(cfg: IRRBBConfig) -> List[dict]:
    rows = []
    for cs in cfg.core_shares:
        for m in cfg.avg_maturities:
            bs = I.synthetic_balance_sheet(nmd_core_share=cs, nmd_avg_maturity=m)
            o = R.outlier_tests(R.run_scenarios(bs, CURVE, cfg.presets[0]), cfg.eve_threshold, cfg.nii_threshold)
            rows.append({"table": "nmd", "preset": cfg.presets[0], "core_share": cs, "avg_maturity": m,
                         "worst_eve_tier1": -o["eve_ratio"], "worst_scenario": o["eve_worst_scenario"],
                         "eve_outlier": o["eve_outlier"]})
    return rows


def duration_rows(bs, cfg: IRRBBConfig) -> List[dict]:
    """Net EVE under parallel shifts (no floor) vs its first- and second-order expansions."""
    base = R.as_zero_curve(CURVE)
    e0 = R.eve(bs, base)
    h = 1.0 * R.BP
    up = R.eve(bs, C.shifted(base, lambda t: np.full_like(t, h)))
    dn = R.eve(bs, C.shifted(base, lambda t: np.full_like(t, -h)))
    d1 = (up - dn) / (2 * h)                 # dEVE/dz
    d2 = (up + dn - 2 * e0) / (h * h)        # d2EVE/dz2
    rows = []
    for s in cfg.shifts_bp:
        d = s * R.BP
        full = R.eve(bs, C.shifted(base, lambda t, d=d: np.full_like(t, d))) - e0
        rows.append({"table": "duration", "shift_bp": float(s), "full": full, "first_order": d1 * d,
                     "second_order": d1 * d + 0.5 * d2 * d * d})
    return rows


def population_rows(cfg: IRRBBConfig, progress=None) -> List[dict]:
    rng = make_rng(cfg.seed)
    seeds = rng.integers(0, 2 ** 31 - 1, size=cfg.m)
    rows = []
    for preset in cfg.presets:
        worst, out, nout = [], [], []
        for s in seeds:
            bs = I.synthetic_balance_sheet(int(s))
            o = R.outlier_tests(R.run_scenarios(bs, CURVE, preset), cfg.eve_threshold, cfg.nii_threshold)
            worst.append(-o["eve_ratio"])
            out.append(o["eve_outlier"])
            nout.append(o["nii_outlier"])
        w = np.asarray(worst)
        p, q = float(np.mean(out)), float(np.mean(nout))
        n = len(w)
        rows.append({"table": "population", "preset": preset, "m": n, "mean": float(w.mean()),
                     "mean_se": float(w.std(ddof=1) / math.sqrt(n)) if n > 1 else None,
                     "q05": float(np.quantile(w, 0.05)), "q50": float(np.quantile(w, 0.5)),
                     "q95": float(np.quantile(w, 0.95)), "outlier_share": p,
                     "outlier_share_se": math.sqrt(p * (1 - p) / n), "nii_outlier_share": q,
                     "nii_outlier_share_se": math.sqrt(q * (1 - q) / n)})
        if progress:
            progress(f"population {preset}: done")
    return rows


def run_study(cfg: IRRBBConfig, progress: Optional[Callable[[str], None]] = None) -> Tuple[List[dict], dict]:
    if cfg.m < 2:
        raise ValueError("m must be >= 2")
    bs = I.synthetic_balance_sheet()
    rows, outl, assumptions = [], {}, {}
    for preset in cfg.presets:
        r, o, res = scenario_rows(bs, preset)
        rows += r
        o = R.outlier_tests(res, cfg.eve_threshold, cfg.nii_threshold)
        outl[preset] = o
        rows.append({"table": "outlier", "preset": preset, "scenario": o["eve_worst_scenario"],
                     "worst_eve_tier1": -o["eve_ratio"], "eve_outlier": o["eve_outlier"],
                     "delta_nii_tier1": -o["nii_ratio"]})
        items = dict(res.assumptions.items)
        items[f"shock sizes {preset}"] = items.pop("shock sizes")
        assumptions.update(items)
    rows += key_rate_rows(bs)
    rows += nmd_rows(cfg)
    rows += duration_rows(bs, cfg)
    rows += population_rows(cfg, progress)
    extra = {"outlier_tests": {k: {kk: vv for kk, vv in v.items()} for k, v in outl.items()},
             "assumptions": assumptions, "balance_sheet": {**bs.totals(), **bs.description,
                                                           "instruments": [x.name for x in bs.instruments]},
             "curve": CURVE.params(), "shock_presets": {p: R.SHOCK_PRESETS[p] for p in cfg.presets},
             "shock_sources": R.SHOCK_SOURCES, "key_rates": list(R.KEY_RATES)}
    return rows, extra


def _p(x: Optional[float], d: int = 1) -> str:
    return "n/a" if x is None else f"{100 * x:.{d}f}"


def _rate(p: float, se: float, n: int) -> str:
    """A share in % with its se; exactly 0 or 1 shows the 95 % rule-of-three bound (3 / n)."""
    if p == 0.0:
        return f"0 (≤ {300.0 / n:.2g}) %"
    if p == 1.0:
        return f"100 (≥ {100.0 - 300.0 / n:.4g}) %"
    return f"{100 * p:.1f} ± {100 * se:.1f} %"


def summary_markdown(rows: List[dict], extra: dict, cfg: IRRBBConfig) -> str:
    bs = extra["balance_sheet"]
    lines = ["# IRRBB scenarios of a synthetic bank", "",
             f"Reference bank: assets {bs['assets']:.0f}, liabilities {bs['liabilities']:.0f}, Tier 1 {bs['tier1']:.0f} "
             f"(EUR m, synthetic). Base curve: Nelson-Siegel {extra['curve']}. Population: M = {cfg.m} banks, seed "
             f"{cfg.seed}. risklab {risklab.__version__}, numpy {np.__version__}. No market data.", "",
             "## Delta EVE and Delta NII (% of Tier 1)", "",
             "| preset | scenario | Delta EVE | Delta NII |", "|---|---|---|---|"]
    for r in rows:
        if r["table"] == "scenario":
            lines.append(f"| {r['preset']} | {r['scenario']} | {_p(r['delta_eve_tier1'])} | {_p(r['delta_nii_tier1'])} |")
    lines += ["", "## Supervisory outlier tests", "", "| preset | worst EVE scenario | EVE decline / Tier 1 | outlier "
              "(> 15 %) | worst NII (parallel) | NII decline / Tier 1 | outlier (> 5 %) |", "|---|---|---|---|---|---|---|"]
    for p, o in extra["outlier_tests"].items():
        lines.append(f"| {p} | {o['eve_worst_scenario']} | {_p(o['eve_ratio'])} | {o['eve_outlier']} | "
                     f"{o['nii_worst_scenario']} | {_p(o['nii_ratio'])} | {o['nii_outlier']} |")
    lines += ["", "## NMD sensitivity (worst Delta EVE / Tier 1, %, EUR_BCBS2016)", "",
              "| core share \\ avg maturity | " + " | ".join(f"{m:g} y" for m in cfg.avg_maturities) + " |",
              "|---|" + "---|" * len(cfg.avg_maturities)]
    nm = {(r["core_share"], r["avg_maturity"]): r for r in rows if r["table"] == "nmd"}
    for cs in cfg.core_shares:
        lines.append(f"| {cs:.0%} | " + " | ".join(_p(nm[(cs, m)]["worst_eve_tier1"]) for m in cfg.avg_maturities) + " |")
    lines += ["", "## Duration check (net EVE, parallel shifts, no floor, EUR m)", "",
              "| shift (bp) | full revaluation | first order | second order |", "|---|---|---|---|"]
    for r in rows:
        if r["table"] == "duration":
            lines.append(f"| {r['shift_bp']:+.0f} | {r['full']:.2f} | {r['first_order']:.2f} | {r['second_order']:.2f} |")
    lines += ["", f"## Population of {cfg.m} synthetic banks (worst Delta EVE / Tier 1, %)", "",
              "| preset | mean ± se | 5 % | median | 95 % | EVE outliers | NII outliers |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        if r["table"] == "population":
            lines.append(f"| {r['preset']} | {_p(r['mean'])} ± {_p(r['mean_se'], 2)} | {_p(r['q05'])} | {_p(r['q50'])} | "
                         f"{_p(r['q95'])} | {_rate(r['outlier_share'], r['outlier_share_se'], r['m'])} | "
                         f"{_rate(r['nii_outlier_share'], r['nii_outlier_share_se'], r['m'])} |")
    lines += ["", "Assumptions (sources or ASSUMPTION): see `meta.assumptions` in the results file."]
    return "\n".join(lines) + "\n"


def write_outputs(rows: List[dict], extra: dict, cfg: IRRBBConfig, out_dir: Path) -> List[Path]:
    meta = {"study": STUDY, "code_sha256": code_fingerprint(__file__),
            "config": {k: (list(v) if isinstance(v, tuple) else v) for k, v in asdict(cfg).items()}, **extra}
    out_dir = Path(out_dir)
    return [table_to_csv(rows, COLUMNS, out_dir / "results.csv", cfg.seed, meta),
            table_to_json(rows, out_dir / "results.json", cfg.seed, meta),
            text_to_file(summary_markdown(rows, extra, cfg), out_dir / "summary.md")]


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mode", choices=sorted(MODES), default="default",
                    help="smoke (M=5 banks), default (M=200) or full (M=2000)")
    ap.add_argument("--m", type=int, default=None)
    ap.add_argument("--seed", type=int, default=IRRBBConfig.seed)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--save-results", action="store_true",
                    help="with --mode full: also save results.json / summary.md as studies/results/"
                         f"{STUDY}_full_*.json / .md (the inputs of the report)")
    args = ap.parse_args(argv)
    if args.save_results and (args.mode != "full" or args.m is not None or args.seed != ap.get_default("seed")):
        ap.error("--save-results needs --mode full with the default M and seed")
    cfg = IRRBBConfig(m=args.m if args.m is not None else MODES[args.mode], seed=args.seed)
    out_dir = Path(args.out_dir) if args.out_dir else default_out_dir() / STUDY
    log = None if args.quiet else (lambda msg: print(msg, flush=True))
    start = time.perf_counter()
    rows, extra = run_study(cfg, log)
    paths = write_outputs(rows, extra, cfg, out_dir)
    if args.save_results:
        paths += save_full_results(out_dir, STUDY)
    if log:
        log(f"{len(rows)} rows in {time.perf_counter() - start:.1f} s -> " + ", ".join(str(p) for p in paths))
    return 0


if __name__ == "__main__":
    sys.exit(main())
