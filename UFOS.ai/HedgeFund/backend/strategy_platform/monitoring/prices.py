"""Daily closes for the Shadow Account (SPEC §19.7/§19.8, review fix H2 2026-10-18), from the data layer
(``backtest.engine.DataLoader``: Alpaca SIP + Tiingo history, Alpaca crypto bars), USD.

Two price bases, never mixed:
- ``total`` — the ``all``-adjusted series (splits AND dividends): only for RETURN series (shadow portfolios,
  benchmark timing).
- ``raw`` — the price as traded on that day: the ``split``-adjusted series × the split ratios with an ex-date AFTER
  that day (the data layer's corporate actions; ratio = new/old). Used wherever a price is compared with a trade
  price (paper gains/losses, the end valuation). Splits are reported per symbol (``splits_between``) so callers
  refuse a comparison across a split (imported lots are not split-adjusted).

An instrument the data layer does not know has NO price — callers count it as excluded; never a stand-in, never a
ticker guess. ``raw_on`` / ``total_on`` take the close of that exact date only. Tests inject ``set_source(fn,
actions_fn)``: ``fn(symbol, start, end) -> [(iso day, close)]`` (both bases), ``actions_fn(symbol, start, end) ->
[(ex_date, ratio)]``.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

PRICE_SOURCE = ("one source per instrument, in its own currency (§20.11): Alpaca / Tiingo for US tickers (USD; raw "
                "closes for price comparisons, total-return closes for return series), EODHD for the listing you "
                "picked, Kraken public for BTC / ETH / SOL in EUR — plus your valuation; never converted")

_source: Optional[Callable] = None
_actions: Optional[Callable] = None


def set_source(fn: Optional[Callable], actions_fn: Optional[Callable] = None) -> None:
    global _source, _actions
    _source, _actions = fn, actions_fn


def _history(symbol: str, adjustment: str, start: date, end: date) -> List[Tuple[str, float]]:
    from ..backtest import engine as bt
    from ..core import instruments
    loader = bt.DataLoader()
    inst = instruments.instrument(symbol)
    if inst.asset_class == "crypto":
        rows, _info = loader.crypto_bars(inst.symbol, start, end)
    else:
        rows, _info = loader.history(inst.symbol, adjustment, start, end)
    out = []
    for r in rows or []:
        c = r.get("close")
        if c is None or float(c) <= 0:
            continue
        out.append((bt._utc_date(r["ts"]).isoformat(), float(c)))
    return out


def _splits_default(symbol: str, start: date, end: date) -> List[Tuple[str, float]]:
    from ..backtest import engine as bt
    from ..core import instruments
    if instruments.instrument(symbol).asset_class == "crypto":
        return []
    rows, _info = bt.DataLoader().corporate_actions(symbol, start, end)
    return sorted((str(a["ex_date"])[:10], float(a["split_ratio"])) for a in rows or []
                  if a.get("kind") != "cash_dividend" and a.get("split_ratio"))


class Closes:
    """Per-request cache of both price bases and the splits."""

    def __init__(self, start: date, end: date, actions_start: Optional[date] = None):
        """``start``..``end`` = the price window; ``actions_start`` (default ``start``) = from when the splits are
        loaded (e.g. the first purchase), so a short recent price window still sees every split since then."""
        self.start, self.end = start, end
        self.actions_start = actions_start or start
        self._total: Dict[str, Optional[List[Tuple[str, float]]]] = {}
        self._raw: Dict[str, Optional[Dict[str, float]]] = {}
        self._splits: Dict[str, Optional[List[Tuple[str, float]]]] = {}
        self.errors: Dict[str, str] = {}

    def _load(self, s: str, adjustment: str) -> Optional[List[Tuple[str, float]]]:
        try:
            if _source is not None:
                rows = sorted(_source(s, self.start, self.end) or [])
            else:
                rows = sorted(_history(s, adjustment, self.start, self.end))
        except Exception as ex:      # noqa: BLE001 — unknown symbol / source down: no price (reason kept)
            self.errors[s] = (getattr(ex, "user_message", None) or str(ex) or type(ex).__name__)[:200]
            return None
        if not rows:
            self.errors.setdefault(s, "no bars in the period")
            return None
        return rows

    def series(self, symbol: Optional[str]) -> Optional[List[Tuple[str, float]]]:
        """The ``total`` (all-adjusted) series — for return series only."""
        if not symbol:
            return None
        s = symbol.strip().upper()
        if s not in self._total:
            self._total[s] = self._load(s, "all")
        return self._total[s]

    def splits(self, symbol: str) -> Optional[List[Tuple[str, float]]]:
        s = symbol.strip().upper()
        if s not in self._splits:
            try:
                fn = _actions if _source is not None else _splits_default
                self._splits[s] = sorted(fn(s, self.actions_start, self.end) or []) if fn else []
            except Exception as ex:  # noqa: BLE001 — without the corporate actions no raw price can be derived
                self.errors[s] = ("corporate actions unavailable: " +
                                  (getattr(ex, "user_message", None) or str(ex) or type(ex).__name__))[:200]
                self._splits[s] = None
        return self._splits[s]

    def raw(self, symbol: str) -> Optional[Dict[str, float]]:
        s = symbol.strip().upper()
        if s in self._raw:
            return self._raw[s]
        rows = self._load(s, "split")
        sp = self.splits(s) if rows else None
        if rows is None or sp is None:
            self._raw[s] = None
            return None
        out = {}
        for d, c in rows:
            f = 1.0
            for ex, r in sp:
                if ex > d:
                    f *= r
            out[d] = c * f
        self._raw[s] = out
        return out

    def raw_on(self, symbol: Optional[str], day: str) -> Optional[float]:
        """The raw close of ``symbol`` on exactly ``day`` (None: no session / no data)."""
        if not symbol:
            return None
        r = self.raw(symbol)
        return None if r is None else r.get(day)

    def splits_between(self, symbol: str, a: str, b: str) -> List[Tuple[str, float]]:
        """Splits with a < ex_date ≤ b."""
        return [(d, r) for d, r in (self.splits(symbol) or []) if a < d <= b]

    def total_on(self, symbol: Optional[str], day: str) -> Optional[float]:
        rows = self.series(symbol)
        if not rows:
            return None
        return dict(rows).get(day)
