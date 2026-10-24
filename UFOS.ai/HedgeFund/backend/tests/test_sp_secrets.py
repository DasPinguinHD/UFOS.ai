"""Secrets wiring (strategy_platform/SPEC.md §2, user decision 2026-10-02):
every HedgeFund key comes ONLY from the Windows Credential Manager
(``strategy_platform.secrets``), read at use time; ``.env`` values are
ignored. Offline: a MemoryStore fakes the Credential Manager, aiohttp /
openai / dotenv / config are stubbed, no network.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_secrets -v
"""
import asyncio
import importlib.util
import logging
import os
import sys
import types
import unittest

from strategy_platform import secrets
from strategy_platform.core.errors import SecretMissing, SecretStoreUnavailable

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_NEWSROOM = os.path.join(_BACKEND, "newsroom")

FRED = secrets.target("fred-api-key")
SEC = secrets.target("sec-edgar-user-agent")
OPENROUTER = secrets.target("openrouter-api-key")


class _Stop(Exception):
    """Ends a collector's endless loop inside a test."""


class _Session:
    def __init__(self, connector=None):
        self.connector = connector              # the collectors pass a TCPConnector with the shared TLS context

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _load(filename, modname, extra_stubs=None):
    """Loads newsroom/<filename> as ``modname`` with stubbed aiohttp/config
    (+ extra stubs); sys.modules is restored afterwards."""
    aiohttp = types.ModuleType("aiohttp")
    aiohttp.ClientSession = _Session
    aiohttp.ClientTimeout = lambda **kw: None
    aiohttp.TCPConnector = lambda **kw: kw
    config = types.ModuleType("config")
    for name, value in {"FRED_POLL_SECONDS": 60, "CALENDAR_POLL_SECONDS": 60, "SEC_EDGAR_POLL_SECONDS": 60,
                        "SEC_EDGAR_MIN_REQUEST_INTERVAL_SECONDS": 0, "OPENROUTER_BASE_URL": "https://openrouter.ai/api/v1",
                        "OPENROUTER_MODEL": "m-standard", "OPENROUTER_ANALYSIS_PREMIUM_MODEL": "m-premium"}.items():
        setattr(config, name, value)
    stubs = {"aiohttp": aiohttp, "config": config, **(extra_stubs or {})}
    names = list(stubs) + [modname]
    saved = {n: sys.modules.get(n) for n in names}
    import newsroom  # noqa: F401  (real package: no imports of its own)
    try:
        sys.modules.update(stubs)
        spec = importlib.util.spec_from_file_location(modname, os.path.join(_NEWSROOM, filename))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[modname] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        for n, m in saved.items():
            if m is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = m


def _ai_stubs():
    openai = types.ModuleType("openai")
    openai.AsyncOpenAI = object
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *a, **k: False          # never read the real .env in tests
    return {"openai": openai, "dotenv": dotenv}


fred = _load("fred_collector.py", "newsroom.fred_collector")
cal = _load("calendar_collector.py", "newsroom.calendar_collector")
sec = _load("sec_edgar_collector.py", "newsroom.sec_edgar_collector")
ai = _load("ai.py", "newsroom.ai", _ai_stubs())
from newsroom import keys  # noqa: E402


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.store = secrets.MemoryStore()
        secrets.set_store(self.store)
        self._env = {k: os.environ.get(k) for k in ("FRED_API_KEY", "SEC_EDGAR_USER_AGENT", "OPENROUTER_API_KEY",
                                                      "ALPACA_API_KEY_ID", "APCA_API_KEY_ID",
                                                      "ALPACA_API_SECRET_KEY", "APCA_API_SECRET_KEY")}
        for k in self._env:
            os.environ.pop(k, None)

    def tearDown(self):
        secrets.set_store(None)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _run_until_status(coro_fn, *args):
    """Runs a collector's run() until its first report_status; returns
    (statuses, emitted)."""
    statuses, emitted = [], []

    async def report_status(ok, msg):
        statuses.append((ok, msg))
        raise _Stop()

    with_emit = coro_fn(emitted.append, report_status, *args)
    try:
        asyncio.run(with_emit)
    except _Stop:
        pass
    return statuses, emitted


class SecretsModuleTest(StoreCase):
    def test_require_and_status_never_contain_values(self):
        with self.assertRaises(SecretMissing) as cm:
            secrets.require_secret("fred-api-key")
        self.assertIn("Hedge Fund → Settings → Keys", cm.exception.user_message)
        self.store.values[FRED] = "fred-123456"
        self.assertEqual(secrets.require_secret("fred-api-key"), "fred-123456")
        os.environ["FRED_API_KEY"] = "env-value-ignored"
        self.addCleanup(os.environ.pop, "FRED_API_KEY", None)
        st = {e["name"]: e for e in secrets.status()}
        self.assertTrue(st["fred-api-key"]["present"])
        self.assertEqual(set(st["fred-api-key"]), {"name", "label", "target", "present", "error"})   # S10-E
        self.assertFalse(st["openrouter-api-key"]["present"])
        self.assertNotIn("fred-123456", repr(st))
        self.assertNotIn("env-value-ignored", repr(st))

    def test_env_value_alone_is_ignored(self):
        os.environ["OPENROUTER_API_KEY"] = "sk-from-env"
        with self.assertRaises(SecretMissing):
            secrets.require_secret("openrouter-api-key")

    def test_store_failure_is_reported_per_entry(self):
        class Broken:
            def read(self, target):
                raise SecretStoreUnavailable("The Windows Credential Manager could not be read.")
        secrets.set_store(Broken())
        st = secrets.status()
        self.assertTrue(all(e["present"] is None and e["error"] for e in st))


class ConfigNoEnvTest(StoreCase):
    def test_config_reads_no_env_file_and_has_no_secrets(self):
        """Stage 10 S10-E: config has no dotenv, no secret constants, no .env warning helper."""
        spec = importlib.util.spec_from_file_location("config_under_test", os.path.join(_BACKEND, "config.py"))
        config = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(config)
        for name in ("OPENROUTER_API_KEY", "FRED_API_KEY", "SEC_EDGAR_USER_AGENT", "log_ignored_env_secrets"):
            self.assertFalse(hasattr(config, name), name)
        with open(os.path.join(_BACKEND, "config.py"), encoding="utf-8") as fh:
            self.assertNotIn("dotenv", fh.read())


class RedactTest(unittest.TestCase):
    def test_redact(self):
        url = "400, message='Bad Request', url=URL('https://api.stlouisfed.org/fred/x?api_key=abcd1234&file_type=json')"
        out = keys.redact(url, "abcd1234")
        self.assertNotIn("abcd1234", out)
        self.assertIn("api_key=***", out)
        self.assertEqual(keys.redact("api_key=zzzz9999"), "api_key=***")
        self.assertEqual(keys.redact("plain", None, ""), "plain")


class WaitForSecretTest(StoreCase):
    def test_waits_reports_and_returns_once_set(self):
        statuses, sleeps = [], []

        async def report_status(ok, msg):
            statuses.append((ok, msg))

        async def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                self.store.values[FRED] = "now-set"

        value = asyncio.run(keys.wait_for_secret("fred-api-key", report_status, recheck_seconds=5,
                                                 suffix=" (x)", sleep=fake_sleep))
        self.assertEqual(value, "now-set")
        self.assertEqual(sleeps, [5, 5])
        self.assertEqual(len(statuses), 2)
        self.assertEqual(statuses[0], (False, "FRED API key is not set — enter it in Hedge Fund → Settings → "
                                              "Keys. (x)"))


class CollectorTest(StoreCase):
    def test_fred_missing_key_reports_settings_message(self):
        os.environ["FRED_API_KEY"] = "ignored-env-value"
        statuses, _ = _run_until_status(fred.run)
        self.assertEqual(statuses[0][0], False)
        self.assertIn("FRED API key is not set — enter it in Hedge Fund → Settings → Keys", statuses[0][1])

    def test_fred_with_key_polls_and_reads_key_at_use_time(self):
        self.store.values[FRED] = "fred-key-1"
        polled = []

        async def fake_poll(session, emit, report_status):
            polled.append(fred._fred_key())
            raise _Stop()
        saved = fred._poll_once
        fred._poll_once = fake_poll
        try:
            with self.assertRaises(_Stop):
                asyncio.run(fred.run(lambda e: None, None))
        finally:
            fred._poll_once = saved
        self.assertEqual(polled, ["fred-key-1"])

    def test_fred_http_error_is_redacted(self):
        self.store.values[FRED] = "fred-key-SECRET"

        class Session:
            def get(self, url, params=None, timeout=None):
                raise RuntimeError(f"500, url=URL('{url}?api_key={params['api_key']}&file_type=json')")

        with self.assertRaises(RuntimeError) as cm:
            asyncio.run(fred._fetch_observations(Session(), "DGS2", None, "2026-01-01"))
        self.assertNotIn("fred-key-SECRET", str(cm.exception))
        with self.assertRaises(RuntimeError) as cm:
            asyncio.run(fred._get_json(Session(), "series/release", {"series_id": "DGS2"}))
        self.assertNotIn("fred-key-SECRET", str(cm.exception))
        with self.assertRaises(RuntimeError) as cm:
            asyncio.run(cal._fetch_json(Session(), "releases/dates", {}))
        self.assertNotIn("fred-key-SECRET", str(cm.exception))

    def test_fred_fetch_without_key_raises_secret_missing(self):
        with self.assertRaises(SecretMissing):
            asyncio.run(fred._fetch_observations(None, "DGS2", None, "2026-01-01"))

    def test_calendar_missing_key_shows_fomc_only_with_reason(self):
        statuses, emitted = _run_until_status(cal.run)
        self.assertFalse(statuses[0][0])
        self.assertTrue(statuses[0][1].endswith("Showing FOMC dates only."))
        self.assertEqual(len(emitted), 1)
        self.assertTrue(all(e["kind"] == "fomc" for e in emitted[0]["events"]))
        self.assertIn("FRED API key is not set", emitted[0]["errors"][0])

    def test_sec_missing_user_agent(self):
        os.environ["SEC_EDGAR_USER_AGENT"] = "ignored you@example.com"
        statuses, _ = _run_until_status(sec.run)
        self.assertFalse(statuses[0][0])
        self.assertIn("SEC EDGAR user agent (name + e-mail) is not set", statuses[0][1])
        # _poll_once itself refuses without the key (key removed between checks)
        out = []

        async def report_status(ok, msg):
            out.append((ok, msg))
        self.assertFalse(asyncio.run(sec._poll_once(None, None, report_status)))
        self.assertIn("Settings → Keys", out[0][1])

    def test_sec_user_agent_sent_as_header(self):
        self.store.values[SEC] = "UFOS test you@example.com"
        seen = []

        async def fake_entries(session, headers):
            seen.append(headers)
            return []
        saved = sec._fetch_feed_entries
        sec._fetch_feed_entries = fake_entries
        try:
            async def report_status(ok, msg):
                pass
            self.assertTrue(asyncio.run(sec._poll_once(None, None, report_status)))
        finally:
            sec._fetch_feed_entries = saved
        self.assertEqual(seen, [{"User-Agent": "UFOS test you@example.com"}])


class NewsroomAiTest(StoreCase):
    def test_key_error_and_settings(self):
        os.environ["OPENROUTER_API_KEY"] = "sk-env-ignored"
        err = ai.key_error()
        self.assertIsInstance(err, SecretMissing)
        self.assertEqual(err.user_message, "OpenRouter API key is not set — enter it in Hedge Fund → Settings → Keys.")
        self.assertFalse(ai.is_configured())
        with self.assertRaises(SecretMissing):
            ai._current_settings()
        with self.assertRaises(SecretMissing):
            asyncio.run(ai.summarize_insider_trades("table"))
        with self.assertRaises(SecretMissing):
            asyncio.run(ai.analyse_selection("ctx", "standard"))
        self.store.values[OPENROUTER] = "sk-store"
        self.assertIsNone(ai.key_error())
        self.assertEqual(ai._current_settings()[0], "sk-store")
        # model ids need no key
        self.store.values.clear()
        self.assertEqual(set(ai.analysis_models()), {"standard", "premium"})


class FundLlmTest(StoreCase):
    def test_missing_key_is_no_key_with_settings_message(self):
        from strategy_platform.core import llm as llm_mod
        saved = llm_mod._newsroom_ai
        llm_mod._newsroom_ai = lambda: ai
        try:
            with self.assertRaises(llm_mod.LlmUnavailable) as cm:
                asyncio.run(llm_mod._openrouter([{"role": "user", "content": "x"}], "m", 10, 0.2, "fund-judge"))
        finally:
            llm_mod._newsroom_ai = saved
        self.assertEqual(cm.exception.reason, "no_key")
        self.assertIn("Hedge Fund → Settings → Keys", cm.exception.message)
        self.assertFalse(cm.exception.attempted)


if __name__ == "__main__":
    unittest.main()
