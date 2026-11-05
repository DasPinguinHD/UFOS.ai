"""Overfitting study of the UFOS validation gates: how often does a searched-over noise rule pass?

Question: the UFOS strategy platform (SPEC §9) lets a strategy go live only after the
validation gates G1-G8 and the holdout gate H. When a user searches over K parameter
configurations of a rule and validates the best one, how often does a rule WITHOUT an edge
pass (false-positive rate, FPR), how often does a rule with a real edge pass (power), and
what does the holdout add? Synthetic data only (no market data).

Design (details in :mod:`risklab.validation.sim`):

* rule family: moving-average crossover on the log price, fast 4..19, slow 24..69 step 3
  (256 configurations); the UFOS grid of the selected configuration is subsampled to 64;
* data: daily returns with standardised Student-t (nu = 5) innovations, iid (``iid_t``) or
  GARCH(1,1) (``garch_t``), 20 % volatility a year, with a planted signal: the true
  configuration (fast 8, slow 42) earns a gross Sharpe of 0, 0.25, 0.5, 0.75 or 1.0 a year;
* training sample of 5, 10 or 20 years, then a 24-month holdout evaluated once;
* search: best training net Sharpe among K = 1, 8, 64, 256 tried configurations (nested);
* costs: ``cost_bps`` per unit of turnover; the position changes are the closed trades (G7);
* gates in scope: G1 (time-series form), G2, G4, G5, G6 (grid-neighbour part only), G7 and H,
  with the presets lenient / standard / strict; out of scope: G1's cross-sectional rank-IC
  form, G3, the cost and stress-episode parts of G6, G8 (see :mod:`risklab.validation.gates`).

Per (data, years, signal, K, preset): the pass rate of every gate alone, of all in-scope
training gates together ("in-scope gates") and of those plus H, of the statistical gates
(in-scope without the sample-size gate G7, which depends on the rule's trading frequency rather
than on the search) and of those plus H, the conditional holdout pass rates P(H | in-scope
gates), P(H | statistical gates) and P(H | G4), each with its Monte-Carlo standard error
``sqrt(p (1 - p) / M)`` (``sqrt(p (1 - p) / n)`` for conditional rates on n passing paths).
Under signal 0 a pass is a false positive; under a positive signal the pass rate is the power.

Common random numbers: within a (data, years) cell, all signal levels use the same
innovations and the same search order, so the power curves are smooth in the signal.

Calibration (SPEC §9.6): "at least 95 % of noise seeds fail at least one gate" and
"best-of-64 noise fails G4 or G5" are checked with the standard preset on the noise rows.

Run from ``UFOS.ai/RiskLab/``::

    python studies/gate_overfitting.py                 # default: M = 75 per cell (a demo, ~3 min)
    python studies/gate_overfitting.py --mode full     # M = 2000

Measured run times: the default (M = 75 per cell) took 178 s on the reference VM (2 cores,
Python 3.10; the two data types measured separately, 89 s each) and 64 s in the build
container; the full run (M = 2000) took 31 min (1836 s) in the container, about 80 min on the
VM by extrapolation.
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
from risklab.report.export import (code_fingerprint, default_out_dir, save_full_results, table_to_csv,
                                   table_to_json, text_to_file)
from risklab.validation import gates as G
from risklab.validation import sim
from risklab.validation.stats import expected_max_sharpe

STUDY = "gate_overfitting"
MODES = {"smoke": 4, "default": 75, "full": 2000}
GATES = ("G1", "G2", "G4", "G5", "G6", "G7", "H")
#: the statistical in-scope gates: all in-scope gates except the sample-size gate G7 (which depends
#: on the rule's trading frequency, not on the search)
STAT_GATES = ("G1", "G2", "G4", "G5", "G6")
CHUNK = 250
COLUMNS = (["data", "years", "signal", "k", "preset", "m"]
           + [f"pass_{s}" for g in GATES + ("all", "all_h", "stat", "stat_h") for s in (g, g + "_se")]
           + ["h_given_all", "h_given_all_se", "n_all", "h_given_stat", "h_given_stat_se", "n_stat",
              "h_given_g4", "h_given_g4_se", "n_g4", "fail_g4_or_g5", "fail_g4_or_g5_se",
              "train_sharpe", "train_sharpe_se", "holdout_sharpe", "holdout_sharpe_se", "emax_sharpe_iid",
              "true_selected", "neighbours_available", "trades_mean", "dsr_mean", "pbo_mean"])


@dataclass
class OverfitConfig:
    m: int = MODES["default"]
    data: Tuple[str, ...] = sim.DATA_TYPES
    years: Tuple[int, ...] = (5, 10, 20)
    signals: Tuple[float, ...] = (0.0, 0.25, 0.5, 0.75, 1.0)
    ks: Tuple[int, ...] = (1, 8, 64, 256)
    presets: Tuple[str, ...] = G.PRESET_NAMES
    cost_bps: float = 2.0
    seed: int = 20261109


def gate_passes(ev: sim.PathEval, k: int, th: dict) -> Dict[str, bool]:
    """Pass / fail of every in-scope gate for the selection at K under the thresholds ``th``."""
    return {"G1": G.g1(ev.fold_sharpes[k], th)[0],
            "G2": G.g2({"value": ev.alpha_t[k], "alpha_daily": ev.alpha_daily[k]}, th)[0],
            "G4": G.g4({"value": ev.dsr[k]}, th)[0],
            "G5": G.g5({"value": ev.pbo[k], "configs": ev.pbo_configs[k]}, th)[0],
            "G6": G.g6_neighbours(ev.base_sharpe[k], ev.neighbour_sharpes[k], th)[0],
            "G7": G.g7(ev.trades[k], th)[0],
            "H": G.holdout(ev.holdout_sharpe[k], ev.train_sharpe[k])[0]}


def run_cell(cfg: OverfitConfig, data: str, years: int, cell: Sequence[int]) -> Dict[float, List[sim.PathEval]]:
    """M evaluated paths for every signal level of one (data, years) cell (common random numbers)."""
    train = 252 * years
    n = sim.BURN_IN + train + sim.HOLDOUT_DAYS
    out: Dict[float, List[sim.PathEval]] = {s: [] for s in cfg.signals}
    for c0 in range(0, cfg.m, CHUNK):
        m = min(CHUNK, cfg.m - c0)
        key = list(cell) + [c0]
        for sr in cfg.signals:
            seed = int(np.random.SeedSequence(key + [0]).generate_state(1)[0])
            perm_rng = make_rng(int(np.random.SeedSequence(key + [1]).generate_state(1)[0]))
            R = sim.simulate_returns(m, n, data, sr, seed)
            out[sr] += [sim.evaluate_path(R[i], train, cfg.ks, cfg.cost_bps, perm_rng) for i in range(m)]
    return out


def _rate(x: Sequence[bool]) -> Tuple[Optional[float], Optional[float]]:
    n = len(x)
    if n == 0:
        return None, None
    p = sum(1 for v in x if v) / n
    return p, math.sqrt(p * (1.0 - p) / n)


def _mean_se(x: Sequence[Optional[float]]) -> Tuple[Optional[float], Optional[float]]:
    a = np.asarray([v for v in x if v is not None], dtype=np.float64)
    if a.size == 0:
        return None, None
    return float(a.mean()), (float(a.std(ddof=1) / math.sqrt(a.size)) if a.size > 1 else None)


def summarise(cfg: OverfitConfig, data: str, years: int, sr: float, evs: List[sim.PathEval]) -> List[dict]:
    rows = []
    for k in cfg.ks:
        ts, ts_se = _mean_se([e.train_sharpe[k] for e in evs])
        hs, hs_se = _mean_se([e.holdout_sharpe[k] for e in evs])
        common = {"train_sharpe": ts, "train_sharpe_se": ts_se, "holdout_sharpe": hs, "holdout_sharpe_se": hs_se,
                  "emax_sharpe_iid": expected_max_sharpe(k, 1.0 / years),
                  "true_selected": float(np.mean([e.selected[k] == sim.TRUE_CONFIG for e in evs])),
                  "neighbours_available": float(np.mean([len(e.neighbour_sharpes[k]) > 0 for e in evs])),
                  "trades_mean": float(np.mean([e.trades[k] for e in evs])),
                  "dsr_mean": _mean_se([e.dsr[k] for e in evs])[0], "pbo_mean": _mean_se([e.pbo[k] for e in evs])[0]}
        for preset in cfg.presets:
            th = G.PRESETS[preset]
            P = [gate_passes(e, k, th) for e in evs]
            row = {"data": data, "years": years, "signal": sr, "k": k, "preset": preset, "m": len(evs)}
            for g in GATES:
                row[f"pass_{g}"], row[f"pass_{g}_se"] = _rate([p[g] for p in P])
            allp = [all(p[g] for g in G.IN_SCOPE) for p in P]
            row["pass_all"], row["pass_all_se"] = _rate(allp)
            row["pass_all_h"], row["pass_all_h_se"] = _rate([a and p["H"] for a, p in zip(allp, P)])
            row["h_given_all"], row["h_given_all_se"] = _rate([p["H"] for a, p in zip(allp, P) if a])
            row["n_all"] = int(sum(allp))
            stat = [all(p[g] for g in STAT_GATES) for p in P]
            row["pass_stat"], row["pass_stat_se"] = _rate(stat)
            row["pass_stat_h"], row["pass_stat_h_se"] = _rate([a and p["H"] for a, p in zip(stat, P)])
            row["h_given_stat"], row["h_given_stat_se"] = _rate([p["H"] for a, p in zip(stat, P) if a])
            row["n_stat"] = int(sum(stat))
            row["h_given_g4"], row["h_given_g4_se"] = _rate([p["H"] for p in P if p["G4"]])
            row["n_g4"] = int(sum(p["G4"] for p in P))
            row["fail_g4_or_g5"], row["fail_g4_or_g5_se"] = _rate([not (p["G4"] and p["G5"]) for p in P])
            row.update(common)
            rows.append(row)
    return rows


def run_study(cfg: OverfitConfig, progress: Optional[Callable[[str], None]] = None) -> List[dict]:
    if cfg.m < 1:
        raise ValueError("m must be >= 1")
    rows: List[dict] = []
    for i, data in enumerate(cfg.data):
        for j, years in enumerate(cfg.years):
            t0 = time.perf_counter()
            evs = run_cell(cfg, data, years, [cfg.seed, i, j])
            for sr in cfg.signals:
                rows += summarise(cfg, data, years, sr, evs[sr])
            if progress:
                progress(f"{data} {years}y: {time.perf_counter() - t0:.1f} s")
    return rows


def calibration(rows: List[dict]) -> List[dict]:
    """SPEC §9.6 checks on the noise rows, standard preset: fail >= 1 in-scope gate; best-of-64 fails G4 or G5."""
    out = []
    for r in rows:
        if r["signal"] == 0.0 and r["preset"] == "standard":
            fail_any = 1.0 - r["pass_all"]
            out.append({"data": r["data"], "years": r["years"], "k": r["k"], "m": r["m"],
                        "fail_any": fail_any, "fail_any_se": r["pass_all_se"],
                        "fail_g4_or_g5": r["fail_g4_or_g5"], "fail_g4_or_g5_se": r["fail_g4_or_g5_se"]})
    return out


def _pct(x: Optional[float], se: Optional[float] = None, n: Optional[int] = None) -> str:
    """Rate in % with its se; a rate of exactly 0 or 1 (se 0) shows the 95 % bound of the rule of
    three instead (0 of n: upper bound 3 / n; n of n: lower bound 1 - 3 / n)."""
    if x is None:
        return "n/a"
    if n and se is not None and x == 0.0:
        return f"0 (≤ {300.0 / n:.2g})"
    if n and se is not None and x == 1.0:
        return f"100 (≥ {100.0 - 300.0 / n:.4g})"
    return f"{100 * x:.1f}" + ("" if se is None else f" ± {100 * se:.1f}")


def summary_markdown(rows: List[dict], cfg: OverfitConfig) -> str:
    lines = [
        "# Overfitting study of the UFOS validation gates (synthetic data)",
        "",
        f"MA-crossover family (256 configurations, UFOS grid of 64), Student-t (nu = 5) innovations iid or GARCH(1,1), "
        f"planted signal Sharpe {', '.join(f'{s:g}' for s in cfg.signals)}, training {', '.join(map(str, cfg.years))} "
        f"years + 24-month holdout, best of K = {', '.join(map(str, cfg.ks))}, costs {cfg.cost_bps:g} bps per unit "
        f"turnover; M = {cfg.m} paths per cell, seed = {cfg.seed}. risklab {risklab.__version__}, numpy "
        f"{np.__version__}. No market data.",
        "",
        "Gates in scope: G1 (time-series), G2, G4, G5, G6 (grid neighbours only), G7, H. Out of scope: G1 rank-IC "
        "form, G3, G6 costs and stress episodes, G8. Rates in %, ± Monte-Carlo se.",
    ]
    if cfg.m < MODES["full"]:
        lines.append(f"M = {cfg.m} is a demo; use `--mode full` (M = {MODES['full']}) for conclusions.")
    lines += ["", "## False-positive rate under noise (signal 0)", "",
              "In-scope gates per preset; standard: + H, statistical gates (without G7) and those + H. A rate "
              "of 0 or 100 % shows the 95 % bound of the rule of three (3 / M).", "",
              "| data | years | K | lenient | standard | strict | standard + H | standard stat. | standard stat. + H |",
              "|---|---|---|---|---|---|---|---|---|"]
    idx = {(r["data"], r["years"], r["signal"], r["k"], r["preset"]): r for r in rows}
    for d in cfg.data:
        for y in cfg.years:
            for k in cfg.ks:
                cells = [idx.get((d, y, 0.0, k, p)) for p in ("lenient", "standard", "strict")]
                if any(c is None for c in cells):
                    continue
                st = cells[1]
                lines.append(f"| {d} | {y} | {k} | " + " | ".join(_pct(c["pass_all"], c["pass_all_se"], c["m"]) for c in cells)
                             + f" | {_pct(st['pass_all_h'], st['pass_all_h_se'], st['m'])} | "
                             f"{_pct(st['pass_stat'], st['pass_stat_se'], st['m'])}"
                             f" | {_pct(st['pass_stat_h'], st['pass_stat_h_se'], st['m'])} |")
    lines += ["", "## Single-gate pass rates under noise, standard preset, 10 years", "",
              "| data | K | " + " | ".join(GATES) + " |", "|---|---|" + "---|" * len(GATES)]
    for d in cfg.data:
        for k in cfg.ks:
            r = idx.get((d, 10, 0.0, k, "standard"))
            if r:
                lines.append(f"| {d} | {k} | " + " | ".join(_pct(r[f'pass_{g}'], r[f'pass_{g}_se'], r['m']) for g in GATES) + " |")
    for key, label in (("all", "in-scope gates"), ("stat", "statistical gates, without G7")):
        lines += ["", f"## Power ({label}, standard preset, K = 64)", "",
                  "| data | years | " + " | ".join(f"SR {s:g}" for s in cfg.signals) + " |",
                  "|---|---|" + "---|" * len(cfg.signals)]
        for d in cfg.data:
            for y in cfg.years:
                cells = [idx.get((d, y, s, 64, "standard")) for s in cfg.signals]
                if all(cells):
                    lines.append(f"| {d} | {y} | " + " | ".join(_pct(c[f"pass_{key}"], c[f"pass_{key}_se"], c["m"])
                                                               for c in cells) + " |")
    lines += ["", "## Calibration (SPEC §9.6, standard preset, noise)", "",
              "| data | years | K | fail >= 1 gate | fail G4 or G5 |", "|---|---|---|---|---|"]
    for c in calibration(rows):
        lines.append(f"| {c['data']} | {c['years']} | {c['k']} | {_pct(c['fail_any'], c['fail_any_se'], c['m'])} | "
                     f"{_pct(c['fail_g4_or_g5'], c['fail_g4_or_g5_se'], c['m'])} |")
    return "\n".join(lines) + "\n"


def write_outputs(rows: List[dict], cfg: OverfitConfig, out_dir: Path) -> List[Path]:
    meta = {"study": STUDY, "code_sha256": code_fingerprint(__file__),
            "config": {k: (list(v) if isinstance(v, tuple) else v) for k, v in asdict(cfg).items()},
            "family": {"fast": list(sim.FAST), "slow": list(sim.SLOW), "true_config": list(sim.TRUE_CONFIG),
                       "holdout_days": sim.HOLDOUT_DAYS, "sigma_annual": sim.SIGMA_ANNUAL, "nu": sim.NU,
                       "garch": [sim.GARCH_A, sim.GARCH_B]},
            "presets": G.PRESETS, "in_scope": list(G.IN_SCOPE), "out_of_scope": G.OUT_OF_SCOPE,
            "calibration": calibration(rows)}
    out_dir = Path(out_dir)
    return [table_to_csv(rows, COLUMNS, out_dir / "results.csv", cfg.seed, meta),
            table_to_json(rows, out_dir / "results.json", cfg.seed, meta),
            text_to_file(summary_markdown(rows, cfg), out_dir / "summary.md")]


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mode", choices=sorted(MODES), default="default",
                    help="smoke (M=4, 5 years, signals 0 / 1), default (M=75, a demo, ~3 min) or full (M=2000)")
    ap.add_argument("--m", type=int, default=None)
    ap.add_argument("--seed", type=int, default=OverfitConfig.seed)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--save-results", action="store_true",
                    help="with --mode full: also save results.json / summary.md as studies/results/"
                         f"{STUDY}_full_*.json / .md (the inputs of the report)")
    args = ap.parse_args(argv)
    if args.save_results and (args.mode != "full" or args.m is not None or args.seed != ap.get_default("seed")):
        ap.error("--save-results needs --mode full with the default M and seed")
    cfg = OverfitConfig(m=args.m if args.m is not None else MODES[args.mode], seed=args.seed)
    if args.mode == "smoke":
        cfg.years, cfg.signals = (5,), (0.0, 1.0)
    out_dir = Path(args.out_dir) if args.out_dir else default_out_dir() / STUDY
    log = None if args.quiet else (lambda msg: print(msg, flush=True))
    start = time.perf_counter()
    rows = run_study(cfg, log)
    paths = write_outputs(rows, cfg, out_dir)
    if args.save_results:
        paths += save_full_results(out_dir, STUDY)
    if log:
        log(f"{len(rows)} rows in {time.perf_counter() - start:.1f} s -> " + ", ".join(str(p) for p in paths))
    return 0


if __name__ == "__main__":
    sys.exit(main())
