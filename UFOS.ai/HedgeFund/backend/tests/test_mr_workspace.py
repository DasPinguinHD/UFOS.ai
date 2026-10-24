"""Market Research MR4 — symbol workspace REST routes (SPEC §13 "MR4 backend notes"): watchlist CRUD,
ordering, item add/remove/replace, limits (lists, items, name length), validation (types, unknown fields,
symbols via the asset list, ids), names stored as plain text (injection-safe), session restore round-trip,
recent symbols and the Ctrl+K palette ranking, route registration + store v3.

Run from HedgeFund/backend:  python -m unittest tests.test_mr_workspace -v
"""
import asyncio
import json
import logging
import sqlite3
import unittest

try:
    from mr_data_testkit import FakeSources, TempStore, utc
except ImportError:  # pragma: no cover - run as package
    from tests.mr_data_testkit import FakeSources, TempStore, utc

from market_research import ResearchService, api as mr_api, limits, store
from market_research.data import DataService
from market_research.supervisor import ResearchSupervisor
from strategy_platform.api.routes import NO_BODY, dumps

NOW = utc(2026, 10, 15, 15, 0)


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


def extra_assets(n):
    return [{"symbol": f"Z{i:03d}", "name": f"Zed {i} Corp", "asset_class": "us_equity", "exchange": "NYSE",
             "tradable": True, "status": "active"} for i in range(n)]


class WsCase(unittest.TestCase):
    def setUp(self):
        self._ts = TempStore()
        self._ts.__enter__()
        self.fs = FakeSources(lambda: NOW)
        self.svc = ResearchService(supervisor=ResearchSupervisor(),
                                   data=DataService(sources=self.fs, now=lambda: NOW))
        self.api = mr_api.ResearchApi(self.svc)

    def tearDown(self):
        self._ts.__exit__(None, None, None)

    def call(self, name, body=NO_BODY, query=(), **match):
        status, payload = run(getattr(self.api, name)(Q(query), body, match))
        json.loads(dumps(payload))                       # strict JSON
        return status, payload

    def create(self, name):
        st, p = self.call("watchlist_create", {"name": name})
        self.assertEqual(st, 201, p)
        return p["id"]

    def names(self):
        return [w["name"] for w in self.call("watchlists_list")[1]["lists"]]


class WatchlistCrudTest(WsCase):
    def test_empty_then_create_patch_delete(self):
        st, p = self.call("watchlists_list")
        self.assertEqual((st, p["lists"]), (200, []))                       # nothing seeded
        self.assertEqual(p["limits"], {"max_lists": 20, "max_items": 200, "max_name_chars": 40})
        a = self.create("  Tech  ")
        b = self.create("Crypto")
        st, p = self.call("watchlists_list")
        self.assertEqual([(w["name"], w["position"], w["collapsed"]) for w in p["lists"]],
                         [("Tech", 0, False), ("Crypto", 1, False)])
        self.assertEqual(set(p["lists"][0]), {"id", "name", "position", "collapsed", "created_at", "updated_at",
                                               "items"})
        self.assertRegex(a, r"^[0-9a-f]{32}$")
        st, p = self.call("watchlist_patch", {"collapsed": True, "name": "Big Tech"}, id=a)
        self.assertEqual(st, 200, p)
        self.assertEqual((p["lists"][0]["name"], p["lists"][0]["collapsed"]), ("Big Tech", True))
        st, p = self.call("watchlist_patch", {"position": 0}, id=b)                # reorder lists
        self.assertEqual([w["id"] for w in p["lists"]], [b, a])
        self.assertEqual([w["position"] for w in p["lists"]], [0, 1])
        c = self.create("Third")
        st, p = self.call("watchlist_delete", id=b)
        self.assertEqual(st, 200)
        self.assertEqual([(w["id"], w["position"]) for w in p["lists"]], [(a, 0), (c, 1)])   # compacted
        self.assertEqual(self.call("watchlist_delete", id=b)[0], 404)

    def test_limits_and_conflicts(self):
        self.create("Tech")
        st, p = self.call("watchlist_create", {"name": "TECH"})
        self.assertEqual(st, 409)
        self.assertEqual(p["error"], 'A watchlist named "TECH" already exists.')
        for i in range(limits.MAX_WATCHLISTS - 1):
            self.create(f"L{i}")
        st, p = self.call("watchlist_create", {"name": "One too many"})
        self.assertEqual((st, p["error"]), (409, "At most 20 watchlists — delete one first."))
        ids = [w["id"] for w in self.call("watchlists_list")[1]["lists"]]
        st, p = self.call("watchlist_patch", {"name": "tech"}, id=ids[1])
        self.assertEqual(st, 409)
        st, p = self.call("watchlist_patch", {"name": "Tech"}, id=ids[0])            # same list: ok
        self.assertEqual(st, 200)
        st, p = self.call("watchlist_patch", {"position": 20}, id=ids[0])
        self.assertEqual((st, list(p["errors"])), (400, ["position"]))

    def test_name_validation(self):
        bad = [None, 5, "", "   ", "x" * 41, "tab\there", "nl\nhere", "bell\x07", "rtl‮override", "zw​sp",
               "line sep", ["a"]]
        for name in bad:
            with self.subTest(name=name):
                st, p = self.call("watchlist_create", {"name": name})
                self.assertEqual(st, 400)
                self.assertIn("name", p["errors"])
        self.assertEqual(self.call("watchlist_create", {"name": "x" * 40})[0], 201)
        self.assertEqual(self.call("watchlist_create", {"name": "Ünïcödé ★ 日本"})[0], 201)

    def test_injection_names_stored_as_plain_text(self):
        evil = ["<script>alert(1)</script>", "'; DROP TABLE watchlists; --", "Robert\"); --", "{{7*7}}", "&amp;"]
        for n in evil:
            self.create(n)
        self.assertEqual(self.names(), evil)
        with sqlite3.connect(store.db_path()) as conn:
            self.assertEqual([r[0] for r in conn.execute("SELECT name FROM watchlists ORDER BY position")], evil)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='watchlists'").fetchone()[0], 1)

    def test_body_and_param_validation(self):
        a = self.create("Tech")
        cases = [("watchlist_create", NO_BODY, {}), ("watchlist_create", [1], {}), ("watchlist_create", {}, {}),
                 ("watchlist_create", {"name": "A", "x": 1}, {}), ("watchlist_patch", {}, {"id": a}),
                 ("watchlist_patch", {"collapsed": "yes"}, {"id": a}), ("watchlist_patch", {"position": True}, {"id": a}),
                 ("watchlist_patch", {"position": 1.0}, {"id": a}), ("watchlist_items_put", {"symbols": "AAPL"}, {"id": a}),
                 ("watchlist_items_put", {}, {"id": a}), ("watchlist_item_add", {"symbol": 1}, {"id": a}),
                 ("watchlist_item_add", {"symbol": "AAPL", "position": -1}, {"id": a}),
                 ("watchlist_create", {"name": "x" * (limits.MAX_WORKSPACE_BODY_BYTES + 10)}, {})]
        for name, body, match in cases:
            with self.subTest(name=name, body=str(body)[:60]):
                st, p = self.call(name, body, **match)
                self.assertEqual(st, 400, p)
                self.assertEqual(p["error"], "invalid input")
        st, p = self.call("watchlists_list", query=[("x", "1")])
        self.assertEqual(st, 400)
        for bad_id in ("nothex", "A" * 32, a[:-1], "../../etc"):
            with self.subTest(id=bad_id):
                self.assertEqual(self.call("watchlist_patch", {"collapsed": True}, id=bad_id),
                                 (404, {"error": "Watchlist not found."}))
        self.assertEqual(self.call("watchlist_quotes", id="0" * 32)[0], 404)


class ItemsTest(WsCase):
    def items(self, list_id):
        for w in self.call("watchlists_list")[1]["lists"]:
            if w["id"] == list_id:
                return [i["symbol"] for i in w["items"]]

    def test_put_add_remove_reorder(self):
        a = self.create("Mixed")
        st, p = self.call("watchlist_items_put", {"symbols": ["aapl", "BTC/USD", "SPY"]}, id=a)
        self.assertEqual(st, 200, p)
        items = p["lists"][0]["items"]
        self.assertEqual([(i["symbol"], i["position"]) for i in items], [("AAPL", 0), ("BTC-USD", 1), ("SPY", 2)])
        self.assertEqual(items[0], {"symbol": "AAPL", "position": 0, "added_at": items[0]["added_at"],
                                    "name": "Apple Inc. Common Stock", "asset_class": "equity", "kind": "stock",
                                    "exchange": "NASDAQ", "covered": True})
        self.assertEqual(items[1]["asset_class"], "crypto")
        self.assertEqual(items[2]["kind"], "etf")
        added = {i["symbol"]: i["added_at"] for i in items}
        st, p = self.call("watchlist_items_put", {"symbols": ["SPY", "AAPL", "BTC-USD"]}, id=a)   # reorder
        self.assertEqual(self.items(a), ["SPY", "AAPL", "BTC-USD"])
        self.assertEqual({i["symbol"]: i["added_at"] for i in p["lists"][0]["items"]}, added)     # kept
        st, p = self.call("watchlist_item_add", {"symbol": "aa", "position": 1}, id=a)
        self.assertEqual(self.items(a), ["SPY", "AA", "AAPL", "BTC-USD"])
        st, p = self.call("watchlist_item_add", {"symbol": "ETH-USD"}, id=a)
        self.assertEqual(self.items(a)[-1], "ETH-USD")
        self.assertEqual(self.call("watchlist_item_add", {"symbol": "AAPL"}, id=a),
                         (409, {"error": "AAPL is already in this watchlist."}))
        st, p = self.call("watchlist_item_remove", id=a, symbol="aapl")
        self.assertEqual(st, 200)
        self.assertEqual([i["position"] for i in p["lists"][0]["items"]], [0, 1, 2, 3])
        self.assertEqual(self.call("watchlist_item_remove", id=a, symbol="AAPL"),
                         (404, {"error": "AAPL is not in this watchlist."}))
        self.assertEqual(self.call("watchlist_item_remove", id=a, symbol="no good")[0], 400)
        self.assertEqual(self.call("watchlist_items_put", {"symbols": []}, id=a)[0], 200)
        self.assertEqual(self.items(a), [])

    def test_validation_of_symbols(self):
        a = self.create("L")
        st, p = self.call("watchlist_items_put", {"symbols": ["AAPL", "aapl"]}, id=a)
        self.assertEqual((st, p["errors"]), (400, {"symbols": "AAPL is listed twice."}))
        st, p = self.call("watchlist_items_put", {"symbols": ["AAPL", "not a symbol"]}, id=a)
        self.assertEqual((st, list(p["errors"])), (400, ["symbols[1]"]))
        st, p = self.call("watchlist_items_put", {"symbols": ["AAPL", 7]}, id=a)
        self.assertEqual(st, 400)
        st, p = self.call("watchlist_items_put", {"symbols": ["AAPL", "NOPE", "DOGE-USD"]}, id=a)
        self.assertEqual(st, 404)
        self.assertEqual((p["kind"], p["symbols"]), ("SymbolNotCovered", ["NOPE", "DOGE-USD"]))
        self.assertIn("NOPE is not in Alpaca's list", p["error"])
        self.assertEqual(self.items(a), [])                                    # nothing written
        st, p = self.call("watchlist_item_add", {"symbol": "DOGE-USD"}, id=a)
        self.assertEqual((st, p["error"]), (404, "Not available from Alpaca (the configured crypto source)."))

    def test_delisted_symbol_kept_on_reorder(self):
        a = self.create("L")
        self.call("watchlist_items_put", {"symbols": ["AAPL", "PAPL"]}, id=a)
        store.assets_replace([r for r in store.assets_candidates("") if r["symbol"] != "PAPL"])  # PAPL delisted
        st, p = self.call("watchlist_items_put", {"symbols": ["PAPL", "AAPL"]}, id=a)
        self.assertEqual(st, 200, p)
        papl = p["lists"][0]["items"][0]
        self.assertEqual((papl["symbol"], papl["covered"], papl["name"], papl["kind"], papl["asset_class"]),
                         ("PAPL", False, None, None, "equity"))

    def test_item_limit(self):
        from market_research import assets
        rows = assets.normalise(extra_assets(limits.MAX_WATCHLIST_ITEMS + 1), "2026-10-15T00:00:00+00:00")
        store.assets_replace(rows)
        a = self.create("Big")
        syms = [r["symbol"] for r in rows]
        st, p = self.call("watchlist_items_put", {"symbols": syms}, id=a)
        self.assertEqual((st, p["errors"]["symbols"]), (400, "at most 200 symbols per watchlist"))
        self.assertEqual(self.call("watchlist_items_put", {"symbols": syms[:200]}, id=a)[0], 200)
        self.assertEqual(self.call("watchlist_item_add", {"symbol": syms[200]}, id=a),
                         (409, {"error": "A watchlist holds at most 200 symbols."}))
        self.assertEqual(self.call("watchlist_delete", id=a)[0], 200)
        with sqlite3.connect(store.db_path()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM watchlist_items").fetchone()[0], 0)

    def test_no_audit_rows(self):
        a = self.create("L")
        self.call("watchlist_items_put", {"symbols": ["AAPL"]}, id=a)
        self.call("session_put", {"tabs": [], "active": None, "sidebar": {"collapsed": False, "section": "symbol"}})
        self.assertEqual(store.list_audit(), [])


class SessionTest(WsCase):
    BODY = {"tabs": [{"symbol": "aapl", "tf": "1D", "type": "candles", "session": "rth"},
                     {"symbol": "BTC-USD", "tf": "1H", "type": "line", "session": "utc"},
                     {"symbol": "AAPL", "tf": "5m", "type": "area", "session": "eth"}],
            "active": 1, "sidebar": {"collapsed": True, "section": "symbol"}}

    def test_default_and_round_trip(self):
        st, p = self.call("session_get")
        self.assertEqual(p, {"schema": 1, "tabs": [], "active": None,
                             "sidebar": {"collapsed": False, "section": "overview"}, "saved": False,
                             "updated_at": None, "note": None, "limits": {"max_tabs": 12}})
        st, p = self.call("session_put", self.BODY)
        self.assertEqual(st, 200, p)
        self.assertEqual(p["tabs"][0]["symbol"], "AAPL")
        st, g = self.call("session_get")
        self.assertEqual(g, p)
        self.assertTrue(g["saved"])
        self.assertEqual((g["active"], g["sidebar"]), (1, {"collapsed": True, "section": "symbol"}))
        self.assertEqual([t["tf"] for t in g["tabs"]], ["1D", "1H", "5m"])
        st, p = self.call("session_put", {"tabs": [], "active": None,
                                          "sidebar": {"collapsed": False, "section": "macro"}})
        self.assertEqual((st, self.call("session_get")[1]["tabs"]), (200, []))

    def test_validation(self):
        tab = {"symbol": "AAPL", "tf": "1D", "type": "candles", "session": "rth"}
        sb = {"collapsed": False, "section": "symbol"}
        bad = [
            {"tabs": [tab], "active": 0},                                         # missing sidebar
            {"tabs": [tab], "active": 0, "sidebar": sb, "extra": 1},
            {"tabs": [tab] * 13, "active": 0, "sidebar": sb},
            {"tabs": [dict(tab, tf="2m")], "active": 0, "sidebar": sb},
            {"tabs": [dict(tab, type="renko")], "active": 0, "sidebar": sb},
            {"tabs": [dict(tab, session="utc")], "active": 0, "sidebar": sb},
            {"tabs": [dict(tab, symbol="BTC-USD", session="rth")], "active": 0, "sidebar": sb},
            {"tabs": [dict(tab, symbol="no way")], "active": 0, "sidebar": sb},
            {"tabs": [dict(tab, extra=1)], "active": 0, "sidebar": sb},
            {"tabs": [tab], "active": 1, "sidebar": sb},
            {"tabs": [tab], "active": None, "sidebar": sb},
            {"tabs": [tab], "active": True, "sidebar": sb},
            {"tabs": [], "active": 0, "sidebar": sb},
            {"tabs": [tab], "active": 0, "sidebar": {"collapsed": "no", "section": "symbol"}},
            {"tabs": [tab], "active": 0, "sidebar": {"collapsed": False, "section": "markets"}},
            {"tabs": [tab], "active": 0, "sidebar": {"collapsed": False}},
            {"tabs": "AAPL", "active": 0, "sidebar": sb},
        ]
        for body in bad:
            with self.subTest(body=str(body)[:90]):
                st, p = self.call("session_put", body)
                self.assertEqual(st, 400, p)
        self.assertFalse(self.call("session_get")[1]["saved"])
        st, p = self.call("session_put", {"tabs": [tab] * 12, "active": 11, "sidebar": sb})   # 12 = MAX_TABS
        self.assertEqual(st, 200)

    def test_unreadable_saved_session_reported(self):
        self.call("session_put", self.BODY)
        with sqlite3.connect(store.db_path()) as conn:
            conn.execute("UPDATE session SET state_json = '{\"tabs\": 5}'")
        st, p = self.call("session_get")
        self.assertEqual((st, p["saved"], p["tabs"]), (200, False, []))
        self.assertEqual(p["note"], "The saved session could not be read and was ignored.")


class PaletteTest(WsCase):
    def test_recent_and_ranking(self):
        st, p = self.call("palette")
        self.assertEqual((st, p["items"], p["recent"]), (200, [], []))
        for s in ("AA", "aapl", "BTC-USD", "AA"):
            st, p = self.call("recent_add", {"symbol": s})
            self.assertEqual(st, 200, p)
        self.assertEqual(p["recent"], ["AA", "BTC-USD", "AAPL"])
        st, p = self.call("palette")
        self.assertEqual([(i["symbol"], i["match"], i["recent"]) for i in p["items"]],
                         [("AA", "recent", True), ("BTC-USD", "recent", True), ("AAPL", "recent", True)])
        a = self.create("Tech")
        self.call("watchlist_items_put", {"symbols": ["AAPW"]}, id=a)
        # q=aa: exact AA first; symbol prefix group: recent AAPL before shorter-but-not-recent? (AAPL, AAPW both len 4)
        self.call("recent_add", {"symbol": "AAPW"})
        st, p = self.call("palette", query=[("q", "aa")])
        got = [(i["symbol"], i["match"], i["recent"]) for i in p["items"]]
        self.assertEqual(got[0], ("AA", "exact", True))
        self.assertEqual(got[1:3], [("AAPW", "symbol_prefix", True), ("AAPL", "symbol_prefix", True)])  # newest first
        self.assertEqual(p["items"][1]["watchlists"], [a])
        st, p = self.call("palette", query=[("q", "apple")])
        self.assertEqual(p["items"][0]["match"], "name_word")
        self.assertEqual(set(p["items"][0]), {"symbol", "name", "asset_class", "kind", "exchange", "tradable",
                                              "match", "recent", "watchlists"})
        self.assertEqual(self.call("palette", query=[("q", "aa"), ("limit", "1")])[1]["items"][0]["symbol"], "AA")
        for q in [("q", "<x>"), ("q", "a" * 33), ("limit", "0"), ("limit", "51"), ("zz", "1")]:
            with self.subTest(q=q):
                self.assertEqual(self.call("palette", query=[q])[0], 400)
        st, p = self.call("recent_add", {"symbol": "NOPE"})
        self.assertEqual((st, p["kind"]), (404, "SymbolNotCovered"))
        self.assertEqual(self.call("recent_add", {"symbol": "AAPL", "x": 1})[0], 400)
        self.assertEqual(self.call("recent_clear"), (200, {"recent": []}))

    def test_recent_cap(self):
        from market_research import assets
        store.assets_replace(assets.normalise(extra_assets(30), "2026-10-15T00:00:00+00:00"))
        for i in range(25):
            p = self.call("recent_add", {"symbol": f"Z{i:03d}"})[1]
        self.assertEqual(len(p["recent"]), limits.MAX_RECENT_SYMBOLS)
        self.assertEqual(p["recent"][0], "Z024")
        self.assertEqual(p["recent"][-1], "Z005")


class WiringTest(unittest.TestCase):
    def test_routes_registered_and_store_v3(self):
        paths = {(m, p): n for m, p, n, _b in mr_api.ROUTES}
        for m, p in [("GET", "/research/watchlists"), ("POST", "/research/watchlists"),
                     ("PATCH", "/research/watchlists/{id}"), ("DELETE", "/research/watchlists/{id}"),
                     ("PUT", "/research/watchlists/{id}/items"), ("POST", "/research/watchlists/{id}/items"),
                     ("DELETE", "/research/watchlists/{id}/items/{symbol}"), ("GET", "/research/watchlists/{id}/quotes"),
                     ("GET", "/research/session"), ("PUT", "/research/session"), ("POST", "/research/recent"),
                     ("DELETE", "/research/recent"), ("GET", "/research/palette")]:
            self.assertIn((m, p), paths)
            self.assertTrue(hasattr(mr_api.ResearchApi, paths[(m, p)]))
        self.assertGreaterEqual(store.SCHEMA_VERSION, 3)
        self.assertIn(3, [v for v, _s in store.MIGRATIONS])
        with TempStore():
            info = store.storage_info()
            for t in ("watchlists", "watchlist_items", "session", "recent_symbols"):
                self.assertIn(t, info["rows"])


if __name__ == "__main__":
    unittest.main()
