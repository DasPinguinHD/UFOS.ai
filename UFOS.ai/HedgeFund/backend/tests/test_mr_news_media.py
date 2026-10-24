"""Market Research MR6 — thumbnail proxy + logos (SPEC §13 "MR6 backend notes"): image checks (type
allow-list png/jpeg/webp/gif, SVG rejected, signature must match, size and pixel caps), the proxy fetcher
(https + public hosts only, checked redirects, Content-Length / streamed size limit, timeouts, HTTP errors),
the proxy cache (ok cached, rejected never retried, failed retried after the window, unknown keys), the
provider's ``logo`` op (PNG checks, 404 → none, 401/403 → not on this plan, keys in headers only), the logo
cache (30 days ok, 7 days none, plan refusal remembered + re-checked) and the routes incl. the binary
answer through ``api.register``. Fakes only; no network.

Run from HedgeFund/backend:  python -m unittest tests.test_mr_news_media -v
"""
import asyncio
import json
import logging
import socket
import sys
import types
import unittest
from datetime import timedelta
from unittest import mock

try:
    from mr_data_testkit import TempStore
    from mr_news_testkit import NOW, FakeData, Mono, Q, gif, iso, jpeg, png, raw_news, webp
except ImportError:  # pragma: no cover - run as package
    from tests.mr_data_testkit import TempStore
    from tests.mr_news_testkit import NOW, FakeData, Mono, Q, gif, iso, jpeg, png, raw_news, webp

from market_research import ResearchService, api as mr_api, limits, store
from market_research.logos import LogoService
from market_research.media import ImageFetcher, ImageFetchFailed, ImageRejected, check_image, dimensions, sniff
from market_research.news import NewsService, image_key
from market_research.news_text import sanitize_item
from market_research.supervisor import ResearchSupervisor
from strategy_platform.api.routes import NO_BODY, dumps
from strategy_platform.core.errors import DataSourceUnavailable, UserFacingError
from strategy_platform.data.http_gate import HttpGate
from strategy_platform.data.providers import alpaca as A

IMG = "https://cdn.benzinga.com/files/a.jpeg"


def setUpModule():
    logging.getLogger("market_research").setLevel(logging.CRITICAL + 1)


def tearDownModule():
    logging.getLogger("market_research").setLevel(logging.NOTSET)


def run(c):
    return asyncio.run(c)


class CheckImageTest(unittest.TestCase):
    def test_allowed_types_and_dimensions(self):
        for data, mime, size in ((png(3, 4), "image/png", (3, 4)), (jpeg(10, 20), "image/jpeg", (10, 20)),
                                 (gif(5, 6), "image/gif", (5, 6))):
            self.assertEqual(sniff(data), mime)
            self.assertEqual(dimensions(data, mime), size)
            self.assertEqual(check_image(data, mime + "; charset=binary"), mime)
        self.assertEqual((sniff(webp(16, 9)), dimensions(webp(16, 9), "image/webp")), ("image/webp", (16, 9)))
        with self.assertRaises(ImageRejected):                # review L6: WebP is no longer accepted
            check_image(webp(16, 9), "image/webp")
        self.assertEqual(check_image(jpeg(), "image/jpg"), "image/jpeg")

    def test_rejections(self):
        cases = [(b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml", "SVG"),
                 (b"<svg/>", "image/png", "SVG"),
                 (b"  <?xml version='1.0'?><svg/>", "image/png", "SVG"),
                 (png(), "text/html", "not allowed"),
                 (png(), "image/bmp", "not allowed"),
                 (png(), "", "not allowed"),
                 (jpeg(), "image/png", "does not match"),
                 (b"GIF89a", "image/gif", "header could not be read"),
                 (png(5000, 10), "image/png", "pixels per side"),
                 (png(0, 10), "image/png", "header could not be read"),
                 (png(pad=limits.MAX_NEWS_IMAGE_BYTES), "image/png", "larger than 512 KB")]
        for data, ctype, why in cases:
            with self.assertRaises(ImageRejected, msg=(ctype, why)) as cm:
                check_image(data, ctype)
            self.assertIn(why, cm.exception.user_message)


class Resp:
    def __init__(self, status=200, headers=None, body=b"", chunk=65536, on_read=None):
        self.status, self.headers = status, dict(headers or {})
        self._body, self._chunk, self._pos, self.on_read, self.closed = body, chunk, 0, on_read, False

    def read(self, n):
        if self.on_read:
            self.on_read()
        out = self._body[self._pos:self._pos + min(n, self._chunk)]
        self._pos += len(out)
        return out

    def close(self):
        self.closed = True


class Opener:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, url, ip, headers, timeout, deadline):
        self.calls.append((url, headers, timeout, ip))
        a = self.answers.pop(0)
        if isinstance(a, BaseException):
            raise a
        return a


def public(host, timeout=None):
    return ["93.184.216.34"]


def gate():
    return HttpGate("news_images_test", 6000, capacity=100, max_attempts=1, acquire_timeout=1)


class FetcherTest(unittest.TestCase):
    def fetcher(self, *answers, resolver=public, mono=None):
        self.op = Opener(*answers)
        return ImageFetcher(opener=self.op, resolver=resolver, gate=gate(), monotonic=mono or Mono())

    def test_ok_with_headers(self):
        f = self.fetcher(Resp(200, {"Content-Type": "image/jpeg", "Content-Length": "60"}, jpeg()))
        self.assertEqual(f.fetch(IMG), ("image/jpeg", jpeg()))
        url, headers, timeout, ip = self.op.calls[0]
        self.assertEqual(ip, "93.184.216.34")                 # review L5: the checked IP is the one connected to
        self.assertEqual(url, IMG)
        self.assertEqual(set(headers), {"User-Agent", "Accept", "Accept-Encoding", "Connection"})   # no cookies/keys
        self.assertLessEqual(timeout, limits.NEWS_IMAGE_TIMEOUT_SECONDS)

    def test_oversize_by_header_and_by_stream(self):
        with self.assertRaises(ImageRejected):
            self.fetcher(Resp(200, {"Content-Type": "image/png", "Content-Length": str(10 ** 7)}, png())).fetch(IMG)
        big = png(pad=limits.MAX_NEWS_IMAGE_BYTES)
        r = Resp(200, {"Content-Type": "image/png"}, big, chunk=100000)
        with self.assertRaises(ImageRejected) as cm:
            self.fetcher(r).fetch(IMG)
        self.assertIn("larger than 512 KB", cm.exception.user_message)
        self.assertTrue(r.closed)
        self.assertLessEqual(r._pos, limits.MAX_NEWS_IMAGE_BYTES + 100000)   # stopped reading at the cap

    def test_wrong_type_and_svg(self):
        for ctype, body in (("text/html", b"<html>"), ("image/svg+xml", b"<svg/>"), ("image/png", b"<svg/>"),
                            ("application/octet-stream", png()), ("image/webp", webp())):
            with self.assertRaises(ImageRejected, msg=ctype):
                self.fetcher(Resp(200, {"Content-Type": ctype}, body)).fetch(IMG)

    def test_timeouts(self):
        with self.assertRaises(ImageFetchFailed) as cm:
            self.fetcher(socket.timeout("timed out")).fetch(IMG)
        self.assertIn("did not answer in time", cm.exception.user_message)
        mono = Mono()

        def slow():
            mono.t += limits.NEWS_IMAGE_TOTAL_SECONDS + 1
        with self.assertRaises(ImageFetchFailed) as cm:
            self.fetcher(Resp(200, {"Content-Type": "image/png"}, png() * 10, chunk=10, on_read=slow),
                         mono=mono).fetch(IMG)
        self.assertIn("took too long", cm.exception.user_message)
        with self.assertRaises(ImageFetchFailed):
            self.fetcher(OSError("refused")).fetch(IMG)

    def test_http_errors(self):
        with self.assertRaises(ImageFetchFailed) as cm:
            self.fetcher(Resp(404)).fetch(IMG)
        self.assertIn("HTTP 404", cm.exception.user_message)

    def test_unsafe_urls_and_hosts(self):
        for url in ("http://cdn.benzinga.com/a.png", "https://127.0.0.1/a.png", "https://localhost/a.png"):
            with self.assertRaises(ImageRejected):
                self.fetcher().fetch(url)
        with self.assertRaises(ImageRejected) as cm:
            self.fetcher(resolver=lambda h, t: ["10.1.2.3"]).fetch(IMG)
        self.assertIn("private or local", cm.exception.user_message)
        with self.assertRaises(ImageRejected):
            self.fetcher(resolver=lambda h, t: ["93.184.216.34", "127.0.0.1"]).fetch(IMG)

        def nx(h, t):
            raise socket.gaierror("nx")
        with self.assertRaises(ImageFetchFailed):
            self.fetcher(resolver=nx).fetch(IMG)
        self.assertEqual(self.op.calls, [])

    def test_redirects_are_checked(self):
        ok = Resp(200, {"Content-Type": "image/gif"}, gif())
        f = self.fetcher(Resp(302, {"Location": "/b.gif"}), ok)
        self.assertEqual(f.fetch(IMG)[0], "image/gif")
        self.assertEqual(self.op.calls[1][0], "https://cdn.benzinga.com/b.gif")
        for loc in ("http://cdn.benzinga.com/b.gif", "https://169.254.169.254/latest", "file:///etc/passwd"):
            with self.assertRaises(ImageRejected, msg=loc):
                self.fetcher(Resp(301, {"Location": loc})).fetch(IMG)
        hops = [Resp(302, {"Location": f"/{i}.gif"}) for i in range(limits.NEWS_IMAGE_MAX_REDIRECTS + 1)]
        with self.assertRaises(ImageRejected) as cm:
            self.fetcher(*hops).fetch(IMG)
        self.assertIn("redirects", cm.exception.user_message)


class ProxyCacheTest(unittest.TestCase):
    def setUp(self):
        self._ts = TempStore()
        self._ts.__enter__()
        self.answers = []
        self.urls = []
        outer = self

        class F:
            def fetch(self, url):
                outer.urls.append(url)
                a = outer.answers.pop(0)
                if isinstance(a, BaseException):
                    raise a
                return a
        self.news = NewsService(lambda: FakeData(), fetcher=F())
        store.news_upsert([sanitize_item(raw_news(1, NOW, images=[{"size": "thumb", "url": IMG}]))], iso(NOW), "rest",
                          image_key)
        self.key = image_key(IMG)

    def tearDown(self):
        self._ts.__exit__(None, None, None)

    def test_ok_is_cached(self):
        self.answers = [("image/jpeg", jpeg())]
        self.assertEqual(self.news.image(self.key), ("image/jpeg", jpeg()))
        self.assertEqual(self.news.image(self.key), ("image/jpeg", jpeg()))
        self.assertEqual(self.urls, [IMG])

    def test_rejected_is_not_retried(self):
        self.answers = [ImageRejected("SVG/markup images are not accepted")]
        for _ in range(2):
            with self.assertRaises(UserFacingError) as cm:
                self.news.image(self.key)
            self.assertIn("Thumbnail unavailable: SVG", cm.exception.user_message)
        self.assertEqual(len(self.urls), 1)
        self.assertEqual(store.news_image_get(self.key)["status"], "rejected")

    def test_failed_is_retried_after_the_window(self):
        self.answers = [ImageFetchFailed("the image server did not answer in time"), ("image/gif", gif())]
        with self.assertRaises(UserFacingError):
            self.news.image(self.key)
        with self.assertRaises(UserFacingError):
            self.news.image(self.key)                     # inside the retry window: no new request
        self.assertEqual(len(self.urls), 1)
        old = iso(NOW - timedelta(seconds=limits.NEWS_IMAGE_RETRY_SECONDS + 5))
        import sqlite3
        with sqlite3.connect(store.db_path()) as c:
            c.execute("UPDATE news_images SET fetched_at = ?", (old,))
        self.assertEqual(self.news.image(self.key)[0], "image/gif")
        self.assertEqual(len(self.urls), 2)

    def test_unknown_key(self):
        from market_research.news import UnknownImage
        with self.assertRaises(UnknownImage):
            self.news.image("0" * 64)


# --- logos ------------------------------------------------------------------------------------------

class FakeSecrets:
    @staticmethod
    def require_secret(name):
        return {"alpaca-paper-key-id": "KEYID", "alpaca-paper-secret-key": "SECRET"}[name]


class LResp:
    def __init__(self, status, ctype="image/png", body=b"", text=""):
        self.status_code, self.headers, self.body, self.text, self.closed = status, {"Content-Type": ctype}, body, text, False

    def iter_content(self, n):
        for i in range(0, len(self.body), n):
            yield self.body[i:i + n]

    def close(self):
        self.closed = True


class LSession:
    def __init__(self, owner):
        self.owner = owner

    def get(self, url, **kw):
        self.owner.calls.append((url, kw))
        return self.owner.answer

    def close(self):
        self.owner.closed += 1


class ProviderLogoTest(unittest.TestCase):
    def provider(self, answer):
        self.calls, self.closed, self.answer = [], 0, answer
        return A.AlpacaProvider(gate=HttpGate("alpaca_logo_test", 6000, max_attempts=1), secrets_module=FakeSecrets,
                                http=lambda: LSession(self))

    def test_ok_and_request(self):
        p = self.provider(LResp(200, body=png(64, 64)))
        self.assertEqual(p.logo("brk.b"), png(64, 64))
        url, kw = self.calls[0]
        self.assertEqual(url, "https://data.alpaca.markets/v1beta1/logos/BRK.B")
        self.assertEqual((kw["headers"]["APCA-API-KEY-ID"], kw["allow_redirects"], kw["stream"]), ("KEYID", False, True))
        self.assertNotIn("placeholder", url)
        self.assertEqual(self.closed, 1)
        self.provider(LResp(200, body=png()))
        p.logo("BTC-USD")
        self.assertTrue(self.calls[-1][0].endswith("/logos/BTCUSD"))

    def test_404_none_and_plan_refusal(self):
        self.assertIsNone(self.provider(LResp(404, ctype="application/json")).logo("AAPL"))
        for status in (401, 403):
            with self.assertRaises(A.LogosNotAvailable) as cm:
                self.provider(LResp(status, ctype="application/json", text='{"message":"forbidden"}')).logo("AAPL")
            self.assertIn(f"HTTP {status}", cm.exception.reason)
            self.assertIn("paid add-on", cm.exception.reason)
            self.assertNotIn("SECRET", cm.exception.user_message)

    def test_checks(self):
        for answer, why in ((LResp(200, ctype="image/jpeg", body=jpeg()), "not a PNG"),
                            (LResp(200, ctype="image/svg+xml", body=b"<svg/>"), "not a PNG"),
                            (LResp(200, body=png(pad=A.LOGO_MAX_BYTES)), "larger than"),
                            (LResp(200, body=b"GIF89a....."), "not a valid PNG"),
                            (LResp(302, ctype="text/html"), "HTTP 302")):
            with self.assertRaises(DataSourceUnavailable, msg=why) as cm:
                self.provider(answer).logo("AAPL")
            self.assertIn(why, cm.exception.user_message)

    def test_news_images_https_only(self):
        self.assertEqual(A._news_images([{"size": "thumb", "url": "https://a.test/t.jpg"},
                                         {"size": "small", "url": "http://a.test/s.jpg"},
                                         {"size": "large", "url": "javascript:x"}, "junk"]),
                         [{"size": "thumb", "url": "https://a.test/t.jpg"}])


class LogoCase(unittest.TestCase):
    def setUp(self):
        self._ts = TempStore()
        self._ts.__enter__()
        self.data = FakeData()
        self.svc = ResearchService(supervisor=ResearchSupervisor(), data=self.data)
        self.svc.logos = LogoService(lambda: self.data)
        self.svc.news.logos = self.svc.logos
        self.api = mr_api.ResearchApi(self.svc)

    def tearDown(self):
        self._ts.__exit__(None, None, None)

    def logo(self, symbol="AAPL", query=()):
        return run(self.api.symbol_logo(Q(query), NO_BODY, {"symbol": symbol}))


class LogoServiceTest(LogoCase):
    def test_ok_cached_30_days(self):
        self.data.sources.logo_result["AAPL"] = png(64, 64)
        st, b = self.logo()
        self.assertEqual((st, b.content_type, b.data), (200, "image/png", png(64, 64)))
        self.assertEqual(b.headers()["X-Content-Type-Options"], "nosniff")
        self.logo()
        self.assertEqual(self.data.sources.logo_calls, ["AAPL"])
        self.data.advance(days=31)
        self.logo()
        self.assertEqual(self.data.sources.logo_calls, ["AAPL", "AAPL"])
        self.assertEqual(self.svc.logos.status()["state"], "available")

    def test_none_cached_7_days(self):
        self.data.sources.logo_result["MSFT"] = None
        st, p = self.logo("MSFT")
        self.assertEqual((st, p), (404, {"error": "No logo available from Alpaca for MSFT.", "kind": "NoLogo"}))
        self.data.advance(days=6)
        self.assertEqual(self.logo("MSFT")[0], 404)
        self.assertEqual(len(self.data.sources.logo_calls), 1)
        self.data.advance(days=2)
        self.logo("MSFT")
        self.assertEqual(len(self.data.sources.logo_calls), 2)

    def test_not_on_plan_remembered_and_rechecked(self):
        self.data.sources.logo_default = A.LogosNotAvailable(403, "forbidden")
        st, p = self.logo()
        self.assertEqual((st, p["kind"]), (404, "LogosNotOnPlan"))
        self.assertTrue(p["error"].startswith("Logos are not available on this Alpaca plan: Alpaca refused the logo "
                                              "request (HTTP 403: forbidden)"))
        self.assertEqual(self.logo("MSFT")[1]["kind"], "LogosNotOnPlan")   # account-wide: no second request
        self.assertEqual(len(self.data.sources.logo_calls), 1)
        st, s = run(self.api.news_settings_get(Q(), NO_BODY, {}))
        self.assertEqual(s["logos"]["state"], "not_on_plan")
        self.assertIn("HTTP 403", s["logos"]["reason"])
        self.data.advance(seconds=limits.LOGO_PLAN_RECHECK_SECONDS + 1)
        self.data.sources.logo_default = png()
        self.assertEqual(self.logo("MSFT")[0], 200)                  # plan changed: works again
        self.assertEqual(self.svc.logos.status()["state"], "available")

    def test_errors_and_validation(self):
        self.data.sources.logo_default = DataSourceUnavailable("alpaca", "logos", "network/TLS error")
        with self.assertRaises(DataSourceUnavailable):               # → 503 by api.register (user_error)
            self.logo()
        self.data.sources.logo_default = png(5000, 5)
        with self.assertRaises(DataSourceUnavailable) as cm:
            self.logo()
        self.assertIn("pixels per side", cm.exception.user_message)
        self.assertIsNone(store.logo_get("AAPL"))                     # nothing cached from a rejected logo
        self.assertEqual(self.logo(query=(("size", "1"),))[0], 400)
        self.assertEqual(self.logo("bad sym")[0], 400)
        self.assertEqual(self.logo("ZZZZ")[1]["kind"], "SymbolNotCovered")


# --- review fixes: deadline-capped download (H1), pinned IP (L5), own pool (M2) -------------------------------
import socket as _socket
import threading as _threading
import time as _time
from market_research import media as media_mod


def _drip_server(head=b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: 100000\r\n\r\n",
                 drip_header=False):
    srv = _socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    stop = _threading.Event()

    def serve():
        try:
            c, _ = srv.accept()
            c.recv(4096)
            if drip_header:
                for b in head:
                    if stop.is_set():
                        break
                    c.sendall(bytes([b]))
                    _time.sleep(0.3)
            else:
                c.sendall(head)
            while not stop.is_set():
                c.sendall(b"x")
                _time.sleep(0.3)
        except OSError:
            pass
    _threading.Thread(target=serve, daemon=True).start()
    return srv, stop


class DeadlineTest(unittest.TestCase):
    def _run(self, drip_header):
        srv, stop = _drip_server(drip_header=drip_header)
        port = srv.getsockname()[1]
        try:
            t0 = _time.monotonic()
            deadline = t0 + 1.5
            with self.assertRaises(ImageFetchFailed) as cm:
                resp = media_mod.pinned_open("https://cdn.example.com/a.png", "127.0.0.1", {"Host": "x"}, 1.0,
                                             deadline, tls=False, port=port)
                try:
                    while resp.read(8192):
                        pass
                finally:
                    resp.close()
            took = _time.monotonic() - t0
            self.assertLess(took, 3.0, took)                 # hard-capped near the deadline (drip: 1 byte/0.3 s)
            self.assertIn("took too long", cm.exception.user_message)
        finally:
            stop.set()
            srv.close()

    def test_drip_body_is_cut_at_the_deadline(self):
        self._run(False)

    def test_drip_headers_are_cut_at_the_deadline(self):
        self._run(True)

    def test_dns_lookup_is_bounded(self):
        gate_ = _threading.Event()

        def slow(*a, **kw):
            gate_.wait(5)
            raise _socket.gaierror("late")
        with mock.patch.object(media_mod.socket, "getaddrinfo", slow):
            t0 = _time.monotonic()
            with self.assertRaises(ImageFetchFailed) as cm:
                media_mod.resolve("cdn.example.com", 0.3)
            self.assertLess(_time.monotonic() - t0, 1.5)
        gate_.set()
        self.assertIn("in time", cm.exception.user_message)

    def test_connects_to_the_checked_ip_with_sni_of_the_host(self):
        seen = {}

        def fake_conn(addr, timeout=None, *a, **kw):
            seen["addr"] = addr
            raise OSError("stop here")
        wrapped = {}
        orig = media_mod.ssl.SSLContext.wrap_socket

        with mock.patch.object(media_mod.socket, "create_connection", fake_conn):
            with self.assertRaises(OSError):
                media_mod.pinned_open("https://cdn.benzinga.com/a.png", "93.184.216.34", {}, 1.0,
                                      _time.monotonic() + 5)
        self.assertEqual(seen["addr"], ("93.184.216.34", 443))
        conn = media_mod._PinnedConnection("cdn.benzinga.com", "93.184.216.34", 443, 1.0)
        self.assertEqual(conn.host, "cdn.benzinga.com")              # Host header + SNI + cert check name
        self.assertTrue(conn._context.check_hostname)
        self.assertEqual(conn._context.verify_mode, media_mod.ssl.CERT_REQUIRED)


class ImagePoolTest(unittest.TestCase):
    """Thumbnails run on their own bounded pool; Research routes stay responsive while images hang."""

    def setUp(self):
        self._ts = TempStore()
        self._ts.__enter__()

    def tearDown(self):
        self._ts.__exit__(None, None, None)

    def test_busy_and_research_routes_unaffected(self):
        release = _threading.Event()
        data = FakeData()
        data.sources.rows = [raw_news(1, NOW - timedelta(hours=1), images=[{"size": "thumb", "url": IMG}])]

        class Hang:
            def fetch(self, url):
                release.wait(10)
                return "image/png", png()
        svc = ResearchService(supervisor=ResearchSupervisor(), data=data)
        svc.news = NewsService(lambda: data, fetcher=Hang())
        svc.news.logos = svc.logos
        api = mr_api.ResearchApi(svc)
        key = image_key(IMG)
        store.news_upsert([sanitize_item(raw_news(1, NOW, images=[{"size": "thumb", "url": IMG}]))], iso(NOW), "rest",
                          image_key)

        async def body():
            jobs = [asyncio.ensure_future(api.news_image(Q(), NO_BODY, {"key": key}))
                    for _ in range(limits.IMAGE_QUEUE_MAX + 2)]
            await asyncio.sleep(0.2)
            t0 = _time.monotonic()
            st, p = await api.symbol_news(Q(), NO_BODY, {"symbol": "AAPL"})   # Research pool: not blocked
            self.assertEqual(st, 200)
            self.assertLess(_time.monotonic() - t0, 1.0)
            done = [j for j in jobs if j.done()]
            self.assertEqual(len(done), 2)                     # the two above the queue cap
            for j in done:
                st, p = j.result()
                self.assertEqual((st, p["kind"]), (503, "ThumbnailBusy"))
            release.set()
            res = await asyncio.gather(*jobs)
            self.assertEqual(sum(1 for st, _ in res if st == 200), limits.IMAGE_QUEUE_MAX)
        run(body())


# --- binary answers through api.register (aiohttp stubbed) ---------------------------------------------

def _stub_aiohttp():
    web = types.ModuleType("aiohttp.web")

    class Response:
        def __init__(self, body=b"", status=200, content_type=None, headers=None, text=None):
            self.body, self.status, self.content_type, self.headers = body if text is None else text, status, content_type, dict(headers or {})
    web.Response = Response
    web.json_response = lambda data, status=200, dumps=json.dumps, headers=None: Response(text=dumps(data), status=status,
                                                                                         content_type="application/json")

    class Router:
        def __init__(self):
            self.routes = {}

        def add_route(self, method, path, handler):
            self.routes[(method, path)] = handler

    class App:
        def __init__(self):
            self.router = Router()
    web.Application = App
    pkg = types.ModuleType("aiohttp")
    pkg.web = web
    return pkg, web


class Req:
    def __init__(self, match):
        self.query, self.match_info, self.can_read_body = Q(), match, False


class RegisterTest(LogoCase):
    def test_binary_and_json_answers(self):
        pkg, web = _stub_aiohttp()
        saved = {k: sys.modules.get(k) for k in ("aiohttp", "aiohttp.web")}
        sys.modules["aiohttp"], sys.modules["aiohttp.web"] = pkg, web
        try:
            app = web.Application()
            mr_api.register(app, self.svc)
            handler = app.router.routes[("GET", "/research/symbols/{symbol}/logo")]
            self.data.sources.logo_result["AAPL"] = png(8, 8)
            r = run(handler(Req({"symbol": "AAPL"})))
            self.assertEqual((r.status, r.content_type, r.body), (200, "image/png", png(8, 8)))
            self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("default-src 'none'", r.headers["Content-Security-Policy"])
            self.data.sources.logo_result["MSFT"] = None
            r = run(handler(Req({"symbol": "MSFT"})))
            self.assertEqual((r.status, json.loads(r.body)["kind"]), (404, "NoLogo"))
            self.data.sources.logo_result["BTC-USD"] = DataSourceUnavailable("alpaca", "logos", "boom")
            r = run(handler(Req({"symbol": "BTC-USD"})))
            self.assertEqual((r.status, json.loads(r.body)["kind"]), (503, "DataSourceUnavailable"))
            img = app.router.routes[("GET", "/research/news/images/{key}")]
            r = run(img(Req({"key": "../../etc/passwd"})))
            self.assertEqual(r.status, 404)
        finally:
            for k, m in saved.items():
                if m is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = m


if __name__ == "__main__":
    unittest.main()
