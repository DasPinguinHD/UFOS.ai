"""Session-aligned bar aggregation (SPEC §7.2) — pure functions, no I/O.

Minute dicts (input): ``{"t": aware UTC datetime (minute/bar start), "o", "h", "l", "c", "v", "vwap", "n",
"feed": "sip"|"iex_tail"|"crypto"}``, oldest first. Output bars: ``{"t": UTC ISO string (bucket start),
"o", "h", "l", "c", "v", "vwap", "n", "feed", "final"}``.

Buckets (America/New_York for equities, ``core/calendars`` sessions):
- ``rth``: 09:30 – close (16:00, 13:00 on early-close days), anchored at 09:30
  (1H: 09:30 … 15:30, the last bucket 30 min; 4H: 09:30, 13:30). The last bucket of an early-close
  day ends at 13:00 (shorter).
- ``eth``: 04:00 – 20:00 (17:00 on early-close days — SPEC §11 "verify"), anchored at 04:00
  (1H: 04:00 … 19:00; 4H: 04:00, 08:00, 12:00, 16:00).
- crypto: UTC-aligned ``floor(t, tf)`` (also ``1D`` = UTC day, Q9); ``session`` is ignored.
Minutes outside the session window are dropped; minutes without trades are absent (never filled).

Bucket values: o = first, h = max, l = min, c = last, v = Σ, vwap = Σ(vwap·v)/Σv (None if any minute
lacks vwap/volume or Σv = 0), n = Σ (None if any is None). A bucket with any ``iex_tail`` minute is
``iex_tail`` and not final; an equity bucket is final when its end ≤ ``sip_through``; a crypto bucket when
its end ≤ ``now``.

Session flag ``s`` (bridge contract §10 MR3 / MR3b notes: extended-hours shading): every INTRADAY equity
bar (1m … 4H, REST and hub) carries ``"s": "pre"|"rth"|"post"`` from the bucket START vs the regular
session of its New York day (``core/calendars``, early closes included): start < open → ``pre``, open ≤
start < close → ``rth``, start ≥ close → ``post``. An ETH bucket that spans the open/close (1H 09:00,
4H 08:00/12:00) is classified by its start. Crypto and 1D/1W bars carry no ``s``.

Weeks (``weeks``): equities Mon–Fri, labelled Monday 00:00 New York (also when Monday is a holiday);
crypto Monday 00:00 UTC. The current week is partial (``final: false``).

Part of UFOS.ai, a proof-of-concept portfolio project; research only.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from typing import Iterable, List, Optional, Tuple

from strategy_platform.core import calendars
from strategy_platform.core.clock import iso, to_utc

NY = calendars.NY
TF_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1H": 60, "4H": 240}
INTRADAY_TFS = tuple(TF_MINUTES)
TIMEFRAMES = INTRADAY_TFS + ("1D", "1W")
SESSIONS = ("rth", "eth")
ETH_OPEN = time(4, 0)
ETH_CLOSE = time(20, 0)
ETH_EARLY_CLOSE = time(17, 0)          # SPEC §11: extended-hours end on early-close days (verify)
CRYPTO_MINUTES = dict(TF_MINUTES, **{"1D": 1440})


@lru_cache(maxsize=4096)
def session_window(d: date, session: str) -> Optional[Tuple[datetime, datetime]]:
    """(start, end) UTC of the ``rth``/``eth`` window on NY date ``d``; None when d is not a trading day.
    Raises CalendarUnavailable outside the calendar's years."""
    if session not in SESSIONS:
        raise ValueError(f"session must be one of {', '.join(SESSIONS)}")
    bounds = calendars.session_bounds(d)
    if bounds is None:
        return None
    if session == "rth":
        return bounds
    end = ETH_EARLY_CLOSE if calendars.is_early_close(d) else ETH_CLOSE
    return (datetime.combine(d, ETH_OPEN, tzinfo=NY).astimezone(timezone.utc),
            datetime.combine(d, end, tzinfo=NY).astimezone(timezone.utc))


def ny_midnight(d: date) -> datetime:
    """00:00 America/New_York of ``d`` as UTC (the ``t`` of an equity daily bar, like Alpaca's)."""
    return datetime.combine(d, time(0, 0), tzinfo=NY).astimezone(timezone.utc)


def utc_midnight(d: date) -> datetime:
    return datetime.combine(d, time(0, 0), tzinfo=timezone.utc)


def bucket_start(t, tf: str, session: str, asset_class: str) -> Optional[datetime]:
    """Start (aware UTC) of the bucket containing the minute ``t``; None = outside the session window."""
    t = to_utc(t)
    if asset_class == "crypto":
        size = CRYPTO_MINUTES[tf] * 60
        epoch = int(t.timestamp())
        return datetime.fromtimestamp(epoch - epoch % size, tz=timezone.utc)
    size = TF_MINUTES[tf]
    w = session_window(t.astimezone(NY).date(), session)
    if w is None or not (w[0] <= t < w[1]):
        return None
    idx = int((t - w[0]).total_seconds() // (size * 60))
    return w[0] + timedelta(minutes=idx * size)


def session_flag(start, asset_class: str = "equity", tf: str = "1m") -> Optional[str]:
    """"pre" | "rth" | "post" for an intraday equity bar starting at ``start``; None for crypto, 1D/1W or a
    day without a session."""
    if asset_class == "crypto" or tf not in TF_MINUTES:
        return None
    start = to_utc(start)
    rth = session_window(start.astimezone(NY).date(), "rth")
    if rth is None:
        return None
    if start < rth[0]:
        return "pre"
    return "rth" if start < rth[1] else "post"


def bucket_end(start: datetime, tf: str, session: str, asset_class: str) -> datetime:
    if asset_class == "crypto":
        return start + timedelta(minutes=CRYPTO_MINUTES[tf])
    w = session_window(start.astimezone(NY).date(), session)
    end = start + timedelta(minutes=TF_MINUTES[tf])
    return min(end, w[1]) if w else end


def combine(ms: List[dict]) -> dict:
    """OHLCV of consecutive minutes (non-empty, oldest first)."""
    vols = [m.get("v") for m in ms]
    v = None if any(x is None for x in vols) else float(sum(vols))
    vwaps = [m.get("vwap") for m in ms]
    vwap = None
    if v is not None and v > 0 and all(x is not None for x in vwaps):
        vwap = sum(float(a) * float(b) for a, b in zip(vwaps, vols)) / v
    ns = [m.get("n") for m in ms]
    return {"o": float(ms[0]["o"]), "h": max(float(m["h"]) for m in ms), "l": min(float(m["l"]) for m in ms),
            "c": float(ms[-1]["c"]), "v": v, "vwap": vwap,
            "n": None if any(x is None for x in ns) else int(sum(ns))}


def aggregate(minutes: Iterable[dict], tf: str, session: str, asset_class: str, *,
              sip_through: Optional[datetime] = None, now: Optional[datetime] = None) -> List[dict]:
    """Buckets of ``tf`` (1m … 4H; crypto also 1D) from minute dicts (see module docstring)."""
    if asset_class == "crypto":
        if tf not in CRYPTO_MINUTES:
            raise ValueError(f"unknown timeframe {tf!r}")
    elif tf not in TF_MINUTES:
        raise ValueError(f"unknown timeframe {tf!r}")
    groups: List[Tuple[datetime, List[dict]]] = []
    for m in minutes:
        b = bucket_start(m["t"], tf, session, asset_class)
        if b is None:
            continue
        if groups and groups[-1][0] == b:
            groups[-1][1].append(m)
        else:
            groups.append((b, [m]))
    now = to_utc(now) if now is not None else None
    sip_through = to_utc(sip_through) if sip_through is not None else None
    out = []
    for b, ms in groups:
        bar = combine(ms)
        end = bucket_end(b, tf, session, asset_class)
        tail = any(m.get("feed") == "iex_tail" for m in ms)
        if asset_class == "crypto":
            feed, final = "crypto", now is not None and end <= now
        else:
            feed = "iex_tail" if tail else (ms[0].get("feed") or "sip")
            final = (not tail) and sip_through is not None and end <= sip_through
        bar.update({"t": iso(b), "feed": feed, "final": bool(final)})
        row = {"t": bar["t"], "o": bar["o"], "h": bar["h"], "l": bar["l"], "c": bar["c"], "v": bar["v"],
               "vwap": bar["vwap"], "n": bar["n"], "feed": bar["feed"], "final": bar["final"]}
        flag = session_flag(b, asset_class, tf)
        if flag is not None:
            row["s"] = flag
        out.append(row)
    return out


def day_bar(minutes: List[dict], t: datetime, *, feed: Optional[str] = None, final: bool = False) -> dict:
    """One daily candle from minutes (today's partial candle, model c)."""
    bar = combine(minutes)
    tail = any(m.get("feed") == "iex_tail" for m in minutes)
    return {"t": iso(t), "o": bar["o"], "h": bar["h"], "l": bar["l"], "c": bar["c"], "v": bar["v"],
            "vwap": bar["vwap"], "n": bar["n"], "feed": "iex_tail" if tail else (feed or minutes[0].get("feed")),
            "final": bool(final) and not tail}


def week_start(t, asset_class: str) -> datetime:
    t = to_utc(t)
    if asset_class == "crypto":
        d = t.date()
        return utc_midnight(d - timedelta(days=d.weekday()))
    d = t.astimezone(NY).date()
    return ny_midnight(d - timedelta(days=d.weekday()))


def weeks(day_bars: List[dict], asset_class: str, now) -> List[dict]:
    """1W bars from 1D bars (output shape above; oldest first)."""
    now = to_utc(now)
    groups: List[Tuple[datetime, List[dict]]] = []
    for b in day_bars:
        w = week_start(b["t"], asset_class)
        if groups and groups[-1][0] == w:
            groups[-1][1].append(b)
        else:
            groups.append((w, [b]))
    out = []
    for w, bs in groups:
        bar = combine(bs)
        if asset_class == "crypto":
            end = w + timedelta(days=7)
        else:
            end = ny_midnight(w.astimezone(NY).date() + timedelta(days=7))
        feeds = {b["feed"] for b in bs}
        feed = "iex_tail" if "iex_tail" in feeds else bs[-1]["feed"]
        final = all(b["final"] for b in bs) and end <= now
        out.append({"t": iso(w), "o": bar["o"], "h": bar["h"], "l": bar["l"], "c": bar["c"], "v": bar["v"],
                    "vwap": bar["vwap"], "n": bar["n"], "feed": feed, "final": bool(final)})
    return out


def minute(row: dict, feed: str) -> dict:
    """Provider / store bar row (``ts``, ``open`` …) → minute dict."""
    return {"t": to_utc(row["ts"]), "o": float(row["open"]), "h": float(row["high"]), "l": float(row["low"]),
            "c": float(row["close"]), "v": row.get("volume"), "vwap": row.get("vwap"), "n": row.get("trade_count"),
            "feed": feed}


def daily_bar(row: dict, feed: str) -> dict:
    """hedgefund.db daily bar row → output bar (final: a cached bar is complete)."""
    return {"t": iso(to_utc(row["ts"])), "o": float(row["open"]), "h": float(row["high"]), "l": float(row["low"]),
            "c": float(row["close"]), "v": row.get("volume"), "vwap": row.get("vwap"), "n": row.get("trade_count"),
            "feed": feed, "final": True}
