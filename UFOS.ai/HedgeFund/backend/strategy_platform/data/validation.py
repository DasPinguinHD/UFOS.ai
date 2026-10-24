"""Bar validation (SPEC §3): findings, never silent repairs.

``validate_bars(rows, *, symbol, timeframe="1Day", corporate_actions=(),
now=None) -> [finding]`` with finding = ``{code, severity, message, ts}``
(severity ``error`` | ``warning`` | ``info``; ts = the bar's ts or None).
Stored on the response for later run cards; nothing is changed or dropped.

Codes:
- ``duplicate``      — the same bar timestamp twice (error)
- ``non_positive``   — open/high/low/close <= 0 (error)
- ``high_below_low`` — high < low (error)
- ``ohlc_outside``   — open or close outside [low, high] (warning)
- ``gap``            — missing daily bars vs the NYSE calendar (equities) or
                       vs every UTC day (crypto); one finding listing the
                       first missing dates (warning). Intraday: not checked.
- ``calendar_unavailable`` — the gap check needs a year the NYSE calendar
                       does not cover (warning; the check did not run)
- ``return_outlier`` — |log return| > ``SIGMA_LIMIT`` x the standard
                       deviation of the previous ``WINDOW`` returns (at least
                       ``MIN_WINDOW``), without a split/dividend with ex-date
                       on that bar's date (warning)
- ``stale``          — no new bar although the market was open: daily
                       equities without the bar of the last completed
                       session; crypto daily without a bar that ended in the
                       last 48 h; needs ``now`` (warning)

Part of UFOS.ai, a proof-of-concept portfolio project.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Iterable, List, Optional

from ..core import calendars
from ..core.instruments import instrument
from .providers.base import parse_ts

SIGMA_LIMIT = 8.0
WINDOW = 60
MIN_WINDOW = 20
MAX_LISTED_GAPS = 10
CRYPTO_STALE_HOURS = 48


def _finding(code: str, severity: str, message: str, ts: Optional[str] = None) -> dict:
    return {"code": code, "severity": severity, "message": message, "ts": ts}


def _bar_date(row: dict, asset_class: str) -> date:
    ts = parse_ts(row["ts"])
    return ts.astimezone(calendars.NY).date() if asset_class == "equity" else ts.date()


def validate_bars(rows: Iterable[dict], *, symbol: str, timeframe: str = "1Day",
                  corporate_actions: Iterable[dict] = (), now=None) -> List[dict]:
    inst = instrument(symbol)
    rows = sorted(list(rows or []), key=lambda r: r["ts"])
    out: List[dict] = []
    if not rows:
        return out

    # duplicates
    seen = set()
    unique = []
    for r in rows:
        if r["ts"] in seen:
            out.append(_finding("duplicate", "error", f"{inst.symbol}: bar {r['ts']} appears more than once", r["ts"]))
            continue
        seen.add(r["ts"])
        unique.append(r)
    rows = unique

    # price sanity
    for r in rows:
        o, h, lo, c = (float(r[k]) for k in ("open", "high", "low", "close"))
        if min(o, h, lo, c) <= 0:
            out.append(_finding("non_positive", "error", f"{inst.symbol}: non-positive price in bar {r['ts']}", r["ts"]))
            continue
        if h < lo:
            out.append(_finding("high_below_low", "error",
                                f"{inst.symbol}: high {h:g} below low {lo:g} in bar {r['ts']}", r["ts"]))
        elif not (lo <= o <= h and lo <= c <= h):
            out.append(_finding("ohlc_outside", "warning",
                                f"{inst.symbol}: open/close outside the high-low range in bar {r['ts']}", r["ts"]))

    # gaps (daily only)
    if timeframe == "1Day":
        dates = sorted({_bar_date(r, inst.asset_class) for r in rows})
        missing: List[date] = []
        if inst.asset_class == "equity":
            try:
                expected = calendars.trading_days(dates[0], dates[-1])
                present = set(dates)
                missing = [d for d in expected if d not in present]
            except calendars.CalendarUnavailable as ex:
                out.append(_finding("calendar_unavailable", "warning",
                                    f"{inst.symbol}: gap check not run — {ex.user_message}"))
        else:
            d, present = dates[0], set(dates)
            while d <= dates[-1]:
                if d not in present:
                    missing.append(d)
                d += timedelta(days=1)
        if missing:
            listed = ", ".join(x.isoformat() for x in missing[:MAX_LISTED_GAPS])
            more = f" and {len(missing) - MAX_LISTED_GAPS} more" if len(missing) > MAX_LISTED_GAPS else ""
            out.append(_finding("gap", "warning",
                                f"{inst.symbol}: {len(missing)} missing daily bar(s): {listed}{more}"))

    # return outliers (sigma from the previous WINDOW returns only — no look-ahead)
    actions_on = {str(a.get("ex_date"))[:10] for a in corporate_actions or []
                  if str(a.get("symbol") or inst.symbol).upper() == inst.symbol}
    rets: List[float] = []
    for prev, cur in zip(rows, rows[1:]):
        p, c = float(prev["close"]), float(cur["close"])
        if p <= 0 or c <= 0:
            continue
        r = math.log(c / p)
        window = rets[-WINDOW:]
        if len(window) >= MIN_WINDOW:
            mean = sum(window) / len(window)
            sd = math.sqrt(sum((x - mean) ** 2 for x in window) / (len(window) - 1))
            if sd > 0 and abs(r - mean) > SIGMA_LIMIT * sd:
                day = _bar_date(cur, inst.asset_class).isoformat()
                if day not in actions_on:
                    out.append(_finding("return_outlier", "warning",
                                        f"{inst.symbol}: return {r * 100:.1f}% on {day} is more than "
                                        f"{SIGMA_LIMIT:g} sigma and no split/dividend is known for that day",
                                        cur["ts"]))
        rets.append(r)

    # stale
    if now is not None and timeframe == "1Day":
        now_dt = parse_ts(now)
        last = rows[-1]
        if inst.asset_class == "equity":
            try:
                last_close = calendars.last_close_before(now_dt)
                if _bar_date(last, "equity") < last_close.astimezone(calendars.NY).date():
                    out.append(_finding("stale", "warning",
                                        f"{inst.symbol}: no bar for the session of "
                                        f"{last_close.astimezone(calendars.NY).date().isoformat()} "
                                        f"(newest: {_bar_date(last, 'equity').isoformat()})", last["ts"]))
            except calendars.CalendarUnavailable as ex:
                out.append(_finding("calendar_unavailable", "warning",
                                    f"{inst.symbol}: stale check not run — {ex.user_message}"))
        elif parse_ts(last["known_at"]) < now_dt - timedelta(hours=CRYPTO_STALE_HOURS):
            out.append(_finding("stale", "warning",
                                f"{inst.symbol}: no daily bar completed in the last {CRYPTO_STALE_HOURS} h", last["ts"]))
    return out
