"""OS1 archive process and its read-only backend view (SP §21.4 / §21.11): the single-instance lock, the service loop
on a fake clock (catch-up of today's OI / FRED, slots in order, missed chain slots NOT collected later and shown as
missing, the stop file, the status file), an early-close day without 15:25, the US DST change in the loop, the CLI
(status / gaps / run-slot / run-once / lock held → 5), and the /v2/options-archive/* routes (not set up, status after
runs, gaps, runs, registration DATA only (no script text), audit rows)."""
import asyncio
import json
import os
from datetime import datetime, timezone

try:
    from os_archive_testkit import FKEY, KID, SKEY, ArchiveCase, Clock, FakeApi, Sec
    from sp_trading_testkit import TradingCase
except ImportError:  # pragma: no cover
    from tests.os_archive_testkit import FKEY, KID, SKEY, ArchiveCase, Clock, FakeApi, Sec
    from tests.sp_trading_testkit import TradingCase

from strategy_platform.options_archive import report, slots  # noqa: E402
from strategy_platform.options_archive.config import Config  # noqa: E402
from strategy_platform.options_archive.service import InstanceLock  # noqa: E402

import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location(
    "options_archive_tool", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools",
                                         "options_archive.py"))
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)

UTC = timezone.utc


class ServiceTest(ArchiveCase):
    def test_loop_catch_up_order_and_missing(self):
        start = datetime(2026, 10, 26, 13, 46, tzinfo=UTC)          # 09:46 New York — the 09:45 slot is gone
        self.make(start)
        self.api.slot_ms = None
        self.assertEqual(self.arch.serve(max_slots=3), 0)
        runs = self.rows("SELECT kind, slot_label, status FROM os_runs ORDER BY id")
        self.assertEqual([(r["kind"], r["slot_label"]) for r in runs],
                         [("oi", "oi"), ("fred", "fred"), ("intraday", "09:55"), ("intraday", "10:00"),
                          ("intraday", "10:15")])
        rep = report.day_report(self.arch.conn, start.date(), Config(), self.clock.now())
        st = {s["label"]: s["status"] for s in rep["slots"]}
        self.assertEqual(st["09:45"], "missing")                    # never collected later, shown as a gap
        self.assertIn("09:45", [g["slot_ny"] for g in rep["gap_rows"] if g["kind"] == "not_collected"])
        status = json.load(open(self.arch.paths["status"]))
        self.assertEqual(status["state"], "stopped")
        self.assertEqual(status["today"]["missing"], 1)

    def test_stop_file(self):
        self.make(datetime(2026, 10, 24, 12, 0, tzinfo=UTC))       # Saturday: the loop would sleep until Monday
        open(self.arch.paths["stop"], "w").close()
        self.assertEqual(self.arch.serve(max_slots=5), 0)
        self.assertEqual(self.rows("SELECT * FROM os_runs"), [])

    def test_early_close_day(self):
        start = datetime(2026, 11, 27, 17, 40, tzinfo=UTC)          # 12:40 New York, 13:00 close
        self.make(start)
        self.arch.serve(max_slots=2)
        labels = [r["slot_label"] for r in self.rows("SELECT slot_label FROM os_runs ORDER BY id")]
        self.assertEqual(labels, ["oi", "fred", "12:45", "eod"])
        eod = self.rows("SELECT slot_ms FROM os_runs WHERE kind = 'eod'")[0]["slot_ms"]
        self.assertEqual(datetime.fromtimestamp(eod / 1000, slots.NY).strftime("%H:%M"), "13:15")

    def test_us_dst_change_in_the_loop(self):
        start = datetime(2026, 10, 30, 20, 0, tzinfo=UTC)           # Fri 16:00 EDT
        self.make(start)
        self.arch.serve(max_slots=2)
        r = self.rows("SELECT kind, slot_ms FROM os_runs ORDER BY id")
        self.assertEqual([x["kind"] for x in r], ["eod", "oi"])
        self.assertEqual(datetime.fromtimestamp(r[0]["slot_ms"] / 1000, UTC).strftime("%m-%d %H:%M"), "10-30 20:15")
        self.assertEqual(datetime.fromtimestamp(r[1]["slot_ms"] / 1000, UTC).strftime("%m-%d %H:%M"), "11-02 13:30")

    def test_single_instance_lock(self):
        a, b = InstanceLock(os.path.join(self.tmp, "x.lock")), InstanceLock(os.path.join(self.tmp, "x.lock"))
        self.assertTrue(a.acquire())
        self.assertFalse(b.acquire())
        a.release()
        self.assertTrue(b.acquire())
        b.release()


class CliTest(ArchiveCase):
    def run_cli(self, argv, start, api=None):
        clock = Clock(start)
        api = api or FakeApi(clock, start.date())
        lines = []
        code = tool.main(argv, data_dir=self.tmp, transport=api, secrets_module=Sec(), now=clock.now,
                         sleep=clock.sleep, out=lines.append)
        return code, lines

    def test_status_not_set_up_then_runs(self):
        code, lines = self.run_cli(["status"], datetime(2026, 10, 26, 12, 0, tzinfo=UTC))
        st = json.loads("\n".join(lines))
        self.assertEqual((code, st["state"], st["db"]["exists"]), (0, "not_set_up", False))
        code, lines = self.run_cli(["run-slot", "oi"], datetime(2026, 10, 26, 12, 30, tzinfo=UTC))
        self.assertIn(code, (0, 1))
        self.assertTrue(lines[0].split()[0] in ("OK", "PARTIAL"))
        code, lines = self.run_cli(["run-once"], datetime(2026, 10, 26, 14, 0, tzinfo=UTC))
        self.assertEqual(len(lines), 2)                             # FRED catch-up + one manual snapshot
        code, lines = self.run_cli(["gaps", "--date", "2026-10-26"], datetime(2026, 10, 26, 14, 1, tzinfo=UTC))
        rep = json.loads("\n".join(lines))
        self.assertEqual(rep["slots"][0]["label"], "oi")
        self.assertNotIn(KID, open(os.path.join(self.tmp, "logs", "options_archive.log")).read())

    def test_lock_held_and_usage(self):
        lock = InstanceLock(os.path.join(self.tmp, "options_archive.lock"))
        self.assertTrue(lock.acquire())
        self.addCleanup(lock.release)
        code, lines = self.run_cli(["run-once"], datetime(2026, 10, 26, 14, 0, tzinfo=UTC))
        self.assertEqual(code, 5)
        self.assertIn("lock held", lines[0])
        self.assertEqual(self.run_cli(["trade"], datetime(2026, 10, 26, 14, 0, tzinfo=UTC))[0], 1)

    def test_keys_missing_exit_2(self):
        clock = Clock(datetime(2026, 10, 26, 14, 0, tzinfo=UTC))
        lines = []
        code = tool.main(["run-slot", "10:00"], data_dir=self.tmp, transport=FakeApi(clock, clock.now().date()),
                         secrets_module=Sec({}), now=clock.now, sleep=clock.sleep, out=lines.append)
        self.assertEqual(code, 2)


class ApiTest(TradingCase):
    def setUp(self):
        super().setUp()
        from strategy_platform.api import routes
        self.api = routes.V2Api()
        self.api.data_dir = os.path.join(self.tmp, "oa")
        os.makedirs(self.api.data_dir, exist_ok=True)
        self.now = datetime(2026, 10, 26, 14, 30, tzinfo=UTC)
        self.api.now_fn = lambda: self.now
        self.venv = os.path.join(self.tmp, "venv", "Scripts")
        os.makedirs(self.venv)
        for f in ("python.exe", "pythonw.exe"):
            open(os.path.join(self.venv, f), "w").close()
        self.api.archive_python = os.path.join(self.venv, "python.exe")
        self.api.archive_sandbox = False

    def call(self, name, query=None, body=None):
        from strategy_platform.api import routes
        status, payload = asyncio.run(getattr(self.api, name)(query or {}, routes.NO_BODY if body is None else body, {}))
        routes.dumps(payload)
        return status, payload

    def test_routes(self):
        st, p = self.call("options_archive_status")
        self.assertEqual((st, p["state"], p["db"]["exists"], p["service"]["status_file"]), (200, "not_set_up", False, False))
        self.assertEqual(p["config"]["rate_per_min"], 30)
        self.assertEqual(p["next_slot"]["label"], "10:45")              # 14:30Z = 10:30 New York
        from strategy_platform.options_archive.service import Archive
        clock = Clock(datetime(2026, 10, 26, 12, 30, tzinfo=UTC))
        arch = Archive(Config(), self.api.data_dir, transport=FakeApi(clock, clock.now().date()), secrets_module=Sec(),
                       now=clock.now, sleep=clock.sleep, monotonic=clock.monotonic)
        arch.run(slots.find(clock.now().date(), "oi", Config()))
        arch.write_status()
        arch.close()
        st, p = self.call("options_archive_status")
        self.assertEqual((p["db"]["exists"], p["db"]["schema_version"], p["counts"]["oi_rows"]), (True, 1, 104))
        self.assertEqual(p["last_run"]["kind"], "oi")
        self.assertEqual(p["today"]["missing"], 6)        # 10:30 NY: FRED + 09:45, 09:55, 10:00, 10:15, 10:30 not run
        self.assertEqual(p["state"], "degraded")
        st, p = self.call("options_archive_gaps", {"date": "2026-10-26"})
        self.assertEqual(st, 200)
        self.assertEqual(p["days"][0]["slots"][1]["status"], "missing")
        self.assertEqual(self.call("options_archive_gaps", {"from": "2026-10-01", "to": "2026-11-15"})[0], 400)
        self.assertEqual(self.call("options_archive_gaps", {"date": "x"})[0], 400)
        st, p = self.call("options_archive_runs", {"limit": "5"})
        self.assertEqual((st, len(p["runs"])), (200, 1))
        self.assertEqual(self.call("options_archive_runs", {"limit": "0"})[0], 400)

    def test_registration_data_only(self):
        st, p = self.call("options_archive_registration")
        self.assertEqual(st, 200)
        self.assertNotIn("commands", p)                                 # review M1: no script text at all
        self.assertNotIn("Register-ScheduledTask", json.dumps(p))
        self.assertEqual(p["task_name"], "UFOS.ai OptionsArchive")              # root task folder (Round 2b)
        for k in ("backend_dir", "workdir", "pythonw", "python", "tool", "service_args", "run_once_args", "available",
                  "unavailable_reason", "sandbox", "summary"):
            self.assertIn(k, p)
        self.assertEqual((p["available"], p["sandbox"], p["service_args"], p["run_once_args"]),
                         (True, False, ["service"], ["run-once"]))
        self.assertTrue(p["tool"].endswith(os.path.join("tools", "options_archive.py")))
        self.assertEqual(p["pythonw"], os.path.join(self.venv, "pythonw.exe"))
        self.assertEqual(set(p["summary"]), {"what", "when", "account", "keys", "data", "stop"})
        self.assertIn("no trading", p["summary"]["what"])
        self.assertIn("right after you confirm", p["summary"]["when"])         # review M4: consent truth
        self.assertIn("every Windows logon", p["summary"]["when"])
        self.assertIn("ends the running archive now", p["summary"]["stop"])
        os.remove(os.path.join(self.venv, "pythonw.exe"))
        st, p = self.call("options_archive_registration")
        self.assertEqual((p["available"], p["pythonw"]), (False, None))
        self.assertIn("pythonw.exe", p["unavailable_reason"])

    def test_audit(self):
        from strategy_platform.storage import db
        st, p = self.call("options_archive_audit", body={"action": "registered", "detail": "task created"})
        self.assertEqual((st, p["ok"]), (200, True))
        with db._connect() as conn:
            row = conn.execute("SELECT action, detail_json FROM audit WHERE id = ?", (p["audit_id"],)).fetchone()
        self.assertEqual(row["action"], "options_archive_registered")
        self.assertEqual(self.call("options_archive_audit", body={"action": "delete_everything"})[0], 400)
        self.assertEqual(self.call("options_archive_audit", body={"action": "run_once", "detail": "x" * 301})[0], 400)
        self.assertEqual(self.call("options_archive_audit", body={"action": "run_once", "x": 1})[0], 400)


if __name__ == "__main__":
    import unittest
    unittest.main()
