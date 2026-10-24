"""SPEC §10.2 (R2): parallel compute of validation runs — the pool plan
(cpu − 1, memory guard ≤ 2 GB), the worker isolation guard (no secrets, no
network, no database, no credential env vars; spawn does not re-run the
parent's main script), cancel terminates the workers, and the report of a
pool run is IDENTICAL to the sequential one (real engine, synthetic universe,
fake loader). Offline; temp dirs only.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_parallel -v
"""
import json
import multiprocessing
import os
import pickle
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import date, timedelta

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402

from strategy_platform.backtest import engine, metrics, stress  # noqa: E402
from strategy_platform.backtest.engine import BacktestCancelled  # noqa: E402
from strategy_platform.core import calendars  # noqa: E402
from strategy_platform.core.clock import FixedClock  # noqa: E402
from strategy_platform.core.errors import DataSourceUnavailable, SecretMissing  # noqa: E402
from strategy_platform.data.providers import tiingo  # noqa: E402
from strategy_platform.validation import families, gates, parallel  # noqa: E402
from strategy_platform.validation import jobs as vjobs  # noqa: E402
import test_sp_backtest_support as bsup  # noqa: E402
import test_sp_validation_support as sup  # noqa: E402

SYMS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
_DAYS = calendars.trading_days(date(2023, 6, 1), date(2024, 10, 31))


def _prices(syms=SYMS, days=_DAYS, seed=5):
    rng = np.random.default_rng(seed)
    out = {}
    for k, s in enumerate(list(syms) + ["SPY"]):
        r = rng.normal(0.0003 + 0.0001 * k, 0.012, len(days))
        c = 40.0 * (1 + k) * np.cumprod(1 + r)
        out[s] = {d: (float(c[i] * 0.999), float(c[i])) for i, d in enumerate(days)}
    return out


PRICES = _prices()
# stress equivalence: data that covers the 2022 episodes (rate shock, crypto 2022) — 3 symbols, short
LONG_SYMS = ["AAA", "BBB", "CCC"]
PRICES_LONG = _prices(LONG_SYMS, calendars.trading_days(date(2021, 8, 2), date(2022, 12, 30)), seed=9)
_SEQ = {}


def seq_run(mult=2.0):
    if mult not in _SEQ:
        _SEQ[mult] = run(mult=mult)
    return _SEQ[mult]


def spec():
    return {"schema": "ufos.strategy/1", "name": "parallel test", "universe": {"symbols": list(SYMS)},
            "parameters": {"lookback": {"value": 10, "min": 5, "max": 20, "step": 5, "integer": True}},
            "signal": {"op": "cs_rank", "args": [{"op": "pct_change", "args": [{"field": "close"},
                                                                                {"param": "lookback"}]}]},
            "portfolio": {"mode": "long_short", "selection": {"type": "top_bottom_n", "n": 2}, "weighting": "equal",
                          "max_weight": 1.0, "gross_exposure": 1.0},
            "rebalance": {"frequency": "weekly"}}


def run(runner=None, data=None, mult=2.0):
    th = dict(gates.effective("standard"), g6_cost_multiplier=mult)
    return vjobs.run_validation(
        spec(), run_engine=engine.run,
        compute_metrics=lambda r: metrics.compute(r, analysis=getattr(r, "analysis", None)),
        run_stress=stress.run_stress, synthetic_costs=stress.synthetic_costs,
        training_end=families.training_end(spec()), data=data if data is not None else bsup.FakeLoader(PRICES),
        thresholds=th, settings={"preset": "standard", "overrides": {}}, n_trials_before=0, trial_sharpes_before=[],
        runner=runner)


def norm(obj):
    return json.loads(parallel.canonical_json(obj))


class ClockCase(unittest.TestCase):
    def setUp(self):
        families.set_clock(FixedClock(sup.NOW))
        self.addCleanup(families.set_clock, None)


class PlanTest(unittest.TestCase):
    def test_cpu_minus_one_memory_guard_and_tasks(self):
        p = parallel.plan_workers(10, 1000, cpu_count=8)
        self.assertEqual((p["cpu_workers"], p["workers"]), (7, 7))
        self.assertLessEqual(p["workers"] * p["per_worker_bytes"], parallel.MEMORY_LIMIT_BYTES)
        self.assertEqual(parallel.plan_workers(10, 2000, cpu_count=1)["workers"], 1)
        self.assertEqual(parallel.plan_workers(10, 2000, 3, cpu_count=16)["workers"], 3)
        big = parallel.plan_workers(100, 6500, cpu_count=16)                 # 2000-history, 100 symbols
        self.assertEqual(big["workers"], 1)                                  # estimate > 2 GB: inline only
        mid = parallel.plan_workers(60, 2700, cpu_count=16)
        self.assertTrue(1 <= mid["workers"] < 15)
        self.assertLessEqual(mid["workers"] * mid["per_worker_bytes"], parallel.MEMORY_LIMIT_BYTES)

    def test_freeze_thaw_keeps_class_and_message(self):
        for ex in (SecretMissing("tiingo-api-key", "Tiingo API key"), tiingo.UnknownSymbol("ZZZ"),
                   families.HoldoutWindowViolation(date(2024, 10, 8), "end"),
                   DataSourceUnavailable("tiingo", "equity_bars_history", "HTTP 500")):
            back = parallel.thaw_exception(pickle.loads(pickle.dumps(parallel.freeze_exception(ex))))
            self.assertIs(type(back), type(ex))
            self.assertEqual(back.user_message, ex.user_message)
        self.assertEqual(back.reason, "HTTP 500")

    def test_ext_names_and_replay_needs_prefetch(self):
        s = dict(spec(), signal={"op": "add", "args": [{"ext": "macro_regime"}, {"field": "close"}]})
        self.assertEqual(parallel.ext_names(s), ["macro_regime"])
        rl = parallel.ReplayLoader({})
        with self.assertRaises(parallel.NeedsPrefetch) as cm:
            rl.history("AAA", "all", date(2024, 1, 2), date(2024, 3, 1))
        self.assertEqual(cm.exception.keys, [("history", "AAA", "all", "2024-01-02", "2024-03-01")])


class ClassificationTest(unittest.TestCase):
    def test_context_is_not_a_connection_error_but_cause_is(self):
        from strategy_platform.data import network
        conn = network.mark_connection(DataSourceUnavailable("alpaca", "equity_bars_history", "timed out"))
        try:
            try:
                raise conn
            except DataSourceUnavailable:
                raise KeyError("bug while handling the timeout")
        except KeyError as ex:
            self.assertIsNotNone(ex.__context__)
            self.assertFalse(parallel.is_connection_error(ex))
        try:
            raise RuntimeError("wrapped") from conn
        except RuntimeError as ex:
            self.assertTrue(parallel.is_connection_error(ex))
        self.assertTrue(parallel.is_connection_error(parallel.ConnectionLost(conn)))

    def test_data_hash_ignores_fetch_and_known_timestamps(self):
        a = [{"ts": "2024-01-02", "close": 1.0, "known_at": "2026-10-09T10:00:00", "fetched_at": "x"}]
        b = [{"ts": "2024-01-02", "close": 1.0, "known_at": "2026-10-10T10:00:00", "fetched_at": "y"}]
        c = [{"ts": "2024-01-02", "close": 1.01, "known_at": "2026-10-09T10:00:00"}]
        self.assertEqual(parallel.rows_sha256(a), parallel.rows_sha256(b))
        self.assertNotEqual(parallel.rows_sha256(a), parallel.rows_sha256(c))

    def test_forbidden_paths(self):
        for p in (".env", "C:\\x\\HedgeFund\\backend\\data\\hedgefund.db", "/a/b.db-wal", "/a/data/api-token",
                  "C:\\Users\\u\\AppData\\Roaming\\Microsoft\\Credentials\\ABC"):
            self.assertTrue(parallel._forbidden_path(p), p)
        for p in ("/usr/lib/python3/zoneinfo/x.py", "C:\\venv\\Lib\\site-packages\\numpy\\core.pyd", 5):
            self.assertFalse(parallel._forbidden_path(p), p)


class ExtPrefetchTest(unittest.TestCase):
    def test_ext_values_prefetched_in_the_parent_and_replayed(self):
        import types
        from strategy_platform.strategy import evaluate as ev
        from strategy_platform.strategy import features
        calls = []

        def load(start, end):
            calls.append((start, end))
            return {"2024-01-03": 1.0, "2024-01-04": 2.0}
        features.register(features.ExtFeature("r2_test_ext", "test", load), replace=True)
        self.addCleanup(features.unregister, "r2_test_ext")
        s = {"signal": {"op": "add", "args": [{"ext": "r2_test_ext"}, {"field": "close"}]}}
        panel = types.SimpleNamespace(dates=[date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)],
                                      symbols=["AAA", "BBB"])
        key = parallel.ext_key("r2_test_ext", panel)
        with self.assertRaises(parallel.NeedsPrefetch) as cm:
            parallel.ReplayLoader({}).ext_values(s, panel)
        self.assertEqual(cm.exception.keys, [key])
        rec = parallel.RecordingLoader(bsup.FakeLoader({}), keep=True)
        rec.fetch(key)
        got = parallel.ReplayLoader(rec.snapshot([key])).ext_values(s, panel)
        self.assertEqual(got, {"r2_test_ext": ev.load_ext("r2_test_ext", panel)})
        self.assertEqual(calls[0], (date(2024, 1, 2) - timedelta(days=ev.EXT_HISTORY_DAYS), date(2024, 1, 4)))


class IsolationTest(unittest.TestCase):
    def test_worker_guard_blocks_secrets_network_db_and_env(self):
        os.environ["TIINGO_API_KEY_TEST_R2"] = "must-not-reach-workers"
        self.addCleanup(os.environ.pop, "TIINGO_API_KEY_TEST_R2", None)
        tmp = tempfile.mkdtemp(prefix="sp-par-iso-")
        self.addCleanup(shutil.rmtree, tmp, True)
        blob = pickle.dumps({"snapshot": {}})
        out = parallel._run_pool(1, blob, [(0, "isolation_check", {"db_path": os.path.join(tmp, "x.db"),
                                                                   "tmp_dir": tmp})],
                                 cancel=None, on_result=None)
        rep = out[0][1]
        self.assertEqual(out[0][0], "ok", rep)
        self.assertTrue(rep["guard_active"])
        for k in ("secrets", "keyring", "socket", "dns", "sqlite", "storage", "http", "raw_socket", "udp_sendmsg",
                  "raw_sqlite", "sqlite_connection", "ctypes", "asyncio_connect", "open_env", "open_db",
                  "open_token", "subprocess"):
            self.assertTrue(str(rep[k]).startswith("blocked"), f"{k}: {rep[k]}")
        self.assertTrue(rep["audit_hook"])
        self.assertEqual(rep["secret_env"], [])
        self.assertEqual(os.listdir(tmp), [])                    # no database file was created
        self.assertEqual(multiprocessing.active_children(), [])
        self.assertFalse(parallel.guard_active())                # the parent is not guarded

    def test_spawn_does_not_rerun_the_main_script(self):
        tmp = tempfile.mkdtemp(prefix="sp-par-main-")
        self.addCleanup(shutil.rmtree, tmp, True)
        marker = os.path.join(tmp, "main_ran_in_child")
        script = os.path.join(tmp, "fake_main.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(
                "import os, sys, pickle, json\n"
                f"sys.path.insert(0, {BACKEND!r})\n"
                "if __name__ != '__main__':\n"
                f"    open({marker!r}, 'w').write(__name__)\n"
                "from strategy_platform.validation import parallel\n"
                "if __name__ == '__main__':\n"
                "    out = parallel._run_pool(2, pickle.dumps({'snapshot': {}}), "
                "[(0, 'isolation_check', {}), (1, 'isolation_check', {})], cancel=None, on_result=None)\n"
                "    print(json.dumps({k: v[0] for k, v in out.items()}))\n")
        res = subprocess.run([sys.executable, script], cwd=tmp, capture_output=True, text=True, timeout=120)
        self.assertEqual(res.returncode, 0, res.stderr[-2000:])
        self.assertEqual(json.loads(res.stdout.strip().splitlines()[-1]), {"0": "ok", "1": "ok"})
        self.assertFalse(os.path.exists(marker), "the main script ran again in a worker")

    def test_workers_end_when_the_parent_dies(self):
        tmp = tempfile.mkdtemp(prefix="sp-par-orphan-")
        self.addCleanup(shutil.rmtree, tmp, True)
        script = os.path.join(tmp, "parent.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(
                "import os, sys, pickle, threading, time\n"
                f"sys.path.insert(0, {BACKEND!r})\n"
                "from strategy_platform.validation import parallel\n"
                "if __name__ == '__main__':\n"
                "    items = [(0, 'hold', {'seconds': 50}), (1, 'hold', {'seconds': 50})]\n"
                "    threading.Thread(target=parallel._run_pool, args=(2, pickle.dumps({'snapshot': {}}), items),\n"
                "                     kwargs={'cancel': None, 'on_result': None}, daemon=True).start()\n"
                "    for _ in range(600):\n"
                "        if len(parallel.active_workers()) == 2: break\n"
                "        time.sleep(0.05)\n"
                "    time.sleep(2.0)\n"
                "    print(' '.join(str(p.pid) for p in parallel.active_workers()), flush=True)\n"
                "    os._exit(0)\n")
        res = subprocess.run([sys.executable, script], cwd=tmp, capture_output=True, text=True, timeout=120)
        pids = [int(x) for x in res.stdout.split()]
        self.assertEqual(len(pids), 2, res.stderr[-2000:])

        def alive(pid):
            try:
                with open(f"/proc/{pid}/stat") as fh:
                    return fh.read().split(")")[-1].split()[0] not in ("Z", "X")
            except OSError:
                return False
        t0 = time.monotonic()
        while any(alive(p) for p in pids) and time.monotonic() - t0 < 10:
            time.sleep(0.1)
        self.assertFalse(any(alive(p) for p in pids), "worker processes outlived their parent")

    def test_terminate_all_stops_running_pools(self):
        import threading
        box = {}

        def go():
            try:
                parallel._run_pool(2, pickle.dumps({"snapshot": {}}), [(0, "hold", {"seconds": 50}),
                                                                       (1, "hold", {"seconds": 50})],
                                   cancel=None, on_result=None)
            except Exception as ex:  # noqa: BLE001
                box["error"] = ex
        t = threading.Thread(target=go, daemon=True)
        t.start()
        for _ in range(600):
            if len(parallel.active_workers()) == 2:
                break
            time.sleep(0.05)
        self.assertEqual(parallel.terminate_all(), 2)
        t.join(30)
        self.assertFalse(t.is_alive())
        self.assertIsInstance(box.get("error"), RuntimeError)
        self.assertEqual(multiprocessing.active_children(), [])

    def test_cancel_terminates_workers(self):
        t0 = time.monotonic()
        with self.assertRaises(BacktestCancelled):
            parallel._run_pool(2, pickle.dumps({"snapshot": {}}), [(i, "isolation_check", {}) for i in range(4)],
                               cancel=lambda: True, on_result=None)
        self.assertEqual(multiprocessing.active_children(), [])
        self.assertLess(time.monotonic() - t0, 60)


class EquivalenceTest(ClockCase):
    def test_pool_report_identical_to_sequential(self):
        seq = seq_run()
        loader = parallel.RecordingLoader(bsup.FakeLoader(PRICES), keep=True)
        runner = parallel.TaskRunner(workers=2, loader=loader)
        pool = run(runner, data=loader)
        self.assertEqual(runner.mode, "pool")
        self.assertEqual(runner.rounds, 2)                       # grid + stress, no extra prefetch round
        self.assertEqual(multiprocessing.active_children(), [])
        a, c = norm(seq["report"]), norm(pool["report"])
        self.assertEqual(len(a["grid"]["configs"]), 4)
        self.assertTrue(all(x["sharpe"] is not None for x in a["grid"]["configs"]), a["grid"]["configs"])
        self.assertEqual(a, c)
        self.assertEqual(norm(seq["stress"]), norm(pool["stress"]))
        keys = [s["key"] for s in seq["stress"]["scenarios"]]
        self.assertEqual(keys, [e[0] for e in stress.EPISODES] + ["costs_x2", "slippage_x3", "monte_carlo"])

    def test_inline_runner_identical_to_sequential(self):
        rec = parallel.RecordingLoader(bsup.FakeLoader(PRICES))
        inline = run(parallel.TaskRunner(workers=1, loader=rec), data=rec)
        self.assertEqual(norm(seq_run()["report"]), norm(inline["report"]))

    def _base_long(self):
        s = dict(spec(), universe={"symbols": list(LONG_SYMS)}, signal={"field": "close"},
                 portfolio={"mode": "long_only", "selection": {"type": "all"}, "weighting": "equal",
                            "max_weight": 1.0, "gross_exposure": 1.0}, parameters={})
        return s, engine.run(s, data=bsup.FakeLoader(PRICES_LONG))

    def test_stress_scenarios_identical_on_the_pool(self):
        s, base = self._base_long()
        seq = stress.run_stress(s, base, data=bsup.FakeLoader(PRICES_LONG))
        seq3 = stress.synthetic_costs("costs_x3", "Costs x3", s, base, 3.0, 3.0, data=bsup.FakeLoader(PRICES_LONG))
        loader = parallel.RecordingLoader(bsup.FakeLoader(PRICES_LONG), keep=True)
        runner = parallel.TaskRunner(workers=2, loader=loader)
        pool = vjobs._stress_runner(s, base, mult=3.0, seed=None, runner=runner, checkpoints=None)
        done = [x["key"] for x in seq["scenarios"] if x["status"] == "done" and x["kind"] == "episode"]
        self.assertEqual(done, ["rate_shock_2022", "crypto_2022"])
        self.assertEqual(norm(pool), norm(dict(seq, scenarios=seq["scenarios"] + [seq3])))
        self.assertEqual(multiprocessing.active_children(), [])

    def test_validation_job_on_the_pool(self):
        from strategy_platform.backtest import jobs, runcard
        from strategy_platform.lifecycle import registry as lc
        from strategy_platform.storage import db
        tmp = tempfile.mkdtemp(prefix="sp-par-job-")
        self.addCleanup(shutil.rmtree, tmp, True)
        db.close()
        db.init(os.path.join(tmp, "hedgefund.db"))
        self.addCleanup(db.close)
        mgr = jobs.JobManager()
        jobs.set_manager(mgr)
        self.addCleanup(jobs.set_manager, None)
        sid = lc.create_from_text(json.dumps(spec()), "user")["id"]
        svc = vjobs.ValidationJobs(data_factory=lambda: bsup.FakeLoader(PRICES), cpu_count=3)
        rid = svc.submit_validation(sid, None, None)
        self.assertTrue(mgr.wait(170))
        card = runcard.get(rid)
        self.assertEqual(card["status"], "done", card["error"])
        self.assertEqual(svc.last_plan["workers"], 2)
        self.assertEqual(multiprocessing.active_children(), [])
        rep = card["metrics"]["validation"]["report"]
        ref = norm(seq_run()["report"])
        for k in ("gates", "grid", "pbo", "dsr", "folds", "stress", "ic", "training", "trial_count"):
            self.assertEqual(norm(rep[k]), ref[k], k)
        steps = {r["step_key"].split(":")[0] for r in db.checkpoints_list([rid])}
        self.assertEqual(steps, {"basis", "data", "grid", "stress"})

    def test_pool_cancel_raises(self):
        loader = parallel.RecordingLoader(bsup.FakeLoader(PRICES), keep=True)
        calls = []

        def cancel():
            calls.append(1)
            return len(calls) > 40
        with self.assertRaises(BacktestCancelled):
            run(parallel.TaskRunner(workers=2, loader=loader, cancel=cancel), data=loader)
        self.assertEqual(multiprocessing.active_children(), [])


if __name__ == "__main__":
    unittest.main()
