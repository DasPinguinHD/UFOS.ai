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
