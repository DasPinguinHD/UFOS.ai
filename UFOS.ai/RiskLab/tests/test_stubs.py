"""Regression guard: no sheet-1 stub is left (v0.1 stubs were implemented in v0.2)."""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: tests run without installing the package
    sys.path.insert(0, _SRC)

import unittest

import numpy as np

from risklab.backtest import christoffersen_cc, christoffersen_ind, kupiec_pof, traffic_light
from risklab.models import HistoricalSimulation, NormalModel, StudentTModel


class NoStubsLeftTests(unittest.TestCase):
    def test_models_implemented(self):
        x = np.random.default_rng(1).standard_normal(300)
        for model in (HistoricalSimulation(), NormalModel(), StudentTModel(), StudentTModel(nu=5)):
            with self.subTest(model.name):
                model.fit(x)
                f = model.forecast([0.99])
                self.assertTrue(np.isfinite(f.var[0.99]))
                self.assertTrue(np.isfinite(f.es[0.99]))
                self.assertGreaterEqual(f.es[0.99], f.var[0.99])

    def test_backtests_implemented(self):
        exc = np.zeros(250, dtype=bool)
        exc[[10, 100]] = True
        for fn in (kupiec_pof, christoffersen_ind, christoffersen_cc, traffic_light):
            with self.subTest(fn.__name__):
                self.assertTrue(fn(exc, 0.99).decision)


if __name__ == "__main__":
    unittest.main()
