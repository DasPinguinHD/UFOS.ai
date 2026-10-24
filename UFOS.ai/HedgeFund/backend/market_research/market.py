"""Market status (SPEC §7.3, C2): from ``strategy_platform/core/calendars.py`` (hardcoded NYSE calendar,
early closes) — no Alpaca clock/calendar request (C2: reuse, no extra requests). Outside the calendar's
years → ``CalendarUnavailable`` (503 with its message, nothing guessed).

Equities (America/New_York): ``pre_market`` 04:00 – open, ``open`` 09:30 – close (16:00, 13:00 on
early-close days), ``after_hours`` close – 20:00 (17:00 on early-close days, SPEC §11 verify),
``closed`` otherwise, ``holiday`` on a weekday that is not a trading day. ``session_date`` = the session
in progress (pre/open/after) or the next one (closed/holiday); ``opens_at``/``closes_at`` = its regular
bounds; ``next_open_at``/``next_close_at`` = the first regular open/close strictly after now. All UTC ISO;
the block ``ny`` repeats them with the New York offset (display help, MR2a addition).
Crypto: always ``open``; ``day_start``/``day_end`` = the current UTC day (Q9).

Part of UFOS.ai, a proof-of-concept portfolio project; research only.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from strategy_platform.core import calendars
from strategy_platform.core.clock import iso, to_utc

from .bars import aggregate as agg

NY = calendars.NY


def _ny(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(NY).isoformat(timespec="seconds") if dt else None


def equities(now=None) -> dict:
    now = to_utc(now or datetime.now(timezone.utc))
    today = now.astimezone(NY).date()
    state = None
    session_date = today
    if calendars.is_trading_day(today):
        o, c = calendars.session_bounds(today)
        pre, post = agg.session_window(today, "eth")
        if pre <= now < o:
            state = "pre_market"
        elif o <= now < c:
            state = "open"
        elif c <= now < post:
            state = "after_hours"
        else:
            state = "closed"
            if now >= post:
                session_date = calendars.next_trading_day(today)
    else:
        state = "holiday" if today.weekday() < 5 else "closed"
        session_date = calendars.next_trading_day(today)
    opens_at, closes_at = calendars.session_bounds(session_date)
    ext_open, ext_close = agg.session_window(session_date, "eth")
    if opens_at > now:
        next_open = opens_at
    else:
        next_open = calendars.session_bounds(calendars.next_trading_day(session_date))[0]
    if closes_at > now:
        next_close = closes_at
    else:
        next_close = calendars.session_bounds(calendars.next_trading_day(session_date))[1]
    early = calendars.is_early_close(session_date)
    times = {"opens_at": opens_at, "closes_at": closes_at, "next_open_at": next_open, "next_close_at": next_close,
             "extended_opens_at": ext_open, "extended_closes_at": ext_close}
    out = {"state": state, "session_date": session_date.isoformat(), "early_close": bool(early)}
    out.update({k: iso(v) for k, v in times.items()})
    out["tz"] = "America/New_York"
    out["ny"] = {k: _ny(v) for k, v in times.items()}
    return out


def crypto(now=None) -> dict:
    now = to_utc(now or datetime.now(timezone.utc))
    start = agg.utc_midnight(now.date())
    return {"state": "open", "day_start": iso(start), "day_end": iso(start + timedelta(days=1)), "tz": "UTC"}


def status(now=None) -> dict:
    now = to_utc(now or datetime.now(timezone.utc))
    return {"equities": equities(now), "crypto": crypto(now), "generated_at": iso(now)}
