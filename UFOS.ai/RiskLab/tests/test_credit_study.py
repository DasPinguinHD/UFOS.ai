"""Credit study: smoke mode end to end, the tables it produces, reproducibility."""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: tests run without installing the package
    sys.path.insert(0, _SRC)

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

STUDY = Path(__file__).resolve().parents[1] / "studies" / "credit_risk.py"


def _load():
    spec = importlib.util.spec_from_file_location("credit_risk", STUDY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class CreditStudyTests(unittest.TestCase):
    def setUp(self):
        self.s = _load()

    def test_smoke_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.s.main(["--mode", "smoke", "--out-dir", tmp, "--quiet"]), 0)
            d = json.loads(Path(tmp, "results.json").read_text(encoding="utf-8"))
            self.assertTrue(Path(tmp, "summary.md").is_file())
        tables = {r["table"] for r in d["rows"]}
        self.assertEqual(tables, {"sample_size", "coef", "cap", "cap_summary", "calibration_size", "downturn",
                                  "ecl_grid", "cliff", "irb"})
        for k in ("irb_sources", "ifrs9_sources", "true_coef", "jeffreys_source"):
            self.assertIn(k, d["meta"])
        cliff = [r for r in d["rows"] if r["table"] == "cliff"]
        self.assertEqual({r["stage"] for r in cliff}, {1, 2})
        for r in d["rows"]:
            if r["table"] == "sample_size":
                self.assertLessEqual(r["fit_failures"], r["m"])
                if r["fit_failures"] < r["m"] - 1:
                    self.assertGreater(r["macro_coef_sd"], 0)
                    self.assertGreater(r["macro_se_mean"], 0)
            if r["table"] == "downturn":
                self.assertIsNotNone(r["pred_pd_median"])

    def test_reproducible(self):
        cfg = self.s.CreditConfig(m=2, n_sims=5000, n_train=(2000,), test_n=2000, calib_n=2000, n_loans=50,
                                  names_curve=(10,))
        a, b = self.s.run_study(cfg), self.s.run_study(cfg)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    def test_save_results_needs_full_mode(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.s.main(["--mode", "smoke", "--save-results"])


if __name__ == "__main__":
    unittest.main()
