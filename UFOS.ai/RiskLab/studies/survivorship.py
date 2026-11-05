"""Survivorship bias in growth base rates: a synthetic, data-licence-free demonstration.

Question: a "base rate" such as "x % of firms grow revenue by at least g % a year
over 10 years" is often computed from a database of firms that are listed TODAY.
Firms that failed (or were taken over after a weak decade) are missing. How much
does that overstate the base rate?

Synthetic universe (no real firms, no real data):
* N firms, 10 years. Log revenue growth of firm i in year t:
  ``g_it = mu_i + sigma * eps_it`` with firm drift ``mu_i ~ N(drift_mean, drift_sd)``
  and ``eps_it ~ N(0, 1)`` iid.
* Each year a surviving firm exits (failure or acquisition) with probability
  ``p_it = min(p_max, h0 * exp(-kappa * z_it))``, where
  ``z_it = (g_it - drift_mean) / sqrt(drift_sd**2 + sigma**2)`` is the standardised
  growth of that year: the hazard rises with poor growth, ``kappa`` is the
  hazard strength (kappa = 0: exits unrelated to growth), ``h0`` the base hazard.
* 10-year CAGR of a survivor: ``exp(mean_t g_it) - 1``.

Three base rates for a threshold g, each answering a different question:
* (a) naive, survivors only: ``#{survivors with CAGR >= g} / #survivors`` -
  "what share of TODAY's listed firms grew this fast?" (what a database of
  current constituents shows);
* (c) counterfactual, all firms had they survived: every simulated path is
  complete, so the CAGR exists for exited firms too:
  ``#{all firms with path CAGR >= g} / N`` - "what share of firms has the
  growth DRIVER for this?" (the selection-free rate);
* (b) full cohort, dead = miss: ``#{survivors with CAGR >= g} / N`` - "what is
  the probability that a firm picked at the START achieves this as a going
  concern?" (the forecasting base rate).

Decomposition of the naive bias (a) - (b):
* selection bias = (a) - (c): survivors are not a random sample when exits
  depend on growth (kappa > 0); with kappa = 0 it is 0 up to noise;
* attrition = (c) - (b): firms that would have reached the threshold but exited
  count as misses in (b); it is > 0 whenever firms exit, even with kappa = 0.
So "exits alone (kappa = 0) inflate the naive rate" is true only against (b),
i.e. when exited firms count as misses; against (c) the naive rate is unbiased
at kappa = 0.

Modelling choices: acquisitions are treated like failures (an exit is an exit;
a takeover after a strong decade would REDUCE the bias); the ± se columns are
Monte-Carlo noise only - the parameter choices (h0, kappa, the growth model)
dominate the size of the bias.

With zero hazard (h0 = 0) nobody exits and all three rates coincide exactly.

M replications per setting give the Monte-Carlo mean and its se. A sweep over
kappa (with h0 fixed) and the zero-hazard control show when the bias matters.

Run from ``UFOS.ai/RiskLab/``::

    python studies/survivorship.py                 # default: M = 200 (a demo)
    python studies/survivorship.py --mode full     # M = 2000
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

STUDY = "survivorship"
MODES = {"smoke": 5, "default": 200, "full": 2000}
COLUMNS = ["h0", "kappa", "threshold", "m", "survival_rate", "naive", "naive_se", "counterfactual", "counterfactual_se",
           "true", "true_se", "bias", "bias_se", "selection", "selection_se", "attrition", "attrition_se"]


@dataclass
class SurvivorConfig:
    m: int = MODES["default"]
    n_firms: int = 2000
    years: int = 10
    drift_mean: float = 0.05
    drift_sd: float = 0.05
    sigma: float = 0.15
    h0: float = 0.04
    p_max: float = 0.9
    kappas: Tuple[float, ...] = (0.0, 0.5, 1.0, 1.5, 2.0)
    thresholds: Tuple[float, ...] = (0.0, 0.05, 0.10, 0.15, 0.20)
    seed: int = 20261108


def simulate_cohort(cfg: SurvivorConfig, h0: float, kappa: float, rng: np.random.Generator):
    """(alive after the last year: bool array, 10-year CAGR: NaN for exited firms, path CAGR of every firm).

    The path CAGR is the counterfactual CAGR the firm would have had without the exit
    (its whole growth path is simulated before the exits are applied)."""
    n, t = cfg.n_firms, cfg.years
    mu = rng.normal(cfg.drift_mean, cfg.drift_sd, size=n)
    g = mu[:, None] + cfg.sigma * rng.standard_normal((n, t))
    u = rng.random((n, t))
    z = (g - cfg.drift_mean) / math.sqrt(cfg.drift_sd ** 2 + cfg.sigma ** 2)
    p = np.minimum(cfg.p_max, h0 * np.exp(-kappa * z))
    exits = u < p
    alive = ~np.any(exits, axis=1)
    path_cagr = np.exp(g.mean(axis=1)) - 1.0
    cagr = np.where(alive, path_cagr, np.nan)
    return alive, cagr, path_cagr


def base_rates(alive: np.ndarray, cagr: np.ndarray, threshold: float) -> Tuple[float, float]:
    """(naive survivors-only rate, full-cohort rate)."""
    hits = int(np.count_nonzero(alive & (np.nan_to_num(cagr, nan=-np.inf) >= threshold)))
    n_alive = int(np.count_nonzero(alive))
    naive = hits / n_alive if n_alive else float("nan")
    return naive, hits / alive.shape[0]


def counterfactual_rate(path_cagr: np.ndarray, threshold: float) -> float:
    """Share of ALL firms whose complete growth path reaches the threshold (as if none had exited)."""
    return float(np.count_nonzero(path_cagr >= threshold)) / path_cagr.shape[0]


def run_setting(cfg: SurvivorConfig, h0: float, kappa: float, seed_key: Sequence[int]) -> List[dict]:
    rng = make_rng(int(np.random.SeedSequence(list(seed_key)).generate_state(1)[0]))
    naive = np.empty((cfg.m, len(cfg.thresholds)))
    true = np.empty_like(naive)
    cf = np.empty_like(naive)
    surv = np.empty(cfg.m)
    for r in range(cfg.m):
        alive, cagr, path_cagr = simulate_cohort(cfg, h0, kappa, rng)
        surv[r] = alive.mean()
        for j, g in enumerate(cfg.thresholds):
            naive[r, j], true[r, j] = base_rates(alive, cagr, g)
            cf[r, j] = counterfactual_rate(path_cagr, g)
    se = (lambda x: float(x.std(ddof=1) / math.sqrt(cfg.m)) if cfg.m > 1 else None)
    rows = []
    for j, g in enumerate(cfg.thresholds):
        bias, sel, att = naive[:, j] - true[:, j], naive[:, j] - cf[:, j], cf[:, j] - true[:, j]
        rows.append({"h0": h0, "kappa": kappa, "threshold": g, "m": cfg.m, "survival_rate": float(surv.mean()),
                     "naive": float(naive[:, j].mean()), "naive_se": se(naive[:, j]),
                     "counterfactual": float(cf[:, j].mean()), "counterfactual_se": se(cf[:, j]),
                     "true": float(true[:, j].mean()), "true_se": se(true[:, j]),
                     "bias": float(bias.mean()), "bias_se": se(bias),
                     "selection": float(sel.mean()), "selection_se": se(sel),
                     "attrition": float(att.mean()), "attrition_se": se(att)})
    return rows


def run_study(cfg: SurvivorConfig, progress: Optional[Callable[[str], None]] = None) -> List[dict]:
    if cfg.m < 1:
        raise ValueError("m must be >= 1")
    rows = run_setting(cfg, 0.0, 0.0, [cfg.seed, 0])  # zero-hazard control: bias exactly 0
    for i, k in enumerate(cfg.kappas):
        rows += run_setting(cfg, cfg.h0, k, [cfg.seed, i + 1])
        if progress:
            progress(f"kappa={k}: done")
    return rows


def _pp(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}"


def _pm(x: Optional[float], se: Optional[float]) -> str:
    return "n/a" if x is None else f"{round(100 * x, 1) + 0.0:.1f} ± {100 * (se or 0):.2f}"


def summary_markdown(rows: List[dict], cfg: SurvivorConfig) -> str:
    lines = [
        "# Survivorship bias in 10-year growth base rates (synthetic universe)",
        "",
        f"N = {cfg.n_firms} firms, {cfg.years} years, log growth mu_i + sigma eps with mu_i ~ N({cfg.drift_mean}, "
        f"{cfg.drift_sd}), sigma = {cfg.sigma}; yearly exit probability min({cfg.p_max}, h0 exp(-kappa z)), "
        f"h0 = {cfg.h0}; M = {cfg.m} cohorts per setting, seed = {cfg.seed}. risklab {risklab.__version__}, "
        f"numpy {np.__version__}. No real firms or data.",
        "",
        "Base rate = share of firms with a 10-year CAGR >= the threshold, in %. Three rates, three questions:",
        "",
        "- naive (survivors only): what share of today's listed firms grew this fast?",
        "- counterfactual (all firms, had they survived): what share of firms has the growth driver for this?",
        "- full cohort (exited = miss): how likely is a firm picked at the start to achieve it as a going concern?",
        "",
        "Decomposition: total bias = naive - full cohort = selection (naive - counterfactual) + attrition "
        "(counterfactual - full cohort), in percentage points. Acquisitions are treated like failures (a "
        "modelling choice). The ± se is Monte-Carlo noise only; the parameter choices dominate the size of "
        "the bias.",
    ]
    if cfg.m < MODES["full"]:
        lines.append(f"M = {cfg.m} is a demo; use `--mode full` (M = {MODES['full']}) for conclusions.")
    lines += ["", "| h0 | kappa | survival rate | CAGR threshold | naive | counterfactual | full cohort "
              "| total bias (pp) | selection (pp) | attrition (pp) |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['h0']:g} | {r['kappa']:g} | {100 * r['survival_rate']:.1f} % | "
                     f"{100 * r['threshold']:g} % | {_pp(r['naive'])} % | {_pp(r['counterfactual'])} % | "
                     f"{_pp(r['true'])} % | {_pm(r['bias'], r['bias_se'])} | {_pm(r['selection'], r['selection_se'])} | "
                     f"{_pm(r['attrition'], r['attrition_se'])} |")
    lines += ["", "Reading: with zero hazard all three rates coincide. With exits unrelated to growth (kappa = 0) "
              "the survivors are a random sample: the selection bias is 0 up to noise, and the naive rate overstates "
              "only the full-cohort rate (attrition, dead = miss). With kappa > 0 the exits remove exactly the "
              "slow growers, and the naive rate also overstates the counterfactual rate (selection)."]
    return "\n".join(lines) + "\n"


def write_outputs(rows: List[dict], cfg: SurvivorConfig, out_dir: Path) -> List[Path]:
    meta = {"study": STUDY, "code_sha256": code_fingerprint(__file__), "config": {k: (list(v) if isinstance(v, tuple) else v) for k, v in asdict(cfg).items()}}
    out_dir = Path(out_dir)
    return [table_to_csv(rows, COLUMNS, out_dir / "results.csv", cfg.seed, meta),
            table_to_json(rows, out_dir / "results.json", cfg.seed, meta),
            text_to_file(summary_markdown(rows, cfg), out_dir / "summary.md")]


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mode", choices=sorted(MODES), default="default",
                    help="smoke (M=5, 300 firms), default (M=200, a demo) or full (M=2000)")
    ap.add_argument("--m", type=int, default=None)
    ap.add_argument("--seed", type=int, default=SurvivorConfig.seed)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--save-results", action="store_true",
                    help="with --mode full: also save results.json / summary.md as studies/results/"
                         f"{STUDY}_full_*.json / .md (the inputs of the report)")
    args = ap.parse_args(argv)
    if args.save_results and (args.mode != "full" or args.m is not None or args.seed != ap.get_default("seed")):
        ap.error("--save-results needs --mode full with the default M and seed")
    cfg = SurvivorConfig(m=args.m if args.m is not None else MODES[args.mode], seed=args.seed)
    if args.mode == "smoke":
        cfg.n_firms = 300
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
