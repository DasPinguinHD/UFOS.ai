"""LaTeX tables for the validation report and the gate overfitting study, generated from the saved
study results (no matplotlib needed).

Every table is a complete ``table`` environment with a caption and a label
``tab:<name>`` and uses ``booktabs`` rules (the build checks that booktabs is
installed). Monte-Carlo numbers carry their standard error as ``value $\\pm$ se``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

TABLE_NAMES = ("T1_model_inventory", "T2_size", "T3_power", "T4_estimation_window", "T5_horizon",
               "T6_survivorship", "T7_findings")
#: tables of the second document, the overfitting study of the UFOS validation gates
OVERFIT_TABLE_NAMES = ("O1_gates_scope", "O2_fpr", "O3_power", "O4_holdout", "O5_calibration", "O6_findings")

_LATEX_SPECIAL = {"\\": "\\textbackslash{}", "&": "\\&", "%": "\\%", "$": "\\$", "#": "\\#", "_": "\\_",
                  "{": "\\{", "}": "\\}", "~": "\\textasciitilde{}", "^": "\\textasciicircum{}"}


def esc(text) -> str:
    """Escape LaTeX special characters in plain text."""
    return "".join(_LATEX_SPECIAL.get(c, c) for c in str(text))


def pct(x: Optional[float], digits: int = 1) -> str:
    """A fraction as a percentage number without the sign, e.g. 0.0948 -> '9.5'; None -> '--'."""
    return "--" if x is None else f"{100.0 * x:.{digits}f}"


def pm(x: Optional[float], se: Optional[float], digits: int = 1, se_digits: Optional[int] = None) -> str:
    """'value $\\pm$ se' in percent; '--' if missing. ``se_digits`` defaults to ``digits``."""
    if x is None:
        return "--"
    if se is None:
        return pct(x, digits)
    return f"{pct(x, digits)} $\\pm$ {pct(se, digits if se_digits is None else se_digits)}"


def pmr(x: Optional[float], se: Optional[float], n: Optional[int], digits: int = 1) -> str:
    """A Monte-Carlo rate like :func:`pm`, but a rate of exactly 0 or 1 out of ``n`` paths shows the
    95 % bound of the rule of three instead of '$\\pm$ 0.0': '0 ($\\le$ 3/n)' or '100 ($\\ge$ 1 - 3/n)'."""
    if x is None or not n:
        return pm(x, se, digits)
    if x == 0.0:
        return f"0 ($\\le$ {300.0 / n:.2g})"
    if x == 1.0:
        return f"100 ($\\ge$ {100.0 - 300.0 / n:.4g})"
    return pm(x, se, digits)


def _rag(*widths: str) -> str:
    """Ragged-right paragraph columns (needs the array package)."""
    return "".join(f">{{\\raggedright\\arraybackslash}}p{{{w}}}" for w in widths)


def _table(name: str, caption: str, colspec: str, header: Sequence[str], rows: Iterable[Sequence[str]],
           size: str = "\\small", notes: Optional[str] = None, colsep: Optional[str] = None) -> str:
    lines = ["\\begin{table}[H]", "\\centering", size, f"\\caption{{{caption}}}", f"\\label{{tab:{name}}}"]
    if colsep:
        lines.append(f"\\setlength{{\\tabcolsep}}{{{colsep}}}")
    lines += [f"\\begin{{tabular}}{{{colspec}}}", "\\toprule", " & ".join(header) + " \\\\", "\\midrule"]
    for r in rows:
        lines.append("\\midrule" if r == "MIDRULE" else " & ".join(r) + " \\\\")
    lines += ["\\bottomrule", "\\end{tabular}"]
    if notes:
        lines.append(f"\\par\\smallskip\\parbox{{0.95\\linewidth}}{{\\footnotesize {notes}}}")
    lines.append("\\end{table}")
    return "\n".join(lines) + "\n"


# ---- T1 model inventory (static description of the code) -------------------------------

MODEL_INVENTORY = (
    ("Historical simulation", "iid losses; the window is representative",
     "none (order statistic $k=\\lceil n\\alpha\\rceil$)", "250", "daily"),
    ("Normal", "iid normal losses", "$\\mu$, $\\sigma$ (sample, ddof 1)", "250", "daily"),
    ("Student-t", "iid location-scale t", "$\\mu$, $s$, $\\nu \\in [2.05, 200]$ (MLE)", "250", "50 days"),
    ("EWMA-t", "zero mean, $\\sigma^2_{t+1} = \\lambda\\sigma^2_t + (1-\\lambda)L_t^2$", "$\\lambda = 0.94$, $\\nu = 6$ fixed",
     "250", "50 days + daily update"),
    ("GARCH(1,1)-t", "unit-variance t innovations", "$\\mu, \\omega, \\alpha, \\beta, \\nu$ (MLE, $\\alpha+\\beta \\le 0.999$)",
     "250 / 1000", "50 days + daily update"),
    ("Filtered HS", "GARCH(1,1) filter, empirical residuals", "GARCH by Gaussian QMLE; residual quantile",
     "250 / 1000", "50 days + daily update"),
    ("10-day: $\\sqrt{10}$ scaling", "iid, zero mean (approximation)", "one-day model", "500", "per origin"),
    ("10-day: GARCH Monte Carlo", "GARCH(1,1)-t dynamics", "20\\,000 simulated paths", "500", "per origin"),
    ("10-day: block bootstrap", "stationary dependence", "automatic block length (Politis--White)", "500", "per origin"),
)


def table_model_inventory() -> str:
    return _table("T1_model_inventory", "Model inventory: the models under validation.",
                  _rag("3.0cm", "3.5cm", "4.0cm", "1.4cm", "2.0cm"),
                  ["Model", "Assumptions", "Parameters", "Window (days)", "Refit"],
                  MODEL_INVENTORY, size="\\footnotesize")


# ---- T2 size --------------------------------------------------------------------------

def table_size(power: dict) -> str:
    rows: List[Sequence[str]] = []
    oracles = [r for r in power["rows"] if r["model"] == "oracle"]
    for T in sorted({r["T"] for r in oracles}):
        r0 = next(r for r in oracles if r["T"] == T)
        rows.append([f"exact size, T = {T}", pct(r0["exact_size_kupiec"], 2), pct(r0["exact_size_binom"], 2),
                     "--", "--"])
    rows.append("MIDRULE")
    for r in sorted(oracles, key=lambda r: (r["generator"], r["T"])):
        rows.append([f"oracle, {esc(r['generator'])}, T = {r['T']}", pm(r["reject_kupiec"], r["se_kupiec"]),
                     pm(r["reject_binom"], r["se_binom"]), pm(r["reject_cc"], r["se_cc"]),
                     pm(r["reject_z2"], r["se_z2"])])
    m = power["meta"]["config"]["m"]
    return _table("T2_size", f"Size of the backtests at 5\\,\\% (rejection rate of a correct VaR / ES, in \\%). "
                             f"Exact binomial sums; oracle rows: M = {m} synthetic replications ($\\pm$ se).",
                  "lrrrr", ["", "Kupiec", "Binomial exact", "Christoffersen cc", "ES Z2"], rows)


# ---- T3 power -------------------------------------------------------------------------

POWER_ROWS = (("iid_t5", "normal", 250), ("garch_t6", "normal", 250), ("garch_t6", "hs", 250),
              ("garch_t6", "ewma_t", 250), ("garch_t6", "garch_t", 250), ("garch_t6", "fhs", 250),
              ("garch_t6", "garch_t", 1000), ("garch_t6", "fhs", 1000), ("regime", "normal", 250),
              ("regime", "hs", 250), ("regime", "ewma_t", 250), ("regime", "garch_t", 250), ("regime", "fhs", 250))


def table_power(power: dict) -> str:
    idx = {(r["generator"], r["model"], r["window"], r["T"]): r for r in power["rows"]}
    rows: List[Sequence[str]] = []
    for T in sorted({r["T"] for r in power["rows"]}):
        if rows:
            rows.append("MIDRULE")
        for gen, model, w in POWER_ROWS:
            r = idx.get((gen, model, w, T))
            if r is None:
                continue
            rows.append([esc(gen), esc(model), str(w), str(T), pct(r["exceed_rate"], 2),
                         pm(r["reject_kupiec"], r["se_kupiec"]), pm(r["reject_binom"], r["se_binom"]),
                         pm(r["reject_cc"], r["se_cc"]), pm(r["reject_z2"], r["se_z2"])])
    m = power["meta"]["config"]["m"]
    return _table("T3_power", f"Rejection rates (\\%, $\\pm$ se) of the backtests at 5\\,\\% by data, model and "
                              f"estimation window; M = {m} synthetic replications. Not size-adjusted: compare with "
                              f"Table~\\ref{{tab:T2_size}}.",
                  "llrrrrrrr", ["Data", "Model", "Window", "T", "Exceed.\\ (\\%)", "Kupiec", "Binomial", "cc", "Z2"],
                  rows, size="\\scriptsize")


# ---- T4 estimation window --------------------------------------------------------------

def table_estimation_window(ew: dict) -> str:
    rows = []
    for r in ew["rows"]:
        label = "true VaR" if r["model"] == "oracle" else esc(r["model"])
        diff = ("--" if r["model"] == "oracle" or r.get("exceed_diff") is None
                else f"{round(100 * r['exceed_diff'], 3) + 0.0:+.3f} $\\pm$ {pct(r['exceed_diff_se'], 3)}")
        rows.append([label, "--" if r["window"] is None else str(r["window"]), pm(r["exceed_rate"], r["exceed_se"], 2),
                     diff, pm(r["reject_kupiec"], r["se_kupiec"]), pm(r["reject_binom"], r["se_binom"]),
                     pm(r["reject_cc"], r["se_cc"]), pm(r["reject_z2"], r["se_z2"]), pct(r["var_rel_error"])])
    ref = ew["rows"][0]
    c = ew["meta"]["config"]
    return _table("T4_estimation_window",
                  f"Estimation window: correctly specified GARCH(1,1)-t and FHS on synthetic GARCH-t6 data, "
                  f"T = {c['T']} (same forecast days for every window), M = {c['m']}; rates in \\% ($\\pm$ se). "
                  f"Reference sizes: Kupiec {pct(ref['ref_kupiec'], 2)}, binomial {pct(ref['ref_binom'], 2)} (exact); "
                  f"cc and Z2: the true-VaR row. $\\Delta$: paired difference of the exceedance rate against the true "
                  f"VaR on the same days (percentage points $\\pm$ se); positive = anti-conservative. The 1000-day "
                  f"rows use the same design as the 1000-day rows of the power study (checked per replication); "
                  f"only the data seeds differ, so differences between the two studies are Monte-Carlo noise.",
                  "lrrrrrrrr", ["Model", "Window", "Exceed.\\ (\\%)", "$\\Delta$ exceed.\\ (pp)", "Kupiec",
                                "Binomial", "cc", "Z2", "VaR error (\\%)"], rows, size="\\scriptsize", colsep="3.5pt")


# ---- T5 horizon -----------------------------------------------------------------------

def table_horizon(horizon: dict) -> str:
    rows = []
    for r in horizon["rows"]:
        rows.append([esc(r["generator"]), esc(r["method"]),
                     "--" if r["bias"] is None else f"{100 * r['bias']:+.1f} $\\pm$ {pct(r['bias_se'])}",
                     pct(r["rmse"]), "--" if r["est_mc_rel_se"] is None else pct(r["est_mc_rel_se"], 2),
                     pct(r["truth_mc_rel_se"], 2)])
    c = horizon["meta"]["config"]
    return _table("T5_horizon", f"10-day {100 * c['alpha']:g}\\,\\% VaR against the true 10-day VaR (relative, in \\%; "
                                f"$\\pm$ se over M = {c['m']} replications). MC se: simulation error of the estimate "
                                f"and of the truth.",
                  "llrrrr", ["Data", "Method", "Bias", "RMSE", "Est.\\ MC se", "Truth MC se"], rows)


# ---- T6 survivorship ------------------------------------------------------------------

#: CAGR thresholds shown in Table T6 (all are in the results file).
T6_THRESHOLDS = (0.05, 0.10, 0.15)


def table_survivorship(surv: dict) -> str:
    rows: List[Sequence[str]] = []
    settings = list(dict.fromkeys((r["h0"], r["kappa"]) for r in surv["rows"]))
    for h0, k in settings:
        for r in surv["rows"]:
            if r["h0"] != h0 or r["kappa"] != k or not any(abs(r["threshold"] - g) < 1e-12 for g in T6_THRESHOLDS):
                continue
            rows.append([f"{h0:g}", f"{k:g}", pct(r["survival_rate"]), f"{100 * r['threshold']:g}",
                         pct(r["naive"]), pct(r["counterfactual"]), pct(r["true"]),
                         pm(r["selection"], r["selection_se"], 1, 2), pm(r["attrition"], r["attrition_se"], 1, 2),
                         pm(r["bias"], r["bias_se"], 1, 2)])
    c = surv["meta"]["config"]
    return _table("T6_survivorship",
                  f"Survivorship bias of 10-year growth base rates (in \\%; synthetic universe of {c['n_firms']} firms, "
                  f"M = {c['m']} cohorts per setting). Naive: survivors only (``what share of today's listed firms "
                  f"grew this fast?''). Counterfactual: all firms had they survived (``what share has the growth "
                  f"driver?''). Full cohort: exited firms count as misses (``how likely is a firm picked at the start "
                  f"to achieve it?''). Selection = naive $-$ counterfactual; attrition = counterfactual $-$ full "
                  f"cohort; total = their sum (percentage points $\\pm$ Monte-Carlo se; the parameter choices "
                  f"dominate the size). Acquisitions are treated like failures.",
                  "rrrrrrrrrr",
                  ["$h_0$", "$\\kappa$", "Surv.\\ (\\%)", "CAGR $\\ge$ (\\%)", "Naive", "Counterf.", "Full cohort",
                   "Selection", "Attrition", "Total"], rows, size="\\scriptsize", colsep="3pt")


# ---- T7 findings register -------------------------------------------------------------

def findings(power: dict, horizon: dict, ew: dict, surv: dict) -> List[Dict[str, str]]:
    """The findings the studies support, with numbers taken from the results. Severities are PROPOSED."""
    p = {(r["generator"], r["model"], r["window"], r["T"]): r for r in power["rows"]}
    o250 = p[("garch_t6", "oracle", 250, 250)]
    g250, g1000 = p[("garch_t6", "garch_t", 250, 1000)], p.get(("garch_t6", "garch_t", 1000, 1000))
    f250 = p[("garch_t6", "fhs", 250, 1000)]
    n_g = p[("garch_t6", "normal", 250, 1000)]
    ind_or = [r["reject_ind"] for r in power["rows"] if r["model"] == "oracle"]
    h = {(r["generator"], r["method"]): r for r in horizon["rows"]}
    hs = p.get(("iid_normal", "hs", 250, 1000))
    ew_g = {r["window"]: r for r in ew["rows"] if r["model"] == "garch_t"}
    ew_f = {r["window"]: r for r in ew["rows"] if r["model"] == "fhs"}
    curve = ", ".join(f"{w}: {pct(ew_g[w]['reject_kupiec'])}" for w in sorted(ew_g))
    exc_curve = ", ".join(f"{w}: {pct(ew_g[w]['exceed_rate'], 2)}" for w in sorted(ew_g))
    ew_or = next((r for r in ew["rows"] if r["model"] == "oracle"), None)
    ex_f = [ew_f[w]["exceed_rate"] for w in sorted(ew_f) if w >= 750]
    ex_g = [ew_g[w]["exceed_rate"] for w in sorted(ew_g) if w >= 500]
    sv = {(r["h0"], r["kappa"], r["threshold"]): r for r in surv["rows"]}
    sv10 = sv.get((surv["meta"]["config"]["h0"], 1.0, 0.1))
    out = [
        {"id": "F1", "severity": "High",
         "finding": f"With a 250-day window the correctly specified GARCH(1,1)-t over-rejects: Kupiec "
                    f"{pm(g250['reject_kupiec'], g250['se_kupiec'])}\\,\\% vs exact size "
                    f"{pct(g250['exact_size_kupiec'])}\\,\\%, Z2 {pm(g250['reject_z2'], g250['se_z2'])}\\,\\% "
                    f"(T = 1000); exceedance rate {pct(g250['exceed_rate'], 2)}\\,\\%."
                    + (f" With 1000 days: Kupiec {pm(g1000['reject_kupiec'], g1000['se_kupiec'])}\\,\\%." if g1000 else "")
                    + (f" Estimation-window study: the rejection rates fall to about the reference size by 500 days "
                       f"(GARCH-t) and 750 days (FHS) (Kupiec, \\% by window: {curve}), but the VaR stays slightly "
                       f"anti-conservative: exceedance rate {pct(min(ex_g + ex_f), 2)}--{pct(max(ex_g + ex_f), 2)}\\,\\% "
                       f"against the true VaR's {pct(ew_or['exceed_rate'], 2)}\\,\\% (GARCH-t by window: {exc_curve})."
                       if ew_g and ew_or and ex_g and ex_f else "")
                    + (f" Paired difference against the true VaR at {max(ew_g)} days: "
                       f"{round(100 * ew_g[max(ew_g)]['exceed_diff'], 3) + 0.0:+.3f} $\\pm$ "
                       f"{pct(ew_g[max(ew_g)]['exceed_diff_se'], 3)} pp."
                       if ew_g and ew_g[max(ew_g)].get("exceed_diff") is not None else ""),
         "evidence": "Tab.~\\ref{tab:T3_power}, \\ref{tab:T4_estimation_window}; Fig.~\\ref{fig:fig3_estimation_window}",
         "remediation": "Estimation window of at least 500 days for GARCH-t and 750 days for FHS (where the "
                        "rejection rates reach the reference sizes); account for the remaining small "
                        "anti-conservatism (parameter uncertainty), e.g. by a VaR add-on or a longer window."},
        {"id": "F2", "severity": "Medium",
         "finding": f"FHS with a 250-day window underestimates the tail (ES): Z2 rejects in {pm(f250['reject_z2'], f250['se_z2'])}\\,\\% "
                    f"(oracle {pct(f250['ref_size_z2'])}\\,\\%, T = 1000)"
                    + (f"; with 750 days {pm(ew_f[750]['reject_z2'], ew_f[750]['se_z2'])}\\,\\%." if 750 in ew_f else "."),
         "evidence": "Tab.~\\ref{tab:T3_power}, \\ref{tab:T4_estimation_window}",
         "remediation": "Longer window for the residual distribution; monitor the ES backtest separately from VaR."},
        {"id": "F3", "severity": "High",
         "finding": f"Static models miss volatility clustering: the static normal VaR on GARCH-t data is rejected by cc in "
                    f"{pm(n_g['reject_cc'], n_g['se_cc'])}\\,\\% and by Z2 in {pm(n_g['reject_z2'], n_g['se_z2'])}\\,\\% "
                    f"(T = 1000).",
         "evidence": "Fig.~\\ref{fig:fig1_exceedances}, \\ref{fig:fig2_rejection_rates}; Tab.~\\ref{tab:T3_power}",
         "remediation": "Use a conditional volatility model (GARCH-t / FHS with an adequate window)."},
        {"id": "F4", "severity": "Medium",
         "finding": f"The Kupiec test is mis-sized at T = 250: exact size {pct(o250['exact_size_kupiec'], 2)}\\,\\% at a nominal "
                    f"5\\,\\% (x = 0 is rejected); the exact binomial test keeps {pct(o250['exact_size_binom'], 2)}\\,\\%.",
         "evidence": "Tab.~\\ref{tab:T2_size}",
         "remediation": "Report the exact binomial test (or the exact size) alongside Kupiec for one-year backtests."},
        {"id": "F5", "severity": "Low",
         "finding": f"The Christoffersen independence test is undersized ({pct(min(ind_or))}--{pct(max(ind_or))}\\,\\% on "
                    f"correct VaRs) and has little power on its own.",
         "evidence": "Results file of the power study (ind columns)",
         "remediation": "Do not rely on the independence test alone; use cc and the ES test."},
        {"id": "F6", "severity": "Medium",
         "finding": f"$\\sqrt{{10}}$ scaling of a fat-tailed one-day VaR overstates the 10-day VaR on iid t5 data "
                    f"({100 * h[('iid_t5', 'sqrt10_garch')]['bias']:+.1f} $\\pm$ {pct(h[('iid_t5', 'sqrt10_garch')]['bias_se'])}\\,\\%). "
                    f"On GARCH-t6 data $\\sqrt{{10}}$ is unbiased on average but $\\sqrt{{10}}\\times$ static normal has an RMSE "
                    f"of {pct(h[('garch_t6', 'sqrt10_normal')]['rmse'])}\\,\\%.",
         "evidence": "Fig.~\\ref{fig:fig5_horizon}; Tab.~\\ref{tab:T5_horizon}",
         "remediation": "Simulate the 10-day distribution (GARCH Monte Carlo); use $\\sqrt{10}$ only for a conditional "
                        "one-day VaR when the 10-day sum is near-normal."},
        {"id": "F7", "severity": "Low",
         "finding": f"The stationary block bootstrap is biased on GARCH-t6 data "
                    f"({100 * h[('garch_t6', 'bootstrap')]['bias']:+.1f} $\\pm$ {pct(h[('garch_t6', 'bootstrap')]['bias_se'])}\\,\\%, "
                    f"RMSE {pct(h[('garch_t6', 'bootstrap')]['rmse'])}\\,\\%): it does not condition on the current volatility.",
         "evidence": "Tab.~\\ref{tab:T5_horizon}",
         "remediation": "Do not use the unconditional bootstrap as the 10-day benchmark under volatility clustering."},
        {"id": "F8", "severity": "Low",
         "finding": "Historical simulation with $k = \\lceil n\\alpha\\rceil$ has a finite-sample exceedance probability of "
                    "$(n-k+1)/(n+1)$ = 1.20\\,\\% at n = 250, $\\alpha$ = 0.99"
                    + (f" (simulated: {pct(hs['exceed_rate'], 2)}\\,\\% on iid normal data)." if hs else "."),
         "evidence": "Results of the power study (HS on iid normal data)",
         "remediation": "Document the quantile convention; consider a longer window or interpolation."},
    ]
    if sv10 is not None:
        out.append({"id": "F9", "severity": "Medium",
                    "finding": f"Survivorship (synthetic, hazard strength 1, CAGR $\\ge$ 10\\,\\%): a survivors-only "
                               f"database shows {pct(sv10['naive'])}\\,\\%, against {pct(sv10['counterfactual'])}\\,\\% "
                               f"for all firms had they survived (selection bias "
                               f"{pm(sv10['selection'], sv10['selection_se'], 1, 2)} pp) and {pct(sv10['true'])}\\,\\% for "
                               f"the full cohort with exits as misses (attrition {pm(sv10['attrition'], sv10['attrition_se'], 1, 2)} pp). "
                               f"The size depends on the assumed hazard; the $\\pm$ se is Monte-Carlo noise only.",
                    "evidence": "Fig.~\\ref{fig:fig7_survivorship}; Tab.~\\ref{tab:T6_survivorship}",
                    "remediation": "Use point-in-time data that include delisted firms, and state which base rate "
                                   "(counterfactual or full cohort) answers the question asked."})
    return out


def table_findings(power: dict, horizon: dict, ew: dict, surv: dict) -> str:
    rows = [[f["id"], f["finding"], f"PROPOSED: {f['severity']}", f["evidence"], f["remediation"]]
            for f in findings(power, horizon, ew, surv)]
    return _table("T7_findings", "Findings register. Severities are PROPOSED and must be confirmed by the validator.",
                  _rag("0.5cm", "5.2cm", "1.8cm", "2.7cm", "3.5cm"),
                  ["ID", "Finding", "Severity", "Evidence", "Proposed remediation"], rows, size="\\scriptsize")


# ---- O1..O6: overfitting study of the UFOS validation gates ------------------------------

GATE_SCOPE = (
    ("G1", "Consistency", "time-series: fold Sharpe $> 0$ in $\\ge n$ of 5 walk-forward folds; cross-sectional: "
     "rank-IC NW $t$ and fold mean IC", "time-series form", "g1_folds_min", "{:d} of 5"),
    ("G2", "Alpha", "OLS alpha vs the benchmark $> 0$ and its Newey-West $t \\ge$ threshold",
     "vs buy and hold of the asset", "g2_alpha_t", "$t \\ge$ {:g}"),
    ("G3", "Signal decay", "IC half-life $\\ge$ average holding period (single-asset: not applicable)", "out of scope",
     None, None),
    ("G4", "Deflated Sharpe", "DSR with $N$ = trials + 1, trial-Sharpe variance floored at 1/years", "$N = K + 1$",
     "g4_dsr_min", "$\\ge$ {:.2f}"),
    ("G5", "Overfitting (PBO)", "CSCV, $S = 10$ blocks, over the parameter grid ($\\le 64$); $< 4$ configurations fail",
     "64-configuration UFOS grid", "g5_pbo_max", "$\\le$ {:.2f}"),
    ("G6", "Robustness", "(a) grid neighbours ($\\pm$20\\,\\%) keep $\\ge r\\times$ the base Sharpe; (b) costs "
     "$\\times m$ stay profitable; (c) stress-episode drawdowns", "(a) only", "g6_neighbour_ratio", "$r$ = {:.2f}"),
    ("G7", "Sample size", "closed trades $\\ge n$", "position changes", "g7_min_trades", "$\\ge$ {:d}"),
    ("G8", "Capacity", "capacity estimate $\\ge$ 10 $\\times$ the tested capital", "out of scope", None, None),
    ("H", "Holdout", "holdout Sharpe $> 0$ and $\\ge$ 50\\,\\% of the training Sharpe (fixed)", "last 24 months, once",
     None, None),
)


def table_go_gates_scope(go: dict) -> str:
    presets = go["meta"]["presets"]
    rows = []
    for gid, name, rule, here, key, fmt in GATE_SCOPE:
        th = ["--"] * 3 if key is None else [fmt.format(presets[p][key]) for p in ("lenient", "standard", "strict")]
        if gid == "H":
            th = ["fixed"] * 3
        rows.append([gid, esc(name), rule, here] + th)
    return _table("O1_gates_scope", "The UFOS validation gates (SPEC \\S 9.4), what this study covers, and the "
                  "preset thresholds. Out of scope: G1's cross-sectional rank-IC form, G3, the cost and stress parts of "
                  "G6, and G8.", _rag("0.75cm", "1.7cm", "5.1cm", "2.3cm") + "lll",
                  ["Gate", "Name", "UFOS rule", "This study", "Lenient", "Standard", "Strict"], rows,
                  size="\\scriptsize", colsep="3pt")


def _go_index(go: dict) -> Dict[tuple, dict]:
    return {(r["data"], r["years"], r["signal"], r["k"], r["preset"]): r for r in go["rows"]}


DATA_NAMES = {"iid_t": "iid $t_5$", "garch_t": "GARCH-$t_5$"}


def table_go_fpr(go: dict) -> str:
    idx = _go_index(go)
    c = go["meta"]["config"]
    rows = []
    for d in c["data"]:
        for y in c["years"]:
            for k in c["ks"]:
                cells = [idx[(d, y, 0.0, k, p)] for p in ("lenient", "standard", "strict")]
                st = cells[1]
                rows.append([DATA_NAMES.get(d, esc(d)), str(y), str(k)]
                            + [pmr(x["pass_stat"], x["pass_stat_se"], x["m"]) for x in cells]
                            + [pmr(st["pass_stat_h"], st["pass_stat_h_se"], st["m"]),
                               pmr(st["pass_all"], st["pass_all_se"], st["m"]), pmr(st["pass_all_h"], st["pass_all_h_se"], st["m"])])
            rows.append("MIDRULE")
    rows = rows[:-1]
    return _table("O2_fpr", f"False-positive rate (\\%) under noise (true Sharpe 0) of the best of $K$ "
                  f"configurations: statistical gates (G1, G2, G4, G5, G6 neighbours) per preset, with H, and all in-scope "
                  f"gates (with G7) for the standard preset. M = {c['m']} paths per cell, $\\pm$ Monte-Carlo se; a rate of 0 or 100 shows the 95\\,\\% bound of the rule of three "
                  "in brackets (3 / n).",
                  "lrrrrrrrr", ["Data", "Years", "$K$", "Stat.\\ L", "Stat.\\ S", "Stat.\\ St", "Stat.\\ S + H",
                                "All S", "All S + H"], rows, size="\\scriptsize", colsep="3pt")


def table_go_power(go: dict) -> str:
    idx = _go_index(go)
    c = go["meta"]["config"]
    sigs = [s for s in c["signals"] if s > 0]
    rows = []
    for key, preset, label in (("stat", "lenient", "Stat., lenient"), ("stat", "standard", "Stat., standard"),
                               ("stat", "strict", "Stat., strict"), ("all", "standard", "All, standard")):
        for d in c["data"]:
            for y in c["years"]:
                rows.append([label, DATA_NAMES.get(d, esc(d)), str(y)]
                            + [pmr(idx[(d, y, s, 64, preset)][f"pass_{key}"], idx[(d, y, s, 64, preset)][f"pass_{key}_se"],
                                   idx[(d, y, s, 64, preset)]["m"])
                               for s in sigs])
        rows.append("MIDRULE")
    return _table("O3_power", f"Power (\\%): pass rate of the best of $K$ = 64 configurations when the true "
                  f"configuration has the given gross Sharpe ratio a year. Statistical gates per preset and all in-scope "
                  f"gates (standard). M = {c['m']} paths per cell, $\\pm$ Monte-Carlo se; a rate of 0 or 100 shows the 95\\,\\% bound of the rule of three "
                  "in brackets (3 / n).",
                  "llr" + "r" * len(sigs), ["Gates", "Data", "Years"] + [f"SR {s:g}" for s in sigs], rows[:-1],
                  size="\\scriptsize", colsep="3pt")


def table_go_holdout(go: dict, years: int = 20, k: int = 64, preset: str = "standard") -> str:
    idx = _go_index(go)
    c = go["meta"]["config"]
    rows = []
    for d in c["data"]:
        for s in c["signals"]:
            r = idx[(d, years, s, k, preset)]
            rows.append([DATA_NAMES.get(d, esc(d)), f"{s:g}", pmr(r["pass_H"], r["pass_H_se"], r["m"]),
                         pmr(r["h_given_g4"], r["h_given_g4_se"], r["n_g4"]) + f" ({r['n_g4']})",
                         pmr(r["h_given_stat"], r["h_given_stat_se"], r["n_stat"]) + f" ({r['n_stat']})",
                         pmr(r["pass_stat"], r["pass_stat_se"], r["m"]), pmr(r["pass_stat_h"], r["pass_stat_h_se"], r["m"])])
        rows.append("MIDRULE")
    return _table("O4_holdout", f"Value of the holdout gate H ({years} training years, best of $K$ = {k}, {preset} "
                  f"preset): the unconditional H pass rate, the conditional rates given G4 and given the statistical "
                  f"gates (number of passing paths in brackets), and the statistical gates without and with H. "
                  f"M = {c['m']} paths per cell, rates in \\%, $\\pm$ Monte-Carlo se; a rate of 0 or 100 shows the 95\\,\\% bound of the rule of three "
                  "in brackets (3 / n).",
                  "lrrrrrr", ["Data", "True SR", "P(H)", "P(H $\\mid$ G4) (n)", "P(H $\\mid$ stat.) (n)", "Stat.",
                              "Stat. + H"], rows[:-1], size="\\scriptsize", colsep="3pt")


def table_go_calibration(go: dict) -> str:
    cal = {(c["data"], c["years"], c["k"]): c for c in go["meta"]["calibration"]}
    c = go["meta"]["config"]
    rows = []
    for d in c["data"]:
        for y in c["years"]:
            a, b = cal[(d, y, 64)], cal[(d, y, max(c["ks"]))]
            rows.append([DATA_NAMES.get(d, esc(d)), str(y), pmr(a["fail_any"], a["fail_any_se"], a["m"]),
                         pmr(a["fail_g4_or_g5"], a["fail_g4_or_g5_se"], a["m"]), pmr(b["fail_any"], b["fail_any_se"], b["m"]),
                         pmr(b["fail_g4_or_g5"], b["fail_g4_or_g5_se"], b["m"])])
    return _table("O5_calibration", "Calibration claims of SPEC \\S 9.6 on noise (standard preset): share of noise "
                  "paths that fail at least one in-scope gate (claim: $\\ge$ 95\\,\\%) and share of best-of-64 noise "
                  f"selections that fail G4 or G5 (claim: all). M = {c['m']} paths per cell, \\%, $\\pm$ Monte-Carlo se; a rate of 0 or 100 shows the 95\\,\\% bound of the rule of three "
                  "in brackets (3 / n).",
                  "lrrrrr", ["Data", "Years", "Fail $\\ge$1, $K$=64", "Fail G4/G5, $K$=64",
                             f"Fail $\\ge$1, $K$={max(c['ks'])}", f"Fail G4/G5, $K$={max(c['ks'])}"], rows,
                  size="\\scriptsize")


def overfitting_findings(go: dict) -> List[Dict[str, str]]:
    """Findings of the gate overfitting study, numbers from the results. Severities are PROPOSED."""
    idx = _go_index(go)
    c = go["meta"]["config"]
    ks, ys = c["ks"], c["years"]
    kmax = max(ks)
    null_stat = [idx[(d, y, 0.0, k, p)] for d in c["data"] for y in ys for k in ks for p in ("lenient", "standard", "strict")]
    worst = max(null_stat, key=lambda r: r["pass_stat"])
    null_std = [idx[(d, y, 0.0, k, "standard")] for d in c["data"] for y in ys for k in ks]
    worst_std = max(null_std, key=lambda r: r["pass_stat"])
    g1n = idx[("iid_t", 10, 0.0, kmax, "standard")]
    g4 = {k: idx[("iid_t", 20, 1.0, k, "standard")] for k in ks}
    p10 = idx[("iid_t", 10, 1.0, 64, "standard")]
    p20 = idx[("iid_t", 20, 1.0, 64, "standard")]
    g5s = [idx[("iid_t", y, 1.0, 64, "standard")]["pass_G5"] for y in ys]
    g7 = {y: idx[("iid_t", y, 0.0, 64, "standard")] for y in ys}
    ho = idx[("iid_t", 10, 0.0, 64, "standard")]
    hp = idx[("iid_t", 20, 1.0, 64, "standard")]
    h75 = idx[("iid_t", 20, 0.75, 64, "standard")]
    g5n = [idx[("iid_t", y, 0.0, 64, "standard")]["pass_G5"] for y in ys]
    n64 = idx[("iid_t", 10, 0.0, 64, "standard")]
    hs = idx[("iid_t", 20, 1.0, 64, "standard")]
    nb = min(r["neighbours_available"] for r in go["rows"])
    trades = ", ".join(f"{g7[y]['trades_mean']:.0f}" for y in ys)
    cal = [x for x in go["meta"]["calibration"] if x["k"] == 64]
    cal_min = min(cal, key=lambda x: x["fail_any"])
    g45_pass = sum(int(round((1.0 - x["fail_g4_or_g5"]) * x["m"])) for x in cal)
    out = [
        {"id": "O-F1", "severity": "Low",
         "finding": f"The gates control false positives of a searched noise rule: the statistical gates pass at most "
                    f"{pm(worst['pass_stat'], worst['pass_stat_se'])}\\,\\% of noise selections in any cell "
                    f"({worst['preset']}, {worst['years']} years, $K$ = {worst['k']}); standard preset at most "
                    f"{pm(worst_std['pass_stat'], worst_std['pass_stat_se'])}\\,\\%. Single gates leak much more: G1 "
                    f"passes {pm(g1n['pass_G1'], g1n['pass_G1_se'])}\\,\\% and H {pm(g1n['pass_H'], g1n['pass_H_se'])}\\,\\% "
                    f"of best-of-{kmax} noise (10 years).",
         "evidence": "Tab.~\\ref{tab:O2_fpr}; Fig.~\\ref{fig:fig8_fpr_vs_k}, \\ref{fig:fig10_gate_heat}",
         "remediation": "Keep the conjunction of gates; do not relax G4 / G5 to a single-gate screen."},
        {"id": "O-F2", "severity": "High",
         "finding": f"Low power for a genuine but modest edge: with a true Sharpe of 1.0 and $K$ = 64 the statistical "
                    f"gates (standard) pass {pm(p10['pass_stat'], p10['pass_stat_se'])}\\,\\% with 10 training years and "
                    f"{pm(p20['pass_stat'], p20['pass_stat_se'])}\\,\\% with 20; all in-scope gates "
                    f"{pm(p10['pass_all'], p10['pass_all_se'])}\\,\\% and {pm(p20['pass_all'], p20['pass_all_se'])}\\,\\%.",
         "evidence": "Tab.~\\ref{tab:O3_power}; Fig.~\\ref{fig:fig9_power}",
         "remediation": "State the power in the UI next to the presets (a pass is rare even for a real Sharpe of 1); "
                        "consider the lenient preset for long samples only together with H."},
        {"id": "O-F3", "severity": "Medium",
         "finding": f"G5 (PBO) rejects real edges shared by a correlated family: with a true Sharpe of 1.0 and $K$ = 64, "
                    f"G5 passes only {', '.join(pct(v) for v in g5s)}\\,\\% ({' / '.join(map(str, ys))} years, standard), against "
                    f"{', '.join(pct(v) for v in g5n)}\\,\\% under noise: G5 barely separates the two. The planted edge "
                    f"lifts all neighbouring configurations, so the in-sample winner is close to a coin flip out of sample "
                    f"within the grid; PBO measures the stability of the ranking, not the existence of an edge.",
         "evidence": "Tab.~\\ref{tab:O3_power}; Fig.~\\ref{fig:fig10_gate_heat}",
         "remediation": "Document that G5 tests the selection within the grid; consider reporting PBO beside "
                        "a test of the grid's median Sharpe instead of as a hard gate for smooth families."},
        {"id": "O-F4", "severity": "Medium",
         "finding": f"G4 power falls with the search size even for a real edge: with a true Sharpe of 1.0 and 20 years, "
                    f"G4 passes {', '.join(pct(g4[k]['pass_G4']) for k in ks)}\\,\\% for $K$ = "
                    f"{', '.join(str(k) for k in ks)} (standard). $N = K + 1$ counts every tried configuration as an "
                    f"independent trial. The selection's training Sharpe is consistent with strongly correlated trials: under "
                    f"noise the best of 64 has a mean training Sharpe of {n64['train_sharpe']:.2f} (10 years), against "
                    f"{n64['emax_sharpe_iid']:.2f} expected for 64 independent trials. (No effective trial count was "
                    f"estimated in this study.)",
         "evidence": "Fig.~\\ref{fig:fig10_gate_heat}; results file",
         "remediation": "Consider an effective number of trials for correlated families (e.g. from the trials' "
                        "correlation) and record how $N$ was counted."},
        {"id": "O-F5", "severity": "Medium",
         "finding": f"G7 (closed trades) is decided by the rule's trading frequency and the sample length, not by "
                    f"overfitting: the noise selections pass G7 in "
                    f"{', '.join(pct(g7[y]['pass_G7']) for y in ys)}\\,\\% of paths for {', '.join(map(str, ys))} years "
                    f"(standard, mean {trades} trades).",
         "evidence": "Results file (pass\\_G7, trades\\_mean)",
         "remediation": "Scale the minimum trade count with the sample length or the holding period."},
        {"id": "O-F6", "severity": "Medium",
         "finding": f"The holdout gate mostly costs power here: H alone passes {pm(ho['pass_H'], ho['pass_H_se'])}\\,\\% "
                    f"of best-of-64 noise (10 years) and {pm(hs['pass_H'], hs['pass_H_se'])}\\,\\% with a true Sharpe of 1.0 "
                    f"(20 years); 24 months give a Sharpe standard error of about 0.7. H is a separate sample, but its "
                    f"50\\,\\%-of-training clause makes it harder to pass for selections with high training Sharpe ratios: "
                    f"among the selections that pass the statistical gates (20 years, $K$ = 64), H passes "
                    f"{pm(h75['h_given_stat'], h75['h_given_stat_se'])}\\,\\% against {pm(h75['pass_H'], h75['pass_H_se'])}\\,\\% "
                    f"unconditionally at a true Sharpe of 0.75, and {pm(hp['h_given_stat'], hp['h_given_stat_se'])}\\,\\% "
                    f"against {pm(hp['pass_H'], hp['pass_H_se'])}\\,\\% at 1.0. Behind the statistical gates, whose "
                    f"false-positive rate is already near 0, H cuts the power at a true Sharpe of 1.0 (20 years, $K$ = 64) "
                    f"from {pm(hp['pass_stat'], hp['pass_stat_se'])}\\,\\% to {pm(hp['pass_stat_h'], hp['pass_stat_h_se'])}\\,\\%.",
         "evidence": "Tab.~\\ref{tab:O4_holdout}; Fig.~\\ref{fig:fig11_holdout}",
         "remediation": "Treat H as a sanity check, not as evidence; a longer holdout or a pooled out-of-sample "
                        "record adds more."},
        {"id": "O-F7", "severity": "Low",
         "finding": f"SPEC \\S 9.6 calibration: of the best-of-64 noise selections (standard), "
                    f"{_share(cal_min['fail_any'], cal_min['m'])} fail at least one in-scope gate in every cell (claim: "
                    f"$\\ge$ 95\\,\\%). The claim that every best-of-64 noise selection fails G4 or G5 "
                    + (f"holds in all {len(cal)} cells." if not g45_pass else
                       f"fails narrowly: {g45_pass} of {sum(x['m'] for x in cal)} selection(s) passed both G4 and G5 "
                       f"(lowest cell {pct(min(x['fail_g4_or_g5'] for x in cal), 2)}\\,\\% fail G4 or G5); those "
                       f"selection(s) failed another in-scope gate.")
                    + (f" The selected configuration has a grid neighbour in at least {pct(nb)}\\,\\% of the paths of every "
                       f"cell." if nb is not None else ""),
         "evidence": "Tab.~\\ref{tab:O5_calibration}",
         "remediation": "None for the claims; extend the study to G3, the G6 cost / stress parts and G8."},
    ]
    return out


def _share(x: float, m: int) -> str:
    """'all M (95 % lower bound 1 - 3/M)' for a rate of 1, else 'x %'."""
    if x == 1.0:
        return f"all {m} (95\\,\\% lower bound of the rate {100.0 - 300.0 / m:.4g}\\,\\%)"
    return f"{pct(x)}\\,\\%"


def table_overfitting_findings(go: dict) -> str:
    rows = [[f["id"], f["finding"], f"PROPOSED: {f['severity']}", f["evidence"], f["remediation"]]
            for f in overfitting_findings(go)]
    return _table("O6_findings", "Findings register of the gate overfitting study. Severities are PROPOSED and must "
                  "be confirmed by the validator.", _rag("0.75cm", "6.8cm", "1.85cm", "2.2cm", "3.5cm"),
                  ["ID", "Finding", "Severity", "Evidence", "Proposed remediation"], rows, size="\\scriptsize",
                  colsep="3pt")


# ---- write all --------------------------------------------------------------------------

def load(path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_all(results_dir, out_dir, doc: str = "validation") -> List[Path]:
    """Write the tables of ``doc`` (``validation``: T1..T7; ``overfitting``: O1..O6) from
    ``<results_dir>/<study>_full_results.json`` into ``out_dir``."""
    r = Path(results_dir)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if doc == "overfitting":
        go = load(r / "gate_overfitting_full_results.json")
        texts = {"O1_gates_scope": table_go_gates_scope(go), "O2_fpr": table_go_fpr(go),
                 "O3_power": table_go_power(go), "O4_holdout": table_go_holdout(go),
                 "O5_calibration": table_go_calibration(go), "O6_findings": table_overfitting_findings(go)}
        return _write_tables(out, OVERFIT_TABLE_NAMES, texts)
    if doc != "validation":
        raise ValueError(f"unknown document {doc!r}")
    power = load(r / "var_backtest_power_full_results.json")
    horizon = load(r / "horizon_10d_full_results.json")
    ew = load(r / "estimation_window_full_results.json")
    surv = load(r / "survivorship_full_results.json")
    texts = {
        "T1_model_inventory": table_model_inventory(),
        "T2_size": table_size(power),
        "T3_power": table_power(power),
        "T4_estimation_window": table_estimation_window(ew),
        "T5_horizon": table_horizon(horizon),
        "T6_survivorship": table_survivorship(surv),
        "T7_findings": table_findings(power, horizon, ew, surv),
    }
    return _write_tables(out, TABLE_NAMES, texts)


def _write_tables(out: Path, names: Sequence[str], texts: Dict[str, str]) -> List[Path]:
    paths = []
    for name in names:
        p = out / f"{name}.tex"
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(texts[name])
        paths.append(p)
    return paths
