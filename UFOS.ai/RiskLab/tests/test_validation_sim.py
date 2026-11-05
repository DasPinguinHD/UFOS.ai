"""Simulator: the planted Sharpe is recovered, selection over K raises the in-sample Sharpe as the
expected maximum of K normals predicts, costs and trade counts, nested searches."""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: tests run without installing the package
    sys.path.insert(0, _SRC)

import math
import unittest

import numpy as np
from scipy import integrate, stats as st

from risklab.validation import sim


def expected_max_normals(k: int) -> float:
    """E[max of k iid N(0, 1)] by numerical integration."""
    f = lambda x: x * k * st.norm.pdf(x) * st.norm.cdf(x) ** (k - 1)
    return integrate.quad(f, -12, 12)[0]


class SimulatorTests(unittest.TestCase):
    def test_planted_sharpe_recovered(self):
        years = 200
        n = sim.BURN_IN + 252 * years
        for data in sim.DATA_TYPES:
            with self.subTest(data=data):
                r = sim.simulate_returns(1, n, data, 0.75, 4)[0]
                R, _ = sim.strategy_returns(sim.positions(r), r, 0.0)
                s = sim.annual_sharpe_rows(R)[sim.CONFIG_INDEX[sim.TRUE_CONFIG]]
                self.assertLess(abs(s - 0.75), 3 * math.sqrt(1.0 / years) * 1.2, s)

    def test_noise_has_no_edge(self):
        r = sim.simulate_returns(1, sim.BURN_IN + 252 * 200, "iid_t", 0.0, 9)[0]
        R, _ = sim.strategy_returns(sim.positions(r), r, 0.0)
        s = sim.annual_sharpe_rows(R)
        self.assertLess(abs(float(np.mean(s))), 3 * math.sqrt(1 / 200.0))

    def test_volatility_and_tails(self):
        r = sim.simulate_returns(4, 20000, "garch_t", 0.0, 2)
        self.assertAlmostEqual(float(r.std() * math.sqrt(252)), sim.SIGMA_ANNUAL, delta=0.01)
        self.assertGreater(float(st.kurtosis(r.ravel())), 1.0)

    def test_selection_matches_expected_max_of_k_normals(self):
        """Best of K independent noise strategies: the mean selected annualised Sharpe is about
        E[max of K N(0,1)] x sqrt(1 / years) (Sharpe se of an iid series with true Sharpe 0)."""
        rng = np.random.default_rng(21)
        years, T, reps = 4, 1008, 300
        for k in (1, 8, 64):
            best = []
            for _ in range(reps):
                R = rng.normal(0, 0.01, (k, T))
                s = sim.annual_sharpe_rows(R)
                best.append(s[sim.select_best(s, range(k))])
            want = expected_max_normals(k) * math.sqrt(1.0 / years)
            se = np.std(best, ddof=1) / math.sqrt(reps)
            self.assertLess(abs(np.mean(best) - want), 4 * se + 0.01, (k, np.mean(best), want))

    def test_select_best_ties_and_nan(self):
        self.assertEqual(sim.select_best(np.array([0.1, 0.5, 0.5, np.nan]), [3, 2, 1]), 2)
        self.assertEqual(sim.select_best(np.array([np.nan, -1.0]), [0, 1]), 1)

    def test_costs_and_trades(self):
        r = sim.simulate_returns(1, sim.BURN_IN + 1000, "iid_t", 0.0, 3)[0]
        pos = sim.positions(r)
        R0, ch = sim.strategy_returns(pos, r, 0.0)
        R5, _ = sim.strategy_returns(pos, r, 5.0)
        turn = np.abs(np.diff(pos.astype(float), axis=1))[:, sim.BURN_IN - 2:-1]
        np.testing.assert_allclose(R0 - R5, 5e-4 * turn)
        np.testing.assert_array_equal(ch, turn > 0)
        self.assertTrue(np.all(np.isin(pos[:, sim.BURN_IN - 1:], (-1, 0, 1))))

    def test_evaluate_path_nested_search(self):
        train = 252 * 5
        r = sim.simulate_returns(1, sim.BURN_IN + train + sim.HOLDOUT_DAYS, "iid_t", 0.5, 8)[0]
        ev = sim.evaluate_path(r, train, (1, 8, 64, 256), 2.0, np.random.default_rng(1))
        s = [ev.train_sharpe[k] for k in (1, 8, 64, 256)]
        self.assertTrue(all(a <= b for a, b in zip(s, s[1:])))
        for k in (1, 8, 64, 256):
            self.assertEqual(len(ev.fold_sharpes[k]), 5)
            self.assertEqual(ev.pbo_configs[k], 64)
            self.assertIsNotNone(ev.dsr[k])
        with self.assertRaises(ValueError):
            sim.evaluate_path(r[:-1], train, (1,), 2.0, np.random.default_rng(1))


    def test_g4_wiring_like_the_platform_job(self):
        """G4 as jobs.run_validation feeds it (HedgeFund strategy_platform/validation/jobs.py, the
        n_trials / trial_sharpes lines): n_trials = trials before + 1, and the trial Sharpes are the
        family's trial Sharpes plus the validated run's own training Sharpe. Here the trials before
        are the K tried configurations, so N = K + 1 and the selection's Sharpe appears twice."""
        from risklab.validation.stats import deflated_sharpe

        train = 252 * 5
        r = sim.simulate_returns(1, sim.BURN_IN + train + sim.HOLDOUT_DAYS, "iid_t", 0.5, 31)[0]
        ks = (1, 8, 64)
        ev = sim.evaluate_path(r, train, ks, 2.0, np.random.default_rng(5))
        R, _ = sim.strategy_returns(sim.positions(r), r, 2.0)
        Rtr = R[:, :train]
        s_tr = sim.annual_sharpe_rows(Rtr)
        perm = np.random.default_rng(5).permutation(len(sim.CONFIGS))
        for k in ks:
            tried = perm[:k]
            best = sim.select_best(s_tr, tried)
            self.assertEqual(sim.CONFIGS[best], ev.selected[k])
            trials = [float(v) for v in s_tr[tried]] + [float(s_tr[best])]
            want = deflated_sharpe(Rtr[best], sim.PERIODS, k + 1, trials)
            self.assertEqual(want["n_trials"], k + 1)
            self.assertAlmostEqual(ev.dsr[k], want["value"], places=12)
            # the off-by-one wiring N = K gives a different value
            if k > 1:
                self.assertNotAlmostEqual(ev.dsr[k], deflated_sharpe(Rtr[best], sim.PERIODS, k, trials)["value"], places=6)


if __name__ == "__main__":
    unittest.main()
