"""Backtest interface: the result record and the sheet-1 test stubs.

Inputs of every test: ``exceedances`` is a 1-D bool array (True = the day's
loss exceeded that day's VaR, ``loss > var``), as produced by
:func:`risklab.engine.walk_forward` for one alpha; ``alpha`` is the VaR
confidence level in (0, 1) (expected exceedance rate ``1 - alpha``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from risklab.conventions import not_derived_yet


@dataclass(frozen=True)
class TestResult:
    """Outcome of one backtest.

    Attributes:
        name: test name, e.g. "kupiec_pof".
        statistic: test statistic (None for a zone-based test such as the traffic light).
        df: degrees of freedom of the reference distribution, or None.
        p_value: p-value in [0, 1], or None.
        reject_at: significance level the decision used, e.g. 0.05 (None for zones).
        decision: "reject" / "accept", or a zone "green" / "yellow" / "red".
        n_obs: number of backtest days.
        n_exceed: number of exceedances.
        notes: free text (assumptions, warnings).
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


def kupiec_pof(exceedances: np.ndarray, alpha: float, reject_at: float = 0.05) -> TestResult:
    """Kupiec proportion-of-failures test (unconditional coverage). Sheet 1.

    Null hypothesis: the exceedance rate equals ``1 - alpha``.

    Args:
        exceedances: 1-D bool array, one entry per backtest day in time order
            (True = ``loss > var`` that day), e.g. ``BacktestFrame.exceedance[alpha]``.
        alpha: VaR confidence level in (0, 1); the expected exceedance rate is ``1 - alpha``.
        reject_at: significance level for the decision, in (0, 1), e.g. 0.05.
    Returns:
        :class:`TestResult` with name ``"kupiec_pof"``, the test statistic
        (dimensionless), its degrees of freedom ``df``, the p-value in [0, 1],
        ``reject_at``, ``decision`` = "reject" if p_value < reject_at else
        "accept", ``n_obs`` = len(exceedances) and ``n_exceed`` = their count.
    Raises:
        NotImplementedError: until sheet 1 is derived with the user.
    """
    raise not_derived_yet()


def christoffersen_ind(exceedances: np.ndarray, alpha: float, reject_at: float = 0.05) -> TestResult:
    """Christoffersen independence test (first-order Markov). Sheet 1.

    Null hypothesis: exceedances are independent over time (today's exceedance
    probability does not depend on whether yesterday was an exceedance).

    Args:
        exceedances: 1-D bool array, one entry per backtest day in time order
            (True = ``loss > var`` that day), e.g. ``BacktestFrame.exceedance[alpha]``.
        alpha: VaR confidence level in (0, 1); the expected exceedance rate is ``1 - alpha``.
        reject_at: significance level for the decision, in (0, 1), e.g. 0.05.
    Returns:
        :class:`TestResult` with name ``"christoffersen_ind"``, the test statistic
        (dimensionless), its degrees of freedom ``df``, the p-value in [0, 1],
        ``reject_at``, ``decision`` = "reject" if p_value < reject_at else
        "accept", ``n_obs`` = len(exceedances) and ``n_exceed`` = their count.
    Raises:
        NotImplementedError: until sheet 1 is derived with the user.
    """
    raise not_derived_yet()


def christoffersen_cc(exceedances: np.ndarray, alpha: float, reject_at: float = 0.05) -> TestResult:
    """Christoffersen conditional coverage test (POF + independence). Sheet 1.

    Null hypothesis: correct exceedance rate ``1 - alpha`` AND independence.

    Args:
        exceedances: 1-D bool array, one entry per backtest day in time order
            (True = ``loss > var`` that day), e.g. ``BacktestFrame.exceedance[alpha]``.
        alpha: VaR confidence level in (0, 1); the expected exceedance rate is ``1 - alpha``.
        reject_at: significance level for the decision, in (0, 1), e.g. 0.05.
    Returns:
        :class:`TestResult` with name ``"christoffersen_cc"``, the test statistic
        (dimensionless), its degrees of freedom ``df``, the p-value in [0, 1],
        ``reject_at``, ``decision`` = "reject" if p_value < reject_at else
        "accept", ``n_obs`` = len(exceedances) and ``n_exceed`` = their count.
    Raises:
        NotImplementedError: until sheet 1 is derived with the user.
    """
    raise not_derived_yet()


def traffic_light(exceedances: np.ndarray, alpha: float = 0.99) -> TestResult:
    """Basel traffic-light zone (green / yellow / red) for the exceedance count. Sheet 1.

    Args:
        exceedances: 1-D bool array, one entry per backtest day (True = ``loss > var``);
            the regulatory setting is 250 days of 99 % one-day VaR.
        alpha: VaR confidence level in (0, 1), 0.99 in the regulatory setting.
    Returns:
        :class:`TestResult` with name ``"traffic_light"``, ``statistic``, ``df``,
        ``p_value`` and ``reject_at`` = None, ``decision`` = "green" / "yellow" /
        "red", ``n_obs`` and ``n_exceed``; the zone boundaries used go into ``notes``.
    Raises:
        NotImplementedError: until sheet 1 is derived with the user.
    """
    raise not_derived_yet()
