"""Stage 7 (SPEC §17.2, §17.3): incubation runs — the length is chosen per run and then
IMMUTABLE, start/abort/retire/new-version rules, who may trigger what, the runtime
end to end on synthetic bars (decision with the shared decision block → allocation →
execution through netting/OMS → sub-ledger → session bookkeeping), auto trading off =
missed sessions, and the completion result (passed / failed / inconclusive).
Temp databases, a fake data loader, fake quotes. Paper only."""
import json
import unittest
from datetime import date, datetime, timezone

try:
    from sp_trading_testkit import EXPECTED, FRI, TradingCase, make_strategy
    from test_sp_backtest_support import FakeLoader, path
except ImportError:  # pragma: no cover
    from tests.sp_trading_testkit import EXPECTED, FRI, TradingCase, make_strategy
    from tests.test_sp_backtest_support import FakeLoader, path

from strategy_platform.api import routes
from strategy_platform.core import calendars
from strategy_platform.execution import control, oms
from strategy_platform.execution.errors import TradingError
from strategy_platform.lifecycle import registry
from strategy_platform.live import incubation
from strategy_platform.portfolio import ledger
from strategy_platform.storage import db

DAYS = calendars.trading_days(date(2026, 4, 1), date(2026, 12, 31))


def prices(base):
    return {d: oc for d, oc in zip(DAYS, path(base, len(DAYS), drift=0.0005))}


class IncubationCase(TradingCase):
    def setUp(self):
        super().setUp()
        self._exp = incubation.expected_for
        incubation.expected_for = lambda sid, v: dict(EXPECTED)
        self.addCleanup(setattr, incubation, "expected_for", self._exp)


class StartAbortTest(IncubationCase):
    def test_length_is_fixed_and_immutable(self):
        sid = make_strategy()
        out = incubation.start(sid, 60, "paper test", now=FRI)
        run = out["run"]
        self.assertEqual(out["strategy"]["state"], "incubation")
        self.assertEqual(run["start_date"], "2026-10-05")              # the next session after the request
        self.assertEqual(run["end_date"], incubation.add_sessions("nyse", date(2026, 10, 5), 60).isoformat())
        self.assertEqual(run["length_sessions"], 60)
        with self.assertRaises(db.StorageUnavailable):                   # trigger: insert-only
            with db._connect() as conn:
                conn.execute("UPDATE incubation_runs SET length_sessions = 100 WHERE id = ?", (run["id"],))
        self.assertEqual(incubation.run_row(run["id"])["length_sessions"], 60)
        self.assertFalse([r for r in routes.ROUTES if r[1].startswith("/v2/incubation") and r[0] in ("PUT", "PATCH")])
        acts = [a["action"] for a in db.list_audit(50)]
        self.assertIn("incubation_started", acts)

    def test_bounds_and_rules(self):
        sid = make_strategy(state="candidate")
        for n in (19, 251, 60.0, True):
            with self.assertRaises(ValueError):
                incubation.start(make_strategy(), n, "x")
        with self.assertRaises(TradingError) as cm:
            incubation.start(sid, 60, "not validated")
        self.assertEqual(cm.exception.code, "not_validated")
        v = make_strategy()
        with self.assertRaises(registry.Conflict) as cm:
            registry.transition(v, "incubation", "via the generic route")
        self.assertIn("start an incubation run", cm.exception.message)
        self.acc.set_halt(True, "kill", "x")
        el = incubation.eligibility(v)
        self.assertEqual((el["eligible"], el["code"]), (False, "halted"))
        self.acc.set_halt(False)
        self.assertTrue(incubation.eligibility(v)["eligible"])
        self.assertEqual(incubation.eligibility(v)["start_date_if_now"], "2026-10-05")

    def test_abort_and_restart_with_a_new_length(self):
        sid = make_strategy()
        r1 = incubation.start(sid, 60, "first")["run"]
        out = incubation.abort(r1["id"], "changed my mind")
        self.assertEqual(out["strategy"]["state"], "validated")
        self.assertEqual(out["run"]["status"], "aborted")
        with self.assertRaises(TradingError) as cm:
            incubation.abort(r1["id"], "again")
        self.assertEqual(cm.exception.code, "not_running")
        with self.assertRaises(registry.Conflict):
            registry.transition(sid, "validated", "generic route")
        r2 = incubation.start(sid, 30, "second, shorter")["run"]
        self.assertEqual(r2["length_sessions"], 30)
        self.assertEqual(incubation.run_row(r1["id"])["length_sessions"], 60)

    def test_retire_and_new_version_end_the_run(self):
        sid = make_strategy()
        r = incubation.start(sid, 60, "x")["run"]
        registry.transition(sid, "retired", "no longer needed")
        self.assertEqual(incubation.status(incubation.run_row(r["id"]))["status"], "aborted")
        sid2 = make_strategy(name="other")
        r2 = incubation.start(sid2, 60, "x")["run"]
        spec = registry.get_version(sid2, 1)["spec"]
        spec["name"] = "other v2"
        spec["portfolio"]["max_weight"] = 0.5
        registry.new_version(sid2, json.dumps(spec), 1)                 # back to draft (existing rule)
        self.rt._incubation_bookkeeping(FRI)
        st = incubation.status(incubation.run_row(r2["id"]))
        self.assertEqual(st["status"], "aborted")
        self.assertIn("new version", st["end_reason"])

    def test_live_stays_off(self):
        sid = make_strategy()
        incubation.start(sid, 60, "x")
        row = registry.get(sid)
        allowed = {a["to"]: a for a in registry.allowed_transitions(row)}
        self.assertFalse(allowed["live"]["allowed"])
        self.assertIn("live trading is off", allowed["live"]["reason_if_not"])


class RuntimeEndToEndTest(IncubationCase):
    def setUp(self):
        super().setUp()
        self.loader = FakeLoader({"SPY": prices(500.0), "QQQ": prices(400.0)})
        self.rt.loader = self.loader
        self.sid = make_strategy()
        self.run = incubation.start(self.sid, 20, "end to end", now=FRI)["run"]

    def quote_at(self, d, field):
        for s, base in (("SPY", 500.0), ("QQQ", 400.0)):
            o, c = self.loader.prices[s][d]
            self.quotes.prices[s] = o if field == "open" else c

    def test_decide_execute_book(self):
        src = self.sid
        alloc = ledger.source_state("paper-main", src)
        self.assertAlmostEqual(alloc["capital_in"], 40000.0)            # max weight 0.4 of the 100,000 pool
        # Friday after the close: the decision for 2026-10-02 (data known at that close)
        self.set_now(datetime(2026, 10, 2, 20, 30, tzinfo=timezone.utc))
        self.quote_at(date(2026, 10, 2), "close")
        out = self.rt.cycle()
        self.assertEqual(out["summary"]["decisions"], 1, out)
        d = self.rt.latest_decision(self.run["id"])
        self.assertEqual(d["bar_date"], "2026-10-02")
        self.assertEqual(d["result"]["targets"], {"SPY": 0.5, "QQQ": 0.5})
        self.assertEqual(d["result"]["window"]["start"], "2026-10-05T13:36:00+00:00")
        # Monday 09:40 NY, auto trading on: executed at the open through netting + OMS
        control.set_autotrade(True, "user")
        self.set_now(datetime(2026, 10, 5, 13, 40, tzinfo=timezone.utc))
        control.touch(self.clock())
        self.quote_at(date(2026, 10, 5), "open")
        out = self.rt.cycle()
        self.assertEqual(out["summary"]["orders"], 2, out)
        hold = ledger.holdings("paper-main")[src]
        self.assertEqual(set(hold), {"SPY", "QQQ"})
        o = oms.list_orders("paper-main")[0]
        self.assertEqual((o["origin"], o["state"], o["allocations"][0]["source_id"]), ("runtime", "filled", src))
        self.assertTrue(o["client_order_id"].startswith("rt-ex-"))
        st = ledger.source_state("paper-main", src, {"SPY": self.quotes.prices["SPY"], "QQQ": self.quotes.prices["QQQ"]})
        self.assertLess(abs(st["equity"] - 40000.0), 100.0)
        acc_qty = {p["symbol"]: p["qty"] for p in self.acc.store.get_positions()}
        self.assertEqual(acc_qty, hold)
        # Monday after the close: next decision; the session 2026-10-05 counts as traded
        self.set_now(datetime(2026, 10, 5, 20, 30, tzinfo=timezone.utc))
        control.touch(self.clock())
        self.quote_at(date(2026, 10, 5), "close")
        self.rt.cycle()
        self.rt.cycle()
        st = incubation.status(incubation.run_row(self.run["id"]))
        self.assertEqual(st["progress"]["traded"], 1)
        # auto trading off on Tuesday: not executed -> the session is missed
        control.set_autotrade(False, "user")
        self.set_now(datetime(2026, 10, 6, 13, 40, tzinfo=timezone.utc))
        self.quote_at(date(2026, 10, 6), "open")
        self.rt.cycle()
        self.set_now(datetime(2026, 10, 6, 20, 30, tzinfo=timezone.utc))
        self.quote_at(date(2026, 10, 6), "close")
        self.rt.cycle()
        self.rt.cycle()
        st = incubation.status(incubation.run_row(self.run["id"]))
        self.assertEqual((st["progress"]["traded"], st["progress"]["missed"]), (1, 1))
        decs = routes.V2Api and __import__("strategy_platform.api.trading_routes",
                                           fromlist=["decisions_of"]).decisions_of(self.run["id"])
        self.assertEqual([x["status"] for x in decs[:3]], ["pending", "not_executed", "executed"])

    def test_data_not_there_yet_waits(self):
        self.loader.prices["SPY"] = {d: v for d, v in self.loader.prices["SPY"].items() if d < date(2026, 10, 2)}
        self.loader.prices["QQQ"] = {d: v for d, v in self.loader.prices["QQQ"].items() if d < date(2026, 10, 2)}
        self.set_now(datetime(2026, 10, 2, 20, 30, tzinfo=timezone.utc))
        out = self.rt.cycle()
        self.assertEqual(out["summary"]["decisions"], 0)
        self.assertIn("not available yet", list(out["summary"]["errors"].values())[0])
        self.assertIsNone(self.rt.latest_decision(self.run["id"]))


class CompletionTest(IncubationCase):
    def make_run(self, path_returns, missed=0):
        sid = make_strategy()
        run = incubation.start(sid, 20, "completion", now=FRI)["run"]
        r = incubation.run_row(run["id"])
        days = incubation.sessions_between("nyse", date(2026, 10, 5), date.fromisoformat(r["end_date"]))
        eq = 40000.0
        snaps = [(calendars.previous_trading_day(days[0]), eq)]
        for d, ret in zip(days, path_returns):
            eq *= (1 + ret)
            snaps.append((d, eq))
        with db._connect() as conn:
            for d, e in snaps:
                conn.execute("INSERT OR REPLACE INTO sl_snapshots VALUES (?, ?, ?, ?, ?, ?, 0, 0)",
                             ("paper-main", sid, d.isoformat(), datetime.combine(d, datetime.min.time(),
                                                                                         tzinfo=timezone.utc)
                              .replace(hour=21).isoformat(), e, e))
                # S10-H: snapshots at the session close (21:00 UTC), like the runtime and sp_monitor_testkit —
                # realised now starts at the session BEFORE start_date, so the start-day allocation (15:00) must lie
                # before that snapshot, as it does in reality
        for k, d in enumerate(days):
            incubation.record_session(r, d, k >= missed, None)
        return incubation.run_row(run["id"]), days

    def test_passed_failed_inconclusive(self):
        r, days = self.make_run([0.0005] * 20)
        data = incubation.complete(r)
        self.assertEqual(data["result"], "passed", data)
        st = incubation.status(r)
        self.assertEqual(st["status"], "completed")
        self.assertTrue(st["live_eligible"])
        self.assertIsNotNone(st["tracking_error"])
        r2, _ = self.make_run([-0.02] * 20)
        d2 = incubation.complete(r2)
        self.assertEqual(d2["result"], "failed")
        self.assertTrue(any("outside the expected band" in x for x in d2["result_reasons"]))
        self.assertTrue(any("max drawdown" in x for x in d2["result_reasons"]))
        r3, _ = self.make_run([0.0005] * 20, missed=5)
        self.assertEqual(incubation.complete(r3)["result"], "inconclusive")
        self.assertIsNone(incubation.complete(r3))                     # completed exactly once


class ExpectedBandFromTheCardTest(unittest.TestCase):
    """expected_for() on the real run-card shape (curve.dates/equity, range.calendar, metrics.performance) —
    the other incubation tests stub it (audit 2026-10-18)."""

    def test_band_inputs_and_dates_across_a_valuation_gap(self):
        import math
        from strategy_platform.backtest import runcard
        card = {"curve": {"dates": ["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"],
                          "equity": [100.0, 101.0, None, 99.99, 100.9899]},
                "range": {"calendar": "nyse"}, "metrics": {"performance": {"max_drawdown": 0.01}}}
        orig = (runcard.latest_done, runcard.get)
        runcard.latest_done = lambda sid, v, kind=None: {"id": "bt-card"} if kind == "holdout" else None
        runcard.get = lambda rid: card
        try:
            exp = incubation.expected_for("s-x", 1)
        finally:
            runcard.latest_done, runcard.get = orig
        lr = [math.log(101 / 100), math.log(99.99 / 101), math.log(100.9899 / 99.99)]
        self.assertEqual(exp["sessions"], 3)
        self.assertAlmostEqual(exp["mean_daily"], sum(lr) / 3, places=12)
        self.assertEqual(exp["max_drawdown"], 0.01)
        self.assertEqual(exp["source_run_id"], "bt-card")
        self.assertEqual([d for d, _r in exp["returns"]], ["2026-01-05", "2026-01-07", "2026-01-08"])
        runcard.latest_done = lambda sid, v, kind=None: None
        try:
            with self.assertRaises(TradingError) as cm:
                incubation.expected_for("s-x", 1)
        finally:
            runcard.latest_done, runcard.get = orig
        self.assertEqual(cm.exception.code, "no_holdout")


if __name__ == "__main__":
    unittest.main()
