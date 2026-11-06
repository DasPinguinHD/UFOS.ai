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
#: table of the project overview
OVERVIEW_TABLE_NAMES = ("V1_key_results",)
#: tables of the IRRBB report
IRRBB_TABLE_NAMES = ("R1_scenarios", "R2_outlier_test", "R3_assumptions", "R4_findings")
#: tables of the credit risk report
CREDIT_TABLE_NAMES = ("C1_pd_model", "C2_calibration", "C3_ecl", "C4_irb", "C5_assumptions", "C6_findings")

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


# ---- V1: key results of the project overview ----------------------------------------------

def table_key_results(power: dict, horizon: dict, ew: dict, surv: dict, go: dict) -> str:
    """One row per headline result, every number read from the saved full runs."""
    p = {(r["generator"], r["model"], r["window"], r["T"]): r for r in power["rows"]}
    o250 = p[("garch_t6", "oracle", 250, 250)]
    g = p[("garch_t6", "garch_t", 250, 1000)]
    n = p[("garch_t6", "normal", 250, 1000)]
    h = {(r["generator"], r["method"]): r for r in horizon["rows"]}["iid_t5", "sqrt10_garch"]
    e = {(r["model"], r["window"]): r for r in ew["rows"]}
    e500, e1000 = e[("garch_t", 500)], e[("garch_t", 1000)]
    sv = {(r["h0"], r["kappa"], r["threshold"]): r for r in surv["rows"]}[(surv["meta"]["config"]["h0"], 1.0, 0.1)]
    gi = {(r["data"], r["years"], r["signal"], r["k"], r["preset"]): r for r in go["rows"]}
    worst = max((r for r in go["rows"] if r["signal"] == 0.0 and r["preset"] == "standard"),
                key=lambda r: r["pass_stat"])
    p10, p20 = gi[("iid_t", 10, 1.0, 64, "standard")], gi[("iid_t", 20, 1.0, 64, "standard")]
    rows = [
        ["Backtest size", "Kupiec's exact size at T = 250 (nominal 5\\,\\%); exact binomial test",
         f"{pct(o250['exact_size_kupiec'], 2)}\\,\\%; {pct(o250['exact_size_binom'], 2)}\\,\\%", "size / power"],
        ["Estimation risk", "Correct GARCH-t, 250-day window, T = 1000: Kupiec / Z2 rejection rate "
         f"(exact Kupiec size {pct(g['exact_size_kupiec'], 1)}\\,\\%)",
         f"{pm(g['reject_kupiec'], g['se_kupiec'])} / {pm(g['reject_z2'], g['se_z2'])}\\,\\%", "size / power"],
        ["", "Same model, 500-day window: Kupiec; 1000 days: exceedance excess over the true VaR (pp)",
         f"{pm(e500['reject_kupiec'], e500['se_kupiec'])}\\,\\%; "
         f"{round(100 * e1000['exceed_diff'], 3) + 0.0:+.3f} $\\pm$ {pct(e1000['exceed_diff_se'], 3)}",
         "estimation window"],
        ["Clustering", "Static normal VaR on GARCH-t data, T = 1000: cc / Z2 rejection rate",
         f"{pm(n['reject_cc'], n['se_cc'])} / {pm(n['reject_z2'], n['se_z2'])}\\,\\%", "size / power"],
        ["10-day VaR", "$\\sqrt{10}\\times$ GARCH-t one-day VaR on iid $t_5$ data: relative bias",
         f"{100 * h['bias']:+.1f} $\\pm$ {pct(h['bias_se'])}\\,\\%", "10-day horizon"],
        ["Survivorship", "CAGR $\\ge$ 10\\,\\% (hazard strength 1): survivors / counterfactual / full cohort; "
         "selection bias (pp)", f"{pct(sv['naive'])} / {pct(sv['counterfactual'])} / {pct(sv['true'])}\\,\\%; "
                                f"{pm(sv['selection'], sv['selection_se'], 1, 2)}", "survivorship"],
        ["Gate study", "UFOS statistical gates, standard: worst false-positive rate under noise; power at a true "
         "Sharpe of 1, $K$ = 64, 10 / 20 years",
         f"{pm(worst['pass_stat'], worst['pass_stat_se'])}\\,\\%; {pm(p10['pass_stat'], p10['pass_stat_se'])} / "
         f"{pm(p20['pass_stat'], p20['pass_stat_se'])}\\,\\%", "gate overfitting"],
    ]
    return _table("V1_key_results", "Key results (synthetic data with known truth; Monte-Carlo rates $\\pm$ se). "
                  "Every number is read from the saved full runs in \\texttt{studies/results/}.",
                  _rag("1.9cm", "7.0cm", "3.6cm", "2.2cm"), ["Topic", "Result", "Value", "Study"], rows,
                  size="\\footnotesize", colsep="4pt")


# ---- R1..R4: IRRBB -------------------------------------------------------------------------

IRRBB_SCEN = {"parallel_up": "parallel up", "parallel_down": "parallel down", "steepener": "steepener",
              "flattener": "flattener", "short_up": "short up", "short_down": "short down"}


def _ir(res: dict, table: str) -> List[dict]:
    return [r for r in res["rows"] if r["table"] == table]


def _pmr2(x: float, se: float, n: int) -> str:
    """:func:`pmr`, with one more digit when the se would round to 0.0 (e.g. 99.95 $\\pm$ 0.05)."""
    if x not in (0.0, 1.0) and se is not None and 100 * se < 0.1:
        return pm(x, se, 2)
    return pmr(x, se, n)


def _pct0(x: float) -> str:
    return f"{100 * x:.0f}\\,\\%"


def _years(m: float) -> str:
    return f"{m:g} year" + ("" if m == 1 else "s")


def _signed(x: float, d: int = 1) -> str:
    return f"{100 * x:+.{d}f}"


def table_irrbb_scenarios(res: dict) -> str:
    rows = _ir(res, "scenario")
    presets = list(dict.fromkeys(r["preset"] for r in rows))
    idx = {(r["preset"], r["scenario"]): r for r in rows}
    bs = res["meta"]["balance_sheet"]
    body = []
    for s in IRRBB_SCEN:
        line = [IRRBB_SCEN[s]]
        for p in presets:
            r = idx[(p, s)]
            line += [f"{r['delta_eve']:+.1f}", _signed(r["delta_eve_tier1"]), f"{r['delta_nii']:+.2f}",
                     _signed(r["delta_nii_tier1"])]
        body.append(line)
    head = ["Scenario"] + [f"{h}$_{{{p.split('_')[-1][-4:]}}}$" for p in presets
                           for h in ("$\\Delta$EVE", "\\%T1", "$\\Delta$NII", "\\%T1")]
    sz = res["meta"]["shock_presets"]
    return _table("R1_scenarios", "Delta EVE (run-off, equity excluded) and Delta NII (12 months, constant balance "
                  "sheet) of the synthetic reference bank per standardised scenario, in EUR m and in \\% of Tier 1 "
                  f"({bs['tier1']:.0f}); EUR shock sizes (parallel / short / long, bp): "
                  + "; ".join(f"{p.replace('_', ' ')} {v['parallel']:g} / {v['short']:g} / {v['long']:g}"
                              for p, v in sz.items()) + ". Deterministic (no Monte-Carlo error).",
                  "l" + "rrrr" * len(presets), head, body, size="\\scriptsize", colsep="3pt")


def table_irrbb_outlier(res: dict) -> str:
    cfg = res["meta"]["config"]
    pop = {r["preset"]: r for r in _ir(res, "population")}
    body = []
    for p, o in res["meta"]["outlier_tests"].items():
        q = pop[p]
        body.append([esc(p.replace("_", " ")), IRRBB_SCEN[o["eve_worst_scenario"]], pct(o["eve_ratio"]),
                     "yes" if o["eve_outlier"] else "no", IRRBB_SCEN[o["nii_worst_scenario"]], pct(o["nii_ratio"]),
                     "yes" if o["nii_outlier"] else "no", _pmr2(q["outlier_share"], q["outlier_share_se"], q["m"]),
                     _pmr2(q["nii_outlier_share"], q["nii_outlier_share_se"], q["m"])])
    return _table("R2_outlier_test", "Supervisory outlier tests of the reference bank: the largest EVE decline over "
                  f"the six scenarios against {100 * cfg['eve_threshold']:g}\\,\\% of Tier 1, and the largest NII decline "
                  f"over parallel up / down against {100 * cfg['nii_threshold']:g}\\,\\% of Tier 1 (both thresholds are "
                  f"parameters). Last two columns: share of outliers among {cfg['m']} variants of the reference bank "
                  "(notionals $\\times$ U(0.7, 1.3), fixed rates $\\pm$ 50 bp; same NMD rates, betas and deposit floor, "
                  "Tier 1 = 9\\,\\% of assets), \\% $\\pm$ Monte-Carlo se; a rate of 0 or "
                  "100 shows the 95\\,\\% bound of the rule of three in brackets.",
                  "lllllllrr", ["Shocks", "Worst EVE", "\\% T1", "Outlier", "Worst NII", "\\% T1", "Outlier",
                                "Pop.\\ EVE", "Pop.\\ NII"], body, size="\\scriptsize", colsep="3pt")


def table_irrbb_assumptions(res: dict) -> str:
    body = []
    for k, v in sorted(res["meta"]["assumptions"].items()):
        flag = "ASSUMPTION" if "ASSUMPTION" in v else ("verify" if ("verify" in v or "secondary" in v) else "sourced")
        body.append([esc(k), esc(v), flag])
    return _table("R3_assumptions", "Assumptions register: every modelling choice and parameter source the results "
                  "carry (``ASSUMPTION'' = not verified against a primary source in this work; ``verify'' = taken from "
                  "a secondary source).", _rag("3.0cm", "10.6cm", "2.1cm"), ["Item", "Statement and source", "Status"],
                  body, size="\\scriptsize", colsep="3pt")


def irrbb_findings(res: dict) -> List[Dict[str, str]]:
    sc = {(r["preset"], r["scenario"]): r for r in _ir(res, "scenario")}
    o16 = res["meta"]["outlier_tests"]["EUR_BCBS2016"]
    o24 = res["meta"]["outlier_tests"].get("EUR_BCBS2024")
    nm = _ir(res, "nmd")
    lo, hi = min(nm, key=lambda r: -r["worst_eve_tier1"]), max(nm, key=lambda r: -r["worst_eve_tier1"])
    ref = next(r for r in nm if abs(r["core_share"] - 0.7) < 1e-9 and abs(r["avg_maturity"] - 3.0) < 1e-9)
    du = {r["shift_bp"]: r for r in _ir(res, "duration")}
    d200 = du[200.0]
    pop = {r["preset"]: r for r in _ir(res, "population")}
    p16 = pop["EUR_BCBS2016"]
    out = [
        {"id": "R-F1", "severity": "High",
         "finding": f"The reference bank breaches the EVE outlier test: its worst EVE decline is {pct(o16['eve_ratio'])}\\,\\% of "
                    f"Tier 1 under {IRRBB_SCEN[o16['eve_worst_scenario']]} (threshold 15\\,\\%)"
                    + (f"; {pct(o24['eve_ratio'])}\\,\\% with the 2024 EUR shock sizes" if o24 else "")
                    + ". The exposure comes from long fixed-rate assets (10-year mortgages and bonds) funded by short "
                      "liabilities.",
         "evidence": "Tab.~\\ref{tab:R1_scenarios}, \\ref{tab:R2_outlier_test}; Fig.~\\ref{fig:fig14_irrbb_delta}",
         "remediation": "Hedge the long fixed-rate assets (payer swaps at the 5--10-year key rates, Fig.~\\ref{fig:fig15_key_rates})."},
        {"id": "R-F2", "severity": "High",
         "finding": f"The result depends on the non-maturity-deposit assumptions: with the core share and average maturity "
                    f"of the current accounts inside the caps, the worst EVE decline ranges from {pct(-lo['worst_eve_tier1'])}\\,\\% "
                    f"({_pct0(lo['core_share'])} core, {_years(lo['avg_maturity'])}) to {pct(-hi['worst_eve_tier1'])}\\,\\% "
                    f"({_pct0(hi['core_share'])} core, {_years(hi['avg_maturity'])}) of Tier 1; the base case (70\\,\\%, 3 years) "
                    f"gives {pct(-ref['worst_eve_tier1'])}\\,\\%. Only the current accounts (EUR 450\\,m of the 600\\,m non-maturity "
                    f"deposits) vary. The outlier test can be passed or failed by the NMD model alone.",
         "evidence": "Fig.~\\ref{fig:fig16_nmd_heat}",
         "remediation": "Treat the NMD model as a model with its own validation (deposit history, behavioural "
                        "back-testing) and report the sensitivity with every EVE figure."},
        {"id": "R-F3", "severity": "Medium",
         "finding": f"The bank also breaches the NII outlier test ({pct(o16['nii_ratio'])}\\,\\% of Tier 1 under "
                    f"{IRRBB_SCEN[o16['nii_worst_scenario']]}, threshold 5\\,\\%): deposit rates are floored at 0\\,\\% and cannot follow "
                    f"a fall in market rates, while floating-rate loans and reserves reprice down in full. The result "
                    f"depends on the 0\\,\\% deposit floor and the pass-through betas (0.3 current accounts, 0.5 savings). "
                    f"The short-down scenario, which the NII test does not use, is worse "
                    f"({_signed(sc[('EUR_BCBS2016', 'short_down')]['delta_nii_tier1'])}\\,\\%).",
         "evidence": "Tab.~\\ref{tab:R1_scenarios}, \\ref{tab:R2_outlier_test}",
         "remediation": "Review the deposit-floor and pass-through assumptions (beta); monitor NII under short-rate "
                        "scenarios as well."},
        {"id": "R-F4", "severity": "Medium",
         "finding": f"The breaches are robust to $\\pm$30\\,\\% changes in the reference bank's composition (same NMD "
                    f"rates and betas, 0\\,\\% deposit floor, Tier 1 = 9\\,\\% of assets): of {p16['m']} variants, "
                    f"{_pmr2(p16['outlier_share'], p16['outlier_share_se'], p16['m'])}\\,\\% breach the EVE test and "
                    f"{_pmr2(p16['nii_outlier_share'], p16['nii_outlier_share_se'], p16['m'])}\\,\\% the NII test (BCBS 2016 "
                    f"shocks). The NII breach is structural: floored deposits against fully repricing floating-rate loans "
                    f"and reserves.",
         "evidence": "Tab.~\\ref{tab:R2_outlier_test}",
         "remediation": "Treat the variants as a robustness check of the synthetic design, not as a population of "
                        "real banks; for a real bank, analyse the deposit floor and pass-through instead."},
        {"id": "R-F5", "severity": "Low",
         "finding": f"A duration (DV01) approximation misstates Delta EVE for large shocks: at +200 bp it gives "
                    f"{d200['first_order']:.1f} against {d200['full']:.1f} EUR m by full revaluation; adding convexity gives "
                    f"{d200['second_order']:.1f}.",
         "evidence": "Fig.~\\ref{fig:fig17_duration_check}",
         "remediation": "Compute regulatory scenarios by full revaluation; use DV01 / key rates for limits and hedging only."},
        {"id": "R-F6", "severity": "Low",
         "finding": "One parameter set could not be verified against a primary text: the BCBS caps on the core share and "
                    "the average maturity of non-maturity deposits (marked ASSUMPTION in the register). The shock shapes "
                    "and sizes, the floor and its rule for rates below it, and both outlier thresholds are cited to their "
                    "articles.",
         "evidence": "Tab.~\\ref{tab:R3_assumptions}",
         "remediation": "Verify the NMD caps against BCBS SRP31 (and the EU rules) before any use beyond this showcase."},
    ]
    return out


def table_irrbb_findings(res: dict) -> str:
    rows = [[f["id"], f["finding"], f"PROPOSED: {f['severity']}", f["evidence"], f["remediation"]]
            for f in irrbb_findings(res)]
    return _table("R4_findings", "Findings register of the IRRBB study. Severities are PROPOSED and must be confirmed "
                  "by the validator.", _rag("0.75cm", "6.6cm", "1.85cm", "2.4cm", "3.5cm"),
                  ["ID", "Finding", "Severity", "Evidence", "Proposed remediation"], rows, size="\\scriptsize",
                  colsep="3pt")


# ---- C1..C6: credit risk ---------------------------------------------------------------------

CREDIT_TEST_LABELS = {"hosmer_lemeshow": "Hosmer--Lemeshow", "binomial_any_grade": "binomial, any grade",
                      "jeffreys_any_grade": "Jeffreys, any grade"}


def _cr(res: dict, table: str) -> List[dict]:
    return [r for r in res["rows"] if r["table"] == table]


def _num(x: Optional[float], fmt: str = ".3f") -> str:
    return "--" if x is None else format(x, fmt)


def table_credit_pd(res: dict) -> str:
    coef = _cr(res, "coef")
    ss = sorted(_cr(res, "sample_size"), key=lambda r: r["n_train"])
    body = [[esc(r["name"]), f"{r['true']:+.2f}", f"{r['estimate']:+.3f}", f"{r['se']:.3f}", f"{r['z']:+.2f}", ""]
            for r in coef]
    body.append("MIDRULE")
    m = res["meta"]["config"]["m"]
    body.append(["\\textit{N train}", "\\textit{failed}", "\\textit{AUC}", "\\textit{gap to true}",
                 "\\textit{coef.\\ RMSE}", "\\textit{macro: sd / se}"])
    for r in ss:
        body.append([str(r["n_train"]), str(r["fit_failures"]), f"{r['auc']:.3f} $\\pm$ {r['auc_se']:.3f}",
                     f"{r['auc_gap']:+.3f} $\\pm$ {r['auc_gap_se']:.3f}",
                     f"{r['coef_rmse']:.3f} $\\pm$ {r['coef_rmse_se']:.3f}",
                     f"{_num(r['macro_coef_sd'])} / {_num(r['macro_se_mean'])}"])
    n = max(r["n_train"] for r in ss)
    return _table("C1_pd_model", f"Logistic PD model. Top: one fit on {n} borrower-years against the true coefficients "
                  "(standard errors from the MLE information matrix; $z$ = (estimate $-$ true) / se). Bottom: out-of-time "
                  f"quality against the training size (M = {m} replications, $\\pm$ Monte-Carlo se); failed = fits stopped "
                  "with an explicit separation error. Failed fits are EXCLUDED: all averages are conditional on a "
                  "successful fit. Macro: standard deviation of the macro coefficient over the successful fits / mean "
                  "MLE standard error.", "lrrrrr", ["Coefficient", "True", "Estimate", "SE", "$z$", ""], body,
                  size="\\scriptsize", colsep="4pt")


def table_credit_calibration(res: dict) -> str:
    cs = _cr(res, "calibration_size")
    dt = {r["model"]: r for r in _cr(res, "downturn")}
    body = []
    for t in CREDIT_TEST_LABELS:
        rr = {r["rho"]: r for r in cs if r["test"] == t}
        rhos = sorted(rr)
        body.append([CREDIT_TEST_LABELS[t]] + [pmr(rr[x]["reject"], rr[x]["reject_se"], rr[x]["m"]) for x in rhos])
    body.append("MIDRULE")
    body.append(["\\textit{Downturn}", "\\textit{predicted PD: mean (median)}", "\\textit{realised DR}"])
    for k, lab in (("true_pd", "true PD"), ("full", "model with macro"), ("ttc", "model without macro")):
        r = dt[k]
        body.append([lab, f"{pm(r['pred_pd'], r['pred_pd_se'], 2)} ({pct(r['pred_pd_median'], 2)})",
                     pm(r["realised_dr"], r["realised_dr_se"], 2)])
    c = res["meta"]["config"]
    return _table("C2_calibration", f"Top: rejection rates (\\%, level 5\\,\\%) of calibration tests applied to the TRUE PD "
                  f"of one period of {c['calib_n']} borrowers (M = {c['m']}), with independent defaults (asset correlation "
                  f"0) and with asset correlation {res['meta']['rho']:g}. Hosmer--Lemeshow: 10 groups, chi-square with 10 "
                  "degrees of freedom (the true PD is not fitted on the sample; $g-2$ applies only to the development "
                  "sample of a fitted model). The per-grade tests reject if any grade with at "
                  "least 50 borrowers does (a multiple test over about 8 grades). Bottom: a downturn period (macro variable at "
                  f"{c['downturn_macro']:g} sd), \\% $\\pm$ Monte-Carlo se.", "lrr",
                  ["Test", "Correlation 0", f"Correlation {res['meta']['rho']:g}"], body, size="\\scriptsize")


def table_credit_ecl(res: dict) -> str:
    g = {(r["downside_weight"], r["sicr_multiple"]): r for r in _cr(res, "ecl_grid")}
    ws = sorted({k[0] for k in g})
    ms = sorted({k[1] for k in g})
    body = [[f"{100 * w:.0f}\\,\\%"] + [f"{100 * g[(w, m)]['coverage']:.2f} ({100 * g[(w, m)]['stage2_share']:.0f})"
                                         for m in ms] for w in ws]
    return _table("C3_ecl", f"IFRS 9 ECL coverage (\\% of EAD) of the synthetic book of {res['meta']['config']['n_loans']} "
                  "loans by the weight of the downside scenario (rows; upside fixed at 20\\,\\%, base = the rest) and the "
                  "relative SICR threshold (columns); in brackets the share of loans in stage 2 (\\%). The SICR test compares "
                  "the current and the origination 12-month PD on the same basis (both without the scenario overlay), so "
                  "the stage-2 share depends on the threshold only. Deterministic.",
                  "l" + "r" * len(ms), ["Downside weight"] + [f"SICR $\\times${m:g}" for m in ms], body,
                  size="\\scriptsize", colsep="4pt")


def table_credit_irb(res: dict) -> str:
    body = []
    for r in _cr(res, "irb"):
        body.append([esc(r["portfolio"].replace("_", " ")), str(r["names"]), f"{r['k_sum']:.1f}", f"{r['el']:.1f}",
                     f"{r['q999']:.1f} ({r['q999_lo']:.1f}--{r['q999_hi']:.1f})", f"{r['unexpected']:.1f}",
                     f"{r['ratio']:.3f}"])
    return _table("C4_irb", f"IRB capital (sum of K $\\times$ EAD, M = 1 year, no PD floor) against the one-factor loss "
                  f"distribution ({res['meta']['config']['n_sims']:,} draws; 95\\,\\% order-statistic interval of the "
                  "quantile). Total EAD 10\\,000, LGD 45\\,\\%; granular and concentrated portfolios share the PD mix "
                  "0.1--5\\,\\%; the homogeneous ones have PD 1\\,\\%.".replace(",", "\\,", 1), "lrrrrrr",
                  ["Portfolio", "Names", "Sum K", "EL", "q$_{99.9}$ (95\\,\\% CI)", "q$_{99.9}-$EL", "Ratio"], body,
                  size="\\scriptsize", colsep="4pt")


def credit_assumptions(res: dict) -> Dict[str, str]:
    m = res["meta"]
    out = {f"IRB {k}": v for k, v in m["irb_sources"].items()}
    out.update({f"IFRS 9 {k}": v for k, v in m["ifrs9_sources"].items()})
    out["Jeffreys test"] = m["jeffreys_source"]
    out["data"] = (f"synthetic: logit PD with known coefficients, one-factor Gaussian copula with asset correlation "
                   f"{m['rho']:g}, observable AR(1) macro variable (modelling choices, not calibrations)")
    out["master scale"] = "9 grades with PD bounds " + ", ".join(f"{100 * b:g}" for b in m["master_scale"][1:-1]) + " % (synthetic)"
    return out


def table_credit_assumptions(res: dict) -> str:
    body = []
    for k, v in sorted(credit_assumptions(res).items()):
        flag = "ASSUMPTION" if "ASSUMPTION" in v else ("verify" if ("verify" in v or "secondary" in v) else "stated")
        body.append([esc(k), esc(v), flag])
    return _table("C5_assumptions", "Assumptions register of the credit study (``ASSUMPTION'' = not verified against a "
                  "primary source in this work; ``stated'' = a modelling choice or a cited rule).",
                  _rag("3.0cm", "10.6cm", "2.1cm"), ["Item", "Statement and source", "Status"], body,
                  size="\\scriptsize", colsep="3pt")


def credit_findings(res: dict) -> List[Dict[str, str]]:
    cs = {(r["rho"], r["test"]): r for r in _cr(res, "calibration_size")}
    rho = res["meta"]["rho"]
    hl_c, hl_0 = cs[(rho, "hosmer_lemeshow")], cs[(0.0, "hosmer_lemeshow")]
    bi_c, bi_0 = cs[(rho, "binomial_any_grade")], cs[(0.0, "binomial_any_grade")]
    je_c, je_0 = cs[(rho, "jeffreys_any_grade")], cs[(0.0, "jeffreys_any_grade")]
    coef = {r["name"]: r for r in _cr(res, "coef")}
    dt = {r["model"]: r for r in _cr(res, "downturn")}
    ss = sorted(_cr(res, "sample_size"), key=lambda r: r["n_train"])
    big = ss[-1]
    g = {(r["downside_weight"], r["sicr_multiple"]): r for r in _cr(res, "ecl_grid")}
    ws, ms = sorted({k[0] for k in g}), sorted({k[1] for k in g})
    irb = {r["portfolio"]: r for r in _cr(res, "irb")}
    return [
        {"id": "C-F1", "severity": "High",
         "finding": f"Calibration tests that assume independent defaults reject correct PDs in a single period: the "
                    f"Hosmer--Lemeshow test (chi-square with $g = 10$ degrees of freedom, as the true PD is not fitted on "
                    f"the sample) rejects the TRUE PD in {pm(hl_c['reject'], hl_c['reject_se'])}\\,\\% of the periods with "
                    f"asset correlation {rho:g}, against {pm(hl_0['reject'], hl_0['reject_se'])}\\,\\% (its nominal 5\\,\\%) "
                    f"with independent defaults; a one-year default rate carries the systematic factor. The per-grade "
                    f"binomial and Jeffreys tests are a multiple test over about 8 grades: they reject already with "
                    f"independent defaults ({pm(bi_0['reject'], bi_0['reject_se'])} / {pm(je_0['reject'], je_0['reject_se'])}"
                    f"\\,\\%), and correlation adds little ({pm(bi_c['reject'], bi_c['reject_se'])} / "
                    f"{pm(je_c['reject'], je_c['reject_se'])}\\,\\%).",
         "evidence": "Tab.~\\ref{tab:C2_calibration}; Fig.~\\ref{fig:fig20_calibration_drift}",
         "remediation": "Interpret one-period calibration tests with correlation-adjusted critical values or over a "
                        "cycle; control the family-wise level of per-grade tests (e.g. Bonferroni / Holm)."},
        {"id": "C-F2", "severity": "Medium",
         "finding": f"The MLE standard errors ignore the default correlation within a period: over the {big['m'] - big['fit_failures']} "
                    f"replications with {big['n_train']} borrower-years (5 periods) the macro coefficient (true "
                    f"{coef['macro']['true']:+.2f}) has a spread of {big['macro_coef_sd']:.3f} (sd), "
                    f"{big['macro_coef_sd'] / big['macro_se_mean']:.0f} times its mean MLE standard error "
                    f"({big['macro_se_mean']:.3f}): the macro effect is identified from only 5 period-level observations, "
                    f"each shifted by the unobserved systematic factor.",
         "evidence": "Tab.~\\ref{tab:C1_pd_model}",
         "remediation": "Cluster standard errors by period, or estimate macro sensitivities on long histories."},
        {"id": "C-F3", "severity": "High",
         "finding": f"In a downturn (macro at {res['meta']['config']['downturn_macro']:g} sd) a model without the macro "
                    f"variable predicts {pm(dt['ttc']['pred_pd'], dt['ttc']['pred_pd_se'], 2)}\\,\\% against a realised "
                    f"{pm(dt['ttc']['realised_dr'], dt['ttc']['realised_dr_se'], 2)}\\,\\%. The model with the macro variable, "
                    f"estimated from 8 periods, overshoots on average ({pm(dt['full']['pred_pd'], dt['full']['pred_pd_se'], 2)}"
                    f"\\,\\%; true PD {pct(dt['true_pd']['pred_pd'], 2)}\\,\\%), but its median over the replications is "
                    f"{pct(dt['full']['pred_pd_median'], 2)}\\,\\%: the overshoot of the mean is a Jensen effect - the noisy "
                    f"macro coefficient enters through the exponential, so the mean prediction is biased upwards; the median "
                    f"fit is close to the true PD.",
         "evidence": "Tab.~\\ref{tab:C2_calibration}; Fig.~\\ref{fig:fig20_calibration_drift}",
         "remediation": "Monitor the calibration through the cycle; constrain macro sensitivities (expert bounds, longer data)."},
        {"id": "C-F4", "severity": "Medium",
         "finding": f"Small samples cost discrimination and stability: with {ss[0]['n_train']} borrower-years "
                    f"{ss[0]['fit_failures']} of {ss[0]['m']} fits fail with separation, and the successful fits lose "
                    f"{ss[0]['auc_gap']:.3f} AUC against the true PD; with {ss[-1]['n_train']} the gap is "
                    f"{ss[-1]['auc_gap']:.3f}. Failed fits are excluded, so the small-sample averages are conditional on "
                    f"success and flatter the model.",
         "evidence": "Tab.~\\ref{tab:C1_pd_model}; Fig.~\\ref{fig:fig19_pd_sample_size}",
         "remediation": "Pool data or simplify the model for low-default portfolios; never hide failed fits."},
        {"id": "C-F5", "severity": "Medium",
         "finding": f"The ECL depends strongly on two judgements: the coverage ranges from "
                    f"{100 * g[(ws[0], ms[-1])]['coverage']:.2f}\\,\\% (no downside weight, SICR $\\times${ms[-1]:g}) to "
                    f"{100 * g[(ws[-1], ms[0])]['coverage']:.2f}\\,\\% (downside weight {100 * ws[-1]:.0f}\\,\\%, SICR "
                    f"$\\times${ms[0]:g}). The SICR threshold moves the stage-2 share from "
                    f"{100 * g[(ws[0], ms[-1])]['stage2_share']:.0f} to {100 * g[(ws[0], ms[0])]['stage2_share']:.0f}\\,\\% and "
                    f"dominates; the downside weight changes the measurement only (staging compares PDs on the same basis, "
                    f"without the scenario overlay). The stage-2 transfer makes a single loan's provision jump by a multiple.",
         "evidence": "Tab.~\\ref{tab:C3_ecl}; Fig.~\\ref{fig:fig21_ecl_cliff}",
         "remediation": "Disclose the sensitivity to the scenario weights and the SICR threshold; back-test the staging."},
        {"id": "C-F6", "severity": "Medium",
         "finding": f"The IRB capital matches the one-factor 99.9\\,\\% loss of a granular portfolio (ratio "
                    f"{irb['granular']['ratio']:.3f}) but understates a concentrated one: with 30 names the simulated "
                    f"unexpected loss is {irb['concentrated']['ratio']:.2f} times the sum of K.",
         "evidence": "Tab.~\\ref{tab:C4_irb}; Fig.~\\ref{fig:fig22_irb_vs_mc}",
         "remediation": "Add a concentration charge (Pillar 2 / granularity adjustment) for portfolios with few names."},
        {"id": "C-F7", "severity": "Low",
         "finding": "The IRB formula reproduces the illustrative risk weights of Basel II Annex 5 / CRE99 exactly, and the "
                    "PD floor (CRE32.4), the M bounds (CRE32.46) and the CRR-only 1.06 scaling are confirmed; still used as "
                    "recalled: the IFRS 9 paragraph numbers other than B5.5.13 and the ECB form of the Jeffreys test.",
         "evidence": "Tab.~\\ref{tab:C5_assumptions}",
         "remediation": "Verify the remaining IFRS 9 references and the ECB instructions before any use beyond this showcase."},
    ]


def table_credit_findings(res: dict) -> str:
    rows = [[f["id"], f["finding"], f"PROPOSED: {f['severity']}", f["evidence"], f["remediation"]]
            for f in credit_findings(res)]
    return _table("C6_findings", "Findings register of the credit risk study. Severities are PROPOSED and must be "
                  "confirmed by the validator.", _rag("0.75cm", "6.6cm", "1.85cm", "2.4cm", "3.5cm"),
                  ["ID", "Finding", "Severity", "Evidence", "Proposed remediation"], rows, size="\\scriptsize",
                  colsep="3pt")


# ---- write all --------------------------------------------------------------------------

def load(path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_all(results_dir, out_dir, doc: str = "validation") -> List[Path]:
    """Write the tables of ``doc`` (``validation``: T1..T7; ``overfitting``: O1..O6; ``overview``: V1; ``irrbb``: R1..R4; ``credit``: C1..C6) from
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
    if doc == "credit":
        res = load(r / "credit_risk_full_results.json")
        texts = {"C1_pd_model": table_credit_pd(res), "C2_calibration": table_credit_calibration(res),
                 "C3_ecl": table_credit_ecl(res), "C4_irb": table_credit_irb(res),
                 "C5_assumptions": table_credit_assumptions(res), "C6_findings": table_credit_findings(res)}
        return _write_tables(out, CREDIT_TABLE_NAMES, texts)
    if doc == "irrbb":
        res = load(r / "irrbb_scenarios_full_results.json")
        texts = {"R1_scenarios": table_irrbb_scenarios(res), "R2_outlier_test": table_irrbb_outlier(res),
                 "R3_assumptions": table_irrbb_assumptions(res), "R4_findings": table_irrbb_findings(res)}
        return _write_tables(out, IRRBB_TABLE_NAMES, texts)
    if doc == "overview":
        loaded = [load(r / f"{x}_full_results.json") for x in
                  ("var_backtest_power", "horizon_10d", "estimation_window", "survivorship", "gate_overfitting")]
        return _write_tables(out, OVERVIEW_TABLE_NAMES, {"V1_key_results": table_key_results(*loaded)})
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
