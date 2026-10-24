"""Panel: dates × symbols arrays per field (SPEC §7.7).

``Panel(dates, symbols, fields, calendar)`` — ``dates`` ascending
``datetime.date`` list, ``fields`` ``{name: float ndarray[len(dates),
len(symbols)]}`` with NaN = no value. Row t is the state at the END of date
t (NYSE: that session's close; UTC calendar: the end of that UTC day's bar).

``Panel.from_bars(bars_by_symbol, calendar="auto")`` builds it from bar
dicts (data/providers/base.py shape, ``1Day``):
- equities only (or ``calendar="nyse"``): rows = NYSE sessions
  (core.calendars) between the first and the last equity bar; an equity
  bar sits on its New York date; a session without a bar stays NaN;
- crypto only (``auto``) or ``calendar="utc"``: rows = every UTC day between
  the first and last bar; a bar sits on the UTC date of its start;
- mixed: NYSE sessions, and each crypto value is the newest crypto daily
  bar whose ``known_at`` (data/pit.py) is ≤ that session's close. Only a bar
  known within the 24 h before the close qualifies (with complete data the
  newest bar is always younger) — an older bar is NOT carried forward (gap → NaN, no stale value). Rows end at the last session
  with an equity bar.

numpy is imported lazily (main.py imports this package at startup).

Part of UFOS.ai, a proof-of-concept portfolio project.
"""
from __future__ import annotations

from bisect import bisect_right
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence

from . import dsl

CALENDARS = ("auto", "nyse", "utc")
MIXED_MAX_AGE = timedelta(hours=24)   # complete data: the newest known bar is always < 24 h old at a close


class Panel:
    def __init__(self, dates: Sequence[date], symbols: Sequence[str], fields: Dict[str, object],
                 calendar: str = "nyse"):
        import numpy as np
        self.dates: List[date] = list(dates)
        self.symbols: List[str] = list(symbols)
        self.calendar = calendar
        shape = (len(self.dates), len(self.symbols))
        self.fields: Dict[str, object] = {}
        for name, arr in fields.items():
            a = np.asarray(arr, dtype=float)
            if a.shape != shape:
                raise ValueError(f"field {name!r} has shape {a.shape}, expected {shape}")
            self.fields[name] = a
        for name in dsl.FIELDS:
            if name not in self.fields:
                self.fields[name] = np.full(shape, np.nan)
        if any(self.dates[i] >= self.dates[i + 1] for i in range(len(self.dates) - 1)):
            raise ValueError("panel dates must be strictly ascending")
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("duplicate symbols in panel")

    def __len__(self) -> int:
        return len(self.dates)

    @property
    def shape(self):
        return len(self.dates), len(self.symbols)

    def field(self, name: str):
        return self.fields[name]

    def head(self, n: int) -> "Panel":
        """The first n rows (``panel[:n]``) — what was known at row n-1."""
        n = max(0, int(n))
        return Panel(self.dates[:n], self.symbols, {k: v[:n] for k, v in self.fields.items()}, self.calendar)

    def __getitem__(self, item) -> "Panel":
        if not isinstance(item, slice) or item.step not in (None, 1) or item.start not in (None, 0):
            raise TypeError("Panel supports only prefix slices panel[:n]")
        stop = len(self.dates) if item.stop is None else item.stop
        if stop < 0:
            stop = max(0, len(self.dates) + stop)
        return self.head(stop)

    def asset_classes(self) -> List[str]:
        from ..core.instruments import asset_class
        return [asset_class(s) for s in self.symbols]

    def index_of(self, d) -> int:
        d = d if isinstance(d, date) and not isinstance(d, datetime) else _as_date(d)
        return self.dates.index(d)

    @classmethod
    def from_bars(cls, bars_by_symbol: Dict[str, List[dict]], calendar: str = "auto", *,
                  symbols: Optional[Sequence[str]] = None) -> "Panel":
        import numpy as np
        from ..core import calendars as cal
        from ..core.instruments import instrument
        from ..data.providers.base import parse_ts
        if calendar not in CALENDARS:
            raise ValueError(f"calendar: one of {CALENDARS}")
        syms = [instrument(s).symbol for s in (symbols if symbols is not None else bars_by_symbol)]
        classes = {s: instrument(s).asset_class for s in syms}
        bars = {instrument(k).symbol: list(v or []) for k, v in bars_by_symbol.items()}
        has_eq = any(c == "equity" for c in classes.values())
        has_cr = any(c == "crypto" for c in classes.values())
        mode = calendar if calendar != "auto" else ("nyse" if has_eq or not has_cr else "utc")
        if mode == "utc" and has_eq:
            raise ValueError("a UTC-day panel holds crypto only; equities need the NYSE calendar")

        def eq_date(b) -> date:
            return parse_ts(b["ts"]).astimezone(cal.NY).date()

        def utc_date(b) -> date:
            return parse_ts(b["ts"]).astimezone(timezone.utc).date()

        if mode == "nyse":
            eq_dates = [eq_date(b) for s in syms if classes[s] == "equity" for b in bars.get(s, [])]
            if eq_dates:
                lo, hi = min(eq_dates), max(eq_dates)
            else:
                cr = [_known_at(b, s) for s in syms for b in bars.get(s, [])]
                lo = min(cr).astimezone(cal.NY).date() if cr else None
                hi = max(cr).astimezone(cal.NY).date() if cr else None
            dates = cal.trading_days(lo, hi) if lo is not None else []
        else:
            all_d = [utc_date(b) for s in syms for b in bars.get(s, [])]
            dates = []
            if all_d:
                d, hi = min(all_d), max(all_d)
                while d <= hi:
                    dates.append(d)
                    d += timedelta(days=1)
        row = {d: i for i, d in enumerate(dates)}
        shape = (len(dates), len(syms))
        arrays = {f: np.full(shape, np.nan) for f in dsl.FIELDS}
        closes = None
        for j, s in enumerate(syms):
            rows = bars.get(s, [])
            if mode == "nyse" and classes[s] == "crypto":
                if closes is None:
                    closes = [cal.session_bounds(d)[1] for d in dates]
                ordered = sorted(rows, key=lambda b: _known_at(b, s))
                kn = [_known_at(b, s) for b in ordered]
                for i, c in enumerate(closes):
                    k = bisect_right(kn, c) - 1
                    if k >= 0 and c - kn[k] <= MIXED_MAX_AGE:
                        _put(arrays, i, j, ordered[k])
                continue
            seen = set()
            for b in rows:
                d = eq_date(b) if classes[s] == "equity" else utc_date(b)
                i = row.get(d)
                if i is None:
                    continue
                if d in seen:
                    raise ValueError(f"two daily bars for {s} on {d.isoformat()}")
                seen.add(d)
                _put(arrays, i, j, b)
        return cls(dates, syms, arrays, mode)


def _put(arrays, i, j, bar):
    for f in dsl.FIELDS:
        v = bar.get(f)
        if v is not None:
            try:
                arrays[f][i, j] = float(v)
            except (TypeError, ValueError):
                raise ValueError(f"bad {f} value {v!r} in bar {bar.get('symbol')} {bar.get('ts')}") from None


def _known_at(bar, symbol) -> datetime:
    from ..data import pit
    from ..data.providers.base import parse_ts
    if bar.get("known_at"):
        return parse_ts(bar["known_at"])
    return pit.known_at("crypto", "1Day", bar["ts"])


def _as_date(v) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])
