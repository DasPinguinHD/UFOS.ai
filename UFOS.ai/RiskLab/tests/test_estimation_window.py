"""Estimation-window study: smoke run, paired design and reference sizes."""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: tests run without installing the package
    sys.path.insert(0, _SRC)

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

STUDY = Path(__file__).resolve().parents[1] / "studies" / "estimation_window.py"


def _load():
    spec = importlib.util.spec_from_file_location("estimation_window", STUDY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class EstimationWindowSmokeTests(unittest.TestCase):
    def test_smoke(self):
        s = _load()
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp, "a"), Path(tmp, "b")
            self.assertEqual(s.main(["--mode", "smoke", "--quiet", "--out-dir", str(a)]), 0)
            self.assertEqual(s.main(["--mode", "smoke", "--quiet", "--out-dir", str(b)]), 0)
            self.assertEqual((a / "results.json").read_bytes(), (b / "results.json").read_bytes())
            d = json.loads((a / "results.json").read_text(encoding="utf-8"))
            keys = [(r["model"], r["window"]) for r in d["rows"]]
            self.assertEqual(keys, [("oracle", None), ("garch_t", 250), ("garch_t", 500), ("fhs", 250), ("fhs", 500)])
            self.assertEqual(d["rows"][0]["exceed_diff"], 0.0)
            for r in d["rows"]:
                self.assertEqual(r["failed_reps"], 0)
                self.assertIn("exceed_diff_se", r)
                self.assertAlmostEqual(r["exceed_diff"], r["exceed_rate"] - d["rows"][0]["exceed_rate"], places=12)
                self.assertAlmostEqual(r["ref_kupiec"], d["rows"][0]["ref_kupiec"])
                self.assertEqual(r["ref_z2"], d["rows"][0]["reject_z2"])
            self.assertIn("is a demo", (a / "summary.md").read_text(encoding="utf-8"))
        self.assertNotEqual(s.mc_seed(5), 5)
        with self.assertRaises(SystemExit):  # --save-results only with the full default run
            s.main(["--mode", "smoke", "--save-results", "--quiet"])


if __name__ == "__main__":
    unittest.main()
