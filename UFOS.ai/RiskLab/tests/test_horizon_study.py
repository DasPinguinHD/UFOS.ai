"""The 10-day horizon study runs in smoke mode and writes deterministic outputs."""
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

STUDY = Path(__file__).resolve().parents[1] / "studies" / "horizon_10d.py"


def _load():
    spec = importlib.util.spec_from_file_location("horizon_10d", STUDY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses need the module registered
    spec.loader.exec_module(mod)
    return mod


class HorizonStudySmokeTests(unittest.TestCase):
    def test_smoke_run(self):
        study = _load()
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp, "a"), Path(tmp, "b")
            self.assertEqual(study.main(["--mode", "smoke", "--quiet", "--out-dir", str(a)]), 0)
            self.assertEqual(study.main(["--mode", "smoke", "--quiet", "--out-dir", str(b)]), 0)
            for name in ("results.csv", "results.json", "summary.md"):
                self.assertEqual((a / name).read_bytes(), (b / name).read_bytes(), name)
            d = json.loads((a / "results.json").read_text(encoding="utf-8"))
            self.assertEqual(len(d["rows"]), len(study.GENERATORS) * len(study.METHODS))
            for row in d["rows"]:
                self.assertEqual(row["failed_reps"], 0)
                self.assertEqual(row["block_undefined_reps"], 0)
                self.assertTrue(abs(row["bias"]) < 1.0)
                if row["generator"] == "iid_normal":
                    self.assertEqual(row["truth_mc_rel_se"], 0.0)  # closed-form truth
                if row["method"] in ("garch_mc", "bootstrap"):
                    self.assertGreater(row["est_mc_rel_se"], 0.0)
            text = (a / "summary.md").read_text(encoding="utf-8")
            self.assertIn("is a demo", text)
            self.assertIn("| garch_t6 | garch_mc |", text)

    def test_mc_seeds_independent(self):
        study = _load()
        s = study.mc_seeds(777)
        self.assertEqual(len(set(s)), 3)
        self.assertNotIn(777, s)
        self.assertNotIn(778, s)
        self.assertEqual(s, study.mc_seeds(777))

    def test_undefined_block_length_is_counted(self):
        study = _load()
        from risklab.bootstrap import BlockLengthError

        def boom(*args, **kwargs):
            raise BlockLengthError("g0 <= 0 (test)")

        study._one = boom
        rows = study.run_study(study.HorizonConfig(m=2, n_paths=500, truth_sims=500))
        self.assertTrue(all(r["block_undefined_reps"] == 2 and r["m_ok"] == 0 for r in rows))
        self.assertIn("2 undefined auto block lengths", study.summary_markdown(rows, study.HorizonConfig(m=2)))

    def test_failed_fits_are_counted(self):
        study = _load()
        from risklab.models import FitError

        def boom(*args, **kwargs):
            raise FitError("did not converge (test)")

        study._one = boom
        rows = study.run_study(study.HorizonConfig(m=2, n_paths=500, truth_sims=500))
        self.assertTrue(all(r["failed_reps"] == 2 and r["m_ok"] == 0 and r["bias"] is None for r in rows))
        self.assertIn("2 failed GARCH fits", study.summary_markdown(rows, study.HorizonConfig(m=2)))


if __name__ == "__main__":
    unittest.main()
