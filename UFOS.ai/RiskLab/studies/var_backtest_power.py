"""Size and power of the VaR backtests on synthetic data (Risk Lab showcase study).

Question: how often do Kupiec POF, the exact binomial test, Christoffersen
independence (ind) and conditional coverage (cc) reject
* SIZE  - a correct VaR (ideally at the nominal level, 5 %), and
* POWER - a wrong VaR model (the higher, the better the test)?

Design (all synthetic, deterministic seeds):
* Data generators (daily losses, unconditional sd about 1 %): iid normal,
  iid Student-t (nu = 5), GARCH(1,1) with normal innovations, two-state regime switching.
* Each replication draws ``window + T`` days; models are fitted walk-forward on a
  rolling ``window`` (250 days) and forecast the next day's 99 % VaR; the tests
  run on the T forecast days (T = 250 and 1000).
* Reference size per T: the EXACT size of Kupiec and of the exact binomial
  test under H0 (binomial sums, :func:`risklab.backtest.exact_size`). Kupiec
  is asymptotic: at T = 250 its exact size is 9.48 %, not 5 %, because x = 0
  is rejected. For ind and cc there is no closed form; their reference is the
  empirical rate of the oracle row of the same data and T.
* Size rows: the "oracle" (the true conditional VaR from
  :func:`risklab.data.true_var_es`) on every generator, plus correctly specified
  estimated models on iid data (normal on normal, t on t; estimation risk can
  move them away from the reference) and HS on normal data, which is NOT a clean
  size row: the HS exceedance probability is (n - k + 1) / (n + 1) = 1.20 %
  at n = 250. Regime oracle: conditional on the realised state of the day.
* Power rows: normal on t data; static models (normal, HS) on GARCH and regime
  data. They are NOT size-adjusted: compare them with the reference size of the
  same T and test.
* Monte-Carlo error: every rate r carries ``se = sqrt(r (1 - r) / M)``.
* The Student-t model is refitted every ``t_refit_every`` days (default 50;
  maximum likelihood is the expensive step); all other models daily. A t fit
  that does not converge raises; such replications are counted (``failed_reps``)
  and left out of the rates.
* Traffic light: the zone of the last 250 forecast days (BCBS 1996).

Run from ``UFOS.ai/RiskLab/``::

    python studies/var_backtest_power.py                 # default: M = 100 (a demo), about 1-1.5 minutes
    python studies/var_backtest_power.py --mode full     # M = 1000, about 8-15 minutes
    python studies/var_backtest_power.py --m 200 --out-dir out/my_run

Output (``out/var_backtest_power/`` unless ``--out-dir``): ``results.csv``,
``results.json`` and ``summary.md``. No plotting dependency. The full-run
summary is kept in ``studies/results/var_backtest_power_full_summary.md``.
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
from scipy import stats

import risklab
from risklab.backtest import (REJECT, binomial_exact, christoffersen_cc, christoffersen_ind, exact_size,
                              kupiec_pof, traffic_light)
from risklab.data import garch11, iid_normal, iid_student_t, regime_switching, true_var_es
from risklab.engine import walk_forward
from risklab.models import FitError, HistoricalSimulation, NormalModel, StudentTModel
from risklab.models.historical import exceedance_probability
from risklab.report.export import default_out_dir, table_to_csv, table_to_json, text_to_file

STUDY = "var_backtest_power"

GENERATORS: Dict[str, Callable] = {
    "iid_normal": lambda n, seed: iid_normal(n, 0.0, 0.01, seed),
    "iid_t5": lambda n, seed: iid_student_t(n, 0.0, 0.01, 5.0, seed),
    "garch_normal": lambda n, seed: garch11(n, 2e-6, 0.08, 0.90, seed=seed),
    "regime": lambda n, seed: regime_switching(n, (0.007, 0.02), ((0.98, 0.02), (0.05, 0.95)), seed=seed),
}

MODELS: Dict[str, Callable] = {
    "normal": NormalModel,
    "hs": HistoricalSimulation,
    "student_t": StudentTModel,
}

#: (generator, model, role). "oracle" = the true conditional VaR.
CASES: List[Tuple[str, str, str]] = [
    ("iid_normal", "oracle", "size"),
    ("iid_t5", "oracle", "size"),
    ("garch_normal", "oracle", "size"),
    ("regime", "oracle", "size"),
    ("iid_normal", "normal", "size"),
    ("iid_normal", "hs", "size"),
    ("iid_t5", "student_t", "size"),
    ("iid_t5", "normal", "power"),
    ("garch_normal", "normal", "power"),
    ("garch_normal", "hs", "power"),
    ("regime", "normal", "power"),
    ("regime", "hs", "power"),
]

MODES = {"smoke": 2, "default": 100, "full": 1000}

TESTS = ("kupiec", "binom", "ind", "cc")

COLUMNS = (["generator", "model", "role", "T", "m", "m_ok", "failed_reps", "exceed_rate"]
           + [c for t in TESTS for c in (f"reject_{t}", f"se_{t}")]
           + ["exact_size_kupiec", "exact_size_binom", "ref_size_ind", "ref_size_cc",
              "ind_no_info", "tl_green", "tl_yellow", "tl_red", "var_rel_error", "size_adjusted", "note"])


@dataclass
class StudyConfig:
    m: int = MODES["default"]
    horizons: Tuple[int, ...] = (250, 1000)
    window: int = 250
    alpha: float = 0.99
    reject_at: float = 0.05
    seed: int = 20261105
    t_refit_every: int = 50
    cases: Sequence[Tuple[str, str, str]] = field(default_factory=lambda: list(CASES))


def data_seed(base: int, gen_index: int, horizon: int, rep: int) -> int:
    """Deterministic per-replication seed; all models of a replication see the same data."""
    return int(np.random.SeedSequence([base, gen_index, horizon, rep]).generate_state(1)[0])


def mc_se(rate: Optional[float], m: int) -> Optional[float]:
    """Monte-Carlo standard error of a rejection rate: sqrt(r (1 - r) / M)."""
    if rate is None or m < 1:
        return None
    return math.sqrt(rate * (1.0 - rate) / m)


def _note(gen: str, model: str, role: str, cfg: StudyConfig) -> str:
    if role == "power":
        return "not size-adjusted"
    if model == "oracle":
        return "truth conditional on the realised state (oracle)" if gen == "regime" else "true VaR"
    if model == "hs":
        p = exceedance_probability(cfg.window, cfg.alpha)
        return f"not a clean size row: HS exceedance probability (n-k+1)/(n+1) = {100 * p:.2f} %"
    if model == "student_t":
        return f"estimation risk; t refit every {cfg.t_refit_every} days"
    return "estimation risk"


def _exceedances(series, model: str, cfg: StudyConfig):
    """(exceedance flags, VaR forecasts, true VaR) for the T forecast days."""
    w = cfg.window
    truth = true_var_es(series, [cfg.alpha]).var[cfg.alpha][w:]
    losses = series.losses[w:]
    if model == "oracle":
        return losses > truth, truth, truth
    refit = cfg.t_refit_every if model == "student_t" else 1
    frame = walk_forward(series.losses, MODELS[model], w, [cfg.alpha], refit_every=refit)
    return frame.exceedance[cfg.alpha], frame.var[cfg.alpha], truth


def _no_info(ind) -> bool:
    """The independence test had no information: no exceedance, or only exceedances, on days 2..T."""
    d = ind.details
    return d["n01"] + d["n11"] == 0 or d["n00"] + d["n10"] == 0


def run_study(cfg: StudyConfig, progress: Optional[Callable[[str], None]] = None) -> List[dict]:
    if cfg.m < 1:
        raise ValueError("m must be >= 1")
    gen_names = list(GENERATORS)
    sizes = {h: (exact_size("kupiec_pof", h, cfg.alpha, cfg.reject_at)["size"],
                 exact_size("binomial_exact", h, cfg.alpha, cfg.reject_at)["size"]) for h in cfg.horizons}
    rows = []
    for gen in gen_names:
        models = [(m, role) for g, m, role in cfg.cases if g == gen]
        if not models:
            continue
        for horizon in cfg.horizons:
            acc = {m: {"exceed": [], "kupiec": 0, "binom": 0, "ind": 0, "cc": 0, "no_info": 0, "failed": 0,
                       "green": 0, "yellow": 0, "red": 0, "rel_err": []} for m, _ in models}
            for rep in range(cfg.m):
                series = GENERATORS[gen](cfg.window + horizon, data_seed(cfg.seed, gen_names.index(gen), horizon, rep))
                for model, _ in models:
                    a = acc[model]
                    try:
                        exc, var_hat, var_true = _exceedances(series, model, cfg)
                    except FitError:
                        a["failed"] += 1
                        continue
                    a["exceed"].append(float(np.mean(exc)))
                    a["kupiec"] += kupiec_pof(exc, cfg.alpha, cfg.reject_at).decision == REJECT
                    a["binom"] += binomial_exact(exc, cfg.alpha, cfg.reject_at).decision == REJECT
                    ind = christoffersen_ind(exc, cfg.alpha, cfg.reject_at)
                    a["ind"] += ind.decision == REJECT
                    a["no_info"] += _no_info(ind)
                    a["cc"] += christoffersen_cc(exc, cfg.alpha, cfg.reject_at).decision == REJECT
                    a[traffic_light(exc, cfg.alpha, min(250, horizon)).decision] += 1
                    a["rel_err"].append(float(np.mean(np.abs(var_hat - var_true) / np.abs(var_true))))
            for model, role in models:
                a = acc[model]
                m_ok = cfg.m - a["failed"]
                rate = (lambda k: a[k] / m_ok if m_ok else None)
                row = {"generator": gen, "model": model, "role": role, "T": horizon, "m": cfg.m, "m_ok": m_ok,
                       "failed_reps": a["failed"],
                       "exceed_rate": float(np.mean(a["exceed"])) if m_ok else None}
                for t in TESTS:
                    row[f"reject_{t}"] = rate(t)
                    row[f"se_{t}"] = mc_se(rate(t), m_ok)
                row.update({
                    "exact_size_kupiec": sizes[horizon][0], "exact_size_binom": sizes[horizon][1],
                    "ref_size_ind": None, "ref_size_cc": None,
                    "ind_no_info": rate("no_info"), "tl_green": rate("green"), "tl_yellow": rate("yellow"),
                    "tl_red": rate("red"), "var_rel_error": float(np.mean(a["rel_err"])) if m_ok else None,
                    "size_adjusted": False, "note": _note(gen, model, role, cfg),
                })
                rows.append(row)
            if progress:
                progress(f"{gen} T={horizon}: done")
    # ind / cc reference size: the oracle row of the same data and T (empirical)
    oracle = {(r["generator"], r["T"]): r for r in rows if r["model"] == "oracle"}
    for r in rows:
        ref = oracle.get((r["generator"], r["T"]))
        if ref is not None:
            r["ref_size_ind"], r["ref_size_cc"] = ref["reject_ind"], ref["reject_cc"]
    order = {c: i for i, c in enumerate((g, m) for g, m, _ in cfg.cases)}
    rows.sort(key=lambda r: (r["role"] != "size", order[(r["generator"], r["model"])], r["T"]))
    return rows


def _pct(x: Optional[float], digits: int = 1) -> str:
    return "n/a" if x is None else f"{100.0 * x:.{digits}f} %"


def _rate(r: dict, t: str) -> str:
    v, se = r[f"reject_{t}"], r[f"se_{t}"]
    return "n/a" if v is None else f"{100.0 * v:.1f} ± {100.0 * se:.1f}"


def _set_text(xs: List[int], n: int) -> str:
    """Compact rejection set, e.g. "{0} ∪ {7..250}"."""
    if not xs:
        return "∅"
    parts, start, prev = [], xs[0], xs[0]
    for x in xs[1:] + [None]:
        if x is not None and x == prev + 1:
            prev = x
            continue
        parts.append(f"{{{start}}}" if start == prev else f"{{{start}..{prev}}}")
        if x is not None:
            start = prev = x
    return " ∪ ".join(parts)


def summary_markdown(rows: List[dict], cfg: StudyConfig) -> str:
    se5 = mc_se(0.05, cfg.m)
    p = 1.0 - cfg.alpha
    lines = [
        "# VaR backtest size and power (synthetic data)",
        "",
        f"alpha = {cfg.alpha}, estimation window = {cfg.window} days, tests at {100 * cfg.reject_at:g} %, "
        f"M = {cfg.m} replications per row, seed = {cfg.seed}, Student-t refit every {cfg.t_refit_every} days "
        f"(other models daily). risklab {risklab.__version__}, numpy {np.__version__}, scipy {scipy.__version__}.",
        "",
        f"**Monte-Carlo error.** Every rate is shown as `rate ± se` with se = sqrt(r (1 - r) / M); "
        f"at M = {cfg.m} a rate near 5 % has se ≈ ±{100 * se5:.1f} percentage points.",
    ]
    if cfg.m < MODES["full"]:
        lines.append(f"M = {cfg.m} is a demo; use `--mode full` (M = {MODES['full']}, se ≈ "
                     f"±{100 * mc_se(0.05, MODES['full']):.1f} points at 5 %) for conclusions.")
    lines += ["", "## Reference size under H0", "",
              "| T | Kupiec exact size | Kupiec rejects x in | binomial exact size | binomial rejects x in |",
              "|---|---|---|---|---|"]
    for h in cfg.horizons:
        k = exact_size("kupiec_pof", h, cfg.alpha, cfg.reject_at)
        b = exact_size("binomial_exact", h, cfg.alpha, cfg.reject_at)
        lines.append(f"| {h} | {_pct(k['size'], 2)} | {_set_text(k['reject_counts'], h)} | "
                     f"{_pct(b['size'], 2)} | {_set_text(b['reject_counts'], h)} |")
    t0 = min(cfg.horizons)
    lr0 = -2.0 * t0 * math.log(1.0 - p)
    lines += [
        "",
        f"Kupiec is asymptotic (chi-square). At T = {t0} it rejects x = 0: LR = -2 T ln(1 - p) = {lr0:.2f} "
        f"> 3.84, p = {stats.chi2.sf(lr0, 1):.3f}, and P(X = 0) = {(1 - p) ** t0:.3f} under H0, so its exact "
        f"size is far above 5 %. The exact binomial test keeps the size at or below 5 % by construction "
        f"(it cannot reject x = 0 at T = {t0}: P(X = 0) alone exceeds 5 %).",
        "For ind and cc there is no closed form: their reference is the empirical rate of the oracle row of "
        "the same data and T (`ref_size_ind`, `ref_size_cc` in the CSV).",
        "",
        "## Rejection rates (%, ± Monte-Carlo se)",
        "",
        "| role | data | model | T | exceed rate | Kupiec | binomial exact | ind | cc | ind no-info "
        "| TL green / yellow / red | mean abs rel VaR error | note |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        failed = f" ({r['failed_reps']} failed fits)" if r["failed_reps"] else ""
        lines.append(
            f"| {r['role']} | {r['generator']} | {r['model']} | {r['T']} | {_pct(r['exceed_rate'], 2)} | "
            f"{_rate(r, 'kupiec')} | {_rate(r, 'binom')} | {_rate(r, 'ind')} | {_rate(r, 'cc')} | "
            f"{_pct(r['ind_no_info'])} | {_pct(r['tl_green'])} / {_pct(r['tl_yellow'])} / {_pct(r['tl_red'])} | "
            f"{_pct(r['var_rel_error'])} | {r['note']}{failed} |")
    lines += ["", "## Reading the table", ""]
    ex = next((r for r in rows if (r["generator"], r["model"], r["T"]) == ("iid_t5", "normal", t0)), None)
    if ex is not None and ex["reject_kupiec"] is not None:
        gap = ex["reject_kupiec"] - ex["exact_size_kupiec"]
        verdict = ("practically no power (within 2 se of the size)" if gap < 2 * ex["se_kupiec"]
                   else f"{100 * gap:.1f} points above the size")
        lines.append(
            f"- Power rows are not size-adjusted. Compare each with the reference size of the same T and test: "
            f"e.g. normal on iid_t5 at T = {t0} is rejected by Kupiec in {_rate(ex, 'kupiec')} % of the runs, "
            f"against an exact size of {_pct(ex['exact_size_kupiec'], 1)}: {verdict}.")
    hs = exceedance_probability(cfg.window, cfg.alpha)
    lines += [
        f"- HS on iid data is not a clean size row: with k = ceil(n alpha) the true exceedance probability "
        f"is (n - k + 1) / (n + 1) = {100 * hs:.2f} % at n = {cfg.window}, not {100 * p:g} %.",
        "- The regime VaR error is measured against the state-conditional oracle (the true VaR given the "
        "realised state of the day), which no model can reach from past data alone.",
        "- \"ind no-info\": share of runs in which the independence test had no information (no exceedance, "
        "or only exceedances, on days 2..T); its statistic is then 0 and it counts as not rejected.",
        "- \"mean abs rel VaR error\": mean over days of |VaR forecast - true VaR| / true VaR.",
    ]
    return "\n".join(lines) + "\n"


def write_outputs(rows: List[dict], cfg: StudyConfig, out_dir: Path) -> List[Path]:
    meta = {"study": STUDY, "config": {k: (list(v) if isinstance(v, tuple) else v)
                                       for k, v in asdict(cfg).items() if k != "cases"},
            "cases": [list(c) for c in cfg.cases], "scipy_version": scipy.__version__}
    out_dir = Path(out_dir)
    return [
        table_to_csv(rows, COLUMNS, out_dir / "results.csv", cfg.seed, meta),
        table_to_json(rows, out_dir / "results.json", cfg.seed, meta),
        text_to_file(summary_markdown(rows, cfg), out_dir / "summary.md"),
    ]


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mode", choices=sorted(MODES), default="default",
                    help="smoke (M=2, T=250 only), default (M=100, a demo) or full (M=1000)")
    ap.add_argument("--m", type=int, default=None, help="replications per row (overrides --mode)")
    ap.add_argument("--seed", type=int, default=StudyConfig.seed)
    ap.add_argument("--out-dir", default=None, help="default: RiskLab/out/var_backtest_power")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    cfg = StudyConfig(m=args.m if args.m is not None else MODES[args.mode], seed=args.seed)
    if args.mode == "smoke":
        cfg.horizons = (250,)
    out_dir = Path(args.out_dir) if args.out_dir else default_out_dir() / STUDY
    log = None if args.quiet else (lambda msg: print(msg, flush=True))
    start = time.perf_counter()
    rows = run_study(cfg, log)
    paths = write_outputs(rows, cfg, out_dir)
    if log:
        log(f"{len(rows)} rows in {time.perf_counter() - start:.1f} s -> " + ", ".join(str(p) for p in paths))
    return 0


if __name__ == "__main__":
    sys.exit(main())
