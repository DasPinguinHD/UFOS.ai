"""Build the validation report: figures, tables, then pdflatex twice.

Steps:
1. check the toolchain: matplotlib (report extra), ``pdflatex`` and ``kpsewhich`` on
   PATH, every LaTeX package in :data:`REQUIRED_PACKAGES`, and the four study results
   in ``studies/results/``; anything missing stops the build with a clear message
   (exit code 2) - there is no fallback renderer;
   each results file must carry the code fingerprint (``meta.code_sha256``) of the
   CURRENT study code (:func:`risklab.report.export.code_fingerprint`); a stale or
   unfingerprinted input stops the build unless ``--allow-stale`` is given, in which case
   every stale input is printed as a warning - never silently;
2. figures -> ``report/figures/`` (see ``build_figures.py``);
3. tables -> ``report/tables/`` (:func:`risklab.report.tables.write_all`);
4. ``pdflatex`` twice (a third time if LaTeX asks for a rerun) in ``report/``, output and auxiliary files in ``out/report/``
   (``SOURCE_DATE_EPOCH`` fixed; the template omits the PDF date and trailer ID);
5. ``--publish`` copies the PDF to ``report/validation_report_draft.pdf``.

Run from ``UFOS.ai/RiskLab/``::

    python report/build_report.py [--publish] [--skip-figures]
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]  # RiskLab/
_SRC = str(ROOT / "src")
if _SRC not in sys.path:  # path shim: runs without installing the package
    sys.path.insert(0, _SRC)
sys.path.insert(0, str(Path(__file__).resolve().parent))

REPORT_DIR = ROOT / "report"
TEX = "validation_report.tex"
OUT_DIR = ROOT / "out" / "report"
DRAFT = REPORT_DIR / "validation_report_draft.pdf"
#: LaTeX packages the template uses (all part of a standard TeX Live "latex-extra"-level installation).
REQUIRED_PACKAGES = ("geometry", "graphicx", "xcolor", "booktabs", "array", "amsmath", "amssymb", "caption",
                     "float", "hyperref")
#: pdflatex runs at least twice; a third pass only if LaTeX still asks for a rerun (the table of
#: contents can move labels to other pages after the second pass).
MAX_PASSES = 3
#: Fixed date for reproducible PDFs (2026-11-05 00:00:00 UTC).
SOURCE_DATE_EPOCH = "1793836800"


class ReportBuildError(RuntimeError):
    """The report cannot be built (missing tool or package, LaTeX error)."""


RESULT_FILES = ("var_backtest_power_full_results.json", "horizon_10d_full_results.json",
                "estimation_window_full_results.json", "survivorship_full_results.json")


def stale_inputs(results_dir: Path, studies_dir: Optional[Path] = None) -> List[str]:
    """Results files whose code fingerprint does not match the current study code (with the reason)."""
    import json

    from risklab.report.export import code_fingerprint

    studies_dir = Path(studies_dir) if studies_dir is not None else ROOT / "studies"
    stale = []
    for name in RESULT_FILES:
        path = Path(results_dir) / name
        if not path.is_file():
            continue
        study = name[: -len("_full_results.json")]
        try:
            recorded = json.loads(path.read_text(encoding="utf-8"))["meta"].get("code_sha256")
        except (ValueError, KeyError) as exc:
            stale.append(f"{name}: unreadable meta ({exc})")
            continue
        current = code_fingerprint(studies_dir / f"{study}.py")
        if recorded is None:
            stale.append(f"{name}: no code fingerprint recorded (produced before fingerprints existed)")
        elif recorded != current:
            stale.append(f"{name}: produced by different code (fingerprint {recorded[:12]}, current {current[:12]})")
    return stale


def check_toolchain(which=None, run=None, results_dir: Optional[Path] = None, allow_stale: bool = False) -> List[str]:
    """Raise :class:`ReportBuildError` naming every missing piece (tools, packages, study results)."""
    from build_figures import MissingDependency, require_matplotlib

    which = which or shutil.which
    run = run or subprocess.run
    results_dir = Path(results_dir) if results_dir is not None else ROOT / "studies" / "results"

    missing: List[str] = []
    try:
        require_matplotlib()
    except MissingDependency as exc:
        missing.append(str(exc))
    for tool in ("pdflatex", "kpsewhich"):
        if which(tool) is None:
            missing.append(f"'{tool}' not found on PATH: install a TeX distribution (e.g. TeX Live)")
    if which("kpsewhich") is not None:
        for pkg in REQUIRED_PACKAGES:
            res = run(["kpsewhich", f"{pkg}.sty"], capture_output=True, text=True)
            if res.returncode != 0 or not res.stdout.strip():
                missing.append(f"LaTeX package '{pkg}' not found (kpsewhich {pkg}.sty)")
    for name in RESULT_FILES:
        if not (results_dir / name).is_file():
            missing.append(f"study results '{name}' not found in {results_dir}: run the study with --mode full "
                           "and save its results.json under that name")
    stale = stale_inputs(results_dir)
    if stale and not allow_stale:
        missing += [f"stale input: {s}; re-run the study with --mode full --save-results "
                    "(or pass --allow-stale to build anyway, with warnings)" for s in stale]
    if missing:
        raise ReportBuildError("cannot build the report:\n  - " + "\n  - ".join(missing)
                               + "\nNo fallback renderer is used.")
    return stale


def run_pdflatex(out_dir: Path = OUT_DIR, run=None) -> Path:
    run = run or subprocess.run
    out_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, SOURCE_DATE_EPOCH=SOURCE_DATE_EPOCH, FORCE_SOURCE_DATE="1")
    cmd = ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "-file-line-error",
           f"-output-directory={out_dir}", TEX]
    for i in range(MAX_PASSES):
        res = run(cmd, cwd=str(REPORT_DIR), env=env, capture_output=True, text=True)
        if res.returncode != 0:
            tail = "\n".join((res.stdout or "").splitlines()[-25:])
            raise ReportBuildError(f"pdflatex failed (pass {i + 1}); see {out_dir / 'validation_report.log'}\n{tail}")
        if i >= 1 and "Rerun to get" not in (res.stdout or ""):
            break
    else:
        raise ReportBuildError(f"cross-references still unstable after {MAX_PASSES} pdflatex passes")
    return out_dir / "validation_report.pdf"


def build(publish: bool = False, skip_figures: bool = False, allow_stale: bool = False) -> Path:
    for warning in check_toolchain(allow_stale=allow_stale):
        print(f"build_report: WARNING stale input: {warning}", file=sys.stderr)
    from build_figures import build_figures
    from risklab.report.tables import write_all

    results = ROOT / "studies" / "results"
    if not skip_figures:
        build_figures(results, REPORT_DIR / "figures")
    write_all(results, REPORT_DIR / "tables")
    pdf = run_pdflatex()
    if publish:
        shutil.copyfile(pdf, DRAFT)
        return DRAFT
    return pdf


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--publish", action="store_true", help="copy the PDF to report/validation_report_draft.pdf")
    ap.add_argument("--skip-figures", action="store_true", help="reuse the figures in report/figures")
    ap.add_argument("--allow-stale", action="store_true",
                    help="build even if a results file was produced by other code (prints a warning per file)")
    args = ap.parse_args(argv)
    try:
        pdf = build(args.publish, args.skip_figures, args.allow_stale)
    except ReportBuildError as exc:
        print(f"build_report: {exc}", file=sys.stderr)
        return 2
    print(f"report written: {pdf}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
