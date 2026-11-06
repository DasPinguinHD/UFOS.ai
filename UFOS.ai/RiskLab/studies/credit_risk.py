"""Credit risk study: PD model quality, calibration tests under correlated defaults and a macro shift, IFRS 9 ECL
sensitivity, and Basel IRB capital against the simulated one-factor loss distribution.

Synthetic data only (:mod:`risklab.credit.data`: known true PD model, one-factor Gaussian copula,
asset correlation 0.12). Four parts:

A. **PD model quality vs sample size.** A logistic model with the true specification is fitted
   on N borrower-years (N = 500 ... 50 000; 5 training periods) and validated out of time on 3
   later periods of 10 000 borrowers: AUC (against the AUC of the true PD on the same data),
   coefficient error, the spread of the macro coefficient over the replications against the mean
   MLE standard error, Brier score, Hosmer-Lemeshow rejection (out of time: chi-square with g
   degrees of freedom, not g - 2); failed fits (separation / non-convergence) are counted, never
   hidden, and EXCLUDED from the averages, which are therefore conditional on a successful fit.
B. **Calibration tests under correlated defaults and a macro shift.** (i) The size of the
   Hosmer-Lemeshow (g degrees of freedom: the true PD is not fitted on the sample), binomial and
   Jeffreys tests on ONE period scored with the TRUE PD, with asset correlation 0.12 and 0 -
   correlation makes the tests reject correct PDs far more often than their level, and the
   per-grade tests (any of about 8 grades) are a multiple test; (ii) a downturn period (macro
   variable at -2 sd) scored by the full model and by a model without the macro variable
   ("through the cycle"): predicted (mean and median over the replications) vs realised default
   rate.
C. **IFRS 9 ECL sensitivity** of a synthetic loan book (2000 loans) to the downside-scenario
   weight and the SICR threshold (relative PD increase), and the stage-2 cliff of one loan.
D. **IRB capital vs simulated losses** (M = 1 year so the maturity adjustment is 1): the 99.9 %
   loss quantile minus the expected loss against the sum of K for a granular and a concentrated
   portfolio, and the gap as a function of the number of names.

Run from ``UFOS.ai/RiskLab/``::

    python studies/credit_risk.py                 # default: M = 20 replications, 200 000 loss draws
    python studies/credit_risk.py --mode full     # M = 200, 2 000 000 draws

Measured run times in the build container: smoke 2 s, default 12 s, full 42 s.
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
from risklab.credit import data as D
from risklab.credit import ifrs9 as F
from risklab.credit import irb as I
from risklab.credit import pd as P
from risklab.models.base import FitError
from risklab.report.export import (code_fingerprint, default_out_dir, save_full_results, table_to_csv,
                                   table_to_json, text_to_file)

STUDY = "credit_risk"
MODES = {"smoke": (2, 20_000), "default": (20, 200_000), "full": (200, 2_000_000)}
COLUMNS = ["table", "n_train", "m", "fit_failures", "auc", "auc_se", "auc_true", "auc_gap", "auc_gap_se",
           "coef_rmse", "coef_rmse_se", "brier", "hl_reject", "hl_reject_se", "rho", "test", "reject", "reject_se",
           "model", "pred_pd", "pred_pd_se", "realised_dr", "realised_dr_se", "downside_weight", "sicr_multiple",
           "ecl", "coverage", "stage2_share", "pd_now", "stage", "ecl_loan", "portfolio", "names", "k_sum", "el",
           "q999", "q999_lo", "q999_hi", "unexpected", "ratio", "name", "true", "estimate", "se", "z", "share",
           "captured", "gini", "macro_coef_mean", "macro_coef_sd", "macro_se_mean", "pred_pd_median"]


@dataclass
class CreditConfig:
    m: int = MODES["default"][0]
    n_sims: int = MODES["default"][1]
    seed: int = 20261111
    n_train: Tuple[int, ...] = (500, 2000, 10000, 50000)
    train_periods: int = 5
    test_n: int = 10000
    test_periods: int = 3
    calib_n: int = 20000
    downturn_macro: float = -2.0
    downside_weights: Tuple[float, ...] = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6)
    sicr_multiples: Tuple[float, ...] = (1.5, 2.0, 2.5, 3.0, 4.0, 5.0)
    n_loans: int = 2000
    names_curve: Tuple[int, ...] = (10, 30, 100, 300, 1000, 10000)


def _seed(cfg: CreditConfig, *key: int) -> int:
    return int(np.random.SeedSequence([cfg.seed, *key]).generate_state(1)[0])


def _ms(x: Sequence[float]) -> Tuple[Optional[float], Optional[float]]:
    a = np.asarray([v for v in x if v is not None], dtype=np.float64)
    if a.size == 0:
        return None, None
    return float(a.mean()), (float(a.std(ddof=1) / math.sqrt(a.size)) if a.size > 1 else None)


def _rate(flags: Sequence[bool]) -> Tuple[float, float]:
    n = len(flags)
    p = float(np.mean(flags))
    return p, math.sqrt(p * (1 - p) / n)


TRUE_VEC = np.array([D.TRUE_COEF["const"]] + [D.TRUE_COEF[f] for f in D.FEATURES])
MACRO = 1 + list(D.FEATURES).index("macro")      # index of the macro coefficient in the fitted vector


def part_a(cfg: CreditConfig, progress=None) -> List[dict]:
    rows = []
    for j, N in enumerate(cfg.n_train):
        aucs, gaps, rm, br, hl, fails = [], [], [], [], [], 0
        mc, ms = [], []
        for r in range(cfg.m):
            n = max(1, N // cfg.train_periods)
            tr = D.generate_panel(n, cfg.train_periods, _seed(cfg, 1, j, r, 0))
            te = D.generate_panel(cfg.test_n, cfg.test_periods, _seed(cfg, 1, j, r, 1))
            try:
                fit = P.fit_logit(tr.X, tr.y)
            except FitError:
                fails += 1
                continue
            p = fit.predict(te.X)
            a, at = P.auc(p, te.y), P.auc(te.pd_true, te.y)
            aucs.append(a)
            gaps.append(at - a)
            rm.append(float(np.sqrt(np.mean((fit.coef - TRUE_VEC) ** 2))))
            br.append(P.brier(p, te.y))
            hl.append(P.hosmer_lemeshow(p, te.y, fitted_on_sample=False)["p_value"] < 0.05)   # out of time
            mc.append(float(fit.coef[MACRO]))
            ms.append(float(fit.se[MACRO]))
        a_m, a_se = _ms(aucs)
        g_m, g_se = _ms(gaps)
        r_m, r_se = _ms(rm)
        h_p, h_se = _rate(hl) if hl else (None, None)
        rows.append({"table": "sample_size", "n_train": N, "m": cfg.m, "fit_failures": fails, "auc": a_m, "auc_se": a_se,
                     "auc_true": (a_m + g_m) if a_m is not None else None, "auc_gap": g_m, "auc_gap_se": g_se,
                     "coef_rmse": r_m, "coef_rmse_se": r_se, "brier": _ms(br)[0], "hl_reject": h_p, "hl_reject_se": h_se,
                     "macro_coef_mean": _ms(mc)[0], "macro_coef_sd": float(np.std(mc, ddof=1)) if len(mc) > 1 else None,
                     "macro_se_mean": _ms(ms)[0]})
        if progress:
            progress(f"A: N = {N} done")
    return rows


def part_a_example(cfg: CreditConfig) -> List[dict]:
    """One fit on the largest training sample: coefficients vs the truth, and the CAP curves out of time."""
    N = max(cfg.n_train)
    tr = D.generate_panel(N // cfg.train_periods, cfg.train_periods, _seed(cfg, 6, 0))
    te = D.generate_panel(cfg.test_n, cfg.test_periods, _seed(cfg, 6, 1))
    fit = P.fit_logit(tr.X, tr.y)
    names = ["const"] + list(D.FEATURES)
    rows = [{"table": "coef", "name": n, "true": float(t), "estimate": float(e), "se": float(se),
             "z": float((e - t) / se)} for n, t, e, se in zip(names, TRUE_VEC, fit.coef, fit.se)]
    p = fit.predict(te.X)
    grid = np.linspace(0, 1, 51)
    for label, score in (("model", p), ("true_pd", te.pd_true)):
        x, c = P.cap_curve(score, te.y)
        cap = np.interp(grid, x, c)
        rows += [{"table": "cap", "model": label, "share": float(g), "captured": float(v)} for g, v in zip(grid, cap)]
    dr = float(te.y.mean())
    rows += [{"table": "cap", "model": "perfect", "share": float(g), "captured": float(min(1.0, g / dr))} for g in grid]
    rows.append({"table": "cap_summary", "n_train": N, "auc": P.auc(p, te.y), "auc_true": P.auc(te.pd_true, te.y),
                 "gini": P.gini(p, te.y), "realised_dr": dr})
    return rows


def part_b(cfg: CreditConfig, progress=None) -> List[dict]:
    rows = []
    # (i) size of the calibration tests with the TRUE PD, one period, rho = 0.12 vs 0
    for i, rho in enumerate((D.RHO, 0.0)):
        flags = {"hosmer_lemeshow": [], "binomial_any_grade": [], "jeffreys_any_grade": []}
        for r in range(cfg.m):
            pn = D.generate_panel(cfg.calib_n, 1, _seed(cfg, 2, i, r), rho=rho)
            flags["hosmer_lemeshow"].append(P.hosmer_lemeshow(pn.pd_true, pn.y, fitted_on_sample=False)["p_value"] < 0.05)
            g = P.grade_table(pn.pd_true, pn.y)
            flags["binomial_any_grade"].append(any(x["binomial_p"] < 0.05 for x in g if x["n"] >= 50))
            flags["jeffreys_any_grade"].append(any(x["jeffreys_p"] < 0.05 for x in g if x["n"] >= 50))
        for t, f in flags.items():
            p, se = _rate(f)
            rows.append({"table": "calibration_size", "rho": rho, "test": t, "m": cfg.m, "reject": p, "reject_se": se})
    # (ii) downturn: full model vs a model without the macro variable
    res = {"full": ([], []), "ttc": ([], [])}
    for r in range(cfg.m):
        tr = D.generate_panel(4000, 8, _seed(cfg, 3, r, 0))
        te = D.generate_panel(cfg.calib_n, 1, _seed(cfg, 3, r, 1), macro_path=[cfg.downturn_macro])
        for name, cols in (("full", slice(None)), ("ttc", slice(0, len(D.FEATURES) - 1))):
            fit = P.fit_logit(tr.X[:, cols], tr.y)
            res[name][0].append(float(fit.predict(te.X[:, cols]).mean()))
            res[name][1].append(float(te.y.mean()))
    true_mean = []
    for r in range(cfg.m):
        te = D.generate_panel(cfg.calib_n, 1, _seed(cfg, 3, r, 1), macro_path=[cfg.downturn_macro])
        true_mean.append(float(te.pd_true.mean()))
    for name, (pp, dr) in res.items():
        rows.append({"table": "downturn", "model": name, "m": cfg.m, "pred_pd": _ms(pp)[0], "pred_pd_se": _ms(pp)[1],
                     "pred_pd_median": float(np.median(pp)), "realised_dr": _ms(dr)[0], "realised_dr_se": _ms(dr)[1]})
    rows.append({"table": "downturn", "model": "true_pd", "m": cfg.m, "pred_pd": _ms(true_mean)[0],
                 "pred_pd_se": _ms(true_mean)[1], "pred_pd_median": float(np.median(true_mean)),
                 "realised_dr": _ms(res["full"][1])[0],
                 "realised_dr_se": _ms(res["full"][1])[1]})
    if progress:
        progress("B: done")
    return rows


def loan_book(cfg: CreditConfig) -> List[F.Loan]:
    rng = make_rng(_seed(cfg, 4))
    loans = []
    for _ in range(cfg.n_loans):
        pd0 = float(np.clip(np.exp(rng.normal(np.log(0.01), 0.8)), 0.0005, 0.2))
        pdn = float(np.clip(pd0 * np.exp(rng.normal(0.1, 0.6)), 0.0003, 0.5))
        u = rng.random()
        dpd = 90 if u < 0.01 else (30 if u < 0.03 else 0)
        loans.append(F.Loan(ead=float(rng.uniform(0.05, 2.0)), eir=float(rng.uniform(0.03, 0.07)),
                            maturity=int(rng.integers(1, 11)), lgd=float(rng.choice([0.25, 0.45])),
                            pd_now=pdn, pd_orig=pd0, dpd=dpd, amortising=bool(rng.random() < 0.7)))
    return loans


def part_c(cfg: CreditConfig) -> List[dict]:
    loans = loan_book(cfg)
    rows = []
    for wd in cfg.downside_weights:
        sc = (F.Scenario("upside", 0.2, (0.8, 0.9)), F.Scenario("base", round(0.8 - wd, 10), ()),
              F.Scenario("downside", wd, (1.8, 1.5, 1.2)))
        for mult in cfg.sicr_multiples:
            t = F.portfolio_ecl(loans, sc, F.StagingRules(sicr_multiple=mult))
            rows.append({"table": "ecl_grid", "downside_weight": wd, "sicr_multiple": mult, "ecl": t["ecl"],
                         "coverage": t["coverage"], "stage2_share": t["stage2"] / len(loans)})
    # the stage-2 cliff of one loan: 5-year amortising loan, PD at origination 1 %
    for pdn in np.round(np.linspace(0.005, 0.06, 23), 4):
        ln = F.Loan(ead=1.0, eir=0.05, maturity=5, lgd=0.45, pd_now=float(pdn), pd_orig=0.01)
        e = F.ecl(ln)
        rows.append({"table": "cliff", "pd_now": float(pdn), "stage": e["stage"], "ecl_loan": e["ecl"]})
    return rows


def _granular() -> List[I.Bucket]:
    pds = (0.001, 0.0025, 0.005, 0.0075, 0.01, 0.015, 0.02, 0.03, 0.04, 0.05)
    return [I.Bucket(1000, 1.0, 0.45, p) for p in pds]


def _concentrated() -> List[I.Bucket]:
    """The same total EAD (10 000) and PD mix in 30 names: 10 large (700) and 20 small (150)."""
    pds = (0.001, 0.0025, 0.005, 0.0075, 0.01, 0.015, 0.02, 0.03, 0.04, 0.05)
    return [I.Bucket(1, 700.0, 0.45, p) for p in pds] + [I.Bucket(2, 150.0, 0.45, p) for p in pds]


def part_d(cfg: CreditConfig, progress=None) -> List[dict]:
    rows = []
    cases = [("granular", _granular()), ("concentrated", _concentrated())]
    cases += [(f"homogeneous_{n}", [I.Bucket(n, 10000.0 / n, 0.45, 0.01)]) for n in cfg.names_curve]
    for i, (name, b) in enumerate(cases):
        L = I.simulate_losses(b, cfg.n_sims, _seed(cfg, 5, i))
        q, lo, hi = I.quantile_ci(L)
        el, k = I.expected_loss(b), I.portfolio_k(b, m=1.0, pd_floor=0.0)
        rows.append({"table": "irb", "portfolio": name, "names": int(sum(x.n for x in b)), "k_sum": k, "el": el,
                     "q999": q, "q999_lo": lo, "q999_hi": hi, "unexpected": q - el, "ratio": (q - el) / k})
    if progress:
        progress("D: done")
    return rows


def run_study(cfg: CreditConfig, progress: Optional[Callable[[str], None]] = None) -> List[dict]:
    if cfg.m < 2:
        raise ValueError("m must be >= 2")
    return (part_a(cfg, progress) + part_a_example(cfg) + part_b(cfg, progress) + part_c(cfg)
            + part_d(cfg, progress))


def meta_extra() -> dict:
    return {"true_coef": D.TRUE_COEF, "features": list(D.FEATURES), "rho": D.RHO, "master_scale": list(P.MASTER_SCALE),
            "irb_sources": I.IRB_SOURCES, "ifrs9_sources": F.IFRS9_SOURCES,
            "default_scenarios": [asdict(s) for s in F.DEFAULT_SCENARIOS],
            "jeffreys_source": "ECB instructions for reporting the validation results of IRB models (2019), as recalled "
                               "- ASSUMPTION; formula: Jeffreys-prior posterior"}


def _f3(x, fmt: str = ".3f") -> str:
    return "n/a" if x is None else format(x, fmt)


def _p(x, d=1):
    return "n/a" if x is None else f"{100 * x:.{d}f}"


def _r(p, se, n) -> str:
    """A rejection rate in % with its se; exactly 0 or 1 shows the 95 % rule-of-three bound (3 / n)."""
    if p is None:
        return "n/a"
    if p == 0.0:
        return f"0 (≤ {300.0 / n:.2g}) %"
    if p == 1.0:
        return f"100 (≥ {100.0 - 300.0 / n:.4g}) %"
    return f"{100 * p:.1f} ± {100 * se:.1f} %"


def summary_markdown(rows: List[dict], cfg: CreditConfig) -> str:
    L = ["# Credit risk study (synthetic data)", "",
         f"M = {cfg.m} replications, {cfg.n_sims} loss draws, seed {cfg.seed}. True PD model and asset correlation "
         f"{D.RHO} known. risklab {risklab.__version__}, numpy {np.__version__}. No real borrowers or data.", "",
         "## A. PD model quality vs sample size (out of time)", "",
         "Failed fits are excluded: every average is conditional on a successful fit. Hosmer-Lemeshow out of time "
         "uses chi-square with g = 10 degrees of freedom. Macro coefficient: mean and spread (sd) of the estimate "
         "over the successful fits against the mean MLE standard error.", "",
         "| N train | failed fits | AUC | AUC gap to true PD | coef RMSE | Brier | HL rejects (5 %) | macro coef (true "
         f"{D.TRUE_COEF['macro']:g}): mean / sd / mean MLE se |",
         "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if r["table"] == "sample_size":
            L.append(f"| {r['n_train']} | {r['fit_failures']} | {r['auc']:.3f} ± {r['auc_se']:.3f} | "
                     f"{r['auc_gap']:+.3f} ± {r['auc_gap_se']:.3f} | {r['coef_rmse']:.3f} ± {r['coef_rmse_se']:.3f} | "
                     f"{r['brier']:.4f} | {_r(r['hl_reject'], r['hl_reject_se'], r['m'] - r['fit_failures'])} | "
                     f"{_f3(r['macro_coef_mean'], '+.3f')} / {_f3(r['macro_coef_sd'])} / {_f3(r['macro_se_mean'])} |")
    L += ["", "## B. Calibration tests on the TRUE PD, one period (rejection rate at 5 %)", "",
          "The two per-grade tests reject if ANY grade with >= 50 borrowers has p < 5 % (about 8 grades: "
          "multiple testing, so even with independent defaults the rate is well above 5 %).", "",
          "| asset correlation | test | rejects |", "|---|---|---|"]
    for r in rows:
        if r["table"] == "calibration_size":
            L.append(f"| {r['rho']:g} | {r['test']} | {_r(r['reject'], r['reject_se'], r['m'])} |")
    L += ["", f"## B. Downturn period (macro = {cfg.downturn_macro:g} sd)", "",
          "| model | mean predicted PD | median predicted PD | realised default rate |", "|---|---|---|---|"]
    for r in rows:
        if r["table"] == "downturn":
            L.append(f"| {r['model']} | {_p(r['pred_pd'], 2)} ± {_p(r['pred_pd_se'], 2)} % | {_p(r['pred_pd_median'], 2)} % | "
                     f"{_p(r['realised_dr'], 2)} ± {_p(r['realised_dr_se'], 2)} % |")
    L += ["", "## C. IFRS 9 ECL coverage (% of EAD) by downside weight (rows) and SICR multiple (columns)", "",
          "| downside weight | " + " | ".join(f"x{m:g}" for m in cfg.sicr_multiples) + " |",
          "|---|" + "---|" * len(cfg.sicr_multiples)]
    g = {(r["downside_weight"], r["sicr_multiple"]): r for r in rows if r["table"] == "ecl_grid"}
    for w in cfg.downside_weights:
        L.append(f"| {w:g} | " + " | ".join(f"{_p(g[(w, m)]['coverage'], 2)} ({_p(g[(w, m)]['stage2_share'], 0)} % st. 2)"
                                           for m in cfg.sicr_multiples) + " |")
    L += ["", "## D. IRB capital vs simulated 99.9 % loss (M = 1 year)", "",
          "| portfolio | names | sum K | EL | q99.9 (95 % CI) | q99.9 - EL | ratio to sum K |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        if r["table"] == "irb":
            L.append(f"| {r['portfolio']} | {r['names']} | {r['k_sum']:.1f} | {r['el']:.1f} | {r['q999']:.1f} "
                     f"({r['q999_lo']:.1f}-{r['q999_hi']:.1f}) | {r['unexpected']:.1f} | {r['ratio']:.3f} |")
    return "\n".join(L) + "\n"


def write_outputs(rows: List[dict], cfg: CreditConfig, out_dir: Path) -> List[Path]:
    meta = {"study": STUDY, "code_sha256": code_fingerprint(__file__),
            "config": {k: (list(v) if isinstance(v, tuple) else v) for k, v in asdict(cfg).items()}, **meta_extra()}
    out_dir = Path(out_dir)
    return [table_to_csv(rows, COLUMNS, out_dir / "results.csv", cfg.seed, meta),
            table_to_json(rows, out_dir / "results.json", cfg.seed, meta),
            text_to_file(summary_markdown(rows, cfg), out_dir / "summary.md")]


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mode", choices=sorted(MODES), default="default",
                    help="smoke (M=2, 20 000 draws), default (M=20, 200 000) or full (M=200, 2 000 000)")
    ap.add_argument("--m", type=int, default=None)
    ap.add_argument("--seed", type=int, default=CreditConfig.seed)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--save-results", action="store_true",
                    help="with --mode full: also save results.json / summary.md as studies/results/"
                         f"{STUDY}_full_*.json / .md (the inputs of the report)")
    args = ap.parse_args(argv)
    if args.save_results and (args.mode != "full" or args.m is not None or args.seed != ap.get_default("seed")):
        ap.error("--save-results needs --mode full with the default M and seed")
    m, sims = MODES[args.mode]
    cfg = CreditConfig(m=args.m if args.m is not None else m, n_sims=sims, seed=args.seed)
    if args.mode == "smoke":
        cfg.n_train, cfg.test_n, cfg.calib_n, cfg.n_loans = (2000, 10000), 3000, 5000, 200
        cfg.names_curve = (10, 1000)
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
