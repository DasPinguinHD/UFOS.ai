"""Stage 9 (SPEC §19.3): alert events — the audit scanner (first start does not flood, every mapped action, dedupe),
the broker link watcher (one alert per outage ≥ 10 min, reconnected), list/ack, the monitor tick. Offline."""
import unittest
from datetime import timedelta

try:
    from sp_monitor_testkit import FRI, MonCase
except ImportError:  # pragma: no cover
    from tests.sp_monitor_testkit import FRI, MonCase

from strategy_platform.monitoring import alerts, service
from strategy_platform.storage import db


class AuditScannerTest(MonCase):
    def test_first_start_sets_the_cursor_only(self):
        db.add_audit("engine", "platform_kill", {"reason": "old", "accounts": []})
        self.assertEqual(alerts.scan_audit(), 0)
        self.assertEqual(alerts.list_alerts()["alerts"], [])
        self.assertIsNotNone(db.meta_get(alerts.CURSOR_KEY))

    def test_every_producer(self):
        alerts.scan_audit()
        db.add_audit("engine", "broker_reconcile_mismatch", {"account_id": "alpaca-paper", "reconciliation_id": 3,
                                                             "reason": "Reconciliation mismatch: SPY 10 vs 0"})
        db.add_audit("user", "platform_kill", {"reason": "panic", "accounts": [{"account_id": "paper-main"}]})
        db.add_audit("user", "broker_kill", {"account_id": "kraken-demo", "reason": "x", "orders": 2, "pending": True})
        db.add_audit("engine", "post_trade_halt", {"account_id": "paper-main", "kind": "drawdown", "drawdown_pct": 10.4})
        db.add_audit("engine", "post_trade_halt", {"account_id": "paper-main", "kind": "daily_loss", "class": "equity",
                                                   "pnl_pct": -2.1, "limit_pct": 2.0, "until": "2026-10-03"})
        db.add_audit("engine", "autotrade_heartbeat_off", {"last_client_seen": "…", "timeout_minutes": 15,
                                                           "cancelled_orders": 1})
        db.add_audit("engine", "incubation_completed", {"run_id": "inc-1", "strategy_id": "s-x", "result": "passed",
                                                        "reasons": []})
        db.add_audit("user", "settings_changed", {"x": 1})          # not alert-worthy
        self.assertEqual(alerts.scan_audit(), 7)
        kinds = [a["kind"] for a in reversed(alerts.list_alerts()["alerts"])]
        self.assertEqual(kinds, ["reconciliation_halt", "kill_switch", "kill_switch", "drawdown_kill",
                                 "daily_loss_limit", "autotrade_off", "incubation_finished"])
        a = alerts.list_alerts()["alerts"][-1]
        self.assertEqual((a["severity"], a["toast"], a["account_id"]), ("critical", True, "alpaca-paper"))
        self.assertIn("SPY 10 vs 0", a["body"])
        self.assertEqual(alerts.scan_audit(), 0)                     # cursor moved: nothing twice
        # dedupe key: the same event cannot become a second alert
        self.assertIsNone(alerts.emit("kill_switch", "t", "b", f"audit:{a['data']['audit_id']}"))

    def test_list_after_and_ack(self):
        ids = [alerts.emit("decay", f"t{i}", "b", f"k{i}") for i in range(3)]
        out = alerts.list_alerts(after=ids[0])
        self.assertEqual([a["id"] for a in out["alerts"]], [ids[2], ids[1]])      # newest first, id > after
        self.assertEqual(out["unacked"], 3)
        self.assertEqual(out["last_id"], ids[2])
        self.assertEqual(alerts.ack([ids[0], ids[0], 999999])["acked"], 1)
        self.assertEqual(alerts.list_alerts(unacked=True)["unacked"], 2)
        self.assertEqual(alerts.ack(all_=True)["unacked"], 0)
        self.assertTrue(alerts.list_alerts()["alerts"][0]["acked"])
        with self.assertRaises(ValueError):
            alerts.emit("nope", "t", "b", "k")
        with self.assertRaises(db.StorageUnavailable):
            with db._connect() as conn:
                conn.execute("DELETE FROM mon_alerts")


class FakeLink:
    def __init__(self):
        self.v = {"state": "ok", "attempt": 0, "message": "Connected", "last_ok_at": None, "next_retry_at": None}

    def view(self):
        return dict(self.v)


class LinkWatcherTest(MonCase):
    def test_one_alert_per_outage(self):
        from strategy_platform.execution import accounts
        from strategy_platform.execution.brokers import mirror
        link = FakeLink()
        orig_rows, orig_link = accounts.rows, mirror.link
        accounts.rows = lambda include_disconnected=False: [{"id": "alpaca-paper"}]
        mirror.link = lambda aid: link
        self.addCleanup(setattr, accounts, "rows", orig_rows)
        self.addCleanup(setattr, mirror, "link", orig_link)
        t0 = FRI
        link.v.update(state="waiting", attempt=2, message="Waiting for broker (attempt 2)",
                      last_ok_at=(t0 - timedelta(minutes=3)).isoformat())
        self.assertEqual(alerts.watch_links(t0), 0)                   # down 3 min
        self.assertEqual(alerts.watch_links(t0 + timedelta(minutes=8)), 1)
        self.assertEqual(alerts.watch_links(t0 + timedelta(minutes=20)), 0)   # once per outage
        alerts.reset_state()                                                    # e.g. a backend restart
        self.assertEqual(alerts.watch_links(t0 + timedelta(minutes=20)), 0)   # deduped alert is not counted
        link.v.update(state="ok")
        self.assertEqual(alerts.watch_links(t0 + timedelta(minutes=21)), 1)
        kinds = [a["kind"] for a in alerts.list_alerts()["alerts"]]
        self.assertEqual(kinds, ["broker_reconnected", "broker_disconnected"])
        self.assertFalse(alerts.list_alerts()["alerts"][0]["toast"])


class TickTest(MonCase):
    def test_tick_runs_every_step(self):
        out = service.tick(FRI)
        self.assertEqual(out["errors"], {})
        self.assertIsNotNone(service.state["last_tick_at"])

    def test_hung_tick_is_stale(self):
        from datetime import datetime, timezone
        saved = dict(service.state)
        self.addCleanup(service.state.update, saved)
        now = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
        service.state.update(running=True, last_tick_at="2026-10-18T11:50:00+00:00",
                             tick_started_at=now - timedelta(minutes=6))
        st = service.status(now)
        self.assertTrue(st["stale"] and st["tick_in_progress"])
        self.assertEqual(st["tick_started_at"], "2026-10-18T11:54:00+00:00")
        service.state.update(tick_started_at=None, last_tick_at="2026-10-18T11:59:00+00:00")
        self.assertFalse(service.status(now)["stale"])
        service.state.update(last_tick_at="2026-10-18T11:50:00+00:00")
        self.assertTrue(service.status(now)["stale"])                       # no tick for 10 min

    def test_overview_keeps_a_failing_strategy(self):
        from strategy_platform.monitoring import decay
        sid, run = self.start_run()
        orig = decay.monitor_view
        decay.monitor_view = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("broken view"))
        self.addCleanup(setattr, decay, "monitor_view", orig)
        ov = service.overview()
        self.assertEqual(ov["strategies"][0]["strategy_id"], sid)
        self.assertIn("broken view", ov["strategies"][0]["error"])
        self.assertIn("stale", ov["monitor"])


if __name__ == "__main__":
    unittest.main()
