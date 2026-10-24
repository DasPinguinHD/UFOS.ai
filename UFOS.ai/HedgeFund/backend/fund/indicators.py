"""Pure indicator helpers for the HedgeFund agents and the backtest
(fund/SPEC.md §5). Stdlib only, no I/O, deterministic.

Conventions:
- ``values`` are plain floats, oldest first; ``bars`` are ``fund.types.Bar``
  (or anything with ``high``/``low``/``close`` attributes), oldest first.
- Functions that return "the latest value" return ``None`` when there is not
  enough data instead of raising.
- Percent-like results are FRACTIONS (0.015 = 1.5 %) unless the name says
  ``_pct``-in-percent explicitly (none here does): ``atr_pct`` returns the ATR
  as a fraction of the last close, ``max_drawdown`` a positive fraction.

Proof of concept — reading aids, not investment advice.
"""
from __future__ import annotations

import math
from typing import List, Optional, Sequence


def _clean(values) -> List[float]:
    return [float(v) for v in values]


def sma(values: Sequence[float], n: int) -> Optional[float]:
    """Simple moving average of the last ``n`` values (None if fewer)."""
    n = int(n)
    if n <= 0 or len(values) < n:
        return None
    window = _clean(values[-n:])
    return sum(window) / n


def sma_series(values: Sequence[float], n: int) -> List[Optional[float]]:
    """SMA for every position (None until ``n`` values are available)."""
    n = int(n)
    out: List[Optional[float]] = []
    total = 0.0
    vals = _clean(values)
    for i, v in enumerate(vals):
        total += v
        if i >= n:
            total -= vals[i - n]
        out.append(total / n if n > 0 and i >= n - 1 else None)
    return out


def ema(values: Sequence[float], n: int) -> Optional[float]:
    """Exponential moving average (alpha = 2/(n+1)), seeded with the SMA of
    the first ``n`` values. None if fewer than ``n`` values."""
    n = int(n)
    if n <= 0 or len(values) < n:
        return None
    vals = _clean(values)
    alpha = 2.0 / (n + 1)
    e = sum(vals[:n]) / n
    for v in vals[n:]:
        e = alpha * v + (1 - alpha) * e
    return e


def rsi(values: Sequence[float], n: int = 14) -> Optional[float]:
    """Wilder's RSI (0..100) of the closes. Needs n+1 values. A series that
    only rose gives 100, one that only fell gives 0, a flat one 50."""
    n = int(n)
    if n <= 0 or len(values) < n + 1:
        return None
    vals = _clean(values)
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = vals[i] - vals[i - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    avg_g, avg_l = gains / n, losses / n
    for i in range(n + 1, len(vals)):
        d = vals[i] - vals[i - 1]
        avg_g = (avg_g * (n - 1) + max(d, 0.0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0.0)) / n
    if avg_l == 0.0:
        return 50.0 if avg_g == 0.0 else 100.0
    rs = avg_g / avg_l
    return 100.0 - 100.0 / (1.0 + rs)


def true_ranges(bars) -> List[float]:
    """True range of every bar after the first."""
    out = []
    for i in range(1, len(bars)):
        h, l, pc = float(bars[i].high), float(bars[i].low), float(bars[i - 1].close)
        out.append(max(h - l, abs(h - pc), abs(l - pc)))
    return out


def atr(bars, n: int = 14) -> Optional[float]:
    """Wilder's Average True Range over ``n`` periods (needs n+1 bars)."""
    n = int(n)
    if n <= 0 or len(bars) < n + 1:
        return None
    trs = true_ranges(bars)
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def atr_pct(bars, n: int = 14) -> Optional[float]:
    """ATR as a FRACTION of the last close (0.015 = 1.5 % daily range)."""
    a = atr(bars, n)
    if a is None:
        return None
    last = float(bars[-1].close)
    if last <= 0:
        return None
    return a / last


def returns(values: Sequence[float]) -> List[float]:
    """Simple period returns r_i = v_i / v_{i-1} - 1 (len = len(values)-1).
    A non-positive previous value yields 0.0 for that period."""
    vals = _clean(values)
    out = []
    for i in range(1, len(vals)):
        prev = vals[i - 1]
        out.append(vals[i] / prev - 1.0 if prev > 0 else 0.0)
    return out


def pct_change(values: Sequence[float], n: int) -> Optional[float]:
    """Total return over the last ``n`` periods as a fraction
    (values[-1] / values[-1-n] - 1); None if not enough data."""
    n = int(n)
    if n <= 0 or len(values) < n + 1:
        return None
    base = float(values[-1 - n])
    if base <= 0:
        return None
    return float(values[-1]) / base - 1.0


def max_drawdown(values: Sequence[float]) -> float:
    """Largest peak-to-trough decline as a positive fraction (0.2 = -20 %);
    0.0 for an empty or never-falling series."""
    peak = None
    worst = 0.0
    for v in _clean(values):
        if peak is None or v > peak:
            peak = v
        if peak and peak > 0:
            worst = max(worst, (peak - v) / peak)
    return worst


def stdev(values: Sequence[float]) -> float:
    """Sample standard deviation (0.0 for fewer than 2 values)."""
    vals = _clean(values)
    if len(vals) < 2:
        return 0.0
    m = sum(vals) / len(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))


def sharpe(daily_returns: Sequence[float], periods_per_year: int = 252) -> Optional[float]:
    """Annualised Sharpe ratio of daily returns with a 0 risk-free rate;
    None when there is no variation."""
    vals = _clean(daily_returns)
    sd = stdev(vals)
    if len(vals) < 2 or sd <= 1e-12:
        return None
    return (sum(vals) / len(vals)) / sd * math.sqrt(periods_per_year)


def highest(values: Sequence[float], n: int) -> Optional[float]:
    if not values:
        return None
    return max(_clean(values[-int(n):]))


def lowest(values: Sequence[float], n: int) -> Optional[float]:
    if not values:
        return None
    return min(_clean(values[-int(n):]))


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x
