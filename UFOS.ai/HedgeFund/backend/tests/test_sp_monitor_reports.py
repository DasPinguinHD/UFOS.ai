"""Stage 9 (SPEC §19.4): daily / weekly reports (numbers + fact table), the scheduler (once per period), the AI
commentary (fake LLM transport: numbers only in the prompt, validated output, number check, provenance, the
``reports`` budget key blocks BEFORE the call, failed calls charged). Offline."""
import asyncio
import json
import unittest
from datetime import date, datetime, timezone

try:
    from sp_monitor_testkit import FRI, MonCase
except ImportError:  # pragma: no cover
    from tests.sp_monitor_testkit import FRI, MonCase

import ai_budgets
from strategy_platform.core import llm
from strategy_platform.execution.errors import TradingError
from strategy_platform.monitoring import alerts, reports
from strategy_platform.storage import db


NOW = datetime(2026, 10, 7, 1, 0, tzinfo=timezone.utc)


class Fake:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    async def __call__(self, messages, model, max_tokens, temperature):
        self.calls.append(messages)
        return self.answer, "fake/model", {"cost_usd": 0.001, "prompt_tokens": 10, "completion_tokens": 5}


GOOD = json.dumps({"summary": "The paper account returned 1.00 % [acc.paper-main.return_pct] and had 17 alerts.",
                   "sections": [{"title": "Account", "text": "Equity ended at $101,000.00 [acc.paper-main.equity_end]."}],
                   "hypotheses": [], "limits": ["Only one day."]})


class ReportsTest(MonCase):
    def setUp(self):
        super().setUp()
        ai_budgets._reports = None
        self.addCleanup(setattr, ai_budgets, "_reports", None)
        self.addCleanup(setattr, llm, "TRANSPORT", None)
        with db._connect() as conn:
            for ts, eq in (("2026-10-05T21:00:00+00:00", 100000.0), ("2026-10-06T14:00:00+00:00", 99000.0),
                           ("2026-10-06T21:00:00+00:00", 101000.0)):
                conn.execute("INSERT OR REPLACE INTO pa_equity (account_id, ts, equity, cash, long_mv, short_mv) VALUES"
                             " ('paper-main', ?, ?, ?, 0, 0)", (ts, eq, eq))

    def test_daily_numbers_and_facts(self):
        rep = reports.create("daily", date(2026, 10, 6), "user", now=NOW)
        self.assertEqual((rep["period"], rep["period_id"]), ("daily", "2026-10-06"))
        a = rep["numbers"]["accounts"][0]
        self.assertEqual((a["equity_start"], a["equity_end"], a["pnl"]), (100000.0, 101000.0, 1000.0))
        self.assertAlmostEqual(a["return_pct"], 1.0)
        self.assertAlmostEqual(a["max_drawdown_pct"], 1.0)
        ids = {f["id"] for f in rep["facts"]}
        self.assertIn("acc.paper-main.return_pct", ids)
        self.assertEqual(reports.get(rep["id"])["id"], rep["id"])
        self.assertEqual(reports.list_reports()["reports"][0]["id"], rep["id"])
        with self.assertRaises(TradingError):
            reports.get("rep-000000000000")
        pid, s, e = reports.bounds("weekly", date(2026, 10, 2))
        self.assertEqual((pid, s.date().isoformat(), e.date().isoformat()), ("2026-W40", "2026-09-28", "2026-10-05"))

    def test_scheduler_once(self):
        now = datetime(2026, 10, 3, 0, 31, tzinfo=timezone.utc)
        made = reports.due(now)
        # paper-main was opened on 2026-10-02 15:00 (the test clock): no report for W39 (before it existed),
        # the daily report of 2026-10-02 covers it only partly (click-test 2026-10-22)
        self.assertEqual({r["period_id"] for r in made}, {"2026-10-02"})
        a = made[0]["numbers"]["accounts"][0]
        self.assertEqual((a["partial"], a["opened"], a["note"]),
                         (True, "2026-10-02T15:00:00+00:00", "opened 2026-10-02T15:00:00+00:00 — partial period"))
        self.assertIn("acc.paper-main.partial", {f["id"] for f in made[0]["facts"]})
        self.assertEqual({r["created_by"] for r in made}, {"engine"})
        self.assertEqual(reports.due(now), [])
        self.assertEqual(reports.due(datetime(2026, 10, 3, 0, 40, tzinfo=timezone.utc)), [])
        self.assertIn("report_ready", [x["kind"] for x in alerts.list_alerts()["alerts"]])

    def test_accounts_opened_after_the_period_are_left_out(self):
        rep = reports.create("weekly", date(2026, 9, 22), "user", now=NOW)        # W39, before paper-main existed
        self.assertEqual(rep["numbers"]["accounts"], [])
        self.assertNotIn("acc.paper-main.pnl", {f["id"] for f in rep["facts"]})
        rep = reports.create("weekly", date(2026, 10, 2), "user", now=NOW)        # W40: opened on its Friday
        a = rep["numbers"]["accounts"][0]
        self.assertEqual((a["account_id"], a["partial"]), ("paper-main", True))
        rep = reports.create("daily", date(2026, 10, 6), "user", now=NOW)         # fully covered
        a = rep["numbers"]["accounts"][0]
        self.assertEqual((a["partial"], a["opened"], a["note"]), (False, None, None))

    def test_commentary(self):
        rep = reports.create("daily", date(2026, 10, 6), "user", now=NOW)
        fake = Fake(GOOD)
        llm.TRANSPORT = fake
        c = asyncio.run(reports.report_commentary(rep["id"], "standard", "en"))
        self.assertTrue(c["provenance"]["ai_generated"])
        self.assertEqual(c["kind"], "report")
        self.assertFalse(c["check"]["ok"])                   # "17 alerts" is not a fact → flagged, not removed
        self.assertEqual([u["text"] for u in c["check"]["unverified"]], ["17"])
        self.assertIn("17 alerts", c["output"]["summary"])
        sent = fake.calls[0][1]["content"]
        self.assertNotIn("sp7 test", sent)                   # numbers + generated labels only
        self.assertEqual(reports.get(rep["id"])["commentaries"][0]["id"], c["id"])
        self.assertGreater(ai_budgets.reports_caps().spent("reports"), 0)
        self.assertEqual(db.list_audit(action="report_commentary")[0]["detail"]["commentary_id"], c["id"])

    def test_budget_blocks_before_the_call(self):
        rep = reports.create("daily", date(2026, 10, 6), "user", now=NOW)
        ai_budgets.reports_caps().put("reports", 0.0, actor="user")
        fake = Fake(GOOD)
        llm.TRANSPORT = fake
        with self.assertRaises(TradingError) as cm:
            asyncio.run(reports.report_commentary(rep["id"], "standard", "en"))
        self.assertEqual((cm.exception.status, cm.exception.code), (402, "budget"))
        self.assertEqual(cm.exception.payload()["budget_key"], "reports")
        self.assertEqual(fake.calls, [])
        keys = [b["key"] for b in ai_budgets.HedgeFundBudgets(None, provider_modules=()).payload()["budgets"]]
        self.assertIn("reports", keys)

    def test_bad_answer_is_charged_and_not_shown(self):
        rep = reports.create("daily", date(2026, 10, 6), "user", now=NOW)
        llm.TRANSPORT = Fake(json.dumps({"summary": "x", "extra": 1}))
        with self.assertRaises(TradingError) as cm:
            asyncio.run(reports.report_commentary(rep["id"], "standard", "en"))
        self.assertEqual(cm.exception.code, "bad_json")
        self.assertEqual(reports.commentaries("report", rep["id"]), [])
        self.assertGreater(ai_budgets.reports_caps().spent("reports"), 0)


if __name__ == "__main__":
    unittest.main()
