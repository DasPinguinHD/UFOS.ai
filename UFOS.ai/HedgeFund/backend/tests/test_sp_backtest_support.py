"""Fakes for the stage-3c backtest engine tests (no tests here): a data
loader with synthetic daily bars, volumes, dividends and crypto bars, and
spec builders. Offline, deterministic. Part of UFOS.ai (proof of concept)."""
from datetime import date, datetime, time, timedelta, timezone

from strategy_platform.core import calendars
from strategy_platform.data import pit

DAYS = calendars.trading_days(date(2024, 1, 2), date(2024, 4, 30))
BIG_DV = 1e12


def eq_bar(symbol, d, o, c, *, volume=1e6, source="alpaca"):
    ts = datetime.combine(d, time(0), tzinfo=calendars.NY).astimezone(timezone.utc)
    return {"symbol": symbol, "timeframe": "1Day", "ts": ts.isoformat(timespec="seconds"), "open": o,
            "high": max(o, c) * 1.01, "low": min(o, c) * 0.99, "close": c, "volume": volume, "vwap": c,
            "trade_count": 10, "source": source, "known_at": pit.known_at_iso("equity", "1Day", ts)}


def cr_bar(symbol, d, o, c, *, volume=1e6):
    ts = datetime.combine(d, time(0), tzinfo=timezone.utc)
    return {"symbol": symbol, "timeframe": "1Day", "ts": ts.isoformat(timespec="seconds"), "open": o,
            "high": max(o, c) * 1.01, "low": min(o, c) * 0.99, "close": c, "volume": volume, "vwap": c,
            "trade_count": 10, "source": "alpaca", "known_at": (ts + timedelta(days=1)).isoformat(timespec="seconds")}


def path(base, n, drift=0.001, amp=0.01):
    """(open, close) per day: a gentle deterministic wiggle."""
    out = []
    for i in range(n):
        c = base * (1 + drift) ** i * (1 + amp * ((i * 7) % 5 - 2) / 2)
        o = c * (1 - amp / 3 if i % 2 else 1 + amp / 4)
        out.append((round(o, 4), round(c, 4)))
    return out


class FakeLoader:
    """prices {sym: {date: (open, close)}} (split series; ``all`` = split ×
    all_factor), volumes {sym: {date: dollar volume}} (default BIG_DV on every
    price day; None = Tiingo has no rows), dividends {sym: [(ex_date, amount)]},
    crypto {sym: {date: (open, close, volume)}}."""

    def __init__(self, prices=None, *, volumes=None, dividends=None, crypto=None, all_factor=0.98,
                 volume_last=None):
        self.prices = prices or {}
        self.volumes = volumes or {}
        self.dividends = dividends or {}
        self.crypto = crypto or {}
        self.all_factor = all_factor
        self.volume_last = volume_last
        self.calls = []

    @staticmethod
    def _in(d, start, end):
        return d >= start and (end is None or d <= end)

    def history(self, symbol, adjustment, start, end=None, *, progress=None):
        self.calls.append(("history", symbol, adjustment))
        f = 1.0 if adjustment == "split" else self.all_factor
        rows = [eq_bar(symbol, d, o * f, c * f) for d, (o, c) in sorted(self.prices.get(symbol, {}).items())
                if self._in(d, start, end)]
        return rows, {"symbol": symbol, "adjustment": adjustment, "sources": ["alpaca"] if rows else [],
                      "ranges": {}, "seam": None, "warnings": [], "licences": {}}

    def corporate_actions(self, symbol, start, end=None, *, progress=None):
        rows = [{"symbol": symbol, "kind": "cash_dividend", "ex_date": ex.isoformat(), "cash_amount": amt,
                 "cash_amount_split_adj": amt, "split_ratio": None, "pay_date": None, "source": "alpaca",
                 "known_at": pit.action_known_at_iso(ex)} for ex, amt in self.dividends.get(symbol, [])
                if self._in(ex, start, end)]
        return rows, {"symbol": symbol, "sources": ["alpaca"], "seam": None, "warnings": [], "licences": {}}

    def volume(self, symbol, start, end=None, *, progress=None):
        dv = self.volumes.get(symbol, "default")
        if dv == "default":
            dv = {d: BIG_DV for d in self.prices.get(symbol, {})}
        rows = []
        for d in sorted(dv or {}):
            if self._in(d, start, end) and (self.volume_last is None or d <= self.volume_last):
                close = self.prices[symbol][d][1]
                rows.append({"date": d.isoformat(), "volume": dv[d] / close, "close_split": close,
                             "dollar_volume_split": dv[d], "known_at": "x", "source": "tiingo"})
        return rows, {"symbol": symbol, "source": "tiingo", "rows": len(rows),
                      "first": rows[0]["date"] if rows else None, "last": rows[-1]["date"] if rows else None,
                      "warnings": []}

    def crypto_bars(self, symbol, start, end=None):
        rows = [cr_bar(symbol, d, o, c, volume=v) for d, (o, c, v) in sorted(self.crypto.get(symbol, {}).items())
                if self._in(d, start, end)]
        return rows, {"symbol": symbol, "sources": ["alpaca"], "rows": len(rows), "warnings": []}

    def ext_values(self, spec, panel):
        return None


def series(base, days=DAYS, **kw):
    return {d: oc for d, oc in zip(days, path(base, len(days), **kw))}


def spec(symbols, *, mode="long_only", selection=None, signal=None, rebalance="daily", timing="next_open",
         risk=None, costs=None, gross=1.0, max_weight=1.0):
    s = {"schema": "ufos.strategy/1", "name": "bt test", "universe": {"symbols": list(symbols)},
         "signal": signal or {"field": "close"},
         "portfolio": {"mode": mode, "selection": selection or {"type": "all"}, "weighting": "equal",
                       "max_weight": max_weight, "gross_exposure": gross},
         "rebalance": {"frequency": rebalance}, "execution": {"order_type": "market", "timing": timing}}
    if risk:
        s["risk"] = dict({"stop_loss_pct": None, "take_profit_pct": None, "vol_target_annual": None,
                          "max_drawdown_stop_pct": None}, **risk)
    if costs:
        s["costs"] = costs
    return s
