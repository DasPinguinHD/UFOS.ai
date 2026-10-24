"""Symbol header — GET /research/symbols/{s}/snapshot (SPEC §4.2, §7.1, §7.4).

Every number is a Num ``{value, feed, feed_label, as_of, reason}``; a missing value is ``null`` with the
reason (never a stand-in). Equities: last = last IEX trade (``iex``); prev_close = SIP daily close of the
session before the reference session (``sip_daily``, split-adjusted); change / change_pct ``computed``;
day low/high = regular-session low/high over SIP minutes + IEX tail (``computed``, "SIP + IEX live
tail"); volume = Σ SIP 1m volume of the regular session up to ``sip_through`` (``sip_delayed``). Crypto:
last crypto trade, prev close = last price of the previous UTC day, day range / volume over the UTC day
(``crypto``). Reference session: ``stats/context.py``.

Part of UFOS.ai, a proof-of-concept portfolio project; research only, not investment advice.
"""
from __future__ import annotations

from datetime import timedelta

from strategy_platform.core import calendars
from strategy_platform.core.clock import iso

from .. import market
from .context import num

DAY_RANGE_LABEL = {"equity": "SIP + IEX live tail (regular session)", "crypto": "Alpaca crypto 1m (UTC day)"}


def _minutes_end(ctx):
    if not ctx.minutes:
        return None
    return min(ctx.minutes[-1]["t"] + timedelta(minutes=1), ctx.window[1])


def no_minutes_reason(ctx) -> str:
    if ctx.asset_class == "crypto":
        return "No crypto trades in the current UTC day yet."
    if ctx.now < ctx.window[0]:
        return "The regular session has not opened yet (09:30 ET)."
    return "No trades in the regular session yet."


def snapshot(ctx, asset: dict) -> dict:
    ac = ctx.asset_class
    last = num(ctx.last, ctx.last_feed, ctx.last_at, ctx.last_reason)
    pb = ctx.daily_on(ctx.prev_day) if ctx.prev_day else None
    if ac == "crypto":
        prev = num(pb["c"] if pb else None, "crypto", iso(ctx.window[0]) if pb else None,
                   None if pb else f"No crypto bar for the UTC day {ctx.prev_day.isoformat()}.",
                   label="Alpaca crypto · previous UTC day close")
    else:
        prev = num(pb["c"] if pb else None, "sip_daily", calendars.session_bounds(ctx.prev_day)[1] if pb else None,
                   None if pb else f"No SIP daily bar for {ctx.prev_day.isoformat()}.")
    if last["value"] is not None and prev["value"] is not None and prev["value"] != 0:
        ch = last["value"] - prev["value"]
        change = num(ch, "computed", last["as_of"], label=f"{last['feed_label']} − {prev['feed_label']}")
        change_pct = num(ch / prev["value"] * 100.0, "computed", last["as_of"],
                         label=f"({last['feed_label']} − {prev['feed_label']}) / previous close")
    else:
        why = last["reason"] or prev["reason"] or "The previous close is zero."
        change = num(None, "computed", None, why, label="last − previous close")
        change_pct = num(None, "computed", None, why, label="(last − previous close) / previous close")
    ms = ctx.minutes
    end = _minutes_end(ctx)
    if ms:
        lo = num(min(m["l"] for m in ms), "computed", end, label=DAY_RANGE_LABEL[ac])
        hi = num(max(m["h"] for m in ms), "computed", end, label=DAY_RANGE_LABEL[ac])
    else:
        r = no_minutes_reason(ctx)
        lo = num(None, "computed", None, r, label=DAY_RANGE_LABEL[ac])
        hi = num(None, "computed", None, r, label=DAY_RANGE_LABEL[ac])
    if ac == "crypto":
        vols = [m["v"] for m in ms]
        if ms and all(v is not None for v in vols):
            volume = num(sum(vols), "crypto", end, label="Alpaca crypto · UTC day volume")
        else:
            volume = num(None, "crypto", None, no_minutes_reason(ctx) if not ms else "A minute bar has no volume.",
                         label="Alpaca crypto · UTC day volume")
        mkt = market.crypto(ctx.now)
    else:
        sip = [m for m in ms if m["feed"] == "sip"]
        vols = [m["v"] for m in sip]
        if sip and all(v is not None for v in vols):
            volume = num(sum(vols), "sip_delayed", min(ctx.sip_through, ctx.window[1]))
        elif sip:
            volume = num(None, "sip_delayed", None, "A SIP minute bar has no volume.")
        elif ctx.now < ctx.window[0]:
            volume = num(None, "sip_delayed", None, "The regular session has not opened yet (09:30 ET).")
        elif ctx.sip_through <= ctx.window[0]:
            volume = num(None, "sip_delayed", None,
                         "SIP volume is 15 minutes delayed — the first value is available at 09:45 ET.")
        else:
            volume = num(None, "sip_delayed", None, "No SIP trades in the regular session yet.")
        mkt = market.equities(ctx.now)
    return {"symbol": ctx.symbol, "name": asset["name"], "exchange": asset["exchange"],
            "asset_class": asset["asset_class"], "kind": asset["kind"], "last": last, "prev_close": prev,
            "change": change, "change_pct": change_pct, "day_low": lo, "day_high": hi, "volume": volume,
            "market": mkt}
