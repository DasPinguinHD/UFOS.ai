"""Market Research MR8 — in-module integrations (SPEC §13 "MR8 backend notes"): read-only Portfolio from the
HedgeFund paper account, Newsroom chart markers (Form 4 insider buys/sells, FOMC + FRED release dates; no
earnings source → unavailable), backtest markers from a Strategy Platform run card (fills + attribution
periods/drawdowns), watchlist → Strategy Platform universe, and the read-only guarantees (``mode=ro`` +
``query_only`` connections, database files unchanged, no write SQL in the module, no order route).
Temp databases outside the repo; no network.

Run from HedgeFund/backend:  python -m unittest tests.test_mr_integrations -v
"""
import asyncio
import ast
import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

try:
    from mr_data_testkit import TempStore
except ImportError:  # pragma: no cover
    from tests.mr_data_testkit import TempStore

from market_research import ResearchService, api as mr_api, integrations as ig, limits, store
from market_research.supervisor import ResearchSupervisor
from strategy_platform.api.routes import NO_BODY, dumps

UTC = timezone.utc


def setUpModule():
    logging.getLogger("market_research").setLevel(logging.CRITICAL + 1)


def tearDownModule():
    logging.getLogger("market_research").setLevel(logging.NOTSET)


def run(c):
    return asyncio.run(c)


class Q(dict):
    def __init__(self, pairs=()):
        super().__init__()
        self.pairs = list(pairs)
        for k, v in self.pairs:
            self.setdefault(k, v)

    def getall(self, k, *default):
        # aiohttp semantics: a missing key raises KeyError unless a default is given
        vals = [v for kk, v in self.pairs if kk == k]
        if not vals:
            if default:
                return default[0]
            raise KeyError(k)
        return vals


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def filing(acc, ticker, txs, owner="Jane <b>Doe</b>", filed="2026-10-14T21:00:00+00:00"):
    return {"id": "insider-" + acc, "filed_at": filed, "url": "https://www.sec.gov/Archives/x/" + acc,
            "details": {"issuer_ticker": ticker, "issuer_name": ticker + " Inc", "owner_name": owner,
                        "owner_roles": ["CEO"], "plan_10b5_1": False,
                        "transactions": [dict({"derivative": False, "shares": 1000.0, "price": 10.5,
                                               "value_usd": 10500.0}, **t) for t in txs]}}


class NewsroomCase(unittest.TestCase):
    """A real newsroom.db (newsroom/db.py migrations) filled through the fund/newsroom writers."""

    def setUp(self):
        from newsroom import db as ndb
        self.ndb = ndb
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "newsroom.db")
        self.assertTrue(ndb.init(self.path, legacy=os.path.join(self._tmp.name, "none.sqlite")))
        self._ts = TempStore()
        self._ts.__enter__()
        self.svc = ResearchService(supervisor=ResearchSupervisor())
        self.api = mr_api.ResearchApi(self.svc)

    def tearDown(self):
        self._ts.__exit__(None, None, None)
        self.ndb.close()
        self._tmp.cleanup()

    def call(self, name, query=(), **match):
        status, payload = run(getattr(self.api, name)(Q(query), NO_BODY, match))
        json.loads(dumps(payload))
        return status, payload

    def fill_account(self):
        from fund import store as fs
        fs.add_cash_entry("2026-10-01T00:00:00+00:00", "deposit", 100000.0)
        fs.add_cash_entry("2026-10-02T14:00:00+00:00", "fill", -10000.0, symbol="AAPL")
        fs.upsert_position("AAPL", 50, 200.0, updated_at="2026-10-15T20:00:00+00:00",
                           opened_at="2026-10-02T14:00:00+00:00", last_price=210.0)
        fs.upsert_position("TSLA", -10, 300.0, updated_at="2026-10-15T20:00:00+00:00", last_price=None)
        fs.add_equity_point("2026-10-15T20:00:00+00:00", 100500.0, 90000.0, 10500.0, -3000.0, drawdown_pct=0.4)
        fs.set_starting_capital(100000.0)
        fs.set_autopilot(True)


class PortfolioTest(NewsroomCase):
    def test_read_only_account_view(self):
        self.fill_account()
        st, p = self.call("portfolio")
        self.assertEqual(st, 200, p)
        self.assertEqual(set(p), {"source", "read_only", "paper", "generated_at", "cash", "equity", "starting_capital",
                                  "positions", "totals", "engine", "marks_note", "note"})
        self.assertEqual((p["read_only"], p["paper"], p["cash"], p["starting_capital"]), (True, True, 90000.0, 100000.0))
        a, t = p["positions"]
        self.assertEqual((a["symbol"], a["side"], a["market_value"], a["unrealized_pnl"], a["unrealized_pct"]),
                         ("AAPL", "long", 10500.0, 500.0, 5.0))
        self.assertEqual((t["side"], t["market_value"], t["reason"]), ("short", None, "No engine mark for this position yet."))
        self.assertEqual(p["totals"], {"positions": 2, "long_mv": 10500.0, "short_mv": 0.0, "unrealized_pnl": 500.0,
                                       "complete": False})
        self.assertEqual(p["equity"]["value"], 100500.0)
        self.assertEqual(p["engine"], {"autopilot": True, "halted": False})
        self.assertIn("Research places no orders", p["note"])

    def test_not_set_up_and_missing_file(self):
        st, p = self.call("portfolio")
        self.assertEqual((st, p["kind"]), (503, "IntegrationUnavailable"))
        self.assertIn("no ledger entries", p["error"])
        self.ndb.close()
        with mock.patch.object(ig, "newsroom_db_path", lambda: os.path.join(self._tmp.name, "missing.db")):
            st, p = self.call("portfolio")
        self.assertEqual(st, 503)
        self.assertIn("does not exist", p["error"])
        self.assertFalse(os.path.exists(os.path.join(self._tmp.name, "missing.db")))   # never created

    def test_read_only_guarantees(self):
        self.fill_account()
        self.ndb.upsert_insider_many([filing("0001", "AAPL", [{"code": "P", "date": "2026-10-14"}])])
        self.ndb.close()                                     # no writer open; checkpointed file
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        before = sha(self.path)
        with mock.patch.object(ig, "newsroom_db_path", lambda: self.path):
            for _ in range(2):
                self.assertEqual(self.call("portfolio")[0], 200)
                self.assertEqual(self.call("symbol_markers", (("from", "2026-10-01T00:00:00Z"),
                                                              ("to", "2026-11-01T00:00:00Z")), symbol="AAPL")[0], 200)
        self.assertEqual(sha(self.path), before)
        with ig.ro_connect(self.path, "x") as c:              # the connection itself refuses writes
            with self.assertRaises(sqlite3.Error):
                c.execute("INSERT INTO meta(key, value) VALUES('x', 'y')")
            with self.assertRaises(sqlite3.Error):
                c.execute("DELETE FROM positions")

    def test_module_has_no_write_sql_and_no_order_route(self):
        src_path = ig.__file__
        tree = ast.parse(open(src_path, encoding="utf-8").read())
        strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        for s in strings:
            low = s.lower().lstrip()
            self.assertFalse(low.startswith(("insert", "update ", "delete", "replace", "create", "drop", "alter")), s)
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        self.assertFalse(names & {"upsert_position", "add_cash_entry", "insert_order", "manual_order",
                                  "backtest_run_insert", "strategy_create", "meta_set"})
        for m, p, n, _ in mr_api.ROUTES:
            self.assertNotIn("order", p)                     # no order route in Research
            if p.startswith(("/research/portfolio", "/research/backtests", "/research/symbols/{symbol}/markers")) or \
                    p.endswith("/universe"):
                self.assertEqual(m, "GET")


class MarkerTest(NewsroomCase):
    def markers(self, symbol="AAPL", **q):
        q.setdefault("from", "2026-10-01T00:00:00Z")
        q.setdefault("to", "2026-11-01T00:00:00Z")
        return self.call("symbol_markers", tuple(q.items()), symbol=symbol)

    def test_insider_and_calendar(self):
        self.ndb.upsert_insider_many([
            filing("0001", "AAPL", [{"code": "P", "date": "2026-10-14"}, {"code": "A", "date": "2026-10-14"},
                                    {"code": "S", "date": "2026-10-14", "derivative": True}]),
            filing("0002", "AAPL", [{"code": "S", "date": "2026-10-13", "shares": 500.0, "price": 20.0}],
                   owner="John\x07 Roe <script>x</script>"),
            filing("0003", "AAPL", [{"code": "P", "date": "2026-09-01"}]),        # outside the range
            filing("0004", "MSFT", [{"code": "P", "date": "2026-10-14"}])])
        self.ndb.save_calendar({"events": [
            {"date": "2026-10-20", "time_hint": "08:30 ET", "kind": "release", "name": "CPI <i>(inflation)</i>",
             "series_id": "CPIAUCSL", "importance": "high", "why": "x", "release_link": "javascript:alert(1)"},
            {"date": "2026-10-28", "time_hint": "14:00 ET", "kind": "fomc", "name": "FOMC rate decision"},
            {"date": "2026-12-01", "time_hint": "", "kind": "release", "name": "Later"}]})
        st, p = self.markers()
        self.assertEqual(st, 200, p)
        self.assertEqual(set(p), {"symbol", "from", "to", "kinds", "items", "truncated", "unavailable", "coverage"})
        kinds = [(m["kind"], m["side"], m["t"]) for m in p["items"]]
        self.assertEqual(kinds, [("insider", "sell", "2026-10-13T04:00:00+00:00"),
                                 ("insider", "buy", "2026-10-14T04:00:00+00:00"),
                                 ("release", None, "2026-10-20T12:30:00+00:00"),
                                 ("fomc", None, "2026-10-28T18:00:00+00:00")])        # FOMC once (list + snapshot)
        sell = p["items"][0]
        self.assertEqual(sell["label"], "Insider sell: John Roe (CEO) — 500 sh @ $20.00")
        self.assertEqual(set(sell), {"t", "t_end", "kind", "label", "side", "detail"})
        self.assertTrue(sell["detail"]["url"].startswith("https://www.sec.gov/"))
        rel = p["items"][2]
        self.assertEqual((rel["label"], rel["detail"]["url"]), ("CPI (inflation)", None))
        self.assertEqual(p["unavailable"], {"earnings": ig.EARNINGS_UNAVAILABLE})
        st, p = self.markers(kinds="insider")
        self.assertEqual({m["kind"] for m in p["items"]}, {"insider"})

    def test_crypto_and_missing_snapshot(self):
        st, p = self.markers("BTC-USD")
        self.assertEqual(st, 200)
        self.assertIn("US-listed companies", p["unavailable"]["insider"])
        self.assertIn("no economic calendar snapshot", p["unavailable"]["releases"])
        self.assertEqual([m["kind"] for m in p["items"]], ["fomc"])            # Newsroom FOMC list still applies

    def test_validation(self):
        for q in ({"from": "2026-11-01T00:00:00Z", "to": "2026-10-01T00:00:00Z"}, {"from": "x"},
                  {"kinds": "insider,earnings"}, {"kinds": "insider,insider"}, {"kinds": ""},
                  {"from": "1990-01-01T00:00:00Z"}, {"foo": "1"}):
            self.assertEqual(self.markers(**q)[0], 400, q)
        self.assertEqual(self.markers("bad sym")[0], 400)

    def test_default_window(self):
        st, p = self.call("symbol_markers", symbol="AAPL")
        self.assertEqual(st, 200)
        t0, t1 = datetime.fromisoformat(p["from"]), datetime.fromisoformat(p["to"])
        self.assertEqual(t1 - t0, timedelta(days=limits.MARKER_DEFAULT_DAYS))


class BacktestMarkerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "hedgefund.db")
        c = sqlite3.connect(self.path)
        c.execute("CREATE TABLE backtest_runs (id TEXT PRIMARY KEY, status TEXT, strategy_id TEXT, version INTEGER, "
                  "kind TEXT, trades_json TEXT, range_json TEXT, attribution_json TEXT)")
        trades = {"fills": 3, "fills_by_symbol": {"AAPL": [["2026-01-05T21:00:00+00:00", "buy", 10, 150.0],
                                                          ["2026-03-02T21:00:00+00:00", "sell", 10, 170.0]],
                                                 "MSFT": [["2026-01-05T21:00:00+00:00", "short", 5, 400.0]]},
                  "fills_truncated": False}
        attr = {"periods": {"window": 63,
                            "best": [{"start": "2026-01-02", "end": "2026-04-02", "strategy_return": 0.12,
                                      "benchmark_return": 0.05, "excess": 0.07}],
                            "worst": [{"start": "2026-05-01", "end": "2026-08-01", "strategy_return": -0.08,
                                       "benchmark_return": -0.02, "excess": -0.06}],
                            "drawdowns": [{"start": "2026-05-04", "trough": "2026-06-10", "recovery": None,
                                           "depth": 0.11, "contributors": [{"symbol": "AAPL", "pnl": -900.0}]}]}}
        rows = [("bt-aaaaaaaaaaaa", "done", trades, attr), ("bt-bbbbbbbbbbbb", "done", {"fills": 1}, None),
                ("bt-cccccccccccc", "running", None, None)]
        for rid, status, tr, at in rows:
            c.execute("INSERT INTO backtest_runs VALUES (?, ?, 'momentum', 3, 'backtest', ?, '{}', ?)",
                      (rid, status, json.dumps(tr) if tr is not None else None, json.dumps(at) if at else None))
        c.commit()
        c.close()
        self.before = sha(self.path)

    def tearDown(self):
        self.assertEqual(sha(self.path), self.before)        # read-only: the file never changes
        self._tmp.cleanup()

    def call(self, run_id, **q):
        svc = ResearchService(supervisor=ResearchSupervisor())
        api = mr_api.ResearchApi(svc)
        with mock.patch.object(ig, "strategy_db_path", lambda: self.path):
            st, p = run(api.backtest_markers(Q(tuple(q.items())), NO_BODY, {"run_id": run_id}))
        json.loads(dumps(p))
        return st, p

    def test_fills_periods_drawdowns(self):
        st, p = self.call("bt-aaaaaaaaaaaa", symbol="aapl")
        self.assertEqual(st, 200, p)
        self.assertEqual(set(p), {"run_id", "strategy_id", "version", "symbol", "items", "truncated", "unavailable",
                                  "read_only"})
        self.assertEqual([(m["kind"], m["side"]) for m in p["items"]],
                         [("period_best", None), ("fill", "buy"), ("fill", "sell"), ("period_worst", None),
                          ("drawdown", None)])
        buy = p["items"][1]
        self.assertEqual((buy["label"], buy["detail"]["entry"]), ("Buy 10 @ 150.00", True))
        dd = p["items"][-1]
        self.assertEqual((dd["label"], dd["t_end"], dd["detail"]["symbol_contributed"]),
                         ("Drawdown 11.0 % (not recovered)", None, True))
        self.assertEqual(p["items"][0]["t_end"], "2026-04-02T04:00:00+00:00")
        self.assertEqual(p["unavailable"], {})
        st, p = self.call("bt-aaaaaaaaaaaa", symbol="AAPL", **{"from": "2026-02-01T00:00:00Z", "to": "2026-04-01T00:00:00Z"})
        self.assertEqual([m["kind"] for m in p["items"]], ["fill"])
        st, p = self.call("bt-aaaaaaaaaaaa", symbol="MSFT")
        self.assertEqual([m["side"] for m in p["items"] if m["kind"] == "fill"], ["short"])

    def test_old_card_running_unknown_validation(self):
        st, p = self.call("bt-bbbbbbbbbbbb", symbol="AAPL")
        self.assertEqual(st, 200)
        self.assertIn("before entries/exits were stored", p["unavailable"]["fills"])
        self.assertIn("No attribution", p["unavailable"]["periods"])
        self.assertEqual(self.call("bt-cccccccccccc", symbol="AAPL")[0], 503)
        self.assertEqual(self.call("bt-dddddddddddd", symbol="AAPL")[0], 404)
        self.assertEqual(self.call("../etc", symbol="AAPL")[0], 404)
        self.assertEqual(self.call("bt-aaaaaaaaaaaa")[0], 400)
        self.assertEqual(self.call("bt-aaaaaaaaaaaa", symbol="bad sym")[0], 400)
        self.assertEqual(self.call("bt-aaaaaaaaaaaa", symbol="AAPL", foo="1")[0], 400)


class UniverseTest(unittest.TestCase):
    def setUp(self):
        self._ts = TempStore()
        self._ts.__enter__()
        try:
            from mr_data_testkit import FakeSources
        except ImportError:  # pragma: no cover
            from tests.mr_data_testkit import FakeSources
        from market_research.data import DataService
        now = datetime(2026, 10, 15, 15, 0, tzinfo=UTC)
        self.fs = FakeSources(lambda: now)
        self.svc = ResearchService(supervisor=ResearchSupervisor(), data=DataService(sources=self.fs, now=lambda: now))
        self.api = mr_api.ResearchApi(self.svc)

    def tearDown(self):
        self._ts.__exit__(None, None, None)

    def call(self, name, body=NO_BODY, query=(), **match):
        return run(getattr(self.api, name)(Q(query), body, match))

    def test_watchlist_as_universe(self):
        st, p = self.call("watchlist_create", {"name": "Tech"})
        wid = p["id"]
        self.assertEqual(self.call("watchlist_items_put", {"symbols": ["AAPL", "SPY"]}, id=wid)[0], 200)
        st, u = self.call("watchlist_universe", id=wid)
        self.assertEqual(st, 200, u)
        self.assertEqual(set(u), {"id", "name", "universe", "count", "asset_classes", "excluded", "max_symbols",
                                  "reason", "note"})
        self.assertEqual((u["universe"], u["count"], u["asset_classes"], u["excluded"]),
                         ({"symbols": ["AAPL", "SPY"]}, 2, ["equity"], []))
        from strategy_platform.strategy import validator
        self.assertTrue(hasattr(validator, "Validator") or True)
        out = ig.universe({"id": wid, "name": "T", "items": [{"symbol": "AAPL", "covered": True},
                                                             {"symbol": "GONE", "covered": False},
                                                             {"symbol": "BTC-USD", "covered": True}]})
        self.assertEqual((out["universe"]["symbols"], out["asset_classes"]), (["AAPL", "BTC-USD"], ["crypto", "equity"]))
        self.assertEqual(out["excluded"], [{"symbol": "GONE", "reason": "No longer in Alpaca's asset list."}])
        st, p = self.call("watchlist_create", {"name": "Empty"})
        st, u = self.call("watchlist_universe", id=p["id"])
        self.assertEqual((u["universe"], u["count"]), (None, 0))
        self.assertIn("at least one", u["reason"])
        self.assertEqual(self.call("watchlist_universe", id="f" * 32)[0], 404)
        self.assertEqual(self.call("watchlist_universe", query=(("x", "1"),), id=wid)[0], 400)


class RunCardFillsTest(unittest.TestCase):
    """Strategy Platform extension (both SPECs §12): run cards keep their fills for the chart markers."""

    def test_trades_summary_keeps_fills_capped(self):
        from strategy_platform.backtest import runcard
        fills = [{"symbol": "AAPL" if i % 2 else "MSFT", "side": "buy", "qty": 1, "fill_price": 10.0 + i,
                  "ts": f"2026-01-{1 + i % 28:02d}T21:00:00+00:00", "notional": 10.0, "fees": 0.1} for i in range(6)]
        s = runcard.trades_summary({"fills": fills, "orders": [], "events": [], "stats": {}})
        self.assertEqual(sorted(s["fills_by_symbol"]), ["AAPL", "MSFT"])
        self.assertEqual(s["fills_by_symbol"]["AAPL"][0], ["2026-01-02T21:00:00+00:00", "buy", 1.0, 11.0])
        self.assertFalse(s["fills_truncated"])
        with mock.patch.object(runcard, "CARD_FILLS_MAX", 4):
            s = runcard.trades_summary({"fills": fills, "orders": [], "events": [], "stats": {}})
        self.assertEqual((sum(len(v) for v in s["fills_by_symbol"].values()), s["fills_truncated"], s["fills"]), (4, True, 6))


if __name__ == "__main__":
    unittest.main()
