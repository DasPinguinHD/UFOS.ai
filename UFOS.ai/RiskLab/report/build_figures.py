"""Build the seven report figures (PNG + PDF) and their LaTeX snippets into ``report/figures/``.

Inputs: the saved synthetic study results in ``studies/results/*_full_results.json`` and two
fixed-seed synthetic simulations (figures 1 and 6). Needs the optional extra ``report``
(matplotlib); without it the script stops with a clear message.

Run from ``UFOS.ai/RiskLab/``::

    python report/build_figures.py [--out-dir report/figures]
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


def build_figures(results_dir: Path = RESULTS, out_dir: Path = FIGURES) -> List[Path]:
    """All figures + snippets; returns the written paths."""
    require_matplotlib()
    from risklab.report import figures as F

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
    ap.add_argument("--results-dir", default=str(RESULTS))
    ap.add_argument("--out-dir", default=str(FIGURES))
    args = ap.parse_args(argv)
    try:
        paths = build_figures(Path(args.results_dir), Path(args.out_dir))
    except MissingDependency as exc:
        print(f"build_figures: {exc}", file=sys.stderr)
        return 2
    print(f"{len(paths)} files written to {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
