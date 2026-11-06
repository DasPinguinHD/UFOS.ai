"""Valuation study: smoke mode end to end, consistency of its parts, reproducibility, synthetic labelling."""
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

STUDY = Path(__file__).resolve().parents[1] / "studies" / "valuation_sensitivity.py"


def _load():
    spec = importlib.util.spec_from_file_location("valuation_sensitivity", STUDY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class ValuationStudyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = _load()
        with tempfile.TemporaryDirectory() as tmp:
            assert cls.s.main(["--mode", "smoke", "--out-dir", tmp, "--quiet"]) == 0
            cls.d = json.loads(Path(tmp, "results.json").read_text(encoding="utf-8"))
            cls.md = Path(tmp, "summary.md").read_text(encoding="utf-8")

    def test_tables_and_meta(self):
        tables = {r["table"] for r in self.d["rows"]}
        self.assertEqual(tables, {"implied", "base_rate_curve", "base_rate_ref", "tornado", "two_way", "sobol"})
        self.assertTrue(self.d["meta"]["synthetic"])
        self.assertIn("invented", self.d["meta"]["company"]["name"])
        self.assertIn("synthetic", self.md.lower())
        self.assertEqual({(r["model"], r["output"]) for r in self.d["rows"] if r["table"] == "sobol"},
                         {("dcf", "value_per_share"), ("ishigami", "y"), ("irrbb", "worst_eve_tier1"),
                          ("ecl", "coverage")})

    def test_reference_price_is_the_base_value(self):
        ref = self.d["meta"]["reference"]
        bars = [r for r in self.d["rows"] if r["table"] == "tornado" and r["output"] == "value_per_share"]
        for b in bars:
            self.assertAlmostEqual(b["base_y"], ref["price"], places=8)
        implied = {r["price"]: r for r in self.d["rows"] if r["table"] == "implied"}
        self.assertAlmostEqual(implied[ref["price"]]["implied_growth"], ref["implied_growth"], places=10)
        g = [implied[p]["implied_growth"] for p in sorted(implied)]
        self.assertEqual(g, sorted(g))                               # higher price -> higher implied growth

    def test_base_rate_ordering(self):
        for r in self.d["rows"]:
            if r["table"] == "base_rate_ref" and r["kappa"] > 0:
                self.assertGreater(r["survivors_only"], r["full_cohort"])

    def test_sobol_inputs_equal_the_tornado(self):
        names = {r["name"] for r in self.d["rows"] if r["table"] == "sobol" and r["model"] == "dcf"}
        bars = {r["name"] for r in self.d["rows"] if r["table"] == "tornado" and r["output"] == "value_per_share"}
        self.assertEqual(names, bars)
        dists, ranges = self.d["meta"]["dcf_dists"], self.d["meta"]["dcf_ranges"]
        for k in dists:
            self.assertAlmostEqual(dists[k][0], ranges[k][0], places=12)
            self.assertAlmostEqual(dists[k][1], ranges[k][1], places=12)

    def test_dcf_vector_with_mixed_years(self):
        import numpy as np
        from risklab.valuation import dcf as D
        p = {"fcf0": np.array([100.0, 100.0]), "discount_rate": np.array([0.09, 0.09]),
             "terminal_growth": np.array([0.02, 0.02]), "growth": np.array([0.05, 0.05]),
             "net_debt": np.array([300.0, 300.0]), "years": np.array([7.0, 13.0])}
        v = self.s._dcf_vec(p)
        for i, n in enumerate((7, 13)):
            self.assertAlmostEqual(v[i], D.value_per_share(D.DCFInputs(100.0, 0.09, 0.02, 0.05, n, False, 300.0, 50.0,
                                                                         100.0)), places=10)

    def test_ecl_path_uses_the_same_basis_staging(self):
        """Part E goes through ifrs9.portfolio_ecl: the scenario weight and severity never change the stage;
        pd_scale (a deterioration of the current PD) does."""
        import dataclasses
        from risklab.credit import ifrs9 as F
        loans = self.s.loan_book(self.s.ValuationConfig(n_loans=200))
        f = self.s.ecl_coverage(loans)
        stage2 = set()
        for w in (0.1, 0.3, 0.5):
            for sev in (0.5, 1.5):
                sc = (F.Scenario("upside", 0.2, (0.8, 0.9)), F.Scenario("base", round(0.8 - w, 12), ()),
                      F.Scenario("downside", w, tuple(1.0 + sev * (m - 1.0) for m in self.s.DOWNSIDE)))
                stage2.add(F.portfolio_ecl(loans, sc)["stage2"])
        self.assertEqual(len(stage2), 1)
        lo = F.portfolio_ecl([dataclasses.replace(x, pd_now=x.pd_now * 0.8) for x in loans])["stage2"]
        hi = F.portfolio_ecl([dataclasses.replace(x, pd_now=min(1.0, x.pd_now * 1.2)) for x in loans])["stage2"]
        self.assertLess(lo, hi)
        base = dict(self.s.ECL_BASE)
        self.assertLess(f({**base, "downside_weight": 0.1}), f({**base, "downside_weight": 0.5}))

    def test_reproducible(self):
        cfg = self.s.ValuationConfig(m=2, n_sobol=64, n_irrbb=4, n_ecl=4, n_boot=10, n_loans=20,
                                     prices=(20.0, 30.0), hurdles=(0.0, 0.1))
        a, b = self.s.run_study(cfg), self.s.run_study(cfg)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    def test_save_results_needs_full_mode(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.s.main(["--mode", "smoke", "--save-results"])


if __name__ == "__main__":
    unittest.main()
