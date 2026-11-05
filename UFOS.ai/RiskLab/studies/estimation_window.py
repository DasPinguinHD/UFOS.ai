"""Estimation-window study: how the over-rejection of a correct GARCH-t / FHS shrinks with the window length.

v0.3 found that the correctly specified GARCH(1,1)-t is rejected at about twice the
reference size with a 250-day estimation window, and close to it with 1000 days.
This study turns that into a curve.

Design (synthetic, deterministic seeds):
* Data: GARCH(1,1) with unit-variance t(6) innovations (unconditional sd 1 %).
* Each replication draws ``max(windows) + T`` days; the T = 1000 forecast days are
  the LAST T days for every window, so all windows are compared on the same days
  (a paired design). A model with window w sees the w days before each forecast.
* Models: GARCH(1,1)-t and filtered HS (Gaussian QMLE), each with windows
  {250, 500, 750, 1000}, refitted every ``refit_every`` days (default 50) with a
  daily variance update in between; and the oracle (true VaR / ES) as reference.
* Tests at 5 %: Kupiec, exact binomial, Christoffersen cc (99 % VaR), and
  Acerbi-Szekely Z2 (97.5 % ES, ``z2_sims`` Monte-Carlo draws; seed = a
  ``SeedSequence`` child of the data seed). Reference sizes: exact binomial sums
  for Kupiec and binomial, the oracle row for cc and Z2.
* Reported per model and window: the exceedance rate ± se (over replications);
  the PAIRED difference of the exceedance rate against the oracle on the same
  days (mean of the per-replication differences ± its se), which tests whether
  the VaR is anti-conservative; rejection rates ± sqrt(r (1 - r) / M); and the
  mean absolute relative VaR error against the true VaR. A fit that does not converge (``FitError``) is counted and
  left out.

Run from ``UFOS.ai/RiskLab/``::

    python studies/estimation_window.py                 # default: M = 20 (a demo)
    python studies/estimation_window.py --mode full     # M = 1000

Output (``out/estimation_window/`` unless ``--out-dir``): ``results.csv``,
``results.json``, ``summary.md``. The full run is kept in ``studies/results/``.
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: runs without installing the package
    sys.path.insert(0, _SRC)

import numpy as np
import scipy

import risklab
from risklab import analytic
from risklab.backtest import (REJECT, acerbi_szekely_z2, binomial_exact, christoffersen_cc, exact_size,
                              kupiec_pof)
from risklab.data import garch11, true_var_es
from risklab.dists import LocScale
from risklab.engine import walk_forward
from risklab.models import FilteredHS, FitError, GarchTModel
from risklab.report.export import (code_fingerprint, default_out_dir, save_full_results, table_to_csv,
                                   table_to_json, text_to_file)

STUDY = "estimation_window"
MODELS: Dict[str, Callable] = {"garch_t": GarchTModel, "fhs": FilteredHS}
TESTS = ("kupiec", "binom", "cc", "z2")
MODES = {"smoke": 2, "default": 20, "full": 1000}
COLUMNS = (["model", "window", "T", "m", "m_ok", "failed_reps", "exceed_rate", "exceed_se", "exceed_diff",
            "exceed_diff_se"]
           + [c for t in TESTS for c in (f"reject_{t}", f"se_{t}")]
           + ["ref_kupiec", "ref_binom", "ref_cc", "ref_z2", "var_rel_error"])


@dataclass
class WindowConfig:
    m: int = MODES["default"]
    windows: Tuple[int, ...] = (250, 500, 750, 1000)
    T: int = 1000
    alpha: float = 0.99
    alpha_es: float = 0.975
    reject_at: float = 0.05
    refit_every: int = 50
    z2_sims: int = 500
    seed: int = 20261107
    models: Tuple[str, ...] = field(default_factory=lambda: tuple(MODELS))


def data_seed(base: int, rep: int) -> int:
    return int(np.random.SeedSequence([base, rep]).generate_state(1)[0])


def mc_seed(dseed: int) -> int:
    """Z2 Monte-Carlo seed: a SeedSequence child of the data seed."""
    return int(np.random.SeedSequence(dseed).spawn(1)[0].generate_state(1)[0])


def mc_se(rate: Optional[float], m: int) -> Optional[float]:
    return None if rate is None or m < 1 else math.sqrt(rate * (1.0 - rate) / m)


def _oracle_dists(series, start: int):
    sd = series.sigma_true[start:]
    nu = series.dist_params["nu"]
    return tuple(LocScale(0.0, s, nu) for s in analytic.t_scale_from_sd(sd, nu))


def _tests(losses, var, var_es, es_es, dists, cfg: WindowConfig, zseed: int) -> Dict[str, bool]:
    exc = losses > var
    return {
        "kupiec": kupiec_pof(exc, cfg.alpha, cfg.reject_at).decision == REJECT,
        "binom": binomial_exact(exc, cfg.alpha, cfg.reject_at).decision == REJECT,
        "cc": christoffersen_cc(exc, cfg.alpha, cfg.reject_at).decision == REJECT,
        "z2": acerbi_szekely_z2(losses, var_es, es_es, dists, cfg.alpha_es, seed=zseed, m_sim=cfg.z2_sims,
                                reject_at=cfg.reject_at).decision == REJECT,
    }


def run_study(cfg: WindowConfig, progress: Optional[Callable[[str], None]] = None) -> List[dict]:
    if cfg.m < 1:
        raise ValueError("m must be >= 1")
    w_max = max(cfg.windows)
    alphas = sorted({cfg.alpha, cfg.alpha_es})
    keys = [("oracle", 0)] + [(m, w) for m in cfg.models for w in cfg.windows]
    acc = {k: {"exceed": [], "diff": [], "err": [], "failed": 0, **{t: 0 for t in TESTS}} for k in keys}
    for rep in range(cfg.m):
        dseed = data_seed(cfg.seed, rep)
        series = garch11(w_max + cfg.T, 2e-6, 0.08, 0.90, innovations="t", nu=6.0, seed=dseed)
        zseed = mc_seed(dseed)
        truth = true_var_es(series, alphas)
        losses = series.losses[w_max:]
        t_var = truth.var[cfg.alpha][w_max:]
        res = _tests(losses, t_var, truth.var[cfg.alpha_es][w_max:], truth.es[cfg.alpha_es][w_max:],
                     _oracle_dists(series, w_max), cfg, zseed)
        a = acc[("oracle", 0)]
        oracle_rate = float(np.mean(losses > t_var))
        a["exceed"].append(oracle_rate)
        a["diff"].append(0.0)
        a["err"].append(0.0)
        for t in TESTS:
            a[t] += res[t]
        for model in cfg.models:
            for w in cfg.windows:
                a = acc[(model, w)]
                try:
                    frame = walk_forward(series.losses[w_max - w:], MODELS[model], w, alphas,
                                         refit_every=cfg.refit_every)
                except FitError:
                    a["failed"] += 1
                    continue
                res = _tests(frame.loss, frame.var[cfg.alpha], frame.var[cfg.alpha_es], frame.es[cfg.alpha_es],
                             frame.dists, cfg, zseed)
                a["exceed"].append(float(np.mean(frame.exceedance[cfg.alpha])))
                a["diff"].append(a["exceed"][-1] - oracle_rate)  # paired: same days, same data
                a["err"].append(float(np.mean(np.abs(frame.var[cfg.alpha] - t_var) / t_var)))
                for t in TESTS:
                    a[t] += res[t]
        if progress and (rep + 1) % max(1, cfg.m // 10) == 0:
            progress(f"{rep + 1}/{cfg.m} replications")
    ref_k = exact_size("kupiec_pof", cfg.T, cfg.alpha, cfg.reject_at)["size"]
    ref_b = exact_size("binomial_exact", cfg.T, cfg.alpha, cfg.reject_at)["size"]
    o = acc[("oracle", 0)]
    ref_cc, ref_z2 = o["cc"] / cfg.m, o["z2"] / cfg.m
    rows = []
    for model, w in keys:
        a = acc[(model, w)]
        m_ok = cfg.m - a["failed"]
        ex = np.asarray(a["exceed"])
        dd = np.asarray(a["diff"])
        row = {"model": model, "window": w if model != "oracle" else None, "T": cfg.T, "m": cfg.m, "m_ok": m_ok,
               "failed_reps": a["failed"], "exceed_rate": float(ex.mean()) if m_ok else None,
               "exceed_se": float(ex.std(ddof=1) / math.sqrt(m_ok)) if m_ok > 1 else None,
               "exceed_diff": float(dd.mean()) if m_ok else None,
               "exceed_diff_se": float(dd.std(ddof=1) / math.sqrt(m_ok)) if m_ok > 1 else None}
        for t in TESTS:
            r = a[t] / m_ok if m_ok else None
            row[f"reject_{t}"], row[f"se_{t}"] = r, mc_se(r, m_ok)
        row.update({"ref_kupiec": ref_k, "ref_binom": ref_b, "ref_cc": ref_cc, "ref_z2": ref_z2,
                    "var_rel_error": float(np.mean(a["err"])) if m_ok else None})
        rows.append(row)
    return rows


def _pct(x: Optional[float], d: int = 1) -> str:
    return "n/a" if x is None else f"{100 * x:.{d}f} %"


def _r(row: dict, t: str) -> str:
    v, se = row[f"reject_{t}"], row[f"se_{t}"]
    return "n/a" if v is None else f"{100 * v:.1f} ± {100 * se:.1f}"


def summary_markdown(rows: List[dict], cfg: WindowConfig) -> str:
    ref = rows[0]
    lines = [
        "# Estimation window: over-rejection of a correct GARCH-t / FHS against the window length (synthetic)",
        "",
        f"GARCH(1,1)-t6 data, T = {cfg.T} forecast days (the same days for every window), VaR at {cfg.alpha}, "
        f"ES (Z2) at {cfg.alpha_es}, tests at {100 * cfg.reject_at:g} %, refit every {cfg.refit_every} days, "
        f"M = {cfg.m} replications, seed = {cfg.seed}. risklab {risklab.__version__}, numpy {np.__version__}, "
        f"scipy {scipy.__version__}.",
        "",
        "Rejection rates in % ± se (se = sqrt(r (1 - r) / M)); the exceedance rate ± its se over replications. "
        f"Reference sizes: Kupiec {_pct(ref['ref_kupiec'], 2)} and binomial {_pct(ref['ref_binom'], 2)} "
        f"(exact), cc {_r(ref, 'cc')} and Z2 {_r(ref, 'z2')} (oracle row).",
    ]
    if cfg.m < MODES["full"]:
        lines.append(f"M = {cfg.m} is a demo; use `--mode full` (M = {MODES['full']}) for conclusions.")
    lines += ["", "| model | window | exceed rate | paired diff vs oracle (pp) | Kupiec | binomial | cc | Z2 "
              "| mean abs rel VaR error | note |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        ex = "n/a" if r["exceed_rate"] is None else f"{100 * r['exceed_rate']:.2f} ± {100 * (r['exceed_se'] or 0):.2f} %"
        note = f"{r['failed_reps']} failed fits" if r["failed_reps"] else ""
        diff = ("--" if r["model"] == "oracle" or r["exceed_diff"] is None
                else f"{round(100 * r['exceed_diff'], 3) + 0.0:+.3f} ± {100 * (r['exceed_diff_se'] or 0):.3f}")
        lines.append(f"| {r['model']} | {r['window'] or 'true VaR'} | {ex} | {diff} | {_r(r, 'kupiec')} | {_r(r, 'binom')} | "
                     f"{_r(r, 'cc')} | {_r(r, 'z2')} | {_pct(r['var_rel_error'])} | {note} |")
    lines += ["", "Reading: a correctly specified model should be rejected at about the reference size; the "
              "distance above it at short windows is estimation risk, and it shrinks as the window grows. The "
              "rejection rates can return to the reference size while the VaR is still slightly "
              "anti-conservative: the paired difference of the exceedance rate against the oracle (same data, "
              "same days) measures that directly; it is significant where it exceeds about 2 se."]
    return "\n".join(lines) + "\n"


def write_outputs(rows: List[dict], cfg: WindowConfig, out_dir: Path) -> List[Path]:
    meta = {"study": STUDY, "code_sha256": code_fingerprint(__file__), "config": {k: (list(v) if isinstance(v, tuple) else v) for k, v in asdict(cfg).items()},
            "scipy_version": scipy.__version__}
    out_dir = Path(out_dir)
    return [table_to_csv(rows, COLUMNS, out_dir / "results.csv", cfg.seed, meta),
            table_to_json(rows, out_dir / "results.json", cfg.seed, meta),
            text_to_file(summary_markdown(rows, cfg), out_dir / "summary.md")]


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mode", choices=sorted(MODES), default="default",
                    help="smoke (M=2, windows 250/500, T=300), default (M=20, a demo) or full (M=1000)")
    ap.add_argument("--m", type=int, default=None)
    ap.add_argument("--seed", type=int, default=WindowConfig.seed)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--save-results", action="store_true",
                    help="with --mode full: also save results.json / summary.md as studies/results/"
                         f"{STUDY}_full_*.json / .md (the inputs of the report)")
    args = ap.parse_args(argv)
    if args.save_results and (args.mode != "full" or args.m is not None or args.seed != ap.get_default("seed")):
        ap.error("--save-results needs --mode full with the default M and seed")
    cfg = WindowConfig(m=args.m if args.m is not None else MODES[args.mode], seed=args.seed)
    if args.mode == "smoke":
        cfg.windows, cfg.T, cfg.z2_sims = (250, 500), 300, 199
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
