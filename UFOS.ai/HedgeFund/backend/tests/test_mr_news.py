"""Market Research MR6 — news (SPEC §13 "MR6 backend notes"): untrusted text → plain text (HTML stripped,
entities decoded, control characters removed, caps), link / image-URL scheme filtering, REST news via the
provider (research priority, refresh spacing, paging, 503 on failure), store v5 de-dup by id + 7-day
retention, the thumbnails setting (off → no image paths, proxy refuses, nothing fetched), route validation,
and the news stream: routing by symbol over the hub, repeats dropped, updates re-sent, items stored.
Fakes only; no network.

Run from HedgeFund/backend:  python -m unittest tests.test_mr_news -v
"""
import asyncio
import contextvars
import json
import logging
import sqlite3
import unittest
from datetime import timedelta

try:
    from mr_data_testkit import TempStore
    from mr_news_testkit import NOW, FakeData, Mono, Q, iso, raw_news
except ImportError:  # pragma: no cover - run as package
    from tests.mr_data_testkit import TempStore
    from tests.mr_news_testkit import NOW, FakeData, Mono, Q, iso, raw_news

from market_research import ResearchService, api as mr_api, limits, store
from market_research import news as news_mod
from market_research.news import NewsService, image_key, public_item
from market_research.news_text import plain_text, safe_image_url, safe_link, sanitize_item
from market_research.supervisor import ResearchSupervisor
from strategy_platform.api.routes import NO_BODY, dumps
from strategy_platform.core.errors import DataSourceUnavailable, SecretMissing
from strategy_platform.data import http_gate

IMG = "https://cdn.benzinga.com/files/images/story/2026/10/15/aapl.jpeg"


def setUpModule():
    logging.getLogger("market_research").setLevel(logging.CRITICAL + 1)


def tearDownModule():
    logging.getLogger("market_research").setLevel(logging.NOTSET)


def run(c):
    return asyncio.run(c)


class SanitiseTest(unittest.TestCase):
    def test_html_is_stripped_and_entities_decoded(self):
        self.assertEqual(plain_text("<p>Apple <b>beats</b></p><script>alert(1)</script><style>p{}</style>", 300),
                         "Apple beats")
        self.assertEqual(plain_text("Q3 &amp; Q4: EPS &gt; est. &#8212; up", 300), "Q3 & Q4: EPS > est. — up")
        self.assertEqual(plain_text("x < y and y > z", 300), "x < y and y > z")      # comparisons survive
        self.assertEqual(plain_text("<div>a</div><div>b</div><br/>c", 300), "a b c")  # block tags → spaces
        self.assertEqual(plain_text('<img src=x onerror="alert(1)">Text <a href="javascript:x">link</a>', 300),
                         "Text link")
        self.assertEqual(plain_text("<!-- hidden -->shown", 300), "shown")

    def test_encoded_markup_is_escaped_out(self):
        # entity-encoded tags decode into markup → removed by the second pass (never rendered as a tag)
        out = plain_text("&lt;script&gt;alert(1)&lt;/script&gt;Hi &lt;b&gt;there&lt;/b&gt;", 300)
        self.assertEqual(out, "Hi there")
        self.assertNotIn("<", out)

    def test_truncated_tag_control_chars_and_caps(self):
        self.assertEqual(plain_text("Cut off <a hre", 300), "Cut off")
        self.assertEqual(plain_text("a\x00b\x07c\u2028d\u200be\ufeff", 300), "a b c de")
        self.assertEqual(plain_text("  many\n\n  spaces\t", 300), "many spaces")
        self.assertEqual(plain_text("x" * 400, 300), "x" * 299 + "…")
        self.assertIsNone(plain_text("<b></b>  ", 300))
        self.assertIsNone(plain_text(None, 300))
        self.assertIsNone(plain_text(42, 300))

    def test_link_schemes(self):
        for bad in ("javascript:alert(1)", "JavaScript:alert(1)", "data:text/html,<b>", "file:///C:/x", "ftp://a.b/c",
                    "//benzinga.com/a", "https://user:pw@benzinga.com/a", "https://exa mple.com/", "https://",
                    "https://a.com/\x00", "https://a.com:99999/", "x" * 1001, None, 7):
            self.assertIsNone(safe_link(bad), bad)
        self.assertEqual(safe_link(" https://www.benzinga.com/news/1?x=1 "), "https://www.benzinga.com/news/1?x=1")
        self.assertEqual(safe_link("http://www.benzinga.com/a"), "http://www.benzinga.com/a")

    def test_image_urls_https_and_public_host_names_only(self):
        for bad in ("http://cdn.benzinga.com/a.png", "https://127.0.0.1/a.png", "https://[::1]/a.png",
                    "https://localhost/a.png", "https://intranet/a.png", "https://printer.local/a.png",
                    "https://cdn.benzinga.com:8443/a.png", "https://u:p@cdn.benzinga.com/a.png", "data:image/png;base64,AA",
                    "https://10.0.0.1/a.png"):
            self.assertIsNone(safe_image_url(bad), bad)
        self.assertEqual(safe_image_url(IMG), IMG)
        self.assertEqual(safe_image_url("https://cdn.benzinga.com:443/a.png"), "https://cdn.benzinga.com:443/a.png")

    def test_sanitize_item(self):
        raw = raw_news(5, NOW, headline="<b>Big</b> &amp; bold", summary="<p>" + "word " * 300 + "</p>",
                       url="javascript:alert(1)", symbols=["aapl", "BTCUSD", "bad tag", "AAPL"],
                       images=[{"size": "large", "url": "https://cdn.benzinga.com/l.jpg"},
                               {"size": "small", "url": "http://cdn.benzinga.com/s.jpg"},
                               {"size": "thumb", "url": IMG}, {"size": "thumb", "url": IMG + "?2"}])
        it = sanitize_item(raw)
        self.assertEqual(it["headline"], "Big & bold")
        self.assertLessEqual(len(it["summary"]), limits.MAX_NEWS_SUMMARY_CHARS)
        self.assertTrue(it["summary"].endswith("…"))
        self.assertIsNone(it["url"])
        self.assertEqual(it["symbols"], ["AAPL", "BTCUSD"])
        self.assertEqual(it["images"], [{"size": "thumb", "url": IMG}])        # large dropped, http dropped, 1/size
        self.assertEqual(it["publisher"], "benzinga")
        stream = {"T": "n", "id": 9, "headline": "H", "created_at": "2026-10-15T14:00:00Z", "source": "benzinga"}
        self.assertEqual(sanitize_item(stream)["publisher"], "benzinga")
        for bad in (dict(stream, id=None), dict(stream, id=True), dict(stream, id=-1), dict(stream, headline="<b></b>"),
                    dict(stream, created_at=None), dict(stream, created_at="yesterday"), "x"):
            self.assertIsNone(sanitize_item(bad))
        self.assertEqual(sanitize_item(dict(stream, id="123"))["id"], 123)


class Case(unittest.TestCase):
    def setUp(self):
        self._ts = TempStore()
        self._ts.__enter__()
        self.data = FakeData()
        self.mono = Mono()
        self.fetched = []
        outer = self

        class Fetcher:
            def fetch(self, url):
                outer.fetched.append(url)
                return "image/jpeg", b"\xff\xd8\xff"
        self.svc = ResearchService(supervisor=ResearchSupervisor(), data=self.data)
        self.svc.news = NewsService(lambda: self.data, fetcher=Fetcher(), monotonic=self.mono)
        self.svc.news.logos = self.svc.logos
        self.api = mr_api.ResearchApi(self.svc)

    def tearDown(self):
        self._ts.__exit__(None, None, None)

    def call(self, name, query=(), body=NO_BODY, **match):
        status, payload = run(getattr(self.api, name)(Q(query), body, match))
        if not isinstance(payload, mr_api.Binary):
            json.loads(dumps(payload))
        return status, payload

    def news(self, symbol="AAPL", **q):
        return self.call("symbol_news", tuple((k, str(v)) for k, v in q.items()), symbol=symbol)


class RestNewsTest(Case):
    def test_shape_order_and_source_label(self):
        self.data.sources.rows = [raw_news(1, NOW - timedelta(hours=3)),
                                  raw_news(2, NOW - timedelta(hours=1), images=[{"size": "thumb", "url": IMG}]),
                                  raw_news(3, NOW - timedelta(hours=2), symbols=["MSFT"])]
        st, p = self.news(limit=5)
        self.assertEqual(st, 200, p)
        self.assertEqual(set(p), {"symbol", "source", "source_label", "items", "thumbnails", "fetched_at", "has_more",
                                  "next_before", "note"})
        self.assertEqual((p["symbol"], p["source"], p["source_label"]), ("AAPL", "Alpaca · Benzinga", "Benzinga via Alpaca"))
        self.assertEqual([i["id"] for i in p["items"]], [2, 1])
        it = p["items"][0]
        self.assertEqual(set(it), {"id", "headline", "summary", "publisher", "source_label", "url", "created_at",
                                   "updated_at", "symbols", "images"})
        self.assertEqual(it["images"], [{"size": "thumb", "path": "/research/news/images/" + image_key(IMG)}])
        self.assertEqual(it["url"], "https://www.benzinga.com/news/2")
        self.assertEqual((p["has_more"], p["next_before"], p["fetched_at"]), (False, iso(NOW - timedelta(hours=3)), iso(NOW)))
        self.assertTrue(p["thumbnails"])
        self.assertNotIn(IMG, dumps(p))                     # the third-party image URL never reaches the app

    def test_runs_in_research_priority_on_the_research_pool(self):
        seen = []
        orig = self.data.sources.news

        def spy(*a, **kw):
            import threading
            seen.append((http_gate.is_research_priority(), threading.current_thread().name))
            return orig(*a, **kw)
        self.data.sources.news = spy
        self.assertEqual(self.news()[0], 200)
        self.assertTrue(seen[0][0])
        self.assertTrue(seen[0][1].startswith("research"))

    def test_refresh_spacing_paging_and_dedupe(self):
        self.data.sources.rows = [raw_news(i, NOW - timedelta(minutes=10 * i)) for i in range(1, 8)]
        self.assertEqual([i["id"] for i in self.news(limit=3)[1]["items"]], [1, 2, 3])
        self.news(limit=3)                                  # within NEWS_REFRESH_SECONDS: from the store
        self.news(limit=2)
        self.assertEqual(len(self.data.sources.news_calls), 1)
        self.news(limit=5)                                  # larger page than the stored answer → Alpaca again
        self.assertEqual(len(self.data.sources.news_calls), 2)
        self.mono.t += limits.NEWS_REFRESH_SECONDS
        self.news(limit=3)
        self.assertEqual(len(self.data.sources.news_calls), 3)
        st, p = self.news(limit=3, before=iso(NOW - timedelta(minutes=30)))
        self.assertEqual([i["id"] for i in p["items"]], [4, 5, 6])
        self.assertEqual(self.data.sources.news_calls[-1][1], NOW - timedelta(minutes=30, seconds=1))   # end inclusive
        self.assertTrue(p["has_more"])
        with sqlite3.connect(store.db_path()) as c:          # ids 1–6 seen in 4 answers, each stored once (de-dup)
            self.assertEqual(c.execute("SELECT COUNT(*), COUNT(DISTINCT id) FROM news").fetchone(), (6, 6))
            self.assertEqual(c.execute("SELECT COUNT(*) FROM news_symbols").fetchone(), (6,))

    def test_update_replaces_only_when_newer(self):
        t = NOW - timedelta(hours=1)
        res = store.news_upsert([sanitize_item(raw_news(1, t, headline="v1"))], iso(NOW), "rest", image_key)
        self.assertEqual(res, {1: "new"})
        res = store.news_upsert([sanitize_item(raw_news(1, t, headline="v0 replayed"))], iso(NOW), "stream", image_key)
        self.assertEqual(res, {1: "same"})
        res = store.news_upsert([sanitize_item(raw_news(1, t, headline="v2", updated=t + timedelta(minutes=5)))],
                                iso(NOW), "rest", image_key)
        self.assertEqual(res, {1: "updated"})
        self.assertEqual(store.news_get(1)["headline"], "v2")

    def test_stream_copy_gets_the_rest_images(self):
        t = NOW - timedelta(hours=1)
        store.news_upsert([sanitize_item(raw_news(1, t))], iso(NOW), "stream", image_key)
        store.news_upsert([sanitize_item(raw_news(1, t, images=[{"size": "thumb", "url": IMG}]))], iso(NOW), "rest",
                          image_key)
        self.assertEqual(store.news_get(1)["images"], [{"size": "thumb", "url": IMG}])
        self.assertEqual(store.news_image_get(image_key(IMG))["status"], "pending")

    def test_failure_is_503_with_reason_never_stored_news(self):
        self.data.sources.rows = [raw_news(1, NOW - timedelta(hours=1))]
        self.assertEqual(self.news()[0], 200)
        self.mono.t += 3600
        self.data.sources.news_fail = DataSourceUnavailable("alpaca", "news", "network/TLS error")
        st, p = self.news()
        self.assertEqual(st, 503)
        self.assertTrue(p["error"].startswith("News unavailable: alpaca is unavailable for news: network/TLS error"))
        self.assertEqual(p["kind"], "DataSourceUnavailable")
        self.data.sources.news_fail = SecretMissing("alpaca-paper-key-id", "Alpaca key id")
        st, p = self.news(limit=7)
        self.assertEqual((st, p["kind"]), (503, "SecretMissing"))
        self.assertIn("Alpaca key id is not set", p["error"])

    def test_validation(self):
        self.assertEqual(self.news(foo=1)[0], 400)
        for lim in ("0", "51", "x", "-1"):
            self.assertEqual(self.news(limit=lim)[0], 400, lim)
        self.assertEqual(self.news(before="yesterday")[0], 400)
        self.assertEqual(self.news(before="2" * 41)[0], 400)
        self.assertEqual(self.call("symbol_news", (("limit", "1"), ("limit", "2")), symbol="AAPL")[0], 400)
        self.assertEqual(self.news(symbol="bad sym")[0], 400)
        st, p = self.news(symbol="ZZZZ")
        self.assertEqual((st, p["kind"]), (404, "SymbolNotCovered"))
        self.assertEqual(self.data.sources.news_calls, [])

    def test_crypto_uses_the_news_tag(self):
        self.data.sources.rows = [raw_news(1, NOW - timedelta(hours=1), symbols=["BTCUSD"])]
        st, p = self.news(symbol="btc-usd")
        self.assertEqual((st, [i["id"] for i in p["items"]], p["symbol"]), (200, [1], "BTC-USD"))


class RetentionTest(Case):
    def test_seven_days_news_symbols_and_images(self):
        old, new = NOW - timedelta(days=8), NOW - timedelta(days=1)
        store.news_upsert([sanitize_item(raw_news(1, old, images=[{"size": "thumb", "url": IMG}])),
                           sanitize_item(raw_news(2, new, images=[{"size": "thumb", "url": IMG + "?n"}]))],
                          iso(NOW), "rest", image_key)
        store.logo_set("AAPL", "none", iso(NOW - timedelta(days=31)))
        store.logo_set("MSFT", "none", iso(NOW - timedelta(days=1)))
        deleted = store.prune(NOW)
        self.assertEqual((deleted["news"], deleted["news_symbols"], deleted["news_images"], deleted["logos"]), (1, 1, 1, 1))
        self.assertIsNone(store.news_get(1))
        self.assertIsNotNone(store.news_get(2))
        self.assertIsNone(store.news_image_get(image_key(IMG)))
        self.assertIsNotNone(store.news_image_get(image_key(IMG + "?n")))
        self.assertIsNone(store.logo_get("AAPL"))
        self.assertEqual([n["id"] for n in store.news_for_symbol("AAPL", 10)], [2])

    def test_shared_image_follows_the_newest_item(self):
        store.news_upsert([sanitize_item(raw_news(1, NOW - timedelta(days=8), images=[{"size": "thumb", "url": IMG}]))],
                          iso(NOW), "rest", image_key)
        store.news_upsert([sanitize_item(raw_news(2, NOW - timedelta(days=1), images=[{"size": "thumb", "url": IMG}]))],
                          iso(NOW), "rest", image_key)
        store.prune(NOW)
        self.assertIsNotNone(store.news_image_get(image_key(IMG)))

    def test_schema_v5(self):
        self.assertGreaterEqual(store.SCHEMA_VERSION, 5)
        with sqlite3.connect(store.db_path()) as c:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"news", "news_symbols", "news_images", "logos"} <= tables)


class ThumbnailSettingTest(Case):
    def test_default_on_then_off_hides_paths_and_blocks_the_proxy(self):
        self.data.sources.rows = [raw_news(1, NOW - timedelta(hours=1), images=[{"size": "thumb", "url": IMG}])]
        st, s = self.call("news_settings_get")
        self.assertEqual(st, 200)
        self.assertEqual(set(s), {"thumbnails", "note", "source_label", "logos"})
        self.assertTrue(s["thumbnails"])
        self.assertIn("see your IP address", s["note"])
        path = self.news()[1]["items"][0]["images"][0]["path"]
        key = path.rsplit("/", 1)[1]
        st, s = self.call("news_settings_put", body={"thumbnails": False})
        self.assertEqual((st, s["thumbnails"]), (200, False))
        st, p = self.news()
        self.assertEqual((p["thumbnails"], p["items"][0]["images"]), (False, []))
        st, e = self.call("news_image", key=key)
        self.assertEqual((st, e["kind"]), (409, "ThumbnailsOff"))
        self.assertEqual(self.fetched, [])                 # no third-party request while off
        self.assertTrue(any(a["action"] == "news_thumbnails_set" for a in store.list_audit()))
        self.assertEqual(self.call("news_settings_put", body={"thumbnails": True})[1]["thumbnails"], True)
        st, b = self.call("news_image", key=key)
        self.assertEqual((st, b.content_type), (200, "image/jpeg"))
        self.assertEqual(self.fetched, [IMG])

    def test_put_validation(self):
        for body in (NO_BODY, [], {"thumbnails": "no"}, {"thumbnails": 1}, {"thumbnails": True, "x": 1}, {}):
            self.assertEqual(self.call("news_settings_put", body=body)[0], 400, body)
        self.assertEqual(self.call("news_settings_get", (("x", "1"),))[0], 400)

    def test_routes_registered(self):
        names = {(m, p): n for m, p, n, _ in mr_api.ROUTES}
        for m, p in (("GET", "/research/symbols/{symbol}/news"), ("GET", "/research/symbols/{symbol}/logo"),
                     ("GET", "/research/news/images/{key}"), ("GET", "/research/news/settings"),
                     ("PUT", "/research/news/settings")):
            self.assertIn((m, p), names)
        self.assertEqual(len(names), len(mr_api.ROUTES))
        self.assertIn("news_stream_store", [t["name"] for t in self.svc.supervisor.status()["tasks"]])


# --- the news stream through the hub ------------------------------------------------------------------
from market_research.hub.hub import MarketDataHub   # noqa: E402

try:
    from mr_hub_testkit import FAST, Clock, FakeAlpaca, FakeData as HubData, Sink, keys, running, wait_for
except ImportError:  # pragma: no cover
    from tests.mr_hub_testkit import FAST, Clock, FakeAlpaca, FakeData as HubData, Sink, keys, running, wait_for


def stream_msg(nid, *, symbols=("AAPL",), headline="Stream <b>news</b>", updated="2026-10-15T14:00:00Z", images=None):
    m = {"T": "n", "id": nid, "headline": headline, "summary": "<p>sum</p>", "source": "benzinga",
         "url": "https://www.benzinga.com/x", "created_at": "2026-10-15T14:00:00Z", "updated_at": updated,
         "symbols": list(symbols)}
    if images is not None:
        m["images"] = images
    return m


class StreamTest(unittest.TestCase):
    def setUp(self):
        self._ts = TempStore()
        self._ts.__enter__()

    def tearDown(self):
        self._ts.__exit__(None, None, None)

    def make(self):
        async def ok_probe():
            return True
        srv = FakeAlpaca()
        hub = MarketDataHub(transport=srv, keys=keys, probe=ok_probe, next_probe_in=lambda: 0.0,
                            data=lambda: HubData(), config=FAST, now=Clock())
        news = NewsService(lambda: FakeData())
        hub.news_hook = news.on_stream_item
        return srv, hub, news

    def test_routing_by_symbol_dedupe_update_and_store(self):
        srv, hub, news = self.make()
        a, m, both = Sink("news:AAPL"), Sink("news:MSFT"), Sink("news:AAPL", "news:MSFT")
        for s in (a, m, both):
            hub.register(s)

        async def body():
            store_task = asyncio.ensure_future(news.run())
            try:
                self.assertTrue(await wait_for(lambda: srv.live("news") and srv.live("news")[0].subs == {"AAPL", "MSFT"}))
                conn = srv.live("news")[0]
                conn.push([stream_msg(11, images=[{"size": "thumb", "url": IMG}])])
                self.assertTrue(await wait_for(lambda: a.of("news")))
                item = a.of("news")[0]["item"]
                self.assertEqual(item["headline"], "Stream news")
                self.assertEqual(item["summary"], "sum")
                self.assertEqual(item["source_label"], "Benzinga via Alpaca")
                self.assertEqual(item["images"], [{"size": "thumb", "path": "/research/news/images/" + image_key(IMG)}])
                self.assertEqual(m.of("news"), [])               # MSFT subscriber does not get AAPL news
                self.assertEqual(len(both.of("news")), 1)        # one message even with two matching topics
                conn.push([stream_msg(11, images=[{"size": "thumb", "url": IMG}])])   # repeat: dropped
                conn.push([stream_msg(12, symbols=["MSFT"])])
                self.assertTrue(await wait_for(lambda: m.of("news")))
                self.assertEqual(len(a.of("news")), 1)
                conn.push([stream_msg(11, headline="Corrected", updated="2026-10-15T14:05:00Z")])  # update: re-sent
                self.assertTrue(await wait_for(lambda: len(a.of("news")) == 2))
                self.assertEqual(a.of("news")[1]["item"]["headline"], "Corrected")
                conn.push([stream_msg(13, symbols=["NVDA"])])    # not subscribed by anybody
                await asyncio.sleep(0.05)
                self.assertEqual((len(a.of("news")), len(m.of("news"))), (2, 1))
                self.assertTrue(await wait_for(lambda: store.news_get(11) is not None and
                                               store.news_get(11)["headline"] == "Corrected", timeout=3))
                self.assertEqual(store.news_get(12)["origin"], "stream")
                self.assertEqual([n["id"] for n in store.news_for_symbol("MSFT", 5)], [12])
            finally:
                store_task.cancel()
                await asyncio.gather(store_task, return_exceptions=True)
        run(running(hub, body))

    def test_thumbnails_off_stream_items_without_images(self):
        srv, hub, news = self.make()
        store.meta_set(news_mod.META_THUMBNAILS, "off")
        news.thumbnails_enabled()
        a = Sink("news:AAPL")
        hub.register(a)

        async def body():
            self.assertTrue(await wait_for(lambda: srv.live("news")))
            srv.live("news")[0].push([stream_msg(21, images=[{"size": "thumb", "url": IMG}])])
            self.assertTrue(await wait_for(lambda: a.of("news")))
            self.assertEqual(a.of("news")[0]["item"]["images"], [])
        run(running(hub, body))

    def test_without_the_mr6_service_no_image_urls_pass(self):
        async def ok_probe():
            return True
        srv = FakeAlpaca()
        hub = MarketDataHub(transport=srv, keys=keys, probe=ok_probe, next_probe_in=lambda: 0.0,
                            data=lambda: HubData(), config=FAST, now=Clock())
        a = Sink("news:AAPL")
        hub.register(a)

        async def body():
            self.assertTrue(await wait_for(lambda: srv.live("news")))
            srv.live("news")[0].push([stream_msg(31, images=[{"size": "thumb", "url": IMG}])])
            self.assertTrue(await wait_for(lambda: a.of("news")))
            self.assertEqual(a.of("news")[0]["item"]["images"], [])
        run(running(hub, body))

    def test_hook_failure_does_not_stop_the_stream(self):
        srv, hub, news = self.make()

        def boom(item, syms):
            raise RuntimeError("x")
        hub.news_hook = boom
        a = Sink("news:AAPL")
        hub.register(a)

        async def body():
            self.assertTrue(await wait_for(lambda: srv.live("news")))
            srv.live("news")[0].push([stream_msg(41, images=[{"size": "thumb", "url": IMG}])])
            self.assertTrue(await wait_for(lambda: a.of("news")))
            self.assertEqual(a.of("news")[0]["item"]["images"], [])
            self.assertTrue(hub.running)
        run(running(hub, body))

    def test_public_item_shape(self):
        it = sanitize_item(raw_news(1, NOW, images=[{"size": "thumb", "url": IMG}]))
        self.assertEqual(public_item(it, False)["images"], [])
        self.assertEqual(public_item(it, True)["images"][0]["path"], "/research/news/images/" + image_key(IMG))


if __name__ == "__main__":
    unittest.main()
