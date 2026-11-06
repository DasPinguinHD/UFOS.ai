"""10-day 99 % VaR: square-root-of-time against GARCH Monte Carlo and the block bootstrap (synthetic data).

Question: how far is each 10-day VaR estimate from the TRUE 10-day VaR?

Design (all synthetic, deterministic seeds):
* Data: iid normal (sd 1 %), iid unit-variance Student-t (nu = 5, sd 1 %),
  GARCH(1,1) with unit-variance t(6) innovations (unconditional sd 1 %).
* Each replication draws ``window + 10`` days (window = 500); the origin is day
  ``window``: every method sees only the 500 days before it.
* Methods (10-day VaR at 99 %):
  - ``sqrt10_normal``: sqrt(10) x the one-day normal VaR (static, iid normal assumption);
  - ``sqrt10_garch``: sqrt(10) x the one-day GARCH(1,1)-t VaR;
  - ``garch_mc``: Monte Carlo from the same fitted GARCH(1,1)-t (``n_paths`` paths
    of 10 days started at sigma2_{t+1});
  - ``bootstrap``: stationary block bootstrap of the window, block length
    "auto" (Politis-White 2004 / Patton-Politis-White 2009), ``n_paths`` sums.
* Truth: :func:`risklab.data.true_var_es_h` - closed form for iid normal, Monte
  Carlo with ``truth_sims`` paths for iid t and for GARCH (from the true
  parameters, conditional on the true variance at the origin).
* Metrics per data set and method, relative to the truth:
  bias = mean((VaR_hat - VaR_true) / VaR_true) with its Monte-Carlo standard
  error sd / sqrt(M); RMSE = sqrt(mean(((VaR_hat - VaR_true) / VaR_true)**2));
  plus the mean internal Monte-Carlo error of the simulated estimates and of the
  truth (``var_se / VaR_true``).
* A GARCH fit that does not converge raises ``FitError``; an undefined automatic
  block length raises ``BlockLengthError``. Such replications are counted
  (``failed_reps``, ``block_undefined_reps``) and left out for all methods, so
  every method is compared on the same replications.
* Seeds: the data seed of a replication comes from ``SeedSequence([seed, data set,
  replication])``; the GARCH Monte Carlo, the bootstrap and the Monte-Carlo truth
  use three independent children ``SeedSequence(data seed).spawn(3)``.

Run from ``UFOS.ai/RiskLab/``::

    python studies/horizon_10d.py                 # default: M = 200 (a demo); measured 30 s on the user's PC
    python studies/horizon_10d.py --mode full     # M = 1000; measured 2.1 minutes in the cloud container

Output (``out/horizon_10d/`` unless ``--out-dir``): ``results.csv``,
``results.json`` and ``summary.md``. The full-run summary is kept in
``studies/results/horizon_10d_full_summary.md``.
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: runs without installing the package
    sys.path.insert(0, _SRC)

import numpy as np
import scipy

import risklab
from risklab.bootstrap import BlockLengthError, bootstrap_h_sums, resolve_block_len
from risklab.conventions import make_rng
from risklab.data import garch11, iid_normal, iid_student_t, true_var_es_h
from risklab.horizon import empirical_var_es, simulate_garch_sums, sqrt_time
from risklab.models import FitError, GarchTModel, NormalModel
from risklab.report.export import (code_fingerprint, default_out_dir, save_full_results, table_to_csv,
                                   table_to_json, text_to_file)

STUDY = "horizon_10d"

GENERATORS: Dict[str, Callable] = {
    "iid_normal": lambda n, seed: iid_normal(n, 0.0, 0.01, seed),
    "iid_t5": lambda n, seed: iid_student_t(n, 0.0, 0.01, 5.0, seed),
    "garch_t6": lambda n, seed: garch11(n, 2e-6, 0.08, 0.90, innovations="t", nu=6.0, seed=seed),
}

METHODS = ("sqrt10_normal", "sqrt10_garch", "garch_mc", "bootstrap")
MODES = {"smoke": 3, "default": 200, "full": 1000}
COLUMNS = ["generator", "method", "h", "m", "m_ok", "failed_reps", "block_undefined_reps", "bias", "bias_se",
           "rmse", "mean_var_true",
           "est_mc_rel_se", "truth_mc_rel_se", "mean_block_len", "truth_method"]


@dataclass
class HorizonConfig:
    m: int = MODES["default"]
    h: int = 10
    alpha: float = 0.99
    window: int = 500
    n_paths: int = 20000
    truth_sims: int = 50000
    seed: int = 20261106


def data_seed(base: int, gen_index: int, rep: int) -> int:
    return int(np.random.SeedSequence([base, gen_index, rep]).generate_state(1)[0])


def mc_seeds(dseed: int, k: int = 3) -> List[int]:
    """k independent Monte-Carlo seeds: children of ``SeedSequence(dseed)`` (not dseed + 1, + 2, ...)."""
    return [int(c.generate_state(1)[0]) for c in np.random.SeedSequence(dseed).spawn(k)]


def _one(series, cfg: HorizonConfig, seeds: Sequence[int]) -> Dict[str, dict]:
    """All method estimates for one replication: {method: {"var", "mc_se", "block_len"}}."""
    w, h, a = cfg.window, cfg.h, cfg.alpha
    past = series.losses[:w]
    out = {}
    normal = NormalModel()
    normal.fit(past)
    out["sqrt10_normal"] = {"var": float(sqrt_time(normal.forecast([a]).var[a], h)), "mc_se": None}
    garch = GarchTModel()
    garch.fit(past)  # FitError propagates: the replication is counted as failed
    out["sqrt10_garch"] = {"var": float(sqrt_time(garch.forecast([a]).var[a], h)), "mc_se": None}
    est = empirical_var_es(simulate_garch_sums(garch.params, garch.sigma2_next, h, cfg.n_paths,
                                               make_rng(seeds[0])), a)
    out["garch_mc"] = {"var": est.var, "mc_se": est.var_se}
    b = resolve_block_len(past, "auto")  # BlockLengthError propagates: counted by the caller
    est = empirical_var_es(bootstrap_h_sums(past, h, b, cfg.n_paths, make_rng(seeds[1])), a)
    out["bootstrap"] = {"var": est.var, "mc_se": est.var_se, "block_len": b}
    return out


def run_study(cfg: HorizonConfig, progress: Optional[Callable[[str], None]] = None) -> List[dict]:
    if cfg.m < 1:
        raise ValueError("m must be >= 1")
    rows = []
    for gi, gen in enumerate(GENERATORS):
        rel = {m: [] for m in METHODS}
        est_se = {m: [] for m in METHODS}
        truth_se, truths, blocks, failed, undefined = [], [], [], 0, 0
        method_name = None
        for rep in range(cfg.m):
            seed = data_seed(cfg.seed, gi, rep)
            series = GENERATORS[gen](cfg.window + cfg.h, seed)
            seeds = mc_seeds(seed)
            try:
                est = _one(series, cfg, seeds)
            except FitError:
                failed += 1
                continue
            except BlockLengthError:
                undefined += 1
                continue
            truth = true_var_es_h(series, [cfg.alpha], cfg.h, cfg.truth_sims, seeds[2], origins=[cfg.window])
            method_name = truth.method
            vt = float(truth.var[cfg.alpha][0])
            truths.append(vt)
            truth_se.append(float(truth.var_se[cfg.alpha][0]) / vt)
            for m in METHODS:
                rel[m].append((est[m]["var"] - vt) / vt)
                if est[m]["mc_se"] is not None:
                    est_se[m].append(est[m]["mc_se"] / vt)
            blocks.append(est["bootstrap"]["block_len"])
        m_ok = cfg.m - failed - undefined
        for m in METHODS:
            r = np.asarray(rel[m])
            rows.append({
                "generator": gen, "method": m, "h": cfg.h, "m": cfg.m, "m_ok": m_ok, "failed_reps": failed,
                "block_undefined_reps": undefined,
                "bias": float(r.mean()) if m_ok else None,
                "bias_se": float(r.std(ddof=1) / math.sqrt(m_ok)) if m_ok > 1 else None,
                "rmse": float(np.sqrt(np.mean(r * r))) if m_ok else None,
                "mean_var_true": float(np.mean(truths)) if m_ok else None,
                "est_mc_rel_se": float(np.mean(est_se[m])) if est_se[m] else None,
                "truth_mc_rel_se": float(np.mean(truth_se)) if m_ok else None,
                "mean_block_len": float(np.mean(blocks)) if (m == "bootstrap" and blocks) else None,
                "truth_method": method_name,
            })
        if progress:
            progress(f"{gen}: done")
    return rows


def _p(x: Optional[float], digits: int = 1) -> str:
    return "n/a" if x is None else f"{100.0 * x:.{digits}f} %"


def summary_markdown(rows: List[dict], cfg: HorizonConfig) -> str:
    lines = [
        "# 10-day 99 % VaR: sqrt(10) scaling against GARCH Monte Carlo and the block bootstrap (synthetic data)",
        "",
        f"h = {cfg.h} days, alpha = {cfg.alpha}, estimation window = {cfg.window} days, M = {cfg.m} replications "
        f"per data set, {cfg.n_paths} simulated paths per estimate, {cfg.truth_sims} paths for the Monte-Carlo "
        f"truth, seed = {cfg.seed}. risklab {risklab.__version__}, numpy {np.__version__}, scipy {scipy.__version__}.",
        "",
        "Bias and RMSE are relative to the true 10-day VaR of each replication (mean of (estimate - truth) / truth). "
        "`± se` is the Monte-Carlo standard error of the bias over the M replications. \"est MC se\" is the mean "
        "internal simulation error of the estimate (binomial order-statistic interval), \"truth MC se\" the same "
        "for the simulated truth (0 for the closed-form iid-normal truth).",
    ]
    if cfg.m < MODES["full"]:
        lines.append(f"M = {cfg.m} is a demo; use `--mode full` (M = {MODES['full']}) for conclusions.")
    lines += ["", "| data | method | bias | RMSE | est MC se | truth MC se | mean block length | note |",
              "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        bias = "n/a" if r["bias"] is None else f"{100 * r['bias']:+.1f} ± {100 * (r['bias_se'] or 0):.1f} %"
        notes = []
        if r["failed_reps"]:
            notes.append(f"{r['failed_reps']} failed GARCH fits")
        if r["block_undefined_reps"]:
            notes.append(f"{r['block_undefined_reps']} undefined auto block lengths")
        note = "; ".join(notes)
        block = "" if r["mean_block_len"] is None else f"{r['mean_block_len']:.2f}"
        lines.append(f"| {r['generator']} | {r['method']} | {bias} | {_p(r['rmse'])} | {_p(r['est_mc_rel_se'], 2)} | "
                     f"{_p(r['truth_mc_rel_se'], 2)} | {block} | {note} |")
    lines += [
        "",
        "## Reading the table",
        "",
        "- What to expect: sqrt(10) scaling is exact only for iid normal losses with zero mean. For fat-tailed "
        "iid data the 10-day sum is closer to normal than one day (central limit theorem), so sqrt(10) x a "
        "fat-tailed one-day quantile tends to overstate the 10-day VaR; under GARCH, sqrt(10) ignores that "
        "volatility mean-reverts over the 10 days (it overstates in high-volatility states and understates in "
        "calm ones).",
        "- GARCH Monte Carlo uses the fitted dynamics and innovation distribution; its error is mainly the "
        "estimation error of the 500-day fit plus the stated simulation error.",
        "- The block bootstrap needs no model, but a 500-day window holds only about five 1 % tail days, so "
        "its 10-day tail is noisy. Its automatic block length is chosen on the loss LEVELS, which are nearly "
        "uncorrelated even under GARCH (the dependence is in the squares), so the blocks are short; and it "
        "resamples the whole window, so it does not condition on the current volatility.",
        f"- Truth: {next((r['truth_method'] for r in rows if r['generator'] == 'garch_t6'), 'n/a')} for GARCH; "
        "closed form for iid normal.",
    ]
    b = next((r for r in rows if (r["generator"], r["method"]) == ("garch_t6", "bootstrap")), None)
    if b is not None and b["bias"] is not None and b["bias_se"]:
        z = b["bias"] / b["bias_se"]
        verdict = "biased" if abs(z) >= 2 else "not significantly biased"
        lines.append(f"- On GARCH-t6 the bootstrap bias is {100 * b['bias']:+.1f} ± {100 * b['bias_se']:.1f} % "
                     f"({z:+.1f} se): {verdict}; mean automatic block length {b['mean_block_len']:.2f} days.")
    return "\n".join(lines) + "\n"


def write_outputs(rows: List[dict], cfg: HorizonConfig, out_dir: Path) -> List[Path]:
    meta = {"study": STUDY, "code_sha256": code_fingerprint(__file__), "config": asdict(cfg), "scipy_version": scipy.__version__}
    out_dir = Path(out_dir)
    return [
        table_to_csv(rows, COLUMNS, out_dir / "results.csv", cfg.seed, meta),
        table_to_json(rows, out_dir / "results.json", cfg.seed, meta),
        text_to_file(summary_markdown(rows, cfg), out_dir / "summary.md"),
    ]


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mode", choices=sorted(MODES), default="default",
                    help="smoke (M=3, small simulations), default (M=200, a demo) or full (M=1000)")
    ap.add_argument("--m", type=int, default=None, help="replications per data set (overrides --mode)")
    ap.add_argument("--seed", type=int, default=HorizonConfig.seed)
    ap.add_argument("--out-dir", default=None, help="default: RiskLab/out/horizon_10d")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--save-results", action="store_true",
                    help="with --mode full: also save results.json / summary.md as studies/results/"
                         f"{STUDY}_full_*.json / .md (the inputs of the report)")
    args = ap.parse_args(argv)
    if args.save_results and (args.mode != "full" or args.m is not None or args.seed != ap.get_default("seed")):
        ap.error("--save-results needs --mode full with the default M and seed")
    cfg = HorizonConfig(m=args.m if args.m is not None else MODES[args.mode], seed=args.seed)
    if args.mode == "smoke":
        cfg.n_paths, cfg.truth_sims = 1000, 2000
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
