"""Report tables: golden tests on small fixed results (independent expected strings)."""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: tests run without installing the package
    sys.path.insert(0, _SRC)

import tempfile
import unittest
from pathlib import Path

from risklab.report import tables as T

RESULTS = Path(__file__).resolve().parents[1] / "studies" / "results"

EW = {"meta": {"config": {"T": 1000, "m": 4}},
      "rows": [{"model": "oracle", "window": None, "exceed_rate": 0.0101, "exceed_se": 0.0003, "reject_kupiec": 0.05,
                "se_kupiec": 0.0109, "reject_binom": 0.04, "se_binom": 0.0098, "reject_cc": 0.02, "se_cc": 0.007,
                "reject_z2": 0.045, "se_z2": 0.0104, "var_rel_error": 0.0, "ref_kupiec": 0.0551, "ref_binom": 0.0365,
                "exceed_diff": 0.0, "exceed_diff_se": 0.0},
               {"model": "garch_t", "window": 250, "exceed_rate": 0.0125, "exceed_se": 0.0004, "reject_kupiec": 0.106,
                "se_kupiec": 0.0097, "reject_binom": None, "se_binom": None, "reject_cc": 0.062, "se_cc": 0.0076,
                "reject_z2": 0.177, "se_z2": 0.0121, "var_rel_error": 0.115, "ref_kupiec": 0.0551, "ref_binom": 0.0365,
                "exceed_diff": 0.00243, "exceed_diff_se": 0.00012}]}

EXPECTED_T4 = r"""\begin{table}[H]
\centering
\scriptsize
\caption{Estimation window: correctly specified GARCH(1,1)-t and FHS on synthetic GARCH-t6 data, T = 1000 (same forecast days for every window), M = 4; rates in \% ($\pm$ se). Reference sizes: Kupiec 5.51, binomial 3.65 (exact); cc and Z2: the true-VaR row. $\Delta$: paired difference of the exceedance rate against the true VaR on the same days (percentage points $\pm$ se); positive = anti-conservative. The 1000-day rows use the same design as the 1000-day rows of the power study (checked per replication); only the data seeds differ, so differences between the two studies are Monte-Carlo noise.}
\label{tab:T4_estimation_window}
\setlength{\tabcolsep}{3.5pt}
\begin{tabular}{lrrrrrrrr}
\toprule
Model & Window & Exceed.\ (\%) & $\Delta$ exceed.\ (pp) & Kupiec & Binomial & cc & Z2 & VaR error (\%) \\
\midrule
true VaR & -- & 1.01 $\pm$ 0.03 & -- & 5.0 $\pm$ 1.1 & 4.0 $\pm$ 1.0 & 2.0 $\pm$ 0.7 & 4.5 $\pm$ 1.0 & 0.0 \\
garch\_t & 250 & 1.25 $\pm$ 0.04 & +0.243 $\pm$ 0.012 & 10.6 $\pm$ 1.0 & -- & 6.2 $\pm$ 0.8 & 17.7 $\pm$ 1.2 & 11.5 \\
\bottomrule
\end{tabular}
\end{table}
"""


class FormatTests(unittest.TestCase):
    def test_escape_and_numbers(self):
        self.assertEqual(T.esc("a_b & 5% #1 {x}"), r"a\_b \& 5\% \#1 \{x\}")
        self.assertEqual(T.pct(0.0948, 2), "9.48")
        self.assertEqual(T.pct(None), "--")
        self.assertEqual(T.pm(0.106, 0.0097), r"10.6 $\pm$ 1.0")
        self.assertEqual(T.pm(0.106, None), "10.6")

    def test_golden_estimation_window(self):
        self.assertEqual(T.table_estimation_window(EW), EXPECTED_T4)

    def test_golden_survivorship(self):
        row = {"h0": 0.04, "kappa": 1.0, "threshold": 0.1, "survival_rate": 0.517, "naive": 0.343,
               "counterfactual": 0.256, "true": 0.178, "bias": 0.165, "bias_se": 0.0005, "selection": 0.087,
               "selection_se": 0.0006, "attrition": 0.078, "attrition_se": 0.0004}
        zero = dict(row, h0=0.0, kappa=0.0, survival_rate=1.0, naive=0.256, true=0.256, bias=0.0, bias_se=0.0,
                    selection=0.0, selection_se=0.0, attrition=0.0, attrition_se=0.0)
        skipped = dict(row, threshold=0.2)  # not one of the T6 thresholds
        text = T.table_survivorship({"meta": {"config": {"n_firms": 50, "m": 3}}, "rows": [zero, row, skipped]})
        self.assertIn(r"0 & 0 & 100.0 & 10 & 25.6 & 25.6 & 25.6 & 0.0 $\pm$ 0.00 & 0.0 $\pm$ 0.00 & 0.0 $\pm$ 0.00 \\",
                      text)
        self.assertIn(r"0.04 & 1 & 51.7 & 10 & 34.3 & 25.6 & 17.8 & 8.7 $\pm$ 0.06 & 7.8 $\pm$ 0.04 & 16.5 $\pm$ 0.05 \\",
                      text)
        self.assertNotIn(" & 20 & ", text)
        self.assertIn("Selection = naive $-$ counterfactual", text)

    def test_golden_findings(self):
        def prow(gen, model, window, T, **kw):
            base = {"generator": gen, "model": model, "window": window, "T": T, "exceed_rate": 0.0125,
                    "reject_kupiec": 0.106, "se_kupiec": 0.0097, "reject_binom": 0.105, "se_binom": 0.0097,
                    "reject_cc": 0.062, "se_cc": 0.0076, "reject_ind": 0.02, "reject_z2": 0.177, "se_z2": 0.0121,
                    "exact_size_kupiec": 0.0948, "exact_size_binom": 0.0412, "ref_size_z2": 0.046}
            base.update(kw)
            return base

        power = {"rows": [prow("garch_t6", "oracle", 250, 250, reject_ind=0.009),
                          prow("garch_t6", "garch_t", 250, 1000), prow("garch_t6", "garch_t", 1000, 1000, reject_kupiec=0.058,
                                                                        se_kupiec=0.0074),
                          prow("garch_t6", "fhs", 250, 1000, reject_z2=0.336, se_z2=0.0149),
                          prow("garch_t6", "normal", 250, 1000, reject_cc=0.597, se_cc=0.0155, reject_z2=0.647,
                               se_z2=0.0151),
                          prow("iid_normal", "hs", 250, 1000, exceed_rate=0.0121)]}
        horizon = {"rows": [{"generator": "iid_t5", "method": "sqrt10_garch", "bias": 0.086, "bias_se": 0.003, "rmse": 0.138},
                            {"generator": "garch_t6", "method": "sqrt10_normal", "bias": -0.007, "bias_se": 0.009,
                             "rmse": 0.293},
                            {"generator": "garch_t6", "method": "bootstrap", "bias": 0.037, "bias_se": 0.012, "rmse": 0.372}]}
        ew = {"rows": [{"model": "oracle", "window": None, "exceed_rate": 0.0101},
                       {"model": "garch_t", "window": 250, "exceed_rate": 0.0127, "reject_kupiec": 0.118},
                       {"model": "garch_t", "window": 500, "exceed_rate": 0.0111, "reject_kupiec": 0.045,
                        "exceed_diff": 0.00096, "exceed_diff_se": 0.00008},
                       {"model": "fhs", "window": 750, "exceed_rate": 0.0109, "reject_kupiec": 0.027, "reject_z2": 0.063,
                        "se_z2": 0.0077}]}
        surv = {"meta": {"config": {"h0": 0.04}},
                "rows": [{"h0": 0.04, "kappa": 1.0, "threshold": 0.1, "naive": 0.343, "counterfactual": 0.256,
                          "true": 0.178, "selection": 0.087, "selection_se": 0.0006, "attrition": 0.078,
                          "attrition_se": 0.0004, "bias": 0.165, "bias_se": 0.0005}]}
        f = {x["id"]: x for x in T.findings(power, horizon, ew, surv)}
        self.assertEqual(sorted(f), [f"F{i}" for i in range(1, 10)])
        self.assertEqual({k: v["severity"] for k, v in f.items()},
                         {"F1": "High", "F2": "Medium", "F3": "High", "F4": "Medium", "F5": "Low", "F6": "Medium",
                          "F7": "Low", "F8": "Low", "F9": "Medium"})
        self.assertIn(r"10.6 $\pm$ 1.0\,\% vs exact size 9.5", f["F1"]["finding"])
        self.assertIn(r"With 1000 days: Kupiec 5.8 $\pm$ 0.7", f["F1"]["finding"])
        self.assertIn("anti-conservative: exceedance rate 1.09--1.11", f["F1"]["finding"])
        self.assertIn("1.01", f["F1"]["finding"])
        self.assertIn(r"at 500 days: +0.096 $\pm$ 0.008 pp", f["F1"]["finding"])
        self.assertIn("tail (ES)", f["F2"]["finding"])
        self.assertIn(r"with 750 days 6.3 $\pm$ 0.8", f["F2"]["finding"])
        self.assertIn("9.48", f["F4"]["finding"])
        self.assertIn("0.9--0.9", f["F5"]["finding"])
        self.assertIn("+8.6", f["F6"]["finding"])
        self.assertIn("near-normal", f["F6"]["remediation"])
        self.assertIn("1.21", f["F8"]["finding"])
        self.assertIn(r"selection bias 8.7 $\pm$ 0.06 pp", f["F9"]["finding"])
        self.assertIn(r"attrition 7.8 $\pm$ 0.04 pp", f["F9"]["finding"])
        for x in f.values():
            self.assertTrue(x["evidence"] and x["remediation"])


GO_CAL = {"meta": {"config": {"data": ["iid_t"], "years": [10], "ks": [1, 64, 256], "m": 100},
                   "calibration": [{"data": "iid_t", "years": 10, "k": k, "m": 100, "fail_any": f, "fail_any_se": se,
                                    "fail_g4_or_g5": g, "fail_g4_or_g5_se": gse}
                                   for k, f, se, g, gse in ((1, 0.99, 0.01, 0.98, 0.014), (64, 1.0, 0.0, 0.97, 0.017),
                                                            (256, 1.0, 0.0, 1.0, 0.0))]},
          "rows": []}

EXPECTED_O5 = r"""\begin{table}[H]
\centering
\scriptsize
\caption{Calibration claims of SPEC \S 9.6 on noise (standard preset): share of noise paths that fail at least one in-scope gate (claim: $\ge$ 95\,\%) and share of best-of-64 noise selections that fail G4 or G5 (claim: all). M = 100 paths per cell, \%, $\pm$ Monte-Carlo se; a rate of 0 or 100 shows the 95\,\% bound of the rule of three in brackets (3 / n).}
\label{tab:O5_calibration}
\begin{tabular}{lrrrrr}
\toprule
Data & Years & Fail $\ge$1, $K$=64 & Fail G4/G5, $K$=64 & Fail $\ge$1, $K$=256 & Fail G4/G5, $K$=256 \\
\midrule
iid $t_5$ & 10 & 100 ($\ge$ 97) & 97.0 $\pm$ 1.7 & 100 ($\ge$ 97) & 100 ($\ge$ 97) \\
\bottomrule
\end{tabular}
\end{table}
"""


class OverfitTableTests(unittest.TestCase):
    def test_golden_calibration(self):
        self.assertEqual(T.table_go_calibration(GO_CAL), EXPECTED_O5)

    def test_zero_and_full_rates_show_the_rule_of_three(self):
        self.assertEqual(T.pmr(0.0, 0.0, 2000), "0 ($\\le$ 0.15)")
        self.assertEqual(T.pmr(1.0, 0.0, 2000), "100 ($\\ge$ 99.85)")
        self.assertEqual(T.pmr(0.5, 0.0112, 2000), "50.0 $\\pm$ 1.1")
        self.assertEqual(T.pmr(None, None, 0), "--")

    def test_gates_scope_lists_out_of_scope(self):
        go = {"meta": {"presets": {"lenient": {"g1_folds_min": 3, "g2_alpha_t": 1.5, "g4_dsr_min": 0.9,
                                               "g5_pbo_max": 0.5, "g6_neighbour_ratio": 0.4, "g7_min_trades": 50},
                                   "standard": {"g1_folds_min": 4, "g2_alpha_t": 2.0, "g4_dsr_min": 0.95,
                                                "g5_pbo_max": 0.3, "g6_neighbour_ratio": 0.5, "g7_min_trades": 100},
                                   "strict": {"g1_folds_min": 5, "g2_alpha_t": 3.0, "g4_dsr_min": 0.99,
                                              "g5_pbo_max": 0.2, "g6_neighbour_ratio": 0.6, "g7_min_trades": 200}}}}
        text = T.table_go_gates_scope(go)
        self.assertEqual(text.count("out of scope"), 2)            # G3, G8
        self.assertIn("4 of 5", text)
        self.assertIn("$\\le$ 0.30", text)
        self.assertIn("$\\ge$ 200", text)

    def test_unknown_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                T.write_all(RESULTS, tmp, doc="nope")

    def test_write_all_overfitting_from_saved_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = T.write_all(RESULTS, tmp, doc="overfitting")
            self.assertEqual([p.stem for p in paths], list(T.OVERFIT_TABLE_NAMES))
            for p in paths:
                text = p.read_text(encoding="utf-8")
                self.assertIn(f"\\label{{tab:{p.stem}}}", text)
                self.assertEqual(text.count("\\begin{tabular}"), text.count("\\end{tabular}"))
                self.assertNotIn("nan", text.lower().replace("\\nano", ""))
            findings = (Path(tmp) / "O6_findings.tex").read_text(encoding="utf-8")
            self.assertGreaterEqual(findings.count("PROPOSED:"), 7)
            for ref in ("tab:O2_fpr", "fig:fig9_power", "tab:O5_calibration"):
                self.assertIn(ref, findings)


class OverviewTableTests(unittest.TestCase):
    def test_key_results_from_saved_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = T.write_all(RESULTS, tmp, doc="overview")
            self.assertEqual([p.stem for p in paths], list(T.OVERVIEW_TABLE_NAMES))
            text = paths[0].read_text(encoding="utf-8")
        self.assertIn("\\label{tab:V1_key_results}", text)
        self.assertEqual(text.count(" \\\\\n"), 8)     # header + 7 result rows
        for study in ("size / power", "estimation window", "10-day horizon", "survivorship", "gate overfitting"):
            self.assertIn(study, text)


class IRRBBTableTests(unittest.TestCase):
    def test_irrbb_tables_from_saved_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = T.write_all(RESULTS, tmp, doc="irrbb")
            self.assertEqual([p.stem for p in paths], list(T.IRRBB_TABLE_NAMES))
            texts = {p.stem: p.read_text(encoding="utf-8") for p in paths}
        for name, text in texts.items():
            self.assertIn(f"\\label{{tab:{name}}}", text)
            self.assertNotRegex(text, r"[^\\]%")              # every percent sign escaped
        self.assertGreaterEqual(texts["R4_findings"].count("PROPOSED:"), 6)
        self.assertIn("ASSUMPTION", texts["R3_assumptions"])
        self.assertEqual(texts["R1_scenarios"].count(" \\\\\n"), 7)     # header + 6 scenarios

    def test_rule_of_three_helpers(self):
        self.assertEqual(T._pmr2(0.9995, 0.0005, 2000), "99.95 $\\pm$ 0.05")
        self.assertEqual(T._pmr2(1.0, 0.0, 2000), "100 ($\\ge$ 99.85)")
        self.assertEqual(T._years(1.0), "1 year")


class WriteAllTests(unittest.TestCase):
    def test_write_all_from_saved_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = T.write_all(RESULTS, tmp)
            self.assertEqual([p.stem for p in paths], list(T.TABLE_NAMES))
            for p in paths:
                text = p.read_text(encoding="utf-8")
                self.assertIn(f"\\label{{tab:{p.stem}}}", text)
                self.assertEqual(text.count("\\begin{tabular}"), text.count("\\end{tabular}"))
            findings = (Path(tmp) / "T7_findings.tex").read_text(encoding="utf-8")
            self.assertGreaterEqual(findings.count("PROPOSED:"), 8)
            for ref in ("tab:T3_power", "fig:fig3_estimation_window", "tab:T2_size"):
                self.assertIn(ref, findings)


if __name__ == "__main__":
    unittest.main()
