"""strategy/preview.py with a fake bar cache and clock: rows/rank/weights,
history, symbols without history excluded and listed, source failures and
stale data stop the preview (no fallback), time bound, invalid input.
Offline (no network, no database).

Run from HedgeFund/backend:  python -m unittest tests.test_sp_strategy_preview -v
"""
import unittest
from datetime import datetime, timedelta, timezone

from strategy_platform.core import calendars as cal
from strategy_platform.core.clock import FixedClock
from strategy_platform.core.errors import DataSourceUnavailable, InvalidRequest
from strategy_platform.data import pit
from strategy_platform.data.providers.base import iso, parse_ts
from strategy_platform.strategy.preview import PreviewTimeout, preview

NOW = datetime(2026, 9, 30, 22, 0, tzinfo=timezone.utc)      # after the 2026-09-30 close (20:00 UTC)
DRIFT = {"SPY": 0.002, "TLT": -0.001, "GLD": 0.0005, "BTC-USD": 0.003}


class FakeCache:
    def __init__(self, *, no_history=(), stale=(), fail=()):
        self.no_history, self.stale, self.fail = set(no_history), set(stale), set(fail)
        self.calls = []

    def __call__(self, symbol, timeframe, start, end=None, *, require_fresh_through=None):
        self.calls.append((symbol, require_fresh_through))
        if symbol in self.fail:
            raise DataSourceUnavailable("alpaca", "equity_bars", "HTTP 500 — gave up after 3 attempts")
        if symbol in self.no_history:
            return []
        start, end = parse_ts(start), parse_ts(end)
        rows, price = [], 100.0
        crypto = symbol.endswith("-USD")
        d = start.date()
        last = end.date() - timedelta(days=3 if symbol in self.stale else 0)
        while d <= last:
            if crypto:
                ts = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
                ok = True
            else:
                ok = cal.is_trading_day(d)
                ts = datetime(d.year, d.month, d.day, tzinfo=cal.NY).astimezone(timezone.utc)
            if ok:
                price *= 1 + DRIFT.get(symbol, 0.001)
                kn = pit.known_at("crypto" if crypto else "equity", "1Day", ts)
                if kn <= NOW:
                    rows.append({"symbol": symbol, "timeframe": "1Day", "ts": iso(ts), "open": price,
                                 "high": price * 1.01, "low": price * 0.99, "close": price, "volume": 1e6,
                                 "vwap": price, "trade_count": 100, "source": "alpaca", "known_at": iso(kn),
                                 "fetched_at": iso(NOW)})
            d += timedelta(days=1)
        if require_fresh_through is not None and symbol in self.stale:
            raise DataSourceUnavailable("alpaca", "equity_bars", f"no 1Day bar for {symbol} through "
                                                                 f"{iso(require_fresh_through)} yet")
        return rows


def spec(symbols=("SPY", "TLT", "GLD", "NEWCO"), **pf):
    p = {"mode": "long_only", "selection": {"type": "top_n", "n": 2}, "weighting": "equal", "max_weight": 0.5}
    p.update(pf)
    return {"schema": "ufos.strategy/1", "name": "Preview test", "universe": {"symbols": list(symbols)},
            "signal": {"op": "pct_change", "args": [{"field": "close"}, 5]}, "portfolio": p,
            "rebalance": {"frequency": "weekly"}}


def run(s, cache, **kw):
    return preview(s, days=kw.pop("days", 10), get_bars=cache, clock=FixedClock(NOW),
                   configured_source=lambda dt: "alpaca", ext_catalogue=[], **kw)


class PreviewTest(unittest.TestCase):
    def test_rows_history_and_missing(self):
        cache = FakeCache(no_history={"NEWCO"})
        out = run(spec(), cache)
        self.assertEqual(set(out), {"as_of", "rows", "history", "warnings", "data"})
        self.assertEqual(out["as_of"], iso(NOW))
        rows = out["rows"]
        self.assertEqual([r["symbol"] for r in rows], ["SPY", "GLD", "TLT"])
        self.assertEqual([r["rank"] for r in rows], [1, 2, 3])
        self.assertEqual([r["target_weight"] for r in rows], [0.5, 0.5, 0.0])
        self.assertGreater(rows[0]["signal"], rows[1]["signal"])
        self.assertEqual(out["data"]["symbols_missing"], ["NEWCO"])
        self.assertEqual(out["data"]["source"], "alpaca")
        self.assertEqual(out["data"]["to"], "2026-09-30")
        self.assertIn("symbols_missing", [w["code"] for w in out["warnings"]])
        self.assertEqual(len(out["history"]), 10)
        self.assertEqual(out["history"][-1]["date"], "2026-09-30")
        self.assertEqual(out["history"][-1]["weights"], {"GLD": 0.5, "SPY": 0.5})
        # freshness was required: last close at/before as_of
        fresh = [f for s, f in cache.calls if s == "SPY" and f is not None]
        self.assertEqual(fresh, [cal.last_close_before(NOW)])

    def test_mixed_universe(self):
        out = run(spec(symbols=("SPY", "BTC-USD")), FakeCache())
        self.assertEqual({r["symbol"] for r in out["rows"]}, {"SPY", "BTC-USD"})
        self.assertIn("mixed_calendars", [w["code"] for w in out["warnings"]])
        out = run(spec(symbols=("BTC-USD",), selection={"type": "all"}, max_weight=1.0), FakeCache())
        self.assertEqual(out["rows"][0]["target_weight"], 1.0)
        self.assertEqual(out["data"]["to"], "2026-09-29")     # the 09-30 bar completes only at 10-01 00:00 UTC

    def test_source_failure_stops_the_preview(self):
        with self.assertRaises(DataSourceUnavailable):
            run(spec(), FakeCache(fail={"TLT"}, no_history={"NEWCO"}))

    def test_stale_data_stops_the_preview(self):
        with self.assertRaises(DataSourceUnavailable) as cm:
            run(spec(), FakeCache(stale={"GLD"}, no_history={"NEWCO"}))
        self.assertIn("GLD", cm.exception.user_message)

    def test_no_symbol_with_data(self):
        with self.assertRaises(InvalidRequest):
            run(spec(symbols=("NEWCO",)), FakeCache(no_history={"NEWCO"}))

    def test_time_bound(self):
        ticks = iter(range(0, 10000, 30))
        with self.assertRaises(PreviewTimeout) as cm:
            run(spec(), FakeCache(no_history={"NEWCO"}), monotonic=lambda: next(ticks))
        self.assertIn("60 s", cm.exception.user_message)

    def test_invalid_input(self):
        bad = spec()
        bad["signal"] = {"op": "lag", "args": [{"field": "close"}, 0]}
        with self.assertRaises(InvalidRequest):
            run(bad, FakeCache())
        for days in (0, 251, "5", True):
            with self.assertRaises(InvalidRequest):
                run(spec(), FakeCache(), days=days)
        with self.assertRaises(InvalidRequest):
            run(spec(), FakeCache(), as_of=NOW + timedelta(days=1))

    def test_historic_as_of(self):
        as_of = datetime(2026, 9, 15, 21, 0, tzinfo=timezone.utc)
        out = run(spec(), FakeCache(no_history={"NEWCO"}), as_of=as_of)
        self.assertEqual(out["data"]["to"], "2026-09-15")


if __name__ == "__main__":
    unittest.main()
