"""Report figures (matplotlib, optional extra ``report``).

Every figure is built from saved synthetic study results (``studies/results/*.json``)
or from a fixed-seed synthetic simulation - never from real data. Each figure
function returns a :class:`FigureSpec` (file stem, caption, written paths).

Reproducibility:
* backend Agg; the style is fixed in code (:data:`STYLE`, applied on top of
  matplotlib's defaults, so a user ``matplotlibrc`` has no effect); DejaVu Sans
  (bundled with matplotlib); fixed figure sizes and dpi;
* PNG: ``metadata={"Software": None}`` (no version string);
* PDF: ``metadata={"CreationDate": None, "Producer": ...}`` with a fixed
  producer string, TrueType fonts embedded (``pdf.fonttype`` 42).
With the same matplotlib / FreeType versions the same inputs give byte-identical
PNG and PDF files (tested). Across matplotlib versions the bytes can differ
(rasterisation and font subsetting change); the plotted data do not.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import matplotlib

matplotlib.use("Agg")

import matplotlib.patches  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from scipy import stats  # noqa: E402

#: Okabe-Ito colour-blind-safe palette.
PALETTE = {"black": "#000000", "orange": "#E69F00", "sky": "#56B4E9", "green": "#009E73",
           "yellow": "#F0E442", "blue": "#0072B2", "vermillion": "#D55E00", "purple": "#CC79A7",
           "grey": "#999999"}
CYCLE = [PALETTE[k] for k in ("blue", "orange", "green", "vermillion", "purple", "sky", "yellow", "black")]
STYLE = {
    "font.family": "DejaVu Sans",
    "font.size": 9,
    "axes.labelsize": 9,
    "axes.titlesize": 9,
    "legend.fontsize": 8,
    "xtick.labelsize": 8,
    "ytick.labelsize": 8,
    "axes.prop_cycle": matplotlib.cycler(color=CYCLE),
    "axes.grid": True,
    "grid.color": "#DDDDDD",
    "grid.linewidth": 0.6,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "figure.dpi": 100,
    "savefig.dpi": 150,
    "pdf.fonttype": 42,
    "svg.hashsalt": "risklab",
    "path.simplify": False,
}
WIDE = (6.5, 3.2)
WIDE_LEGEND = (6.5, 3.8)
TALL = (6.5, 4.2)
PDF_METADATA = {"CreationDate": None, "Producer": "risklab report", "Creator": "risklab report"}
PNG_METADATA = {"Software": None}


@dataclass(frozen=True)
class FigureSpec:
    stem: str
    caption: str
    paths: Tuple[Path, ...]


def _style():
    """A context with matplotlib's defaults plus :data:`STYLE` (ignores any user rcParams)."""
    matplotlib.rcdefaults()
    return matplotlib.rc_context(STYLE)


def _save(fig, out_dir: Path, stem: str) -> Tuple[Path, ...]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    png, pdf = out_dir / f"{stem}.png", out_dir / f"{stem}.pdf"
    fig.savefig(png, format="png", metadata=PNG_METADATA)
    fig.savefig(pdf, format="pdf", metadata=PDF_METADATA)
    plt.close(fig)
    return png, pdf


def load_results(path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ---- 1. exceedance plot (fixed-seed simulation) ---------------------------------------

def exceedance_data(seed: int = 20261109, T: int = 1000, long_window: int = 1000, short_window: int = 1000,
                    alpha: float = 0.99) -> dict:
    """One GARCH-t6 path: losses, static-normal VaR and GARCH-t VaR (both with a 1000-day window by default,
    so that the comparison is like for like: only the model differs)."""
    from risklab.data import garch11
    from risklab.engine import walk_forward
    from risklab.models import GarchTModel, NormalModel

    s = garch11(long_window + T, 2e-6, 0.08, 0.90, innovations="t", nu=6.0, seed=seed)
    g = walk_forward(s.losses, GarchTModel, long_window, [alpha], refit_every=50)
    n = walk_forward(s.losses[long_window - short_window:], NormalModel, short_window, [alpha])
    return {"loss": np.asarray(g.loss), "var_garch": np.asarray(g.var[alpha]), "var_normal": np.asarray(n.var[alpha]),
            "alpha": alpha, "long_window": long_window, "short_window": short_window, "seed": seed}


def fig_exceedances(data: dict, out_dir, stem: str = "fig1_exceedances") -> FigureSpec:
    loss, vg, vn = data["loss"], data["var_garch"], data["var_normal"]
    days = np.arange(loss.shape[0])
    with _style():
        fig, ax = plt.subplots(figsize=WIDE_LEGEND)
        ax.plot(days, 100 * loss, color=PALETTE["grey"], lw=0.5, label="daily loss")
        ax.plot(days, 100 * vn, color=PALETTE["orange"], lw=1.0, label=f"static normal VaR ({data['short_window']}-day window)")
        ax.plot(days, 100 * vg, color=PALETTE["blue"], lw=1.0, label=f"GARCH-t VaR ({data['long_window']}-day window)")
        en, eg = loss > vn, loss > vg
        ax.scatter(days[en], 100 * loss[en], s=14, marker="o", facecolors="none", edgecolors=PALETTE["orange"],
                   lw=0.8, label=f"exceedance, normal ({int(en.sum())})")
        ax.scatter(days[eg], 100 * loss[eg], s=10, marker="x", color=PALETTE["blue"], lw=0.8,
                   label=f"exceedance, GARCH-t ({int(eg.sum())})")
        ax.set_xlabel("forecast day")
        ax.set_ylabel("loss (% of portfolio value)")
        ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.18), ncol=3, frameon=False, fontsize=7)
        fig.tight_layout()
        paths = _save(fig, out_dir, stem)
    same = data["short_window"] == data["long_window"]
    caption = (f"One synthetic GARCH(1,1)-t6 path (seed {data['seed']}, {loss.shape[0]} forecast days): daily losses, "
               f"the {100 * data['alpha']:g}\\,\\% VaR of a static normal model ({data['short_window']}-day window) and of "
               f"GARCH(1,1)-t ({data['long_window']}-day window), and their exceedances "
               f"(normal {int(en.sum())}, GARCH-t {int(eg.sum())}; expected {loss.shape[0] * (1 - data['alpha']):g})."
               + (" Both models use the same estimation window, so only the model differs. One path only: the counts "
                  "illustrate clustering, they are not a test." if same else ""))
    return FigureSpec(stem, caption, paths)


# ---- 2. rejection rates by model and test (power study) -------------------------------

POWER_MODELS = (("normal", 250, "static normal"), ("hs", 250, "static HS"), ("ewma_t", 250, "EWMA-t"),
                ("garch_t", 250, "GARCH-t (250)"), ("fhs", 250, "FHS (250)"),
                ("garch_t", 1000, "GARCH-t (1000)"), ("fhs", 1000, "FHS (1000)"))
TEST_LABELS = (("kupiec", "Kupiec"), ("binom", "binomial"), ("cc", "cc"), ("z2", "ES Z2"))


def _power_rows(results: dict, generator: str, T: int):
    rows = {(r["model"], r["window"]): r for r in results["rows"] if r["generator"] == generator and r["T"] == T}
    oracle = rows.get(("oracle", results["meta"]["config"]["window"]))
    return rows, oracle


def fig_rejection_rates(power: dict, out_dir, stem: str = "fig2_rejection_rates", generator: str = "garch_t6",
                        T: int = 1000) -> FigureSpec:
    rows, oracle = _power_rows(power, generator, T)
    models = [m for m in POWER_MODELS if (m[0], m[1]) in rows]
    refs = {"kupiec": oracle["exact_size_kupiec"], "binom": oracle["exact_size_binom"],
            "cc": oracle["reject_cc"], "z2": oracle["reject_z2"]}
    width = 0.8 / len(models)
    with _style():
        fig, ax = plt.subplots(figsize=TALL)
        for i, (key, w, label) in enumerate(models):
            r = rows[(key, w)]
            x = np.arange(len(TEST_LABELS)) + (i - (len(models) - 1) / 2) * width
            vals = [100 * r[f"reject_{t}"] for t, _ in TEST_LABELS]
            errs = [100 * 1.96 * r[f"se_{t}"] for t, _ in TEST_LABELS]
            ax.bar(x, vals, width * 0.95, yerr=errs, capsize=1.5, label=label, color=CYCLE[i % len(CYCLE)],
                   error_kw={"lw": 0.7})
        for j, (t, _) in enumerate(TEST_LABELS):
            ax.hlines(100 * refs[t], j - 0.45, j + 0.45, colors=PALETTE["black"], linestyles="--", lw=1.0,
                      label="reference size" if j == 0 else None)
        ax.set_xticks(np.arange(len(TEST_LABELS)), [lab for _, lab in TEST_LABELS])
        ax.set_ylabel("rejection rate at 5 % (%)")
        ax.legend(ncol=4, frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.1))
        fig.tight_layout()
        paths = _save(fig, out_dir, stem)
    m = power["meta"]["config"]["m"]
    caption = (f"Rejection rates of the VaR tests (Kupiec, exact binomial, Christoffersen cc; 99\\,\\%) and the "
               f"Acerbi--Sz\\'ekely Z2 ES test (97.5\\,\\%) on synthetic GARCH(1,1)-t6 data, T = {T}, M = {m} "
               f"replications; error bars $\\pm 1.96$ se. Dashed lines: the reference size of each test (exact size "
               f"for Kupiec and binomial, oracle rate for cc and Z2). Numbers in brackets: estimation window in days.")
    return FigureSpec(stem, caption, paths)


# ---- 3. estimation-window curve --------------------------------------------------------

def fig_estimation_window(ew: dict, out_dir, stem: str = "fig3_estimation_window") -> FigureSpec:
    rows = [r for r in ew["rows"] if r["model"] != "oracle"]
    ref = next(r for r in ew["rows"] if r["model"] == "oracle")
    alpha = ew["meta"]["config"]["alpha"]
    with _style():
        fig, (a1, a2) = plt.subplots(1, 2, figsize=WIDE)
        for i, model in enumerate(("garch_t", "fhs")):
            rr = sorted((r for r in rows if r["model"] == model), key=lambda r: r["window"])
            if not rr:
                continue
            w = [r["window"] for r in rr]
            col = CYCLE[i]
            a1.errorbar(w, [100 * r["exceed_rate"] for r in rr], yerr=[100 * 1.96 * r["exceed_se"] for r in rr],
                        marker="o", ms=3, capsize=2, color=col, label=model.replace("garch_t", "GARCH-t").replace("fhs", "FHS"))
            for t, ls in (("kupiec", "-"), ("z2", ":")):
                a2.errorbar(w, [100 * r[f"reject_{t}"] for r in rr], yerr=[100 * 1.96 * r[f"se_{t}"] for r in rr],
                            marker="o", ms=3, capsize=2, color=col, ls=ls,
                            label=f"{'GARCH-t' if model == 'garch_t' else 'FHS'}, {'Kupiec' if t == 'kupiec' else 'Z2'}")
        a1.axhline(100 * (1 - alpha), color=PALETTE["black"], ls="--", lw=1.0, label="target")
        a1.axhline(100 * ref["exceed_rate"], color=PALETTE["grey"], ls=":", lw=1.0, label="true VaR (oracle)")
        a2.axhline(100 * ref["ref_kupiec"], color=PALETTE["black"], ls="--", lw=1.0, label="Kupiec exact size")
        a2.axhline(100 * ref["ref_z2"], color=PALETTE["grey"], ls="--", lw=1.0, label="Z2 oracle rate")
        a1.set_xlabel("estimation window (days)")
        a2.set_xlabel("estimation window (days)")
        a1.set_ylabel("exceedance rate (%)")
        a2.set_ylabel("rejection rate at 5 % (%)")
        a1.legend(frameon=False)
        a2.legend(frameon=False, fontsize=7)
        fig.tight_layout()
        paths = _save(fig, out_dir, stem)
    c = ew["meta"]["config"]
    caption = (f"Estimation risk against the window length: correctly specified GARCH(1,1)-t and FHS on synthetic "
               f"GARCH-t6 data, T = {c['T']} (the same forecast days for every window), M = {c['m']}. Left: exceedance "
               f"rate of the {100 * alpha:g}\\,\\% VaR, with the target and the oracle rate (the left axis does not "
               f"start at 0); right: Kupiec and Z2 rejection rates with their reference sizes. Error bars $\\pm 1.96$ se.")
    return FigureSpec(stem, caption, paths)


# ---- 4. traffic-light zone shares ------------------------------------------------------

def fig_traffic_light(power: dict, out_dir, stem: str = "fig4_traffic_light", generator: str = "garch_t6",
                      T: int = 1000) -> FigureSpec:
    rows, oracle = _power_rows(power, generator, T)
    models = [("oracle", power["meta"]["config"]["window"], "true VaR")] + [m for m in POWER_MODELS if (m[0], m[1]) in rows]
    with _style():
        fig, ax = plt.subplots(figsize=WIDE)
        x = np.arange(len(models))
        bottom = np.zeros(len(models))
        for zone, col in (("green", PALETTE["green"]), ("yellow", PALETTE["yellow"]), ("red", PALETTE["vermillion"])):
            vals = np.array([100 * rows[(k, w)][f"tl_{zone}"] for k, w, _ in models])
            ax.bar(x, vals, 0.6, bottom=bottom, color=col, edgecolor="white", lw=0.5, label=zone)
            bottom += vals
        ax.set_xticks(x, [lab for _, _, lab in models], rotation=20, ha="right")
        ax.set_ylabel("share of replications (%)")
        ax.set_ylim(0, 100)
        ax.legend(ncol=3, frameon=False, loc="upper center", bbox_to_anchor=(0.5, 1.12))
        fig.tight_layout()
        paths = _save(fig, out_dir, stem)
    caption = (f"Basel traffic-light zones (last 250 forecast days, 99\\,\\% VaR) per model on synthetic GARCH(1,1)-t6 "
               f"data, T = {T}, M = {power['meta']['config']['m']}. Numbers in brackets: estimation window in days.")
    return FigureSpec(stem, caption, paths)


# ---- 5. 10-day horizon -----------------------------------------------------------------

METHOD_LABELS = {"sqrt10_normal": "√10 × normal", "sqrt10_garch": "√10 × GARCH-t",
                 "garch_mc": "GARCH-t Monte Carlo", "bootstrap": "block bootstrap"}
DATA_LABELS = {"iid_normal": "iid normal", "iid_t5": "iid t5", "garch_t6": "GARCH-t6"}


def fig_horizon(horizon: dict, out_dir, stem: str = "fig5_horizon") -> FigureSpec:
    rows = horizon["rows"]
    gens = list(dict.fromkeys(r["generator"] for r in rows))
    methods = list(dict.fromkeys(r["method"] for r in rows))
    width = 0.8 / len(methods)
    with _style():
        fig, (a1, a2) = plt.subplots(1, 2, figsize=WIDE)
        for i, m in enumerate(methods):
            rr = [next(r for r in rows if r["generator"] == g and r["method"] == m) for g in gens]
            x = np.arange(len(gens)) + (i - (len(methods) - 1) / 2) * width
            a1.bar(x, [100 * r["bias"] for r in rr], width * 0.95, yerr=[100 * 1.96 * r["bias_se"] for r in rr],
                   capsize=1.5, color=CYCLE[i], label=METHOD_LABELS.get(m, m), error_kw={"lw": 0.7})
            a2.bar(x, [100 * r["rmse"] for r in rr], width * 0.95, color=CYCLE[i], label=METHOD_LABELS.get(m, m))
        for a in (a1, a2):
            a.set_xticks(np.arange(len(gens)), [DATA_LABELS.get(g, g) for g in gens])
        a1.axhline(0, color=PALETTE["black"], lw=0.8)
        a1.set_ylabel("relative bias of the 10-day VaR (%)")
        a2.set_ylabel("relative RMSE (%)")
        a2.legend(frameon=False, fontsize=7)
        fig.tight_layout()
        paths = _save(fig, out_dir, stem)
    c = horizon["meta"]["config"]
    caption = (f"10-day {100 * c['alpha']:g}\\,\\% VaR against the true 10-day VaR: relative bias ($\\pm 1.96$ se) and "
               f"relative RMSE per method and synthetic data set; {c['window']}-day window, M = {c['m']}.")
    return FigureSpec(stem, caption, paths)


# ---- 6. QQ plot of GARCH-t residuals (fixed-seed simulation) ---------------------------

def qq_data(seed: int = 20261110, n: int = 1000) -> dict:
    from risklab.data import garch11
    from risklab.models.garch import fit_garch11, variance_path

    s = garch11(n, 2e-6, 0.08, 0.90, innovations="t", nu=6.0, seed=seed)
    p = fit_garch11(s.losses, "t")
    h = variance_path(p, s.losses, float(np.var(s.losses)))[:-1]
    z = np.sort((s.losses - p.mu) / np.sqrt(h))
    probs = (np.arange(1, n + 1) - 0.5) / n
    theo = stats.t.ppf(probs, p.nu) * np.sqrt((p.nu - 2) / p.nu)
    return {"z": z, "theoretical": theo, "nu": p.nu, "seed": seed, "n": n}


def fig_qq(data: dict, out_dir, stem: str = "fig6_qq_residuals") -> FigureSpec:
    with _style():
        fig, ax = plt.subplots(figsize=(3.6, 3.4))
        lim = float(max(np.abs(data["z"]).max(), np.abs(data["theoretical"]).max())) * 1.05
        ax.plot([-lim, lim], [-lim, lim], color=PALETTE["black"], lw=0.8, ls="--", label="45° line")
        ax.scatter(data["theoretical"], data["z"], s=5, color=PALETTE["blue"], label="residuals")
        ax.set_xlabel(f"unit-variance t quantile (nu = {data['nu']:.2f})")
        ax.set_ylabel("standardised residual")
        ax.set_xlim(-lim, lim)
        ax.set_ylim(-lim, lim)
        ax.legend(frameon=False, loc="upper left")
        fig.tight_layout()
        paths = _save(fig, out_dir, stem)
    caption = (f"QQ plot of the standardised residuals of a GARCH(1,1)-t fit to one synthetic GARCH-t6 path "
               f"(seed {data['seed']}, n = {data['n']}) against the fitted unit-variance t "
               f"($\\hat\\nu$ = {data['nu']:.2f}); plotting positions $(i - 0.5)/n$.")
    return FigureSpec(stem, caption, paths)


# ---- 7. survivorship -------------------------------------------------------------------

def fig_survivorship(surv: dict, out_dir, stem: str = "fig7_survivorship", kappa: float = 1.0,
                     threshold: float = 0.10) -> FigureSpec:
    rows = [r for r in surv["rows"] if r["h0"] > 0]
    sel = sorted((r for r in rows if abs(r["kappa"] - kappa) < 1e-12), key=lambda r: r["threshold"])
    with _style():
        fig, (a1, a2) = plt.subplots(1, 2, figsize=WIDE)
        g = [100 * r["threshold"] for r in sel]
        a1.plot(g, [100 * r["naive"] for r in sel], marker="o", ms=3, color=PALETTE["vermillion"],
                label="naive (survivors only)")
        a1.plot(g, [100 * r["counterfactual"] for r in sel], marker="^", ms=3, color=PALETTE["green"],
                label="counterfactual (all firms, had they survived)")
        a1.plot(g, [100 * r["true"] for r in sel], marker="s", ms=3, color=PALETTE["blue"],
                label="full cohort (exited = miss)")
        a1.set_xlabel("10-year CAGR threshold (%)")
        a1.set_ylabel("base rate (% of firms)")
        a1.legend(frameon=False, fontsize=7)
        rr = sorted((r for r in rows if abs(r["threshold"] - threshold) < 1e-12), key=lambda r: r["kappa"])
        k = [r["kappa"] for r in rr]
        for key, col, mk, lab in (("bias", PALETTE["black"], "o", "total (naive - full cohort)"),
                                  ("selection", PALETTE["vermillion"], "^", "selection (naive - counterfactual)"),
                                  ("attrition", PALETTE["blue"], "s", "attrition (counterfactual - full cohort)")):
            a2.errorbar(k, [100 * r[key] for r in rr], yerr=[100 * 1.96 * r[f"{key}_se"] for r in rr], marker=mk,
                        ms=3, capsize=2, color=col, label=lab)
        a2.axhline(0, color=PALETTE["grey"], lw=0.8)
        a2.set_xticks(k)
        a2.set_xlabel("hazard strength kappa")
        a2.set_ylabel(f"bias at CAGR >= {100 * threshold:g} % (pp)")
        a2.legend(frameon=False, fontsize=7)
        fig.tight_layout()
        paths = _save(fig, out_dir, stem)
    c = surv["meta"]["config"]
    caption = (f"Survivorship bias in 10-year growth base rates, synthetic universe of {c['n_firms']} firms "
               f"(M = {c['m']} cohorts, base hazard h0 = {c['h0']:g}). Left: the naive, counterfactual and full-cohort "
               f"base rates for kappa = {kappa:g}. Right: at a CAGR threshold of {100 * threshold:g}\\,\\%, the total "
               f"bias and its parts against the hazard strength: selection (survivors are not a random sample) "
               f"and attrition (exited firms count as misses). Error bars $\\pm 1.96$ se (Monte-Carlo noise only, "
               f"mostly smaller than the markers); the size depends on the assumed hazard.")
    return FigureSpec(stem, caption, paths)


# ---- 8-11. overfitting study of the UFOS validation gates ---------------------------------

PRESET_STYLE = {"lenient": (PALETTE["green"], "o"), "standard": (PALETTE["blue"], "s"),
                "strict": (PALETTE["vermillion"], "^")}
GATE_LIST = ("G1", "G2", "G4", "G5", "G6", "G7", "H")
GATE_COLOURS = dict(zip(GATE_LIST, (PALETTE["blue"], PALETTE["orange"], PALETTE["green"], PALETTE["vermillion"],
                                    PALETTE["purple"], PALETTE["sky"], PALETTE["black"])))


def _go_idx(go: dict) -> dict:
    return {(r["data"], r["years"], r["signal"], r["k"], r["preset"]): r for r in go["rows"]}


def fig_fpr_vs_k(go: dict, out_dir, stem: str = "fig8_fpr_vs_k", data: str = "iid_t", years: int = 10) -> FigureSpec:
    idx = _go_idx(go)
    c = go["meta"]["config"]
    ks = c["ks"]
    with _style():
        fig, (a1, a2) = plt.subplots(1, 2, figsize=WIDE_LEGEND)
        for g in GATE_LIST:
            rr = [idx[(data, years, 0.0, k, "standard")] for k in ks]
            a1.plot(ks, [100 * r[f"pass_{g}"] for r in rr], marker="o", ms=3, color=GATE_COLOURS[g], label=g)
        a1.set_xscale("log", base=2)
        a1.set_xticks(ks)
        a1.set_xticklabels([str(k) for k in ks])
        a1.set_xlabel("configurations tried K")
        a1.set_ylabel("pass rate under noise (%)")
        a1.set_title(f"single gates, standard, {years} years")
        a1.set_ylim(bottom=0)
        a1.legend(frameon=False, fontsize=7, ncol=2)
        top = 0.0
        for preset, (col, mk) in PRESET_STYLE.items():
            for d, ls in (("iid_t", "-"), ("garch_t", "--")):
                rr = [idx[(d, years, 0.0, k, preset)] for k in ks]
                top = max([top] + [100 * (r["pass_stat"] + 1.96 * r["pass_stat_se"]) for r in rr])
                a2.errorbar(ks, [100 * r["pass_stat"] for r in rr], yerr=[100 * 1.96 * r["pass_stat_se"] for r in rr],
                            marker=mk, ms=3, capsize=2, color=col, ls=ls,
                            label=f"{preset}, {'iid' if d == 'iid_t' else 'GARCH'}")
        a2.set_xscale("log", base=2)
        a2.set_xticks(ks)
        a2.set_xticklabels([str(k) for k in ks])
        a2.set_xlabel("configurations tried K")
        a2.set_ylabel("false-positive rate (%)")
        a2.set_title(f"statistical gates together, {years} years")
        a2.set_ylim(0, max(5.0, 1.1 * top))
        a2.legend(frameon=False, fontsize=7)
        fig.tight_layout()
        paths = _save(fig, out_dir, stem)
    caption = (f"Pass rates of the best of K configurations under noise (true Sharpe 0), {years} training years, "
               f"M = {c['m']} paths per cell. Left: each gate alone (standard preset, iid $t_5$ data; Monte-Carlo se at "
               f"most {100 * 0.5 / np.sqrt(c['m']):.1f} percentage points). Right: the statistical gates together (G1, G2, G4, G5, G6 neighbours) per preset; solid iid, dashed "
               f"GARCH, error bars $\\pm 1.96$ se.")
    return FigureSpec(stem, caption, paths)


def fig_power(go: dict, out_dir, stem: str = "fig9_power", data: str = "iid_t", k: int = 64) -> FigureSpec:
    idx = _go_idx(go)
    c = go["meta"]["config"]
    sig = c["signals"]
    ys = c["years"]
    with _style():
        fig, axes = plt.subplots(1, len(ys), figsize=WIDE_LEGEND, sharey=True)
        for ax, y in zip(np.atleast_1d(axes), ys):
            for preset, (col, mk) in PRESET_STYLE.items():
                rr = [idx[(data, y, s, k, preset)] for s in sig]
                ax.errorbar(sig, [100 * r["pass_stat"] for r in rr], yerr=[100 * 1.96 * r["pass_stat_se"] for r in rr],
                            marker=mk, ms=3, capsize=2, color=col, label=f"stat., {preset}")
            rr = [idx[(data, y, s, k, "standard")] for s in sig]
            ax.errorbar(sig, [100 * r["pass_all"] for r in rr], yerr=[100 * 1.96 * r["pass_all_se"] for r in rr],
                        marker="x", ms=3, capsize=2, color=PALETTE["black"], ls=":", label="all in scope, standard")
            ax.set_title(f"{y} training years")
            ax.set_xlabel("true Sharpe a year")
            ax.set_ylim(-3, 103)
        np.atleast_1d(axes)[0].set_ylabel("pass rate (%)")
        np.atleast_1d(axes)[0].legend(frameon=False, fontsize=6.5, loc="upper left")
        fig.tight_layout()
        paths = _save(fig, out_dir, stem)
    caption = (f"Power curves: pass rate of the best of K = {k} configurations against the true (gross) Sharpe ratio "
               f"of the planted configuration, iid $t_5$ data, per training length. Coloured: the statistical gates "
               f"per preset; dotted black: all in-scope gates including G7 (standard). The value at Sharpe 0 is the "
               f"false-positive rate. M = {c['m']} paths per point, error bars $\\pm 1.96$ se.")
    return FigureSpec(stem, caption, paths)


def fig_gate_heat(go: dict, out_dir, stem: str = "fig10_gate_heat", data: str = "iid_t",
                  signals: Sequence[float] = (0.0, 1.0), preset: str = "standard") -> FigureSpec:
    idx = _go_idx(go)
    c = go["meta"]["config"]
    cols = [(y, k) for y in c["years"] for k in c["ks"]]
    with _style():
        fig, axes = plt.subplots(len(signals), 1, figsize=(6.5, 2.6 * len(signals) + 0.4), layout="constrained")
        for ax, sr in zip(np.atleast_1d(axes), signals):
            Z = np.array([[100 * (1 - idx[(data, y, sr, k, preset)][f"pass_{g}"]) for (y, k) in cols] for g in GATE_LIST])
            im = ax.imshow(Z, cmap="Greys", vmin=0, vmax=100, aspect="auto")
            for i in range(Z.shape[0]):
                for j in range(Z.shape[1]):
                    ax.text(j, i, f"{Z[i, j]:.0f}", ha="center", va="center", fontsize=6.5,
                            color="white" if Z[i, j] > 55 else "black")
            ax.set_yticks(range(len(GATE_LIST)))
            ax.set_yticklabels(GATE_LIST)
            ax.set_xticks(range(len(cols)))
            ax.set_xticklabels([f"{y}y\nK={k}" for (y, k) in cols], fontsize=6.5)
            for b in range(1, len(c["years"])):
                ax.axvline(b * len(c["ks"]) - 0.5, color="white", lw=2)
            ax.grid(False)
            ax.set_title(f"rejection rate (%), true Sharpe {sr:g}, {preset} preset")
        fig.colorbar(im, ax=list(np.atleast_1d(axes)), fraction=0.025, pad=0.01)
        paths = _save(fig, out_dir, stem)
    caption = (f"Per-gate rejection rates (\\%) of the selected configuration, iid $t_5$ data, {preset} preset, by "
               f"training length (years) and search size K. Top: noise (a rejection is correct); bottom: a planted "
               f"Sharpe of {signals[-1]:g} (a rejection is a miss). M = {c['m']} paths per cell; the Monte-Carlo se is at "
               f"most {100 * 0.5 / np.sqrt(c['m']):.1f} percentage points.")
    return FigureSpec(stem, caption, paths)


def fig_holdout(go: dict, out_dir, stem: str = "fig11_holdout", data: str = "iid_t", k: int = 64,
                preset: str = "standard") -> FigureSpec:
    idx = _go_idx(go)
    c = go["meta"]["config"]
    sig = c["signals"]
    ys = c["years"]
    with _style():
        fig, (a1, a2) = plt.subplots(1, 2, figsize=WIDE_LEGEND)
        y_mid = ys[-1]
        rr = [idx[(data, y_mid, s, k, preset)] for s in sig]
        for key, lab, col, mk in (("pass_H", "P(H)", PALETTE["black"], "o"),
                                  ("h_given_g4", "P(H | G4)", PALETTE["green"], "s"),
                                  ("h_given_stat", "P(H | stat. gates)", PALETTE["blue"], "^")):
            xs = [s for s, r in zip(sig, rr) if r[key] is not None]
            a1.errorbar(xs, [100 * r[key] for r in rr if r[key] is not None],
                        yerr=[100 * 1.96 * r[key + "_se"] for r in rr if r[key] is not None],
                        marker=mk, ms=3, capsize=2, color=col, label=lab)
        a1.set_xlabel("true Sharpe a year")
        a1.set_ylabel("holdout pass rate (%)")
        a1.set_title(f"{y_mid} years, K = {k}, {preset}")
        a1.set_ylim(-3, 103)
        a1.legend(frameon=False, fontsize=7)
        for y, ls in zip(ys, ("-", "--", ":")):
            r2 = [idx[(data, y, s, k, preset)] for s in sig]
            a2.plot(sig, [100 * (r["pass_stat"] - r["pass_stat_h"]) for r in r2], color=PALETTE["vermillion"], ls=ls,
                    marker="o", ms=3, label=f"{y} years")
        a2.set_xlabel("true Sharpe a year")
        a2.set_ylabel("removed by H (pp of all paths)")
        a2.set_title("stat. gates minus stat. gates + H")
        a2.legend(frameon=False, fontsize=7)
        fig.tight_layout()
        paths = _save(fig, out_dir, stem)
    caption = (f"Value of the holdout gate H (24 months, evaluated once), iid $t_5$ data, best of K = {k}, {preset} "
               f"preset. Left: H pass rate unconditionally and among the paths that passed G4 or all statistical gates "
               f"(points with no passing path are omitted). Right: the share of all paths that pass the statistical "
               f"gates but fail H: under noise this is the holdout's gain, under a real edge its cost. M = {c['m']} paths "
               f"per point, error bars $\\pm 1.96$ se (conditional rates: on the passing paths only).")
    return FigureSpec(stem, caption, paths)


# ---- 18-22. credit risk -------------------------------------------------------------------

def _cr(res: dict, table: str) -> List[dict]:
    return [r for r in res["rows"] if r["table"] == table]


def fig_cap_roc(res: dict, out_dir, stem: str = "fig18_cap_roc") -> FigureSpec:
    cap = _cr(res, "cap")
    summ = _cr(res, "cap_summary")[0]
    with _style():
        fig, ax = plt.subplots(figsize=(4.8, 3.6))
        for label, col, ls, name in (("perfect", PALETTE["grey"], ":", "perfect model"),
                                     ("true_pd", PALETTE["green"], "--", f"true PD (AUC {summ['auc_true']:.3f})"),
                                     ("model", PALETTE["blue"], "-", f"fitted model (AUC {summ['auc']:.3f})")):
            rr = [r for r in cap if r["model"] == label]
            ax.plot([100 * r["share"] for r in rr], [100 * r["captured"] for r in rr], color=col, ls=ls, label=name)
        ax.plot([0, 100], [0, 100], color=PALETTE["black"], lw=0.8, label="random")
        ax.set_xlabel("share of borrowers, riskiest first (%)")
        ax.set_ylabel("share of defaults captured (%)")
        ax.legend(frameon=False, fontsize=7, loc="lower right")
        fig.tight_layout()
        paths = _save(fig, out_dir, stem)
    caption = (f"Cumulative accuracy profile out of time (3 later periods) of the logistic PD model fitted on "
               f"{summ['n_train']} borrower-years, against the true PD and a perfect model (synthetic data). The model "
               f"reaches the discrimination of the true PD (Gini {summ['gini']:.3f}); the true PD itself is far from "
               f"perfect because defaults are random given the PD.")
    return FigureSpec(stem, caption, paths)


def fig_pd_sample_size(res: dict, out_dir, stem: str = "fig19_pd_sample_size") -> FigureSpec:
    rows = sorted(_cr(res, "sample_size"), key=lambda r: r["n_train"])
    n = [r["n_train"] for r in rows]
    m = res["meta"]["config"]["m"]
    with _style():
        fig, (a1, a2) = plt.subplots(1, 2, figsize=WIDE)
        a1.errorbar(n, [r["auc_gap"] for r in rows], yerr=[1.96 * r["auc_gap_se"] for r in rows], marker="o", ms=3,
                    capsize=2, color=PALETTE["blue"])
        a1.axhline(0, color=PALETTE["grey"], lw=0.8)
        a1.set_xscale("log")
        a1.set_xlabel("training borrower-years")
        a1.set_ylabel("AUC(true PD) - AUC(model)")
        a2.errorbar(n, [r["coef_rmse"] for r in rows], yerr=[1.96 * r["coef_rmse_se"] for r in rows], marker="s",
                    ms=3, capsize=2, color=PALETTE["vermillion"])
        a2.set_xscale("log")
        a2.set_xlabel("training borrower-years")
        a2.set_ylabel("coefficient RMSE")
        fig.tight_layout()
        paths = _save(fig, out_dir, stem)
    fails = ", ".join(f"{r['fit_failures']} at {r['n_train']}" for r in rows if r["fit_failures"])
    caption = (f"PD model quality against the training sample size (M = {m} replications per point, out-of-time "
               f"test on 30\\,000 borrower-years; error bars $\\pm 1.96$ se). Left: the AUC lost against the true PD; "
               f"right: the error of the estimated coefficients. Fits that failed with an explicit separation error "
               f"are excluded and counted ({fails if fails else 'none'}).")
    return FigureSpec(stem, caption, paths)


def fig_calibration_drift(res: dict, out_dir, stem: str = "fig20_calibration_drift") -> FigureSpec:
    cs = _cr(res, "calibration_size")
    dt = {r["model"]: r for r in _cr(res, "downturn")}
    tests = list(dict.fromkeys(r["test"] for r in cs))
    labels = {"hosmer_lemeshow": "Hosmer-Lemeshow", "binomial_any_grade": "binomial (any grade)",
              "jeffreys_any_grade": "Jeffreys (any grade)"}
    with _style():
        fig, (a1, a2) = plt.subplots(1, 2, figsize=WIDE_LEGEND)
        x = np.arange(len(tests))
        for j, (rho, col) in enumerate(((0.0, PALETTE["blue"]), (res["meta"]["rho"], PALETTE["vermillion"]))):
            rr = {r["test"]: r for r in cs if abs(r["rho"] - rho) < 1e-12}
            a1.bar(x + (j - 0.5) * 0.38, [100 * rr[t]["reject"] for t in tests], 0.38, color=col,
                   yerr=[196 * rr[t]["reject_se"] for t in tests], capsize=2, label=f"asset correlation {rho:g}")
        a1.axhline(5, color=PALETTE["black"], ls="--", lw=0.8, label="test level 5 %")
        a1.set_xticks(x)
        a1.set_xticklabels([labels.get(t, t) for t in tests], rotation=20, ha="right", fontsize=7)
        a1.set_ylabel("rejection rate of the TRUE PD (%)")
        a1.legend(frameon=False, fontsize=6.5)
        names = [("true_pd", "true PD"), ("full", "model with macro"), ("ttc", "model without macro")]
        a2.bar(range(3), [100 * dt[k]["pred_pd"] for k, _ in names], 0.6,
               yerr=[196 * (dt[k]["pred_pd_se"] or 0) for k, _ in names], capsize=2,
               color=[PALETTE["green"], PALETTE["blue"], PALETTE["orange"]])
        a2.plot(range(3), [100 * dt[k]["pred_pd_median"] for k, _ in names], ls="", marker="D", ms=4,
                color=PALETTE["black"], label="median over the replications")
        a2.axhline(100 * dt["full"]["realised_dr"], color=PALETTE["black"], ls="--", lw=0.9, label="realised default rate")
        a2.set_xticks(range(3))
        a2.set_xticklabels([n for _, n in names], rotation=20, ha="right", fontsize=7)
        a2.set_ylabel("predicted PD in the downturn (%)")
        a2.legend(frameon=False, fontsize=6.5)
        fig.tight_layout()
        paths = _save(fig, out_dir, stem)
    c = res["meta"]["config"]
    caption = (f"Left: rejection rates of calibration tests applied to the TRUE PD of one period ({c['calib_n']} "
               f"borrowers, M = {c['m']}), with independent defaults and with asset correlation {res['meta']['rho']:g}; "
               f"the per-grade tests reject if any grade does (multiple testing). Right: a downturn period (macro "
               f"variable at {c['downturn_macro']:g} sd): mean (bars) and median (diamonds) predicted PD of the true model "
               f"and of two fitted models (8 training periods) against the realised default rate; the mean of the model "
               f"with the macro variable lies above its median (a noisy coefficient enters through the exponential). "
               f"Hosmer--Lemeshow uses $g = 10$ degrees of freedom (the true PD is not fitted on the sample). "
               f"Error bars $\\pm 1.96$ se.")
    return FigureSpec(stem, caption, paths)


def fig_ecl_cliff(res: dict, out_dir, stem: str = "fig21_ecl_cliff") -> FigureSpec:
    cl = sorted(_cr(res, "cliff"), key=lambda r: r["pd_now"])
    grid = _cr(res, "ecl_grid")
    mults = sorted({r["sicr_multiple"] for r in grid})
    with _style():
        fig, (a1, a2) = plt.subplots(1, 2, figsize=WIDE_LEGEND)
        for st, col in ((1, PALETTE["blue"]), (2, PALETTE["vermillion"])):
            rr = [r for r in cl if r["stage"] == st]
            a1.plot([100 * r["pd_now"] for r in rr], [100 * r["ecl_loan"] for r in rr], marker="o", ms=3, color=col,
                    ls="", label=f"stage {st}")
        a1.set_xlabel("current 12-month PD (%)")
        a1.set_ylabel("ECL (% of EAD)")
        a1.set_title("one loan: PD at origination 1 %")
        a1.legend(frameon=False, fontsize=7)
        cmap = plt.get_cmap("viridis")
        for i, m in enumerate(mults):
            rr = sorted((r for r in grid if r["sicr_multiple"] == m), key=lambda r: r["downside_weight"])
            a2.plot([100 * r["downside_weight"] for r in rr], [100 * r["coverage"] for r in rr], marker="o", ms=3,
                    color=cmap(i / max(1, len(mults) - 1)), label=f"SICR x{m:g}")
        a2.set_xlabel("weight of the downside scenario (%)")
        a2.set_ylabel("ECL coverage of the book (% of EAD)")
        a2.legend(frameon=False, fontsize=6.5, ncol=2)
        fig.tight_layout()
        paths = _save(fig, out_dir, stem)
    caption = ("IFRS 9 ECL. Left: the stage-2 cliff of one 5-year amortising loan (PD at origination 1\\,\\%, LGD "
               "45\\,\\%, EIR 5\\,\\%): when its current 12-month PD reaches 3 times the origination PD (both "
               "base-scenario PDs, the same basis) the provision jumps from 12-month to lifetime ECL. Right: the ECL "
               f"coverage of a synthetic book of {res['meta']['config']['n_loans']} loans against the downside-scenario "
               "weight, for several relative SICR thresholds; the weight changes the measurement, not the stage.")
    return FigureSpec(stem, caption, paths)


def fig_irb_vs_mc(res: dict, out_dir, stem: str = "fig22_irb_vs_mc") -> FigureSpec:
    rows = _cr(res, "irb")
    hom = sorted((r for r in rows if r["portfolio"].startswith("homogeneous")), key=lambda r: r["names"])
    other = {r["portfolio"]: r for r in rows if not r["portfolio"].startswith("homogeneous")}
    with _style():
        fig, ax = plt.subplots(figsize=WIDE)
        n = [r["names"] for r in hom]
        lo = [(r["q999"] - r["q999_lo"]) / r["k_sum"] for r in hom]
        hi = [(r["q999_hi"] - r["q999"]) / r["k_sum"] for r in hom]
        ax.errorbar(n, [r["ratio"] for r in hom], yerr=[lo, hi], marker="o", ms=3, capsize=2, color=PALETTE["blue"],
                    label="homogeneous portfolio, PD 1 %")
        for name, mk, col in (("granular", "s", PALETTE["green"]), ("concentrated", "^", PALETTE["vermillion"])):
            if name in other:
                r = other[name]
                ax.plot([r["names"]], [r["ratio"]], marker=mk, ms=6, ls="", color=col, label=f"{name} ({r['names']} names)")
        ax.axhline(1.0, color=PALETTE["black"], ls="--", lw=0.8, label="IRB capital (ASRF)")
        ax.set_xscale("log")
        ax.set_xlabel("number of names (same total exposure)")
        ax.set_ylabel("(99.9 % loss quantile - EL) / sum K")
        ax.legend(frameon=False, fontsize=7)
        fig.tight_layout()
        paths = _save(fig, out_dir, stem)
    caption = (f"IRB capital against the simulated one-factor loss distribution ({res['meta']['config']['n_sims']:,} "
               "draws, M = 1 year so the maturity adjustment is 1, the IRB correlation of each PD). The ratio of the "
               "simulated 99.9\\,\\% quantile minus the expected loss to the sum of K is 1 for a large granular "
               "portfolio and grows as the exposure is concentrated in fewer names (name concentration is not in the "
               "IRB formula). Error bars: 95\\,\\% order-statistic intervals of the quantile.").replace(",", "\\,", 1)
    return FigureSpec(stem, caption, paths)


# ---- 12. architecture (overview document) ------------------------------------------------

ARCH_BOXES = (
    ("data", "synthetic generators\n+ true VaR / ES", 0.0, 1.0),
    ("models", "HS, normal, t, EWMA,\nGARCH-t, FHS", 1.0, 1.0),
    ("engine", "walk-forward,\nh-day blocks", 2.0, 1.0),
    ("backtests", "Kupiec, binomial,\nChristoffersen, Z2,\ntraffic light", 3.0, 1.0),
    ("studies", "size / power, 10-day,\nwindow, survivorship", 4.0, 1.0),
    ("validation", "UFOS gates: DSR,\nPBO, Newey-West, grid", 2.0, 0.0),
    ("gate study", "best-of-K search,\nFPR / power / holdout", 3.0, 0.0),
    ("report", "figures, tables,\nLaTeX, fingerprints", 5.0, 0.5),
)
ARCH_EDGES = (("data", "models"), ("models", "engine"), ("engine", "backtests"), ("backtests", "studies"),
              ("validation", "gate study"), ("studies", "report"), ("gate study", "report"))


def fig_architecture(out_dir, stem: str = "fig12_architecture") -> FigureSpec:
    """The module pipeline as boxes and arrows (static; no data)."""
    pos = {k: (x, y) for k, _, x, y in ARCH_BOXES}
    w, h = 0.9, 0.62
    with _style():
        fig, ax = plt.subplots(figsize=(6.5, 2.6))
        ax.set_xlim(-0.5, 5.5)
        ax.set_ylim(-0.45, 1.45)
        ax.axis("off")
        for k, text, x, y in ARCH_BOXES:
            col = PALETTE["orange"] if k in ("validation", "gate study") else (
                PALETTE["green"] if k == "report" else PALETTE["blue"])
            ax.add_patch(matplotlib.patches.FancyBboxPatch((x - w / 2, y - h / 2), w, h,
                                                           boxstyle="round,pad=0.02", fc="white", ec=col, lw=1.4))
            ax.text(x, y + 0.17, k, ha="center", va="center", fontsize=8, weight="bold", color=col)
            ax.text(x, y - 0.08, text, ha="center", va="center", fontsize=6)
        for a, b in ARCH_EDGES:
            (x0, y0), (x1, y1) = pos[a], pos[b]
            ax.annotate("", xy=(x1 - w / 2, y1), xytext=(x0 + w / 2, y0),
                        arrowprops=dict(arrowstyle="->", color=PALETTE["grey"], lw=1.0))
        fig.tight_layout()
        paths = _save(fig, out_dir, stem)
    caption = ("Risk Lab architecture. Blue: the market-risk pipeline from the synthetic data with known truth to "
               "the studies; orange: the re-implemented UFOS validation gates and their overfitting study; green: "
               "the report pipeline, which reads only saved, fingerprinted study results.")
    return FigureSpec(stem, caption, paths)


# ---- 13-17. IRRBB ---------------------------------------------------------------------------

SCEN_LABELS = {"parallel_up": "parallel up", "parallel_down": "parallel down", "steepener": "steepener",
               "flattener": "flattener", "short_up": "short up", "short_down": "short down"}


def _irrbb(res: dict, table: str) -> List[dict]:
    return [r for r in res["rows"] if r["table"] == table]


def fig_irrbb_curves(res: dict, out_dir, stem: str = "fig13_irrbb_curves", preset: str = "EUR_BCBS2016") -> FigureSpec:
    from risklab.rates import curve as C
    from risklab.rates import irrbb as R

    p = res["meta"]["curve"]
    base = C.NelsonSiegel(p["b0"], p["b1"], p["b2"], p["tau"], p["b3"], p["tau2"])
    sizes = res["meta"]["shock_presets"][preset]
    t = np.linspace(0.0, 30.0, 301)
    cols = {"parallel_up": PALETTE["vermillion"], "parallel_down": PALETTE["blue"], "steepener": PALETTE["orange"],
            "flattener": PALETTE["green"], "short_up": PALETTE["purple"], "short_down": PALETTE["sky"]}
    with _style():
        fig, ax = plt.subplots(figsize=WIDE_LEGEND)
        ax.plot(t, 100 * base.zero(t), color=PALETTE["black"], lw=2.0, label="base (Nelson-Siegel)")
        for s in R.SCENARIOS:
            ax.plot(t, 100 * R.shocked_curve(base, s, sizes).zero(t), color=cols[s], lw=1.1, label=SCEN_LABELS[s])
        ax.plot(t, 100 * R.Floor()(t), color=PALETTE["grey"], ls=":", lw=1.0, label="EBA post-shock floor")
        ax.set_xlabel("maturity (years)")
        ax.set_ylabel("zero rate (%, continuous)")
        ax.legend(frameon=False, fontsize=7, ncol=2)
        fig.tight_layout()
        paths = _save(fig, out_dir, stem)
    caption = (f"The synthetic base zero curve (Nelson-Siegel, parameters in the results file) and the six "
               f"standardised shock scenarios with the EUR shock sizes {sizes['parallel']:g} / {sizes['short']:g} / "
               f"{sizes['long']:g} bp (parallel / short / long, BCBS 2016), after the EBA post-shock floor (dotted). "
               f"Synthetic curve, no market data.")
    return FigureSpec(stem, caption, paths)


def fig_irrbb_delta(res: dict, out_dir, stem: str = "fig14_irrbb_delta") -> FigureSpec:
    rows = _irrbb(res, "scenario")
    presets = list(dict.fromkeys(r["preset"] for r in rows))
    scen = list(dict.fromkeys(r["scenario"] for r in rows))
    idx = {(r["preset"], r["scenario"]): r for r in rows}
    cfg = res["meta"]["config"]
    with _style():
        fig, (a1, a2) = plt.subplots(1, 2, figsize=WIDE_LEGEND, sharey=False)
        x = np.arange(len(scen))
        w = 0.8 / len(presets)
        for j, p in enumerate(presets):
            col = (PALETTE["blue"], PALETTE["orange"])[j % 2]
            a1.bar(x + (j - (len(presets) - 1) / 2) * w, [100 * idx[(p, s)]["delta_eve_tier1"] for s in scen], w,
                   color=col, label=p.replace("_", " "))
            a2.bar(x + (j - (len(presets) - 1) / 2) * w, [100 * idx[(p, s)]["delta_nii_tier1"] for s in scen], w,
                   color=col, label=p.replace("_", " "))
        a1.axhline(-100 * cfg["eve_threshold"], color=PALETTE["vermillion"], ls="--", lw=1.0, label="EVE outlier test")
        a2.axhline(-100 * cfg["nii_threshold"], color=PALETTE["vermillion"], ls="--", lw=1.0, label="NII outlier test")
        for a, t in ((a1, "Delta EVE (% of Tier 1)"), (a2, "Delta NII, 12 months (% of Tier 1)")):
            a.axhline(0, color=PALETTE["grey"], lw=0.8)
            a.set_xticks(x)
            a.set_xticklabels([SCEN_LABELS[s] for s in scen], rotation=35, ha="right", fontsize=7)
            a.set_title(t)
        a1.legend(frameon=False, fontsize=6.5)
        fig.tight_layout()
        paths = _save(fig, out_dir, stem)
    caption = ("Delta EVE (run-off, equity excluded) and Delta NII (12 months, constant balance sheet) of the synthetic "
               "reference bank per scenario, in \\% of Tier 1, for the BCBS 2016 and 2024 EUR shock sizes. Dashed: the "
               f"supervisory outlier thresholds ({100 * cfg['eve_threshold']:g}\\,\\% EVE decline over the six scenarios; "
               f"{100 * cfg['nii_threshold']:g}\\,\\% NII decline, tested on parallel up / down only).")
    return FigureSpec(stem, caption, paths)


def fig_key_rates(res: dict, out_dir, stem: str = "fig15_key_rates") -> FigureSpec:
    rows = _irrbb(res, "key_rate")
    k = [r["key_tenor"] for r in rows]
    x = np.arange(len(k))
    with _style():
        fig, ax = plt.subplots(figsize=WIDE)
        ax.bar(x - 0.2, [r["dv01_assets"] * 1000 for r in rows], 0.4, color=PALETTE["blue"], label="assets")
        ax.bar(x + 0.2, [r["dv01_liabilities"] * 1000 for r in rows], 0.4, color=PALETTE["orange"], label="liabilities")
        ax.plot(x, [r["dv01_net"] * 1000 for r in rows], color=PALETTE["black"], marker="o", ms=3, label="net")
        ax.axhline(0, color=PALETTE["grey"], lw=0.8)
        ax.set_xticks(x)
        ax.set_xticklabels([f"{v:g}" for v in k])
        ax.set_xlabel("key tenor (years)")
        ax.set_ylabel("key-rate DV01 (EUR thousand per bp)")
        ax.legend(frameon=False, fontsize=7)
        fig.tight_layout()
        paths = _save(fig, out_dir, stem)
    caption = ("Key-rate DV01 profile of the reference bank: value change for a 1 bp fall of the zero curve around each "
               "key tenor (tent-shaped bumps; the key rates sum to the parallel DV01). Assets gain, liabilities "
               "(negative cash flows) lose when rates fall; the net line is the bank's exposure.")
    return FigureSpec(stem, caption, paths)


def fig_nmd_heat(res: dict, out_dir, stem: str = "fig16_nmd_heat") -> FigureSpec:
    rows = _irrbb(res, "nmd")
    cs = sorted({r["core_share"] for r in rows})
    ms = sorted({r["avg_maturity"] for r in rows})
    idx = {(r["core_share"], r["avg_maturity"]): r for r in rows}
    thr = res["meta"]["config"]["eve_threshold"]
    Z = np.array([[-100 * idx[(c, m)]["worst_eve_tier1"] for m in ms] for c in cs])
    with _style():
        fig, ax = plt.subplots(figsize=(5.2, 3.4))
        im = ax.imshow(Z, cmap="Greys", aspect="auto", origin="lower", vmin=0, vmax=max(1.0, float(Z.max())))
        for i in range(len(cs)):
            for j in range(len(ms)):
                v = Z[i, j]
                ax.text(j, i, f"{v:.1f}" + ("*" if v > 100 * thr else ""), ha="center", va="center", fontsize=7,
                        color="white" if v > 0.55 * Z.max() else "black")
        ax.set_xticks(range(len(ms)))
        ax.set_xticklabels([f"{m:g}" for m in ms])
        ax.set_yticks(range(len(cs)))
        ax.set_yticklabels([f"{c:.0%}" for c in cs])
        ax.set_xlabel("average maturity of the core deposits (years)")
        ax.set_ylabel("core share")
        ax.grid(False)
        fig.colorbar(im, ax=ax, fraction=0.05, pad=0.02, label="worst EVE decline (% of Tier 1)")
        fig.tight_layout()
        paths = _save(fig, out_dir, stem)
    caption = ("Model risk of the non-maturity-deposit assumptions: the worst EVE decline over the six scenarios "
               "(\\% of Tier 1, BCBS 2016 EUR shocks) of the reference bank when the core share and the average maturity of "
               "the current-account deposits change (inside the caps). Only the current accounts (EUR 450\\,m of the "
               "600\\,m non-maturity deposits) vary; the savings deposits keep their base assumptions. * = above the "
               f"{100 * thr:g}\\,\\% outlier threshold.")
    return FigureSpec(stem, caption, paths)


def fig_duration_check(res: dict, out_dir, stem: str = "fig17_duration_check") -> FigureSpec:
    rows = sorted(_irrbb(res, "duration"), key=lambda r: r["shift_bp"])
    s = [r["shift_bp"] for r in rows]
    with _style():
        fig, (a1, a2) = plt.subplots(1, 2, figsize=WIDE)
        a1.plot(s, [r["full"] for r in rows], color=PALETTE["black"], marker="o", ms=3, label="full revaluation")
        a1.plot(s, [r["first_order"] for r in rows], color=PALETTE["vermillion"], ls="--", label="first order (DV01)")
        a1.plot(s, [r["second_order"] for r in rows], color=PALETTE["blue"], ls=":", label="second order (+ convexity)")
        a1.set_xlabel("parallel shift (bp)")
        a1.set_ylabel("Delta EVE (EUR m)")
        a1.legend(frameon=False, fontsize=7)
        a2.plot(s, [r["first_order"] - r["full"] for r in rows], color=PALETTE["vermillion"], marker="o", ms=3,
                label="first order")
        a2.plot(s, [r["second_order"] - r["full"] for r in rows], color=PALETTE["blue"], marker="s", ms=3,
                label="second order")
        a2.axhline(0, color=PALETTE["grey"], lw=0.8)
        a2.set_xlabel("parallel shift (bp)")
        a2.set_ylabel("approximation error (EUR m)")
        a2.legend(frameon=False, fontsize=7)
        fig.tight_layout()
        paths = _save(fig, out_dir, stem)
    caption = ("Duration check on the reference bank: Delta EVE under parallel shifts (no floor) by full revaluation "
               "against the first-order (DV01) and second-order (DV01 + convexity) approximations. The first-order "
               "error grows with the square of the shift; the convexity term removes most of it.")
    return FigureSpec(stem, caption, paths)


# ---- LaTeX snippets --------------------------------------------------------------------

def latex_snippet(spec: FigureSpec, width: str = "\\linewidth") -> str:
    """A figure environment that includes the PDF version and carries the caption and a label ``fig:<stem>``."""
    return ("\\begin{figure}[H]\n\\centering\n"
            f"\\includegraphics[width={width}]{{figures/{spec.stem}.pdf}}\n"
            f"\\caption{{{spec.caption}}}\n\\label{{fig:{spec.stem}}}\n\\end{{figure}}\n")


def write_snippet(spec: FigureSpec, out_dir, width: str = "\\linewidth") -> Path:
    p = Path(out_dir) / f"{spec.stem}.tex"
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(latex_snippet(spec, width))
    return p
