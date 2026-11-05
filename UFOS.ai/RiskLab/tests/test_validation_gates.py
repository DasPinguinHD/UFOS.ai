"""Gate rules at their boundaries for every preset; the parameter grid, its neighbours and the folds."""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: tests run without installing the package
    sys.path.insert(0, _SRC)

import unittest

import numpy as np

from risklab.validation import folds, gates as G, grid


class GateBoundaryTests(unittest.TestCase):
    def test_presets_complete(self):
        self.assertEqual(set(G.PRESETS), set(G.PRESET_NAMES))
        for th in G.PRESETS.values():
            self.assertEqual(set(th), {"g1_folds_min", "g2_alpha_t", "g4_dsr_min", "g5_pbo_max",
                                       "g6_neighbour_ratio", "g7_min_trades"})

    def test_boundaries_per_preset(self):
        for name, th in G.PRESETS.items():
            with self.subTest(preset=name):
                k = int(th["g1_folds_min"])
                self.assertTrue(G.g1([1.0] * k + [-1.0] * (5 - k), th)[0])
                self.assertFalse(G.g1([1.0] * (k - 1) + [-1.0] * (6 - k), th)[0])
                self.assertFalse(G.g1([0.0] * 5, th)[0])                       # Sharpe 0 is not > 0
                self.assertFalse(G.g1([1.0] * 4, th)[0])                       # needs 5 folds
                self.assertFalse(G.g1([1.0] * 4 + [None], th)[0] and k == 5)
                t = th["g2_alpha_t"]
                self.assertTrue(G.g2({"value": t, "alpha_daily": 1e-6}, th)[0])
                self.assertFalse(G.g2({"value": t - 1e-9, "alpha_daily": 1e-6}, th)[0])
                self.assertFalse(G.g2({"value": t + 1, "alpha_daily": 0.0}, th)[0])
                self.assertFalse(G.g2({"value": None, "alpha_daily": None}, th)[0])
                d = th["g4_dsr_min"]
                self.assertTrue(G.g4({"value": d}, th)[0])
                self.assertFalse(G.g4({"value": d - 1e-9}, th)[0])
                self.assertFalse(G.g4({"value": None}, th)[0])
                p = th["g5_pbo_max"]
                self.assertTrue(G.g5({"value": p, "configs": 4}, th)[0])
                self.assertFalse(G.g5({"value": p + 1e-9, "configs": 64}, th)[0])
                self.assertFalse(G.g5({"value": 0.0, "configs": 3}, th)[0])
                self.assertFalse(G.g5({"value": None, "configs": 64}, th)[0])
                q = th["g6_neighbour_ratio"]
                self.assertTrue(G.g6_neighbours(2.0, [2 * q, 3.0], th)[0])
                self.assertFalse(G.g6_neighbours(2.0, [2 * q - 1e-9, 3.0], th)[0])
                self.assertFalse(G.g6_neighbours(0.0, [1.0], th)[0])
                self.assertFalse(G.g6_neighbours(1.0, [], th)[0])
                self.assertFalse(G.g6_neighbours(1.0, [1.0, None], th)[0])
                n = int(th["g7_min_trades"])
                self.assertTrue(G.g7(n, th)[0])
                self.assertFalse(G.g7(n - 1, th)[0])
                self.assertFalse(G.g7(None, th)[0])

    def test_holdout(self):
        self.assertTrue(G.holdout(0.5, 1.0)[0])
        self.assertFalse(G.holdout(0.4999, 1.0)[0])
        self.assertFalse(G.holdout(0.0, -1.0)[0])
        self.assertTrue(G.holdout(0.01, -1.0)[0])
        self.assertFalse(G.holdout(None, 1.0)[0])
        self.assertFalse(G.holdout(1.0, None)[0])


class GridTests(unittest.TestCase):
    def test_axis_values(self):
        self.assertEqual(grid.axis_values({"min": 1, "max": 2, "step": 0.25, "value": 1.6}),
                         [1.0, 1.25, 1.5, 1.6, 1.75, 2.0])
        self.assertEqual(grid.axis_values({"min": 2, "max": 9, "step": 3, "value": 4, "integer": True}), [2, 4, 5, 8])
        with self.assertRaises(ValueError):
            grid.axis_values({"min": 0, "max": 1, "step": 0, "value": 0})

    def test_small_grid_and_neighbours(self):
        P = {"b": {"min": 8, "max": 12, "step": 1, "value": 10, "integer": True},
             "a": {"min": 0.0, "max": 1.0, "step": 0.5, "value": 0.0}}
        g = grid.build(P)
        self.assertEqual(list(g["axes"]), ["a", "b"])
        self.assertEqual(g["total"], 15)
        self.assertEqual(g["configs"][g["base_index"]], {"a": 0.0, "b": 10})
        nb = [g["configs"][i] for i in grid.neighbours(g, P)]
        # a: base 0 -> 20 % of the range (0.2): only a = 0; b within 2 of 10
        self.assertEqual(nb, [{"a": 0.0, "b": 8}, {"a": 0.0, "b": 9}, {"a": 0.0, "b": 11}, {"a": 0.0, "b": 12}])

    def test_subsample_forces_base(self):
        P = {"fast": {"min": 4, "max": 19, "step": 1, "value": 9, "integer": True},
             "slow": {"min": 24, "max": 69, "step": 3, "value": 45, "integer": True}}
        g = grid.build(P)
        self.assertEqual(g["total"], 256)
        self.assertTrue(g["subsampled"])
        self.assertEqual(len(g["configs"]), 64)
        self.assertEqual(g["configs"][g["base_index"]], {"fast": 9, "slow": 45})
        even = sorted({round(i * 255 / 63) for i in range(64)})
        self.assertEqual(len(set(g["picks"]) - set(even)), 1 if (9 - 4) * 16 + 7 not in even else 0)
        self.assertEqual(g["picks"][0], 0)
        self.assertEqual(g["picks"][-1], 255)


class FoldTests(unittest.TestCase):
    def test_bounds_like_array_split(self):
        for n in (5, 11, 1260, 2521):
            ref = [(int(p[0]), int(p[-1])) for p in np.array_split(np.arange(n), 5)]
            self.assertEqual(folds.fold_bounds(n), ref)
        with self.assertRaises(ValueError):
            folds.fold_bounds(4)

    def test_fold_sharpes(self):
        r = np.concatenate([np.full(10, 0.01), np.full(10, -0.01)]) + np.tile([0.001, -0.001], 10)
        s = folds.fold_sharpes(r, 252)
        self.assertEqual(len(s), 5)
        self.assertGreater(s[0], 0)
        self.assertLess(s[-1], 0)


if __name__ == "__main__":
    unittest.main()
