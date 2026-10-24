"""Market Research MR4 — live watchlists (SPEC §13 "MR4 backend notes"): the ``watchlist:<id>`` WebSocket
topic (demand registration of shown lists, one subscription, expansion follows list changes, priority after
the tabs), the 30-symbol stock limit message per symbol (no snapshot fallback: nothing fetched beyond it),
watchlist quotes (stream-only last, SIP daily prev close, change, not-subscribed / no-trade reasons, lookup
budget → complete=false), lazy loading of the lists for the hub.

Run from HedgeFund/backend:  python -m unittest tests.test_mr_workspace_live -v
"""
import asyncio
import json
import logging
import unittest

from tests.mr_data_testkit import FakeSources, TempStore, utc
from tests.mr_hub_testkit import Sink, trade, wait_for, running
from tests.test_mr_hub import make_hub
from tests.test_mr_workspace import Q, extra_assets

from market_research import ResearchService, api as mr_api, assets, limits, store, watchlists
from market_research.data import DataService
from market_research.hub import hub as hub_mod
from market_research.hub.demand import parse_topic
from market_research.stats import quote as quote_mod
from market_research.supervisor import ResearchSupervisor
from strategy_platform.api.routes import NO_BODY, dumps

NOW = utc(2026, 10, 15, 15, 0)
LIMIT_TEXT = "Stream limit: 30 symbols — {} is not live."


def setUpModule():
    logging.getLogger("market_research").setLevel(logging.CRITICAL + 1)


def tearDownModule():
    logging.getLogger("market_research").setLevel(logging.NOTSET)


class TopicTest(unittest.TestCase):
    def test_parse_watchlist_topic(self):
        t = parse_topic("watchlist:" + "a" * 32)
        self.assertEqual((t.kind, t.list_id, t.symbol), ("watchlist", "a" * 32, None))
        for bad in ("watchlist:", "watchlist:" + "A" * 32, "watchlist:" + "a" * 31, "watchlist:x:y",
                    "watchlist:" + "a" * 32 + ":1"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_topic(bad)

    def test_expansion_order_and_wants(self):
        s = Sink("trades:AAPL", "watchlist:" + "b" * 32, "candle:MSFT:1D:rth")
        s.expanded = {"watchlist:" + "b" * 32: ["SPY", "AAPL", "BTC-USD"]}
        self.assertEqual(s.symbols(), ["AAPL", "SPY", "BTC-USD", "MSFT"])
        self.assertEqual(s.symbols(("news",)), [])
        self.assertTrue(s.wants("trades:SPY"))
        self.assertFalse(s.wants("trades:QQQ"))
        self.assertFalse(s.wants("candle:SPY:1D:rth"))


class LiveCase(unittest.TestCase):
    def setUp(self):
        self._ts = TempStore()
        self._ts.__enter__()
        self.fs = FakeSources(lambda: NOW)
        assets.refresh(self.fs, NOW)
        rows = store.assets_candidates("") + assets.normalise(extra_assets(40), "2026-10-15T00:00:00+00:00")
        store.assets_replace(rows)
        self.syms = [f"Z{i:03d}" for i in range(31)]

    def tearDown(self):
        self._ts.__exit__(None, None, None)

    def service(self, srv):
        from tests.mr_hub_testkit import FakeAlpaca
        self.srv = srv or FakeAlpaca()
        hub = make_hub(self.srv)
        svc = ResearchService(supervisor=ResearchSupervisor(), data=DataService(sources=self.fs, now=lambda: NOW),
                              hub=hub)
        return svc, hub, mr_api.ResearchApi(svc)


class LiveWatchlistTest(LiveCase):
    def test_topic_limit_quotes_and_list_changes(self):
        svc, hub, api = self.service(None)
        srv = self.srv

        async def body():
            st, p = await api.watchlist_create(Q(), {"name": "Big"}, {})
            lid = p["id"]
            st, p = await api.watchlist_items_put(Q(), {"symbols": self.syms + ["BTC-USD"]}, {"id": lid})
            self.assertEqual(st, 200, p)
            sink = Sink("trades:AAPL", f"watchlist:{lid}")          # tab first, then the watchlist
            self.assertEqual(len(sink.topics), 2)                    # the whole list = ONE subscription
            hub.register(sink)
            self.assertTrue(await wait_for(lambda: srv.live("stocks") and len(srv.live("stocks")[0].subs) == 30))
            self.assertEqual(srv.live("stocks")[0].subs, {"AAPL"} | set(self.syms[:29]))
            self.assertTrue(await wait_for(lambda: srv.live("crypto") and "BTC/USD" in srv.live("crypto")[0].subs))
            st = {m["symbol"]: m for m in sink.of("symbol_status")}
            for s in ("Z029", "Z030"):
                self.assertEqual(st[s], {"type": "symbol_status", "symbol": s, "live": False,
                                         "reason": LIMIT_TEXT.format(s)})
            self.assertTrue(await wait_for(lambda: sink.of("symbol_status", symbol="Z001")
                                           and sink.of("symbol_status", symbol="Z001")[-1]["live"]))
            srv.live("stocks")[0].push([trade("Z000", 52.5, 3, "2026-10-15T14:59:01Z")])
            self.assertTrue(await wait_for(lambda: sink.of("trade", symbol="Z000")))
            self.assertEqual(sink.of("trade", symbol="Z000")[-1]["price"], 52.5)

            self.fs.calls.clear()
            st, q = await api.watchlist_quotes(Q(), NO_BODY, {"id": lid})
            self.assertEqual(st, 200, q)
            json.loads(dumps(q))
            self.assertEqual(set(q), {"id", "generated_at", "complete", "retry_after_seconds", "stream_limit",
                                      "note", "items"})
            self.assertEqual((q["complete"], q["retry_after_seconds"], q["stream_limit"]), (True, None, 30))
            items = {i["symbol"]: i for i in q["items"]}
            z0 = items["Z000"]
            self.assertTrue(z0["live"])
            self.assertEqual((z0["last"]["value"], z0["last"]["feed"], z0["last"]["as_of"]),
                             (52.5, "iex", "2026-10-15T14:59:01+00:00"))
            self.assertEqual(z0["prev_close"]["feed"], "sip_daily")
            self.assertIsNotNone(z0["prev_close"]["value"])
            pc = z0["prev_close"]["value"]
            self.assertAlmostEqual(z0["change"]["value"], 52.5 - pc)
            self.assertAlmostEqual(z0["change_pct"]["value"], (52.5 - pc) / pc * 100)
            z1 = items["Z001"]
            self.assertEqual((z1["live"], z1["last"]["value"], z1["last"]["reason"]),
                             (True, None, quote_mod.NO_TRADE_REASON))
            self.assertIsNotNone(z1["prev_close"]["value"])
            self.assertIsNone(z1["change_pct"]["value"])
            for s in ("Z029", "Z030"):                               # beyond the limit: message only
                it = items[s]
                self.assertFalse(it["live"])
                self.assertEqual(it["reason"], LIMIT_TEXT.format(s))
                for k in ("last", "prev_close", "change", "change_pct"):
                    self.assertEqual((it[k]["value"], it[k]["reason"]), (None, LIMIT_TEXT.format(s)), k)
            self.assertEqual(items["BTC-USD"]["prev_close"]["feed"], "crypto")
            self.assertIsNotNone(items["BTC-USD"]["prev_close"]["value"])
            self.assertTrue(items["BTC-USD"]["live"])

            # list change -> expansion follows at once (no re-subscribe): Z000 out, Z029 gets the slot
            st, p = await api.watchlist_item_remove(Q(), NO_BODY, {"id": lid, "symbol": "Z000"})
            self.assertEqual(st, 200)
            self.assertTrue(await wait_for(lambda: "Z029" in srv.live("stocks")[0].subs))
            self.assertNotIn("Z000", srv.live("stocks")[0].subs)
            self.assertNotIn("Z000", hub.last_trades)
            self.assertTrue(await wait_for(lambda: sink.of("symbol_status", symbol="Z029")[-1]["live"]))
            self.assertEqual(sink.of("symbol_status", symbol="Z030")[-1]["reason"], LIMIT_TEXT.format("Z030"))
            # deleting the list expands the topic to nothing
            await api.watchlist_delete(Q(), NO_BODY, {"id": lid})
            self.assertTrue(await wait_for(lambda: srv.live("stocks")[0].subs == {"AAPL"}))
        asyncio.run(running(hub, body))

    def test_beyond_limit_fetches_nothing(self):
        svc, hub, api = self.service(None)
        srv = self.srv
        fetched = []
        orig = quote_mod.prev_close

        def spy(loader, sources, symbol, ac, now):
            fetched.append(symbol)
            return orig(loader, sources, symbol, ac, now)

        async def body():
            st, p = await api.watchlist_create(Q(), {"name": "Big"}, {})
            lid = p["id"]
            await api.watchlist_items_put(Q(), {"symbols": self.syms}, {"id": lid})
            hub.register(Sink(f"watchlist:{lid}"))
            self.assertTrue(await wait_for(lambda: srv.live("stocks") and len(srv.live("stocks")[0].subs) == 30))
            quote_mod.prev_close = spy
            try:
                st, q = await api.watchlist_quotes(Q(), NO_BODY, {"id": lid})
            finally:
                quote_mod.prev_close = orig
            self.assertEqual(fetched, self.syms[:30])
            self.assertEqual(q["items"][30]["prev_close"]["reason"], LIMIT_TEXT.format("Z030"))
        asyncio.run(running(hub, body))

    def test_not_subscribed_and_hub_stopped(self):
        svc, hub, api = self.service(None)

        async def body():
            st, p = await api.watchlist_create(Q(), {"name": "L"}, {})
            lid = p["id"]
            await api.watchlist_items_put(Q(), {"symbols": ["AAPL"]}, {"id": lid})
            hub.register(Sink("trades:SPY"))
            self.assertTrue(await wait_for(lambda: self.srv.live("stocks")))
            await asyncio.sleep(0.05)
            st, q = await api.watchlist_quotes(Q(), NO_BODY, {"id": lid})
            it = q["items"][0]
            self.assertFalse(it["live"])
            self.assertEqual(it["last"]["reason"], hub_mod.NOT_SUBSCRIBED_MESSAGE)
            self.assertIsNotNone(it["prev_close"]["value"])            # not a limit case: prev close shown
        asyncio.run(running(hub, body))
        lid = watchlists.list_all()[1][0]["id"]
        st, q = asyncio.run(api.watchlist_quotes(Q(), NO_BODY, {"id": lid}))   # hub task stopped
        self.assertFalse(q["items"][0]["live"])
        self.assertEqual(q["items"][0]["last"]["reason"], hub_mod.NOT_RUNNING_MESSAGE)

    def test_uncovered_item_and_quotes_validation(self):
        svc, hub, api = self.service(None)
        _id, _s, _l = watchlists.create("L")
        watchlists.replace_items(_id, ["AAPL", "PAPL"], svc.data.asset)
        store.assets_replace([r for r in store.assets_candidates("") if r["symbol"] != "PAPL"])
        st, q = asyncio.run(api.watchlist_quotes(Q(), NO_BODY, {"id": _id}))
        papl = q["items"][1]
        self.assertEqual((papl["covered"], papl["live"]), (False, False))
        self.assertIn("PAPL is not in Alpaca's list", papl["last"]["reason"])
        self.assertIsNone(papl["prev_close"]["value"])
        self.assertEqual(asyncio.run(api.watchlist_quotes(Q([("x", "1")]), NO_BODY, {"id": _id}))[0], 400)

    def test_lazy_load_for_the_hub(self):
        svc, hub, api = self.service(None)
        lid, _s, _l = watchlists.create("L")
        watchlists.replace_items(lid, ["AAPL", "SPY"], svc.data.asset)
        self.assertFalse(svc.workspace.loaded)

        async def body():
            hub.register(Sink(f"watchlist:{lid}"))                   # backend restarted, WPF re-subscribes
            self.assertTrue(await wait_for(lambda: self.srv.live("stocks")
                                           and self.srv.live("stocks")[0].subs == {"AAPL", "SPY"}))
            self.assertTrue(svc.workspace.loaded)
        asyncio.run(running(hub, body))

    def test_workspace_ignores_older_answers(self):
        calls = []

        class H:
            def refresh(self):
                calls.append(1)
        ws = watchlists.Workspace(H())
        lists = [{"id": "a" * 32, "items": [{"symbol": "AAPL"}]}]
        self.assertTrue(ws.apply(5, lists))
        self.assertFalse(ws.apply(4, []))
        self.assertEqual(ws.symbols_for("a" * 32), ["AAPL"])
        self.assertTrue(ws.apply(6, lists))                           # unchanged: no hub refresh
        self.assertEqual(len(calls), 1)


class WsTopicTest(unittest.TestCase):
    def test_watchlist_topic_over_research_ws(self):
        from tests.test_mr_hub_ws import FakeWs, make_hub as ws_hub, session
        hub, ws = ws_hub(), FakeWs()
        lid = "c" * 32
        hub.watchlist_symbols = lambda i: ["AAPL", "SPY"] if i == lid else []

        async def body(s):
            ws.client({"op": "hello", "visible": True})
            self.assertTrue(await wait_for(lambda: ws.sent))
            ws.client({"op": "subscribe", "topics": [f"watchlist:{lid}"] + [f"trades:S{i}" for i in range(63)]})
            self.assertTrue(await wait_for(lambda: ws.of("symbol_status", symbol="SPY")))
            self.assertEqual(len(s.topics), 64)                       # the list counts as one subscription
            self.assertEqual(s.symbols()[:2], ["AAPL", "SPY"])        # its position = its priority
            ws.client({"op": "subscribe", "topics": ["watchlist:" + "d" * 32]})
            self.assertTrue(await wait_for(lambda: ws.of("error", code="subscription_limit")))
            ws.client({"op": "subscribe", "topics": ["watchlist:XYZ"]})
            self.assertTrue(await wait_for(lambda: ws.of("error", code="bad_message")))
            ws.bye()
        asyncio.run(session(hub, ws, body))


class PrevCloseTest(LiveCase):
    def test_budget_and_cache(self):
        data = DataService(sources=self.fs, now=lambda: NOW)
        cache = quote_mod.PrevCloseCache()
        out, complete = quote_mod.prev_closes([("AAPL", "equity"), ("SPY", "equity")], data.loader, data.sources,
                                              NOW, cache, budget=0)
        self.assertFalse(complete)
        self.assertEqual(out["AAPL"]["reason"], quote_mod.LOADING_REASON)
        out, complete = quote_mod.prev_closes([("AAPL", "equity"), ("BTC-USD", "crypto")], data.loader,
                                              data.sources, NOW, cache)
        self.assertTrue(complete)
        self.assertEqual(out["AAPL"]["as_of"], "2026-10-14T20:00:00+00:00")
        snap = data.snapshot("AAPL")
        self.assertEqual(out["AAPL"]["value"], snap["prev_close"]["value"])    # = the header's definition
        self.assertEqual(out["BTC-USD"]["value"], data.snapshot("BTC-USD")["prev_close"]["value"])
        self.fs.fail["daily"] = RuntimeError("must come from the cache")
        out, complete = quote_mod.prev_closes([("AAPL", "equity")], data.loader, data.sources, NOW, cache)
        self.assertEqual(out["AAPL"]["value"], snap["prev_close"]["value"])

    def test_source_failure_is_per_row(self):
        from strategy_platform.core.errors import DataSourceUnavailable
        data = DataService(sources=self.fs, now=lambda: NOW)
        self.fs.fail["daily"] = DataSourceUnavailable("alpaca", "equity_bars_history", "down")
        out, complete = quote_mod.prev_closes([("AAPL", "equity"), ("BTC-USD", "crypto")], data.loader,
                                              data.sources, NOW, quote_mod.PrevCloseCache())
        self.assertIsNone(out["AAPL"]["value"])
        self.assertTrue(out["AAPL"]["reason"])
        self.assertIsNotNone(out["BTC-USD"]["value"])

    def test_52w_position(self):
        data = DataService(sources=self.fs, now=lambda: NOW)
        st = data.stats("AAPL")
        rng = st["range_52w"]
        pos = rng["position_pct"]
        last = data.snapshot("AAPL")["last"]["value"]
        exp = (last - rng["low"]["value"]) / (rng["high"]["value"] - rng["low"]["value"]) * 100
        self.assertAlmostEqual(pos["value"], exp)
        self.assertEqual(pos["feed"], "computed")


if __name__ == "__main__":
    unittest.main()
