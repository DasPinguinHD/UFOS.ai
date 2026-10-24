"""Stage 10 security audit, fix batch S10-A1 (LiveStreamAgent backend): OPENROUTER_BASE_URL only https on
openrouter.ai (L1), event logs pruned after 30 days (L6), no PyPI "asyncio" requirement (M3). Offline."""
import importlib.util
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


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
            for name in ("2026-09-17.jsonl", "2026-09-25.jsonl", "2026-10-14.jsonl", "ui-errors.log", "env-setup.log"):
                Path(d, name).write_text("x", encoding="utf-8")
            self.assertEqual(logger.prune_old_logs(30, today=date(2026, 10, 24), log_dir=d), ["2026-09-17.jsonl"])
            self.assertEqual(sorted(os.listdir(d)), ["2026-09-25.jsonl", "2026-10-14.jsonl", "env-setup.log",
                                                     "ui-errors.log"])
        src = (BACKEND / "main.py").read_text(encoding="utf-8")
        self.assertIn("prune_old_logs()", src)


class RequirementsTest(unittest.TestCase):
    def test_no_pypi_asyncio(self):
        lines = [x.split("#")[0].strip().lower() for x in (BACKEND / "requirements.txt").read_text(encoding="utf-8")
                 .splitlines()]
        self.assertFalse([x for x in lines if x.startswith("asyncio")])


if __name__ == "__main__":
    unittest.main()
