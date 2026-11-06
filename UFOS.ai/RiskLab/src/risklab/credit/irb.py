"""Basel IRB capital for corporate exposures and the one-factor (Vasicek / ASRF) loss distribution.

Risk-weight function for corporate exposures not in default (BCBS 2006, Basel II, para 272;
Basel Framework CRE31.4-31.5; CRR Art. 153(1)), PD and LGD as decimals::

    R = 0.12 w + 0.24 (1 - w),   w = (1 - e^{-50 PD}) / (1 - e^{-50})
    b = (0.11852 - 0.05478 ln PD)^2
    K = [LGD N((G(PD) + sqrt(R) G(0.999)) / sqrt(1 - R)) - PD LGD] (1 + (M - 2.5) b) / (1 - 1.5 b)
    RWA = 12.5 K EAD (x ``scaling``)

N / G are the standard normal CDF / quantile. Sources and status (see :data:`IRB_SOURCES`):

* the formula: the implementation reproduces the illustrative risk weights of Basel II Annex 5
  and of the Basel Framework CRE99 tables (14.44 / 29.65 / 92.32 % at PD 0.03 / 0.10 / 1.00 %,
  LGD 45 %, M = 2.5 years); the v0.8 review confirmed that they match those tables exactly;
* ``scaling``: 1.0 under the Basel framework (CRE31); the 1.06 factor is CRR-only (Art. 153(1));
  a parameter, default 1.0;
* the PD floor: 0.05 % (CRE32.4, confirmed in the v0.8 review); a parameter (default 0.05 %);
* effective maturity M floored at 1 and capped at 5 years (CRE32.46, confirmed in the v0.8 review;
  CRR Art. 162); parameters.
The SME firm-size adjustment (CRE31.8) and defaulted exposures are not modelled.

**ASRF check:** with M = 1 the maturity adjustment is 1 and ``K / LGD + PD`` is the Vasicek
99.9 % quantile of the default rate of an infinitely granular portfolio,
``Phi((G(PD) + sqrt(R) G(q)) / sqrt(1 - R))``. :func:`simulate_losses` draws the loss of a
portfolio of homogeneous buckets in the same one-factor model (asset value of obligor i:
``sqrt(R) Z + sqrt(1 - R) e_i``, default when below ``G(PD)``; given Z the number of defaults
in a bucket is binomial), so the simulated 99.9 % quantile minus the expected loss can be
compared with the sum of K: equal for a large granular portfolio, larger for a concentrated
one (name concentration, the granularity gap).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

import numpy as np
from scipy import stats

from risklab.conventions import SeedLike, make_rng

Q = 0.999
PD_FLOOR = 0.0005
M_MIN, M_MAX = 1.0, 5.0
IRB_SOURCES = {
    "formula": "BCBS (2006) Basel II para 272 / Basel Framework CRE31.4-31.5 / CRR Art. 153(1); reproduces the "
               "illustrative risk weights of Basel II Annex 5 and the CRE99 tables (14.44 / 29.65 / 92.32 % at PD "
               "0.03 / 0.10 / 1 %, LGD 45 %, M 2.5) exactly - confirmed in the v0.8 review",
    "scaling": "1.0 (Basel framework, CRE31); the 1.06 factor is CRR-only (CRR Art. 153(1)); parameter, default 1.0",
    "pd_floor": "0.05 % (Basel Framework CRE32.4; confirmed in the v0.8 review); parameter",
    "maturity": "M floored at 1 and capped at 5 years (Basel Framework CRE32.46, confirmed in the v0.8 review; "
                "CRR Art. 162); parameters",
    "not modelled": "SME firm-size adjustment (CRE31.8), defaulted exposures, double default, credit risk mitigation",
}


def correlation(pd) -> np.ndarray:
    pd = np.asarray(pd, dtype=np.float64)
    w = -np.expm1(-50.0 * pd) / -math.expm1(-50.0)
    return 0.12 * w + 0.24 * (1.0 - w)


def maturity_b(pd) -> np.ndarray:
    return (0.11852 - 0.05478 * np.log(np.asarray(pd, dtype=np.float64))) ** 2


def vasicek_quantile(pd, rho, q: float = Q) -> np.ndarray:
    """q-quantile of the default rate of an infinitely granular one-factor portfolio."""
    return stats.norm.cdf((stats.norm.ppf(pd) + np.sqrt(rho) * stats.norm.ppf(q)) / np.sqrt(1.0 - np.asarray(rho)))


def capital_k(pd, lgd, m=2.5, pd_floor: float = PD_FLOOR, m_min: float = M_MIN, m_max: float = M_MAX,
              q: float = Q) -> np.ndarray:
    """Capital requirement K per unit of EAD (module docstring); PD floored, M clipped to [m_min, m_max]."""
    pd = np.maximum(np.asarray(pd, dtype=np.float64), pd_floor)
    if np.any(pd >= 1.0):
        raise ValueError("PD must be < 1 (defaulted exposures are not modelled)")
    lgd = np.asarray(lgd, dtype=np.float64)
    m = np.clip(np.asarray(m, dtype=np.float64), m_min, m_max)
    R = correlation(pd)
    b = maturity_b(pd)
    return (lgd * vasicek_quantile(pd, R, q) - pd * lgd) * (1.0 + (m - 2.5) * b) / (1.0 - 1.5 * b)


def risk_weight(pd, lgd, m=2.5, scaling: float = 1.0, **kw) -> np.ndarray:
    """RW = 12.5 K (x scaling); RWA = RW x EAD."""
    return 12.5 * scaling * capital_k(pd, lgd, m, **kw)


@dataclass(frozen=True)
class Bucket:
    """``n`` identical obligors with exposure ``ead`` each."""
    n: int
    ead: float
    lgd: float
    pd: float


def portfolio_k(buckets: Sequence[Bucket], m: float = 1.0, **kw) -> float:
    """Sum of K x EAD over the portfolio (capital in currency units)."""
    return float(sum(b.n * b.ead * float(capital_k(b.pd, b.lgd, m, **kw)) for b in buckets))


def expected_loss(buckets: Sequence[Bucket]) -> float:
    return float(sum(b.n * b.ead * b.lgd * b.pd for b in buckets))


def simulate_losses(buckets: Sequence[Bucket], n_sims: int, seed: SeedLike, rho=None, chunk: int = 200_000) -> np.ndarray:
    """Portfolio losses in the one-factor model; ``rho=None`` uses the IRB correlation of each bucket's PD."""
    rng = make_rng(seed)
    out = np.empty(n_sims)
    rhos = [float(correlation(b.pd)) if rho is None else float(rho) for b in buckets]
    for s in range(0, n_sims, chunk):
        k = min(chunk, n_sims - s)
        z = rng.standard_normal(k)
        loss = np.zeros(k)
        for b, r in zip(buckets, rhos):
            p = stats.norm.cdf((stats.norm.ppf(b.pd) - math.sqrt(r) * z) / math.sqrt(1.0 - r))
            loss += rng.binomial(b.n, p) * b.ead * b.lgd
        out[s:s + k] = loss
    return out


def quantile_ci(x: np.ndarray, q: float = Q, level: float = 0.95) -> Tuple[float, float, float]:
    """Empirical q-quantile with a distribution-free order-statistic confidence interval."""
    xs = np.sort(np.asarray(x))
    n = xs.size
    a = (1 - level) / 2
    lo = int(stats.binom.ppf(a, n, q))
    hi = int(stats.binom.ppf(1 - a, n, q))
    return float(np.quantile(xs, q)), float(xs[max(lo - 1, 0)]), float(xs[min(hi, n - 1)])
