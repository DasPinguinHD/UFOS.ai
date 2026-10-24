"""Fakes for the Market Research MR2a data tests (no network, no alpaca-py, temp market_research.db).

``FakeSources`` = the ``market_research.sources.AlpacaSources`` interface with deterministic synthetic
bars: SIP/IEX 1m for every minute of the ETH window (04:00–20:00 ET, 17:00 on early-close days) of NYSE
trading days, crypto 1m/1Hour for every minute/hour, daily bars per trading day (equities) — and it
ASSERTS the free plan's SIP rule (a SIP request never ends within the last 15 minutes).
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

from market_research import store
from market_research.bars import aggregate as agg
from strategy_platform.core import calendars
from strategy_platform.core.clock import iso, to_utc
from strategy_platform.core.errors import UserFacingError


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def price(t):
    """Deterministic price of the minute starting at t (UTC)."""
    m = int(to_utc(t).timestamp() // 60)
    return 100.0 + (m % 97) * 0.1


def bar_row(t, vol=10.0, base=None):
    p = price(t) if base is None else base
    return {"ts": iso(t), "open": p, "high": p + 0.5, "low": p - 0.5, "close": p + 0.1, "volume": vol,
            "vwap": p + 0.05, "trade_count": 3}


def thirty_minute_rows(rows):
    """1m rows -> 30-minute rows (UTC-epoch floor = New York half hours; o first, c last, v Σ, vwap Σ(vw·v)/Σv)."""
    groups = {}
    for r in rows:
        t = to_utc(r["ts"])
        e = int(t.timestamp())
        groups.setdefault(e - e % 1800, []).append(r)
    out = []
    for e in sorted(groups):
        g = groups[e]
        v = sum(x["volume"] for x in g)
        out.append({"ts": iso(datetime.fromtimestamp(e, tz=timezone.utc)), "open": g[0]["open"],
                    "high": max(x["high"] for x in g), "low": min(x["low"] for x in g), "close": g[-1]["close"],
                    "volume": v, "vwap": sum(x["vwap"] * x["volume"] for x in g) / v,
                    "trade_count": sum(x["trade_count"] for x in g)})
    return out


class TempStore:
    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        store.init(os.path.join(self._tmp.name, "market_research.db"))
        from market_research import assets
        assets.reset_failure()
        return store

    def __exit__(self, *exc):
        store.close()
        self._tmp.cleanup()


ASSETS = [
    {"symbol": "AAPL", "name": "Apple Inc. Common Stock", "asset_class": "us_equity", "exchange": "NASDAQ",
     "tradable": True, "status": "active"},
    {"symbol": "AA", "name": "Alcoa Corporation", "asset_class": "us_equity", "exchange": "NYSE", "tradable": True,
     "status": "active"},
    {"symbol": "AAPW", "name": "Roundhill AAPL WeeklyPay ETF", "asset_class": "us_equity", "exchange": "BATS",
     "tradable": True, "status": "active"},
    {"symbol": "PAPL", "name": "Pineapple Holdings", "asset_class": "us_equity", "exchange": "NYSE",
     "tradable": True, "status": "active"},
    {"symbol": "SPY", "name": "SPDR S&P 500 ETF Trust", "asset_class": "us_equity", "exchange": "ARCA",
     "tradable": True, "status": "active"},
    {"symbol": "OTCX", "name": "Some OTC thing", "asset_class": "us_equity", "exchange": "OTC", "tradable": True,
     "status": "active"},
    {"symbol": "BTC/USD", "name": "Bitcoin  /  US Dollar\x07", "asset_class": "crypto", "exchange": "CRYPTO",
     "tradable": True, "status": "active"},
    {"symbol": "BTC/USDT", "name": "Bitcoin / USD Tether", "asset_class": "crypto", "exchange": "CRYPTO",
     "tradable": True, "status": "active"},
    {"symbol": "ETH/USD", "name": "Ethereum / US Dollar", "asset_class": "crypto", "exchange": "CRYPTO",
     "tradable": True, "status": "active"},
]


class FakeSources:
    def __init__(self, now_fn, *, daily_from=None):
        self.now = now_fn
        self.calls = []
        self.fail = {}            # op -> exception
        self.assets_rows = list(ASSETS)
        self.trade = {}           # symbol -> price
        self.vol = 10.0
        self.daily_overrides = {}  # (symbol, date) -> dict(open/high/low/close/volume)

    def _check(self, op):
        self.calls.append(op)
        ex = self.fail.get(op)
        if ex is not None:
            raise ex

    def _equity_minutes(self, start, end):
        out = []
        d = to_utc(start).astimezone(agg.NY).date()
        last = (to_utc(end) - timedelta(seconds=1)).astimezone(agg.NY).date()
        while d <= last:
            w = agg.session_window(d, "eth")
            if w:
                t = max(w[0], to_utc(start))
                t = t.replace(second=0, microsecond=0)
                stop = min(w[1], to_utc(end))
                while t < stop:
                    out.append(bar_row(t, self.vol))
                    t += timedelta(minutes=1)
            d += timedelta(days=1)
        return out

    # AlpacaSources interface (end exclusive)
    def sip_1m(self, symbol, start, end):
        self._check("sip_1m")
        limit = to_utc(self.now()) - timedelta(minutes=15)
        assert to_utc(end) <= limit + timedelta(seconds=1), f"SIP request ends within the last 15 min: {end}"
        self.calls.append(("sip_1m", iso(start), iso(end)))
        return self._equity_minutes(start, end)

    def sip_30m(self, symbol, start, end):
        """30-minute SIP bars built from the same synthetic minutes (clock aligned, like Alpaca's)."""
        self._check("sip_30m")
        limit = to_utc(self.now()) - timedelta(minutes=15)
        assert to_utc(end) <= limit + timedelta(seconds=1), f"SIP request ends within the last 15 min: {end}"
        self.calls.append(("sip_30m", iso(start), iso(end)))
        return thirty_minute_rows(self._equity_minutes(start, end))

    def iex_1m(self, symbol, start, end):
        self._check("iex_1m")
        rows = self._equity_minutes(start, end)
        for r in rows:
            r["volume"] = 1.0           # IEX volume is a fraction of SIP
        return rows

    def crypto_1m(self, symbol, start, end):
        self._check("crypto_1m")
        self.calls.append(("crypto_1m", iso(start), iso(end)))
        out, t = [], to_utc(start).replace(second=0, microsecond=0)
        while t < to_utc(end):
            out.append(bar_row(t, 2.0))
            t += timedelta(minutes=1)
        return out

    def latest_trade(self, symbol):
        self._check("latest_trade")
        return {"symbol": symbol, "price": self.trade.get(symbol, 150.0), "ts": iso(self.now())}

    def assets(self):
        self._check("assets")
        return [dict(a) for a in self.assets_rows]

    def daily(self, symbol, start, end, *, adjustment="all", fresh=None):
        self._check("daily")
        self.calls.append(("daily", adjustment, iso(fresh) if fresh else None))
        now = to_utc(self.now())
        out = []
        for d in calendars.trading_days(to_utc(start), min(to_utc(end), now)):
            close = calendars.session_bounds(d)[1]
            if close + timedelta(minutes=15) > now:       # SIP daily only for sessions closed ≥ 15 min ago
                continue
            t = agg.ny_midnight(d)
            if not (to_utc(start) <= t <= to_utc(end)):
                continue
            k = (d - datetime(2016, 1, 1).date()).days
            p = 50.0 + k * 0.05
            row = {"ts": iso(t), "open": p, "high": p + 1, "low": p - 1, "close": p, "volume": 1000.0 + k,
                   "vwap": p, "trade_count": 100, "known_at": iso(close)}
            row.update(self.daily_overrides.get((symbol, d), {}))
            out.append(row)
        return out

    def crypto_hourly(self, symbol, start, end):
        self._check("crypto_hourly")
        now = to_utc(self.now())
        out, t = [], to_utc(start).replace(minute=0, second=0, microsecond=0)
        while t <= to_utc(end) and t + timedelta(hours=1) <= now:
            if t >= to_utc(start):
                out.append(bar_row(t, 120.0))
            t += timedelta(hours=1)
        return out


class FailingSources(FakeSources):
    def __init__(self, now_fn, ex: UserFacingError):
        super().__init__(now_fn)
        for op in ("sip_1m", "sip_30m", "iex_1m", "crypto_1m", "latest_trade", "assets", "daily", "crypto_hourly"):
            self.fail[op] = ex
