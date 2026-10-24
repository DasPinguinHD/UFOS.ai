"""OS1 review fixes (SP §21.11, review 2026-10-24): config ranges (NaN / Infinity / cadence 0 / oi_slot before the
floor refused, never clamped) and the bounded slot loop; recursive triggers (INSERT OR REPLACE cannot delete); OI
observations never collapse (pre-floor corrections, duplicates in one answer → ``oi_conflict``); clock skew from the
server Date (gap > 30 s, stop > 5 min); service resilience (a crashing run → failed run + ``crash`` gap, the loop
continues; status writes never raise; a fatal loop error → state error + exit 7); slot windows (missed OI / FRED after
a sleep are NOT run under the old slot; run-slot outside its window → ``manual:<slot>``, never ranked); the calendar's
end (status shows the error, the service retries hourly); sandbox mode (run-once refused, exit 6; register / run-once
audit refused 409); run-once writes only the ``manual`` status block; late fixed slots count as gaps; /gaps ≤ 31 days
inclusive and bad dates → 400; the heartbeat during long runs."""
import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest import mock

try:
    from os_archive_testkit import KID, SKEY, ArchiveCase, Clock, FakeApi, Sec
    from sp_trading_testkit import TradingCase
except ImportError:  # pragma: no cover
    from tests.os_archive_testkit import KID, SKEY, ArchiveCase, Clock, FakeApi, Sec
    from tests.sp_trading_testkit import TradingCase

from strategy_platform.options_archive import config, db, report, slots  # noqa: E402
from strategy_platform.options_archive.config import Config  # noqa: E402

import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location(
    "options_archive_tool_r2", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools",
                                            "options_archive.py"))
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)

UTC = timezone.utc
D = datetime(2026, 10, 26, tzinfo=UTC).date()
CFG = Config()


def slot(label, d=D):
    return slots.find(d, label, CFG)


class ConfigRangesTest(ArchiveCase):
    def load(self, text):
        p = os.path.join(self.tmp, "cfg.json")
        with open(p, "w") as fh:
            fh.write(text)
        return config.load(p)

    def test_refused_never_clamped(self):
        self.assertEqual(config.validate(Config()), Config())
        for text, msg in (('{"cadence_minutes": 0}', "cadence_minutes"), ('{"strike_band_pct": NaN}', "NaN"),
                          ('{"quote_max_age_s": Infinity}', "NaN"), ('{"expiry_days": 1e999}', "finite"),
                          ('{"timeouts_s": {"fred": 0}}', "timeouts_s.fred"),
                          ('{"timeouts_s": {"bogus": 5}}', "unknown key"), ('{"max_pages": 5000}', "max_pages"),
                          ('{"retries": 2}', "retries"), ('{"oi_slot": "07:30"}', "oi_floor"),
                          ('{"symbols": ["SPY", "TSLA"]}', "symbols"), ('{"heartbeat_s": 600}', "heartbeat_s"),
                          ('{"expiry_days": 4.5}', "whole number"), ('{"intraday_first": "25:00"}', "HH:MM"),
                          ('{"fred_series": ["VIX CLS"]}', "fred_series"), ("[1]", "JSON object"),
                          ("{bad json", "not valid JSON")):
            with self.assertRaises(ValueError, msg=text) as cm:
                self.load(text)
            self.assertIn(msg, str(cm.exception), text)

    def test_cli_refuses_to_start_on_bad_config(self):
        with open(os.path.join(self.tmp, "options_archive_config.json"), "w") as fh:
            fh.write('{"cadence_minutes": 0}')
        lines = []
        self.assertEqual(tool.main(["service"], data_dir=self.tmp, out=lines.append), 3)
        self.assertIn("cadence_minutes", lines[0])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "options_archive.db")))

    def test_slot_loop_bounded(self):
        from dataclasses import replace
        with self.assertRaises(ValueError):
            slots.day_slots(D, replace(Config(), cadence_minutes=0))       # bypassing validate: no endless loop
        self.assertEqual(len(slots.day_slots(D, replace(Config(), cadence_minutes=5))), 73 + 3)   # 09:45…15:45 every 5 min (09:55 / 15:25 on the grid) + oi, fred, eod


class DbRecursiveTriggerTest(ArchiveCase):
    def test_insert_or_replace_cannot_delete(self):
        conn = db.open_rw(os.path.join(self.tmp, "x.db"))
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA recursive_triggers").fetchone()[0], 1)
        sql = ("INSERT OR REPLACE INTO os_runs (id, kind, slot_ms, started_ms, finished_ms, status, source, "
               "model_version, assumptions_hash, app_version) VALUES (1, 'intraday', 1, 1, 2, ?, 'a', 'raw', 'h', 'v')")
        conn.execute(sql, ("ok",))
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute(sql, ("failed",))
        self.assertEqual(conn.execute("SELECT status FROM os_runs").fetchone()[0], "ok")
        conn.execute("INSERT INTO os_fred (series_id, date, value, available_ms, observed_ms) VALUES ('V', 'd', 1, 1, 1)")
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("REPLACE INTO os_fred (series_id, date, value, available_ms, observed_ms) "
                         "VALUES ('V', 'd', 2, 1, 1)")


class OiNeverCollapsesTest(ArchiveCase):
    def test_pre_floor_correction_and_duplicate(self):
        s = slot("oi")
        clock = Clock(s.utc - timedelta(hours=1))           # 07:30 NY: before the 08:00 floor of today's OI
        api = FakeApi(clock, D, oi_date="2026-10-23")
        self.make(clock.now(), api)
        # a pre-floor fetch of OI valid for 10-23 (floor = 10-26 08:00 NY): two different values must both survive
        self.arch.run(slots.Slot("oi", s.ny - timedelta(hours=1), "oi"), manual=True)
        api.oi_value = 999
        self.clock.advance(60)
        self.arch.run(slots.Slot("oi", s.ny - timedelta(hours=1), "oi"), manual=True)
        sym = self.rows("SELECT symbol FROM os_contracts LIMIT 1")[0]["symbol"]
        rows = self.rows("SELECT open_interest, available_ms, observed_ms, early_oi FROM os_open_interest "
                         "WHERE symbol = ? ORDER BY observed_ms", (sym,))
        self.assertEqual([r["open_interest"] for r in rows], [1234, 999])
        self.assertEqual(rows[0]["available_ms"], rows[1]["available_ms"])          # both at the floor
        self.assertEqual({r["early_oi"] for r in rows}, {1})
        self.assertLess(rows[0]["observed_ms"], rows[1]["observed_ms"])            # the tie-breaker for readers
        self.assertEqual(self.rows("SELECT slot_label, kind FROM os_runs")[0], {"slot_label": "manual:oi",
                                                                                "kind": "manual"})

    def test_duplicate_in_one_answer_is_a_gap(self):
        s = slot("oi")
        clock = Clock(s.utc)
        api = FakeApi(clock, D, oi_date="2026-10-23")
        orig = api.contracts

        def doubled(sym):
            rows = orig(sym)
            twin = dict(rows[0], open_interest="77")
            return rows + [twin]
        api.contracts = doubled
        self.make(clock.now(), api)
        out = self.arch.run(s)
        self.assertEqual(out["status"], "partial")
        g = self.rows("SELECT kind, what FROM os_gaps")
        self.assertEqual({x["kind"] for x in g}, {"oi_conflict"})
        self.assertIn("first value was kept", g[0]["what"])


class ClockSkewTest(ArchiveCase):
    def run_skew(self, skew, date_header=True):
        s = slot("15:25")
        clock = Clock(s.start_utc(CFG))
        api = FakeApi(clock, D, skew_s=skew, date_header=date_header)
        api.slot_ms = s.ms
        self.make(clock.now(), api)
        return self.arch.run(s)

    def test_small_skew_recorded(self):
        out = self.run_skew(2.0)
        self.assertEqual(out["status"], "ok")
        self.assertTrue(1000 <= self.rows("SELECT skew_ms FROM os_runs")[0]["skew_ms"] <= 3000)

    def test_gap_above_30s(self):
        out = self.run_skew(60.0)
        self.assertEqual(out["status"], "partial")
        self.assertEqual([g["kind"] for g in self.rows("SELECT kind FROM os_gaps")], ["clock_skew"])
        self.assertEqual(report.runs(self.arch.conn, 1)[0]["skew_ms"] // 1000, 59)

    def test_stop_above_5min(self):
        out = self.run_skew(-600.0)
        self.assertEqual((out["status"], out["exit_code"]), ("failed", 3))
        self.assertTrue(out["error"].startswith("clock:"))
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM os_chain_snapshots")[0]["n"], 0)
        self.assertEqual([g["kind"] for g in self.rows("SELECT kind FROM os_gaps")], ["clock_skew"])

    def test_no_date_header(self):
        self.assertEqual(self.run_skew(0, date_header=False)["status"], "ok")
        self.assertIsNone(self.rows("SELECT skew_ms FROM os_runs")[0]["skew_ms"])


class ResilienceTest(ArchiveCase):
    def test_crashing_run_is_recorded_and_loop_continues(self):
        start = datetime(2026, 10, 26, 13, 46, tzinfo=UTC)          # 09:46 NY
        self.make(start)
        from strategy_platform.options_archive import service
        handler = service.setup_logging(self.arch.paths["log"], self.arch.hidden)
        self.addCleanup(logging.getLogger("options_archive").removeHandler, handler)
        self.addCleanup(handler.close)
        self.arch.collector.alpaca_headers()                        # the key is known to the redaction filter
        real = self.arch.collector._chain
        calls = {"n": 0}

        def flaky(r, s, deadline):
            calls["n"] += 1
            if calls["n"] == 1:
                raise KeyError(f"greeks {KID}")                     # a key in the text must not reach the log
            return real(r, s, deadline)
        self.arch.collector._chain = flaky
        self.assertEqual(self.arch.serve(max_slots=2), 0)
        runs = self.rows("SELECT slot_label, status FROM os_runs WHERE kind = 'intraday' ORDER BY id")
        self.assertEqual([(r["slot_label"], r["status"]) for r in runs], [("09:55", "failed"), ("10:00", "ok")])
        self.assertEqual(self.rows("SELECT kind FROM os_gaps WHERE kind = 'crash'"), [{"kind": "crash"}])
        log = open(self.arch.paths["log"], encoding="utf-8").read()
        self.assertIn("Traceback", log)
        self.assertIn("KeyError", log)
        self.assertNotIn(KID, log)
        self.assertNotIn(KID, json.dumps(self.rows("SELECT * FROM os_runs")))

    def test_status_write_failure_never_raises(self):
        s = slot("15:25")
        self.make(s.start_utc(CFG))
        self.api.slot_ms = s.ms
        with mock.patch("strategy_platform.options_archive.report.os.replace", side_effect=PermissionError("locked")):
            self.assertFalse(report.write_status(self.arch.paths["status"], {"x": 1}, sleep=lambda _s: None))
            out = self.arch.run(s)
        self.assertEqual(out["status"], "ok")
        self.assertFalse(os.path.exists(self.arch.paths["status"]))

    def test_fatal_loop_error_exit_7_state_error(self):
        self.make(datetime(2026, 10, 26, 13, 0, tzinfo=UTC))
        from strategy_platform.options_archive import service
        handler = service.setup_logging(self.arch.paths["log"], self.arch.hidden)
        self.addCleanup(logging.getLogger("options_archive").removeHandler, handler)
        self.addCleanup(handler.close)
        self.arch.collector.alpaca_headers()
        with mock.patch.object(self.arch, "_sleep_until", side_effect=RuntimeError(f"boom {SKEY}")):
            self.assertEqual(self.arch.serve(max_slots=3), 7)
        st = json.load(open(self.arch.paths["status"]))
        self.assertEqual(st["state"], "error")
        self.assertIn("RuntimeError", st["last_error"])
        self.assertNotIn(SKEY, json.dumps(st))
        self.assertNotIn(SKEY, open(self.arch.paths["log"], encoding="utf-8").read())

    def test_heartbeat_during_long_run(self):
        s = slot("15:25")
        clock = Clock(s.start_utc(CFG))
        api = FakeApi(clock, D, page=2, latency=3.0)                 # many slow pages
        api.slot_ms = None
        self.make(clock.now(), api, cfg=Config(heartbeat_s=10))
        beats = []
        orig = self.arch.write_status
        self.arch.write_status = lambda state=None: beats.append(state) or orig(state)
        self.arch.run(s)
        self.assertGreater(beats.count(None), 3)                    # heartbeats between the pages


class WindowTest(ArchiveCase):
    def test_in_window_rules(self):
        self.make(datetime(2026, 10, 26, 12, 0, tzinfo=UTC))
        a, oi = self.arch, slot("oi")
        ny = lambda h, m, d=D: datetime(d.year, d.month, d.day, h, m, tzinfo=slots.NY)   # noqa: E731
        self.assertTrue(a.in_window(oi, ny(11, 0)))
        self.assertFalse(a.in_window(oi, ny(8, 0)))                  # before the slot
        self.assertFalse(a.in_window(oi, ny(16, 30)))                # after the close
        self.assertFalse(a.in_window(oi, ny(9, 0, D + timedelta(days=1))))   # the next day
        s = slot("15:25")
        self.assertTrue(a.in_window(s, ny(15, 24)))
        self.assertFalse(a.in_window(s, ny(15, 25) + timedelta(seconds=1)))
        self.assertFalse(a.in_window(s, ny(15, 20)))
        eod = slot("eod")
        self.assertTrue(a.in_window(eod, ny(16, 50)))
        self.assertFalse(a.in_window(eod, ny(17, 5)))

    def test_missed_morning_slots_after_sleep_are_missing(self):
        clock = Clock(datetime(2026, 10, 26, 12, 0, tzinfo=UTC))    # Mon 08:00 NY
        jumped = {"done": False}
        real_sleep = clock.sleep

        def asleep(sec):                                            # the PC sleeps until Tuesday 08:00
            if not jumped["done"]:
                jumped["done"] = True
                clock.advance(24 * 3600)
            real_sleep(sec)
        self.make(clock.now())
        self.clock = clock
        self.arch.sleep = asleep
        self.arch.now = clock.now
        self.assertEqual(self.arch.serve(max_slots=3), 0)           # Monday oi, fred, 09:45 → skipped
        self.assertEqual(self.rows("SELECT * FROM os_runs"), [])

    def test_run_slot_outside_window_is_manual(self):
        lines = []
        clock = Clock(datetime(2026, 10, 26, 14, 0, tzinfo=UTC))    # 10:00 NY — 15:25 is hours away
        api = FakeApi(clock, D)
        code = tool.main(["run-slot", "15:25"], data_dir=self.tmp, transport=api, secrets_module=Sec(), now=clock.now,
                         sleep=clock.sleep, out=lines.append)
        self.assertIn(code, (0, 1))
        conn = db.open_ro(os.path.join(self.tmp, "options_archive.db"))
        self.addCleanup(conn.close)
        r = [dict(x) for x in conn.execute("SELECT kind, slot_label FROM os_runs")]
        self.assertEqual(r, [{"kind": "manual", "slot_label": "manual:15:25"}])
        rep = report.day_report(conn, D, CFG, clock.now())
        self.assertEqual({x["label"]: x["status"] for x in rep["slots"]}["15:25"], "pending")    # never ranked


class CalendarEndTest(ArchiveCase):
    NOW = datetime(2030, 12, 31, 22, 0, tzinfo=UTC)                  # after the last known close: 2031 unknown (S10-H)

    def test_status_shows_calendar_end(self):
        p = report.status_payload(CFG, self.NOW, self.tmp)
        self.assertEqual((p["state"], p["next_slot"]), ("error", None))
        self.assertIn("calendar", p["last_error"].lower())

    def test_service_retries_hourly(self):
        self.make(self.NOW)
        states = []
        orig = self.arch.write_status

        def ws(state=None):
            states.append(state)
            if self.clock.now() - self.NOW >= timedelta(hours=2):
                open(self.arch.paths["stop"], "w").close()
            return orig(state)
        self.arch.write_status = ws
        self.assertEqual(self.arch.serve(max_slots=1), 0)
        self.assertGreaterEqual(states.count("error"), 2)
        self.assertGreaterEqual(self.clock.now() - self.NOW, timedelta(hours=2))
        st = json.load(open(self.arch.paths["status"]))
        self.assertIn("calendar", st["last_error"].lower())


class ManualAndSandboxCliTest(ArchiveCase):
    def cli(self, argv, start):
        clock = Clock(start)
        lines = []
        code = tool.main(argv, data_dir=self.tmp, transport=FakeApi(clock, start.date()), secrets_module=Sec(),
                         now=clock.now, sleep=clock.sleep, out=lines.append)
        return code, lines

    def test_run_once_writes_only_the_manual_block(self):
        status = os.path.join(self.tmp, "os_archive_status.json")
        with open(status, "w") as fh:
            json.dump({"state": "stopped", "heartbeat_at": "2026-10-25T10:00:00Z", "pid": 1}, fh)
        code, _ = self.cli(["run-once"], datetime(2026, 10, 26, 14, 0, tzinfo=UTC))
        st = json.load(open(status))
        self.assertEqual((st["state"], st["heartbeat_at"], st["pid"]), ("stopped", "2026-10-25T10:00:00Z", 1))
        self.assertTrue(st["manual"]["finished_at"].startswith("2026-10-26T14:00:"))
        self.assertEqual(len(st["manual"]["run_ids"]), 3)                  # oi + fred catch-up + one manual snapshot
        p = report.status_payload(CFG, datetime(2026, 10, 26, 14, 1, tzinfo=UTC), self.tmp)
        self.assertEqual((p["service"]["state"], p["service"]["alive"]), ("stopped", False))
        self.assertEqual(p["manual"]["status"], st["manual"]["status"])

    def test_sandbox_refuses_manual_runs(self):
        open(os.path.join(self.tmp, "SANDBOX_ACTIVE"), "w").close()
        for argv in (["run-once"], ["run-slot", "oi"]):
            code, lines = self.cli(argv, datetime(2026, 10, 26, 14, 0, tzinfo=UTC))
            self.assertEqual(code, 6)
            self.assertIn("sandbox", lines[0])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "options_archive.db")))

    def test_internal_error_exit_7(self):
        with mock.patch("strategy_platform.options_archive.service.Archive.run_once", side_effect=RuntimeError("x")):
            code, lines = self.cli(["run-once"], datetime(2026, 10, 26, 14, 0, tzinfo=UTC))
        self.assertEqual(code, 7)
        self.assertIn("internal error", lines[-1])


class SummaryAndGapsTest(ArchiveCase):
    def test_late_fixed_slot_is_a_gap(self):
        rep = {"date": "2026-10-26", "trading_day": True, "early_close": False,
               "slots": [{"status": "late", "fixed": True}, {"status": "late", "fixed": False},
                         {"status": "ok", "fixed": True}, {"status": "missing", "fixed": True}]}
        t = report.today_summary(rep)
        self.assertEqual((t["collected"], t["late"], t["fixed_slot_gaps"], t["missing"]), (2, 2, 2, 1))

    def test_gap_range_inclusive(self):
        now = datetime(2026, 11, 30, tzinfo=UTC)
        self.assertEqual(len(report.gaps(None, D, D + timedelta(days=30), CFG, now)["days"]), 31)
        with self.assertRaises(ValueError):
            report.gaps(None, D, D + timedelta(days=31), CFG, now)


class ApiRound2Test(TradingCase):
    def setUp(self):
        super().setUp()
        from strategy_platform.api import routes
        self.api = routes.V2Api()
        self.api.data_dir = os.path.join(self.tmp, "oa")
        os.makedirs(self.api.data_dir, exist_ok=True)
        self.api.now_fn = lambda: datetime(2026, 10, 26, 14, 30, tzinfo=UTC)
        venv = os.path.join(self.tmp, "venv")
        os.makedirs(venv)
        for f in ("python.exe", "pythonw.exe"):
            open(os.path.join(venv, f), "w").close()
        self.api.archive_python = os.path.join(venv, "python.exe")
        self.api.archive_sandbox = True

    def call(self, name, query=None, body=None):
        from strategy_platform.api import routes
        return asyncio.run(getattr(self.api, name)(query or {}, routes.NO_BODY if body is None else body, {}))

    def test_sandbox(self):
        st, p = self.call("options_archive_status")
        self.assertEqual((st, p["sandbox"], p["actions_enabled"]), (200, True, False))
        self.assertIn("sandbox", p["actions_note"].lower())
        st, p = self.call("options_archive_registration")
        self.assertEqual((p["available"], p["sandbox"]), (False, True))
        for action in ("registered", "register_failed", "run_once", "run_once_failed"):
            st, p = self.call("options_archive_audit", body={"action": action})
            self.assertEqual((st, p["code"]), (409, "sandbox"), action)
        self.assertEqual(self.call("options_archive_audit", body={"action": "unregistered"})[0], 200)

    def test_gaps_bounds(self):
        ok = self.call("options_archive_gaps", {"from": "2026-10-01", "to": "2026-10-31"})
        self.assertEqual((ok[0], len(ok[1]["days"])), (200, 31))
        self.assertEqual(self.call("options_archive_gaps", {"from": "2026-10-01", "to": "2026-11-01"})[0], 400)
        for bad in ("9999-12-31", "0001-01-01", "2026-02-30"):
            self.assertEqual(self.call("options_archive_gaps", {"date": bad})[0], 400, bad)
        st, p = self.call("options_archive_gaps", {"date": "2031-03-03"})       # beyond the calendar: shown, not 500
        self.assertEqual(st, 200)
        self.assertIn("error", p["days"][0])


if __name__ == "__main__":
    import unittest
    unittest.main()
