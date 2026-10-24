"""SPEC §16.11 acceptance (plan L5): on synthetic data with a PLANTED persistent cross-sectional signal (AR(1)
drift, φ 0.98) and PLANTED noise (open/close ratio with i.i.d. noise, unrelated to future returns), a loop
started from an idea finds the planted signal (a finalist that uses it) and rejects the noise (no noise variant
is a finalist). Real engine + JobManager, fake LLM transport that proposes both kinds of variants.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_loop_acceptance -v
"""
import json
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402

from strategy_platform.core import calendars  # noqa: E402
from strategy_platform.discovery import loop  # noqa: E402
from strategy_platform.storage import db  # noqa: E402
from strategy_platform.validation import families  # noqa: E402
import test_sp_backtest_support as bsup  # noqa: E402
import test_sp_loop_support as ls  # noqa: E402

SYMS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"]
DAYS = calendars.trading_days(date(2021, 1, 4), date(2026, 10, 8))


def planted_prices(seed=7, beta=0.002, phi=0.98, vol=0.012):
    rng = np.random.default_rng(seed)
    T, N = len(DAYS), len(SYMS)
    z = np.zeros((T, N))
    z[0] = rng.normal(size=N)
    sd = np.sqrt(1 - phi * phi)
    for t in range(1, T):
        z[t] = phi * z[t - 1] + sd * rng.normal(size=N)
    mkt = rng.normal(0.0003, 0.009, size=T)
    zc = z - z.mean(axis=1, keepdims=True)
    r = np.zeros((T, N))
    r[1:] = mkt[1:, None] + beta * zc[:-1] + rng.normal(0, vol, size=(T - 1, N))
    close = 50.0 * np.cumprod(1 + r, axis=0)
    u = rng.normal(0, 0.003, size=(T, N))                    # planted noise: open = close × (1 + u)
    out = {s: {d: (float(close[i, k] * (1 + u[i, k])), float(close[i, k])) for i, d in enumerate(DAYS)}
           for k, s in enumerate(SYMS)}
    b = 400 * np.cumprod(1 + mkt)
    out["SPY"] = {d: (float(b[i]), float(b[i])) for i, d in enumerate(DAYS)}
    return out


PRICES = planted_prices()


def spec(name, signal, params=None):
    return {"schema": "ufos.strategy/1", "name": name, "description": "synthetic acceptance test",
            "universe": {"symbols": list(SYMS)}, "parameters": params or {}, "signal": signal,
            "portfolio": {"mode": "long_short", "selection": {"type": "top_bottom_n", "n": 2}, "weighting": "equal",
                          "max_weight": 1.0, "gross_exposure": 1.0},
            "rebalance": {"frequency": "weekly"}}


def momentum(lookback, name):
    return spec(name, {"op": "cs_rank", "args": [{"op": "pct_change", "args": [{"field": "close"},
                                                                                {"param": "lookback"}]}]},
                {"lookback": {"value": lookback, "min": 10, "max": 60, "step": 10, "integer": True}})


OC = {"op": "div", "args": [{"field": "open"}, {"field": "close"}]}
NOISE_A = spec("open/close noise", {"op": "cs_rank", "args": [OC]})
NOISE_B = spec("negated open/close noise", {"op": "cs_rank", "args": [{"op": "neg", "args": [OC]}]})
NOISE_C = spec("shifted open/close noise", {"op": "cs_rank", "args": [{"op": "sub", "args": [OC, 1]}]})
PLANTED = momentum(20, "momentum 20")
PLANTED_CHILD = momentum(40, "momentum 40")
REFINES = [PLANTED, NOISE_B, NOISE_C, PLANTED_CHILD]


def uses_planted(s) -> bool:
    return "pct_change" in json.dumps(s.get("signal"))


class AcceptanceTest(ls.LoopCase):
    def setUp(self):
        super().setUp()
        self.mgr._data_factory = lambda: bsup.FakeLoader(PRICES)

    def test_planted_signal_found_and_planted_noise_rejected(self):
        f = self.fake(refine=lambda m, n: REFINES[n - 1], seed=ls.draft(NOISE_A, reply="A first idea."))
        lid = self.start(seed={"kind": "idea", "text": "Trade the open/close gap across eight stocks."},
                         variants=3, generations=2, search_points=0)
        out = self.wait_loop(lid)
        self.assertEqual(out["status"], "done", out["status_reason"])
        self.assertEqual(len(f.calls["seed"]), 1)
        root = db.strategy_get(out["root_strategy_id"])
        self.assertEqual(root["origin"], "loop")
        fam = root["family_id"]
        variants = [v for g in out["generations"] for v in g["variants"] if not v.get("carried_from")]
        specs = {v["id"]: json.loads(db.strategy_version_get(v["strategy_id"], v["chosen_version"])["spec_json"])
                 for v in variants if v["strategy_id"] and v["chosen_version"]}
        fins = out["finalists"]
        self.assertTrue(fins, out["finalists_note"])
        for fz in fins:
            s = json.loads(db.strategy_version_get(fz["strategy_id"], fz["version"])["spec_json"])
            self.assertTrue(uses_planted(s), s["signal"])             # the planted signal is found …
            self.assertGreater(fz["score"], 0)
        noise_ids = [vid for vid, s in specs.items() if not uses_planted(s)]
        self.assertGreaterEqual(len(noise_ids), 3)
        fin_ids = {fz["variant_id"] for fz in fins}
        by_id = {v["id"]: v for v in variants}
        for vid in noise_ids:                                         # … and every noise variant is rejected
            self.assertNotIn(vid, fin_ids)
            sc = by_id[vid]["score"] or {}
            self.assertTrue(sc.get("score") is None or sc["score"] <= 0 or not sc["checks_passed"], sc)
        planted = [v for v in variants if v["id"] in specs and uses_planted(specs[v["id"]])]
        self.assertTrue(all(v["score"]["checks_passed"] for v in planted))
        self.assertGreater(min(v["score"]["score"] for v in planted),
                           max((by_id[i]["score"] or {}).get("score") or -99 for i in noise_ids))
        # every backtest is a trial of the root's family
        self.assertEqual(families.trial_count(fam), out["counts"]["backtests_started"])
        self.assertEqual(out["counts"]["backtests_started"], 5)
        self.assertEqual(out["finalists_note"], None)


    def test_noise_only_ends_without_a_finalist(self):
        noises = [NOISE_B, NOISE_C, spec("noise D", {"op": "cs_rank", "args": [{"op": "abs", "args": [OC]}]})]
        self.fake(refine=lambda m, n: noises[(n - 1) % len(noises)], seed=ls.draft(NOISE_A, reply="A noise idea."))
        lid = self.start(seed={"kind": "idea", "text": "Trade the open/close gap across eight stocks."},
                         variants=3, generations=1, search_points=0)
        out = self.wait_loop(lid)
        self.assertEqual(out["status"], "done", out["status_reason"])
        self.assertEqual(out["finalists"], [])
        self.assertEqual(out["finalists_note"], loop.NO_FINALIST_MESSAGE)
        self.assertEqual(out["counts"]["backtests_started"], 3)

if __name__ == "__main__":
    unittest.main()
