"""Stage 8 routes (SPEC §18.9/§18.10): /v2/brokers/* payload keys, strict input, connect confirm, kill always 200,
resolve validation, routing switch with confirmation + preconditions, POST /v2/incubation uses the route, manual
order types / venue rule on /v2/oms/orders, the key status (no Kraken demo keys since S10-K). Handlers called directly
(no aiohttp), temp DBs, fakes."""
import asyncio
import unittest

try:
    from sp_broker_testkit import BrokerCase, make_strategy
    from sp_trading_testkit import EXPECTED
except ImportError:  # pragma: no cover
    from tests.sp_broker_testkit import BrokerCase, make_strategy
    from tests.sp_trading_testkit import EXPECTED

from strategy_platform.api import routes
from strategy_platform.execution.brokers import service
from strategy_platform.live import incubation

STATUS_KEYS = {"id", "broker", "label", "venue", "instruments", "paper", "live", "endpoint", "sdk", "keys",
               "connection", "link", "reconciliation", "halt", "snapshot", "working_orders", "routes", "capabilities", "baseline",
               "notes", "terms", "live_guard", "can", "kill", "state", "retired", "chip", "currency",
               "currency_note"}


def call(name, query=None, body=routes.NO_BODY, match=None):
    status, payload = asyncio.run(getattr(routes.V2Api(), name)(query or {}, body, match or {}))
    routes.dumps(payload)
    return status, payload


class BrokerApiTest(BrokerCase):
    def test_list_detail_connect(self):
        st, p = call("brokers_list")
        self.assertEqual(st, 200)
        self.assertEqual(set(p), {"brokers", "paper_only", "notice", "live_trading"})
        self.assertFalse(p["live_trading"]["enabled"])
        b = p["brokers"][0]
        self.assertEqual(set(b), STATUS_KEYS)
        self.assertEqual((b["id"], b["connection"]["status"], b["reconciliation"]["status"]),
                         ("alpaca-paper", "never_connected", "never"))
        self.assertEqual([x["id"] for x in p["brokers"]], ["alpaca-paper", "binance-demo"])    # S10-K
        self.assertEqual(p["brokers"][1]["endpoint"], "https://demo-api.binance.com")
        self.assertEqual((p["brokers"][1]["currency"], p["brokers"][1]["chip"]), ("USDT", "DEMO · SIMULATED FUNDS"))
        self.assertEqual(call("broker_get", match={"id": "nope"})[0], 404)
        st, p = call("broker_connect", match={"id": "alpaca-paper"}, body={"confirm": "connect", "reason": "r" * 501})
        self.assertEqual(st, 400)
        self.assertEqual(set(p["errors"]), {"reason"})          # fix1023: the confirm word is no longer checked
        st, p = call("broker_connect", match={"id": "alpaca-paper"}, body={"confirm": "CONNECT ALPACA-PAPER",
                                                                           "reason": "first test", "x": 1})
        self.assertEqual((st, p["errors"]), (400, {"x": "unknown field"}))
        st, p = call("broker_test", match={"id": "alpaca-paper"}, body={})
        self.assertEqual((st, p["ok"], p["flat"]), (200, True, True))
        self.assertEqual([c["name"] for c in p["checks"]][:3], ["SDK installed", "Keys present", "Paper/demo endpoint"])
        st, p = call("broker_connect", match={"id": "alpaca-paper"},
                     body={"confirm": "CONNECT ALPACA-PAPER", "reason": "first test"})
        self.assertEqual((st, p["reconciliation"]["status"], p["broker"]["can"]["kill"]), (200, "ok", True))
        st, p = call("broker_get", match={"id": "alpaca-paper"})
        self.assertEqual(set(p), {"broker", "positions", "open_orders", "reconciliations", "events"})
        self.assertEqual(p["broker"]["snapshot"]["account"]["cash"], 100000.0)
        st, p = call("broker_reconcile", match={"id": "alpaca-paper"}, body={})
        self.assertEqual((st, p["reconciliation"]["status"]), (200, "ok"))
        st, p = call("broker_reconciliations", match={"id": "alpaca-paper"}, query={"limit": "5"})
        self.assertEqual(len(p["reconciliations"]), 2)
        self.assertEqual(call("broker_reconciliations", match={"id": "alpaca-paper"}, query={"limit": "0"})[0], 400)
        st, p = call("portfolio_account_get", match={"id": "alpaca-paper"})
        self.assertEqual((st, p["account"]["broker"], p["account"]["reconciliation"]["status"]),
                         (200, "alpaca_paper", "ok"))
        st, p = call("broker_resolve", match={"id": "alpaca-paper"}, body={"action": "accept_broker", "reason": "abc"})
        self.assertEqual((st, p["code"]), (409, "nothing_to_resolve"))       # fix1023: no confirm phrase needed
        st, p = call("broker_resolve", match={"id": "alpaca-paper"}, body={"action": "cancel_unknown_orders",
                                                                           "reason": "nothing wrong"})
        self.assertEqual((st, p["code"]), (409, "nothing_to_resolve"))
        st, p = call("broker_instrument", match={"id": "alpaca-paper"}, query={"symbol": "BTC-USD"})
        self.assertEqual(st, 400)
        st, p = call("broker_instrument", match={"id": "kraken-demo"}, query={"symbol": "BTC-USD"})
        self.assertEqual((st, p["code"]), (409, "retired"))                 # retired (§18.13)

    def test_kill_always_answers(self):
        st, p = call("broker_kill", match={"id": "alpaca-paper"}, body={})
        self.assertEqual((st, p["ok"]), (200, False))
        self.connect("alpaca-paper")
        st, p = call("broker_kill", match={"id": "alpaca-paper"}, body={"reason": "test"})
        self.assertEqual((st, p["ok"], p["halt"]["kind"]), (200, True, "kill"))

    def test_oms_order_types_and_venue(self):
        st, p = call("oms_order_create", body={"account_id": "paper-main", "symbol": "SPY", "side": "buy", "qty": 1,
                                               "type": "limit", "limit_price": 400})
        self.assertEqual((st, p["errors"]["type"]), (400, "the internal paper broker supports market orders only"))
        st, p = call("oms_order_create", body={"account_id": "alpaca-paper", "symbol": "BTC-USD", "side": "buy",
                                               "qty": 1})
        self.assertEqual((st, set(p["errors"])), (400, {"symbol"}))
        st, p = call("oms_order_create", body={"account_id": "alpaca-paper", "symbol": "SPY", "side": "sell",
                                               "qty": 1, "type": "bracket"})
        self.assertEqual(set(p["errors"]), {"take_profit", "stop_loss", "side"})
        st, p = call("oms_order_create", body={"account_id": "alpaca-paper", "symbol": "SPY", "side": "buy", "qty": 1})
        self.assertEqual((st, p["code"]), (409, "not_connected"))
        self.connect()
        st, p = call("oms_order_create", body={"account_id": "alpaca-paper", "symbol": "SPY", "side": "buy", "qty": 2,
                                               "type": "bracket", "take_profit": {"limit_price": 600},
                                               "stop_loss": {"stop_price": 450}, "client_order_id": "wpf-guid-0002"})
        self.assertEqual((st, p["order"]["state"], p["order"]["order_type"]), (201, "filled", "bracket"))
        self.assertEqual(p["order"]["bracket"], {"take_profit": {"limit_price": 600.0},
                                                 "stop_loss": {"stop_price": 450.0}})
        for k in ("order_type", "limit_price", "stop_price", "legs", "parent_client_order_id", "broker_status"):
            self.assertIn(k, p["order"])

    def test_routing_and_incubation(self):
        sid = make_strategy(symbols=("SPY", "QQQ"), state="validated")
        csid = make_strategy(symbols=("BTC-USD",), state="validated", name="crypto one")
        st, p = call("brokers_routing_get")
        r = {x["strategy_id"]: x for x in p["routes"]}
        self.assertEqual((r[sid]["universe"], r[sid]["account_id"], r[sid]["allowed_accounts"]),
                         ("equity", "paper-main", ["paper-main", "alpaca-paper"]))
        self.assertEqual(r[csid]["allowed_accounts"], ["paper-main", "binance-demo"])   # kraken-demo retired (§18.13)
        body = {"strategy_id": sid, "account_id": "alpaca-paper", "confirm": "ROUTE TO ALPACA-PAPER",
                "reason": "try alpaca"}
        st, p = call("brokers_routing_post", body=dict(body, confirm="route"))
        self.assertEqual((st, p["code"]), (409, "not_connected"))        # fix1023: confirm accepted and ignored
        st, p = call("brokers_routing_post", body=body)
        self.assertEqual((st, p["code"]), (409, "not_connected"))
        self.connect()
        st, p = call("brokers_routing_post", body=dict(body, account_id="kraken-demo", confirm="ROUTE TO KRAKEN-DEMO"))
        self.assertEqual((st, p["code"]), (409, "retired"))
        st, p = call("brokers_routing_post", body=body)
        self.assertEqual((st, p["route"]["account_id"]), (200, "alpaca-paper"))
        self.assertEqual(service.route_for(sid), "alpaca-paper")
        st, p = call("broker_disconnect", match={"id": "alpaca-paper"}, body={"reason": "stop it"})
        self.assertEqual((st, p["code"]), (409, "routed"))
        # POST /v2/incubation: the route is the account; another account -> 409 route_mismatch
        from unittest import mock
        with mock.patch.object(incubation, "expected_for", return_value=EXPECTED):
            st, p = call("incubation_create", body={"strategy_id": sid, "length_sessions": 20, "reason": "go incubate",
                                                    "account_id": "paper-main"})
            self.assertEqual((st, p["code"]), (409, "route_mismatch"))
            st, p = call("incubation_create", body={"strategy_id": sid, "length_sessions": 20, "reason": "go incubate"})
            self.assertEqual((st, p["run"]["account_id"]), (201, "alpaca-paper"))
        st, p = call("brokers_routing_post", body={"strategy_id": sid, "account_id": "paper-main",
                                                   "confirm": "ROUTE TO PAPER-MAIN", "reason": "back home"})
        self.assertEqual((st, p["code"]), (409, "incubating"))

    def test_keys_status(self):
        st, p = call("keys")
        names = [k["name"] for k in p["keys"]]
        self.assertNotIn("kraken-demo-api-key", names)          # retired (S10-K): never read, not listed
        self.assertNotIn("kraken-demo-api-secret", names)
        self.assertIn("binance-demo-api-key", names)
        for k in p["keys"]:
            self.assertNotIn("value", k)


if __name__ == "__main__":
    unittest.main()
