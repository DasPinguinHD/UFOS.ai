"""Gate overfitting study: smoke mode end to end, row structure, rates and their se, reproducibility."""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: tests run without installing the package
    sys.path.insert(0, _SRC)

import contextlib
import importlib.util
import io
import json
import math
import tempfile
import unittest
from pathlib import Path

STUDY = Path(__file__).resolve().parents[1] / "studies" / "gate_overfitting.py"


def _load():
    spec = importlib.util.spec_from_file_location("gate_overfitting", STUDY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class GateOverfittingTests(unittest.TestCase):
    def setUp(self):
        self.s = _load()

    def test_smoke_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.s.main(["--mode", "smoke", "--out-dir", tmp, "--quiet"]), 0)
            d = json.loads(Path(tmp, "results.json").read_text(encoding="utf-8"))
            self.assertTrue(Path(tmp, "summary.md").is_file())
            self.assertTrue(Path(tmp, "results.csv").is_file())
        rows = d["rows"]
        self.assertEqual(len(rows), 2 * 1 * 2 * 4 * 3)        # data x years x signals x K x presets
        self.assertEqual(d["meta"]["study"], "gate_overfitting")
        self.assertEqual(len(d["meta"]["code_sha256"]), 64)
        self.assertEqual(d["meta"]["in_scope"], ["G1", "G2", "G4", "G5", "G6", "G7"])
        self.assertIn("G8", d["meta"]["out_of_scope"])
        for r in rows:
            self.assertEqual(r["m"], 4)
            for g in self.s.GATES + ("all", "all_h", "stat", "stat_h"):
                p, se = r[f"pass_{g}"], r[f"pass_{g}_se"]
                self.assertTrue(0.0 <= p <= 1.0)
                self.assertAlmostEqual(se, math.sqrt(p * (1 - p) / r["m"]))
            self.assertLessEqual(r["pass_all_h"], r["pass_all"])
            self.assertLessEqual(r["pass_all"], min(r[f"pass_{g}"] for g in ("G1", "G2", "G4", "G5", "G6", "G7")))
            self.assertEqual(r["h_given_all"] is None, r["n_all"] == 0)
            self.assertLessEqual(r["pass_stat_h"], r["pass_stat"])
            self.assertLessEqual(r["pass_all"], r["pass_stat"])
            self.assertEqual(r["h_given_stat"] is None, r["n_stat"] == 0)
            self.assertEqual(r["h_given_g4"] is None, r["n_g4"] == 0)
        self.assertEqual({c["k"] for c in d["meta"]["calibration"]}, {1, 8, 64, 256})

    def test_reproducible_and_common_random_numbers(self):
        cfg = self.s.OverfitConfig(m=3, years=(5,), signals=(0.0, 0.5), ks=(1, 8), data=("iid_t",))
        a = self.s.run_study(cfg)
        b = self.s.run_study(cfg)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        evs = self.s.run_cell(cfg, "iid_t", 5, [cfg.seed, 0, 0])
        # same search order on the same innovations: K = 1 picks the same configuration at every signal level
        self.assertEqual([e.selected[1] for e in evs[0.0]], [e.selected[1] for e in evs[0.5]])

    def test_preset_order_of_strictness(self):
        cfg = self.s.OverfitConfig(m=6, years=(5,), signals=(1.0,), ks=(8,), data=("iid_t",))
        rows = {r["preset"]: r for r in self.s.run_study(cfg)}
        for g in ("G1", "G2", "G4", "G5", "G6", "G7"):
            self.assertGreaterEqual(rows["lenient"][f"pass_{g}"], rows["standard"][f"pass_{g}"])
            self.assertGreaterEqual(rows["standard"][f"pass_{g}"], rows["strict"][f"pass_{g}"])

    def test_save_results_needs_full_mode(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.s.main(["--mode", "smoke", "--save-results"])
            with self.assertRaises(SystemExit):
                self.s.main(["--mode", "full", "--m", "5", "--save-results"])


if __name__ == "__main__":
    unittest.main()
