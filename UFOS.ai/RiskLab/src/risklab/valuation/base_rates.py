"""Base rates for an implied growth: how often do firms achieve it? (synthetic universe, v0.4 link)

The growth universe is the one of the v0.4 survivorship study (``studies/survivorship.py``, its
module docstring has the derivation): N firms, 10 years, log growth ``g_it = mu_i + sigma eps_it``
with ``mu_i ~ N(drift_mean, drift_sd)``; a surviving firm exits with probability
``min(p_max, h0 exp(-kappa z_it))`` where ``z_it`` is the standardised growth of that year (poor
growth -> higher exit hazard). :func:`simulate_cohort` reproduces that study's
``simulate_cohort`` draw for draw (same random-number order; a parity test guards it), so the
numbers here and the v0.4 decomposition describe the same synthetic world. Synthetic only: no real
firms or data.

For a growth hurdle (e.g. the 10-year CAGR implied by a price) three base rates answer three
questions (the v0.4 decomposition):

* ``survivors_only`` - the share of TODAY's surviving firms whose 10-year CAGR reached the hurdle
  (what a database of current constituents shows; biased upwards);
* ``full_cohort`` - the share of ALL firms at the start that reached it as a going concern
  (exited firms count as misses: the forecasting base rate for a firm picked today);
* ``counterfactual`` - the share of all firms whose complete growth path reached it, had none
  exited (the selection-free rate).

The universe is in revenue growth; comparing a FREE-CASH-FLOW CAGR with it assumes a constant FCF
margin (FCF grows with revenue) - an explicit modelling assumption of the comparison.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

import numpy as np

from ..conventions import SeedLike, make_rng

RATE_NAMES = ("survivors_only", "full_cohort", "counterfactual")


@dataclass(frozen=True)
class GrowthUniverse:
    """Defaults = the v0.4 survivorship study (``SurvivorConfig``) with kappa = 1."""
    n_firms: int = 2000
    years: int = 10
    drift_mean: float = 0.05
    drift_sd: float = 0.05
    sigma: float = 0.15
    h0: float = 0.04
    p_max: float = 0.9
    kappa: float = 1.0


def simulate_cohort(u: GrowthUniverse, rng: np.random.Generator) -> Tuple[np.ndarray, np.ndarray]:
    """(alive after the last year, counterfactual path CAGR of every firm) - the draw order of the v0.4 study."""
    n, t = u.n_firms, u.years
    mu = rng.normal(u.drift_mean, u.drift_sd, size=n)
    g = mu[:, None] + u.sigma * rng.standard_normal((n, t))
    uu = rng.random((n, t))
    z = (g - u.drift_mean) / math.sqrt(u.drift_sd ** 2 + u.sigma ** 2)
    p = np.minimum(u.p_max, u.h0 * np.exp(-u.kappa * z))
    alive = ~np.any(uu < p, axis=1)
    return alive, np.exp(g.mean(axis=1)) - 1.0


def rates(alive: np.ndarray, path_cagr: np.ndarray, hurdle: float) -> Dict[str, float]:
    """The three base rates of one cohort for a CAGR hurdle (``>=``)."""
    hit = path_cagr >= hurdle
    n_alive = int(np.count_nonzero(alive))
    hits_alive = int(np.count_nonzero(alive & hit))
    return {"survivors_only": hits_alive / n_alive if n_alive else float("nan"),
            "full_cohort": hits_alive / alive.size,
            "counterfactual": float(np.count_nonzero(hit)) / alive.size}


@dataclass
class Cohorts:
    """M simulated cohorts of one universe, kept so that any hurdle can be evaluated on the same draws."""
    universe: GrowthUniverse
    alive: List[np.ndarray]
    path_cagr: List[np.ndarray]

    @property
    def m(self) -> int:
        return len(self.alive)

    def base_rates(self, hurdle: float) -> Dict[str, float]:
        """Mean over the cohorts and its Monte-Carlo se (``<name>_se``) for every rate; plus the survival rate."""
        per = [rates(a, c, hurdle) for a, c in zip(self.alive, self.path_cagr)]
        out: Dict[str, float] = {"hurdle": float(hurdle), "m": self.m,
                                 "survival_rate": float(np.mean([a.mean() for a in self.alive]))}
        for k in RATE_NAMES:
            x = np.asarray([p[k] for p in per])
            out[k] = float(x.mean())
            out[k + "_se"] = float(x.std(ddof=1) / math.sqrt(x.size)) if x.size > 1 else float("nan")
        return out


def simulate_cohorts(u: GrowthUniverse, m: int, seed: SeedLike) -> Cohorts:
    if m < 1:
        raise ValueError("m must be >= 1")
    rng = make_rng(seed)
    alive, cagr = [], []
    for _ in range(m):
        a, c = simulate_cohort(u, rng)
        alive.append(a)
        cagr.append(c)
    return Cohorts(u, alive, cagr)


def base_rate_curve(cohorts: Cohorts, hurdles: Sequence[float]) -> List[Dict[str, float]]:
    return [cohorts.base_rates(h) for h in hurdles]
