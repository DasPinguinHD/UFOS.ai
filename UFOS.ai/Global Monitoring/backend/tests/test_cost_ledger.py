"""Tests for the shared AI cost ledger (cost_ledger.record) and
assess.usage_info (2026-09-25). stdlib unittest only; openai/dotenv are
stubbed so the tests run without the backend's venv.

Run from Global Monitoring/backend:
    python -m unittest tests.test_cost_ledger
"""
import json
import os
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace
from unittest import mock

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


def _stub_module(name, **attrs):
    if name in sys.modules:
        return
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod


# Stub third-party deps (and config, which may itself load .env) before importing assess.
_stub_module("dotenv", load_dotenv=lambda *a, **k: None)
_stub_module("openai", AsyncOpenAI=object)
_stub_module("config", OPENROUTER_API_KEY="", OPENROUTER_BASE_URL="https://openrouter.ai/api/v1",
             OPENROUTER_MODEL="test/model")

import assess  # noqa: E402
import cost_ledger  # noqa: E402


class _Usage:
    """Mimics openai's CompletionUsage: standard fields as attributes,
    OpenRouter's extra `cost` in model_extra."""
    def __init__(self, prompt_tokens, completion_tokens, model_extra=None, **attrs):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.model_extra = model_extra
        for k, v in attrs.items():
            setattr(self, k, v)


class UsageInfoTests(unittest.TestCase):
    def test_cost_from_model_extra(self):
        resp = SimpleNamespace(usage=_Usage(1512, 874, model_extra={"cost": 0.0123}))
        self.assertEqual(assess.usage_info(resp),
                         {"cost_usd": 0.0123, "prompt_tokens": 1512, "completion_tokens": 874})

    def test_cost_from_attribute(self):
        resp = SimpleNamespace(usage=_Usage(10, 5, cost="0.5"))
        self.assertEqual(assess.usage_info(resp)["cost_usd"], 0.5)

    def test_missing_usage(self):
        self.assertEqual(assess.usage_info(SimpleNamespace()),
                         {"cost_usd": None, "prompt_tokens": None, "completion_tokens": None})

    def test_missing_or_bad_cost(self):
        self.assertIsNone(assess.usage_info(SimpleNamespace(usage=_Usage(1, 2)))["cost_usd"])
        self.assertIsNone(assess.usage_info(SimpleNamespace(usage=_Usage(1, 2, model_extra={"cost": "n/a"})))["cost_usd"])


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "sub", "ai-cost-log.jsonl")
        self.env = mock.patch.dict(os.environ, {"UFOS_AI_COST_LOG": self.path})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _lines(self):
        with open(self.path, "rb") as f:
            data = f.read()
        self.assertTrue(data.endswith(b"\n"))
        return [json.loads(l) for l in data.decode("utf-8").splitlines()]

    def test_record_appends_spec_lines(self):
        cost_ledger.record("assess", "Globe · AI Assess / Sentiment", "x/model-a",
                           {"cost_usd": 0.0123, "prompt_tokens": 1512, "completion_tokens": 874})
        cost_ledger.record("localnews-summary", "Globe · AI Sum up local news", "x/model-b",
                           {"cost_usd": None, "prompt_tokens": None, "completion_tokens": None})
        first, second = self._lines()
        self.assertEqual(set(first), {"ts", "module", "feature", "feature_label", "provider", "model",
                                      "cost_usd", "prompt_tokens", "completion_tokens", "status"})
        self.assertEqual(first["module"], "Global Monitoring")
        self.assertEqual(first["provider"], "OpenRouter")
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["feature_label"], "Globe · AI Assess / Sentiment")
        self.assertEqual((first["cost_usd"], first["prompt_tokens"], first["completion_tokens"]), (0.0123, 1512, 874))
        self.assertTrue(first["ts"].endswith("+00:00"))
        self.assertEqual(second["feature"], "localnews-summary")
        self.assertIsNone(second["cost_usd"])

    def test_record_none_usage(self):
        cost_ledger.record("assess", "label", "m", None)
        (entry,) = self._lines()
        self.assertIsNone(entry["prompt_tokens"])

    def test_record_never_raises(self):
        # Parent "folder" is a file -> makedirs fails; must only log a warning.
        blocker = os.path.join(self.tmp.name, "blocker")
        open(blocker, "w").close()
        with mock.patch.dict(os.environ, {"UFOS_AI_COST_LOG": os.path.join(blocker, "x.jsonl")}):
            with self.assertLogs("cost_ledger", level="WARNING"):
                cost_ledger.record("assess", "label", "m", {"cost_usd": 1.0})

    def test_default_path_uses_localappdata(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": "/fake/local"}):
            os.environ.pop("UFOS_AI_COST_LOG", None)
            self.assertEqual(cost_ledger.ledger_path(),
                             os.path.join("/fake/local", "UFOS.ai", "ai-cost-log.jsonl"))


if __name__ == "__main__":
    unittest.main()
