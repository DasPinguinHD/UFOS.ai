"""fund/export.py (paper trading only): orders.csv and decisions.csv —
header, RFC 4180 quoting of commas/quotes/newlines, CSV-injection guard,
recommendations joined with the agent scores of their run, AI labelling
columns, empty / disabled store.

Run from HedgeFund/backend:  python -m unittest tests.test_fund_export -v
"""
import csv
import io
import os
import tempfile
import unittest

from fund import export
from fund import store
from fund.types import AgentSignal
from newsroom import db

TS = "2026-10-02T14:00:00+00:00"


def rows(text):
    return list(csv.DictReader(io.StringIO(text)))


class ExportTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.assertTrue(db.init(os.path.join(self._tmp.name, "n.db"), legacy=""))

    def tearDown(self):
        db.close()
        self._tmp.cleanup()

    def test_empty_store_has_header_only(self):
        self.assertEqual(export.orders_csv(store).strip(), ",".join(export.ORDER_COLUMNS))
        self.assertEqual(export.decisions_csv(store).strip(), ",".join(export.DECISION_COLUMNS))

    def test_orders_csv_quoting_and_order(self):
        store.insert_order(TS, "SPY", "buy", 10, "filled", "manual", rationale='manual, "quoted"\nline two',
                           fill_price=500.25, fees=2.5, notional=5002.5, filled_at=TS)
        store.insert_order("2026-10-02T15:00:00+00:00", "QQQ", "short", 3, "rejected", "autopilot",
                           reject_reason="=HYPERLINK(\"x\")")
        text = export.orders_csv(store)
        self.assertTrue(text.startswith("id,ts,symbol,side,qty,status,origin"))
        got = rows(text)
        self.assertEqual([r["symbol"] for r in got], ["SPY", "QQQ"])          # oldest first
        self.assertEqual(got[0]["rationale"], 'manual, "quoted"\nline two')
        self.assertEqual(got[0]["fill_price"], "500.25")
        self.assertEqual(got[1]["reject_reason"], "'=HYPERLINK(\"x\")")        # formula neutralised
        self.assertEqual(got[1]["fill_price"], "")
        self.assertEqual(got[0]["mode"], "paper")
        self.assertEqual({r["note"] for r in got}, {export.EXPORT_NOTE})

    def test_disclaimer_column_keeps_csv_parseable(self):
        self.assertEqual(export.ORDER_COLUMNS[-1], "note")
        self.assertEqual(export.DECISION_COLUMNS[-1], "note")
        self.assertIn("not investment advice", export.EXPORT_NOTE)
        store.insert_order(TS, "SPY", "buy", 1, "filled", "manual", rationale="-1+1")
        run = store.create_run(TS, 1, "manual", True)
        store.save_recommendation(run, TS, "SPY", "LONG", 0.8, 0.04, 0.3, "@SUM(A1)", "rules")
        for text, cols in ((export.orders_csv(store), export.ORDER_COLUMNS),
                           (export.decisions_csv(store), export.DECISION_COLUMNS)):
            table = list(csv.reader(io.StringIO(text)))
            self.assertEqual(tuple(table[0]), cols)                       # first row is the header
            self.assertTrue(all(len(r) == len(cols) for r in table))       # rectangular
            self.assertEqual(table[1][-1], export.EXPORT_NOTE)
            self.assertTrue(table[1][cols.index("rationale")].startswith("'"))   # injection guard kept

    def test_decisions_joined_with_agent_scores(self):
        run = store.create_run(TS, 1, "manual", True)
        store.save_agent_scores(run, [
            AgentSignal("technical", "SPY", 0.5, 0.7, "up"),
            AgentSignal("macro", "SPY", -0.2, 0.4, "mixed"),
            AgentSignal("sentiment", "SPY", 0.3, 0.6, "news", source="llm", provenance={"ai_generated": True}),
            AgentSignal("risk", "SPY", 0.0, 1.0, "ok", details={"size_multiplier": 0.5, "veto": False,
                                                              "veto_short": True}),
        ])
        store.save_recommendation(run, TS, "SPY", "LONG", 0.8, 0.04, 0.33, "+Buy, because trend", "llm",
                                  model="m/1", provenance={"ai_generated": True, "model": "m/1"},
                                  usage={"cost_usd": 0.0012})
        store.save_recommendation(run, TS, "QQQ", "FLAT", 0.1, 0.0, 0.01, "Rule-based: weak", "rules")
        got = rows(export.decisions_csv(store))
        self.assertEqual([r["symbol"] for r in got], ["SPY", "QQQ"])
        spy, qqq = got
        self.assertEqual((spy["technical_score"], spy["macro_score"], spy["sentiment_source"]), ("0.5", "-0.2", "llm"))
        self.assertEqual((spy["risk_size_multiplier"], spy["risk_veto"], spy["risk_veto_short"]), ("0.5", "no", "yes"))
        self.assertEqual((spy["ai_generated"], spy["model"], spy["cost_usd"]), ("yes", "m/1", "0.0012"))
        self.assertEqual(spy["rationale"], "'+Buy, because trend")
        self.assertEqual((qqq["ai_generated"], qqq["technical_score"]), ("no", ""))

    def test_disabled_store(self):
        db.close()
        self.assertEqual(len(export.decisions_csv(store).splitlines()), 1)
        self.assertEqual(len(export.orders_csv(store).splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
