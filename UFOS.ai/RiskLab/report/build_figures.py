"""Build the report figures (PNG + PDF) and their LaTeX snippets into ``report/figures/``.

``--doc validation`` (default): figures 1-7 of the validation report, from the saved synthetic
study results in ``studies/results/*_full_results.json`` and two fixed-seed synthetic
simulations (figures 1 and 6). ``--doc overfitting``: figures 8-11 of the gate overfitting
study, from ``gate_overfitting_full_results.json``. ``--doc overview``: the architecture figure 12
and figure 3 for the project overview. ``--doc irrbb``: figures 13-17 of the IRRBB report, from
``irrbb_scenarios_full_results.json``. ``--doc credit``: figures 18-22 of the credit report, from
``credit_risk_full_results.json``. ``--doc valuation``: figures 23-27 of the valuation report, from
``valuation_sensitivity_full_results.json``. Needs the optional extra ``report``
(matplotlib); without it the script stops with a clear message.

Run from ``UFOS.ai/RiskLab/``::

    python report/build_figures.py [--doc validation|overfitting] [--out-dir report/figures]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]  # RiskLab/
_SRC = str(ROOT / "src")
if _SRC not in sys.path:  # path shim: runs without installing the package
    sys.path.insert(0, _SRC)

RESULTS = ROOT / "studies" / "results"
FIGURES = ROOT / "report" / "figures"


class MissingDependency(RuntimeError):
    """An optional dependency of the report build is not installed."""


def require_matplotlib():
    try:
        import matplotlib  # noqa: F401
    except ImportError:
        raise MissingDependency("matplotlib is not installed: install the report extra "
                                "(pip install -e .[report] or pip install -r requirements-report.in)") from None


def build_figures(results_dir: Path = RESULTS, out_dir: Path = FIGURES, doc: str = "validation") -> List[Path]:
    """All figures + snippets of ``doc``; returns the written paths."""
    require_matplotlib()
    from risklab.report import figures as F

    if doc == "overfitting":
        go = F.load_results(results_dir / "gate_overfitting_full_results.json")
        specs = [F.fig_fpr_vs_k(go, out_dir), F.fig_power(go, out_dir), F.fig_gate_heat(go, out_dir),
                 F.fig_holdout(go, out_dir)]
        written: List[Path] = []
        for s in specs:
            written += list(s.paths) + [F.write_snippet(s, out_dir)]
        return written
    if doc == "credit":
        res = F.load_results(results_dir / "credit_risk_full_results.json")
        specs = [F.fig_cap_roc(res, out_dir), F.fig_pd_sample_size(res, out_dir), F.fig_calibration_drift(res, out_dir),
                 F.fig_ecl_cliff(res, out_dir), F.fig_irb_vs_mc(res, out_dir)]
        written = []
        for s in specs:
            width = "0.6\\linewidth" if s.stem == "fig18_cap_roc" else "\\linewidth"
            written += list(s.paths) + [F.write_snippet(s, out_dir, width)]
        return written
    if doc == "valuation":
        res = F.load_results(results_dir / "valuation_sensitivity_full_results.json")
        specs = [F.fig_implied_growth(res, out_dir), F.fig_tornado(res, out_dir), F.fig_two_way(res, out_dir),
                 F.fig_sobol(res, out_dir), F.fig_base_rates(res, out_dir)]
        written = []
        for s in specs:
            width = "0.66\\linewidth" if s.stem == "fig26_sobol" else "0.74\\linewidth"
            written += list(s.paths) + [F.write_snippet(s, out_dir, width)]
        return written
    if doc == "irrbb":
        res = F.load_results(results_dir / "irrbb_scenarios_full_results.json")
        specs = [F.fig_irrbb_curves(res, out_dir), F.fig_irrbb_delta(res, out_dir), F.fig_key_rates(res, out_dir),
                 F.fig_nmd_heat(res, out_dir), F.fig_duration_check(res, out_dir)]
        written = []
        for s in specs:
            width = "0.75\\linewidth" if s.stem == "fig16_nmd_heat" else "\\linewidth"
            written += list(s.paths) + [F.write_snippet(s, out_dir, width)]
        return written
    if doc == "overview":
        ew = F.load_results(results_dir / "estimation_window_full_results.json")
        specs = [F.fig_architecture(out_dir), F.fig_estimation_window(ew, out_dir)]
        written = []
        for s in specs:
            written += list(s.paths) + [F.write_snippet(s, out_dir)]
        return written
    if doc != "validation":
        raise ValueError(f"unknown document {doc!r}")
    power = F.load_results(results_dir / "var_backtest_power_full_results.json")
    horizon = F.load_results(results_dir / "horizon_10d_full_results.json")
    ew = F.load_results(results_dir / "estimation_window_full_results.json")
    surv = F.load_results(results_dir / "survivorship_full_results.json")
    specs = [
        F.fig_exceedances(F.exceedance_data(), out_dir),
        F.fig_rejection_rates(power, out_dir),
        F.fig_estimation_window(ew, out_dir),
        F.fig_traffic_light(power, out_dir),
        F.fig_horizon(horizon, out_dir),
        F.fig_qq(F.qq_data(), out_dir),
        F.fig_survivorship(surv, out_dir),
    ]
    paths: List[Path] = []
    for s in specs:
        width = "0.55\\linewidth" if s.stem == "fig6_qq_residuals" else "\\linewidth"
        paths += list(s.paths) + [F.write_snippet(s, out_dir, width)]
    return paths


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--doc", choices=("validation", "overfitting", "irrbb", "credit", "valuation", "overview"), default="validation")
    ap.add_argument("--results-dir", default=str(RESULTS))
    ap.add_argument("--out-dir", default=str(FIGURES))
    args = ap.parse_args(argv)
    try:
        paths = build_figures(Path(args.results_dir), Path(args.out_dir), args.doc)
    except MissingDependency as exc:
        print(f"build_figures: {exc}", file=sys.stderr)
        return 2
    print(f"{len(paths)} files written to {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
