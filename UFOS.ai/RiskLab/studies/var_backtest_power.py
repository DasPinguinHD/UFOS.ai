"""Size and power of the VaR backtests on synthetic data (Risk Lab showcase study).

Question: how often do Kupiec POF, the exact binomial test, Christoffersen
independence (ind) and conditional coverage (cc) - all on the 99 % VaR - and
the Acerbi-Szekely Z2 test of the 97.5 % ES reject
* SIZE  - a correct VaR (ideally at the nominal level, 5 %), and
* POWER - a wrong VaR model (the higher, the better the test)?

Design (all synthetic, deterministic seeds):
* Data generators (daily losses, unconditional sd about 1 %): iid normal,
  iid Student-t (nu = 5), GARCH(1,1) with normal innovations, GARCH(1,1) with
  unit-variance t(6) innovations, two-state regime switching.
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
* Conditional models (v0.3): GARCH(1,1)-t and filtered HS on the GARCH-t data
  (correctly specified, "size" rows with estimation risk), EWMA-t (lambda 0.94,
  nu 6) on the GARCH-t data, and all three on the regime data ("conditional"
  rows: misspecified but adaptive). Key question: how much less often are they
  rejected than the static models? They are refitted every ``cond_refit_every``
  days (default 50) and roll their variance forward daily in between.
* Estimation risk: GARCH-t and FHS on the GARCH-t data also run with a
  1000-day estimation window (T = 1000 only, own data seeds); comparing them
  with the 250-day rows shows how much over-rejection comes from estimation error.
* ES: the Acerbi-Szekely Z2 test at 97.5 % (one-sided, Monte-Carlo p-value
  with ``z2_sims`` draws); its reference size is the oracle row. The Monte-Carlo
  seed is a ``SeedSequence`` child of the data seed (independent of it).
* Monte-Carlo error: every rate r carries ``se = sqrt(r (1 - r) / M)``.
* The Student-t model is refitted every ``t_refit_every`` days (default 50;
  maximum likelihood is the expensive step); all other models daily. A t fit
  that does not converge raises; such replications are counted (``failed_reps``)
  and left out of the rates.
* Traffic light: the zone of the last 250 forecast days (BCBS 1996).

Run from ``UFOS.ai/RiskLab/``::

    python studies/var_backtest_power.py                 # default: M = 30 (a demo); measured 126 s on the user's PC
    python studies/var_backtest_power.py --mode full     # M = 1000; measured 43 minutes in the cloud container
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
from risklab import analytic
from risklab.backtest import (REJECT, acerbi_szekely_z2, binomial_exact, christoffersen_cc, christoffersen_ind,
                              exact_size, kupiec_pof, traffic_light)
from risklab.data import garch11, iid_normal, iid_student_t, regime_switching, true_var_es
from risklab.dists import LocScale
from risklab.engine import walk_forward
from risklab.models import (EWMAModel, FilteredHS, FitError, GarchTModel, HistoricalSimulation, NormalModel,
                            StudentTModel)
from risklab.models.historical import exceedance_probability
from risklab.report.export import default_out_dir, table_to_csv, table_to_json, text_to_file

STUDY = "var_backtest_power"

GENERATORS: Dict[str, Callable] = {
    "iid_normal": lambda n, seed: iid_normal(n, 0.0, 0.01, seed),
    "iid_t5": lambda n, seed: iid_student_t(n, 0.0, 0.01, 5.0, seed),
    "garch_normal": lambda n, seed: garch11(n, 2e-6, 0.08, 0.90, seed=seed),
    "garch_t6": lambda n, seed: garch11(n, 2e-6, 0.08, 0.90, innovations="t", nu=6.0, seed=seed),
    "regime": lambda n, seed: regime_switching(n, (0.007, 0.02), ((0.98, 0.02), (0.05, 0.95)), seed=seed),
}

MODELS: Dict[str, Callable] = {
    "normal": NormalModel,
    "hs": HistoricalSimulation,
    "student_t": StudentTModel,
    "garch_t": GarchTModel,
    "fhs": FilteredHS,
    "ewma_t": lambda: EWMAModel(lam=0.94, dist="t", nu=6.0),
}

CONDITIONAL = ("garch_t", "fhs", "ewma_t")

#: (generator, model, role[, window, horizons]). "oracle" = the true conditional VaR. Cases without
#: window / horizons use ``StudyConfig.window`` and all ``StudyConfig.horizons``.
CASES: List[tuple] = [
    ("iid_normal", "oracle", "size"),
    ("iid_t5", "oracle", "size"),
    ("garch_normal", "oracle", "size"),
    ("regime", "oracle", "size"),
    ("garch_t6", "oracle", "size"),
    ("iid_normal", "normal", "size"),
    ("iid_normal", "hs", "size"),
    ("iid_t5", "student_t", "size"),
    ("garch_t6", "garch_t", "size"),
    ("garch_t6", "fhs", "size"),
    ("iid_t5", "normal", "power"),
    ("garch_normal", "normal", "power"),
    ("garch_normal", "hs", "power"),
    ("garch_t6", "normal", "power"),
    ("garch_t6", "hs", "power"),
    ("regime", "normal", "power"),
    ("regime", "hs", "power"),
    ("garch_t6", "ewma_t", "conditional"),
    ("regime", "garch_t", "conditional"),
    ("regime", "fhs", "conditional"),
    ("regime", "ewma_t", "conditional"),
    # (generator, model, role, estimation window, horizons): a 1000-day window isolates estimation risk
    ("garch_t6", "garch_t", "size", 1000, (1000,)),
    ("garch_t6", "fhs", "size", 1000, (1000,)),
]

MODES = {"smoke": 2, "default": 30, "full": 1000}

TESTS = ("kupiec", "binom", "ind", "cc", "z2")

COLUMNS = (["generator", "model", "role", "window", "T", "m", "m_ok", "failed_reps", "exceed_rate"]
           + [c for t in TESTS for c in (f"reject_{t}", f"se_{t}")]
           + ["exact_size_kupiec", "exact_size_binom", "ref_size_ind", "ref_size_cc", "ref_size_z2",
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
    cond_refit_every: int = 50
    alpha_es: float = 0.975
    z2_sims: int = 500
    cases: Sequence[tuple] = field(default_factory=lambda: list(CASES))


def data_seed(base: int, gen_index: int, horizon: int, rep: int, window: Optional[int] = None) -> int:
    """Deterministic per-replication data seed; all models of a replication see the same data.

    ``window`` is mixed in only for non-default estimation windows (so the
    default rows keep their v0.2 / v0.3 seeds).
    """
    key = [base, gen_index, horizon, rep] + ([] if window is None else [window])
    return int(np.random.SeedSequence(key).generate_state(1)[0])


def mc_seed(dseed: int) -> int:
    """Seed for the Z2 Monte-Carlo p-values: a SeedSequence child of the data seed (independent streams)."""
    return int(np.random.SeedSequence(dseed).spawn(1)[0].generate_state(1)[0])


def _norm_case(case: tuple, cfg: "StudyConfig") -> Tuple[str, str, str, int, Tuple[int, ...]]:
    """(generator, model, role, window, horizons) with the defaults filled in."""
    if len(case) == 3:
        return (case[0], case[1], case[2], cfg.window, tuple(cfg.horizons))
    gen, model, role, w, hs = case
    return (gen, model, role, int(w), tuple(h for h in hs if h in cfg.horizons))


def mc_se(rate: Optional[float], m: int) -> Optional[float]:
    """Monte-Carlo standard error of a rejection rate: sqrt(r (1 - r) / M)."""
    if rate is None or m < 1:
        return None
    return math.sqrt(rate * (1.0 - rate) / m)


def _note(gen: str, model: str, role: str, cfg: StudyConfig, window: Optional[int] = None) -> str:
    if window is not None and window != cfg.window:
        return (f"correct model class; {window}-day estimation window (isolates estimation risk); "
                f"refit every {cfg.cond_refit_every} days")
    if role == "power":
        return "not size-adjusted"
    if model == "oracle":
        return "truth conditional on the realised state (oracle)" if gen == "regime" else "true VaR"
    if model == "hs":
        p = exceedance_probability(cfg.window, cfg.alpha)
        return f"not a clean size row: HS exceedance probability (n-k+1)/(n+1) = {100 * p:.2f} %"
    if model == "student_t":
        return f"estimation risk; t refit every {cfg.t_refit_every} days"
    if role == "conditional":
        if gen == "regime":
            return "conditional but misspecified (regime data); compare with the static rows"
        return "conditional; EWMA lambda 0.94, nu 6 fixed (not the true dynamics)"
    if model in ("garch_t", "fhs"):
        return f"correct model class; estimation risk; refit every {cfg.cond_refit_every} days"
    return "estimation risk"


def _oracle_dists(series, w: int):
    """True predictive distribution of each forecast day (for the Z2 Monte-Carlo test)."""
    mu, sd = series.mu_true[w:], series.sigma_true[w:]
    if series.dist == "normal":
        return tuple(LocScale(m, s) for m, s in zip(mu, sd))
    nu = series.dist_params["nu"]
    scale = analytic.t_scale_from_sd(sd, nu)
    return tuple(LocScale(m, s, nu) for m, s in zip(mu, scale))


def _refit(model: str, cfg: StudyConfig) -> int:
    if model == "student_t":
        return cfg.t_refit_every
    return cfg.cond_refit_every if model in CONDITIONAL else 1


def _evaluate(series, model: str, cfg: StudyConfig, z2_seed: int, w: int):
    """(VaR exceedances, VaR forecasts, true VaR, Z2 TestResult) for the T forecast days after window w."""
    alphas = sorted({cfg.alpha, cfg.alpha_es})
    truth = true_var_es(series, alphas)
    losses = series.losses[w:]
    t_var = truth.var[cfg.alpha][w:]
    if model == "oracle":
        z2 = acerbi_szekely_z2(losses, truth.var[cfg.alpha_es][w:], truth.es[cfg.alpha_es][w:],
                               _oracle_dists(series, w), cfg.alpha_es, seed=z2_seed, m_sim=cfg.z2_sims,
                               reject_at=cfg.reject_at)
        return losses > t_var, t_var, t_var, z2
    frame = walk_forward(series.losses, MODELS[model], w, alphas, refit_every=_refit(model, cfg))
    z2 = acerbi_szekely_z2(frame.loss, frame.var[cfg.alpha_es], frame.es[cfg.alpha_es], frame.dists,
                           cfg.alpha_es, seed=z2_seed, m_sim=cfg.z2_sims, reject_at=cfg.reject_at)
    return frame.exceedance[cfg.alpha], frame.var[cfg.alpha], t_var, z2


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
    cases = [_norm_case(c, cfg) for c in cfg.cases]
    experiments: Dict[Tuple[str, int, int], List[Tuple[str, str]]] = {}  # (gen, window, T) -> [(model, role)]
    for gen, model, role, w, hs in cases:
        for h in hs:
            experiments.setdefault((gen, w, h), []).append((model, role))
    rows = []
    for (gen, w, horizon) in sorted(experiments, key=lambda k: (gen_names.index(k[0]), k[2], k[1])):
        models = experiments[(gen, w, horizon)]
        if models:
            acc = {m: {"exceed": [], "kupiec": 0, "binom": 0, "ind": 0, "cc": 0, "z2": 0, "no_info": 0, "failed": 0,
                       "green": 0, "yellow": 0, "red": 0, "rel_err": []} for m, _ in models}
            for rep in range(cfg.m):
                dseed = data_seed(cfg.seed, gen_names.index(gen), horizon, rep,
                                  None if w == cfg.window else w)
                series = GENERATORS[gen](w + horizon, dseed)
                zseed = mc_seed(dseed)
                for model, _ in models:
                    a = acc[model]
                    try:
                        exc, var_hat, var_true, z2 = _evaluate(series, model, cfg, zseed, w)
                    except FitError:
                        a["failed"] += 1
                        continue
                    a["z2"] += z2.decision == REJECT
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
                row = {"generator": gen, "model": model, "role": role, "window": w, "T": horizon, "m": cfg.m,
                       "m_ok": m_ok,
                       "failed_reps": a["failed"],
                       "exceed_rate": float(np.mean(a["exceed"])) if m_ok else None}
                for t in TESTS:
                    row[f"reject_{t}"] = rate(t)
                    row[f"se_{t}"] = mc_se(rate(t), m_ok)
                row.update({
                    "exact_size_kupiec": sizes[horizon][0], "exact_size_binom": sizes[horizon][1],
                    "ref_size_ind": None, "ref_size_cc": None, "ref_size_z2": None,
                    "ind_no_info": rate("no_info"), "tl_green": rate("green"), "tl_yellow": rate("yellow"),
                    "tl_red": rate("red"), "var_rel_error": float(np.mean(a["rel_err"])) if m_ok else None,
                    "size_adjusted": False, "note": _note(gen, model, role, cfg, w),
                })
                rows.append(row)
            if progress:
                progress(f"{gen} window={w} T={horizon}: done")
    # ind / cc reference size: the oracle row of the same data and T (empirical)
    oracle = {(r["generator"], r["T"]): r for r in rows if r["model"] == "oracle"}
    for r in rows:
        ref = oracle.get((r["generator"], r["T"]))
        if ref is not None:
            r["ref_size_ind"], r["ref_size_cc"] = ref["reject_ind"], ref["reject_cc"]
            r["ref_size_z2"] = ref["reject_z2"]
    order = {(g, m, w): i for i, (g, m, _, w, _) in enumerate(cases)}
    role_order = {"size": 0, "power": 1, "conditional": 2}
    rows.sort(key=lambda r: (role_order.get(r["role"], 3), order[(r["generator"], r["model"], r["window"])], r["T"]))
    return rows


def _pct(x: Optional[float], digits: int = 1) -> str:
    return "n/a" if x is None else f"{100.0 * x:.{digits}f} %"


def _label(r: dict, cfg: "StudyConfig") -> str:
    return r["model"] if r["window"] == cfg.window else f"{r['model']} (window {r['window']})"


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


def _comparison_tables(rows: List[dict], cfg: "StudyConfig") -> List[str]:
    """Static against conditional models: Kupiec, binomial, cc and Z2 side by side with their reference sizes."""
    out = ["", "## Static against conditional models", "",
           "Rejection rates in % ± se. The first line of each block is the reference size: the exact size for "
           "Kupiec and the binomial test, the oracle row of the same data and T for cc and Z2. A correct model "
           "should be rejected at about the reference rate; anything clearly above it is over-rejection.", ""]
    for gen in ("garch_t6", "regime"):
        for h in cfg.horizons:
            block = [r for r in rows if r["generator"] == gen and r["T"] == h and r["model"] != "oracle"]
            ref = next((r for r in rows if r["generator"] == gen and r["T"] == h and r["model"] == "oracle"), None)
            if not block or ref is None:
                continue
            out += [f"**{gen}, T = {h}**", "",
                    "| model | role | exceed rate | Kupiec | binomial exact | cc | ES Z2 |",
                    "|---|---|---|---|---|---|---|",
                    f"| reference size | | 1.00 % (target) | {_pct(ref['exact_size_kupiec'])} | "
                    f"{_pct(ref['exact_size_binom'])} | {_rate(ref, 'cc')} | {_rate(ref, 'z2')} |"]
            for r in block:
                out.append(f"| {_label(r, cfg)} | {r['role']} | {_pct(r['exceed_rate'], 2)} | {_rate(r, 'kupiec')} | "
                           f"{_rate(r, 'binom')} | {_rate(r, 'cc')} | {_rate(r, 'z2')} |")
            out.append("")
    out += ["Reading (computed from the rows above):", ""]
    tests = ("kupiec", "binom", "cc", "z2")
    for static in ("normal", "hs"):
        lower = total = 0
        for gen in ("garch_t6", "regime"):
            for h in cfg.horizons:
                blk = {r["model"]: r for r in rows
                       if r["generator"] == gen and r["T"] == h and r["window"] == cfg.window}
                if static not in blk:
                    continue
                for model in CONDITIONAL:
                    if model in blk:
                        for t in tests:
                            if blk[model][f"reject_{t}"] is None or blk[static][f"reject_{t}"] is None:
                                continue
                            total += 1
                            lower += blk[model][f"reject_{t}"] < blk[static][f"reject_{t}"]
        if total:
            out.append(f"- Conditional models (250-day window) against static {static}: rejected less often in "
                       f"{lower} of {total} comparisons (model x data x T x test).")
    for model in ("garch_t", "fhs"):
        for h in cfg.horizons:
            short = next((r for r in rows if (r["generator"], r["model"], r["T"], r["window"])
                          == ("garch_t6", model, h, cfg.window)), None)
            long_ = [r for r in rows if (r["generator"], r["model"], r["T"]) == ("garch_t6", model, h)
                     and r["window"] != cfg.window]
            for lr in long_:
                if short is None or short["reject_kupiec"] is None or lr["reject_kupiec"] is None:
                    continue
                out.append(
                    f"- Estimation risk, {model} on garch_t6, T = {h}: exceedance rate {_pct(short['exceed_rate'], 2)} "
                    f"with a {cfg.window}-day window against {_pct(lr['exceed_rate'], 2)} with {lr['window']} days; "
                    f"Kupiec {_rate(short, 'kupiec')} against {_rate(lr, 'kupiec')} (exact size "
                    f"{_pct(lr['exact_size_kupiec'])}); Z2 {_rate(short, 'z2')} against {_rate(lr, 'z2')} "
                    f"(oracle {_pct(lr['ref_size_z2'])}).")
    out += ["- A correctly specified model that is rejected clearly above the reference size with the short window "
            "but near it with the long window over-rejects because of estimation error, not because of its form."]
    return out


def summary_markdown(rows: List[dict], cfg: StudyConfig) -> str:
    se5 = mc_se(0.05, cfg.m)
    p = 1.0 - cfg.alpha
    lines = [
        "# VaR / ES backtest size and power (synthetic data)",
        "",
        f"VaR tests at alpha = {cfg.alpha}, ES test (Acerbi-Szekely Z2, one-sided, {cfg.z2_sims} Monte-Carlo draws) "
        f"at alpha = {cfg.alpha_es}; estimation window = {cfg.window} days, tests at {100 * cfg.reject_at:g} %, "
        f"M = {cfg.m} replications per row, seed = {cfg.seed}. Refits: Student-t every {cfg.t_refit_every} days, "
        f"conditional models (GARCH-t, FHS, EWMA-t) every {cfg.cond_refit_every} days with a daily variance update "
        f"in between, static models daily. "
        f"risklab {risklab.__version__}, numpy {np.__version__}, scipy {scipy.__version__}.",
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
        "For ind, cc and Z2 there is no closed form: their reference is the empirical rate of the oracle row "
        "of the same data and T (`ref_size_ind`, `ref_size_cc`, `ref_size_z2` in the CSV). Z2 uses a "
        "Monte-Carlo p-value under each day's predictive distribution, so its size is at most 5 % up to "
        "Monte-Carlo error.",
        "",
        "## Rejection rates (%, ± Monte-Carlo se)",
        "",
        "| role | data | model | T | exceed rate | Kupiec | binomial exact | ind | cc | ES Z2 | ind no-info "
        "| TL green / yellow / red | mean abs rel VaR error | note |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        failed = f" ({r['failed_reps']} failed fits)" if r["failed_reps"] else ""
        lines.append(
            f"| {r['role']} | {r['generator']} | {_label(r, cfg)} | {r['T']} | {_pct(r['exceed_rate'], 2)} | "
            f"{_rate(r, 'kupiec')} | {_rate(r, 'binom')} | {_rate(r, 'ind')} | {_rate(r, 'cc')} | {_rate(r, 'z2')} | "
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
        "- \"conditional\" rows: GARCH-t, FHS and EWMA-t on data they do not exactly describe (regime) or with "
        "fixed, not estimated, dynamics (EWMA-t); compare them with the static rows of the same data and T.",
    ]
    lines += _comparison_tables(rows, cfg)
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
                    help="smoke (M=2, T=250 only), default (M=30, a demo) or full (M=1000)")
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
