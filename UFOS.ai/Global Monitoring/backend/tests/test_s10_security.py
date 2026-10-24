"""Stage 10 security audit, fix batch S10-A1 (Global Monitoring backend): OPENROUTER_BASE_URL only https on
openrouter.ai (L1), the article fetch reaches public addresses only + capped redirects + bounded cache (L3), event
logs pruned after 30 days (L6), generic client error messages (L10), no pid on /version (L2). Offline; runs without
aiohttp installed (a minimal stand-in is used then)."""
import asyncio
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


def _ensure_aiohttp():
    """The real aiohttp when installed, else a stand-in with just what assess_context / news_feeds touch at import."""
    if importlib.util.find_spec("aiohttp") is not None:
        return
    mod = types.ModuleType("aiohttp")
    abc = types.ModuleType("aiohttp.abc")

    class AbstractResolver:
        pass

    class DefaultResolver:
        async def resolve(self, host, port=0, family=None):
            raise OSError("no resolver in tests")

        async def close(self):
            pass
    abc.AbstractResolver = AbstractResolver
    mod.abc, mod.DefaultResolver = abc, DefaultResolver
    mod.ClientSession = mod.TCPConnector = mod.ClientTimeout = lambda *a, **k: None
    sys.modules["aiohttp"], sys.modules["aiohttp.abc"] = mod, abc


_ensure_aiohttp()
import assess_context as ac  # noqa: E402


class SafeBaseUrlTest(unittest.TestCase):
    """L1, since S10-E in settings_file (data/settings.json "openrouter_base_url"; an invalid value refuses the start)."""

    def test_only_https_openrouter(self):
        spec = importlib.util.spec_from_file_location("s10sec_settings_file", BACKEND / "settings_file.py")
        sf = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sf)
        self.assertIsNone(sf.openrouter_url_problem("https://openrouter.ai/api/v1"))
        for bad in ("http://openrouter.ai/api/v1", "https://evil.example/api/v1", "https://openrouter.ai.evil.example/",
                    "https://user:pw@openrouter.ai/api/v1", "https://openrouter.ai:8443/api/v1",
                    "https://openrouter.ai/api/v1?x=1", "ftp://openrouter.ai", "openrouter.ai/api/v1", ""):
            self.assertIsNotNone(sf.openrouter_url_problem(bad), bad)

    def test_assess_applies_it_per_call(self):
        src = (BACKEND / "assess.py").read_text(encoding="utf-8")
        self.assertIn("base_url, model = config.ai_settings()", src)


class ArticleFetchGuardTest(unittest.TestCase):
    def test_public_addresses_only(self):
        for ip in ("8.8.8.8", "2606:4700:4700::1111", "151.101.1.140"):
            self.assertTrue(ac.ip_is_public(ip), ip)
        for ip in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
                   "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1", "224.0.0.1", "not-an-ip", ""):
            self.assertFalse(ac.ip_is_public(ip), ip)

    def test_check_url(self):
        self.assertEqual(ac.check_url("https://www.bbc.co.uk/news/x"), "https://www.bbc.co.uk/news/x")
        self.assertEqual(ac.check_url("http://8.8.8.8/x"), "http://8.8.8.8/x")
        for bad in ("http://127.0.0.1:8768/health", "http://[::1]/x", "http://169.254.169.254/latest/meta-data",
                    "http://localhost/x", "http://printer.local/", "file:///etc/passwd", "https://u:p@example.com/",
                    "http:///nohost", "gopher://example.com/"):
            with self.assertRaises(ac.BlockedAddress, msg=bad):
                ac.check_url(bad)

    def test_resolver_drops_non_public_results(self):
        class Inner:
            def __init__(self, ips):
                self.ips = ips

            async def resolve(self, host, port=0, family=None):
                return [{"hostname": host, "host": ip, "port": port, "family": 2, "proto": 0, "flags": 0}
                        for ip in self.ips]

            async def close(self):
                pass
        r = ac.PublicOnlyResolver(Inner(["10.0.0.1", "93.184.216.34"]))
        out = asyncio.run(r.resolve("example.com", 443))
        self.assertEqual([i["host"] for i in out], ["93.184.216.34"])
        with self.assertRaises(OSError):
            asyncio.run(ac.PublicOnlyResolver(Inner(["127.0.0.1", "192.168.0.10"])).resolve("rebind.example", 80))

    def fake_session(self, routes):
        calls = []

        class Content:
            def __init__(self, body):
                self.body = body

            async def read(self, n):
                return self.body[:n]

        class Resp:
            def __init__(self, status, headers=None, body=b""):
                self.status, self.headers, self.charset = status, headers or {}, "utf-8"
                self.content = Content(body)

            def raise_for_status(self):
                if self.status >= 400:
                    raise RuntimeError(f"HTTP {self.status}")

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class Session:
            def get(self, url, **kw):
                calls.append((url, kw.get("allow_redirects")))
                status, headers, body = routes[url]
                return Resp(status, headers, body)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False
        return Session, calls

    def run_fetch(self, routes, url):
        Session, calls = self.fake_session(routes)
        ac._article_cache.clear()
        self.addCleanup(ac._article_cache.clear)
        with mock.patch.object(ac, "SESSION_FACTORY", Session), \
                mock.patch.object(ac.aiohttp, "ClientTimeout", lambda *a, **k: None, create=True):
            text = asyncio.run(ac.fetch_article_text(None, url))
        return text, calls

    def test_redirects_checked_on_every_hop_and_capped(self):
        para = b"<p>" + b"A long enough paragraph about the event for the extractor to keep it. " * 8 + b"</p>"
        ok, calls = self.run_fetch({"https://a.example/1": (302, {"Location": "/2"}, b""),
                                    "https://a.example/2": (200, {}, b"<article>" + para + b"</article>")},
                                   "https://a.example/1")
        self.assertIn("A long enough paragraph", ok)
        self.assertEqual(calls, [("https://a.example/1", False), ("https://a.example/2", False)])
        with self.assertLogs(ac.logger, "WARNING"):
            text, calls = self.run_fetch({"https://a.example/1": (302, {"Location": "http://169.254.169.254/x"}, b"")},
                                         "https://a.example/1")
        self.assertEqual((text, len(calls)), ("", 1))                  # the metadata address is never requested
        loop = {f"https://a.example/{i}": (302, {"Location": f"/{i + 1}"}, b"") for i in range(10)}
        with self.assertLogs(ac.logger, "WARNING"):
            text, calls = self.run_fetch(loop, "https://a.example/0")
        self.assertEqual((text, len(calls)), ("", ac.ARTICLE_MAX_REDIRECTS + 1))
        with self.assertLogs(ac.logger, "WARNING"):
            text, calls = self.run_fetch({}, "http://127.0.0.1:8768/health")
        self.assertEqual((text, calls), ("", []))

    def test_cache_is_bounded_lru(self):
        ac._article_cache.clear()
        self.addCleanup(ac._article_cache.clear)
        with mock.patch.object(ac, "ARTICLE_CACHE_MAX_ENTRIES", 3):
            for i in range(5):
                ac._cache_put(f"u{i}", "t")
            self.assertEqual(list(ac._article_cache), ["u2", "u3", "u4"])
            self.assertEqual(ac._cache_get("u2"), "t")               # touched -> newest
            ac._cache_put("u5", "t")
            self.assertEqual(list(ac._article_cache), ["u4", "u2", "u5"])


class LogPruneTest(unittest.TestCase):
    def test_only_old_day_files(self):
        import types
        saved = sys.modules.get("config")
        sys.modules["config"] = types.SimpleNamespace(LOG_DIR=tempfile.gettempdir())   # logger needs only LOG_DIR
        try:
            spec = importlib.util.spec_from_file_location("s10sec_logger", BACKEND / "logger.py")
            logger = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(logger)
        finally:
            if saved is None:
                sys.modules.pop("config", None)
            else:
                sys.modules["config"] = saved
        with tempfile.TemporaryDirectory() as d:
            for name in ("2026-09-01.jsonl", "2026-09-23.jsonl", "2026-09-24.jsonl", "2026-10-24.jsonl",
                         "backend.log", "process.log", "notes-2020-01-01.jsonl", "2026-13-01.jsonl"):
                Path(d, name).write_text("x", encoding="utf-8")
            removed = logger.prune_old_logs(30, today=date(2026, 10, 24), log_dir=d)
            self.assertEqual(removed, ["2026-09-01.jsonl", "2026-09-23.jsonl"])     # 09-24 = exactly 30 days: kept
            self.assertEqual(sorted(os.listdir(d)), ["2026-09-24.jsonl", "2026-10-24.jsonl", "2026-13-01.jsonl",
                                                     "backend.log", "notes-2020-01-01.jsonl", "process.log"])


class MainSourceTest(unittest.TestCase):
    def test_generic_errors_and_no_pid(self):
        src = (BACKEND / "main.py").read_text(encoding="utf-8")
        self.assertNotIn('{"error": str(ex)}', src)
        self.assertNotIn("No local news to summarize: {ex}", src)
        self.assertNotIn('"pid"', src)
        self.assertIn("prune_old_logs()", src)


if __name__ == "__main__":
    unittest.main()
