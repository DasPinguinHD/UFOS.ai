"""Basel traffic-light approach for VaR backtesting.

Source: Basel Committee on Banking Supervision (1996), "Supervisory framework
for the use of 'backtesting' in conjunction with the internal models approach
to market risk capital requirements", January 1996, Table 2 (250 observations,
99 % VaR): green zone 0-4 exceptions (plus factor 0.00), yellow zone 5-9
(plus factors 0.40, 0.50, 0.65, 0.75, 0.85), red zone 10 or more (1.00).

The zones are defined there by the binomial cumulative probability under
H0 (exception probability ``1 - alpha``): green while ``P(X <= x) < 95 %``,
red once ``P(X <= x) >= 99.99 %``, yellow in between. This module computes
the zones from that rule for any window and level; the plus factor exists
only for the regulatory case (250 days, alpha = 0.99).
"""
from __future__ import annotations

import numpy as np
from scipy import stats

from risklab.backtest.base import TestResult, as_exceedances, check_level

GREEN_BELOW = 0.95
RED_FROM = 0.9999
#: Plus factor (multiplier add-on) by number of exceptions, BCBS 1996 Table 2 (250 days, 99 %).
PLUS_FACTOR_250_99 = {5: 0.40, 6: 0.50, 7: 0.65, 8: 0.75, 9: 0.85}


def zone_for(n_exceed: int, window: int, alpha: float) -> str:
    """"green" / "yellow" / "red" for x exceptions in ``window`` days at level alpha."""
    c = float(stats.binom.cdf(n_exceed, window, 1.0 - alpha))
    if c < GREEN_BELOW:
        return "green"
    if c >= RED_FROM:
        return "red"
    return "yellow"


def zone_bounds(window: int, alpha: float) -> dict:
    """Smallest exception counts of the yellow and red zones."""
    xs = np.arange(window + 1)
    c = stats.binom.cdf(xs, window, 1.0 - alpha)
    yellow = int(xs[c >= GREEN_BELOW][0]) if np.any(c >= GREEN_BELOW) else None
    red = int(xs[c >= RED_FROM][0]) if np.any(c >= RED_FROM) else None
    return {"yellow_from": yellow, "red_from": red}


def plus_factor_250_99(n_exceed: int) -> float:
    if n_exceed <= 4:
        return 0.0
    if n_exceed >= 10:
        return 1.0
    return PLUS_FACTOR_250_99[n_exceed]


def traffic_light(exceedances, alpha: float = 0.99, window: int = 250) -> TestResult:
    """Basel traffic-light zone for the exceptions of the most recent ``window`` days.

    Args:
        exceedances: 1-D bool (or 0/1) array in time order, at least ``window``
            days; only the last ``window`` days count (the regulatory look-back).
        alpha: VaR confidence level in (0, 1); 0.99 in the regulatory setting.
        window: look-back in days; 250 in the regulatory setting.
    Returns:
        TestResult(name="traffic_light", statistic=None, df=None, p_value=None,
        reject_at=None, decision = "green" / "yellow" / "red", n_obs=window,
        n_exceed=x, details={"cumulative_probability": P(X <= x),
        "plus_factor": add-on or None, "yellow_from", "red_from"}).
        The plus factor is given only for window 250 and alpha 0.99; otherwise
        it is None and ``notes`` says why.
    """
    a = check_level("alpha", alpha)
    if isinstance(window, bool) or not isinstance(window, (int, np.integer)) or window < 1:
        raise ValueError("window must be an int >= 1")
    window = int(window)
    e = as_exceedances(exceedances, 1)
    if e.shape[0] < window:
        raise ValueError(f"traffic_light needs at least window={window} days, got {e.shape[0]}")
    last = e[-window:]
    x = int(np.count_nonzero(last))
    zone = zone_for(x, window, a)
    regulatory = window == 250 and abs(a - 0.99) < 1e-12
    notes = "" if regulatory else "plus factor defined only for 250 days at 99 % (BCBS 1996 Table 2)"
    details = {"cumulative_probability": float(stats.binom.cdf(x, window, 1.0 - a)),
               "plus_factor": plus_factor_250_99(x) if regulatory else None,
               **zone_bounds(window, a)}
    return TestResult("traffic_light", None, None, None, None, zone, window, x, notes, details)
