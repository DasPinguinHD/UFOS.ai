"""Portfolio construction: signal row -> target weights (SPEC §7.7).

``target_weights(spec, signal_row, panel, t) -> {symbol: weight}`` (signed
fractions of equity, only non-zero weights, keys sorted). Deterministic:

1. Eligible = symbols with a finite signal at t; with ``inverse_vol``
   weighting also a finite, positive volatility (sample std of the last
   ``vol_lookback`` daily close-to-close returns up to t; all needed closes
   present) — a symbol without one cannot be weighted and is not selected.
2. Selection (ties broken by symbol, ascending):
   ``top_n`` the n highest signals, ``bottom_n`` the n lowest,
   ``top_bottom_n`` (long_short only) n highest long + n lowest short (a
   symbol is never on both sides), ``threshold`` long where signal >
   ``long_above``, short where signal < ``short_below`` (strict), ``all``
   every eligible symbol. Side: long_only → long, short_only → short;
   long_short → top/high side long, bottom/low side short; ``all`` in
   long_short goes by the signal's sign (0 not held).
3. Weighting of the selected: ``equal`` 1, ``signal`` |signal|,
   ``inverse_vol`` 1/vol; scaled so Σ|w| = ``gross_exposure`` (a zero total →
   no position, all cash).
4. ``max_weight`` caps |w|; the excess is NOT redistributed (held as cash).

``rebalance_dates(spec, dates)``: daily = every date; weekly / monthly = the
first panel date of each ISO week / calendar month.

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional

from . import schema


def _np():
    import numpy as np
    return np


def realized_vol(panel, t: int, lookback: int) -> List[float]:
    """Per symbol: sample std of the last ``lookback`` daily returns ending
    at row t (needs closes t-lookback..t, all present), else NaN."""
    np = _np()
    close = panel.fields["close"]
    if t - lookback < 0:
        return [math.nan] * len(panel.symbols)
    win = close[t - lookback:t + 1]
    with np.errstate(all="ignore"):
        rets = win[1:] / win[:-1] - 1.0
        vol = rets.std(axis=0, ddof=1)
    bad = np.isnan(win).any(axis=0) | ~np.isfinite(vol) | (win <= 0).any(axis=0)
    vol = np.where(bad, np.nan, vol)
    return [float(v) for v in vol]


def target_weights(spec: dict, signal_row, panel, t: int) -> Dict[str, float]:
    pf = schema.with_defaults(spec)["portfolio"]
    mode = pf["mode"]
    sel = pf["selection"]
    weighting = pf["weighting"]
    gross = float(pf["gross_exposure"])
    cap = float(pf["max_weight"])
    symbols = panel.symbols
    sig = [float(v) for v in signal_row]
    vol = realized_vol(panel, t, int(pf["vol_lookback"])) if weighting == "inverse_vol" else None
    eligible = [j for j, v in enumerate(sig) if math.isfinite(v)
                and (vol is None or (math.isfinite(vol[j]) and vol[j] > 0))]
    desc = sorted(eligible, key=lambda j: (-sig[j], symbols[j]))
    asc = sorted(eligible, key=lambda j: (sig[j], symbols[j]))
    longs: List[int] = []
    shorts: List[int] = []
    typ = sel["type"]
    if typ == "top_n":
        chosen = desc[:int(sel["n"])]
        (shorts if mode == "short_only" else longs).extend(chosen)
    elif typ == "bottom_n":
        chosen = asc[:int(sel["n"])]
        (longs if mode == "long_only" else shorts).extend(chosen)
    elif typ == "top_bottom_n":
        n = int(sel["n"])
        longs = desc[:n]
        taken = set(longs)
        shorts = [j for j in asc if j not in taken][:n]
    elif typ == "threshold":
        if mode != "short_only" and sel.get("long_above") is not None:
            longs = [j for j in desc if sig[j] > float(sel["long_above"])]
        if mode != "long_only" and sel.get("short_below") is not None:
            shorts = [j for j in asc if sig[j] < float(sel["short_below"])]
    elif typ == "all":
        if mode == "long_only":
            longs = list(desc)
        elif mode == "short_only":
            shorts = list(asc)
        else:
            longs = [j for j in desc if sig[j] > 0]
            shorts = [j for j in asc if sig[j] < 0]
    else:
        raise ValueError(f"unknown selection type {typ!r}")

    def raw(j) -> float:
        if weighting == "equal":
            return 1.0
        if weighting == "signal":
            return abs(sig[j])
        return 1.0 / vol[j]

    picks = [(j, 1.0) for j in longs] + [(j, -1.0) for j in shorts]
    total = sum(raw(j) for j, _s in picks)
    if not picks or not total > 0 or not math.isfinite(total):
        return {}
    out: Dict[str, float] = {}
    for j, side in picks:
        w = gross * raw(j) / total
        w = min(w, cap)
        if w != 0.0:
            out[symbols[j]] = side * w
    return {k: out[k] for k in sorted(out)}


def rebalance_mask(spec: dict, dates) -> List[bool]:
    freq = schema.with_defaults(spec)["rebalance"]["frequency"]
    out = []
    prev = None
    for d in dates:
        if freq == "daily":
            key = d
        elif freq == "weekly":
            iso = d.isocalendar()
            key = (iso[0], iso[1])
        elif freq == "monthly":
            key = (d.year, d.month)
        else:
            raise ValueError(f"unknown rebalance frequency {freq!r}")
        out.append(key != prev)
        prev = key
    return out


def rebalance_dates(spec: dict, dates) -> list:
    return [d for d, r in zip(dates, rebalance_mask(spec, dates)) if r]


def weights_history(spec: dict, signal, panel, *, start: int = 0) -> List[tuple]:
    """Vectorised counterpart of the SDK loop: [(date, weights | None)] per
    row from ``start`` (None = no rebalance on that date)."""
    mask = rebalance_mask(spec, panel.dates)
    out = []
    for t in range(start, len(panel.dates)):
        out.append((panel.dates[t], target_weights(spec, signal[t], panel, t) if mask[t] else None))
    return out


def held_weights(spec: dict, signal, panel, first: int) -> List[dict]:
    """Weights held after each row from ``first`` to the end: the targets of
    the latest rebalance at or before that row (none yet → {})."""
    mask = rebalance_mask(spec, panel.dates)
    last = None
    for t in range(first, -1, -1):
        if mask[t]:
            last = t
            break
    cur = target_weights(spec, signal[last], panel, last) if last is not None else {}
    out = []
    for t in range(first, len(panel.dates)):
        if mask[t] and t != last:
            cur = target_weights(spec, signal[t], panel, t)
        out.append({"date": panel.dates[t].isoformat(), "weights": cur, "rebalance": bool(mask[t])})
    return out
