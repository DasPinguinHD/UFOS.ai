"""Live vs expected (SPEC §19.1): the frozen expected basis of an incubation run (its ``expected_json`` from the
holdout card, §17.2 — never recomputed, so no look-ahead), the realised sub-ledger log returns of completed
sessions, the tracking series and the deviation statistics.

Pure functions (``track``) plus thin readers (``basis``, ``realised``); no network, no AI.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence, Tuple

BAND_Z = 1.96                 # the §17.2 incubation band (shown next to the decay line)
ROLL_VOL_WINDOW = 20
PERIODS = {"nyse": 252, "utc": 365}
LOG_FLOOR = 1e-9              # a −100 % session (wipe-out) is booked as ln(1e-9), never skipped


def periods(calendar: str) -> int:
    return PERIODS.get(calendar or "nyse", 252)


def basis(run: dict) -> Optional[dict]:
    """The frozen expected basis of the run (μ, σ of daily log returns, max DD, vol, source run) or None."""
    raw = run.get("expected_json")
    if not raw:
        return None
    try:
        exp = json.loads(raw)
    except (TypeError, ValueError):
        return None
    try:
        mu, sd = float(exp["mean_daily"]), float(exp["std_daily"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (math.isfinite(mu) and math.isfinite(sd)):
        return None
    mdd = exp.get("max_drawdown")
    mdd = abs(float(mdd)) if isinstance(mdd, (int, float)) and math.isfinite(float(mdd)) else None
    cal = run.get("calendar") or exp.get("calendar") or "nyse"
    vol = sd * math.sqrt(periods(cal))
    return {"mean_daily": mu, "std_daily": sd, "vol": vol, "max_drawdown": mdd,
            "sharpe": (mu / sd * math.sqrt(periods(cal))) if sd > 0 else None,
            "source_run_id": exp.get("source_run_id"), "sessions": exp.get("sessions"), "calendar": cal}


def to_log(r: float) -> float:
    return math.log(max(1.0 + float(r), LOG_FLOOR))


def realised(run: dict, upto: Optional[date] = None) -> List[Tuple[str, float]]:
    """[(day, log return)] of the run's sub-ledger from its start, days ≤ ``upto`` (the last COMPLETED session)."""
    from datetime import timedelta
    from ..core import calendars
    from ..portfolio import ledger
    start = date.fromisoformat(run["start_date"])
    # from the snapshot of the session BEFORE the start, so the first session's own return is included
    prev = start - timedelta(days=1) if run.get("calendar") == "utc" else calendars.previous_trading_day(start)
    rows = ledger.daily_returns(run["account_id"], run["source_id"], prev.isoformat())
    lim = upto.isoformat() if upto is not None else None
    return [(d, to_log(r)) for d, r in rows if d >= run["start_date"] and (lim is None or d <= lim)]


def last_completed(run: dict, now: datetime) -> Optional[date]:
    from ..live import incubation
    return incubation.last_completed_session(run["calendar"], now, 20)


def _std(xs: Sequence[float]) -> Optional[float]:
    n = len(xs)
    if n < 2:
        return None
    m = math.fsum(xs) / n
    return math.sqrt(math.fsum((x - m) ** 2 for x in xs) / (n - 1))


def drawdown_now(lr: Sequence[float]) -> Tuple[float, float]:
    """(current drawdown, max drawdown) of the cumulative log curve starting at 0 (fractions ≥ 0)."""
    cum, peak, mdd = 0.0, 0.0, 0.0
    for x in lr:
        cum += x
        peak = max(peak, cum)
        mdd = max(mdd, 1.0 - math.exp(cum - peak))
    return 1.0 - math.exp(cum - peak), mdd


def track(exp: Optional[dict], series: List[Tuple[str, float]], decay_line=None) -> dict:
    """Tracking series + realised + deviation statistics (§19.1). ``decay_line(k, exp) -> float|None`` adds the
    decay rule's lower line per session (from decay.py)."""
    lr = [x for _d, x in series]
    n = len(lr)
    P = periods((exp or {}).get("calendar") or "nyse")
    mu = sd = None
    if exp:
        mu, sd = float(exp["mean_daily"]), float(exp["std_daily"])
    points: List[dict] = []
    cum, peak = 0.0, 0.0
    for k, (d, x) in enumerate(series, start=1):
        cum += x
        peak = max(peak, cum)
        win = lr[max(0, k - ROLL_VOL_WINDOW):k]
        s = _std(win) if len(win) >= ROLL_VOL_WINDOW else None
        pt = {"day": d, "n": k, "ret": x, "cum_log": cum, "exp_mean": None, "band_lo": None, "band_hi": None,
              "decay_lo": None, "drawdown": 1.0 - math.exp(cum - peak),
              "roll_vol": s * math.sqrt(P) if s is not None else None}
        if mu is not None:
            pt["exp_mean"] = k * mu
            pt["band_lo"] = k * mu - BAND_Z * sd * math.sqrt(k)
            pt["band_hi"] = k * mu + BAND_Z * sd * math.sqrt(k)
            if decay_line is not None:
                pt["decay_lo"] = decay_line(lr[:k], exp)
        points.append(pt)
    realised_out = {"cum_log_return": None, "return": None, "vol": None, "sharpe": None, "drawdown": None,
                    "max_drawdown": None}
    deviation = {"z_cum": None, "band": None, "in_band": None, "tracking_error": None, "vol_ratio": None,
                 "dd_ratio": None, "rolling_z": None}
    if n >= 1:
        c = math.fsum(lr)
        dd_now, mdd = drawdown_now(lr)
        realised_out.update({"cum_log_return": c, "return": math.exp(c) - 1.0, "drawdown": dd_now,
                             "max_drawdown": mdd})
        s = _std(lr)
        if s is not None:
            m = math.fsum(lr) / n
            realised_out["vol"] = s * math.sqrt(P)
            realised_out["sharpe"] = (m / s * math.sqrt(P)) if s > 0 else None
        if mu is not None and n >= 2:
            band = {"lo": n * mu - BAND_Z * sd * math.sqrt(n), "hi": n * mu + BAND_Z * sd * math.sqrt(n), "n": n}
            deviation["band"] = band
            deviation["in_band"] = band["lo"] <= c <= band["hi"]
            if sd > 0:
                deviation["z_cum"] = (c - n * mu) / (sd * math.sqrt(n))
                w = min(n, 60)
                deviation["rolling_z"] = (math.fsum(lr[-w:]) - w * mu) / (sd * math.sqrt(w))
            deviation["tracking_error"] = math.sqrt(math.fsum((x - mu) ** 2 for x in lr) / n) * math.sqrt(P)
            if realised_out["vol"] is not None and exp.get("vol"):
                deviation["vol_ratio"] = realised_out["vol"] / exp["vol"]
            if exp.get("max_drawdown"):
                deviation["dd_ratio"] = mdd / exp["max_drawdown"]
    if n < 2:
        realised_out = {k: None for k in realised_out}
        deviation = {k: None for k in deviation}
    return {"points": points, "realised": realised_out, "deviation": deviation, "sessions": n,
            "last_session": series[-1][0] if series else None}


def expected_out(exp: Optional[dict]) -> Optional[dict]:
    if exp is None:
        return None
    return {k: exp.get(k) for k in ("mean_daily", "std_daily", "vol", "sharpe", "max_drawdown", "source_run_id")}


def session_counts(series: List[Tuple[str, float]], start: str, end: str) -> Dict[str, float]:
    """Sessions in [start, end) (ISO days) → {n, log_return}."""
    xs = [x for d, x in series if start <= d < end]
    return {"n": len(xs), "log_return": math.fsum(xs) if xs else 0.0}
