"""Backtest interface: the result record and shared input checks.

Inputs of every test: ``exceedances`` is a 1-D bool array (True = the day's
loss exceeded that day's VaR, ``loss > var``), as produced by
:func:`risklab.engine.walk_forward` for one alpha; ``alpha`` is the VaR
confidence level in (0, 1) (expected exceedance rate ``1 - alpha``).

Decision wording: "reject" or "not rejected" (never "accepted": not rejecting
H0 is not evidence that the model is right).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from risklab.conventions import check_alpha

REJECT = "reject"
NOT_REJECTED = "not rejected"


@dataclass(frozen=True)
class TestResult:
    """Outcome of one backtest.

    Attributes:
        name: test name, e.g. "kupiec_pof".
        statistic: test statistic (None for a zone-based test such as the traffic light).
        df: degrees of freedom of the reference distribution, or None.
        p_value: p-value in [0, 1], or None.
        reject_at: significance level the decision used, e.g. 0.05 (None for zones).
        decision: "reject" / "not rejected", or a zone "green" / "yellow" / "red".
        n_obs: number of backtest days.
        n_exceed: number of exceedances.
        notes: free text (assumptions, degenerate cases, conventions).
        details: optional extra values (JSON-serialisable dict), e.g. the
            transition counts or the traffic-light plus factor.
    """

    __test__ = False  # not a unittest / pytest test class

    name: str
    statistic: Optional[float]
    df: Optional[int]
    p_value: Optional[float]
    reject_at: Optional[float]
    decision: str
    n_obs: int
    n_exceed: int
    notes: str = ""
    details: Optional[dict] = None


def as_exceedances(exceedances, min_len: int = 1) -> np.ndarray:
    """A 1-D bool copy of ``exceedances`` (bools or 0/1 values)."""
    arr = np.asarray(exceedances)
    if arr.ndim != 1:
        raise ValueError("exceedances must be one-dimensional")
    if arr.shape[0] < min_len:
        raise ValueError(f"at least {min_len} observation(s) are required")
    if arr.dtype != bool:
        if not np.all((arr == 0) | (arr == 1)):
            raise ValueError("exceedances must be bool or 0/1")
        arr = arr.astype(bool)
    return arr.copy()


def check_level(name: str, value) -> float:
    """A probability strictly inside (0, 1) (alpha, reject_at)."""
    try:
        return check_alpha(value)
    except ValueError:
        raise ValueError(f"{name} must lie strictly inside (0, 1), got {value!r}") from None


def decide(p_value: float, reject_at: float) -> str:
    return REJECT if p_value < reject_at else NOT_REJECTED
