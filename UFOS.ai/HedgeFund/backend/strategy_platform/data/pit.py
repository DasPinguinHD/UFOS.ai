"""Point-in-time rules for bars (SPEC §3).

``known_at`` = the moment a bar could be known, i.e. its END (provider delay
for the IEX feed: 0):

- daily equity bar (NY trading date D): the regular-session close of D from
  core.calendars (16:00 New York, 13:00 on early-close days); a daily bar on
  a date without a session (should not exist) counts as known at 16:00 NY.
  Outside the calendar's years ``CalendarUnavailable`` is raised — no guess.
- every other bar (crypto daily, intraday): bar start + timeframe length.
  Crypto daily bars are therefore known 24 h after their start, whichever
  midnight the provider aligns them to.

``bars_as_of(symbol, timeframe, as_of)`` returns cached bars with
``known_at <= as_of`` only — never a bar that was not yet complete at
``as_of`` (no look-ahead in backtests / research).

Part of UFOS.ai, a proof-of-concept portfolio project.
"""
from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Iterable, List, Optional

from ..core import calendars
from .providers.base import TIMEFRAMES, iso, parse_ts

EQUITY_CLOSE_NY = time(16, 0)


def known_at(asset_class: str, timeframe: str, bar_start) -> datetime:
    """Aware UTC datetime when the bar starting at ``bar_start`` is complete."""
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"unknown timeframe {timeframe!r}")
    start = parse_ts(bar_start)
    if timeframe == "1Day" and asset_class == "equity":
        d = start.astimezone(calendars.NY).date()
        bounds = calendars.session_bounds(d)
        if bounds is not None:
            return bounds[1]
        return datetime.combine(d, EQUITY_CLOSE_NY, tzinfo=calendars.NY).astimezone(timezone.utc)
    return start + TIMEFRAMES[timeframe]


def known_at_iso(asset_class: str, timeframe: str, bar_start) -> str:
    return iso(known_at(asset_class, timeframe, bar_start))


def is_known(row: dict, as_of) -> bool:
    return parse_ts(row["known_at"]) <= parse_ts(as_of)


def filter_known(rows: Iterable[dict], as_of) -> List[dict]:
    """Only rows with known_at <= as_of (order kept)."""
    cutoff = parse_ts(as_of)
    return [r for r in rows if parse_ts(r["known_at"]) <= cutoff]


def bars_as_of(symbol: str, timeframe: str, as_of, *, start=None, source: Optional[str] = None,
               store=None, adjustment: str = "all") -> List[dict]:
    """Cached bars of ``symbol`` known at ``as_of`` (oldest first), from
    hedgefund.db (``store`` = storage.db module or a compatible object;
    ``source`` = the configured source of the bars' data type). Reads the
    cache only — callers that need the newest bars use data.cache first."""
    from ..core.instruments import instrument
    from .providers.base import bars_data_type
    if store is None:
        from ..storage import db as store
    if source is None:
        from . import registry
        source = registry.configured_source(bars_data_type(instrument(symbol).asset_class))
    cutoff = parse_ts(as_of)
    kw = {"known_by": iso(cutoff)}
    if adjustment != "all":             # stage-1 call shape for the default series
        kw["adjustment"] = adjustment
    rows = store.query_bars(instrument(symbol).symbol, timeframe, start, iso(cutoff), source, **kw)
    return filter_known(rows, cutoff)


SESSION_OPEN_NY = time(9, 30)


def action_known_at(ex_date) -> datetime:
    """Point-in-time of a corporate action (SPEC §8.1): the regular open
    (09:30 New York) of its ex-date. Actions are announced earlier, so this is
    never before the moment they were knowable (conservative; no look-ahead).
    Works for every year (no calendar lookup)."""
    from datetime import date as _date
    d = ex_date if isinstance(ex_date, _date) and not isinstance(ex_date, datetime) else \
        _date.fromisoformat(str(ex_date)[:10])
    return datetime.combine(d, SESSION_OPEN_NY, tzinfo=calendars.NY).astimezone(timezone.utc)


def action_known_at_iso(ex_date) -> str:
    return iso(action_known_at(ex_date))
