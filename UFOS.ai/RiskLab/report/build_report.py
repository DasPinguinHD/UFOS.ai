"""Build a report document: figures, tables, then pdflatex twice.

Three documents (``--doc``): ``validation`` (the VaR / ES validation report,
``validation_report.tex``, the default), ``overfitting`` (the overfitting study of the
UFOS validation gates, ``overfitting_study.tex``) and ``overview`` (the 3-4 page project
overview, ``overview.tex``; published as ``overview.pdf``). Each has its own study results, figures,
tables and draft PDF (:data:`DOCS`).

Steps:
1. check the toolchain: matplotlib (report extra), ``pdflatex`` and ``kpsewhich`` on
   PATH, every LaTeX package in :data:`REQUIRED_PACKAGES`, and the four study results
   of the document in ``studies/results/``; anything missing stops the build with a clear message
   (exit code 2) - there is no fallback renderer;
   each results file must carry the code fingerprint (``meta.code_sha256``) of the
   CURRENT study code (:func:`risklab.report.export.code_fingerprint`); a stale or
   unfingerprinted input stops the build unless ``--allow-stale`` is given, in which case
   every stale input is printed as a warning - never silently;
2. figures -> ``report/figures/`` (see ``build_figures.py``);
3. tables -> ``report/tables/`` (:func:`risklab.report.tables.write_all`);
4. ``pdflatex`` twice (a third time if LaTeX asks for a rerun) in ``report/``, output and auxiliary files in ``out/report/``
   (``SOURCE_DATE_EPOCH`` fixed; the template omits the PDF date and trailer ID);
5. ``--publish --publish-to <dir>`` copies the PDF to ``<dir>/<document>_draft.pdf``
   (``validation_report_draft.pdf`` / ``overfitting_study_draft.pdf``). Report PDFs do not live in
   the repository (user decision 2026-11-05; the figure PDFs in ``report/figures/`` are LaTeX
   inputs, not reports, and stay): ``--publish-to`` is required with ``--publish`` (no
   default), the directory must exist and must lie OUTSIDE the repository; a target inside it is
   refused. The repository is the nearest ancestor of ``RiskLab/`` that contains ``.git``; in a copy
   without ``.git`` the folder that contains ``RiskLab/`` counts as the repository (stated rule, so a
   copy is never written into either). Without ``--publish`` the PDF stays in ``out/report/``
   (git-ignored).

Run from ``UFOS.ai/RiskLab/``::

    python report/build_report.py [--doc validation|overfitting|overview] [--publish --publish-to DIR] [--skip-figures]
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
OUT_DIR = ROOT / "out" / "report"
#: the documents: LaTeX source, draft PDF and the study results each one needs
DOCS = {
    "validation": {"tex": "validation_report.tex", "draft": "validation_report_draft.pdf",
                   "results": ("var_backtest_power_full_results.json", "horizon_10d_full_results.json",
                               "estimation_window_full_results.json", "survivorship_full_results.json")},
    "overfitting": {"tex": "overfitting_study.tex", "draft": "overfitting_study_draft.pdf",
                    "results": ("gate_overfitting_full_results.json",)},
    "overview": {"tex": "overview.tex", "draft": "overview.pdf",
                 "results": ("var_backtest_power_full_results.json", "horizon_10d_full_results.json",
                             "estimation_window_full_results.json", "survivorship_full_results.json",
                             "gate_overfitting_full_results.json")},
}
TEX = DOCS["validation"]["tex"]
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


RESULT_FILES = DOCS["validation"]["results"]


def _doc(doc: str) -> dict:
    if doc not in DOCS:
        raise ReportBuildError(f"unknown document {doc!r} (one of {', '.join(DOCS)})")
    return DOCS[doc]


def stale_inputs(results_dir: Path, studies_dir: Optional[Path] = None, doc: str = "validation") -> List[str]:
    """Results files of ``doc`` whose code fingerprint does not match the current study code (with the reason)."""
    import json

    from risklab.report.export import code_fingerprint

    studies_dir = Path(studies_dir) if studies_dir is not None else ROOT / "studies"
    stale = []
    for name in _doc(doc)["results"]:
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


def check_toolchain(which=None, run=None, results_dir: Optional[Path] = None, allow_stale: bool = False,
                    doc: str = "validation") -> List[str]:
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
    for name in _doc(doc)["results"]:
        if not (results_dir / name).is_file():
            missing.append(f"study results '{name}' not found in {results_dir}: run the study with --mode full "
                           "and save its results.json under that name")
    stale = stale_inputs(results_dir, doc=doc)
    if stale and not allow_stale:
        missing += [f"stale input: {s}; re-run the study with --mode full --save-results "
                    "(or pass --allow-stale to build anyway, with warnings)" for s in stale]
    if missing:
        raise ReportBuildError("cannot build the report:\n  - " + "\n  - ".join(missing)
                               + "\nNo fallback renderer is used.")
    return stale


def run_pdflatex(out_dir: Path = OUT_DIR, run=None, tex: str = TEX) -> Path:
    run = run or subprocess.run
    out_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, SOURCE_DATE_EPOCH=SOURCE_DATE_EPOCH, FORCE_SOURCE_DATE="1")
    cmd = ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "-file-line-error",
           f"-output-directory={out_dir}", tex]
    stem = tex[:-len(".tex")]
    for i in range(MAX_PASSES):
        res = run(cmd, cwd=str(REPORT_DIR), env=env, capture_output=True, text=True)
        if res.returncode != 0:
            tail = "\n".join((res.stdout or "").splitlines()[-25:])
            raise ReportBuildError(f"pdflatex failed (pass {i + 1}); see {out_dir / (stem + '.log')}\n{tail}")
        if i >= 1 and "Rerun to get" not in (res.stdout or ""):
            break
    else:
        raise ReportBuildError(f"cross-references still unstable after {MAX_PASSES} pdflatex passes")
    return out_dir / f"{stem}.pdf"


def repo_root(start: Path = ROOT) -> Path:
    """The repository that contains ``start``: the nearest ancestor with a ``.git`` entry, else the
    folder that contains RiskLab (see the module docstring)."""
    for p in [start, *start.parents]:
        if (p / ".git").exists():
            return p
    return ROOT.parent


def check_publish_dir(target, root: Optional[Path] = None) -> Path:
    """The resolved publish directory; :class:`ReportBuildError` if it does not exist or lies inside the repository."""
    if target is None or str(target) == "":
        raise ReportBuildError("--publish needs --publish-to <dir> (a directory outside the repository; no default)")
    t = Path(target).expanduser().resolve()
    r = (root or repo_root()).resolve()
    if t == r or r in t.parents:
        raise ReportBuildError(f"refusing to publish into the repository ({r}): report PDFs do not live in the repo; "
                               f"choose a directory outside it")
    if not t.is_dir():
        raise ReportBuildError(f"publish directory {t} does not exist")
    return t


def build(publish: bool = False, skip_figures: bool = False, allow_stale: bool = False,
          doc: str = "validation", publish_to=None) -> Path:
    d = _doc(doc)
    target = check_publish_dir(publish_to) if publish else None
    if publish_to is not None and not publish:
        raise ReportBuildError("--publish-to is only used together with --publish")
    for warning in check_toolchain(allow_stale=allow_stale, doc=doc):
        print(f"build_report: WARNING stale input: {warning}", file=sys.stderr)
    from build_figures import build_figures
    from risklab.report.tables import write_all

    results = ROOT / "studies" / "results"
    if not skip_figures:
        build_figures(results, REPORT_DIR / "figures", doc=doc)
    write_all(results, REPORT_DIR / "tables", doc=doc)
    pdf = run_pdflatex(tex=d["tex"])
    if target is not None:
        draft = target / d["draft"]
        shutil.copyfile(pdf, draft)
        return draft
    return pdf


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--doc", choices=sorted(DOCS), default="validation",
                    help="validation (the VaR / ES validation report, default), overfitting (the gate study) or "
                         "overview (the project overview)")
    ap.add_argument("--publish", action="store_true",
                    help="copy the PDF to <--publish-to>/<document>_draft.pdf (outside the repository)")
    ap.add_argument("--publish-to", default=None, metavar="DIR",
                    help="existing directory OUTSIDE the repository for --publish (required with it, no default)")
    ap.add_argument("--skip-figures", action="store_true", help="reuse the figures in report/figures")
    ap.add_argument("--allow-stale", action="store_true",
                    help="build even if a results file was produced by other code (prints a warning per file)")
    args = ap.parse_args(argv)
    try:
        pdf = build(args.publish, args.skip_figures, args.allow_stale, args.doc, args.publish_to)
    except ReportBuildError as exc:
        print(f"build_report: {exc}", file=sys.stderr)
        return 2
    print(f"report written: {pdf}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
