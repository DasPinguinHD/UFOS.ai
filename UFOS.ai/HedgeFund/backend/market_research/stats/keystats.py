"""Key statistics — GET /research/symbols/{s}/stats (SPEC §4.2, §7.4 = plan §5.4, binding).

- open: the first regular-session minute's open of the reference session (``sip_delayed``; ``iex_tail``
  while that minute is still in the IEX tail, i.e. before 09:45 ET); crypto: first minute of the UTC day.
- vwap: Σ(vwap·v)/Σv over the reference session's regular-session 1m bars (SIP + tail; ``computed``);
  null when any minute lacks vwap/volume.
- avg_volume_30d: mean SIP daily volume of the last 30 completed sessions (``sip_daily``; crypto: the last
  30 complete UTC days, ``crypto``); fewer than 30 → null with the reason.
- performance 1W/1M/3M/6M/1Y: last vs the close of the last session on or before (reference day − 7 days /
  1 / 3 / 6 / 12 months); YTD: last vs the last close of the previous year. Split-adjusted daily bars,
  dividends excluded (note). ``computed``.
- range_52w: min low / max high of the complete daily bars of the last 52 weeks (``sip_daily``).

Part of UFOS.ai, a proof-of-concept portfolio project; research only, not investment advice.
"""
from __future__ import annotations

import calendar as _cal
from datetime import date, timedelta

from strategy_platform.core.clock import iso

from ..bars import aggregate as agg
from .context import num
from .snapshot import _minutes_end, no_minutes_reason

NOTES = ["Performance excludes dividends."]
WINDOWS = (("1W", None, 7), ("1M", 1, None), ("3M", 3, None), ("6M", 6, None), ("YTD", None, None),
           ("1Y", 12, None))
AVG_SESSIONS = 30


def months_back(d: date, months: int) -> date:
    y, m = divmod(d.year * 12 + (d.month - 1) - months, 12)
    m += 1
    return date(y, m, min(d.day, _cal.monthrange(y, m)[1]))


def _daily_feed(ctx) -> str:
    return "crypto" if ctx.asset_class == "crypto" else "sip_daily"


def stats(ctx, asset: dict) -> dict:
    ac = ctx.asset_class
    ms = ctx.minutes
    # open
    if ms:
        first = ms[0]
        feed = "crypto" if ac == "crypto" else ("sip_delayed" if first["feed"] == "sip" else "iex_tail")
        open_ = num(first["o"], feed, first["t"])
    else:
        open_ = num(None, "crypto" if ac == "crypto" else "sip_delayed", None, no_minutes_reason(ctx))
    # vwap
    vlabel = "Alpaca crypto 1m (UTC day)" if ac == "crypto" else "SIP 1m + IEX live tail (regular session)"
    if ms:
        c = agg.combine(ms)
        vwap = num(c["vwap"], "computed", _minutes_end(ctx),
                   None if c["vwap"] is not None else "A minute bar has no VWAP or volume.", label=vlabel)
    else:
        vwap = num(None, "computed", None, no_minutes_reason(ctx), label=vlabel)
    # average volume
    dfeed = _daily_feed(ctx)
    complete = [b for b in ctx.daily if b["final"]]
    lastn = complete[-AVG_SESSIONS:]
    unit = "UTC days" if ac == "crypto" else "sessions"
    alabel = ("Alpaca crypto · daily volume (UTC days)" if ac == "crypto" else None)
    if len(lastn) < AVG_SESSIONS:
        avg = num(None, dfeed, None, f"Only {len(lastn)} completed {unit} are available (30 needed).", label=alabel)
    elif any(b["v"] is None for b in lastn):
        avg = num(None, dfeed, None, "A daily bar has no volume.", label=alabel)
    else:
        avg = num(sum(b["v"] for b in lastn) / len(lastn), dfeed, lastn[-1]["t"], label=alabel)
    # performance
    plabel = ("Alpaca crypto last trade vs daily close (UTC days)" if ac == "crypto"
              else "IEX last trade vs SIP daily close (split-adjusted)")
    perf = {}
    for key, months, days in WINDOWS:
        if key == "YTD":
            target = date(ctx.ref_day.year - 1, 12, 31)
        elif days is not None:
            target = ctx.ref_day - timedelta(days=days)
        else:
            target = months_back(ctx.ref_day, months)
        base = next((b for b in reversed(complete) if b["day"] <= target), None)
        if ctx.last is None:
            pct = num(None, "computed", None, ctx.last_reason, label=plabel)
        elif base is None:
            pct = num(None, "computed", None, f"No daily close on or before {target.isoformat()} in the loaded "
                                              "history.", label=plabel)
        elif base["c"] == 0:
            pct = num(None, "computed", None, "The base close is zero.", label=plabel)
        else:
            pct = num((ctx.last / base["c"] - 1.0) * 100.0, "computed", ctx.last_at, label=plabel)
        perf[key] = {"pct": pct, "base_date": base["day"].isoformat() if base else None}
    # 52-week range
    since = ctx.ref_day - timedelta(weeks=52)
    yr = [b for b in complete if since < b["day"] <= ctx.ref_day]
    rlabel = "Alpaca crypto · daily bars (UTC days)" if ac == "crypto" else None
    if yr:
        rng = {"low": num(min(b["l"] for b in yr), dfeed, yr[-1]["t"], label=rlabel),
               "high": num(max(b["h"] for b in yr), dfeed, yr[-1]["t"], label=rlabel),
               "from": yr[0]["day"].isoformat(), "to": yr[-1]["day"].isoformat()}
    else:
        r = "No daily bars in the last 52 weeks."
        rng = {"low": num(None, dfeed, None, r, label=rlabel), "high": num(None, dfeed, None, r, label=rlabel),
               "from": None, "to": None}
    rng["position_pct"] = position_pct(ctx, rng)       # MR4: the 52W bar marker (SPEC §13 MR4 backend notes)
    return {"open": open_, "vwap": vwap, "avg_volume_30d": avg, "exchange": asset["exchange"],
            "performance": perf, "range_52w": rng, "notes": list(NOTES)}


POSITION_LABEL = "Last trade within the 52-week range (completed daily bars)"


def position_pct(ctx, rng: dict) -> dict:
    """(last − low) / (high − low) · 100, NOT clamped (the last trade may lie outside a range built from
    completed daily bars); null with the reason when an input is missing or high = low."""
    lo, hi = rng["low"]["value"], rng["high"]["value"]
    if ctx.last is None:
        return num(None, "computed", None, ctx.last_reason, label=POSITION_LABEL)
    if lo is None or hi is None:
        return num(None, "computed", None, rng["low"]["reason"] or rng["high"]["reason"], label=POSITION_LABEL)
    if hi <= lo:
        return num(None, "computed", None, "The 52-week high equals the low.", label=POSITION_LABEL)
    return num((ctx.last - lo) / (hi - lo) * 100.0, "computed", ctx.last_at, label=POSITION_LABEL)
