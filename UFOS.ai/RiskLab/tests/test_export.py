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
from risklab.report.export import default_out_dir
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

    def test_nan_refused(self):
        bad = [TestResult("x", float("nan"), 1, 0.5, 0.05, "accept", 1, 0)]
        with self.assertRaises(ValueError):
            results_to_json(bad, self.dir / "x.json")


if __name__ == "__main__":
    unittest.main()
