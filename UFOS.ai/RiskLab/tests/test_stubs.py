"""Derive-first rule: every sheet-1 formula is still a stub."""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: tests run without installing the package
    sys.path.insert(0, _SRC)

import unittest

import numpy as np

from risklab.backtest import christoffersen_cc, christoffersen_ind, kupiec_pof, traffic_light
from risklab.conventions import SHEET1_MESSAGE
from risklab.models import HistoricalSimulation, NormalModel, RiskModel, StudentTModel


class StubTests(unittest.TestCase):
    def assertSheet1(self, fn, *args):
        with self.assertRaises(NotImplementedError) as ctx:
            fn(*args)
        self.assertEqual(str(ctx.exception), SHEET1_MESSAGE)

    def test_message(self):
        self.assertEqual(SHEET1_MESSAGE, "sheet 1 — implemented together with the user")

    def test_models(self):
        for model in (HistoricalSimulation(), NormalModel(), StudentTModel(), StudentTModel(nu=5)):
            with self.subTest(model.name):
                self.assertIsInstance(model, RiskModel)
                self.assertSheet1(model.fit, np.zeros(10))
                self.assertSheet1(model.forecast, [0.99])
        with self.assertRaises(ValueError):
            StudentTModel(nu=2)

    def test_backtests(self):
        exc = np.zeros(250, dtype=bool)
        for fn in (kupiec_pof, christoffersen_ind, christoffersen_cc, traffic_light):
            with self.subTest(fn.__name__):
                self.assertSheet1(fn, exc, 0.99)


if __name__ == "__main__":
    unittest.main()
