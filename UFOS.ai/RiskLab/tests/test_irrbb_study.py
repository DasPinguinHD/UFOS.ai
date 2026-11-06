"""IRRBB study: smoke mode end to end, deterministic reference-bank rows, consistency of the tables, the duration check."""
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

STUDY = Path(__file__).resolve().parents[1] / "studies" / "irrbb_scenarios.py"


def _load():
    spec = importlib.util.spec_from_file_location("irrbb_scenarios", STUDY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class IRRBBStudyTests(unittest.TestCase):
    def setUp(self):
        self.s = _load()

    def test_smoke_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.s.main(["--mode", "smoke", "--out-dir", tmp, "--quiet"]), 0)
            d = json.loads(Path(tmp, "results.json").read_text(encoding="utf-8"))
            self.assertTrue(Path(tmp, "summary.md").is_file())
        tables = {r["table"] for r in d["rows"]}
        self.assertEqual(tables, {"scenario", "outlier", "key_rate", "nmd", "duration", "population"})
        self.assertEqual(len([r for r in d["rows"] if r["table"] == "scenario"]), 12)
        self.assertEqual(len([r for r in d["rows"] if r["table"] == "nmd"]), 25)
        for k in ("shock sizes EUR_BCBS2016", "shock sizes EUR_BCBS2024", "NMD caps", "post-shock floor"):
            self.assertIn(k, d["meta"]["assumptions"])
        self.assertEqual(len(d["meta"]["code_sha256"]), 64)

    def test_reference_bank_is_deterministic_and_consistent(self):
        cfg = self.s.IRRBBConfig(m=2)
        a, ea = self.s.run_study(cfg)
        b, _ = self.s.run_study(cfg)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        sc = [r for r in a if r["table"] == "scenario" and r["preset"] == "EUR_BCBS2016"]
        worst = min(sc, key=lambda r: r["delta_eve"])
        out = ea["outlier_tests"]["EUR_BCBS2016"]
        self.assertEqual(worst["scenario"], out["eve_worst_scenario"])
        self.assertAlmostEqual(-worst["delta_eve_tier1"], out["eve_ratio"])
        kr = [r for r in a if r["table"] == "key_rate"]
        for r in kr:
            self.assertAlmostEqual(r["dv01_net"], r["dv01_assets"] + r["dv01_liabilities"])

    def test_duration_check_orders(self):
        rows = [r for r in self.s.run_study(self.s.IRRBBConfig(m=2))[0] if r["table"] == "duration"]
        for r in rows:
            self.assertLessEqual(abs(r["second_order"] - r["full"]), abs(r["first_order"] - r["full"]) + 1e-12)
        small = min(rows, key=lambda r: abs(r["shift_bp"]))
        self.assertLess(abs(small["first_order"] - small["full"]), 0.05 * abs(small["full"]))

    def test_save_results_needs_full_mode(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.s.main(["--mode", "smoke", "--save-results"])


if __name__ == "__main__":
    unittest.main()
