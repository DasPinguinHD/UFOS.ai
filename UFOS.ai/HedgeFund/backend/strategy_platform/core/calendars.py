"""Market calendars (SPEC §1): NYSE regular session + crypto (24/7, UTC days,
perp funding windows). Single source of truth for session logic — moved here
from ``fund/market.py`` (2026-10-02), which re-exports these names.

- NYSE regular session 09:30–16:00 America/New_York, 13:00 on early-close
  days. Holidays and early closes are hardcoded for 2016–2027 (update yearly
  from the official NYSE calendar), rule-based for 2000–2015 (historical
  rule set + special closures, see ``RULE_YEARS``) and for 2028–2030 by the
  current NYSE rules (``PROJECTED_YEARS``, stage 10 S10-H: 2028 = the published
  calendar; 2029–2030 projected, see below); ``CALENDAR_YEARS`` = 2000–2030.
- Outside ``CALENDAR_YEARS`` the calendar does NOT guess (project rule "no
  unrequested fallbacks", 2026-10-02): ``is_trading_day`` and everything built
  on it raise ``CalendarUnavailable``. (``fund/market.py`` used to log a
  warning and treat every weekday as a trading day — that default was never
  accepted by the user and is not carried over.)
- Times: inputs may be aware/naive datetimes (naive = UTC), dates, or ISO
  strings; outputs are aware UTC datetimes / ``date`` objects / UTC ISO strings.

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from .errors import UserFacingError
from .instruments import asset_class

NY = ZoneInfo("America/New_York")
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)


class CalendarUnavailable(UserFacingError):
    """A date outside the hardcoded NYSE calendar was asked for."""

    def __init__(self, year: int):
        super().__init__(f"The NYSE trading calendar has no data for {year} (covered: "
                         f"{CALENDAR_YEARS[0]}–{CALENDAR_YEARS[-1]}). This function is paused until the "
                         "calendar is updated — no trading days are guessed.")
        self.year = year


# NYSE full-day closures (official NYSE holiday calendars; national days of
# mourning included: 2018-12-05 George H. W. Bush, 2025-01-09 Jimmy Carter).
NYSE_HOLIDAYS = frozenset(date.fromisoformat(d) for d in (
    # 2016
    "2016-01-01", "2016-01-18", "2016-02-15", "2016-03-25", "2016-05-30", "2016-07-04", "2016-09-05",
    "2016-11-24", "2016-12-26",
    # 2017 (Jan 1 a Sunday -> Jan 2)
    "2017-01-02", "2017-01-16", "2017-02-20", "2017-04-14", "2017-05-29", "2017-07-04", "2017-09-04",
    "2017-11-23", "2017-12-25",
    # 2018
    "2018-01-01", "2018-01-15", "2018-02-19", "2018-03-30", "2018-05-28", "2018-07-04", "2018-09-03",
    "2018-11-22", "2018-12-05", "2018-12-25",
    # 2019
    "2019-01-01", "2019-01-21", "2019-02-18", "2019-04-19", "2019-05-27", "2019-07-04", "2019-09-02",
    "2019-11-28", "2019-12-25",
    # 2020 (Jul 4 a Saturday -> Jul 3)
    "2020-01-01", "2020-01-20", "2020-02-17", "2020-04-10", "2020-05-25", "2020-07-03", "2020-09-07",
    "2020-11-26", "2020-12-25",
    # 2021 (Jul 4 a Sunday -> Jul 5; Dec 25 a Saturday -> Dec 24; Jan 1 2022 a Saturday: no closure)
    "2021-01-01", "2021-01-18", "2021-02-15", "2021-04-02", "2021-05-31", "2021-07-05", "2021-09-06",
    "2021-11-25", "2021-12-24",
    # 2022 (Juneteenth first observed; Jun 19 a Sunday -> Jun 20; Dec 25 a Sunday -> Dec 26)
    "2022-01-17", "2022-02-21", "2022-04-15", "2022-05-30", "2022-06-20", "2022-07-04", "2022-09-05",
    "2022-11-24", "2022-12-26",
    # 2023 (Jan 1 a Sunday -> Jan 2)
    "2023-01-02", "2023-01-16", "2023-02-20", "2023-04-07", "2023-05-29", "2023-06-19", "2023-07-04",
    "2023-09-04", "2023-11-23", "2023-12-25",
    # 2024
    "2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27", "2024-06-19", "2024-07-04",
    "2024-09-02", "2024-11-28", "2024-12-25",
    # 2025
    "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26", "2025-06-19",
    "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
    # 2026 (Jul 4 a Saturday -> Jul 3)
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
    "2026-09-07", "2026-11-26", "2026-12-25",
    # 2027 (Jun 19 a Saturday -> Jun 18; Jul 4 a Sunday -> Jul 5; Dec 25 a Saturday -> Dec 24;
    # Jan 1 2028 is a Saturday: NYSE does not close on Fri Dec 31 2027)
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05",
    "2027-09-06", "2027-11-25", "2027-12-24",
))

# 13:00 ET early closes: Jul 3 when it is a regular weekday session before a
# weekday Jul 4, the day after Thanksgiving, Christmas Eve on a weekday. No
# early close on Jul 2 2026 (Jul 3 is the observed holiday) nor in July 2027
# (Jul 3 is a Saturday) — re-check against the official calendar.
NYSE_EARLY_CLOSES = frozenset(date.fromisoformat(d) for d in (
    "2016-11-25",
    "2017-07-03", "2017-11-24",
    "2018-07-03", "2018-11-23", "2018-12-24",
    "2019-07-03", "2019-11-29", "2019-12-24",
    "2020-11-27", "2020-12-24",
    "2021-11-26",
    "2022-11-25",
    "2023-07-03", "2023-11-24",
    "2024-07-03", "2024-11-29", "2024-12-24",
    "2025-07-03", "2025-11-28", "2025-12-24",
    "2026-11-27", "2026-12-24",
    "2027-11-26",
))


# --- 2000-2015: rule-based (user approval 2026-10-03: backtests from 2000) ------------------------
# Rules and special dates as in the exchange_calendars XNYS calendar (Gerry Manoim et al.,
# https://github.com/gerrymanoim/exchange_calendars — exchange_calendar_xnys.py / us_holidays.py,
# checked 2026-10-03), which follows NYSE's published history:
# - New Year's Day (Sunday -> Monday; a Saturday Jan 1 is NOT moved to Friday), MLK Day (3rd
#   Monday of January, since 1998), Washington's Birthday (3rd Monday of February), Good Friday,
#   Memorial Day (last Monday of May), Independence Day and Christmas (nearest weekday:
#   Saturday -> Friday, Sunday -> Monday), Labor Day (1st Monday of September), Thanksgiving
#   (4th Thursday of November);
# - special full-day closures: 2001-09-11..14 (September 11), 2004-06-11 (Reagan, national day
#   of mourning), 2007-01-02 (Ford, national day of mourning), 2012-10-29/30 (Hurricane Sandy);
# - 13:00 early closes: Jul 3 on a Monday/Tuesday/Thursday (Wednesday only from 2013), Friday
#   Jul 5 before 2013, the day after Thanksgiving, Christmas Eve Monday-Thursday, and 2003-12-26.
# Checked against the official yearly session counts (2001: 248, 2008: 253, 2012: 250).
RULE_YEARS = tuple(range(2000, 2016))
SPECIAL_CLOSURES_2000_2015 = ("2001-09-11", "2001-09-12", "2001-09-13", "2001-09-14", "2004-06-11",
                              "2007-01-02", "2012-10-29", "2012-10-30")
SPECIAL_EARLY_CLOSES_2000_2015 = ("2003-12-26",)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous Gregorian algorithm, Meeus/Jones/Butcher)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    day = (h + l_ - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _nearest_weekday(d: date) -> date:
    return d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d


def rule_holidays(year: int) -> List[date]:
    """Regular NYSE holidays of ``year`` by the historical rules above (no special closures)."""
    out = []
    ny = date(year, 1, 1)
    if ny.weekday() == 6:
        out.append(ny + timedelta(days=1))
    elif ny.weekday() < 5:
        out.append(ny)
    if year >= 1998:
        out.append(_nth_weekday(year, 1, 0, 3))
    out.append(_nth_weekday(year, 2, 0, 3))
    out.append(_easter(year) - timedelta(days=2))
    out.append(_last_weekday(year, 5, 0))
    out.append(_nearest_weekday(date(year, 7, 4)))
    out.append(_nth_weekday(year, 9, 0, 1))
    out.append(_nth_weekday(year, 11, 3, 4))
    out.append(_nearest_weekday(date(year, 12, 25)))
    return sorted(out)


def rule_early_closes(year: int, holidays) -> List[date]:
    """13:00 early closes of ``year`` (2000-2015 rule set above)."""
    out = []
    j3 = date(year, 7, 3)
    if j3.weekday() in (0, 1, 3) or (j3.weekday() == 2 and year >= 2013):
        out.append(j3)
    j5 = date(year, 7, 5)
    if j5.weekday() == 4 and year < 2013:
        out.append(j5)
    out.append(_nth_weekday(year, 11, 3, 4) + timedelta(days=1))
    x = date(year, 12, 24)
    if x.weekday() < 4:
        out.append(x)
    return sorted(d for d in out if d not in holidays)


_RULE_HOLIDAYS = frozenset(d for y in RULE_YEARS for d in rule_holidays(y)) | \
    frozenset(date.fromisoformat(d) for d in SPECIAL_CLOSURES_2000_2015)
_RULE_EARLY = frozenset(d for y in RULE_YEARS for d in rule_early_closes(y, _RULE_HOLIDAYS)) | \
    frozenset(date.fromisoformat(d) for d in SPECIAL_EARLY_CLOSES_2000_2015)
NYSE_HOLIDAYS = NYSE_HOLIDAYS | _RULE_HOLIDAYS
NYSE_EARLY_CLOSES = NYSE_EARLY_CLOSES | (_RULE_EARLY - _RULE_HOLIDAYS)

# --- 2028-2030: the current NYSE rules (stage 10, S10-H, 2026-10-24) --------------------------------------------
# Source: NYSE holiday & trading-hours rules (nyse.com/markets/hours-calendars; the published calendar covers 2026–2028,
# checked 2026-10-24) — the same rules the hardcoded 2022–2027 rows follow (``tests/test_sp_calendars`` proves that):
# - full-day closures: New Year's Day (Sunday -> Monday; on a Saturday NO closure — the NYSE does not close the last
#   trading day of the year), Martin Luther King Jr. Day (3rd Monday of January), Washington's Birthday (3rd Monday of
#   February), Good Friday, Memorial Day (last Monday of May), Juneteenth (June 19, since 2022), Independence Day,
#   Labor Day (1st Monday of September), Thanksgiving (4th Thursday of November), Christmas; June 19, July 4 and
#   December 25 on a Saturday close the Friday before, on a Sunday the Monday after;
# - 13:00 early closes: July 3 Monday–Thursday (the session before a weekday July 4), the day after Thanksgiving,
#   December 24 Monday–Thursday.
# 2028 equals the published NYSE calendar. 2029–2030 are these rules projected: an unforeseeable special closure (a
# national day of mourning, a weather closure) cannot be known in advance — add it here when the NYSE announces one.
PROJECTED_YEARS = (2028, 2029, 2030)


def modern_holidays(year: int) -> List[date]:
    """Regular NYSE full-day closures of ``year`` by the current rules (Juneteenth from 2022)."""
    out = []
    ny = date(year, 1, 1)
    if ny.weekday() == 6:
        out.append(ny + timedelta(days=1))
    elif ny.weekday() < 5:
        out.append(ny)
    out.append(_nth_weekday(year, 1, 0, 3))
    out.append(_nth_weekday(year, 2, 0, 3))
    out.append(_easter(year) - timedelta(days=2))
    out.append(_last_weekday(year, 5, 0))
    if year >= 2022:
        out.append(_nearest_weekday(date(year, 6, 19)))
    out.append(_nearest_weekday(date(year, 7, 4)))
    out.append(_nth_weekday(year, 9, 0, 1))
    out.append(_nth_weekday(year, 11, 3, 4))
    out.append(_nearest_weekday(date(year, 12, 25)))
    return sorted(out)


def modern_early_closes(year: int) -> List[date]:
    """13:00 early closes of ``year`` by the current rules."""
    out = []
    j3 = date(year, 7, 3)
    if j3.weekday() < 4:
        out.append(j3)
    out.append(_nth_weekday(year, 11, 3, 4) + timedelta(days=1))
    x = date(year, 12, 24)
    if x.weekday() < 4:
        out.append(x)
    return sorted(out)


NYSE_HOLIDAYS = NYSE_HOLIDAYS | frozenset(d for y in PROJECTED_YEARS for d in modern_holidays(y))
NYSE_EARLY_CLOSES = NYSE_EARLY_CLOSES | frozenset(d for y in PROJECTED_YEARS for d in modern_early_closes(y))
CALENDAR_YEARS = tuple(range(2000, 2031))


# --- conversions -------------------------------------------------------------

def _to_utc(now) -> datetime:
    """None (= current time), datetime (naive = UTC) or ISO string -> aware UTC datetime."""
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, str):
        now = datetime.fromisoformat(now.strip().replace("Z", "+00:00"))
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _as_date(value) -> date:
    """date | datetime (-> NY date) | 'YYYY-MM-DD' / ISO datetime string."""
    if isinstance(value, datetime):
        return _to_utc(value).astimezone(NY).date()
    if isinstance(value, date):
        return value
    text = str(value)
    if len(text) > 10:
        return _to_utc(datetime.fromisoformat(text.replace("Z", "+00:00"))).astimezone(NY).date()
    return date.fromisoformat(text)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def ny_now(now: Optional[datetime] = None) -> datetime:
    return _to_utc(now).astimezone(NY)


def ny_date(now: Optional[datetime] = None) -> date:
    """The America/New_York calendar date of ``now`` (UTC default)."""
    return ny_now(now).date()


# --- NYSE sessions -------------------------------------------------------------

def is_covered(d) -> bool:
    """True when the NY date lies in CALENDAR_YEARS (holidays known)."""
    return _as_date(d).year in CALENDAR_YEARS


def is_trading_day(d) -> bool:
    """Mon–Fri and not an NYSE holiday. Raises CalendarUnavailable outside
    CALENDAR_YEARS (also for weekends there: the caller asked about a year the
    calendar does not cover)."""
    d = _as_date(d)
    if d.year not in CALENDAR_YEARS:
        raise CalendarUnavailable(d.year)
    if d.weekday() >= 5:
        return False
    return d not in NYSE_HOLIDAYS


def is_early_close(d) -> bool:
    d = _as_date(d)
    return is_trading_day(d) and d in NYSE_EARLY_CLOSES


def session_bounds(d) -> Optional[Tuple[datetime, datetime]]:
    """(open, close) as UTC datetimes of the regular session on NY date d,
    or None when d is not a trading day."""
    d = _as_date(d)
    if not is_trading_day(d):
        return None
    close = EARLY_CLOSE if d in NYSE_EARLY_CLOSES else REGULAR_CLOSE
    o = datetime.combine(d, REGULAR_OPEN, tzinfo=NY).astimezone(timezone.utc)
    c = datetime.combine(d, close, tzinfo=NY).astimezone(timezone.utc)
    return o, c


def next_trading_day(d) -> date:
    """First trading day strictly after d."""
    d = _as_date(d) + timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def previous_trading_day(d) -> date:
    """Last trading day strictly before d."""
    d = _as_date(d) - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def session_state(now: Optional[datetime] = None) -> dict:
    """{"open": bool, "session": "regular"|"closed", "next_open": iso,
    "next_close": iso|None, "trading_day": "YYYY-MM-DD"}.

    While open: next_close = today's close, next_open = the next session's
    open, trading_day = today. While closed: next_close = None, next_open =
    the next session's open, trading_day = that session's date (the day that
    orders queued now will execute on). US equity session only."""
    now = _to_utc(now)
    today = now.astimezone(NY).date()
    bounds = session_bounds(today)
    if bounds and bounds[0] <= now < bounds[1]:
        nxt = session_bounds(next_trading_day(today))
        return {"open": True, "session": "regular", "next_open": _iso(nxt[0]), "next_close": _iso(bounds[1]),
                "trading_day": today.isoformat()}
    if bounds and now < bounds[0]:
        day = today
    else:
        day = next_trading_day(today)
    o, _c = session_bounds(day)
    return {"open": False, "session": "closed", "next_open": _iso(o), "next_close": None,
            "trading_day": day.isoformat()}


def is_open(now: Optional[datetime] = None) -> bool:
    return session_state(now)["open"]


def trading_day(now: Optional[datetime] = None) -> str:
    """'YYYY-MM-DD' of the current session, or of the next one while closed."""
    return session_state(now)["trading_day"]


def trading_days_between(a, b) -> int:
    """Number of trading days d with a < d <= b (NY dates; datetimes are
    converted). Opened Monday, asked Tuesday -> 1; same day -> 0; b before
    a -> 0. Used for min_hold_days."""
    a, b = _as_date(a), _as_date(b)
    if b <= a:
        return 0
    n, d = 0, a + timedelta(days=1)
    while d <= b:
        if is_trading_day(d):
            n += 1
        d += timedelta(days=1)
    return n


def trading_days(start, end) -> List[date]:
    """All NYSE trading days d with start <= d <= end (inclusive), oldest first
    — e.g. the expected daily bars for gap validation."""
    a, b = _as_date(start), _as_date(end)
    out: List[date] = []
    d = a
    while d <= b:
        if is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def last_close_before(now: Optional[datetime] = None) -> datetime:
    """UTC datetime of the most recent regular-session close at or before now."""
    now = _to_utc(now)
    d = now.astimezone(NY).date()
    for _ in range(15):
        b = session_bounds(d)
        if b and b[1] <= now:
            return b[1]
        d -= timedelta(days=1)
    # unreachable with a correct calendar (never 15 days without a session);
    # no "return now" default — say what is wrong
    raise CalendarUnavailable(d.year)


# --- per symbol / crypto ---------------------------------------------------------

def is_crypto(symbol) -> bool:
    """True for platform crypto symbols ("BTC-USD"), see core.instruments."""
    return isinstance(symbol, str) and asset_class(symbol.strip().upper()) == "crypto"


def is_tradable(symbol: str, now: Optional[datetime] = None) -> bool:
    """True when market orders in ``symbol`` can execute now: crypto always
    (24/7), equities/ETFs during the NYSE regular session. (The old broker
    adds the 5-minute opening grace for equities on top.)"""
    if is_crypto(symbol):
        return True
    return bool(session_state(now)["open"])


def utc_days_between(a, b) -> int:
    """UTC calendar days from a to b (datetimes/ISO strings; dates as is);
    0 when b is not after a. Crypto counterpart of trading_days_between."""
    def _d(v):
        if isinstance(v, datetime):
            return _to_utc(v).date()
        if isinstance(v, date):
            return v
        text = str(v)
        if len(text) > 10:
            return _to_utc(datetime.fromisoformat(text.replace("Z", "+00:00"))).date()
        return date.fromisoformat(text)
    return max(0, (_d(b) - _d(a)).days)


FUNDING_HOURS_UTC = (0, 8, 16)


def last_funding_window(now: Optional[datetime] = None) -> datetime:
    """The latest perp funding timestamp (00:00/08:00/16:00 UTC) <= now."""
    now = _to_utc(now)
    h = max(x for x in FUNDING_HOURS_UTC if x <= now.hour)
    return now.replace(hour=h, minute=0, second=0, microsecond=0)


def funding_windows_between(a, b) -> List[datetime]:
    """Funding timestamps w with a < w <= b (UTC), oldest first."""
    a, b = _to_utc(a), _to_utc(b)
    out: List[datetime] = []
    if b <= a:
        return out
    w = last_funding_window(b)
    while w > a:
        out.append(w)
        w -= timedelta(hours=8)
    return out[::-1]


# Names fund/market.py re-exports (one source of truth; see SPEC §1).
__all__ = [
    "NY", "REGULAR_OPEN", "REGULAR_CLOSE", "EARLY_CLOSE", "NYSE_HOLIDAYS", "NYSE_EARLY_CLOSES",
    "CALENDAR_YEARS", "CalendarUnavailable", "_to_utc", "_as_date", "_iso", "ny_now", "ny_date",
    "is_covered", "is_trading_day", "is_early_close", "session_bounds", "next_trading_day",
    "previous_trading_day", "session_state", "is_open", "trading_day", "trading_days_between",
    "trading_days", "last_close_before", "is_crypto", "is_tradable", "utc_days_between",
    "FUNDING_HOURS_UTC", "last_funding_window", "funding_windows_between",
]
