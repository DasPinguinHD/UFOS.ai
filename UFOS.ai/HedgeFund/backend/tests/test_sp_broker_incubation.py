"""Stage 8 fix 2026-10-18 (SPEC §18.2, §18.12): incubation eligibility and start are evaluated against the
strategy's ROUTE (not always paper-main): connected, venue rule, not halted, reconciled. A strategy routed to
alpaca-paper (binance-demo: test_sp_broker_binance_flow; the kraken-demo cases went with its adapter, S10-K). Fakes,
temp DBs, offline."""
import asyncio
import unittest
from unittest import mock

try:
    from sp_broker_testkit import BrokerCase, make_strategy
    from sp_trading_testkit import EXPECTED
except ImportError:  # pragma: no cover
    from tests.sp_broker_testkit import BrokerCase, make_strategy
    from tests.sp_trading_testkit import EXPECTED

from strategy_platform.api import routes
from strategy_platform.execution.brokers import reconcile, service
from strategy_platform.live import incubation


def call(name, query=None, body=routes.NO_BODY, match=None):
    status, payload = asyncio.run(getattr(routes.V2Api(), name)(query or {}, body, match or {}))
    routes.dumps(payload)
    return status, payload


def route(sid, aid):
    return service.set_route(sid, aid, service.route_phrase(aid), "route for the test", "user")


class RoutedEligibilityTest(BrokerCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(incubation, "expected_for", return_value=EXPECTED)
        p.start()
        self.addCleanup(p.stop)

    def test_alpaca_routed(self):
        sid = make_strategy(symbols=("SPY", "QQQ"), state="validated")
        acc, _ = self.connect("alpaca-paper")
        route(sid, "alpaca-paper")
        st, p = call("incubation_eligibility", query={"strategy_id": sid})
        self.assertEqual((st, p["eligible"], p["account_id"]), (200, True, "alpaca-paper"))
        # a reconciliation mismatch on the ROUTED account blocks — paper-main being fine does not help
        self.alpaca.foreign_trade("SPY", 1, 500.0)
        reconcile.run(acc, "cycle")
        el = incubation.eligibility(sid)
        self.assertEqual((el["eligible"], el["code"], el["account_id"]), (False, "halted", "alpaca-paper"))
        self.assertIn("resolve the reconciliation", el["reason"])
        st, p = call("incubation_create", body={"strategy_id": sid, "length_sessions": 20, "reason": "go incubate"})
        self.assertEqual((st, p["code"]), (409, "halted"))
        self.alpaca.foreign_trade("SPY", -1, 500.0)
        reconcile.resolve(acc, "recheck", "fixed at the broker")
        self.advance(minutes=20)                                   # stale reconciliation -> not_reconciled
        el = incubation.eligibility(sid)
        self.assertEqual((el["eligible"], el["code"]), (False, "not_reconciled"))
        reconcile.run(acc, "manual")
        st, p = call("incubation_create", body={"strategy_id": sid, "length_sessions": 20, "reason": "go incubate"})
        self.assertEqual((st, p["run"]["account_id"]), (201, "alpaca-paper"))



if __name__ == "__main__":
    unittest.main()
