"""Expiration and event calendar (SP §21.12, task §5) on the NYSE calendar (``core/calendars.py``; outside its years
``CalendarUnavailable`` — nothing guessed).

* **Monthly options expiration**: the third Friday of the month; if that Friday is not a trading day (holiday), the
  trading day before it (e.g. Good Friday 2025-04-18 → Thu 2025-04-17; Juneteenth 2026-06-19 → Thu 2026-06-18).
* **Quarterly expirations**: the monthly expirations of March, June, September and December (the quarterly cycle;
  the separate end-of-quarter SPX/SPY series are not modelled — stated).
* **VIX expiration** in month m: 30 calendar days before the SPX monthly expiration of month m + 1 (the third
  Friday, or the trading day before it if that Friday is a holiday — Cboe rule); if that day is not a trading day,
  the trading day before it (e.g. 2025-03-18 and 2026-05-19, both Tuesdays).
* **FOMC** decision days (the second meeting day) from the published schedule the Newsroom keeps
  (``newsroom/calendar_collector.FOMC_MEETINGS``, federalreserve.gov) — a static published schedule; **CPI** release
  dates from the Newsroom economic-calendar snapshot (FRED release dates), used only when the snapshot was produced
  by the as-of time (point-in-time); otherwise "unavailable" with the reason.

Features: days to the next monthly / quarterly / VIX expiration and to the next FOMC / CPI date (calendar days, from
the as-of NY date; an event ON the as-of date counts as 0 days).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from strategy_platform.core import calendars

NY = calendars.NY


def third_friday(y: int, m: int) -> date:
    d = date(y, m, 1)
    d += timedelta(days=(4 - d.weekday()) % 7)
    return d + timedelta(days=14)


def _trading_on_or_before(d: date) -> date:
    while not calendars.is_trading_day(d):
        d -= timedelta(days=1)
    return d


def monthly_expiration(y: int, m: int) -> date:
    return _trading_on_or_before(third_friday(y, m))


def vix_expiration(y: int, m: int) -> date:
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    spx = monthly_expiration(ny, nm)
    return _trading_on_or_before(spx - timedelta(days=30))


def _next_by_month(fn: Callable[[int, int], date], d: date, months: Sequence[int] = tuple(range(1, 13))) -> date:
    y, m = d.year, d.month
    for _ in range(30):
        if m in months:
            x = fn(y, m)
            if x >= d:
                return x
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    raise ValueError("no date found within 30 months")


def next_monthly(d: date) -> date:
    return _next_by_month(monthly_expiration, d)


def next_quarterly(d: date) -> date:
    return _next_by_month(monthly_expiration, d, (3, 6, 9, 12))


def next_vix(d: date) -> date:
    return _next_by_month(vix_expiration, d)


def load_fomc() -> Tuple[List[date], Optional[str]]:
    """FOMC decision days from the Newsroom's published schedule (lazy import; (dates, error))."""
    try:
        from newsroom.calendar_collector import FOMC_MEETINGS
        return sorted(date.fromisoformat(second) for _first, second, _sep in FOMC_MEETINGS), None
    except Exception as ex:          # noqa: BLE001 — said, never guessed
        return [], f"FOMC schedule not loadable ({type(ex).__name__})"


def load_cpi() -> Tuple[List[date], Optional[int], Optional[str]]:
    """CPI release dates from the Newsroom calendar snapshot: (dates, snapshot time ms, error)."""
    try:
        from newsroom import db as ndb
        payload = ndb.load_calendar()
    except Exception as ex:          # noqa: BLE001
        return [], None, f"Newsroom calendar not readable ({type(ex).__name__})"
    if not isinstance(payload, dict):
        return [], None, "no Newsroom economic-calendar snapshot"
    try:
        upd = datetime.fromisoformat(str(payload.get("updated")).replace("Z", "+00:00"))
        if upd.tzinfo is None:
            upd = upd.replace(tzinfo=timezone.utc)
        upd_ms = int(upd.timestamp() * 1000)
    except (TypeError, ValueError):
        return [], None, "the calendar snapshot has no valid 'updated' time"
    out = []
    for e in payload.get("events") or []:
        if isinstance(e, dict) and e.get("series_id") == "CPIAUCSL" and e.get("kind") == "release":
            try:
                out.append(date.fromisoformat(str(e.get("date"))[:10]))
            except ValueError:
                continue
    return sorted(set(out)), upd_ms, None


def features(as_of_ms: int, fomc: Optional[Tuple[List[date], Optional[str]]] = None,
             cpi: Optional[Tuple[List[date], Optional[int], Optional[str]]] = None) -> dict:
    """Days to the next expirations / events from the NY date of ``as_of_ms``; every missing part has its reason."""
    d = datetime.fromtimestamp(as_of_ms / 1000, timezone.utc).astimezone(NY).date()
    out: Dict[str, object] = {"as_of_date": d.isoformat(), "reasons": []}
    for name, fn in (("monthly_expiration", next_monthly), ("quarterly_expiration", next_quarterly),
                     ("vix_expiration", next_vix)):
        try:
            x = fn(d)
            out[name] = {"date": x.isoformat(), "days": (x - d).days}
        except Exception as ex:      # noqa: BLE001 — calendar end
            out[name] = None
            out["reasons"].append(f"{name}: {str(ex)[:120]}")
    dates, err = fomc if fomc is not None else load_fomc()
    nxt = next((x for x in dates if x >= d), None)
    out["fomc"] = {"date": nxt.isoformat(), "days": (nxt - d).days, "source": "published FOMC schedule"} if nxt else None
    if nxt is None:
        out["reasons"].append(f"fomc: {err or 'no scheduled meeting after the as-of date in the schedule'}")
    cdates, upd_ms, cerr = cpi if cpi is not None else load_cpi()
    if cerr:
        out["cpi"] = None
        out["reasons"].append(f"cpi: {cerr}")
    elif upd_ms is None or upd_ms > as_of_ms:
        out["cpi"] = None
        out["reasons"].append("cpi: the economic-calendar snapshot is newer than the as-of time (point-in-time)")
    else:
        c = next((x for x in cdates if x >= d), None)
        out["cpi"] = {"date": c.isoformat(), "days": (c - d).days, "source": "Newsroom economic calendar (FRED)"} \
            if c else None
        if c is None:
            out["reasons"].append("cpi: no CPI release in the calendar snapshot's window")
    events = [(v["days"], k) for k, v in out.items() if isinstance(v, dict) and "days" in v]
    out["next_event"] = None
    if events:
        days, name = min(events)
        out["next_event"] = {"name": name, **out[name]}
    return out


def expirations(start: date, end: date) -> List[dict]:
    """Monthly / quarterly / VIX expirations in [start, end] (for GET …/calendar)."""
    out = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        for kind, fn in (("monthly", monthly_expiration), ("vix", vix_expiration)):
            x = fn(y, m)
            if start <= x <= end:
                out.append({"date": x.isoformat(), "kind": kind, "quarterly": kind == "monthly" and m in (3, 6, 9, 12)})
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return sorted(out, key=lambda e: (e["date"], e["kind"]))
