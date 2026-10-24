"""GET /research/symbols/{s}/bars (SPEC §4.2, §7.1, §7.2): windows, paging, 1D/1W.

``bars(loader, sources, symbol, asset_class, tf, session, before, limit, now)`` → the response payload
``{symbol, tf, session, bars, has_more_left, sip_through, tz}`` (bars oldest first, ``t`` = bar start UTC
ISO, ``before`` exclusive).

Windows (``limit`` = number of bars; default = the initial window of plan §3.4):
- equity 1m/5m/15m: from the 1m store (``intraday.MinuteLoader``), sessions counted back from ``before``
  (or now); never older than the retention day (``INTRADAY_LOOKBACK_DAYS``) — a ``before`` older than
  that → ``BadParam`` "Intraday history older than 30 days is not loaded in Research."; reaching it →
  ``has_more_left: false``.
- equity 1H/4H: same, but sessions older than the retention day are fetched as SIP 1m per request (not
  stored), at most ``MAX_TRANSIENT_SESSIONS`` sessions per request (paging continues with ``before``).
- equity 1D: SIP daily bars (hedgefund.db cache, ``equity_bars_history``, adjustment ``all``, fresh through
  the last close ≥ 15 min ago) + today's partial candle from today's 1m (model c, ``final: false``).
  1W from 1D (Mon–Fri, Monday 00:00 NY label).
- crypto 1m/5m/15m: crypto 1m store, UTC buckets, retention as above. Crypto 1H/4H/1D/1W: completed
  1Hour bars from the hedgefund.db cache + 1m for the part after the newest cached hour (UTC
  alignment, Q9), ≤ ``CRYPTO_HOURLY_MAX_DAYS`` per request.
- ``has_more_left`` is false at ``HISTORY_START`` (2016-01-01), at the intraday retention bound, and when
  a crypto window returned no bars at all (before the coin's history).

Part of UFOS.ai, a proof-of-concept portfolio project; research only.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from strategy_platform.core import calendars
from strategy_platform.core.clock import iso, to_utc
from strategy_platform.core.errors import InvalidRequest

from .. import limits
from . import aggregate as agg
from . import intraday

HISTORY_START = date(2016, 1, 1)                  # = providers.alpaca.HISTORY_START
HISTORY_START_UTC = datetime(2016, 1, 1, tzinfo=timezone.utc)
OLD_INTRADAY = "Intraday history older than 30 days is not loaded in Research."
EQUITY_DEFAULT_SESSIONS = {"1m": 2, "5m": 5, "15m": 10, "1H": 22, "4H": 126, "1D": 504}
CRYPTO_DEFAULT_DAYS = {"1m": 2, "5m": 5, "15m": 10, "1H": 22, "4H": 183, "1D": 730}
SESSION_MINUTES = {"rth": 390, "eth": 960}
STORED_TFS = ("1m", "5m", "15m")


class BadParam(InvalidRequest):
    """400 with ``errors = {field: message}``."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field


def max_limit(tf: str) -> int:
    return limits.MAX_BARS_DAILY if tf in ("1D", "1W") else limits.MAX_BARS_INTRADAY


def buckets_per_session(tf: str, session: str) -> int:
    return math.ceil(SESSION_MINUTES[session] / agg.TF_MINUTES[tf])


def default_limit(tf: str, session: Optional[str], asset_class: str, now) -> int:
    now = to_utc(now)
    if tf == "1W":
        n = (now.date() - date(2016, 1, 4)).days // 7 + 1
    elif asset_class == "crypto":
        days = CRYPTO_DEFAULT_DAYS[tf]
        n = days if tf == "1D" else days * 1440 // agg.TF_MINUTES[tf]
    elif tf == "1D":
        n = EQUITY_DEFAULT_SESSIONS["1D"]
    else:
        n = EQUITY_DEFAULT_SESSIONS[tf] * buckets_per_session(tf, session)
    return max(1, min(n, max_limit(tf)))


def _trading_days_back(end_day: date, n: int, floor: date) -> List[date]:
    """Up to ``n`` trading days ≤ end_day, newest first, not before ``floor``."""
    out, d = [], end_day
    while len(out) < n and d >= floor:
        if calendars.is_trading_day(d):
            out.append(d)
        d -= timedelta(days=1)
    return out


def _result(symbol, tf, session, bars, has_more, sip_through, tz) -> dict:
    return {"symbol": symbol, "tf": tf, "session": session, "bars": bars, "has_more_left": bool(has_more),
            "sip_through": iso(sip_through) if sip_through is not None else None, "tz": tz}


def _cut(bars: List[dict], end: datetime, limit: int):
    e = iso(end)
    bars = [b for b in bars if b["t"] < e]
    return bars[-limit:], len(bars) > limit


# --- equities ------------------------------------------------------------------------------------

def _equity_intraday(loader, symbol, tf, session, end, limit, now, explicit_before):
    through = intraday.sip_through(now)
    keep_from = intraday.retention_day(now, "equity")
    end_day = (end - timedelta(minutes=1)).astimezone(agg.NY).date()
    if tf in STORED_TFS:
        if explicit_before and end_day < keep_from:
            raise BadParam("before", OLD_INTRADAY)
        floor = keep_from
    else:
        floor = HISTORY_START
    need = math.ceil(limit / buckets_per_session(tf, session)) + 1
    days = _trading_days_back(end_day, need, floor)
    if tf not in STORED_TFS:
        old = [d for d in days if d < keep_from]
        if len(old) > limits.MAX_TRANSIENT_SESSIONS:
            days = days[:len(days) - (len(old) - limits.MAX_TRANSIENT_SESSIONS)]
    if not days:
        return [], False
    first = days[-1]
    start = agg.session_window(first, session)[0]
    minutes = loader.equity_minutes(symbol, start, end, now=now, with_tail=end > through)
    bars = agg.aggregate(minutes, tf, session, "equity", sip_through=through, now=now)
    bars, more = _cut(bars, end, limit)
    # older sessions exist unless the floor was reached
    prev_exists = bool(_trading_days_back(first - timedelta(days=1), 1, floor))
    return bars, more or prev_exists


def _equity_daily_rows(sources, symbol, start, end, now, explicit_before):
    fresh = None if explicit_before else calendars.last_close_before(now - intraday.SIP_DELAY)
    if end <= start:
        return []
    return sources.daily(symbol, start, end, adjustment="all", fresh=fresh)


def _equity_days(loader, sources, symbol, session, end, n_sessions, now, explicit_before):
    """(1D bars oldest first, start_day)."""
    end_day = (end - timedelta(seconds=1)).astimezone(agg.NY).date()
    days = _trading_days_back(end_day, n_sessions, HISTORY_START)
    if not days:
        return [], HISTORY_START
    first = days[-1]
    rows = _equity_daily_rows(sources, symbol, agg.ny_midnight(first), end, now, explicit_before)
    bars = [agg.daily_bar(r, "sip_daily") for r in rows if to_utc(r["ts"]) < end]
    if not explicit_before:
        today = now.astimezone(agg.NY).date()
        t_today = iso(agg.ny_midnight(today))
        if calendars.is_trading_day(today) and not any(b["t"] == t_today for b in bars):
            w = agg.session_window(today, session)
            if w and now > w[0]:
                ms = [m for m in loader.equity_minutes(symbol, w[0], min(w[1], now), now=now)
                      if w[0] <= m["t"] < w[1]]
                if ms:
                    bars.append(agg.day_bar(ms, agg.ny_midnight(today), feed="sip", final=False))
    return bars, first


# --- crypto --------------------------------------------------------------------------------------

def _crypto_hours(loader, sources, symbol, start, end, now):
    """Hour bars (minute-dict shape) start ≤ t < end: cached 1Hour + 1m after the newest cached hour."""
    keep = agg.utc_midnight(intraday.retention_day(now, "crypto"))
    out = []
    cut = start
    hour_now = now.replace(minute=0, second=0, microsecond=0)
    cache_end = min(end, hour_now)
    if cache_end > start:
        rows = sources.crypto_hourly(symbol, start, cache_end - timedelta(seconds=1))
        for r in rows:
            m = agg.minute(r, "crypto")
            if start <= m["t"] < cache_end:
                out.append(m)
        if out:
            cut = out[-1]["t"] + timedelta(hours=1)
    if cut < end:
        frm = max(cut, keep)
        if frm < end:
            out.extend(loader.crypto_minutes(symbol, frm, end, now=now))
    out.sort(key=lambda m: m["t"])
    return out


def _crypto(loader, sources, symbol, tf, end, limit, now, explicit_before):
    keep = agg.utc_midnight(intraday.retention_day(now, "crypto"))
    if tf in STORED_TFS:
        if explicit_before and end <= keep:
            raise BadParam("before", OLD_INTRADAY)
        size = agg.TF_MINUTES[tf]
        start = max(keep, agg.bucket_start(end - timedelta(minutes=1), tf, "rth", "crypto")
                    - timedelta(minutes=size * limit))
        minutes = loader.crypto_minutes(symbol, start, end, now=now)
        bars = agg.aggregate(minutes, tf, "rth", "crypto", now=now)
        bars, more = _cut(bars, end, limit)
        return bars, more or start > keep
    span_days = {"1H": limit / 24.0, "4H": limit / 6.0, "1D": float(limit), "1W": limit * 7.0}[tf] + 1
    capped = span_days > limits.CRYPTO_HOURLY_MAX_DAYS
    span_days = min(span_days, limits.CRYPTO_HOURLY_MAX_DAYS)
    unit = "1D" if tf == "1W" else tf
    start = agg.bucket_start(end - timedelta(minutes=1), unit, "rth", "crypto") - timedelta(days=span_days)
    if tf == "1W":
        start = agg.week_start(start, "crypto")
    else:
        start = agg.bucket_start(start, unit, "rth", "crypto")
    start = max(start, HISTORY_START_UTC)
    hours = _crypto_hours(loader, sources, symbol, start, end, now)
    if tf == "1W":
        bars = agg.weeks(agg.aggregate(hours, "1D", "rth", "crypto", now=now), "crypto", now)
    else:
        bars = agg.aggregate(hours, unit, "rth", "crypto", now=now)
    bars, more = _cut(bars, end, limit)
    has_more = (more or capped or start > HISTORY_START_UTC) and bool(hours)
    return bars, has_more


# --- entry point ---------------------------------------------------------------------------------

def bars(loader, sources, symbol: str, asset_class: str, tf: str, session: Optional[str], before, limit: Optional[int],
         now) -> dict:
    now = to_utc(now)
    explicit_before = before is not None
    end = min(to_utc(before), now) if explicit_before else now
    if asset_class == "crypto":
        if limit is None:
            limit = default_limit(tf, None, "crypto", now)
        if end <= HISTORY_START_UTC:
            return _result(symbol, tf, "utc", [], False, None, "UTC")
        b, more = _crypto(loader, sources, symbol, tf, end, limit, now, explicit_before)
        return _result(symbol, tf, "utc", b, more, None, "UTC")
    session = session or "rth"
    if limit is None:
        limit = default_limit(tf, session, "equity", now)
    through = intraday.sip_through(now)
    if end <= HISTORY_START_UTC:
        return _result(symbol, tf, session, [], False, through, "America/New_York")
    if tf in agg.TF_MINUTES:
        b, more = _equity_intraday(loader, symbol, tf, session, end, limit, now, explicit_before)
    elif tf == "1D":
        b, first = _equity_days(loader, sources, symbol, session, end, limit, now, explicit_before)
        b, more = _cut(b, end, limit)
        more = more or first > HISTORY_START and bool(_trading_days_back(first - timedelta(days=1), 1, HISTORY_START))
    else:   # 1W
        end_day = (end - timedelta(seconds=1)).astimezone(agg.NY).date()
        monday = end_day - timedelta(days=end_day.weekday()) - timedelta(weeks=limit - 1)
        n_sessions = len(calendars.trading_days(max(monday, HISTORY_START), end_day))
        days, first = _equity_days(loader, sources, symbol, session, end, max(1, n_sessions), now, explicit_before)
        b, more = _cut(agg.weeks(days, "equity", now), end, limit)
        more = more or first > HISTORY_START and bool(_trading_days_back(first - timedelta(days=1), 1, HISTORY_START))
    return _result(symbol, tf, session, b, more, through, "America/New_York")
