"""GET /v2/portfolio/summary (SPEC §17.15, UFOS.ai/docs/tasks/PORTFOLIO_SUMMARY_API.md): stored values only — equity
and day P&L from stored equity snapshots, no quote / broker / market-data call, no heartbeat touch, null WITH a reason
when a value is not stored, per-currency totals, halt / kill / auto trading / Council state. Temp DBs, fakes."""
import asyncio
import json
import unittest
from datetime import timedelta

try:
    from sp_trading_testkit import TradingCase
except ImportError:  # pragma: no cover
    from tests.sp_trading_testkit import TradingCase

from strategy_platform.api import routes
from strategy_platform.council import store as cl_store
from strategy_platform.execution import accounts, control
from strategy_platform.portfolio import summary as pf_summary
from strategy_platform.storage import db

KEYS = {"schema", "generated_at", "paper_only", "notice", "stored_values_only", "accounts", "totals",
        "positions_count", "autotrade", "halts", "kill", "council", "old_engine"}
ACC_KEYS = {"id", "label", "broker", "paper", "currency", "equity", "as_of", "day_pnl", "day_pnl_pct",
            "day_reference", "day_starts_at", "reason", "positions_count", "working_orders", "halt", "kill_pending",
            "spark"}


def call(query=None):
    api = routes.V2Api()
    status, payload = asyncio.run(api.portfolio_summary(query or {}, routes.NO_BODY, {}))
    routes.dumps(payload)                      # strict JSON (no NaN)
    return status, payload


class SummaryTest(TradingCase):
    def setUp(self):
        super().setUp()
        self.advance(hours=1)              # the opening snapshot (at the open time) is older than the test points

    def _point(self, dt, equity):
        self.acc.store.add_equity_point(dt, equity, equity, 0.0, 0.0)

    def test_shape_and_no_snapshot_reason(self):
        with db._connect() as conn:            # test only: an account without any stored snapshot
            conn.execute("DELETE FROM pa_equity WHERE account_id = 'paper-main'")
        st, p = call()
        self.assertEqual(st, 200)
        self.assertEqual(set(p), KEYS)
        self.assertEqual(p["schema"], 1)
        self.assertTrue(p["stored_values_only"])
        a = p["accounts"][0]
        self.assertEqual(set(a), ACC_KEYS)
        self.assertEqual(a["id"], "paper-main")
        self.assertIsNone(a["equity"])
        self.assertIsNone(a["day_pnl"])
        self.assertIn("no equity snapshot", a["reason"])
        self.assertIsNone(p["totals"]["USD"]["equity"])
        self.assertEqual(p["totals"]["USD"]["missing"], ["paper-main"])

    def test_equity_and_day_pnl_from_snapshots(self):
        now = self.clock()                                  # Fri 11:00 New York
        self._point(now - timedelta(days=1), 100000.0)     # Thursday: the day reference
        self._point(now - timedelta(minutes=30), 101000.0)
        self._point(now - timedelta(minutes=5), 101500.0)
        st, p = call()
        a = p["accounts"][0]
        self.assertEqual(a["equity"], 101500.0)
        self.assertAlmostEqual(a["day_pnl"], 1500.0)
        self.assertAlmostEqual(a["day_pnl_pct"], 1.5)
        self.assertEqual(a["day_reference"]["equity"], 100000.0)
        self.assertIsNone(a["reason"])
        self.assertEqual([x["equity"] for x in a["spark"]][-3:], [100000.0, 101000.0, 101500.0])   # + the opening point
        self.assertEqual(p["totals"]["USD"]["equity"], 101500.0)
        self.assertAlmostEqual(p["totals"]["USD"]["day_pnl"], 1500.0)

    def test_no_reference_before_day_start(self):
        self._point(self.clock() - timedelta(minutes=5), 100200.0)
        _st, p = call()
        a = p["accounts"][0]
        self.assertEqual(a["equity"], 100200.0)
        self.assertIsNone(a["day_pnl"])
        self.assertIn("day P&L not available", a["reason"])

    def test_never_quotes_values_or_touches_heartbeat(self):
        calls = []
        orig = accounts.get

        def no_account(*a, **k):                           # building an Account (broker / valuation) is refused
            calls.append(("accounts.get", a))
            raise AssertionError("summary must not build accounts")
        accounts.get = no_account
        self.addCleanup(setattr, accounts, "get", orig)
        control._last_seen = None
        st, _p = call()
        self.assertEqual(st, 200)
        self.assertEqual(calls, [])
        self.assertIsNone(control.last_seen())          # no heartbeat touch

    def test_states_halt_kill_autotrade_council(self):
        self.acc.set_halt(True, "kill", "kill switch")
        db.meta_set(control.AUTOTRADE_KEY, json.dumps({"enabled": True, "changed_at": "x", "changed_by": "user"}))
        cl_store.set_orders_enabled(True, "user", "test")
        _st, p = call()
        self.assertEqual(p["halts"], {"any": True, "accounts": ["paper-main"]})
        self.assertTrue(p["kill"]["active"])
        self.assertTrue(p["autotrade"]["enabled"])
        self.assertEqual(p["council"]["switchover"], "none")
        self.assertTrue(p["council"]["orders_enabled"])
        self.assertEqual(p["old_engine"]["applicable"], True)
        self.assertIsNone(p["old_engine"]["halted"])     # no old engine registered → not guessed

    def test_strict_query(self):
        st, p = call({"x": "1"})
        self.assertEqual(st, 400)
        self.assertEqual(p["errors"], {"x": "unknown parameter"})

    def test_route_registered_and_mode_stamped(self):
        self.assertIn(("GET", "/v2/portfolio/summary", "portfolio_summary", False), routes.ROUTES)
        self.assertIn("/v2/portfolio/summary", routes.MODE_PATHS)

    def test_module_has_no_network_or_quote_calls(self):
        import inspect
        src = inspect.getsource(pf_summary)
        for banned in ("quote", "broker.account", "remote", "requests", "aiohttp", "runtime.default", "touch("):
            self.assertNotIn(banned, src.replace("no quote", "").replace("no stand-in", ""), banned)


class NoAccountTest(TradingCase):
    open_main = False

    def test_no_accounts(self):
        st, p = call()
        self.assertEqual(st, 200)
        self.assertEqual(p["accounts"], [])
        self.assertEqual(p["totals"], {})
        self.assertEqual(p["positions_count"], 0)


if __name__ == "__main__":
    unittest.main()
