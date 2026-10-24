"""history_from (SPEC §8.5, fix 2026-10-09): the "From 2000" toggle means
"load data from 2000-01-03, trade after the warm-up" (like the 2016
default) instead of an explicit start 2000-01-03 whose warm-up needed 1999.
Engine (load start, simulation start, no access before 2000-01-03, explicit
too-early start -> error with hint, bad value), stress episodes before 2016
(warm-up clamped at 2000-01-03 -> short_warmup), the Tiingo key rule
(`needs_pre_seam_equity`) and POST /v2/backtests (validation, key check,
stored request, jobs pass-through). Fake data, temp DB, offline.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_backtest_history_from -v
"""
import json
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strategy_platform.api import routes  # noqa: E402
from strategy_platform.backtest import engine, jobs, runcard, stress  # noqa: E402
from strategy_platform.core import calendars  # noqa: E402
from strategy_platform.core.errors import InvalidRequest, SecretMissing  # noqa: E402
from strategy_platform.data import seam  # noqa: E402
from strategy_platform.data.providers import tiingo  # noqa: E402
from strategy_platform.lifecycle import registry as lc  # noqa: E402
import test_sp_api_backtests as ta  # noqa: E402
import test_sp_backtest_jobs as tj  # noqa: E402
import test_sp_backtest_support as sup  # noqa: E402

DAYS_2000 = calendars.trading_days(date(2000, 1, 3), date(2000, 7, 31))
HINT = "use 'From 2000' or a later start"


class RecordingLoader(sup.FakeLoader):
    """FakeLoader that records the earliest requested load start."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.starts = []

    def history(self, symbol, adjustment, start, end=None, *, progress=None):
        self.starts.append(start)
        return super().history(symbol, adjustment, start, end, progress=progress)

    def corporate_actions(self, symbol, start, end=None, *, progress=None):
        self.starts.append(start)
        return super().corporate_actions(symbol, start, end, progress=progress)

    def volume(self, symbol, start, end=None, *, progress=None):
        self.starts.append(start)
        return super().volume(symbol, start, end, progress=progress)


def loader_2000():
    return RecordingLoader({"AAA": sup.series(50, days=DAYS_2000), "SPY": sup.series(400, days=DAYS_2000)})


class EngineTest(unittest.TestCase):
    def test_history_from_2000_trades_after_the_warmup(self):
        data = loader_2000()
        r = engine.run(sup.spec(["AAA"]), data=data, history_from="2000")
        warmup = r.range["warmup_sessions"]
        self.assertEqual(r.range["load_start"], "2000-01-03")
        self.assertEqual(r.range["first_data"], "2000-01-03")
        self.assertEqual(r.range["start"], DAYS_2000[warmup].isoformat())
        self.assertEqual(r.dates[0], DAYS_2000[warmup].isoformat())
        self.assertEqual(min(data.starts), date(2000, 1, 3))         # nothing requested from 1999
        self.assertEqual(r.request["history_from"], "2000")
        self.assertIsNone(r.request["start"])

    def test_default_history_from_loads_from_the_seam(self):
        data = RecordingLoader({"AAA": sup.series(50), "SPY": sup.series(400)})
        for hf in (None, "2016"):
            r = engine.run(sup.spec(["AAA"]), data=data, history_from=hf)
            self.assertEqual(r.range["load_start"], seam.SEAM_DATE.isoformat())
            self.assertEqual(r.dates[0], sup.DAYS[r.range["warmup_sessions"]].isoformat())
            self.assertEqual(r.request["history_from"], hf)

    def test_explicit_start_keeps_meaning_first_trading_day(self):
        data = loader_2000()
        r = engine.run(sup.spec(["AAA"]), data=data, start="2000-03-01", history_from="2000")
        self.assertEqual(r.range["start"], "2000-03-01")
        self.assertEqual(r.request["start"], "2000-03-01")
        load = date.fromisoformat(r.range["load_start"])
        self.assertEqual(len(calendars.trading_days(load, date(2000, 3, 1))) - 1, r.range["warmup_sessions"])

    def test_explicit_start_with_warmup_before_2000_is_a_clear_error(self):
        for start, hf in (("2000-01-03", None), ("2000-01-03", "2000"), ("2000-01-20", "2016")):
            with self.assertRaises(InvalidRequest) as ctx:
                engine.run(sup.spec(["AAA"]), data=loader_2000(), start=start, history_from=hf)
            msg = ctx.exception.user_message
            self.assertIn(HINT, msg)
            self.assertIn("earliest data is 2000-01-03", msg)
            self.assertNotIn("1999", msg)

    def test_bad_history_from(self):
        for hf in ("1999", "2001", 2000, ""):
            with self.assertRaises(InvalidRequest) as ctx:
                engine.run(sup.spec(["AAA"]), data=loader_2000(), history_from=hf)
            self.assertIn("history_from", ctx.exception.user_message)

    def test_needs_pre_seam_equity(self):
        s = sup.spec(["AAA"])
        self.assertTrue(engine.needs_pre_seam_equity(s, None, None, "2000"))
        self.assertFalse(engine.needs_pre_seam_equity(s, None, None, "2016"))
        self.assertFalse(engine.needs_pre_seam_equity(s, None, None, None))
        self.assertTrue(engine.needs_pre_seam_equity(s, "2008-01-02", None, None))
        self.assertFalse(engine.needs_pre_seam_equity(s, "2020-01-02", None, "2000"))   # explicit start wins
        c = sup.spec(["BTC-USD"])
        self.assertTrue(engine.needs_pre_seam_equity(c, None, "SPY", "2000"))    # equity benchmark
        self.assertFalse(engine.needs_pre_seam_equity(c, None, "ETH-USD", "2000"))


class StressEpisodeTest(unittest.TestCase):
    def test_dotcom_episode_clamps_the_warmup_at_2000(self):
        # vol target -> warm-up 66 sessions > the 56 sessions between 2000-01-03 and 2000-03-24
        s = sup.spec(["AAA"], risk={"vol_target_annual": 0.5})
        with self.assertRaises(InvalidRequest) as ctx:      # a normal explicit start: error with the hint
            engine.run(s, data=loader_2000(), start="2000-03-24", end="2000-06-30")
        self.assertIn(HINT, ctx.exception.user_message)
        data = loader_2000()
        out = stress.episode("dotcom", "Dot-com crash", date(2000, 3, 24), date(2000, 6, 30), s, capital=1e5,
                             benchmark="SPY", data=data)
        self.assertEqual(out["status"], "done", out["reason"])
        self.assertEqual(out["range"]["start"], "2000-03-24")
        self.assertEqual(min(data.starts), date(2000, 1, 3))
        sw = [w for w in out["warnings"] if w["code"] == "short_warmup"]
        self.assertEqual(len(sw), 1)
        self.assertEqual(sw[0]["need"], 66)
        self.assertLess(sw[0]["have"], 66)

    def test_gfc_episode_has_its_full_warmup(self):
        days = calendars.trading_days(date(2007, 1, 2), date(2009, 4, 30))
        data = sup.FakeLoader({"AAA": sup.series(50, days=days), "SPY": sup.series(400, days=days)})
        out = stress.episode("gfc", "GFC", date(2007, 10, 9), date(2009, 3, 9), sup.spec(["AAA"]), capital=1e5,
                             benchmark="SPY", data=data)
        self.assertEqual(out["status"], "done", out["reason"])
        self.assertFalse([w for w in out["warnings"] if w["code"] == "short_warmup"])


class ApiTest(ta.Case):
    def released_manager(self):
        fake = tj.BlockingEngine(result=self.result)
        fake.release.set()
        self.use(jobs.JobManager(run_engine=fake, data_factory=tj.loader))
        return fake

    def missing_key(self):
        err = SecretMissing("tiingo-api-key", "Tiingo API key")
        err.user_message = tiingo.MISSING_KEY_MESSAGE
        self.require_key.side_effect = err

    def test_validation(self):
        for value in ("1999", "2001", 2000, None, True, ""):
            status, payload = ta.call("backtest_create", {"strategy_id": self.sid, "history_from": value})
            self.assertEqual(status, 400, value)
            self.assertIn("history_from", payload["errors"])
        status, payload = ta.call("backtest_create", {"strategy_id": self.sid, "history_from": "2000", "x": 1})
        self.assertEqual(status, 400)
        self.assertEqual(payload["errors"].get("x"), "unknown field")
        self.assertEqual(runcard.list_runs(), [])

    def test_key_needed_for_history_from_2000_only(self):
        self.missing_key()
        status, payload = ta.call("backtest_create", {"strategy_id": self.sid, "history_from": "2000"})
        self.assertEqual(status, 503)
        self.assertEqual(payload, {"error": tiingo.MISSING_KEY_MESSAGE, "kind": "SecretMissing"})
        self.assertEqual(runcard.list_runs(), [])
        self.require_key.reset_mock()
        for body in ({"strategy_id": self.sid, "history_from": "2016"},
                     {"strategy_id": self.sid, "history_from": "2000", "start": "2020-01-02"}):
            self.released_manager()
            self.assertEqual(ta.call("backtest_create", body)[0], 202, body)
            self.assertTrue(self.mgr.wait(30))
        self.require_key.assert_not_called()

    def test_crypto_with_crypto_benchmark_needs_no_key(self):
        self.missing_key()
        s = sup.spec(["BTC-USD", "ETH-USD"])
        s["name"] = "crypto hf"
        cid = lc.create_from_text(json.dumps(s), "user")["id"]
        self.released_manager()
        status, _ = ta.call("backtest_create", {"strategy_id": cid, "history_from": "2000", "benchmark": "BTC-USD"})
        self.assertEqual(status, 202)
        self.assertTrue(self.mgr.wait(30))

    def test_stored_and_passed_to_the_engine(self):
        fake = self.released_manager()
        status, payload = ta.call("backtest_create", {"strategy_id": self.sid, "history_from": "2000"})
        self.assertEqual(status, 202, payload)
        self.assertTrue(self.mgr.wait(30))
        card = runcard.get(payload["run_id"])
        self.assertEqual(card["request"]["history_from"], "2000")
        self.assertIsNone(card["request"]["start"])
        self.assertEqual(fake.calls[0]["history_from"], "2000")
        self.assertIsNone(fake.calls[0]["start"])
        status, detail = ta.call("backtest_get", match={"id": payload["run_id"]})
        self.assertEqual(status, 200)
        self.assertEqual(detail["request"]["history_from"], "2000")


if __name__ == "__main__":
    unittest.main()
