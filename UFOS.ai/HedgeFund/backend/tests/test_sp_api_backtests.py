"""/v2/backtests routes (SPEC §8.5 / §8.11): body validation (unknown
fields, types, ranges, ISO dates, start bounds), 404s, 409 while a job runs,
pre-seam start without the Tiingo key -> 503, progress while running, the
full detail / list shapes after a real run on a synthetic universe (fake
loader), cancel, route registration, main.py crash-recovery wiring and the
Tiingo key in GET /v2/settings/keys. Temp hedgefund.db, offline.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_api_backtests -v
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strategy_platform.api import routes  # noqa: E402
from strategy_platform.backtest import jobs, runcard  # noqa: E402
from strategy_platform.core.errors import SecretMissing  # noqa: E402
from strategy_platform.data.providers import tiingo  # noqa: E402
from strategy_platform.lifecycle import registry as lc  # noqa: E402
from strategy_platform.storage import db  # noqa: E402
import test_sp_backtest_jobs as tj  # noqa: E402
import test_sp_backtest_support as sup  # noqa: E402

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WAIT = 60


def call(name, body=routes.NO_BODY, query=None, match=None):
    return asyncio.run(getattr(routes.V2Api(), name)(query or {}, body, match or {}))


class Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tj.Case.setUpClass.__func__(cls)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sp3e-api-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        db.close()
        db.init(os.path.join(self.tmp, "hedgefund.db"))
        self.addCleanup(db.close)
        s = sup.spec(tj.SYMS)
        s["name"] = "api bt"
        self.sid = lc.create_from_text(json.dumps(s), "user")["id"]
        # Tiingo key present by default (equity universes need it at submit time, SPEC §8.8)
        patcher = mock.patch.object(tiingo, "require_key", return_value="test-key")
        self.require_key = patcher.start()
        self.addCleanup(patcher.stop)
        self.mgr = jobs.JobManager(data_factory=tj.loader)
        jobs.set_manager(self.mgr)
        self.addCleanup(jobs.set_manager, None)
        self.addCleanup(self.mgr.wait, WAIT)

    def use(self, mgr):
        self.mgr = mgr
        jobs.set_manager(mgr)
        self.addCleanup(mgr.wait, WAIT)


class ValidationTest(Case):
    def bad(self, body, field):
        status, payload = call("backtest_create", body)
        self.assertEqual(status, 400, payload)
        self.assertIn(field, payload["errors"], payload)

    def test_body_validation(self):
        sid = self.sid
        self.bad(routes.NO_BODY, "_")
        self.bad([1], "_")
        self.bad({}, "strategy_id")
        self.bad({"strategy_id": sid, "x": 1}, "x")
        self.bad({"strategy_id": 5}, "strategy_id")
        self.bad({"strategy_id": "a b/../c"}, "strategy_id")
        self.bad({"strategy_id": sid, "version": 0}, "version")
        self.bad({"strategy_id": sid, "version": "1"}, "version")
        self.bad({"strategy_id": sid, "version": True}, "version")
        self.bad({"strategy_id": sid, "capital": True}, "capital")
        self.bad({"strategy_id": sid, "capital": "100000"}, "capital")
        self.bad({"strategy_id": sid, "capital": 999}, "capital")
        self.bad({"strategy_id": sid, "capital": 100_000_001}, "capital")
        self.bad({"strategy_id": sid, "capital": 10 ** 400}, "capital")
        self.bad({"strategy_id": sid, "stress": 1}, "stress")
        self.bad({"strategy_id": sid, "start": "2024-1-2"}, "start")
        self.bad({"strategy_id": sid, "start": "2024-02-30"}, "start")
        self.bad({"strategy_id": sid, "end": "20240102"}, "end")
        self.bad({"strategy_id": sid, "start": "1999-12-31"}, "start")
        self.bad({"strategy_id": sid, "start": "2000-01-02"}, "start")
        self.bad({"strategy_id": sid, "start": "2024-03-01", "end": "2024-03-01"}, "start")
        self.bad({"strategy_id": sid, "start": "2024-03-02", "end": "2024-03-01"}, "start")
        self.bad({"strategy_id": sid, "benchmark": "not a symbol!"}, "benchmark")
        self.bad({"strategy_id": sid, "benchmark": 3}, "benchmark")
        self.assertEqual(call("backtest_create", {"strategy_id": sid}, query={"a": "1"})[0], 400)
        self.assertEqual(runcard.list_runs(), [])

    def test_unknown_strategy_404(self):
        self.assertEqual(call("backtest_create", {"strategy_id": "nope"})[0], 404)
        self.assertEqual(call("backtest_create", {"strategy_id": self.sid, "version": 9})[0], 404)

    def missing_key(self):
        err = SecretMissing("tiingo-api-key", "Tiingo API key")
        err.user_message = tiingo.MISSING_KEY_MESSAGE
        self.require_key.side_effect = err

    def crypto_strategy(self):
        s = sup.spec(["BTC-USD", "ETH-USD"])
        s["name"] = "crypto bt"
        return lc.create_from_text(json.dumps(s), "user")["id"]

    def released_manager(self):
        fake = tj.BlockingEngine(result=self.result)
        fake.release.set()
        self.use(jobs.JobManager(run_engine=fake, data_factory=tj.loader))

    def test_tiingo_key_only_when_loading_before_the_seam(self):
        # user decision 2026-10-06: from the seam on prices AND volume come from Alpaca SIP
        self.missing_key()
        for body in ({"strategy_id": self.sid, "start": "2008-01-02"},
                     {"strategy_id": self.sid, "start": "2016-01-15"}):      # the warm-up reaches before the seam
            status, payload = call("backtest_create", body)
            self.assertEqual(status, 503, body)
            self.assertEqual(payload, {"error": tiingo.MISSING_KEY_MESSAGE, "kind": "SecretMissing"})
        self.assertEqual(runcard.list_runs(), [])            # no run card
        self.assertIsNone(self.mgr.active_run_id())
        self.require_key.reset_mock()
        for body in ({"strategy_id": self.sid}, {"strategy_id": self.sid, "start": "2020-01-02"}):
            self.released_manager()
            self.assertEqual(call("backtest_create", body)[0], 202, body)
            self.assertTrue(self.mgr.wait(30))
        self.require_key.assert_not_called()

    def test_crypto_only_needs_key_only_before_seam(self):
        cid = self.crypto_strategy()
        self.missing_key()
        status, payload = call("backtest_create", {"strategy_id": cid, "start": "2015-06-01"})
        self.assertEqual((status, payload["kind"]), (503, "SecretMissing"))
        self.assertEqual(runcard.list_runs(), [])
        self.require_key.reset_mock()
        self.released_manager()
        # a start at the seam still loads the SPY benchmark's warm-up days before it -> key needed
        self.assertEqual(call("backtest_create", {"strategy_id": cid, "start": "2016-01-04"})[0], 503)
        self.require_key.reset_mock()
        self.assertEqual(call("backtest_create", {"strategy_id": cid, "start": "2016-06-01"})[0], 202)
        self.require_key.assert_not_called()

    def test_key_check_uses_requested_version_and_404(self):
        self.missing_key()
        self.assertEqual(call("backtest_create", {"strategy_id": "nope"})[0], 404)
        self.assertEqual(call("backtest_create", {"strategy_id": self.sid, "version": 9})[0], 404)
        self.require_key.side_effect = None
        self.released_manager()
        self.assertEqual(call("backtest_create", {"strategy_id": self.sid, "version": 1,
                                                  "start": "2015-06-01"})[0], 202)
        self.require_key.assert_called_once()

    def test_list_and_get_validation(self):
        self.assertEqual(call("backtests_list", query={"x": "1"})[0], 400)
        self.assertEqual(call("backtests_list", query={"limit": "0"})[0], 400)
        self.assertEqual(call("backtests_list", query={"limit": "201"})[0], 400)
        self.assertEqual(call("backtests_list", query={"limit": "-1"})[0], 400)
        self.assertEqual(call("backtests_list", query={"strategy_id": "a b"})[0], 400)
        self.assertEqual(call("backtests_list"), (200, {"runs": []}))
        self.assertEqual(call("backtest_get", match={"id": "bt-000000000000"})[0], 404)
        self.assertEqual(call("backtest_get", match={"id": "../x y"})[0], 404)
        self.assertEqual(call("backtest_cancel", match={"id": "bt-000000000000"})[0], 404)
        self.assertEqual(call("backtest_cancel", {"x": 1}, match={"id": "bt-000000000000"})[0], 400)


class FlowTest(Case):
    def test_409_progress_and_cancel(self):
        fake = tj.BlockingEngine(result=self.result)
        self.use(jobs.JobManager(run_engine=fake, data_factory=tj.loader))
        status, payload = call("backtest_create", {"strategy_id": self.sid, "capital": 25000})
        self.assertEqual(status, 202)
        rid = payload["run_id"]
        self.assertEqual(set(payload), {"run_id"})
        self.assertTrue(fake.started.wait(WAIT))
        status, busy = call("backtest_create", {"strategy_id": self.sid})
        self.assertEqual((status, busy["error"], busy["run_id"]), (409, "A backtest is already running.", rid))
        status, card = call("backtest_get", match={"id": rid})
        self.assertEqual(status, 200)
        self.assertEqual(card["status"], "running")
        self.assertEqual(set(card["progress"]), {"phase", "percent", "message", "updated_at"})
        self.assertEqual(card["progress"]["phase"], "data")
        self.assertEqual(card["request"]["capital"], 25000.0)
        status, payload = call("backtest_cancel", match={"id": rid})
        self.assertEqual((status, payload), (202, {"run_id": rid, "status": "cancelling"}))
        self.assertTrue(self.mgr.wait(WAIT))
        status, card = call("backtest_get", match={"id": rid})
        self.assertEqual((card["status"], card["error"], card["progress"]),
                         ("cancelled", "The backtest was cancelled.", None))
        status, payload = call("backtest_cancel", match={"id": rid})
        self.assertEqual((status, payload["status"]), (409, "cancelled"))

    def test_orphan_card_cancel(self):
        rid = runcard.create(self.sid, request={})["id"]          # no live job (e.g. recovery failed)
        self.assertEqual(call("backtest_cancel", match={"id": rid}), (200, {"run_id": rid, "status": "cancelled"}))

    def test_failure_message_in_detail(self):
        fake = tj.BlockingEngine(error=RuntimeError("Traceback internals"))
        fake.release.set()
        self.use(jobs.JobManager(run_engine=fake, data_factory=tj.loader))
        with self.assertLogs("strategy_platform.backtest.jobs", "ERROR"):
            rid = call("backtest_create", {"strategy_id": self.sid})[1]["run_id"]
            self.assertTrue(self.mgr.wait(WAIT))
        status, card = call("backtest_get", match={"id": rid})
        self.assertEqual((card["status"], card["error"]), ("failed", "Backtest failed — see backend.log"))
        self.assertNotIn("Traceback", routes.dumps(card))
        self.assertIsNone(card["metrics"])
        self.assertIsNone(card["curve"])
        self.assertEqual(card["data"], [])

    def test_happy_path_shapes(self):
        status, payload = call("backtest_create", {"strategy_id": self.sid, "version": 1, "stress": True,
                                                   "benchmark": "spy", "end": "2024-04-30"})
        self.assertEqual(status, 202, payload)
        rid = payload["run_id"]
        self.assertTrue(self.mgr.wait(WAIT))
        status, card = call("backtest_get", match={"id": rid})
        self.assertEqual(status, 200)
        self.assertEqual(card["status"], "done", card["error"])
        self.assertEqual(set(card), set(routes.BT_DETAIL_FIELDS) | {"strategy_name", "key_metrics", "data",
                                                                     "seam_date", "progress",
                                                                     "resumable",      # resumable: SPEC §10.4
                                                                     "attribution_unavailable"})  # SPEC §11.1
        self.assertEqual(card["strategy_name"], "api bt")
        self.assertEqual(card["request"], {"start": None, "end": "2024-04-30", "benchmark": "SPY",
                                           "capital": None, "stress": True,
                                           "history_from": None})
        self.assertEqual(set(card["key_metrics"]), {"total_return", "cagr", "sharpe", "max_drawdown", "volatility"})
        self.assertEqual(set(card["curve"]), {"dates", "equity", "benchmark", "gross_exposure", "net_exposure",
                                              "drawdown_pct"})
        self.assertTrue(all(isinstance(v, list) for v in card["curve"].values()))
        self.assertIn("end_limited_by", card["range"])
        self.assertEqual(card["seam_date"], "2016-01-04")
        self.assertTrue(all({"code", "message"} <= set(w) for w in card["warnings"]))
        self.assertIn("scenarios", card["stress"])
        self.assertIsNone(card["progress"])
        by = {d["symbol"]: d for d in card["data"]}
        self.assertEqual(set(by), {"AAA", "BBB", "SPY"})
        self.assertEqual(set(by["AAA"]), {"symbol", "asset_class", "role", "excluded", "prices", "volume",
                                          "dividends", "seam"})
        self.assertEqual(by["SPY"]["role"], "benchmark")
        self.assertEqual(by["AAA"]["prices"]["sources"], ["alpaca"])
        self.assertEqual(by["AAA"]["volume"]["source"], "tiingo")
        self.assertIn("alpaca", card["licences"])
        json.loads(routes.dumps(card))                                  # strict JSON
        status, listing = call("backtests_list", query={"strategy_id": self.sid, "limit": "5"})
        self.assertEqual(status, 200)
        row = listing["runs"][0]
        self.assertEqual(set(row), {"id", "created_at", "started_at", "finished_at", "status", "error",
                                    "strategy_id", "strategy_name", "version", "request", "range", "key_metrics",
                                    "kind",           # kind: stage 4 (V2 notes)
                                    "resumed_from", "resume_count", "error_kind", "auto_resume",
                                    "resumable"})     # SPEC §10.4 (R2 notes)
        self.assertEqual((row["id"], row["strategy_name"], row["status"]), (rid, "api bt", "done"))
        self.assertEqual(call("backtests_list", query={"strategy_id": "other"}), (200, {"runs": []}))
        self.assertEqual(call("backtest_cancel", match={"id": rid})[0], 409)


class WiringTest(unittest.TestCase):
    def test_routes_registered(self):
        table = {(m, p): n for m, p, n, _ in routes.ROUTES}
        self.assertEqual(table[("POST", "/v2/backtests")], "backtest_create")
        self.assertEqual(table[("GET", "/v2/backtests")], "backtests_list")
        self.assertEqual(table[("GET", "/v2/backtests/{id}")], "backtest_get")
        self.assertEqual(table[("POST", "/v2/backtests/{id}/cancel")], "backtest_cancel")

    def test_main_recovers_interrupted_runs(self):
        with open(os.path.join(_BACKEND, "main.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("sp_jobs.recover_interrupted", src)
        self.assertLess(src.index("sp_storage.init)"), src.index("sp_jobs.recover_interrupted"))

    def test_keys_list_tiingo(self):
        status, payload = call("keys")
        self.assertEqual(status, 200)
        names = [k["name"] for k in payload["keys"]]
        self.assertIn("tiingo-api-key", names)
        self.assertNotIn("value", next(k for k in payload["keys"] if k["name"] == "tiingo-api-key"))


if __name__ == "__main__":
    unittest.main()
