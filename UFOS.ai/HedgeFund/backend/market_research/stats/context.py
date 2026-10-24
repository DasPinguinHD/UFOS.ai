"""Shared inputs of the header (snapshot) and key statistics (SPEC §7.4) — the "reference session".

Reference session (MR2a definition): equities — today (New York) when it is a trading day and its
pre-market (04:00 ET) has begun, else the previous trading day; crypto — the current UTC day (Q9).
``prev_close`` = the close of the session BEFORE the reference session (equities: SIP daily bar,
split-adjusted; crypto: the last price of the previous UTC day). Day values (open, range, volume, VWAP)
are those of the reference session; when it is over they are its values "at close" (``as_of`` = close).

Inputs (all through ``sources`` / the 1m loader under research priority, no fallback):
- last trade (``latest_trade``: IEX for equities, Alpaca crypto);
- the reference day's 1m bars (equities: regular session, SIP up to ``sip_through`` + IEX tail);
- daily bars for ``STATS_DAILY_LOOKBACK_DAYS`` (equities: SIP daily, adjustment ``split`` — performance
  excludes dividends; crypto: UTC days built from 1Hour bars).

Part of UFOS.ai, a proof-of-concept portfolio project; research only, not investment advice.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import List, Optional

from strategy_platform.core import calendars
from strategy_platform.core.clock import iso, to_utc
from strategy_platform.core.errors import UserFacingError

from .. import limits
from ..bars import aggregate as agg
from ..bars import intraday, series

FEED_LABELS = {"iex": "IEX · real time", "iex_tail": "IEX · live tail", "sip_delayed": "SIP · 15 min delayed",
               "sip_daily": "SIP · daily bars", "crypto": "Alpaca crypto · real time"}


def num(value, feed: str, as_of=None, reason: Optional[str] = None, label: Optional[str] = None) -> dict:
    """The Num shape of SPEC §4.1 (a missing value is null WITH the reason)."""
    if value is None and not reason:
        reason = "No value available."
    return {"value": None if value is None else float(value), "feed": feed,
            "feed_label": label or FEED_LABELS.get(feed, feed),
            "as_of": (as_of if isinstance(as_of, str) else iso(as_of)) if as_of is not None else None,
            "reason": None if value is not None else limits.clip(reason)}


@dataclass
class Context:
    symbol: str
    asset_class: str
    now: datetime
    ref_day: date
    prev_day: Optional[date]
    window: tuple                      # (start, end) UTC of the reference session (RTH / UTC day)
    sip_through: Optional[datetime]
    minutes: List[dict]                # reference session 1m (RTH for equities)
    daily: List[dict]                  # complete daily bars (output shape), oldest first, with "day"
    last: Optional[float] = None
    last_at: Optional[str] = None
    last_reason: Optional[str] = None
    notes: List[str] = field(default_factory=list)

    @property
    def last_feed(self) -> str:
        return "crypto" if self.asset_class == "crypto" else "iex"

    def daily_on(self, d: date) -> Optional[dict]:
        return next((b for b in reversed(self.daily) if b["day"] == d), None)

    def daily_on_or_before(self, d: date) -> Optional[dict]:
        return next((b for b in reversed(self.daily) if b["day"] <= d), None)


def _last_trade(sources, symbol):
    try:
        tr = sources.latest_trade(symbol)
        return float(tr["price"]), tr.get("ts"), None
    except UserFacingError as ex:
        # not a fallback: the value is null and carries the reason (SPEC §4.1); the rest stays exact
        return None, None, ex.user_message


def build(loader, sources, symbol: str, asset_class: str, now) -> Context:
    now = to_utc(now)
    if asset_class == "crypto":
        return _crypto(loader, sources, symbol, now)
    return _equity(loader, sources, symbol, now)


def equity_reference_days(now) -> tuple:
    """(reference session, the session before it) — the MR2a definition above (also MR4 watchlist quotes)."""
    today = now.astimezone(agg.NY).date()
    if calendars.is_trading_day(today) and now >= agg.session_window(today, "eth")[0]:
        ref = today
    else:
        ref = calendars.previous_trading_day(today)
    return ref, calendars.previous_trading_day(ref)


def _equity(loader, sources, symbol, now) -> Context:
    ref, prev = equity_reference_days(now)
    w = agg.session_window(ref, "rth")
    through = intraday.sip_through(now)
    minutes = []
    if now > w[0]:
        minutes = [m for m in loader.equity_minutes(symbol, w[0], min(w[1], now), now=now) if w[0] <= m["t"] < w[1]]
    start = agg.ny_midnight(ref - timedelta(days=limits.STATS_DAILY_LOOKBACK_DAYS))
    fresh = calendars.last_close_before(now - intraday.SIP_DELAY)
    rows = sources.daily(symbol, start, now, adjustment="split", fresh=fresh)
    daily = []
    for r in rows:
        b = agg.daily_bar(r, "sip_daily")
        b["day"] = to_utc(r["ts"]).astimezone(agg.NY).date()
        daily.append(b)
    last, last_at, reason = _last_trade(sources, symbol)
    return Context(symbol, "equity", now, ref, prev, w, through, minutes, daily, last, last_at, reason)


def _crypto(loader, sources, symbol, now) -> Context:
    ref = now.date()
    d0 = agg.utc_midnight(ref)
    minutes = loader.crypto_minutes(symbol, d0, now, now=now)
    start = agg.utc_midnight(ref - timedelta(days=limits.STATS_DAILY_LOOKBACK_DAYS))
    hours = series._crypto_hours(loader, sources, symbol, start, d0, now)
    daily = []
    for b in agg.aggregate(hours, "1D", "rth", "crypto", now=now):
        if b["final"]:
            b["day"] = to_utc(b["t"]).date()
            daily.append(b)
    last, last_at, reason = _last_trade(sources, symbol)
    return Context(symbol, "crypto", now, ref, ref - timedelta(days=1), (d0, d0 + timedelta(days=1)), None,
                   minutes, daily, last, last_at, reason)
