"""build_report: a missing pdflatex or LaTeX package gives a clear error, not a crash (mocked)."""
import io
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: tests run without installing the package
    sys.path.insert(0, _SRC)

import contextlib
import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest import mock

BUILD = Path(__file__).resolve().parents[1] / "report" / "build_report.py"


def _load():
    spec = importlib.util.spec_from_file_location("build_report", BUILD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class BuildReportTests(unittest.TestCase):
    def setUp(self):
        self.b = _load()

    def test_missing_pdflatex(self):
        with self.assertRaises(self.b.ReportBuildError) as ctx:
            self.b.check_toolchain(which=lambda tool: None)
        msg = str(ctx.exception)
        self.assertIn("'pdflatex' not found", msg)
        self.assertIn("No fallback renderer", msg)

    def test_missing_package(self):
        def run(cmd, **kw):
            ok = cmd[1] != "booktabs.sty"
            return subprocess.CompletedProcess(cmd, 0 if ok else 1, stdout="/x.sty\n" if ok else "", stderr="")

        with self.assertRaises(self.b.ReportBuildError) as ctx:
            self.b.check_toolchain(which=lambda tool: "/usr/bin/" + tool, run=run)
        self.assertIn("LaTeX package 'booktabs' not found", str(ctx.exception))
        self.assertNotIn("'geometry'", str(ctx.exception))

    def test_main_exits_with_message(self):
        err = io.StringIO()
        with mock.patch.object(self.b.shutil, "which", return_value=None), contextlib.redirect_stderr(err):
            self.assertEqual(self.b.main([]), 2)
        self.assertIn("build_report: cannot build the report", err.getvalue())

    def test_pdflatex_failure_is_reported(self):
        def run(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 1, stdout="! LaTeX Error: something\n", stderr="")

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(self.b.ReportBuildError) as ctx:
                self.b.run_pdflatex(Path(tmp), run=run)
        self.assertIn("pdflatex failed (pass 1)", str(ctx.exception))
        self.assertIn("LaTeX Error", str(ctx.exception))

    def test_unstable_references_stop_after_max_passes(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="LaTeX Warning: Rerun to get cross-references right.",
                                               stderr="")

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(self.b.ReportBuildError):
                self.b.run_pdflatex(Path(tmp), run=run)
        self.assertEqual(len(calls), self.b.MAX_PASSES)

    def test_two_passes_when_stable(self):
        calls = []

        def run(cmd, **kw):
            calls.append(kw["env"]["SOURCE_DATE_EPOCH"])
            return subprocess.CompletedProcess(cmd, 0, stdout="Output written", stderr="")

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.b.run_pdflatex(Path(tmp), run=run)
        self.assertEqual(calls, [self.b.SOURCE_DATE_EPOCH] * 2)

    def test_stale_inputs(self):
        import json
        import tempfile
        from risklab.report.export import code_fingerprint

        studies = Path(__file__).resolve().parents[1] / "studies"
        with tempfile.TemporaryDirectory() as tmp:
            good = code_fingerprint(studies / "survivorship.py")
            for name, meta in (("survivorship_full_results.json", {"code_sha256": good}),
                               ("horizon_10d_full_results.json", {}),
                               ("estimation_window_full_results.json", {"code_sha256": "0" * 64})):
                Path(tmp, name).write_text(json.dumps({"meta": meta, "rows": []}), encoding="utf-8")
            stale = self.b.stale_inputs(Path(tmp))
        self.assertEqual(len(stale), 2)
        self.assertTrue(any("horizon_10d" in s and "no code fingerprint" in s for s in stale))
        self.assertTrue(any("estimation_window" in s and "different code" in s for s in stale))

    def test_stale_blocks_unless_allowed(self):
        import json
        import tempfile

        def run(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, stdout="/x.sty\n", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            for name in self.b.RESULT_FILES:
                Path(tmp, name).write_text(json.dumps({"meta": {}, "rows": []}), encoding="utf-8")
            with self.assertRaises(self.b.ReportBuildError) as ctx:
                self.b.check_toolchain(which=lambda t: "/usr/bin/" + t, run=run, results_dir=Path(tmp))
            self.assertIn("stale input", str(ctx.exception))
            try:
                import matplotlib  # noqa: F401
            except ImportError:
                return  # without matplotlib the toolchain check fails anyway
            warnings = self.b.check_toolchain(which=lambda t: "/usr/bin/" + t, run=run, results_dir=Path(tmp),
                                              allow_stale=True)
            self.assertEqual(len(warnings), 4)

    def test_saved_results_are_current(self):
        stale = self.b.stale_inputs(self.b.ROOT / "studies" / "results")
        self.assertEqual(stale, [], "re-run the listed studies with --mode full --save-results")

    def test_missing_results_are_named(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(self.b.ReportBuildError) as ctx:
                self.b.check_toolchain(which=lambda tool: None, results_dir=Path(tmp))
        self.assertIn("estimation_window_full_results.json", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
