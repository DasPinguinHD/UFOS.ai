"""Stage 10 security audit, fix batch S10-A1 (HedgeFund backend): always 127.0.0.1 (M6), trimmed public /health and
/version (L2 — no pid, no paths), no WebSocket placeholder (L9), the PDF worker's scrubbed environment (L5),
.gitignore allow-list for data/ (M2) and the .dockerignore (M1). The Host check itself: test_api_auth. Offline."""
import importlib.util
import os
import subprocess
import sys
import types
import unittest
from unittest import mock

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


def _read(name):
    with open(os.path.join(BACKEND, name), encoding="utf-8") as fh:
        return fh.read()


class _AnyModule(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        cls = type(name, (), {"__init__": lambda self, *a, **k: None})
        setattr(self, name, cls)
        return cls


def _drop_module(name):
    """Pop ``name`` from sys.modules and drop the parent package attribute that still points at it."""
    mod = sys.modules.pop(name, None)
    parent, _, child = name.rpartition(".")
    if mod is not None and parent and getattr(sys.modules.get(parent), child, None) is mod:
        delattr(sys.modules[parent], child)


class MainPublicRoutesTest(unittest.TestCase):
    def load_main(self):
        before = set(sys.modules)
        saved, stubs = {}, {}
        for name in ("aiohttp", "aiohttp.web", "websockets", "dotenv", "openai", "logging_setup"):
            saved[name] = sys.modules.get(name)
            stubs[name] = _AnyModule(name)
        stubs["aiohttp"].web = stubs["aiohttp.web"]
        stubs["dotenv"].load_dotenv = lambda *a, **k: False
        stubs["logging_setup"].configure_logging = lambda *a, **k: None
        sys.modules.update(stubs)

        def restore():
            for name in set(sys.modules) - before:
                if name.startswith(("newsroom.ai", "s10_main", "config", "api_auth", "aiohttp", "websockets",
                                    "dotenv", "openai", "logging_setup", "newsroom.fred", "newsroom.sec",
                                    "newsroom.calendar", "newsroom.overview", "newsroom.insider_summary")):
                    _drop_module(name)
            for name, m in saved.items():
                if m is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = m
        self.addCleanup(restore)
        spec = importlib.util.spec_from_file_location("s10_main", os.path.join(BACKEND, "main.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_health_and_version_carry_no_pid_and_no_paths(self):
        m = self.load_main()
        self.assertEqual(m.health_payload(), {"status": "ok", "mode": "normal"})
        self.assertEqual(set(m.version_payload(1.5, "2026-10-24T00:00:00Z")), {"code_mtime", "started_at", "mode"})
        sandbox = {"mode": "sandbox", "sandbox": {"data_dir": "data/sandbox", "banner": "x"}}
        with mock.patch.object(m.sandbox_profile, "mode_fields", return_value=sandbox):
            self.assertEqual(m.health_payload(), {"status": "ok", "mode": "sandbox"})
            v = m.version_payload(1.5, "t")
            self.assertEqual(v["mode"], "sandbox")
            self.assertNotIn("data_dir", repr(v))
            self.assertNotIn("pid", v)

    def test_loopback_only_and_no_websocket_placeholder(self):
        src = _read("main.py")
        self.assertIn('api_auth.bind_host("HTTP API")', src)                # always 127.0.0.1 (Docker removed)
        self.assertNotIn("import websockets", src)
        self.assertNotIn("websockets.serve", src)
        self.assertNotIn("os.getpid()", src)
        cfg = _read("config.py")
        self.assertNotIn("WS_PORT =", cfg)
        self.assertNotIn("WS_HOST =", cfg)
        self.assertNotIn("HTTP_HOST =", cfg)
        self.assertFalse(os.path.exists(os.path.join(BACKEND, "Dockerfile")))          # Docker removed 2026-10-24


class PdfWorkerEnvTest(unittest.TestCase):
    def test_credentials_never_reach_the_worker(self):
        from strategy_platform.research import papers
        fake = {"OPENROUTER_API_KEY": "or-x", "ALPACA_API_KEY_ID": "ak", "FRED_API_KEY": "f",
                "SOME_SECRET": "s", "UFOS_TOKEN": "t", "PATH": os.environ.get("PATH", "/usr/bin"), "SYSTEMROOT": "C:\\W"}
        with mock.patch.dict(os.environ, fake):
            env = papers.worker_env()
            for k in ("OPENROUTER_API_KEY", "ALPACA_API_KEY_ID", "FRED_API_KEY", "SOME_SECRET", "UFOS_TOKEN"):
                self.assertNotIn(k, env)
            self.assertEqual((env["PATH"], env["SYSTEMROOT"]), (fake["PATH"], "C:\\W"))
            seen = {}

            def run(argv, **kw):
                seen.update(kw)
                return subprocess.CompletedProcess(argv, 0, b'{"text": "Plenty of extractable text here."}', b"")
            with mock.patch.object(papers.subprocess, "run", run):
                papers.extract_pdf(b"%PDF-1.4 fake")
        self.assertIn("env", seen)
        self.assertNotIn("OPENROUTER_API_KEY", seen["env"])


class RepoHygieneTest(unittest.TestCase):
    def test_gitignore_allow_lists_data(self):
        lines = [x.strip() for x in _read(".gitignore").splitlines()]
        for rule in ("data/*", "!data/.keep", "!data/scenarios/", "data/scenarios/*", "!data/scenarios/.keep",
                     "data/sandbox0-backup/", "*.stale", ".env"):
            self.assertIn(rule, lines)
        self.assertLess(lines.index("data/*"), lines.index("!data/.keep"))
        self.assertLess(lines.index("data/scenarios/*"), lines.index("!data/scenarios/.keep"))

    def test_no_docker_files(self):
        for name in ("Dockerfile", ".dockerignore"):
            self.assertFalse(os.path.exists(os.path.join(BACKEND, name)), name)        # Docker removed 2026-10-24


if __name__ == "__main__":
    unittest.main()
