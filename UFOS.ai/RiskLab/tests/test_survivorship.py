"""Survivorship-bias study: zero hazard gives zero bias; the bias grows with the hazard; reproducible."""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: tests run without installing the package
    sys.path.insert(0, _SRC)

import importlib.util
import tempfile
import unittest
from pathlib import Path

import numpy as np

STUDY = Path(__file__).resolve().parents[1] / "studies" / "survivorship.py"


def _load():
    spec = importlib.util.spec_from_file_location("survivorship", STUDY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class SurvivorshipTests(unittest.TestCase):
    def setUp(self):
        self.s = _load()
        self.cfg = self.s.SurvivorConfig(m=20, n_firms=500)

    def test_zero_hazard_zero_bias(self):
        rows = self.s.run_setting(self.cfg, 0.0, 2.0, [1, 2])
        for r in rows:
            self.assertEqual(r["bias"], 0.0)
            self.assertEqual(r["selection"], 0.0)
            self.assertEqual(r["attrition"], 0.0)
            self.assertEqual(r["survival_rate"], 1.0)
            self.assertEqual(r["naive"], r["true"])
            self.assertEqual(r["naive"], r["counterfactual"])

    def test_decomposition(self):
        for k in (0.0, 1.0):
            for r in self.s.run_setting(self.cfg, 0.04, k, [1, 4]):
                self.assertAlmostEqual(r["bias"], r["selection"] + r["attrition"], places=12)
                self.assertGreater(r["attrition"], 0)
                if k == 0.0:  # exits unrelated to growth: survivors are a random sample
                    self.assertLess(abs(r["selection"]), 4 * r["selection_se"] + 1e-12)
                else:
                    self.assertGreater(r["selection"], 4 * r["selection_se"])

    def test_counterfactual_by_hand(self):
        path = np.array([0.12, 0.05, 0.11, 0.20])
        self.assertEqual(self.s.counterfactual_rate(path, 0.10), 3 / 4)

    def test_bias_grows_with_hazard_strength(self):
        biases = []
        for k in (0.0, 1.0, 2.0):
            rows = self.s.run_setting(self.cfg, 0.04, k, [1, 3])
            biases.append(next(r for r in rows if r["threshold"] == 0.10))
        for a, b in zip(biases, biases[1:]):
            self.assertGreater(b["bias"] - a["bias"], 3 * (a["bias_se"] + b["bias_se"]))

    def test_base_rates_by_hand(self):
        alive = np.array([True, True, False, True])
        cagr = np.array([0.12, 0.05, np.nan, 0.20])
        naive, true = self.s.base_rates(alive, cagr, 0.10)
        self.assertEqual((naive, true), (2 / 3, 2 / 4))

    def test_reproducible(self):
        a = self.s.run_setting(self.cfg, 0.04, 1.0, [7, 1])
        b = self.s.run_setting(self.cfg, 0.04, 1.0, [7, 1])
        self.assertEqual(a, b)

    def test_smoke_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.s.main(["--mode", "smoke", "--quiet", "--out-dir", tmp]), 0)
            text = Path(tmp, "summary.md").read_text(encoding="utf-8")
            self.assertIn("No real firms or data", text)


if __name__ == "__main__":
    unittest.main()
