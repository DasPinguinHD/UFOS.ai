"""Walk-forward engine: exceedance counting and the no-look-ahead guarantee."""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: tests run without installing the package
    sys.path.insert(0, _SRC)

import unittest

import numpy as np

from risklab.engine import BacktestFrame, walk_forward
from risklab.models.base import Forecast, RiskModel


class ConstantVaR:
    """Test dummy: the same VaR every day (and optionally ES = VaR + 1)."""

    name = "constant"

    def __init__(self, level, with_es=False):
        self.level = level
        self.with_es = with_es

    def fit(self, losses_past):
        pass

    def forecast(self, alphas):
        var = {a: self.level for a in alphas}
        return Forecast(var=var, es={a: self.level + 1.0 for a in alphas} if self.with_es else None)


class Spy:
    """Records what fit() sees; its 'VaR' is the largest index it has seen."""

    name = "spy"
    log = []

    def fit(self, losses_past):
        Spy.log.append(np.array(losses_past))
        self.seen_max = float(np.max(losses_past))
        try:
            losses_past[0] = 1e9
            Spy.mutated = True
        except ValueError:
            pass

    def forecast(self, alphas):
        return Forecast(var={a: self.seen_max for a in alphas})


class EngineTests(unittest.TestCase):
    def test_constant_var_exceedances(self):
        losses = np.array([0.0, 2.0, -1.0, 3.0, 1.0, 1.5, 0.5, 4.0, 1.0, -2.0])
        f = walk_forward(losses, lambda: ConstantVaR(1.0), window=3, alphas=[0.99, 0.95])
        self.assertIsInstance(f, BacktestFrame)
        np.testing.assert_array_equal(f.t, np.arange(3, 10))
        np.testing.assert_array_equal(f.loss, losses[3:])
        expected = losses[3:] > 1.0
        for a in (0.95, 0.99):
            np.testing.assert_array_equal(f.exceedance[a], expected)
            self.assertEqual(f.n_exceed(a), 3)  # 3.0, 1.5, 4.0
            np.testing.assert_array_equal(f.var[a], np.ones(7))
        self.assertIsNone(f.es)
        self.assertEqual(f.alphas, (0.95, 0.99))
        self.assertEqual(f.meta["model"], "constant")
        # equality is not an exceedance (strict loss > var)
        g = walk_forward(np.ones(5), lambda: ConstantVaR(1.0), window=1, alphas=[0.99])
        self.assertEqual(g.n_exceed(0.99), 0)

    def test_frame_read_only(self):
        f = walk_forward(np.arange(6.0), lambda: ConstantVaR(2.0, with_es=True), window=2, alphas=[0.975])
        for arr in (f.t, f.loss, f.var[0.975], f.es[0.975], f.exceedance[0.975]):
            self.assertFalse(arr.flags.writeable)
            with self.assertRaises(ValueError):
                arr[0] = 0

    def test_es_collected(self):
        f = walk_forward(np.arange(6.0), lambda: ConstantVaR(2.0, with_es=True), window=2, alphas=[0.975])
        np.testing.assert_array_equal(f.es[0.975], np.full(4, 3.0))

    def test_no_look_ahead(self):
        n, window = 60, 10
        losses = np.arange(n, dtype=np.float64)  # value == index
        original = losses.copy()
        Spy.log, Spy.mutated = [], False
        f = walk_forward(losses, Spy, window=window, alphas=[0.99])
        self.assertEqual(len(Spy.log), n - window)
        for i, seen in enumerate(Spy.log):
            t = window + i
            np.testing.assert_array_equal(seen, np.arange(t - window, t, dtype=np.float64))
            self.assertLess(seen.max(), t)
        # the forecast for day t only knew indices < t
        self.assertTrue(np.all(f.var[0.99] < f.t))
        self.assertFalse(Spy.mutated)
        np.testing.assert_array_equal(losses, original)

    def test_refit_every(self):
        Spy.log = []
        f = walk_forward(np.arange(30, dtype=np.float64), Spy, window=5, alphas=[0.99], refit_every=7)
        self.assertEqual(len(Spy.log), 4)  # days 5, 12, 19, 26
        self.assertEqual([s.max() for s in Spy.log], [4.0, 11.0, 18.0, 25.0])
        self.assertTrue(np.all(f.var[0.99] < f.t))

    def test_validation(self):
        good = lambda: ConstantVaR(1.0)
        bad = [
            dict(losses=np.ones(5), window=5, alphas=[0.99]),
            dict(losses=np.ones(5), window=0, alphas=[0.99]),
            dict(losses=np.ones(5), window=2, alphas=[1.0]),
            dict(losses=np.ones(5), window=2, alphas=[]),
            dict(losses=np.ones(5), window=2, alphas=[0.99], refit_every=0),
            dict(losses=np.array([1.0, np.nan, 1.0]), window=1, alphas=[0.99]),
            dict(losses=np.ones((3, 2)), window=1, alphas=[0.99]),
        ]
        for i, kw in enumerate(bad):
            with self.subTest(i=i), self.assertRaises(ValueError):
                walk_forward(model_factory=good, **kw)

    def test_bad_forecasts(self):
        class Missing(ConstantVaR):
            def forecast(self, alphas):
                return Forecast(var={})

        class NotFinite(ConstantVaR):
            def forecast(self, alphas):
                return Forecast(var={a: float("nan") for a in alphas})

        class Flip(ConstantVaR):
            calls = 0

            def forecast(self, alphas):
                Flip.calls += 1
                return Forecast(var={a: 1.0 for a in alphas},
                                es={a: 2.0 for a in alphas} if Flip.calls > 1 else None)

        for cls in (Missing, NotFinite, Flip):
            with self.subTest(cls.__name__), self.assertRaises(ValueError):
                walk_forward(np.ones(5), lambda: cls(1.0), window=1, alphas=[0.99])

    def test_stateful_update_sees_only_yesterday(self):
        log = []

        class StatefulSpy:
            name = "stateful_spy"
            refit_every = 7

            def fit(self, losses_past):
                log.append(("fit", np.array(losses_past)))
                self.last = float(np.max(losses_past))

            def update(self, loss):
                log.append(("update", loss))
                self.last = max(self.last, loss)

            def forecast(self, alphas):
                return Forecast(var={a: self.last for a in alphas})

        n, window = 40, 10
        losses = np.arange(n, dtype=np.float64)  # value == index
        f = walk_forward(losses, StatefulSpy, window, [0.99])  # refit_every from the model: 7
        self.assertEqual(f.meta["refit_every"], 7)
        self.assertEqual(len(log), n - window)
        for i, ((kind, payload), t) in enumerate(zip(log, range(window, n))):
            if i % 7 == 0:  # refit day: a copy of the window strictly before t
                self.assertEqual(kind, "fit")
                np.testing.assert_array_equal(payload, np.arange(t - window, t, dtype=np.float64))
            else:  # in between: exactly one value, L_{t-1}
                self.assertEqual(kind, "update")
                self.assertEqual(payload, float(t - 1))
        fits = [i for i, (k, _) in enumerate(log) if k == "fit"]
        self.assertEqual(len(fits), 5)  # days 10, 17, 24, 31, 38
        self.assertEqual(sum(1 for k, _ in log if k == "update"), (n - window) - 5)
        self.assertTrue(np.all(f.var[0.99] < f.t))  # the forecast for day t only knew indices < t

    def test_no_extra_factory_call(self):
        calls = []

        class Counted(ConstantVaR):
            refit_every = 4

            def __init__(self):
                calls.append(1)
                super().__init__(1.0)

        f = walk_forward(np.ones(20), Counted, 4, [0.99])  # 16 forecast days, refit every 4 -> 4 models
        self.assertEqual(f.meta["refit_every"], 4)
        self.assertEqual(len(calls), 4)

    def test_explicit_refit_overrides_model_attribute(self):
        class M(ConstantVaR):
            refit_every = 5

        f = walk_forward(np.ones(20), lambda: M(2.0), 3, [0.99], refit_every=2)
        self.assertEqual(f.meta["refit_every"], 2)
        g = walk_forward(np.ones(20), lambda: ConstantVaR(2.0), 3, [0.99])
        self.assertEqual(g.meta["refit_every"], 1)
        self.assertIsNone(g.dists)

    def test_protocol(self):
        self.assertIsInstance(ConstantVaR(1.0), RiskModel)


if __name__ == "__main__":
    unittest.main()
