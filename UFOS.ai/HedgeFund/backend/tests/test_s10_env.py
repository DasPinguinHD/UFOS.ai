"""Stage 10 S10-E (.env removed, user decision 2026-10-24) — HedgeFund backend guard + unit tests: no dotenv / .env,
no secret (or former .env setting) read from the environment, settings strictly from data/settings.json
(settings_file.py, byte-identical in the three backends), GET /v2/settings/keys without .env fields, python-dotenv
gone, .env.example retired. Offline."""
import importlib.util
import json
import os
import re
import tempfile
import unittest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULES = os.path.dirname(os.path.dirname(BACKEND))
SECRET_LIKE = re.compile(r"KEY|SECRET|TOKEN|PASSW|CREDENTIAL|APCA", re.IGNORECASE)
OLD_ENV_NAMES = {"OPENROUTER_BASE_URL", "OPENROUTER_MODEL", "OPENROUTER_ANALYSIS_PREMIUM_MODEL", "HEALTH_PORT",
                 "MARKET_DATA_POLL_SECONDS", "SEC_EDGAR_POLL_SECONDS", "SEC_EDGAR_MIN_REQUEST_INTERVAL_SECONDS",
                 "FRED_POLL_SECONDS", "CALENDAR_POLL_SECONDS", "INSIDER_DISPLAY_DAYS", "INSIDER_DISPLAY_MAX",
                 "INSIDER_RETENTION_DAYS", "AI_RESULTS_RETENTION_DAYS", "SEC_EDGAR_USER_AGENT"}


def _load(name):
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
            self.assertNotRegex(src, r"^\s*(from dotenv|import dotenv)|load_dotenv\(", rel)

    def test_no_secret_or_old_setting_from_the_environment(self):
        pat = re.compile(r"""os\.(?:getenv|environ\.get)\(\s*['"]([A-Za-z0-9_]+)['"]|os\.environ\[\s*['"]([A-Za-z0-9_]+)['"]""")
        bad = []
        for rel, src in _sources():
            for m in pat.finditer(src):
                name = m.group(1) or m.group(2)
                if SECRET_LIKE.search(name) or name in OLD_ENV_NAMES:
                    bad.append(f"{rel}: {name}")
        self.assertEqual(bad, [])

    def test_requirements_and_example_file(self):
        for name in ("requirements.txt", "requirements-broker.txt"):
            with open(os.path.join(BACKEND, name), encoding="utf-8") as fh:
                self.assertNotIn("dotenv", fh.read())
        self.assertFalse(os.path.exists(os.path.join(BACKEND, ".env.example")))

    def test_settings_file_copies_byte_identical(self):
        with open(os.path.join(BACKEND, "settings_file.py"), "rb") as fh:
            ours = fh.read()
        for module in ("Global Monitoring", "LiveStreamAgent"):
            other = os.path.join(MODULES, module, "backend", "settings_file.py")
            if os.path.exists(other):
                with open(other, "rb") as fh:
                    self.assertEqual(fh.read(), ours, module)


class ConfigSettingsTest(unittest.TestCase):
    def test_schema_defaults_valid_and_strict(self):
        sf = _load("settings_file")
        cfg = _load("config")
        self.assertFalse(hasattr(cfg, "log_ignored_env_secrets"))
        self.assertEqual(cfg.HEALTH_PORT, 8867)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({k: v[1] for k, v in cfg.SCHEMA.items()}, fh)
            self.assertEqual(sf.load(path, cfg.SCHEMA), sf.defaults(cfg.SCHEMA))
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"openrouter_base_url": "http://openrouter.ai/api/v1"}, fh)
            with self.assertRaises(sf.SettingsError):
                sf.load(path, cfg.SCHEMA)
        self.assertEqual(set(cfg.ai_settings()), {"base_url", "model", "premium_model"})

    def test_keys_status_has_no_env_fields(self):
        from strategy_platform import secrets
        secrets.set_store(secrets.MemoryStore({}))
        self.addCleanup(secrets.set_store, None)
        for e in secrets.status():
            self.assertEqual(set(e), {"name", "label", "target", "present", "error"})
        self.assertFalse(hasattr(secrets, "env_shadow_present"))
        self.assertEqual(secrets.SECRETS["openrouter-api-key"], "OpenRouter API key")


class ImportSpecsMatchSchemasTest(unittest.TestCase):
    """W's one-time .env import copies a setting only when it differs from the code default — its table
    (Platform/BackendSettingSpecs.cs) must equal the backends' config.SCHEMA exactly."""
    KIND = {"I": ("int",), "N": ("float",), "T": ("str", "openrouter_url", "youtube_url")}
    MODULE_DIR = {"HedgeFund": "HedgeFund", "GlobalMonitoring": "Global Monitoring", "LiveStreamAgent": "LiveStreamAgent"}

    def test_specs_equal_schemas(self):
        cs_path = os.path.join(os.path.dirname(BACKEND), "Platform", "BackendSettingSpecs.cs")
        if not os.path.exists(cs_path):
            self.skipTest("BackendSettingSpecs.cs not in this checkout")
        with open(cs_path, encoding="utf-8") as fh:
            cs = fh.read()
        consts = dict(re.findall(r'private const string (\w+) = "([^"]*)";', cs))
        specs = {}
        for kind, module, env, default in re.findall(r'\b([ITN])\(EnvModule\.(\w+), "(\w+)", ("[^"]*"|\w+)\)', cs):
            value = default[1:-1] if default.startswith('"') else consts[default]
            specs.setdefault(module, {})[env.lower()] = (kind, value)
        self.assertEqual(set(specs), set(self.MODULE_DIR))
        import sys
        for module, folder in self.MODULE_DIR.items():
            cfg_path = os.path.join(MODULES, folder, "backend", "config.py")
            if not os.path.exists(cfg_path):
                continue
            sys.path.insert(0, os.path.dirname(cfg_path))
            try:
                spec = importlib.util.spec_from_file_location(f"s10env_cfg_{module}", cfg_path)
                cfg = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(cfg)
            finally:
                sys.path.pop(0)
            self.assertEqual(set(specs[module]), set(cfg.SCHEMA), module)
            for key, (kind, value) in specs[module].items():
                skind, sdefault = cfg.SCHEMA[key][0], cfg.SCHEMA[key][1]
                self.assertIn(skind, self.KIND[kind], f"{module}.{key}")
                if kind == "I":
                    self.assertEqual(int(value), sdefault, f"{module}.{key}")
                elif kind == "N":
                    self.assertEqual(float(value), sdefault, f"{module}.{key}")
                else:
                    self.assertEqual(value, sdefault, f"{module}.{key}")


if __name__ == "__main__":
    unittest.main()
