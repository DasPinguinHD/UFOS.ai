"""Stage 11 Overview API (SP §26): ``/v2/overview/{markets,risk,activity}`` — shapes, every item's unavailable path,
the 60-s cache, the active-account rule, the activity merge order and a failing source. Fakes only: no network, no
Alpaca, no real database (temp dirs; synthetic numbers)."""
import asyncio
import json
import sqlite3
import os
import shutil
import sys
import tempfile
import types
import unittest
from datetime import date, datetime, time, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy_platform.api import overview_routes as ov  # noqa: E402
from strategy_platform.core import calendars  # noqa: E402

NY = calendars.NY
UTC = timezone.utc
NOW = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)          # Thursday 10:00 New York, session open


def iso(dt):
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def num(v, label="IEX", reason=None, as_of=None):
    return {"value": v, "feed": "x", "feed_label": label, "as_of": as_of, "reason": reason}


class FakeData:
    """Stand-in for market_research.data.DataService (synthetic values)."""

    def __init__(self, fail=()):
        self.calls = 0
        self.fail = set(fail)

    def snapshot(self, symbol):
        self.calls += 1
        if symbol in self.fail:
            raise RuntimeError("source unavailable")
        last = {"SPY": 600.0, "QQQ": 500.0, "IWM": 200.0, "GLD": 300.0, "BTC-USD": 70000.0}[symbol]
        return {"last": num(last, as_of=iso(NOW - timedelta(minutes=2))),
                "prev_close": num(last / 1.01, "SIP daily close"),
                "change_pct": num(1.0 if symbol != "IWM" else None, reason="No SIP daily bar")}

    def bars(self, symbol, tf, session, before, limit):
        self.calls += 1
        if symbol == "BTC-USD":
            start = NOW - timedelta(hours=25)
            out = [{"t": iso(start + timedelta(minutes=15 * i)), "c": 68000.0 + i} for i in range(100)]
            return {"bars": [b for b in out if b["t"] < iso(NOW)]}
        prev = datetime(2026, 11, 4, 15, 55, tzinfo=NY)
        today = datetime(2026, 11, 5, 9, 30, tzinfo=NY)
        return {"bars": [{"t": iso(prev), "c": 1.0}] + [{"t": iso(today + timedelta(minutes=5 * i)), "c": 2.0 + i}
                                                        for i in range(6)]}


class Api(unittest.TestCase):
    def setUp(self):
        from strategy_platform.api import routes
        self.routes = routes
        self.api = routes.V2Api()
        self.tmp = tempfile.mkdtemp(prefix="s11ov-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.api.data_dir = self.tmp
        self.api.now_fn = lambda: NOW
        self.data = FakeData()
        self.api._ov_research_data = lambda: self.data

        async def direct(fn, *a):
            return fn(*a)
        self.api._ov_research_call = direct
        self.fred = {"DGS10": [{"date": f"2026-10-{d:02d}", "value": 4.0 + d / 100} for d in range(1, 31)] +
                              [{"date": "2026-11-03", "value": 4.10}, {"date": "2026-11-04", "value": 4.05}],
                     "DEXUSEU": [{"date": "2026-11-03", "value": 1.08}, {"date": "2026-11-04", "value": 1.0854}]}
        self.api._ov_fred_points = lambda sid: self.fred.get(sid, [])
        vix = ov._item("VIX", "VIX", "Archive", "pt", "daily")
        vix.update(available=True, value=18.0, change=0.5, provisional=True)
        self.api._ov_vix = lambda now: vix

    def call(self, name, query=None):
        status, payload = asyncio.run(getattr(self.api, name)(query or {}, self.routes.NO_BODY, {}))
        json.loads(self.routes.dumps(payload))
        return status, payload


class MarketsTest(Api):
    def test_routes_registered_get_only(self):
        got = {p: m for m, p, _n, _b in self.routes.ROUTES if p.startswith("/v2/overview/")}
        self.assertEqual(got, {"/v2/overview/markets": "GET", "/v2/overview/risk": "GET",
                               "/v2/overview/activity": "GET"})

    def test_shape_order_and_values(self):
        st, p = self.call("overview_markets")
        self.assertEqual(st, 200)
        self.assertEqual([i["key"] for i in p["items"]], ["SPY", "QQQ", "IWM", "GLD", "BTC", "US10Y", "EURUSD", "VIX"])
        self.assertEqual(p["items"][3]["label"], "Gold (GLD)")
        keys = {"key", "label", "available", "reason", "value", "change", "change_unit", "series", "source", "as_of",
                "age_minutes", "session", "note"}
        for it in p["items"]:
            self.assertTrue(keys <= set(it), it["key"])
        spy = p["items"][0]
        self.assertEqual((spy["value"], spy["change"], spy["source"], spy["session"]), (600.0, 1.0, "Alpaca", "open"))
        self.assertEqual(spy["age_minutes"], 2.0)
        self.assertEqual([x["v"] for x in spy["series"]], [2.0, 3.0, 4.0, 5.0, 6.0, 7.0])   # today's session only
        iwm = p["items"][2]
        self.assertTrue(iwm["available"])
        self.assertIsNone(iwm["change"])
        self.assertIn("change not available (No SIP daily bar)", iwm["note"])
        btc = p["items"][4]
        # bar i starts at NOW−25h+15i min; the 24-h reference = the bar ending at NOW−24h (i = 3, close 68003)
        self.assertAlmostEqual(btc["change"], (70000.0 / 68003.0 - 1) * 100)
        self.assertIn("24 h", btc["note"])
        self.assertTrue(all(x["t"] >= iso(NOW - timedelta(hours=24)) for x in btc["series"]))

    def test_btc_reference_must_be_near_24h(self):
        real = self.data.bars

        def gappy(symbol, *a):
            out = real(symbol, *a)
            if symbol == "BTC-USD":       # drop the bars ending within 15 min before now − 24 h (and the one before)
                cut = iso(NOW - timedelta(hours=24))
                out = {"bars": [b for b in out["bars"]
                                if not (iso(NOW - timedelta(hours=25)) < b["t"] < cut)]}
            return out
        self.data.bars = gappy
        st, p = self.call("overview_markets")
        btc = p["items"][4]
        self.assertTrue(btc["available"])
        self.assertIsNone(btc["change"])                             # the 25-h-old close is not a 24-h reference
        self.assertIn("change over 24 h not available", btc["note"])
        us10 = p["items"][5]
        self.assertEqual((us10["source"], us10["session"], us10["as_of"]), ("FRED", "daily", "2026-11-04"))
        self.assertAlmostEqual(us10["change"], -5.0)                 # 4.10 → 4.05 = −5 bp
        self.assertEqual(us10["change_unit"], "bp")
        self.assertEqual(len(us10["series"]), 30)
        eur = p["items"][6]
        self.assertAlmostEqual(eur["change"], 0.5)                   # 1.08 → 1.0854 = +0.5 %
        self.assertTrue(p["items"][7]["provisional"])

    def test_cache_60s(self):
        clock = [1000.0]
        with mock.patch.object(ov.time, "monotonic", lambda: clock[0]):
            st, p1 = self.call("overview_markets")
            n = self.data.calls
            st, p2 = self.call("overview_markets")
            self.assertEqual(self.data.calls, n)                     # served from the cache
            self.assertTrue(p2["cached"])
            self.assertFalse(p1["cached"])
            clock[0] += 61
            self.call("overview_markets")
            self.assertEqual(self.data.calls, 2 * n)

    def test_failed_items_and_reasons(self):
        self.data.fail = {"QQQ", "BTC-USD"}
        self.fred = {}
        st, p = self.call("overview_markets")
        by = {i["key"]: i for i in p["items"]}
        self.assertTrue(by["SPY"]["available"])
        for k in ("QQQ", "BTC"):
            self.assertFalse(by[k]["available"])
            self.assertIsNone(by[k]["value"])
            self.assertIn("RuntimeError (details in backend.log)", by[k]["reason"])   # no raw exception text
        for k in ("US10Y", "EURUSD"):
            self.assertFalse(by[k]["available"])
            self.assertIn("no stored FRED observations of", by[k]["reason"])
            self.assertIn("first poll pending or the fetch failed", by[k]["reason"])
        self.api._ov_markets_cache = None
        self.api._ov_research_data = lambda: None
        st, p = self.call("overview_markets")
        for it in p["items"][:5]:
            self.assertFalse(it["available"])
            self.assertIn("not installed", it["reason"])

    def test_unknown_param(self):
        self.assertEqual(self.call("overview_markets", {"x": "1"})[0], 400)


class VixTest(unittest.TestCase):
    def rows(self, spec):
        """spec: (slot NY date, hh, mm, VIX date or None, value)."""
        out = []
        for d, hh, mm, vd, v in spec:
            ms = int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=NY).timestamp() * 1000)
            out.append({"valid_for_ms": ms, "provisional": True,
                        "payload": {"vix_structure": {"vix": None if vd is None else
                                                      {"date": vd, "value": v, "available_ms": 1},
                                                      "reason": None if vd else "VIXCLS / VXVCLS not archived"}}})
        return out

    def run_item(self, rows):
        from strategy_platform.options_structure import store
        now = datetime(2026, 11, 5, 11, 0, tzinfo=NY).astimezone(UTC)
        with mock.patch.object(store, "latest", lambda *a, **k: rows[-1] if rows else None), \
                mock.patch.object(store, "history", lambda *a, **k: rows):
            return ov.vix_item(("cfg", "acfg", "hash", False, [], {}), object(), now)

    def test_daily_value_change_series(self):
        d3, d4, d5 = date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5)
        it = self.run_item(self.rows([(d3, 10, 0, "2026-10-31", 16.0), (d4, 10, 0, "2026-11-02", 17.0),
                                      (d4, 15, 25, "2026-11-03", 17.5),       # FRED published during the day
                                      (d5, 10, 0, "2026-11-04", 18.2), (d5, 10, 30, "2026-11-04", 18.2)]))
        self.assertTrue(it["available"])
        self.assertEqual((it["value"], it["as_of"], it["session"]), (18.2, "2026-11-04", "daily"))
        self.assertAlmostEqual(it["change"], 0.7)                    # vs the close 2026-11-03 (17.5)
        self.assertEqual(it["series"], [{"t": "2026-10-31", "v": 16.0}, {"t": "2026-11-02", "v": 17.0},
                                        {"t": "2026-11-03", "v": 17.5}, {"t": "2026-11-04", "v": 18.2}])
        self.assertEqual(it["slot_age_minutes"], 30.0)
        self.assertTrue(it["provisional"])
        self.assertIn("change vs the close 2026-11-03", it["note"])

    def test_same_date_never_compared(self):
        d4, d5 = date(2026, 11, 4), date(2026, 11, 5)
        it = self.run_item(self.rows([(d4, 15, 25, "2026-11-04", 17.9), (d5, 10, 0, "2026-11-04", 18.0)]))
        self.assertTrue(it["available"])
        self.assertIsNone(it["change"])                              # both slots hold the 11-04 close
        self.assertIn("no archived slot with an earlier VIX date", it["note"])
        self.assertEqual(it["series"], [{"t": "2026-11-04", "v": 18.0}])   # one point per date, newest slot

    def test_missing_previous(self):
        it = self.run_item(self.rows([(date(2026, 11, 5), 10, 0, "2026-11-04", 18.0)]))
        self.assertTrue(it["available"])
        self.assertIsNone(it["change"])
        self.assertIn("change not available", it["note"])

    def test_unavailable(self):
        self.assertIn("has not run", ov.vix_item(None, None, NOW)["reason"])
        it = self.run_item([])
        self.assertFalse(it["available"])
        it = self.run_item(self.rows([(date(2026, 11, 5), 10, 0, None, None)]))
        self.assertFalse(it["available"])
        self.assertIn("has no VIX", it["reason"])
        self.assertEqual(it["slot_age_minutes"], 60.0)


def eod_points(n_days, e0=100000.0, r=0.001):
    days = calendars.trading_days(date(2026, 3, 2), date(2026, 11, 4))[-(n_days):]
    pts, e = [], e0
    for d in days:
        pts.append({"ts": datetime.combine(d, time(16, 5), tzinfo=NY).astimezone(UTC).isoformat(), "equity": e,
                    "long_mv": 0, "short_mv": 0})
        e *= 1 + r
    return pts


class RiskTest(Api):
    def test_legacy_shape(self):
        self.api._ov_risk_inputs = lambda: {"account": "legacy", "account_ids": ["old-engine"],
                                            "points": eod_points(30), "equity": 100000.0, "long_mv": 50000.0,
                                            "short_mv": 10000.0, "exposure_reason": None, "series_reason": None}
        st, p = self.call("overview_risk")
        self.assertEqual(st, 200)
        self.assertEqual(p["account"], "legacy")
        self.assertEqual(p["n_daily_returns"], 29)
        self.assertEqual(list(p["figures"]), ["var99_1d", "es975_1d", "net_exposure_pct", "gross_exposure_pct",
                                              "max_drawdown_pct", "vol20_ann_pct"])
        for f in p["figures"].values():
            self.assertEqual(set(f), {"value", "unit", "available", "reason", "method"})
        self.assertEqual(p["figures"]["var99_1d"]["reason"], "needs ≥ 60 daily returns, have 29")
        self.assertAlmostEqual(p["figures"]["net_exposure_pct"]["value"], 40.0)
        self.assertAlmostEqual(p["figures"]["gross_exposure_pct"]["value"], 60.0)
        self.assertEqual(p["figures"]["max_drawdown_pct"]["value"], 0.0)

    def test_inputs_fail(self):
        def boom():
            raise RuntimeError("db locked")
        self.api._ov_risk_inputs = boom
        st, p = self.call("overview_risk")
        self.assertEqual(st, 200)
        self.assertTrue(all(not f["available"] and "RuntimeError" in f["reason"] and "db locked" not in f["reason"]
                            for f in p["figures"].values()))
        self.assertEqual(self.call("overview_risk", {"a": "1"})[0], 400)

    def test_risk_inputs_legacy_and_platform(self):
        from strategy_platform.execution import accounts, control, migration
        from strategy_platform.portfolio import views

        class Store:
            def __init__(self, pts):
                self.pts = pts

            def get_reset_at(self):
                return None

            def equity_points(self, since=None, limit=0):
                return self.pts

        class Eng:
            store = Store(eod_points(5))
            acct = {"equity": 1000.0, "long_mv": 500.0, "short_mv": 0.0, "day_pnl": -12.5, "total_pnl": 40.0,
                    "unreadable": False}

            def state(self):
                return {"now": "2026-11-05T15:00:00Z", "account": dict(self.acct)}

        eng = Eng()
        with mock.patch.object(control, "legacy_engine", lambda: eng), \
                mock.patch.object(migration, "legacy_state", lambda e=None: {"status": "active"}):
            inp = ov.risk_inputs(NOW)
            self.assertEqual((inp["account"], inp["equity"], len(inp["points"])), ("legacy", 1000.0, 5))
            self.assertEqual(inp["summary"], {"ids": ["old-engine"], "n": 1, "available": True, "equity": 1000.0,
                                              "day_pnl": -12.5, "total_pnl": 40.0, "as_of": "2026-11-05T15:00:00Z",
                                              "summed": False, "currency": "USD", "reason": None})
            eng.acct = {"unreadable": True, "error": "Valuation paused: no quote for SPY"}
            sm = ov.risk_inputs(NOW)["summary"]
            self.assertEqual((sm["available"], sm["equity"], sm["reason"]),
                             (False, None, "Valuation paused: no quote for SPY"))
        with mock.patch.object(control, "legacy_engine", lambda: None), \
                mock.patch.object(migration, "legacy_state", lambda e=None: {"status": "migrating"}):
            inp = ov.risk_inputs(NOW)
        self.assertEqual(inp["account"], "legacy")
        self.assertIn("not running", inp["series_reason"])
        self.assertFalse(inp["summary"]["available"])

        from strategy_platform.core.types import AccountView

        class Acc:
            def __init__(self, aid, broker="internal_paper", eq=1000.0, day=5.0, total=50.0):
                self.id, self.broker_name = aid, broker
                self.store = Store(eod_points(3))
                self.broker = self
                self.eq, self.day, self.total = eq, day, total

            def account(self, pm):
                return AccountView(equity=self.eq, cash=0, long_mv=300.0, short_mv=100.0, buying_power=0,
                                   peak_equity=0, drawdown_pct=0, maintenance_required=0, margin_excess=0,
                                   short_exposure_pct=0, starting_capital=0, day_pnl=self.day, total_pnl=self.total)

        class Mirror(Acc):
            is_broker = True

        everything = [Acc("paper-main"), Acc("paper-council", eq=2000.0, day=-7.5, total=-20.0),
                      Mirror("alpaca-paper", "alpaca_paper"), Mirror("binance-demo", "binance_demo"),
                      Mirror("kraken-demo", "kraken_demo")]
        with mock.patch.object(control, "legacy_engine", lambda: None), \
                mock.patch.object(migration, "legacy_state", lambda e=None: {"status": "migrated"}), \
                mock.patch.object(views, "quotes_for", lambda a: {}):
            # the helper: internal paper ledgers in, every broker mirror out
            with mock.patch.object(accounts, "all_accounts", lambda: everything):
                aset = ov.active_account_set()
                self.assertEqual(aset["mode"], "platform")
                self.assertEqual([a.id for a in aset["accounts"]], ["paper-main", "paper-council"])
                self.assertEqual(aset["excluded"], ["alpaca-paper", "binance-demo", "kraken-demo"])
                inp = ov.risk_inputs(NOW)
            self.assertEqual((inp["account_ids"], inp["excluded_ids"]),
                             (["paper-main", "paper-council"], ["alpaca-paper", "binance-demo", "kraken-demo"]))
            self.assertEqual((inp["equity"], inp["long_mv"], inp["short_mv"]), (3000.0, 600.0, 200.0))
            self.assertEqual(inp["summary"], {"ids": ["paper-main", "paper-council"], "n": 2, "available": True,
                                              "equity": 3000.0, "day_pnl": -2.5, "total_pnl": 30.0,
                                              "as_of": "2026-11-05T15:00:00Z", "summed": True, "currency": "USD",
                                              "reason": None})
            self.assertEqual(inp["points"], [])                      # series never summed
            self.assertTrue(inp["series_reason"].startswith("no platform equity history yet"))
            # a not-internal id is out even without the mirror attribute (e.g. an unknown future broker)
            with mock.patch.object(accounts, "all_accounts", lambda: [Acc("paper-council"), Acc("alpaca-paper")]):
                inp = ov.risk_inputs(NOW)
            self.assertEqual((inp["account_ids"], len(inp["points"])), (["paper-council"], 3))
            self.assertEqual((inp["summary"]["n"], inp["summary"]["summed"]), (1, False))

            class Broken(Acc):
                def account(self, pm):
                    raise sqlite3.OperationalError("disk I/O error at C:\\x")
            with mock.patch.object(accounts, "all_accounts", lambda: [Acc("paper-main"), Broken("paper-council")]):
                inp = ov.risk_inputs(NOW)
            self.assertIsNone(inp["equity"])
            why = "paper-council: account unreadable (OperationalError (details in backend.log))"
            self.assertEqual(inp["exposure_reason"], why)
            self.assertEqual((inp["summary"]["available"], inp["summary"]["equity"], inp["summary"]["day_pnl"],
                              inp["summary"]["reason"]), (False, None, None, why))      # no partial sum
            with mock.patch.object(accounts, "all_accounts", lambda: [Acc("paper-main"), Acc("paper-council", day=None)]):
                sm = ov.risk_inputs(NOW)["summary"]
            self.assertEqual((sm["available"], sm["total_pnl"], sm["reason"]),
                             (False, None, "paper-council: day_pnl not available"))
            with mock.patch.object(accounts, "all_accounts", lambda: []):
                inp = ov.risk_inputs(NOW)
            self.assertEqual(inp["series_reason"], "no platform internal paper account is open")
            self.assertEqual(inp["summary"]["n"], 0)

    def test_route_carries_summary(self):
        sm = ov._summary(["old-engine"], 1, False, "2026-11-05T15:00:00Z",
                         {"equity": 1.0, "day_pnl": 0.0, "total_pnl": 0.0})
        self.api._ov_risk_inputs = lambda: {"account": "legacy", "account_ids": ["old-engine"], "excluded_ids": [],
                                            "points": [], "equity": 1.0, "long_mv": 0.0, "short_mv": 0.0,
                                            "exposure_reason": None, "series_reason": "x", "summary": sm}
        st, p = self.call("overview_risk")
        self.assertEqual((p["account_summary"], p["excluded_ids"]), (sm, []))


class ActivityTest(Api):
    def test_merge_order_limit_and_failing_source(self):
        def a(n):
            return [ov._act("2026-11-05T10:00:00+00:00", "order", "A1", "d", "info", "trading/orders"),
                    ov._act("2026-11-05T12:00:00Z", "order", "A2", None, "info", "trading/orders")]

        def b(n):
            return [ov._act(datetime(2026, 11, 5, 11, 0, tzinfo=UTC), "alert", "B1", "x" * 1000, "error",
                            "trading/monitoring"), ov._act("not a time", "alert", "B2", None, "info", "x")]

        def c(n):
            raise ov.SourceUnavailable("the options archive has no database yet")

        def d(n):
            raise OSError("C:\\Users\\someone\\secret path")
        self.api._ov_activity_sources = lambda: [("a", a), ("b", b), ("c", c), ("d", d)]
        st, p = self.call("overview_activity")
        self.assertEqual(st, 200)
        self.assertEqual([i["title"] for i in p["items"]], ["A2", "B1", "A1"])
        self.assertEqual(p["items"][0]["ts"], "2026-11-05T12:00:00Z")
        self.assertEqual(len(p["items"][1]["detail"]), ov.DETAIL_MAX)
        self.assertEqual(p["sources"], {"a": "ok", "b": "ok",
                                        "c": "not available (the options archive has no database yet)",
                                        "d": "not available (OSError (details in backend.log))"})
        st, p = self.call("overview_activity", {"limit": "2"})
        self.assertEqual([i["title"] for i in p["items"]], ["A2", "B1"])
        for bad in ("0", "101", "x"):
            self.assertEqual(self.call("overview_activity", {"limit": bad})[0], 400)
        self.assertEqual(self.call("overview_activity", {"y": "1"})[0], 400)

    def test_real_source_list(self):
        names = [n for n, _f in self.api._ov_activity_sources()]
        self.assertEqual(names, ["oms_orders", "fills", "alerts", "loops", "options_archive", "council",
                                 "old_engine"])
        with self.assertRaises(ov.SourceUnavailable):                # temp data dir: no archive yet
            dict(self.api._ov_activity_sources())["options_archive"](5)

    def test_alert_and_engine_sources(self):
        from strategy_platform.monitoring import alerts
        fake = {"alerts": [{"ts": "2026-11-05T09:00:00+00:00", "severity": "critical", "title": "Kill switch",
                            "body": "line1\nline2"}]}
        with mock.patch.object(alerts, "list_alerts", lambda after, limit: fake):
            got = ov.src_alerts(5)
        self.assertEqual(got[0]["severity"], "error")
        self.assertEqual(got[0]["detail"], "line1 line2")
        self.assertEqual(got[0]["link"], "trading/monitoring")
        from strategy_platform.execution import control

        class EngStore:
            rows = None

            def events_latest(self, n):
                return self.rows

        class Eng:
            store = EngStore()
        eng = Eng()
        with mock.patch.object(control, "legacy_engine", lambda: None):
            with self.assertRaises(ov.SourceUnavailable):           # no old engine registered
                ov.src_old_engine(5)
        with mock.patch.object(control, "legacy_engine", lambda: eng):
            with self.assertRaises(ov.SourceUnavailable):           # store disabled / unreadable → None
                ov.src_old_engine(5)
            eng.store.rows = [{"ts": "2026-11-05T08:00:00+00:00", "kind": "halt", "title": "Halted", "symbol": None,
                               "body": "drawdown"}]
            got = ov.src_old_engine(5)
        self.assertEqual((got[0]["kind"], got[0]["severity"], got[0]["link"]), ("engine:halt", "warning",
                                                                                 "trading/legacy"))


try:
    from sp_trading_testkit import FRI, SAT, TradingCase
except ImportError:      # pragma: no cover
    from tests.sp_trading_testkit import FRI, SAT, TradingCase


class SourcesDbTest(TradingCase):
    """The activity readers against a real temp hedgefund.db / options archive (synthetic rows)."""

    def test_oms_orders_and_fills(self):
        filled = self.manual("SPY", "buy", 10)                       # internal paper: new → submitted → filled
        self.assertEqual(filled["state"], "filled")
        self.set_now(SAT)
        rej = self.manual("SPY", "buy", 5)                           # outside trading hours → rejected
        self.assertEqual(rej["state"], "rejected")
        got = ov.src_oms_orders(20)
        titles = [g["title"] for g in got]
        self.assertIn("Order rejected: buy SPY", titles)
        self.assertEqual(titles.count("Order placed: buy SPY"), 2)
        self.assertFalse(any("filled" in t for t in titles))         # internal fill → the fills source only
        self.assertEqual(next(g for g in got if "rejected" in g["title"])["severity"], "warning")
        self.assertEqual(len(ov.src_oms_orders(1)), 1)               # limit applied in SQL
        fills = ov.src_fills(20)
        self.assertEqual(len(fills), 1)
        self.assertEqual((fills[0]["kind"], fills[0]["title"], fills[0]["link"]), ("fill", "Fill: buy SPY",
                                                                                    "trading/orders"))
        self.assertIn("paper-main · buy 10 SPY @", fills[0]["detail"])

    def test_first_event_rejected_is_labelled_rejected(self):
        from strategy_platform.storage import db
        with db._connect() as c:
            c.execute("INSERT INTO oms_orders (client_order_id, account_id, created_at, updated_at, symbol, side, qty, "
                      "origin, tif, state, reject_reason) VALUES ('cid-x', 'paper-main', '2026-10-02T15:00:00+00:00',"
                      " '2026-10-02T15:00:00+00:00', 'QQQ', 'sell', 1, 'manual', 'day', 'rejected', 'no position')")
            c.execute("INSERT INTO oms_order_events (client_order_id, ts, from_state, to_state) VALUES ('cid-x', "
                      "'2026-10-02T15:00:01+00:00', NULL, 'rejected')")
        got = ov.src_oms_orders(5)
        self.assertEqual(got[0]["title"], "Order rejected: sell QQQ")
        self.assertIn("no position", got[0]["detail"])

    def test_loops(self):
        from strategy_platform.storage import db
        for lid, ev in (("lr-done1", "done"), ("lr-run1", None), ("lr-fail1", "failed")):
            db.s5b_insert("loop_runs", {"id": lid, "seed_kind": "idea", "seed_json": json.dumps({"text": "x"}),
                                        "config_json": "{}", "created_at": "2026-10-01T10:00:00+00:00"})
            db.s5b_insert("loop_events", {"loop_id": lid, "event": "created", "data_json": "{}",
                                          "created_at": "2026-10-01T10:00:00+00:00"})
            if ev:
                db.s5b_insert("loop_events", {"loop_id": lid, "event": ev, "data_json": json.dumps({"message": "m"}),
                                              "created_at": f"2026-10-01T1{'1' if ev == 'done' else '2'}:00:00+00:00"})
        got = ov.src_loops(10)
        self.assertEqual({g["title"] for g in got}, {"Loop done: lr-done1", "Loop failed: lr-fail1"})
        by = {g["title"]: g for g in got}
        self.assertEqual(by["Loop done: lr-done1"]["ts"], "2026-10-01T11:00:00Z")      # the terminal event
        self.assertEqual(by["Loop failed: lr-fail1"]["severity"], "error")
        self.assertEqual(by["Loop done: lr-done1"]["link"], "strategies/loop")

    def test_council(self):
        from strategy_platform.council import store as cs
        r1 = cs.create_run("scheduled", datetime(2026, 10, 2, 14, 0, tzinfo=UTC), 1, "open", False, False)
        cs.finish_run(r1, datetime(2026, 10, 2, 14, 5, tzinfo=UTC), "error", "rules", 0.0123, None)
        cs.create_run("manual", datetime(2026, 10, 2, 15, 0, tzinfo=UTC), 1, "open", False, False)
        got = ov.src_council(10)
        self.assertEqual([g["title"] for g in got], [f"Council run #{r1 + 1} running", f"Council run #{r1} error"])
        self.assertEqual(got[1]["ts"], "2026-10-02T14:05:00Z")
        self.assertEqual((got[1]["severity"], got[1]["link"]), ("warning", "trading/legacy"))
        self.assertIn("AI cost $0.0123", got[1]["detail"])

    def test_archive(self):
        try:
            from os2_testkit import Archive, ny_ms
        except ImportError:      # run as tests.test_s11_overview_api
            from tests.os2_testkit import Archive, ny_ms
        from strategy_platform.options_archive import config as oconfig
        a = Archive(oconfig.paths(self.tmp)["db"])
        d = date(2026, 10, 1)
        for hh, st in ((10, "ok"), (11, "failed"), (12, "ok"), (13, "partial"), (14, "late")):
            a.run("intraday", ny_ms(d, hh, 0), status=st, label=f"{hh}:00")
        a.conn.commit()
        a.conn.close()
        got = ov.src_archive(10, self.tmp)
        self.assertEqual([g["title"] for g in got], ["Options archive run partial", "Options archive run ok",
                                                     "Options archive run failed"])
        self.assertEqual([g["severity"] for g in got], ["warning", "info", "error"])
        self.assertIn("12:00", got[1]["detail"])                     # only the NEWEST ok run


class StoredPointsTest(unittest.TestCase):
    def test_snapshot_copy(self):
        with mock.patch.dict(sys.modules):           # a stub aiohttp only where it is not installed (VM); restored
            try:
                import aiohttp  # noqa: F401
            except ImportError:
                stub = types.ModuleType("aiohttp")
                stub.ClientSession, stub.TCPConnector, stub.ClientTimeout = object, object, (lambda **kw: None)
                sys.modules["aiohttp"] = stub
            from newsroom import fred_collector as fred
            self._check(fred)

    def _check(self, fred):
        obs = {("DEXUSEU", "lin"): {"2026-11-04": 1.0854, "2026-11-03": 1.08}, ("ICSA", "lin"): {"2026-11-01": 231000.0}}
        with mock.patch.object(fred, "_OBS", obs):
            pts = fred.stored_points("DEXUSEU")
            self.assertEqual(pts, [{"date": "2026-11-03", "value": 1.08}, {"date": "2026-11-04", "value": 1.0854}])
            pts.append({"date": "x", "value": 0})
            self.assertEqual(len(obs[("DEXUSEU", "lin")]), 2)        # a copy, never the live dict
            self.assertEqual(fred.stored_points("ICSA"), [{"date": "2026-11-01", "value": 231.0}])   # card scale
            self.assertEqual(fred.stored_points("DGS10"), [])
        self.assertIn(("DEXUSEU", None), fred.fetch_specs())


if __name__ == "__main__":
    unittest.main()
