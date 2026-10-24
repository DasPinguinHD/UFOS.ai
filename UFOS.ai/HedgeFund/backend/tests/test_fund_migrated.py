"""Stage 7 (strategy_platform SPEC §17.1): after the migration of the paper account the
old engine is ADVISORY only — its cycle still analyses (agents, judge, recommendations)
but books nothing on the migrated account (no accruals, fills, margin, drawdown stop or
snapshot), and every trading path refuses with 409. Temp newsroom.db, fakes."""
import asyncio
import unittest

try:
    from fund_testkit import EngineCase
except ImportError:  # pragma: no cover
    from tests.fund_testkit import EngineCase

from fund import store
from fund.engine import EngineError


class MigratedEngineTest(EngineCase):
    def test_cycle_is_advisory_and_trading_refused(self):
        self.cycle()                                   # normal cycle first (account opened, snapshot)
        ledger_rows = len(store.cash_entries(limit=10000))
        points = len(store.equity_points())
        store.set_migration_state({"status": "migrated", "migration_id": "mig-000000000001", "at": "2026-10-02T14:00:00+00:00"})
        self.now = self.now.replace(minute=30)
        out = self.cycle()
        self.assertIn(out["status"], ("ok", "partial"))
        run = store.get_run(out["run_id"])["run"]
        self.assertEqual(run["summary"]["account_migrated"]["status"], "migrated")
        self.assertEqual(len(store.cash_entries(limit=10000)), ledger_rows)
        self.assertEqual(len(store.equity_points()), points)
        self.assertTrue(store.latest_recommendations())        # still advisory
        for call in (lambda: self.engine.set_autopilot(True),
                     lambda: asyncio.run(self.engine.manual_order({"symbol": "SPY", "side": "buy", "qty": 1})),
                     lambda: asyncio.run(self.engine.reset(100000))):
            with self.assertRaises(EngineError) as cm:
                call()
            self.assertEqual(cm.exception.status, 409)
        st = self.engine.state()
        self.assertEqual(st["legacy_account"], {"status": "migrated", "owner": "platform",
                                                "migrated_at": "2026-10-02T14:00:00+00:00",
                                                "migration_id": "mig-000000000001"})
        # the kill switch still works (halts; nothing to close on the old account's side is traded twice)
        orders = len(store.list_orders(limit=10000))
        out = asyncio.run(self.engine.kill("still works"))
        self.assertTrue(out["halted"])
        self.assertEqual(out["orders"], [])                    # the frozen account is never traded here
        self.assertEqual(len(store.list_orders(limit=10000)), orders)

    def test_not_migrated_state(self):
        self.assertIsNone(store.get_migration_state())
        self.assertEqual(self.engine.state()["legacy_account"]["status"], "active")
        store.set_migration_state({"status": "migrating", "migration_id": "m", "at": "x"})
        store.set_migration_state(None)
        self.assertIsNone(store.get_migration_state())


if __name__ == "__main__":
    unittest.main()
