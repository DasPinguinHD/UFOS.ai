"""Tests for the shared AI cost ledger writer (stdlib unittest).

Run from LiveStreamAgent/backend:  python -m unittest tests.test_cost_ledger -v
"""
import json
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cost_ledger  # noqa: E402


class _Usage:
    def __init__(self, prompt_tokens=None, completion_tokens=None, cost=None, extra=None):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        if cost is not None:
            self.cost = cost
        self.model_extra = extra


class CostLedgerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "sub", "ai-cost-log.jsonl")
        self._old = os.environ.get("UFOS_AI_COST_LOG")
        os.environ["UFOS_AI_COST_LOG"] = self.path

    def tearDown(self):
        if self._old is None:
            os.environ.pop("UFOS_AI_COST_LOG", None)
        else:
            os.environ["UFOS_AI_COST_LOG"] = self._old
        self._tmp.cleanup()

    def _lines(self):
        with open(self.path, encoding="utf-8") as f:
            return [json.loads(line) for line in f.read().splitlines()]

    def test_path_override(self):
        self.assertEqual(cost_ledger.ledger_path(), self.path)

    def test_default_path_uses_localappdata(self):
        os.environ.pop("UFOS_AI_COST_LOG", None)
        old = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = self._tmp.name
        try:
            self.assertEqual(cost_ledger.ledger_path(),
                             os.path.join(self._tmp.name, "UFOS.ai", "ai-cost-log.jsonl"))
        finally:
            if old is None:
                os.environ.pop("LOCALAPPDATA", None)
            else:
                os.environ["LOCALAPPDATA"] = old

    def test_record_writes_one_line_with_spec_shape(self):
        cost_ledger.record("verdict", "deepseek/deepseek-chat-v3-0324",
                           {"cost_usd": 0.0021, "prompt_tokens": 812, "completion_tokens": 190})
        cost_ledger.record("verdict", "m2", {"cost_usd": None, "prompt_tokens": None, "completion_tokens": None})
        with open(self.path, "rb") as f:
            raw = f.read()
        self.assertTrue(raw.endswith(b"\n"))
        self.assertNotIn(b"\r\n", raw)
        lines = self._lines()
        self.assertEqual(len(lines), 2)
        e = lines[0]
        self.assertEqual(set(e), {"ts", "module", "feature", "feature_label", "provider", "model",
                                  "cost_usd", "prompt_tokens", "completion_tokens", "status"})
        self.assertEqual(e["module"], "LiveStreamAgent")
        self.assertEqual(e["feature"], "verdict")
        self.assertEqual(e["feature_label"], "LiveStream Agent · AI verdict")
        self.assertEqual(e["provider"], "OpenRouter")
        self.assertEqual(e["model"], "deepseek/deepseek-chat-v3-0324")
        self.assertAlmostEqual(e["cost_usd"], 0.0021)
        self.assertEqual((e["prompt_tokens"], e["completion_tokens"]), (812, 190))
        self.assertEqual(e["status"], "ok")
        self.assertTrue(e["ts"].endswith("+00:00"))
        self.assertIsNone(lines[1]["cost_usd"])
        self.assertIsNone(lines[1]["prompt_tokens"])

    def test_record_never_raises(self):
        # Point the ledger at a directory -> open() fails; must only log a warning.
        os.environ["UFOS_AI_COST_LOG"] = self._tmp.name
        with self.assertLogs("cost_ledger", level="WARNING"):
            cost_ledger.record("verdict", "m", {"cost_usd": 0.1})

    def test_usage_info_cost_attribute(self):
        resp = types.SimpleNamespace(usage=_Usage(812, 190, cost=0.0021))
        self.assertEqual(cost_ledger.usage_info(resp),
                         {"cost_usd": 0.0021, "prompt_tokens": 812, "completion_tokens": 190})

    def test_usage_info_cost_in_model_extra(self):
        resp = types.SimpleNamespace(usage=_Usage(10, 5, extra={"cost": "0.5"}))
        self.assertEqual(cost_ledger.usage_info(resp)["cost_usd"], 0.5)

    def test_usage_info_missing_usage(self):
        self.assertEqual(cost_ledger.usage_info(types.SimpleNamespace()),
                         {"cost_usd": None, "prompt_tokens": None, "completion_tokens": None})


if __name__ == "__main__":
    unittest.main()
