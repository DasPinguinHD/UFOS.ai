"""Walk-forward folds of one training return series (UFOS SPEC §9.3 step 2, time-series form).

The rule is fixed (no refit): the training days are cut into K = 5 consecutive segments of
equal length (sizes differ by at most one day, as ``numpy.array_split``) and each fold's
annualised Sharpe is computed from its own daily returns. Return j belongs to the fold of
day j (a strategy return series indexed by day, as in the platform).
"""
from __future__ import annotations

from typing import List, Optional, Tuple

import numpy as np

from .stats import sharpe

K_FOLDS = 5


def fold_bounds(n: int, k: int = K_FOLDS) -> List[Tuple[int, int]]:
    """Inclusive (first, last) indices of k consecutive near-equal segments of n days."""
    if n < k:
        raise ValueError(f"{n} day(s) cannot be split into {k} folds")
    base, extra = divmod(n, k)
    out, a = [], 0
    for i in range(k):
        size = base + (1 if i < extra else 0)   # the first n mod k folds get one extra day
        out.append((a, a + size - 1))
        a += size
    return out


def fold_sharpes(returns, periods_per_year: int, k: int = K_FOLDS) -> List[Optional[float]]:
    r = np.asarray(returns, dtype=np.float64)
    return [sharpe(r[a:b + 1], periods_per_year) for a, b in fold_bounds(r.size, k)]
