"""Market Research MR5 — indicator series (SPEC §10 MR5, §13 "MR5 backend notes", Q8): spec grammar and
limits (≤ 8, ≤ 3 sub-panes), parity with the Strategy Platform DSL evaluator on the same input (and with
independent reference formulas), warm-up nulls and loaded warm-up history, no look-ahead, session VWAP
reset, compare overlay, the route (same bars as /bars, validation, strict JSON).

Run from HedgeFund/backend:  python -m unittest tests.test_mr_indicators -v
"""
import asyncio
import logging
import math
import unittest
from datetime import date, timedelta

import numpy as np

try:
    from mr_data_testkit import FakeSources, TempStore, utc
    from test_mr_data_api import Q
except ImportError:  # pragma: no cover - run as package
    from tests.mr_data_testkit import FakeSources, TempStore, utc
    from tests.test_mr_data_api import Q

from market_research import ResearchService, api as mr_api, indicators as ind
from market_research.bars import aggregate as agg
from market_research.bars.series import BadParam
from market_research.data import DataService
from market_research.supervisor import ResearchSupervisor
from strategy_platform.api.routes import dumps
from strategy_platform.core import calendars
from strategy_platform.core.clock import iso
from strategy_platform.strategy.evaluate import evaluate_node
from strategy_platform.strategy.panel import Panel

NOW = utc(2026, 10, 15, 15, 0)


def setUpModule():
    logging.getLogger("market_research").setLevel(logging.CRITICAL + 1)


def tearDownModule():
    logging.getLogger("market_research").setLevel(logging.NOTSET)


def daily_bars(n, start=None, seed=1):
    rng = np.random.default_rng(seed)
    days = calendars.trading_days(start or utc(2024, 1, 2), utc(2026, 10, 14))[:n]
    out, c = [], 100.0
    for d in days:
        o = c
        c = max(1.0, c * (1 + rng.normal(0, 0.02)))
        h, l = max(o, c) * 1.01, min(o, c) * 0.99
        out.append({"t": iso(agg.ny_midnight(d)), "o": o, "h": h, "l": l, "c": c, "v": 1000.0 + len(out),
                    "vwap": (h + l + c) / 3, "n": 10, "feed": "sip_daily", "final": True})
    return out


class FakeData:
    """``DataService`` subset: asset() + bars() over fixed bar lists (paging by ``before``/``limit``)."""

    def __init__(self, series, asset_class="equity"):
        self.series = series
        self.ac = asset_class
        self.calls = []

    def asset(self, symbol):
        return {"symbol": symbol, "asset_class": "crypto" if symbol.endswith("-USD") else "equity"}

    def bars(self, symbol, tf, session, before, limit):
        self.calls.append((symbol, tf, session, iso(before) if before else None, limit))
        rows = self.series[symbol]
        if before is not None:
            rows = [b for b in rows if b["t"] < iso(before)]
        lim = limit or 100
        page = rows[-lim:]
        crypto = symbol.endswith("-USD")
        return {"symbol": symbol, "tf": tf, "session": "utc" if crypto else (session or "rth"), "bars": page,
                "has_more_left": len(rows) > len(page), "sip_through": None,
                "tz": "UTC" if crypto else "America/New_York"}


def dsl(node, bars):
    """The DSL evaluator on Panel.from_bars of the same daily bars (the Strategy Platform's own path)."""
    rows = [{"symbol": "AAPL", "ts": b["t"], "open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"],
             "volume": b["v"], "vwap": b["vwap"], "trade_count": b["n"]} for b in bars]
    panel = Panel.from_bars({"AAPL": rows})
    assert len(panel) == len(bars)
    return evaluate_node(node, panel)[:, 0]


def same(a, b, tol=1e-9):
    a = [None if (x is None or (isinstance(x, float) and math.isnan(x))) else x for x in a]
    b = [None if (x is None or (isinstance(x, float) and math.isnan(x))) else float(x) for x in b]
    if len(a) != len(b):
        return False
    return all((x is None and y is None) or (x is not None and y is not None and abs(x - y) <= tol * max(1, abs(y)))
               for x, y in zip(a, b))


class ParseTest(unittest.TestCase):
    def test_normalisation(self):
        specs = ind.parse_list("SMA:20, ema:9:hl2,bb,bb:10:2.5:open,rsi,macd,vwap,cmp:spy", "AAPL")
        self.assertEqual([s.key for s in specs], ["sma:20:close", "ema:9:hl2", "bb:20:2:close", "bb:10:2.5:open",
                                                  "rsi:14", "macd:12:26:9", "vwap", "cmp:SPY"])
        self.assertEqual(specs[2].outputs, ["bb:20:2:close:mid", "bb:20:2:close:upper", "bb:20:2:close:lower"])
        self.assertEqual(specs[5].outputs, ["macd:12:26:9:macd", "macd:12:26:9:signal", "macd:12:26:9:hist"])
        self.assertEqual(specs[7].outputs, ["cmp:SPY:pct", "cmp:SPY:price"])
        self.assertEqual([s.pane for s in specs].count("sub"), 2)
        self.assertEqual(ind.parse_list("cmp:btc/usd", "ETH-USD")[0].key, "cmp:BTC-USD")

    def test_rejections(self):
        bad = ["", "sma", "sma:0", "sma:501", "sma:20:volume", "ema:1", "bb:20:0", "bb:20:11", "bb:20:2:close:x",
               "rsi:1", "rsi:101", "macd:26:12:9", "macd:12:26", "vwap:5", "cmp", "cmp:AAPL", "cmp:not ok!",
               "foo:1", "sma:20,sma:20:close", "rsi,rsi:9,macd,macd:5:10:3", "cmp:SPY,cmp:QQQ,cmp:IWM",
               ",".join(f"sma:{i}" for i in range(2, 11)), "sma:1e1", "x" * 401]
        for b in bad:
            with self.subTest(b=b[:40]):
                with self.assertRaises(BadParam) as cm:
                    ind.parse_list(b, "AAPL")
                self.assertEqual(cm.exception.field, "ind")
        self.assertEqual(len(ind.parse_list("rsi,rsi:9,macd", "AAPL")), 3)          # 3 sub-panes are fine
        self.assertEqual(len(ind.parse_list(",".join(f"sma:{i}" for i in range(2, 10)), "AAPL")), 8)

    def test_ema_halflife(self):
        for n in (2, 9, 12, 26, 200):
            alpha = 1 - 0.5 ** (1 / ind.hl_of(n))
            self.assertAlmostEqual(alpha, 2 / (n + 1), places=12)


class ParityTest(unittest.TestCase):
    """The route's values == the Strategy Platform DSL operators on the same bars (Q8)."""

    def setUp(self):
        self.bars = daily_bars(300)
        self.data = FakeData({"AAPL": self.bars})

    def full(self, spec_text, limit=300):
        return ind.compute(self.data, "AAPL", "1D", None, None, limit, ind.parse_list(spec_text, "AAPL"))

    def test_dsl_parity_full_history(self):
        r = self.full("sma:20,ema:12:hlc3,bb:20:2,rsi:14,macd")
        self.assertEqual(r["t"], [b["t"] for b in self.bars])
        self.assertEqual(r["warmup_loaded"], 0)
        c, h, l = {"field": "close"}, {"field": "high"}, {"field": "low"}
        hlc3 = {"op": "div", "args": [{"op": "add", "args": [h, l, c]}, 3.0]}
        sma = dsl({"op": "sma", "args": [c, 20]}, self.bars)
        self.assertTrue(same(r["series"]["sma:20:close"], sma))
        ema = dsl({"op": "ewm_mean", "args": [hlc3, ind.hl_of(12)]}, self.bars)
        ema[:11] = np.nan
        self.assertTrue(same(r["series"]["ema:12:hlc3"], ema))
        sd = dsl({"op": "ts_std", "args": [c, 20]}, self.bars)
        self.assertTrue(same(r["series"]["bb:20:2:close:mid"], sma))
        self.assertTrue(same(r["series"]["bb:20:2:close:upper"], sma + 2 * sd))
        self.assertTrue(same(r["series"]["bb:20:2:close:lower"], sma - 2 * sd))
        rsi = dsl({"op": "rsi", "args": [14]}, self.bars)
        self.assertTrue(same(r["series"]["rsi:14"], rsi))
        e12 = dsl({"op": "ewm_mean", "args": [c, ind.hl_of(12)]}, self.bars)
        e26 = dsl({"op": "ewm_mean", "args": [c, ind.hl_of(26)]}, self.bars)
        macd = e12 - e26
        sig = dsl({"op": "ewm_mean", "args": [{"op": "sub", "args": [{"op": "ewm_mean", "args": [c, ind.hl_of(12)]},
                                                                      {"op": "ewm_mean", "args": [c, ind.hl_of(26)]}]},
                                              ind.hl_of(9)]}, self.bars)
        m = macd.copy(); m[:25] = np.nan
        s = sig.copy(); s[:33] = np.nan
        hst = macd - sig; hst[:33] = np.nan
        self.assertTrue(same(r["series"]["macd:12:26:9:macd"], m))
        self.assertTrue(same(r["series"]["macd:12:26:9:signal"], s))
        self.assertTrue(same(r["series"]["macd:12:26:9:hist"], hst))
        dumps(r)

    def test_reference_formulas(self):
        import pandas as pd
        r = self.full("sma:10,ema:10,bb:10:2,rsi:5")
        c = pd.Series([b["c"] for b in self.bars])
        self.assertTrue(same(r["series"]["sma:10:close"], c.rolling(10).mean().values))
        ema = c.ewm(alpha=2 / 11, adjust=False).mean().values.copy()
        ema[:9] = np.nan
        self.assertTrue(same(r["series"]["ema:10:close"], ema, 1e-9))
        self.assertTrue(same(r["series"]["bb:10:2:close:upper"],
                             (c.rolling(10).mean() + 2 * c.rolling(10).std(ddof=1)).values))
        d = c.diff()
        g, lo = d.clip(lower=0).values, (-d).clip(lower=0).values
        ag, al = np.mean(g[1:6]), np.mean(lo[1:6])
        ref = [None] * 5 + [100 - 100 / (1 + ag / al)]
        for i in range(6, len(c)):
            ag = (ag * 4 + g[i]) / 5
            al = (al * 4 + lo[i]) / 5
            ref.append(100 - 100 / (1 + ag / al))
        self.assertTrue(same(r["series"]["rsi:5"], ref, 1e-9))

    def test_warmup_nulls_without_history(self):
        r = self.full("sma:20,ema:12,rsi:14,bb:5,macd:3:6:4", limit=300)
        s = r["series"]
        self.assertEqual(sum(v is None for v in s["sma:20:close"]), 19)
        self.assertIsNotNone(s["sma:20:close"][19])
        self.assertEqual(sum(v is None for v in s["ema:12:close"]), 11)
        self.assertEqual(sum(v is None for v in s["rsi:14"]), 14)
        self.assertEqual(sum(v is None for v in s["bb:5:2:close:lower"]), 4)
        self.assertEqual(sum(v is None for v in s["macd:3:6:4:macd"]), 5)
        self.assertEqual(sum(v is None for v in s["macd:3:6:4:signal"]), 8)
        meta = {m["key"]: m for m in r["indicators"]}
        self.assertEqual(meta["sma:20:close"]["warmup_bars"], 19)
        self.assertIn("sample standard deviation", meta["bb:5:2:close"]["note"])

    def test_warmup_history_is_loaded_and_cut(self):
        r = self.full("sma:20,rsi:14", limit=50)
        self.assertEqual(len(r["t"]), 50)
        self.assertEqual(r["t"], [b["t"] for b in self.bars[-50:]])
        self.assertEqual(r["warmup_loaded"], 140)                          # rsi 10·14 > sma 19
        self.assertTrue(all(v is not None for v in r["series"]["sma:20:close"]))
        whole = self.full("sma:20,rsi:14", limit=300)
        self.assertTrue(same(r["series"]["sma:20:close"], whole["series"]["sma:20:close"][-50:]))
        # Wilder smoothing is seeded from the loaded history: 140 vs 250 bars of warm-up differ only marginally
        self.assertTrue(same(r["series"]["rsi:14"], whole["series"]["rsi:14"][-50:], 1e-3))
        # second request = the prefix, before the window's first bar
        self.assertEqual(self.data.calls[1][3], self.bars[-50]["t"])

    def test_no_look_ahead(self):
        whole = self.full("sma:7,ema:5,rsi:6,macd:3:8:4,bb:9:1.5", limit=300)
        for cut in (40, 120, 299):
            part = ind.compute(FakeData({"AAPL": self.bars[:cut]}), "AAPL", "1D", None, None, cut,
                               ind.parse_list("sma:7,ema:5,rsi:6,macd:3:8:4,bb:9:1.5", "AAPL"))
            for k, vals in part["series"].items():
                self.assertTrue(same(vals, whole["series"][k][:cut]), (cut, k))

    def test_empty_window(self):
        r = ind.compute(FakeData({"AAPL": []}), "AAPL", "1D", None, None, 10, ind.parse_list("sma:5,rsi", "AAPL"))
        self.assertEqual((r["t"], r["series"]), ([], {"sma:5:close": [], "rsi:14": []}))


def minute_bars(days, vwap_gap_at=None):
    out = []
    for d in days:
        start = agg.session_window(d, "rth")[0]
        for i in range(30):
            t = start + timedelta(minutes=i)
            p = 100.0 + i + (10 if d != days[0] else 0)
            out.append({"t": iso(t), "o": p, "h": p + 1, "l": p - 1, "c": p, "v": float(i + 1),
                        "vwap": None if iso(t) == vwap_gap_at else p, "n": 1, "feed": "sip", "final": True})
    return out


class VwapCompareTest(unittest.TestCase):
    def test_session_reset_and_gap(self):
        days = calendars.trading_days(date(2026, 10, 12), date(2026, 10, 13))
        bars = minute_bars(days, vwap_gap_at=iso(agg.session_window(days[1], "rth")[0] + timedelta(minutes=10)))
        r = ind.compute(FakeData({"AAPL": bars}), "AAPL", "1m", "rth", None, 60, ind.parse_list("vwap", "AAPL"))
        v = r["series"]["vwap"]
        self.assertEqual(v[0], 100.0)
        exp = sum((100.0 + i) * (i + 1) for i in range(30)) / sum(i + 1 for i in range(30))
        self.assertAlmostEqual(v[29], exp)
        self.assertEqual(v[30], 110.0)                                     # reset at the next session start
        self.assertAlmostEqual(v[31], (110 * 1 + 111 * 2) / 3)
        self.assertEqual(v[40:], [None] * 20)                              # bar without vwap ends the session line
        self.assertIsNotNone(v[39])

    def test_incomplete_first_session_is_null(self):
        days = calendars.trading_days(date(2026, 10, 12), date(2026, 10, 13))
        bars = minute_bars(days)
        data = FakeData({"AAPL": bars[5:]})                                # first session misses its start
        data.bars_orig = data.bars

        def bars_more(*a):
            res = data.bars_orig(*a)
            res["has_more_left"] = True                                    # more history exists beyond
            return res
        data.bars = bars_more
        r = ind.compute(data, "AAPL", "1m", "rth", None, 55, ind.parse_list("vwap", "AAPL"))
        self.assertEqual(r["series"]["vwap"][:25], [None] * 25)
        self.assertEqual(r["series"]["vwap"][25], 110.0)

    def test_vwap_daily_refused(self):
        with self.assertRaises(BadParam):
            ind.compute(FakeData({"AAPL": daily_bars(10)}), "AAPL", "1D", None, None, 10, ind.parse_list("vwap"))

    def test_compare_overlay(self):
        a = daily_bars(40, seed=1)
        b = daily_bars(40, seed=2)
        del b[3]                                                           # a missing bar: null, not filled
        b2 = [dict(x) for x in b[:]]
        data = FakeData({"AAPL": a, "SPY": b2})
        r = ind.compute(data, "AAPL", "1D", None, None, 30, ind.parse_list("cmp:SPY", "AAPL"))
        pct, price = r["series"]["cmp:SPY:pct"], r["series"]["cmp:SPY:price"]
        base = r["indicators"][0]["base"]
        win = a[-30:]
        oc = {x["t"]: x["c"] for x in b2}
        self.assertEqual(base["t"], win[0]["t"])
        self.assertEqual(pct[0], 0.0)
        for i, w in enumerate(win):
            self.assertAlmostEqual(pct[i], (oc[w["t"]] / oc[win[0]["t"]] - 1) * 100)
            self.assertAlmostEqual(price[i], win[0]["c"] * oc[w["t"]] / oc[win[0]["t"]])
        full = ind.compute(data, "AAPL", "1D", None, None, 40, ind.parse_list("cmp:SPY", "AAPL"))
        self.assertIsNone(full["series"]["cmp:SPY:pct"][3])
        with self.assertRaises(BadParam):
            ind.compute(FakeData({"AAPL": a, "BTC-USD": a}), "AAPL", "1D", None, None, 10,
                        ind.parse_list("cmp:BTC-USD", "AAPL"))


class RouteTest(unittest.TestCase):
    def setUp(self):
        self._ts = TempStore()
        self._ts.__enter__()
        self.fs = FakeSources(lambda: NOW)
        self.svc = ResearchService(supervisor=ResearchSupervisor(), data=DataService(sources=self.fs, now=lambda: NOW))
        self.api = mr_api.ResearchApi(self.svc)

    def tearDown(self):
        self._ts.__exit__(None, None, None)

    def call(self, name, sym, *pairs):
        status, p = asyncio.run(getattr(self.api, name)(Q(pairs), None, {"symbol": sym}))
        dumps(p)
        return status, p

    def test_same_bars_as_bars_route(self):
        for tf, extra in (("1D", ()), ("15m", (("session", "eth"),)), ("1H", ())):
            with self.subTest(tf=tf):
                s1, bars = self.call("symbol_bars", "AAPL", ("tf", tf), ("limit", "60"), *extra)
                s2, r = self.call("symbol_indicators", "AAPL", ("tf", tf), ("limit", "60"),
                                  ("ind", "sma:20,rsi:14,macd" + (",vwap" if tf != "1D" else "")), *extra)
                self.assertEqual((s1, s2), (200, 200), r)
                self.assertEqual(r["t"], [b["t"] for b in bars["bars"]])
                for k, v in r["series"].items():
                    self.assertEqual(len(v), len(r["t"]), k)
                self.assertGreater(r["warmup_loaded"], 0)
                self.assertTrue(all(x is not None for x in r["series"]["sma:20:close"]))
                self.assertEqual(set(r), {"symbol", "tf", "session", "t", "series", "indicators", "has_more_left",
                                          "sip_through", "tz", "warmup_loaded", "last_final"})

    def test_validation(self):
        cases = [(("tf", "1D"),), (("tf", "1D"), ("ind", "sma:20"), ("x", "1")), (("tf", "2D"), ("ind", "sma:5")),
                 (("tf", "1D"), ("ind", "sma:0")), (("tf", "1D"), ("ind", "vwap")), (("tf", "1D"), ("ind", "rsi"), ("ind", "rsi")),
                 (("tf", "1D"), ("ind", "rsi"), ("limit", "0")), (("tf", "1D"), ("ind", "cmp:BTC-USD"))]
        for pairs in cases:
            with self.subTest(pairs=pairs):
                status, p = self.call("symbol_indicators", "AAPL", *pairs)
                self.assertEqual(status, 400, p)
        status, p = self.call("symbol_indicators", "BTC-USD", ("tf", "1D"), ("ind", "rsi"), ("session", "rth"))
        self.assertEqual(status, 400)
        status, p = self.call("symbol_indicators", "AAPL", ("tf", "1D"), ("ind", "cmp:ZZZZ"))
        self.assertEqual((status, p.get("kind")), (404, "SymbolNotCovered"))

    def test_crypto_and_compare_route(self):
        status, r = self.call("symbol_indicators", "BTC-USD", ("tf", "1H"), ("limit", "48"), ("ind", "ema:12,vwap,cmp:ETH-USD"))
        self.assertEqual(status, 200, r)
        self.assertEqual((r["session"], r["tz"]), ("utc", "UTC"))
        self.assertEqual(r["series"]["cmp:ETH-USD:pct"][0], 0.0)


if __name__ == "__main__":
    unittest.main()
