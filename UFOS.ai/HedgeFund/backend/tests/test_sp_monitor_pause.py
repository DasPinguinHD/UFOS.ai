"""Stage 9 (SPEC §19.2): pause semantics in the runtime — a strategy paused by the decay monitor gets its decisions
recorded but sends NO orders (``not_executed``, positions and capital kept); keep running → it trades again; demote →
the run is aborted and the runtime closes the source. Same synthetic setup as the stage-7 end-to-end test. Offline."""
import unittest
from datetime import date, datetime, timezone

try:
    from sp_monitor_testkit import FRI, MonCase, make_strategy
    from test_sp_backtest_support import FakeLoader, path
except ImportError:  # pragma: no cover
    from tests.sp_monitor_testkit import FRI, MonCase, make_strategy
    from tests.test_sp_backtest_support import FakeLoader, path

from strategy_platform.api import trading_routes
from strategy_platform.core import calendars
from strategy_platform.execution import control, oms
from strategy_platform.live import incubation
from strategy_platform.monitoring import decay
from strategy_platform.portfolio import ledger
from strategy_platform.storage import db

DAYS = calendars.trading_days(date(2026, 4, 1), date(2026, 12, 31))


def prices(base):
    return {d: oc for d, oc in zip(DAYS, path(base, len(DAYS), drift=0.0005))}


def T(d, hh, mm):
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=timezone.utc)


class PauseTest(MonCase):
    def setUp(self):
        super().setUp()
        self.loader = FakeLoader({"SPY": prices(500.0), "QQQ": prices(400.0)})
        self.rt.loader = self.loader
        self.sid = make_strategy()
        self.run = incubation.start(self.sid, 20, "pause test", now=FRI)["run"]
        control.set_autotrade(True, "user")

    def quote_at(self, d, field):
        for s in ("SPY", "QQQ"):
            o, c = self.loader.prices[s][d]
            self.quotes.prices[s] = o if field == "open" else c

    def step(self, d, hh, mm, field):
        self.set_now(T(d, hh, mm))
        control.touch(self.clock())
        self.quote_at(d, field)
        return self.rt.cycle()

    def pause(self):
        decay._insert_event(self.sid, self.run["id"], "paused", "engine", "test pause", "drawdown", "2026-10-05",
                            {"index": 1, "dd": 0.2}, db.utc_now_iso())

    def test_paused_sends_no_orders_and_keeps_positions(self):
        self.step(date(2026, 10, 2), 20, 30, "close")            # decision for 10-02
        self.step(date(2026, 10, 5), 13, 40, "open")             # executed at Monday's open
        hold = dict(ledger.holdings("paper-main")[self.sid])
        n_orders = len(oms.list_orders("paper-main", limit=500))
        self.assertEqual(set(hold), {"SPY", "QQQ"})
        self.pause()
        # Monday close: a new decision is still taken and stored ...
        out = self.step(date(2026, 10, 5), 20, 30, "close")
        self.assertEqual(out["summary"]["decisions"], 1, out)
        # ... but Tuesday's execution window sends nothing for the paused source
        out = self.step(date(2026, 10, 6), 13, 40, "open")
        self.assertEqual(out["summary"]["orders"], 0, out)
        self.assertEqual(len(oms.list_orders("paper-main", limit=500)), n_orders)
        self.assertEqual(ledger.holdings("paper-main")[self.sid], hold)          # positions kept
        decs = trading_routes.decisions_of(self.run["id"])
        self.assertEqual(decs[0]["status"], "not_executed")
        self.assertIn("paused by the decay monitor", decs[0]["reason"])
        self.assertGreater(ledger.source_state("paper-main", self.sid)["capital_in"], 0)
        # keep running: the next decision executes again
        self.set_now(T(date(2026, 10, 6), 14, 0))
        decay.decide(self.sid, "keep_running", "accepted after review")
        self.step(date(2026, 10, 6), 20, 30, "close")
        self.step(date(2026, 10, 7), 13, 40, "open")
        decs = trading_routes.decisions_of(self.run["id"])
        self.assertEqual(decs[0]["status"], "executed", decs[:2])

    def test_protective_exits_still_run_while_paused(self):
        """Review M3 (default, user away 2026-10-18 — confirm): a paused strategy's own risk-REDUCING exits (the
        decision's ``force`` symbols: stop loss / take profit / drawdown stop) still run; nothing else does."""
        self.step(date(2026, 10, 2), 20, 30, "close")
        self.step(date(2026, 10, 5), 13, 40, "open")
        hold = dict(ledger.holdings("paper-main")[self.sid])
        self.pause()
        self.step(date(2026, 10, 5), 20, 30, "close")             # the next decision is stored
        orig = self.rt.latest_decision

        def with_stop(run_id):
            d = orig(run_id)
            if d is not None:
                d = dict(d, result=dict(d["result"], force=["SPY"], targets={"SPY": 0.9, "QQQ": 0.9}))
            return d
        self.rt.latest_decision = with_stop
        out = self.step(date(2026, 10, 6), 13, 40, "open")
        self.assertEqual(out["summary"]["orders"], 1, out)
        now_hold = {s: q for s, q in ledger.holdings("paper-main")[self.sid].items() if q}
        self.assertEqual(now_hold, {"QQQ": hold["QQQ"]})                   # SPY closed, QQQ untouched (no increase)
        o = oms.list_orders("paper-main", limit=1)[0]
        self.assertEqual((o["symbol"], o["side"]), ("SPY", "sell"))
        decs = trading_routes.decisions_of(self.run["id"])
        self.assertEqual(decs[0]["status"], "executed")
        # recorded and visible: one alert per order, in the strategy's monitoring view
        from strategy_platform.monitoring import alerts
        al = [a for a in alerts.list_alerts()["alerts"] if a["kind"] == "protective_exit_while_paused"]
        self.assertEqual(len(al), 1)
        self.assertEqual((al[0]["data"]["symbol"], al[0]["data"]["qty"], al[0]["strategy_id"]),
                         ("SPY", hold["SPY"], self.sid))
        det = decay.detail(self.sid)
        self.assertEqual([x["symbol"] for x in det["protective_exits"]], ["SPY"])
        self.assertEqual(det["strategy"]["decay"]["protective_exits"], 1)
        self.assertIsNone(decay.record_protective_exit(incubation.run_row(self.run["id"]),
                                                       {"client_order_id": o["client_order_id"], "symbol": "SPY",
                                                        "side": "sell", "state": "filled"}, hold["SPY"], ["SPY"]))

    def test_demote_aborts_and_the_runtime_closes(self):
        self.step(date(2026, 10, 2), 20, 30, "close")
        self.step(date(2026, 10, 5), 13, 40, "open")
        self.assertTrue(ledger.holdings("paper-main")[self.sid])
        self.pause()
        self.set_now(T(date(2026, 10, 5), 15, 0))
        decay.decide(self.sid, "demote", "decayed", confirm="DEMOTE")
        self.assertEqual(db.strategy_get(self.sid)["state"], "validated")
        self.step(date(2026, 10, 6), 13, 40, "open")
        self.assertFalse({s: q for s, q in ledger.holdings("paper-main").get(self.sid, {}).items() if q})

    def test_protective_kill_still_works_while_paused(self):
        self.step(date(2026, 10, 2), 20, 30, "close")
        self.step(date(2026, 10, 5), 13, 40, "open")
        self.pause()
        self.set_now(T(date(2026, 10, 5), 15, 0))
        out = control.kill_platform("test kill", "user")
        self.assertTrue(out["ok"], out)
        from strategy_platform.portfolio import ledger as lg
        lg.attribute(self.acc)
        self.assertFalse({s: q for s, q in ledger.holdings("paper-main").get(self.sid, {}).items() if q})


if __name__ == "__main__":
    unittest.main()
