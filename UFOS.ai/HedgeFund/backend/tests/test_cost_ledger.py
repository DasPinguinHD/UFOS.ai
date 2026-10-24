"""newsroom/cost_ledger.py: shared AI cost ledger writer (offline, temp file via UFOS_AI_COST_LOG)."""
import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime

_NEWSROOM = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "newsroom")

_spec = importlib.util.spec_from_file_location("cost_ledger_under_test", os.path.join(_NEWSROOM, "cost_ledger.py"))
cost_ledger = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cost_ledger)

KEYS = {"ts", "module", "feature", "feature_label", "provider", "model", "cost_usd",
        "prompt_tokens", "completion_tokens", "status"}


class CostLedgerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = os.environ.get("UFOS_AI_COST_LOG")
        self.path = os.path.join(self._tmp.name, "sub", "ai-cost-log.jsonl")
        os.environ["UFOS_AI_COST_LOG"] = self.path

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("UFOS_AI_COST_LOG", None)
        else:
            os.environ["UFOS_AI_COST_LOG"] = self._saved
        self._tmp.cleanup()

    def _lines(self):
        with open(self.path, encoding="utf-8") as f:
            return f.read().split("\n")

    def test_record_writes_one_valid_json_line(self):
        cost_ledger.record("analysis", "Analysis & Reasoning", "openai/gpt-x",
                           {"cost_usd": 0.0123, "prompt_tokens": 1512, "completion_tokens": 874})
        lines = self._lines()
        self.assertEqual(lines[-1], "")  # "\n"-terminated
        self.assertEqual(len(lines), 2)
        entry = json.loads(lines[0])
        self.assertEqual(set(entry), KEYS)
        self.assertEqual(entry["module"], "HedgeFund")
        self.assertEqual(entry["provider"], "OpenRouter")
        self.assertEqual(entry["feature"], "analysis")
        self.assertEqual(entry["feature_label"], "Analysis & Reasoning")
        self.assertEqual(entry["model"], "openai/gpt-x")
        self.assertEqual(entry["status"], "ok")
        self.assertAlmostEqual(entry["cost_usd"], 0.0123)
        self.assertEqual(entry["prompt_tokens"], 1512)
        self.assertEqual(entry["completion_tokens"], 874)
        ts = datetime.fromisoformat(entry["ts"])
        self.assertIsNotNone(ts.tzinfo)
        self.assertEqual(ts.utcoffset().total_seconds(), 0)

    def test_appends_and_keeps_nulls_and_unicode(self):
        cost_ledger.record("insider-summary", "Insider Trades · Sum up most notable", "m1",
                           {"cost_usd": None, "prompt_tokens": None, "completion_tokens": None})
        cost_ledger.record("analysis", "Analysis & Reasoning", "m2", {})
        lines = [l for l in self._lines() if l]
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        self.assertEqual(first["feature_label"], "Insider Trades · Sum up most notable")
        self.assertIsNone(first["cost_usd"])
        self.assertIsNone(first["prompt_tokens"])
        self.assertIsNone(json.loads(lines[1])["completion_tokens"])

    def test_never_raises_on_unwritable_path(self):
        # A directory in place of the file -> open() fails with an OSError.
        os.makedirs(self.path)
        with self.assertLogs(cost_ledger.logger, level="WARNING"):
            cost_ledger.record("analysis", "Analysis & Reasoning", "m", {"cost_usd": 0.1})

    def test_default_path_uses_localappdata(self):
        os.environ.pop("UFOS_AI_COST_LOG", None)
        saved = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = self._tmp.name
        try:
            self.assertEqual(cost_ledger.ledger_path(),
                             os.path.join(self._tmp.name, "UFOS.ai", "ai-cost-log.jsonl"))
        finally:
            if saved is None:
                os.environ.pop("LOCALAPPDATA", None)
            else:
                os.environ["LOCALAPPDATA"] = saved


if __name__ == "__main__":
    unittest.main()
