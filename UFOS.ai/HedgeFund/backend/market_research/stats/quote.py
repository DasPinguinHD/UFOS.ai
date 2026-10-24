"""Watchlist quotes (SPEC §13 "MR4 backend notes") — live values from the hub, previous close from SIP daily.

- ``last`` comes ONLY from the market-data hub (the latest stream trade since the symbol went live); never a
  REST snapshot. Beyond the 30-symbol stock stream limit every value is ``null`` with the limit text and
  nothing is fetched for that symbol (Q10).
- ``prev_close`` = the header's definition (§7.4, ``context.equity_reference_days``): equities the SIP daily
  close (split-adjusted) of the session before the reference session, crypto the previous UTC day's close.
  Read through the hedgefund.db bar cache / gate (the caller runs ``prev_closes`` on the Research pool inside
  research priority) and kept in memory for ``PREV_CLOSE_CACHE_SECONDS``. Lookups stop after
  ``QUOTE_LOOKUP_BUDGET_SECONDS``: the rest are ``null`` with "still loading" and the answer is incomplete.

Part of UFOS.ai, a proof-of-concept portfolio project; research only, not investment advice.
"""
from __future__ import annotations

import threading
import time as _time
from datetime import timedelta
from typing import Callable, Dict, List, Optional, Tuple

from strategy_platform.core import calendars
from strategy_platform.core.clock import iso, to_utc
from strategy_platform.core.errors import UserFacingError

from .. import limits
from ..bars import aggregate as agg
from ..bars import intraday, series
from .context import equity_reference_days, num

LOADING_REASON = "Previous close is still loading — ask again."
NO_TRADE_REASON = "No trade on the live stream yet — the price appears with the next trade."
CRYPTO_PREV_LABEL = "Alpaca crypto · previous UTC day close"
NOTE = ("Live quotes come only from the Alpaca streams while this watchlist is shown. Symbols beyond the "
        f"{limits.STOCK_STREAM_SYMBOL_LIMIT}-symbol stock stream limit are not live — no delayed or snapshot value "
        "is shown for them.")


class PrevCloseCache:
    """{(symbol, day): (Num, stored at)} — thread-safe (Research pool), bounded."""

    def __init__(self, monotonic: Callable[[], float] = _time.monotonic):
        self._mono = monotonic
        self._lock = threading.Lock()
        self._d: Dict[Tuple[str, str], Tuple[dict, float]] = {}

    def get(self, key) -> Optional[dict]:
        with self._lock:
            hit = self._d.get(key)
            if hit is None or self._mono() - hit[1] > limits.PREV_CLOSE_CACHE_SECONDS:
                return None
            return dict(hit[0])

    def put(self, key, value: dict) -> None:
        with self._lock:
            if len(self._d) >= limits.PREV_CLOSE_CACHE_MAX:
                for k in sorted(self._d, key=lambda k: self._d[k][1])[: len(self._d) // 2]:
                    del self._d[k]
            self._d[key] = (dict(value), self._mono())


def prev_day(asset_class: str, now):
    now = to_utc(now)
    if asset_class == "crypto":
        return now.date() - timedelta(days=1)
    return equity_reference_days(now)[1]


def prev_close(loader, sources, symbol: str, asset_class: str, now) -> dict:
    """The Num of the previous close (raises the source's UserFacingError)."""
    now = to_utc(now)
    if asset_class == "crypto":
        ref = now.date()
        d0, p0 = agg.utc_midnight(ref), agg.utc_midnight(ref - timedelta(days=1))
        hours = series._crypto_hours(loader, sources, symbol, p0, d0, now)
        day = next((b for b in agg.aggregate(hours, "1D", "rth", "crypto", now=now)
                    if b["final"] and to_utc(b["t"]) == p0), None)
        return num(day["c"] if day else None, "crypto", iso(d0) if day else None,
                   None if day else f"No crypto bar for the UTC day {(ref - timedelta(days=1)).isoformat()}.",
                   label=CRYPTO_PREV_LABEL)
    _ref, prev = equity_reference_days(now)
    start = agg.ny_midnight(prev - timedelta(days=10))
    fresh = calendars.last_close_before(now - intraday.SIP_DELAY)
    rows = sources.daily(symbol, start, now, adjustment="split", fresh=fresh)
    pb = None
    for r in rows:
        if to_utc(r["ts"]).astimezone(agg.NY).date() == prev:
            pb = agg.daily_bar(r, "sip_daily")
    return num(pb["c"] if pb else None, "sip_daily", calendars.session_bounds(prev)[1] if pb else None,
               None if pb else f"No SIP daily bar for {prev.isoformat()}.")


def prev_closes(wanted: List[Tuple[str, str]], loader, sources, now, cache: PrevCloseCache,
                monotonic: Callable[[], float] = _time.monotonic,
                budget: float = limits.QUOTE_LOOKUP_BUDGET_SECONDS) -> Tuple[Dict[str, dict], bool]:
    """BLOCKING. ``wanted`` = [(symbol, asset_class)] in list order -> ({symbol: Num}, complete)."""
    out: Dict[str, dict] = {}
    complete = True
    t0 = monotonic()
    for sym, ac in wanted:
        try:
            key = (sym, prev_day(ac, now).isoformat())
        except UserFacingError as ex:           # calendar year not covered
            out[sym] = num(None, "crypto" if ac == "crypto" else "sip_daily", None, ex.user_message,
                           label=CRYPTO_PREV_LABEL if ac == "crypto" else None)
            continue
        hit = cache.get(key)
        if hit is not None:
            out[sym] = hit
            continue
        if monotonic() - t0 >= budget:
            complete = False
            out[sym] = num(None, "crypto" if ac == "crypto" else "sip_daily", None, LOADING_REASON,
                           label=CRYPTO_PREV_LABEL if ac == "crypto" else None)
            continue
        try:
            value = prev_close(loader, sources, sym, ac, now)
        except UserFacingError as ex:           # this row only (missing keys, source down, …): null + reason
            out[sym] = num(None, "crypto" if ac == "crypto" else "sip_daily", None, ex.user_message,
                           label=CRYPTO_PREV_LABEL if ac == "crypto" else None)
            continue
        if value["value"] is not None:
            cache.put(key, value)
        out[sym] = value
    return out, complete


def _change(last: dict, prev: dict, reason: Optional[str]):
    if last["value"] is not None and prev["value"] is not None and prev["value"] != 0:
        ch = last["value"] - prev["value"]
        return (num(ch, "computed", last["as_of"], label=f"{last['feed_label']} − {prev['feed_label']}"),
                num(ch / prev["value"] * 100.0, "computed", last["as_of"],
                    label=f"({last['feed_label']} − {prev['feed_label']}) / previous close"))
    why = reason or last["reason"] or prev["reason"] or "The previous close is zero."
    return (num(None, "computed", None, why, label="last − previous close"),
            num(None, "computed", None, why, label="(last − previous close) / previous close"))


def item(it: dict, state: dict, prev: Optional[dict]) -> dict:
    """One quote row. ``state`` = hub.quote_state(symbol) or the not-covered state; ``prev`` None = not fetched."""
    sym, ac = it["symbol"], it["asset_class"]
    feed = "crypto" if ac == "crypto" else "iex"
    blocked = state["reason"] if (state["limited"] or not it["covered"]) else None
    tr = state.get("last")
    if blocked:
        last = num(None, feed, None, blocked)
    elif not state["live"]:
        last = num(None, feed, None, state["reason"])
    elif tr is None:
        last = num(None, feed, None, NO_TRADE_REASON)
    else:
        last = num(tr["price"], feed, tr["ts"])
    if blocked or prev is None:
        prev = num(None, "crypto" if ac == "crypto" else "sip_daily", None, blocked or LOADING_REASON,
                   label=CRYPTO_PREV_LABEL if ac == "crypto" else None)
    change, change_pct = _change(last, prev, blocked)
    return {"symbol": sym, "name": it["name"], "asset_class": ac, "kind": it["kind"], "covered": it["covered"],
            "live": bool(state["live"]) and not blocked, "reason": state["reason"],
            "last": last, "prev_close": prev, "change": change, "change_pct": change_pct}
