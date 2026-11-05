"""The size / power study runs in smoke mode and writes deterministic outputs."""
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

STUDY = Path(__file__).resolve().parents[1] / "studies" / "var_backtest_power.py"


def _load():
    spec = importlib.util.spec_from_file_location("var_backtest_power", STUDY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses need the module registered
    spec.loader.exec_module(mod)
    return mod


class StudySmokeTests(unittest.TestCase):
    def test_smoke_run(self):
        study = _load()
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp, "a"), Path(tmp, "b")
            self.assertEqual(study.main(["--mode", "smoke", "--quiet", "--out-dir", str(a)]), 0)
            self.assertEqual(study.main(["--mode", "smoke", "--quiet", "--out-dir", str(b)]), 0)
            for name in ("results.csv", "results.json", "summary.md"):
                self.assertTrue((a / name).is_file(), name)
                self.assertEqual((a / name).read_bytes(), (b / name).read_bytes(), name)
            d = json.loads((a / "results.json").read_text(encoding="utf-8"))
            cfg = study.StudyConfig(horizons=(250,))
            expected = sum(len(study._norm_case(c, cfg)[4]) for c in study.CASES)
            self.assertEqual(len(d["rows"]), expected)  # the 1000-day-window rows need T = 1000
            self.assertEqual(d["meta"]["config"]["m"], study.MODES["smoke"])
            for row in d["rows"]:
                for t in study.TESTS:
                    r = row[f"reject_{t}"]
                    self.assertTrue(0.0 <= r <= 1.0)
                    self.assertAlmostEqual(row[f"se_{t}"], (r * (1 - r) / row["m_ok"]) ** 0.5, places=15)
                if row["model"] == "oracle":
                    self.assertEqual(row["var_rel_error"], 0.0)
                    self.assertEqual(row["ref_size_ind"], row["reject_ind"])
                self.assertAlmostEqual(row["exact_size_kupiec"], 0.09476, places=5)
                self.assertLessEqual(row["exact_size_binom"], 0.05)
                self.assertEqual(row["failed_reps"], 0)
                self.assertEqual(row["note"] == "not size-adjusted", row["role"] == "power")
            roles = {(r["generator"], r["model"]): r["role"] for r in d["rows"]}
            for gen, model in (("garch_t6", "garch_t"), ("garch_t6", "fhs")):
                self.assertEqual(roles[(gen, model)], "size")
            for model in ("garch_t", "fhs", "ewma_t"):
                self.assertEqual(roles[("regime", model)], "conditional")
            for row in d["rows"]:
                self.assertIsNotNone(row["ref_size_z2"])
            self.assertEqual(d["meta"]["config"]["alpha_es"], 0.975)
            hs = [r for r in d["rows"] if (r["generator"], r["model"]) == ("iid_normal", "hs")][0]
            self.assertIn("not a clean size row", hs["note"])
            text = (a / "summary.md").read_text(encoding="utf-8")
            for phrase in ("| size |", "9.48 %", "{0} ∪ {7..250}", "not size-adjusted", "is a demo",
                           "state-conditional oracle", "1.20 %", "ES Z2", "| conditional |",
                           "Static against conditional models", "reference size"):
                self.assertIn(phrase, text)

    def test_long_window_rows(self):
        study = _load()
        cases = [("garch_t6", "oracle", "size"), ("garch_t6", "garch_t", "size", 1000, (1000,)),
                 ("garch_t6", "fhs", "size", 1000, (1000,))]
        cfg = study.StudyConfig(m=1, horizons=(1000,), cases=cases)
        rows = study.run_study(cfg)
        by = {(r["model"], r["window"]): r for r in rows}
        self.assertEqual(set(by), {("oracle", 250), ("garch_t", 1000), ("fhs", 1000)})
        self.assertIn("1000-day estimation window", by[("garch_t", 1000)]["note"])
        self.assertEqual(by[("garch_t", 1000)]["ref_size_z2"], by[("oracle", 250)]["reject_z2"])
        text = study.summary_markdown(rows, cfg)
        self.assertIn("garch_t (window 1000)", text)
        # own data seeds for the long window; Monte-Carlo seeds independent of the data seed
        self.assertNotEqual(study.data_seed(1, 3, 1000, 0), study.data_seed(1, 3, 1000, 0, 1000))
        self.assertNotEqual(study.mc_seed(12345), 12345)
        self.assertEqual(study.mc_seed(12345), study.mc_seed(12345))

    def test_failed_fits_are_counted(self):
        study = _load()
        from risklab.models import FitError

        class Failing:
            name = "failing"

            def fit(self, losses_past):
                raise FitError("did not converge (test)")

        study.MODELS["student_t"] = Failing
        cfg = study.StudyConfig(m=2, horizons=(250,), cases=[("iid_t5", "student_t", "size")])
        rows = study.run_study(cfg)
        self.assertEqual((rows[0]["failed_reps"], rows[0]["m_ok"]), (2, 0))
        self.assertIsNone(rows[0]["reject_kupiec"])
        self.assertIn("2 failed fits", study.summary_markdown(rows, cfg))

    def test_seeds_are_deterministic_and_distinct(self):
        study = _load()
        self.assertEqual(study.data_seed(1, 0, 250, 0), study.data_seed(1, 0, 250, 0))
        self.assertNotEqual(study.data_seed(1, 0, 250, 0), study.data_seed(1, 0, 250, 1))
        self.assertNotEqual(study.data_seed(1, 0, 250, 0), study.data_seed(1, 1, 250, 0))


if __name__ == "__main__":
    unittest.main()
