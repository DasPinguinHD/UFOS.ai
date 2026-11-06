"""Account-level risk figures for the Overview (SP §26.2) — pure functions, no I/O.

* Daily returns from end-of-day equity: the LAST point of each NY trading day whose regular session has closed by
  ``now``; points on non-trading days are ignored; a point without a finite positive equity is not a valuation and is
  skipped. Simple returns ``r_t = E_t / E_{t-1} - 1`` only between CONSECUTIVE trading days — a missing day leaves a
  gap (that pair is skipped and counted), never interpolated.
* VaR 99 % / ES 97.5 % (1 day), historical simulation on the daily P&L in USD: scenario P&L ``E_latest * r_t``, loss
  positive (``-E_latest * r_t``). Quantile convention identical to RiskLab ``src/risklab/models/historical.py``
  ``hs_var_es`` (re-implemented here: RiskLab's README forbids imports in either direction):
  sort the n losses ascending, ``VaR_a = L_(k)`` with ``k = ceil(round(n * a, 9))`` (1-based, no interpolation),
  ``ES_a`` = mean of all losses ``>= VaR_a`` (ties included); a window shorter than ``ceil(round(1 / (1 - a), 9))``
  is refused (99 % → 100, 97.5 % → 40). On top of that the Overview requires ≥ 60 daily returns (contract).
* vol20: sample standard deviation (n - 1) of the last 20 daily returns × √252, in %.
* Max drawdown: largest peak-to-trough fall of equity over the whole series (every point, not only end of day), in %.

Part of UFOS.ai, a proof-of-concept portfolio project; paper trading only.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Iterable, List, Optional, Tuple

MIN_RETURNS = 60
VOL_WINDOW = 20
TRADING_DAYS = 252
VAR_ALPHA = 0.99
ES_ALPHA = 0.975
_ROUND = 9


def _ceil_rounded(x: float) -> int:
    return int(math.ceil(round(float(x), _ROUND)))


def min_window(alpha: float) -> int:
    """``ceil(1 / (1 - alpha))`` after rounding (RiskLab ``historical.min_window``)."""
    return _ceil_rounded(1.0 / (1.0 - alpha))


def hs_var_es(losses: Iterable[float], alpha: float) -> Tuple[float, float]:
    """(VaR, ES) of the losses at level alpha, RiskLab HS convention (see module docstring); ValueError when the
    sample is shorter than ``min_window(alpha)``."""
    xs = sorted(float(x) for x in losses)
    n = len(xs)
    need = min_window(alpha)
    if n < need:
        raise ValueError(f"historical simulation at {alpha * 100:g} % needs at least {need} observations "
                         f"(ceil(1 / (1 - alpha))), have {n}")
    k = _ceil_rounded(n * alpha)
    v = xs[k - 1]
    tail = [x for x in xs if x >= v]
    return v, sum(tail) / len(tail)


def _parse_ts(ts) -> Optional[datetime]:
    if isinstance(ts, datetime):
        dt = ts
    else:
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _equity(p) -> Optional[float]:
    try:
        e = float(p.get("equity"))
    except (TypeError, ValueError):
        return None
    return e if math.isfinite(e) and e > 0 else None


def end_of_day(points: Iterable[dict], now: datetime) -> List[Tuple[date, float]]:
    """[(NY trading day, last equity of that day)] oldest first, only days whose regular session closed by ``now``."""
    from ..core import calendars
    last: dict = {}
    for p in points:
        dt, e = _parse_ts(p.get("ts")), _equity(p)
        if dt is None or e is None:
            continue
        d = dt.astimezone(calendars.NY).date()
        if d not in last or dt >= last[d][0]:
            last[d] = (dt, e)
    out = []
    for d in sorted(last):
        try:
            b = calendars.session_bounds(d)
        except calendars.CalendarUnavailable:
            continue
        if b is None or b[1] > now:
            continue
        out.append((d, last[d][1]))
    return out


def daily_returns(eod: List[Tuple[date, float]]) -> Tuple[List[float], int]:
    """(simple returns between consecutive trading days, number of skipped gaps)."""
    from ..core import calendars
    rets, gaps = [], 0
    for (d0, e0), (d1, e1) in zip(eod, eod[1:]):
        try:
            consecutive = calendars.previous_trading_day(d1) == d0
        except calendars.CalendarUnavailable:
            consecutive = False                      # outside the NYSE calendar: a gap, counted, never guessed
        if not consecutive:
            gaps += 1
            continue
        rets.append(e1 / e0 - 1.0)
    return rets, gaps


def max_drawdown_pct(points: Iterable[dict]) -> Optional[float]:
    peak, worst, n = None, 0.0, 0
    for p in points:
        e = _equity(p)
        if e is None:
            continue
        n += 1
        peak = e if peak is None or e > peak else peak
        worst = max(worst, (peak - e) / peak * 100.0)
    return worst if n >= 2 else None


def vol_ann_pct(rets: List[float], window: int = VOL_WINDOW) -> Optional[float]:
    if len(rets) < window:
        return None
    w = rets[-window:]
    m = sum(w) / window
    var = sum((x - m) ** 2 for x in w) / (window - 1)
    return math.sqrt(var) * math.sqrt(TRADING_DAYS) * 100.0


def _fig(value, unit, method, reason=None) -> dict:
    ok = value is not None and reason is None
    return {"value": value if ok else None, "unit": unit, "available": ok, "reason": None if ok else reason,
            "method": method}


VAR_METHOD = ("historical simulation, 1 day, 99 %: k-th smallest of the n daily losses (k = ceil(n·0.99), no "
              "interpolation; RiskLab hs_var_es convention); loss = −(latest equity × daily return)")
ES_METHOD = ("historical simulation, 1 day, 97.5 %: mean of the daily losses ≥ VaR 97.5 % (k = ceil(n·0.975); "
             "RiskLab hs_var_es convention); loss = −(latest equity × daily return)")
VOL_METHOD = "sample standard deviation of the last 20 daily returns × √252"
DD_METHOD = "largest peak-to-trough fall of equity over the whole stored series"
EXPO_NET = "(long market value − short market value) / equity"
EXPO_GROSS = "(long market value + short market value) / equity"


def series_figures(points: List[dict], now: datetime, unavailable: Optional[str] = None) -> Tuple[dict, int, dict]:
    """(figures var99_1d / es975_1d / max_drawdown_pct / vol20_ann_pct, number of daily returns, details).
    ``unavailable`` = a reason that makes every series figure unavailable (no series)."""
    if unavailable:
        return ({"var99_1d": _fig(None, "usd", VAR_METHOD, unavailable),
                 "es975_1d": _fig(None, "usd", ES_METHOD, unavailable),
                 "max_drawdown_pct": _fig(None, "pct", DD_METHOD, unavailable),
                 "vol20_ann_pct": _fig(None, "pct", VOL_METHOD, unavailable)}, 0, {})
    eod = end_of_day(points, now)
    rets, gaps = daily_returns(eod)
    n = len(rets)
    latest = next((_equity(p) for p in reversed(points) if _equity(p) is not None), None)
    figs = {}
    for key, alpha, method in (("var99_1d", VAR_ALPHA, VAR_METHOD), ("es975_1d", ES_ALPHA, ES_METHOD)):
        if n < MIN_RETURNS:
            figs[key] = _fig(None, "usd", method, f"needs ≥ {MIN_RETURNS} daily returns, have {n}")
            continue
        if latest is None:
            figs[key] = _fig(None, "usd", method, "no valued equity point")
            continue
        try:
            var, es = hs_var_es([-latest * r for r in rets], alpha)
        except ValueError:
            figs[key] = _fig(None, "usd", method, f"historical simulation at {alpha * 100:g} % needs ≥ "
                                                  f"{min_window(alpha)} daily returns (ceil(1 / (1 − α)), RiskLab "
                                                  f"convention), have {n}")
            continue
        figs[key] = _fig(var if key == "var99_1d" else es, "usd", method)
    dd = max_drawdown_pct(points)
    figs["max_drawdown_pct"] = _fig(dd, "pct", DD_METHOD, None if dd is not None else "fewer than 2 equity points")
    vol = vol_ann_pct(rets)
    figs["vol20_ann_pct"] = _fig(vol, "pct", VOL_METHOD,
                                 None if vol is not None else f"needs ≥ {VOL_WINDOW} daily returns, have {n}")
    return figs, n, {"gaps_skipped": gaps, "first_day": eod[0][0].isoformat() if eod else None,
                     "last_day": eod[-1][0].isoformat() if eod else None, "latest_equity": latest}


def exposure_figures(equity, long_mv, short_mv, reason: Optional[str] = None) -> dict:
    vals = []
    for v in (equity, long_mv, short_mv):
        try:
            f = float(v)
        except (TypeError, ValueError):
            f = None
        vals.append(f if f is not None and math.isfinite(f) else None)
    eq, lmv, smv = vals
    if reason is None and (eq is None or lmv is None or smv is None):
        reason = "the account state has no equity / long / short market value"
    elif reason is None and eq <= 0:
        reason = "equity is not positive"
    if reason:
        return {"net_exposure_pct": _fig(None, "pct", EXPO_NET, reason),
                "gross_exposure_pct": _fig(None, "pct", EXPO_GROSS, reason)}
    return {"net_exposure_pct": _fig((lmv - abs(smv)) / eq * 100.0, "pct", EXPO_NET),
            "gross_exposure_pct": _fig((lmv + abs(smv)) / eq * 100.0, "pct", EXPO_GROSS)}
