"""``calendar.event_risk`` — 1 around high-impact US releases, else 0
(SPEC §7.8, §7.13).

Releases (FRED release ids, https://fred.stlouisfed.org/release?rid=<id>):
CPI (10), Employment Situation (50), GDP (53), FOMC Press Release (101).
Dates come from FRED ``release/dates`` (past and scheduled).

Point in time (review fix 2026-10-03):
- CPI / Employment Situation / GDP are published by BLS / BEA on calendars
  announced months in advance (a rare rescheduling, e.g. after a government
  shutdown, is also announced before the new date) → flag on the release day
  AND on the NYSE trading day before it.
- FOMC: FRED release 101 also contains UNSCHEDULED actions (e.g. 2020-03-03,
  2020-03-15, 2008-01-22, 2001-09-17) that nobody knew a day earlier, and FRED
  does not mark which dates were scheduled. So only the release day itself is
  flagged (a release on a non-trading day → the next trading day): it is
  known by that day's close, and signals trade at the next open/close. No
  day-before flag, no hardcoded meeting schedule.
The NYSE calendar comes from ``core.calendars`` (``CalendarUnavailable``
outside its years). Every calendar day in [start, end] gets 0 or 1 (weekends
0 unless a release falls on them).

Completeness is checked, never assumed: each release must have a known date
on or before ``start`` and one after ``end`` (scheduled), otherwise the
schedule does not cover the range and the feature fails with
``DataSourceUnavailable`` naming the release — no zeros are made up. The
release dates are also recorded in ``ext_observations`` (series
``release:<id>``, value 1).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Dict, Optional

from . import ExtFeature
from . import fred as fred_mod
from ...core.errors import DataSourceUnavailable

NAME = "calendar.event_risk"
SOURCE = "fred"
RELEASES = {10: "CPI", 50: "Employment Situation", 53: "GDP", 101: "FOMC Press Release"}
# Releases published on calendars announced in advance -> also the trading day before is flagged.
PRESCHEDULED = (10, 50, 53)
FOMC_RELEASE = 101           # may contain unscheduled actions -> only the release day itself


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def compute(dates_by_release: Dict[int, list], start: date, end: date) -> Dict[str, float]:
    from ...core import calendars
    for rid, name in RELEASES.items():
        ds = sorted(dates_by_release.get(rid) or [])
        if not ds or ds[0] > start.isoformat():
            raise DataSourceUnavailable(SOURCE, "macro", f"FRED has no {name} release dates on or before "
                                        f"{start.isoformat()} — the event-risk flag can't be computed for that range")
        if ds[-1] <= end.isoformat():
            raise DataSourceUnavailable(SOURCE, "macro", f"FRED has no scheduled {name} release after "
                                        f"{ds[-1]} — the event-risk flag can't be computed through {end.isoformat()}")
    flagged = set()
    lo, hi = (start - timedelta(days=10)).isoformat(), (end + timedelta(days=10)).isoformat()
    for rid in RELEASES:
        for iso in dates_by_release[rid]:
            if not (lo <= iso <= hi):
                continue
            d = date.fromisoformat(iso)
            if rid in PRESCHEDULED:
                flagged.add(iso)
                flagged.add(calendars.previous_trading_day(d).isoformat())
            else:   # FOMC: the day it became known (next trading day if published on a non-trading day)
                flagged.add(d.isoformat() if calendars.is_trading_day(d) else calendars.next_trading_day(d).isoformat())
    out = {}
    d = start
    while d <= end:
        iso = d.isoformat()
        out[iso] = 1.0 if iso in flagged else 0.0
        d += timedelta(days=1)
    return out


def load(start, end, *, client=None, now: Optional[datetime] = None) -> Dict[str, float]:
    """{date_iso: 0.0|1.0} for every calendar day start..end."""
    from ...storage import db
    start, end = _as_date(start), _as_date(end)
    if end < start:
        raise ValueError("end before start")
    client = client or fred_mod.default_client()
    fetched_at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    dates_by_release = {}
    for rid in RELEASES:
        dates = client.release_dates(rid)
        dates_by_release[rid] = dates
        db.upsert_ext_observations([{"series_id": f"release:{rid}", "date": d, "value": 1.0, "realtime_start": d,
                                     "source": SOURCE, "fetched_at": fetched_at} for d in dates])
    return compute(dates_by_release, start, end)


FEATURE = ExtFeature(
    name=NAME,
    description=("1 on the trading day before and on the day of a pre-scheduled high-impact US release (CPI, "
                 "employment situation, GDP) and on the day of an FOMC statement (that day only — FOMC actions "
                 "can be unscheduled), else 0 — from FRED release dates."),
    load=load, point_in_time=True, scope="global", source=SOURCE,
)
