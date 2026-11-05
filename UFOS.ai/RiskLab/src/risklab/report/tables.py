"""LaTeX tables for the validation report, generated from the saved study results (no matplotlib needed).

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


# ---- write all --------------------------------------------------------------------------

def load(path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_all(results_dir, out_dir) -> List[Path]:
    """Write T1..T7 from ``<results_dir>/<study>_full_results.json`` into ``out_dir``."""
    r = Path(results_dir)
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
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in TABLE_NAMES:
        p = out / f"{name}.tex"
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(texts[name])
        paths.append(p)
    return paths
