"""Report figures: every figure function runs on tiny inputs; byte-level reproducibility (needs matplotlib)."""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: tests run without installing the package
    sys.path.insert(0, _SRC)

import tempfile
import unittest
from pathlib import Path

try:
    import matplotlib  # noqa: F401
    HAVE_MPL = True
except ImportError:
    HAVE_MPL = False

SKIP_MSG = "matplotlib is not installed (optional extra 'report'): report figure tests skipped"


def tiny_power():
    rows = []
    for model, w in (("oracle", 250), ("normal", 250), ("hs", 250), ("ewma_t", 250), ("garch_t", 250),
                     ("fhs", 250), ("garch_t", 1000), ("fhs", 1000)):
        k = 0.05 if model == "oracle" else 0.2
        rows.append({"generator": "garch_t6", "model": model, "window": w, "T": 1000, "exceed_rate": 0.012,
                     "reject_kupiec": k, "se_kupiec": 0.01, "reject_binom": k, "se_binom": 0.01, "reject_cc": k,
                     "se_cc": 0.01, "reject_z2": k, "se_z2": 0.01, "exact_size_kupiec": 0.055,
                     "exact_size_binom": 0.036, "tl_green": 0.7, "tl_yellow": 0.25, "tl_red": 0.05})
    return {"meta": {"config": {"window": 250, "m": 10}}, "rows": rows}


def tiny_horizon():
    rows = [{"generator": g, "method": m, "bias": 0.01 * i, "bias_se": 0.005, "rmse": 0.1}
            for g in ("iid_normal", "garch_t6") for i, m in enumerate(("sqrt10_normal", "garch_mc"))]
    return {"meta": {"config": {"alpha": 0.99, "window": 500, "m": 5}}, "rows": rows}


def tiny_ew():
    rows = [{"model": "oracle", "window": None, "exceed_rate": 0.01, "exceed_se": 0.001, "ref_kupiec": 0.055,
             "ref_z2": 0.05, "reject_kupiec": 0.05, "se_kupiec": 0.01, "reject_z2": 0.05, "se_z2": 0.01}]
    for model in ("garch_t", "fhs"):
        for w in (250, 500):
            rows.append({"model": model, "window": w, "exceed_rate": 0.012, "exceed_se": 0.001, "reject_kupiec": 0.1,
                         "se_kupiec": 0.02, "reject_z2": 0.1, "se_z2": 0.02})
    return {"meta": {"config": {"alpha": 0.99, "T": 1000, "m": 5}}, "rows": rows}


def tiny_surv():
    rows = [{"h0": h0, "kappa": k, "threshold": g, "naive": 0.3 + k / 10, "counterfactual": 0.28, "true": 0.2,
             "bias": 0.1 + k / 10, "bias_se": 0.01, "selection": 0.02 + k / 10, "selection_se": 0.01,
             "attrition": 0.08, "attrition_se": 0.01}
            for h0, k in ((0.0, 0.0), (0.04, 0.0), (0.04, 1.0)) for g in (0.05, 0.1)]
    return {"meta": {"config": {"n_firms": 100, "m": 5, "h0": 0.04}}, "rows": rows}


def tiny_go():
    """Rows of the gate overfitting study for a 2 x 2 x 2 x 2 x 3 grid (data, years, signal, K, preset)."""
    rows = []
    gates = ("G1", "G2", "G4", "G5", "G6", "G7", "H", "all", "all_h", "stat", "stat_h")
    for d in ("iid_t", "garch_t"):
        for y in (5, 10):
            for sr in (0.0, 1.0):
                for k in (1, 8):
                    for j, preset in enumerate(("lenient", "standard", "strict")):
                        p = min(0.95, 0.05 + 0.4 * sr + 0.02 * y - 0.1 * j + 0.01 * k)
                        r = {"data": d, "years": y, "signal": sr, "k": k, "preset": preset, "m": 10}
                        for g in gates:
                            r[f"pass_{g}"], r[f"pass_{g}_se"] = p, 0.05
                        r["h_given_g4"], r["h_given_g4_se"] = (0.6, 0.1) if sr else (None, None)
                        r["h_given_stat"], r["h_given_stat_se"] = 0.7, 0.1
                        rows.append(r)
    return {"meta": {"config": {"data": ["iid_t", "garch_t"], "years": [5, 10], "signals": [0.0, 1.0], "ks": [1, 8],
                                "m": 10}}, "rows": rows}


@unittest.skipUnless(HAVE_MPL, SKIP_MSG)
class OverfitFigureTests(unittest.TestCase):
    def _all(self, out):
        from risklab.report import figures as F

        go = tiny_go()
        return [F.fig_fpr_vs_k(go, out, years=10), F.fig_power(go, out, k=8), F.fig_gate_heat(go, out),
                F.fig_holdout(go, out, k=8)]

    def test_four_figures_reproducible(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = self._all(Path(tmp, "a")), self._all(Path(tmp, "b"))
            self.assertEqual([s.stem for s in a], ["fig8_fpr_vs_k", "fig9_power", "fig10_gate_heat", "fig11_holdout"])
            for x, y in zip(a, b):
                self.assertTrue(x.caption)
                for p, q in zip(x.paths, y.paths):
                    self.assertGreater(p.stat().st_size, 1000)
                    self.assertEqual(p.read_bytes(), q.read_bytes(), p.name)
                self.assertNotIn(b"/CreationDate", x.paths[1].read_bytes())


@unittest.skipUnless(HAVE_MPL, SKIP_MSG)
class CreditFigureTests(unittest.TestCase):
    def test_five_figures_from_a_smoke_run(self):
        import importlib.util
        import json

        from risklab.report import figures as F

        study = Path(__file__).resolve().parents[1] / "studies" / "credit_risk.py"
        spec = importlib.util.spec_from_file_location("credit_risk_fig", study)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as tmp:
            mod.main(["--mode", "smoke", "--out-dir", tmp, "--quiet"])
            res = json.loads(Path(tmp, "results.json").read_text(encoding="utf-8"))
            out = []
            for d in ("a", "b"):
                out.append([F.fig_cap_roc(res, Path(tmp, d)), F.fig_pd_sample_size(res, Path(tmp, d)),
                            F.fig_calibration_drift(res, Path(tmp, d)), F.fig_ecl_cliff(res, Path(tmp, d)),
                            F.fig_irb_vs_mc(res, Path(tmp, d))])
            for x, y in zip(*out):
                self.assertNotIn("%", x.caption.replace("\\%", ""))
                for p, q in zip(x.paths, y.paths):
                    self.assertGreater(p.stat().st_size, 1000)
                    self.assertEqual(p.read_bytes(), q.read_bytes())


@unittest.skipUnless(HAVE_MPL, SKIP_MSG)
class IRRBBFigureTests(unittest.TestCase):
    def test_five_figures_from_a_smoke_run(self):
        import importlib.util
        import json

        from risklab.report import figures as F

        study = Path(__file__).resolve().parents[1] / "studies" / "irrbb_scenarios.py"
        spec = importlib.util.spec_from_file_location("irrbb_scenarios_fig", study)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as tmp:
            mod.main(["--mode", "smoke", "--out-dir", tmp, "--quiet"])
            res = json.loads(Path(tmp, "results.json").read_text(encoding="utf-8"))
            out = []
            for d in ("a", "b"):
                out.append([F.fig_irrbb_curves(res, Path(tmp, d)), F.fig_irrbb_delta(res, Path(tmp, d)),
                            F.fig_key_rates(res, Path(tmp, d)), F.fig_nmd_heat(res, Path(tmp, d)),
                            F.fig_duration_check(res, Path(tmp, d))])
            for x, y in zip(*out):
                self.assertNotIn("%", x.caption.replace("\\%", ""))     # LaTeX-safe captions
                for p, q in zip(x.paths, y.paths):
                    self.assertGreater(p.stat().st_size, 1000)
                    self.assertEqual(p.read_bytes(), q.read_bytes())


@unittest.skipUnless(HAVE_MPL, SKIP_MSG)
class ArchitectureFigureTests(unittest.TestCase):
    def test_architecture_reproducible(self):
        from risklab.report import figures as F

        with tempfile.TemporaryDirectory() as tmp:
            a, b = F.fig_architecture(Path(tmp, "a")), F.fig_architecture(Path(tmp, "b"))
            self.assertEqual(a.stem, "fig12_architecture")
            for p, q in zip(a.paths, b.paths):
                self.assertGreater(p.stat().st_size, 1000)
                self.assertEqual(p.read_bytes(), q.read_bytes())
        names = {k for k, *_ in F.ARCH_BOXES}
        self.assertTrue(all(x in names and y in names for x, y in F.ARCH_EDGES))


@unittest.skipUnless(HAVE_MPL, SKIP_MSG)
class FigureTests(unittest.TestCase):
    def _all(self, out):
        from risklab.report import figures as F

        return [
            F.fig_exceedances(F.exceedance_data(seed=1, T=60, long_window=120, short_window=60), out),
            F.fig_rejection_rates(tiny_power(), out),
            F.fig_estimation_window(tiny_ew(), out),
            F.fig_traffic_light(tiny_power(), out),
            F.fig_horizon(tiny_horizon(), out),
            F.fig_qq(F.qq_data(seed=2, n=200), out),
            F.fig_survivorship(tiny_surv(), out),
        ]

    def test_each_figure_writes_png_and_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            specs = self._all(Path(tmp))
            self.assertEqual(len({s.stem for s in specs}), 7)
            for s in specs:
                self.assertEqual([p.suffix for p in s.paths], [".png", ".pdf"])
                for p in s.paths:
                    self.assertGreater(p.stat().st_size, 1000)
                self.assertTrue(s.paths[0].read_bytes().startswith(b"\x89PNG"))
                self.assertTrue(s.paths[1].read_bytes().startswith(b"%PDF"))
                self.assertTrue(s.caption)

    def test_reproducible_bytes_and_no_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp, "a"), Path(tmp, "b")
            for x, y in zip(self._all(a), self._all(b)):
                for p, q in zip(x.paths, y.paths):
                    self.assertEqual(p.read_bytes(), q.read_bytes(), p.name)
                pdf = x.paths[1].read_bytes()
                self.assertNotIn(b"/CreationDate", pdf)
                self.assertNotIn(b"matplotlib v", x.paths[0].read_bytes().lower())

    def test_snippet(self):
        from risklab.report import figures as F

        spec = F.FigureSpec("fig9_x", "A caption.", ())
        text = F.latex_snippet(spec)
        self.assertIn("\\includegraphics[width=\\linewidth]{figures/fig9_x.pdf}", text)
        self.assertIn("\\label{fig:fig9_x}", text)


if __name__ == "__main__":
    unittest.main()
