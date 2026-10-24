"""Stage 10 S10-H backend hardening: the backend-venv guard of every tool, the NYSE calendar through 2030, the committee
status race (was the flaky test_sp_committee), the market_research.db startup race, incubation.realised counting
the first session, and the context notices (= Shared/AiContent.cs). Offline, temp DBs."""
import math
import os
import re
import subprocess
import sys
import threading
import time
import unittest
from datetime import date

try:
    from sp_monitor_testkit import MonCase, write_returns
except ImportError:  # pragma: no cover
    from tests.sp_monitor_testkit import MonCase, write_returns

from strategy_platform.core import calendars as C
from strategy_platform.core import notices, venv_guard

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = {"sandbox.py": 2, "options_archive.py": 3, "verify_alpaca_options.py": 3, "verify_binance_demo.py": 5,
         "make_lockfile.py": 2}          # make_lockfile: stdlib only, its own check (byte-identical in 3 backends)


class VenvGuardTest(unittest.TestCase):
    def test_problem_and_hint(self):
        self.assertIsNone(venv_guard.problem("x.py", BACKEND, os.path.join(BACKEND, ".venv")))
        msg = venv_guard.problem("options_archive.py", BACKEND, "/usr")
        self.assertIn(r"run with .venv\Scripts\python.exe tools\options_archive.py", msg)

    def test_every_tool_guards_before_any_non_stdlib_import(self):
        names = sorted(f for f in os.listdir(os.path.join(BACKEND, "tools")) if f.endswith(".py"))
        self.assertEqual(set(names), set(TOOLS), "a new tool needs the venv guard (and an entry here)")
        for name in names:
            with open(os.path.join(BACKEND, "tools", name), encoding="utf-8") as f:
                src = f.read()
            if name == "sandbox.py":
                self.assertIn("venv_problem()", src.split('if __name__ == "__main__":')[-1])
                continue
            if name == "make_lockfile.py":
                self.assertIn("venv_problem(base)", src)
                self.assertFalse(re.search(r"^(from|import) (strategy_platform|fund|market_research|newsroom|config)",
                                           src, re.M))
                continue
            g = src.index(f'venv_guard.require("{name}", BACKEND_DIR, {TOOLS[name]})')
            hits = [m.start() for m in re.finditer(r"^(from|import) (strategy_platform|sandbox_profile|fund|"
                                                   r"market_research|newsroom|config)", src, re.M)
                    if "venv_guard" not in src[m.start():m.start() + 80]]
            first = min(hits) if hits else len(src)
            self.assertLess(g, first, name)

    def test_tools_refuse_the_system_python(self):
        if venv_guard.problem("x.py", BACKEND) is None:
            self.skipTest("running inside the backend venv")
        for name, code in TOOLS.items():
            if name == "make_lockfile.py":
                p = subprocess.run([sys.executable, os.path.join(BACKEND, "tools", name)], capture_output=True,
                                   text=True, timeout=60, cwd=BACKEND)
                self.assertEqual(p.returncode, 2, p.stderr[-300:])
                self.assertIn("REFUSED: This is not the backend's Python", p.stderr)
                continue
            p = subprocess.run([sys.executable, os.path.join(BACKEND, "tools", name), "status"], capture_output=True,
                               text=True, timeout=60, cwd=BACKEND)
            self.assertEqual(p.returncode, code, (name, p.stderr[-300:]))
            self.assertIn("REFUSED: This is not the backend's Python", p.stderr)
            self.assertIn(f"tools\\{name}", p.stderr)


class CalendarThrough2030Test(unittest.TestCase):
    EXPECTED = {
        2028: (["2028-01-17", "2028-02-21", "2028-04-14", "2028-05-29", "2028-06-19", "2028-07-04", "2028-09-04",
                "2028-11-23", "2028-12-25"], ["2028-07-03", "2028-11-24"]),
        2029: (["2029-01-01", "2029-01-15", "2029-02-19", "2029-03-30", "2029-05-28", "2029-06-19", "2029-07-04",
                "2029-09-03", "2029-11-22", "2029-12-25"], ["2029-07-03", "2029-11-23", "2029-12-24"]),
        2030: (["2030-01-01", "2030-01-21", "2030-02-18", "2030-04-19", "2030-05-27", "2030-06-19", "2030-07-04",
                "2030-09-02", "2030-11-28", "2030-12-25"], ["2030-07-03", "2030-11-29", "2030-12-24"]),
    }

    def test_each_year(self):
        self.assertEqual((C.CALENDAR_YEARS[0], C.CALENDAR_YEARS[-1]), (2000, 2030))
        for y, (hol, early) in self.EXPECTED.items():
            self.assertEqual(sorted(d.isoformat() for d in C.NYSE_HOLIDAYS if d.year == y), hol, y)
            self.assertEqual(sorted(d.isoformat() for d in C.NYSE_EARLY_CLOSES if d.year == y), early, y)
            n = sum(1 for o in range(date(y, 1, 1).toordinal(), date(y, 12, 31).toordinal() + 1)
                    if C.is_trading_day(date.fromordinal(o)))
            self.assertEqual(n, 251, y)
        self.assertTrue(C.is_trading_day("2027-12-31"))       # Jan 1 2028 a Saturday: no closure
        self.assertTrue(C.is_early_close("2028-07-03"))
        self.assertEqual(C.session_bounds("2029-12-24")[1].hour, 18)      # 13:00 New York = 18:00 UTC (EST)
        with self.assertRaises(C.CalendarUnavailable):
            C.is_trading_day("2031-01-02")

    def test_the_rules_reproduce_the_published_2022_2027_rows(self):
        for y in range(2022, 2028):
            hard = {d for d in C.NYSE_HOLIDAYS if d.year == y} - {date(2025, 1, 9)}      # Carter: day of mourning
            self.assertEqual(set(C.modern_holidays(y)), hard, y)
            self.assertEqual(set(C.modern_early_closes(y)), {d for d in C.NYSE_EARLY_CLOSES if d.year == y}, y)


class CommitteeStatusRaceTest(unittest.TestCase):
    def test_a_run_finishing_between_the_reads_is_never_interrupted(self):
        from strategy_platform.research import committee
        rid = "cr-race0000001"
        committee._active[rid] = threading.Event()
        real = committee._events

        def events_after_the_worker_finished(run_id):
            committee._active.pop(run_id, None)          # the worker commits "done" and leaves _active meanwhile
            return []                                   # … but this read saw the events before the commit
        committee._events = events_after_the_worker_finished
        orig_select = committee.db.s5b_select
        committee.db.s5b_select = lambda *a, **k: []
        try:
            out = committee.run_out({"id": rid, "strategy_id": "s-1", "version": 1, "validation_run_id": None,
                                     "model_tier": "standard", "model": None, "trigger": "manual",
                                     "created_at": "2026-10-24T00:00:00+00:00"})
        finally:
            committee._events = real
            committee.db.s5b_select = orig_select
            committee._active.pop(rid, None)
        self.assertEqual(out["status"], "running")      # the next poll sees "done" — never a false "interrupted"


class ResearchStoreStartupRaceTest(unittest.TestCase):
    def test_status_and_operations_during_the_first_open(self):
        import tempfile
        import shutil
        from market_research import store
        tmp = tempfile.mkdtemp(prefix="s10-mr-")
        self.addCleanup(shutil.rmtree, tmp, True)
        store.close()
        self.addCleanup(store.close)
        real = store._open_and_migrate
        entered = threading.Event()

        def slow(target):
            entered.set()
            time.sleep(0.5)
            return real(target)
        store._open_and_migrate = slow
        self.addCleanup(setattr, store, "_open_and_migrate", real)
        t = threading.Thread(target=store.init, args=(os.path.join(tmp, "market_research.db"),))
        t.start()
        self.assertTrue(entered.wait(5))
        self.assertEqual(store.status()["state"], "not_opened")        # "starting", not "unavailable"
        out = store.prune()                                             # waits for the open instead of failing
        self.assertIsInstance(out, dict)
        t.join(10)
        self.assertEqual(store.status()["state"], "ok")


class RealisedFirstSessionTest(MonCase):
    def test_first_session_counts(self):
        from strategy_platform.live import incubation
        sid, run = self.start_run(length=20)
        rets = [0.02, -0.01, 0.005]
        write_returns(run, rets)
        out, lr = incubation.realised(run)
        self.assertEqual(len(lr), 3)                                    # the first session's own return included
        self.assertAlmostEqual(out["log_return"], math.fsum(math.log(1 + r) for r in rets), places=9)


class NoticesTest(unittest.TestCase):
    def test_payload_notices_are_the_aicontent_texts(self):
        from strategy_platform.discovery import loop
        from strategy_platform.execution.brokers import service
        from strategy_platform.monitoring import service as mon
        from strategy_platform.portfolio import views
        from strategy_platform.research import studio
        for n in (views.NOTICE, service.NOTICE, mon.NOTICE):
            self.assertEqual(n, notices.PAPER_EXECUTION)
        self.assertIn(notices.RESEARCH, studio.AI_INTERACTION_NOTICE)
        self.assertIn(notices.RESEARCH, loop.AI_INTERACTION_NOTICE)
        ai = os.path.join(BACKEND, "..", "..", "Shared", "AiContent.cs")
        if not os.path.exists(ai):
            self.skipTest("Shared/AiContent.cs not in this checkout")
        with open(ai, encoding="utf-8") as f:
            cs = f.read()
        joined = "".join(m.group(1) for m in re.finditer(r'"((?:[^"\\]|\\.)*)"', cs))   # C# literals, concatenated
        for text in (notices.ANALYSIS, notices.RESEARCH, notices.PAPER_EXECUTION):
            self.assertIn(text, joined)

    def test_no_blanket_not_investment_advice_in_user_facing_platform_texts(self):
        bad = []
        for root, _d, files in os.walk(os.path.join(BACKEND, "strategy_platform")):
            for f in files:
                if not f.endswith(".py"):
                    continue
                with open(os.path.join(root, f), encoding="utf-8") as fh:
                    src = fh.read()
                for i, line in enumerate(src.splitlines(), 1):
                    s = line.strip()
                    if "not investment advice" in s and (s.startswith(("NOTICE", "f\"", "\"", "+ \"")) or
                                                          "add_event" in s or "emit(" in s):
                        bad.append(f"{f}:{i}")
                # a phrase split across two adjacent string literals ("… not investment " "advice.")
                for m in re.finditer(r'not investment\s*"\s*\n\s*f?"\s*advice', src):
                    bad.append(f"{f}:{src.count(chr(10), 0, m.start()) + 1} (split)")
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
