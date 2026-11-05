"""Results export: round trip and byte-level determinism."""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: tests run without installing the package
    sys.path.insert(0, _SRC)

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

import risklab
from risklab.backtest import TestResult
from risklab.data import iid_normal
from risklab.engine import walk_forward
from risklab.models.base import Forecast
from risklab.backtest import christoffersen_cc, traffic_light
from risklab.report.export import default_out_dir, results_to_csv, table_to_csv, table_to_json
from risklab.report import (export_run, frame_to_csv, frame_to_json, read_frame_csv,
                            read_frame_json, read_results_json, results_to_json)


class Fixed:
    name = "fixed"

    def fit(self, losses_past):
        self.last = float(losses_past[-1])

    def forecast(self, alphas):
        return Forecast(var={a: 0.01 + self.last * a for a in alphas},
                        es={a: 0.02 + self.last * a for a in alphas})


def _frame():
    s = iid_normal(80, 0.0, 0.01, 5)
    return walk_forward(s.losses, Fixed, window=20, alphas=[0.99, 0.975])


RESULTS = [
    TestResult("dummy_a", 1.2345678901234567, 1, 0.26, 0.05, "accept", 60, 2, "synthetic"),
    TestResult("dummy_b", None, None, None, None, "green", 60, 2),
]


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def assertFramesClose(self, a, b):
        np.testing.assert_array_equal(a.t, b.t)
        # full precision: bit-identical after the round trip
        self.assertEqual(a.loss.tobytes(), b.loss.tobytes())
        self.assertEqual(sorted(a.var), sorted(b.var))
        for k in a.var:
            self.assertEqual(a.var[k].tobytes(), b.var[k].tobytes())
            self.assertEqual(a.es[k].tobytes(), b.es[k].tobytes())
            np.testing.assert_array_equal(a.exceedance[k], b.exceedance[k])
            # the exported values reproduce the exceedance flags
            np.testing.assert_array_equal(b.loss > b.var[k], b.exceedance[k])
        self.assertEqual(a.meta, b.meta)

    def test_json_round_trip(self):
        f = _frame()
        p = frame_to_json(f, self.dir / "f.json", seed=5)
        self.assertFramesClose(f, read_frame_json(p))
        d = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(d["meta"]["risklab_version"], risklab.__version__)
        self.assertEqual(d["meta"]["seed"], 5)
        self.assertEqual(d["meta"]["numpy_version"], np.__version__)

    def test_csv_round_trip(self):
        f = _frame()
        p = frame_to_csv(f, self.dir / "f.csv", seed=5)
        self.assertFramesClose(f, read_frame_csv(p))
        self.assertTrue(p.read_text(encoding="utf-8").startswith("# meta: "))

    def test_results_round_trip(self):
        p = results_to_json(RESULTS, self.dir / "r.json", seed=1)
        back = read_results_json(p)
        self.assertEqual(back[1], RESULTS[1])
        self.assertEqual(back[0].statistic, RESULTS[0].statistic)
        self.assertEqual(back[0].decision, "accept")

    def test_deterministic_bytes(self):
        a = export_run(_frame(), RESULTS, out_dir=self.dir / "a", seed=5)
        b = export_run(_frame(), RESULTS, out_dir=self.dir / "b", seed=5)
        self.assertEqual([x.name for x in a], ["run_frame.json", "run_frame.csv", "run_tests.json",
                                               "run_tests.csv"])
        for x, y in zip(a, b):
            self.assertEqual(x.read_bytes(), y.read_bytes(), x.name)
            self.assertNotIn(b"\r\n", x.read_bytes())

    def test_sorted_keys(self):
        p = frame_to_json(_frame(), self.dir / "f.json")
        d = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(list(d), sorted(d))

    def test_near_tie_survives_export(self):
        # loss and VaR differ in the 15th significant digit: a rounded export would flip the flag
        class NearTie:
            name = "near_tie"

            def fit(self, losses_past):
                pass

            def forecast(self, alphas):
                return Forecast(var={a: 0.0123456789012345 for a in alphas})

        losses = np.array([0.0, 0.0123456789012346, 0.0123456789012344])
        f = walk_forward(losses, NearTie, window=1, alphas=[0.99])
        self.assertEqual(list(f.exceedance[0.99]), [True, False])
        for back in (read_frame_json(frame_to_json(f, self.dir / "n.json")),
                     read_frame_csv(frame_to_csv(f, self.dir / "n.csv"))):
            np.testing.assert_array_equal(back.loss > back.var[0.99], f.exceedance[0.99])

    def test_alpha_keys_collision_free(self):
        alphas = [0.99, 0.9900000000001, 0.975]
        f = walk_forward(np.arange(8.0), Fixed, window=3, alphas=alphas)
        d = json.loads(frame_to_json(f, self.dir / "k.json").read_text(encoding="utf-8"))
        self.assertEqual(len(d["var"]), 3)
        back = read_frame_csv(frame_to_csv(f, self.dir / "k.csv"))
        self.assertEqual(sorted(back.var), sorted(alphas))

    def test_default_out_dir_anchored(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(default_out_dir(), root / "out")
        self.assertTrue((root / "pyproject.toml").is_file())

    def test_results_with_details(self):
        exc = np.zeros(250, dtype=bool)
        exc[[3, 4, 90]] = True
        results = [christoffersen_cc(exc, 0.99), traffic_light(exc)]
        back = read_results_json(results_to_json(results, self.dir / "d.json"))
        self.assertEqual(back, results)
        text = results_to_csv(results, self.dir / "d.csv").read_text(encoding="utf-8")
        self.assertIn("details", text.splitlines()[1])
        self.assertIn('""plus_factor"": 0.0', text)

    def test_tables(self):
        rows = [{"name": "a", "x": 0.1 + 0.2, "flag": True, "n": 3}, {"name": "b", "x": None, "flag": False, "n": 4}]
        j = json.loads(table_to_json(rows, self.dir / "t.json", seed=9, meta={"study": "s"}).read_text("utf-8"))
        self.assertEqual(j["rows"][0]["x"], 0.30000000000000004)
        self.assertEqual(j["meta"]["study"], "s")
        lines = table_to_csv(rows, ["name", "x", "flag", "n"], self.dir / "t.csv").read_text("utf-8").splitlines()
        self.assertEqual(lines[1:], ["name,x,flag,n", "a,0.30000000000000004,1,3", "b,,0,4"])

    def test_nan_refused(self):
        bad = [TestResult("x", float("nan"), 1, 0.5, 0.05, "accept", 1, 0)]
        with self.assertRaises(ValueError):
            results_to_json(bad, self.dir / "x.json")


if __name__ == "__main__":
    unittest.main()


class FingerprintTests(unittest.TestCase):
    def test_code_fingerprint_crlf_and_change(self):
        from risklab.report.export import code_fingerprint

        with tempfile.TemporaryDirectory() as tmp:
            a, b, c = Path(tmp, "a.py"), Path(tmp, "b.py"), Path(tmp, "c.py")
            a.write_bytes(b"x = 1\ny = 2\n")
            b.write_bytes(b"x = 1\r\ny = 2\r\n")
            c.write_bytes(b"x = 1\ny = 3\n")
            fa, fb, fc = code_fingerprint(a), code_fingerprint(b), code_fingerprint(c)
        self.assertEqual(len(fa), 64)
        self.assertNotEqual(fa, fc)
        # the file name is part of the hash only through its path relative to RiskLab; same content, same rel name
        self.assertNotEqual(fa, fb)  # different file names (a.py vs b.py)

    def test_crlf_normalised(self):
        from risklab.report.export import code_fingerprint

        with tempfile.TemporaryDirectory() as t1, tempfile.TemporaryDirectory() as t2:
            Path(t1, "s.py").write_bytes(b"x = 1\ny = 2\n")
            Path(t2, "s.py").write_bytes(b"x = 1\r\ny = 2\r\n")
            self.assertEqual(code_fingerprint(Path(t1, "s.py")), code_fingerprint(Path(t2, "s.py")))

    def test_fingerprint_covers_the_runtime_imports(self):
        """Every risklab module (outside risklab.report) a study loads at import time is in the
        static import closure that the fingerprint hashes."""
        import subprocess
        import sys

        from risklab.report import export

        root = Path(__file__).resolve().parents[1]
        for study in sorted((root / "studies").glob("*.py")):
            code = ("import sys, runpy; sys.path.insert(0, %r); sys.argv=['x']; "
                    "runpy.run_path(%r, run_name='not_main'); print('\\n'.join(sorted(k for k in sys.modules if k.startswith('risklab'))))"
                    % (str(root / "src"), str(study)))
            res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(root))
            self.assertEqual(res.returncode, 0, res.stderr)
            loaded = {n for n in res.stdout.split() if not (n == "risklab.report" or n.startswith("risklab.report."))}
            def closure(start):
                seen, todo = set(), list(start)
                while todo:
                    n = todo.pop()
                    if n in seen or n == "risklab.report" or n.startswith("risklab.report."):
                        continue
                    seen.add(n)
                    todo.extend(export.risklab_imports(export._module_file(n)))
                return seen

            seen = closure(export.risklab_imports(study))
            # modules loaded only because report code imports them (export.py imports the engine and
            # the backtest base) do not compute results
            via_report = closure(export.risklab_imports(Path(export.__file__)))
            self.assertTrue(loaded <= seen | via_report, (study.name, loaded - seen - via_report))

    def test_unrelated_module_does_not_change_fingerprint(self):
        from risklab.report import export

        root = Path(__file__).resolve().parents[1]
        fp = export.code_fingerprint(root / "studies" / "survivorship.py")
        files = set()
        todo = list(export.risklab_imports(root / "studies" / "survivorship.py"))
        while todo:
            n = todo.pop()
            if n.startswith("risklab.report") or export._module_file(n) in files:
                continue
            files.add(export._module_file(n))
            todo.extend(export.risklab_imports(export._module_file(n)))
        self.assertNotIn(root / "src" / "risklab" / "validation" / "stats.py", files)
        self.assertIn(root / "src" / "risklab" / "__init__.py", files)
        self.assertEqual(fp, export.code_fingerprint(root / "studies" / "survivorship.py"))

    def test_save_full_results(self):
        from risklab.report.export import save_full_results

        with tempfile.TemporaryDirectory() as out, tempfile.TemporaryDirectory() as res:
            Path(out, "results.json").write_text("{}", encoding="utf-8")
            Path(out, "summary.md").write_text("# x\n", encoding="utf-8")
            paths = save_full_results(out, "demo", res)
            self.assertEqual([p.name for p in paths], ["demo_full_results.json", "demo_full_summary.md"])
