"""Stage 10 S10-E (.env removed, user decision 2026-10-24) — guard + unit tests, the same file in the Global
Monitoring and LiveStreamAgent backends: no dotenv / .env anywhere, no secret read from the environment, keys only
from the Credential Manager (credstore.py, byte-identical copies), non-secret settings strictly from data/settings.json
(settings_file.py, byte-identical in all three backends), python-dotenv gone, .env.example retired. Offline."""
import importlib.util
import json
import os
import re
import sys
import tempfile
import unittest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULES = os.path.dirname(os.path.dirname(BACKEND))
IS_GM = os.path.exists(os.path.join(BACKEND, "assess.py"))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
OLD_ENV_NAMES = {"OPENROUTER_API_KEY", "DEEPGRAM_API_KEY", "OPENSKY_CLIENT_ID", "OPENSKY_CLIENT_SECRET",
                 "OPENROUTER_BASE_URL", "OPENROUTER_MODEL", "YOUTUBE_URL"}
SECRET_LIKE = re.compile(r"KEY|SECRET|TOKEN|PASSW|CREDENTIAL", re.IGNORECASE)


def _load(name):
    """The real module from this backend, under a private name (other tests may have stubbed the plain name)."""
    spec = importlib.util.spec_from_file_location(f"s10env_{name}", os.path.join(BACKEND, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sources():
    for root, dirs, files in os.walk(BACKEND):
        dirs[:] = [d for d in dirs if d not in (".venv", "data", "__pycache__", "tests")]
        for f in files:
            if f.endswith(".py"):
                path = os.path.join(root, f)
                with open(path, encoding="utf-8") as fh:
                    yield os.path.relpath(path, BACKEND), fh.read()


class NoEnvGuardTest(unittest.TestCase):
    def test_no_dotenv_and_no_env_file(self):
        for rel, src in _sources():
            self.assertNotRegex(src, r"\bdotenv\b|load_dotenv", rel)
            self.assertNotRegex(src, r"""open\([^)]*['"][^'"]*\.env['"]""", rel)

    def test_no_secret_or_old_setting_from_the_environment(self):
        pat = re.compile(r"""os\.(?:getenv|environ\.get)\(\s*['"]([A-Za-z0-9_]+)['"]|os\.environ\[\s*['"]([A-Za-z0-9_]+)['"]""")
        for rel, src in _sources():
            for m in pat.finditer(src):
                name = m.group(1) or m.group(2)
                self.assertFalse(SECRET_LIKE.search(name), f"{rel}: {name}")
                self.assertNotIn(name, OLD_ENV_NAMES, rel)

    def test_requirements_and_example_files(self):
        with open(os.path.join(BACKEND, "requirements.txt"), encoding="utf-8") as fh:
            self.assertNotIn("python-dotenv", fh.read())
        self.assertFalse(os.path.exists(os.path.join(BACKEND, ".env.example")))
        with open(os.path.join(BACKEND, "README.md"), encoding="utf-8") as fh:
            self.assertIn("Credential Manager", fh.read())

    def test_copies_byte_identical(self):
        for name, modules in (("credstore.py", ("Global Monitoring", "LiveStreamAgent")),
                              ("settings_file.py", ("Global Monitoring", "LiveStreamAgent", "HedgeFund"))):
            for module in modules:
                other = os.path.join(MODULES, module, "backend", name)
                if not os.path.exists(other):
                    continue
                with open(os.path.join(BACKEND, name), "rb") as a, open(other, "rb") as b:
                    self.assertEqual(a.read(), b.read(), f"{name} vs {module}")


class CredstoreTest(unittest.TestCase):
    def setUp(self):
        self.cs = _load("credstore")

    def test_targets_shared_openrouter_and_memory_store(self):
        self.assertEqual(self.cs.TARGETS["openrouter-api-key"][0], "UFOS.ai/HedgeFund/openrouter-api-key")
        self.assertEqual(set(self.cs.TARGETS), {"openrouter-api-key", "deepgram-api-key", "opensky-client-id",
                                                "opensky-client-secret"})
        target = self.cs.TARGETS["deepgram-api-key"][0]
        self.cs.set_store(self.cs.MemoryStore({target: "  dg-secret-123  "}))
        self.assertEqual(self.cs.require_secret("deepgram-api-key"), "dg-secret-123")
        self.assertIsNone(self.cs.problem("deepgram-api-key"))
        code, msg = self.cs.problem("openrouter-api-key")
        self.assertEqual(code, "key_missing")
        self.assertIn("⚙ Keys & data", msg)
        st = self.cs.status()
        self.assertNotIn("dg-secret-123", repr(st))
        self.assertEqual({e["name"]: e["present"] for e in st}["deepgram-api-key"], True)
        with self.assertRaises(KeyError):
            self.cs.get_secret("OPENROUTER_API_KEY")
        os.environ["OPENROUTER_API_KEY"] = "sk-from-env"            # never read
        try:
            self.assertIsNone(self.cs.get_secret("openrouter-api-key"))
        finally:
            os.environ.pop("OPENROUTER_API_KEY", None)

    def test_store_failure_and_bad_blob(self):
        class Broken:
            def read(self, target, name=""):
                raise self_cs.SecretStoreUnavailable("The Windows Credential Manager could not be read.")
        self_cs = self.cs
        self.cs.set_store(Broken())
        self.assertEqual(self.cs.problem("openrouter-api-key")[0], "key_store_unavailable")
        self.assertTrue(all(e["present"] is None and e["error"] for e in self.cs.status()))
        with self.assertRaises(self.cs.SecretStoreUnavailable):
            self.cs.decode_blob(b"ab\x00c", "deepgram-api-key")
        self.assertIsNone(self.cs.decode_blob(b"   "))


class SettingsFileTest(unittest.TestCase):
    def setUp(self):
        self.sf = _load("settings_file")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "settings.json")
        self.schema = {"a_seconds": ("int", 10, 1, 100), "b_hours": ("float", 1.5, 0.0, 10.0),
                       "model": ("str", "x/y", 50), "openrouter_base_url": ("openrouter_url", "https://openrouter.ai/api/v1"),
                       "youtube_url": ("youtube_url", "")}

    def write(self, obj):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(obj if isinstance(obj, str) else json.dumps(obj))

    def test_defaults_and_values(self):
        self.assertEqual(self.sf.load(self.path, self.schema)["a_seconds"], 10)        # no file -> defaults
        self.write({"a_seconds": 20, "b_hours": 2, "youtube_url": "https://www.youtube.com/watch?v=abc"})
        s = self.sf.load(self.path, self.schema)
        self.assertEqual((s["a_seconds"], s["b_hours"], s["model"]), (20, 2.0, "x/y"))

    def test_strict(self):
        for bad, why in (({"nope": 1}, "unknown setting"), ({"openrouter_api_key": "sk"}, "secrets never go"),
                         ({"a_seconds": True}, "a number"), ({"a_seconds": 1.5}, "whole number"),
                         ({"a_seconds": 0}, "1..100"), ({"model": ""}, "non-empty"),
                         ({"openrouter_base_url": "http://openrouter.ai/api/v1"}, "https on openrouter.ai"),
                         ({"openrouter_base_url": "https://evil.example/v1"}, "https on openrouter.ai"),
                         ({"youtube_url": "https://evil.example/watch"}, "youtube.com"),
                         ("{not json", "not readable JSON"), ("[1, 2]", "JSON object")):
            self.write(bad)
            with self.assertRaises(self.sf.SettingsError, msg=str(bad)) as cm:
                self.sf.load(self.path, self.schema)
            self.assertIn(why, str(cm.exception))

    def test_config_schema_defaults_are_valid(self):
        cfg = _load("config")
        self.write({k: v[1] for k, v in cfg.SCHEMA.items() if not (v[0] == "youtube_url" and v[1] == "")})
        self.assertEqual(self.sf.load(self.path, cfg.SCHEMA), self.sf.defaults(cfg.SCHEMA))
        self.assertTrue(cfg.SETTINGS_PATH.endswith(os.path.join("data", "settings.json")))


class ModuleWiringTest(unittest.TestCase):
    def test_keys_are_read_through_credstore(self):
        def src(name):
            with open(os.path.join(BACKEND, name), encoding="utf-8") as fh:
                return fh.read()
        if IS_GM:
            self.assertIn("credstore.require_secret(KEY_NAME)", src("assess.py"))
            self.assertIn('credstore.get_secret("opensky-client-id")', src(os.path.join("collectors",
                                                                                       "opensky_collector.py")))
            self.assertIn('"code": missing[0], "key": assess.KEY_NAME', src("main.py"))
            self.assertNotIn("backend/.env", src("main.py"))
        else:
            self.assertIn("credstore.require_secret(KEY_NAME)", src("llm_analyst.py"))
            self.assertIn("credstore.require_secret(KEY_NAME)", src("transcription.py"))
            self.assertIn("raise SystemExit(YOUTUBE_URL_MISSING)", src("main.py"))


if __name__ == "__main__":
    unittest.main()
