"""Stage 8 adapters (SPEC §18.3) against fake SDKs (no network): Alpaca paper (alpaca-py TradingClient shape) —
paper endpoint asserted, idempotent client order ids (lookup before send, ambiguous failures looked up, never re-sent
blindly), refusals as rejected acks, status mapping incl. partial fills and bracket legs, parsing of account /
positions / open orders, missing keys. (The Kraken Futures demo adapter and its tests were removed in stage 10,
S10-K; binance-demo: test_sp_broker_binance*.)"""
import sys
import unittest
from types import SimpleNamespace

from strategy_platform import secrets
from strategy_platform.data.http_gate import HttpGate
from strategy_platform.execution.brokers import alpaca_paper, live_guard
from strategy_platform.execution.brokers.common import BrokerAuthRejected, BrokerNotInstalled, BrokerSetupError
from strategy_platform.execution.errors import BrokerUnavailable

KEYS = {secrets.target("alpaca-paper-key-id"): "AK", secrets.target("alpaca-paper-secret-key"): "SK"}


def fast_gate(name):
    return HttpGate(name, 6000, capacity=100, sleep=lambda s: None)


# --- fake alpaca-py -------------------------------------------------------------------------------------------

class APIError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status_code = status
        self.message = message


class Req:
    def __init__(self, **kw):
        self.kw = kw


def kind(name):
    return type(name, (Req,), {"kind": name})


class FakeTrading:
    base = "https://paper-api.alpaca.markets"
    orders = {}
    fail_submit = []          # exceptions raised by submit_order (after storing when stored=True)
    store_before_fail = True
    submits = 0
    account_failures = 0
    positions = []

    def __init__(self, api_key, secret_key, paper, raw_data):
        assert paper is True and raw_data is True
        self._base_url = FakeTrading.base if paper else "https://api.alpaca.markets"
        self._retry = 3

    @classmethod
    def reset(cls):
        cls.base = "https://paper-api.alpaca.markets"
        cls.orders, cls.fail_submit, cls.store_before_fail, cls.submits = {}, [], True, 0
        cls.account_failures, cls.positions = 0, []

    def get_order_by_client_id(self, cid):
        for o in FakeTrading.orders.values():
            if o["client_order_id"] == cid:
                return dict(o)
        raise APIError(404, "order not found")

    def get_order_by_id(self, oid, filter=None):
        return dict(FakeTrading.orders[oid])

    def submit_order(self, order_data):
        FakeTrading.submits += 1
        kw = order_data.kw
        oid = f"o{len(FakeTrading.orders) + 1}"
        o = {"id": oid, "client_order_id": kw["client_order_id"], "symbol": kw["symbol"], "qty": str(kw.get("qty")),
             "filled_qty": "0", "filled_avg_price": None, "status": "accepted", "side": kw["side"],
             "type": order_data.kind, "time_in_force": kw["time_in_force"], "order_class": kw.get("order_class"),
             "legs": None}
        if FakeTrading.fail_submit:
            ex = FakeTrading.fail_submit.pop(0)
            if FakeTrading.store_before_fail:
                FakeTrading.orders[oid] = o
            raise ex
        FakeTrading.orders[oid] = o
        return dict(o)

    def get_orders(self, filter=None):
        return [dict(o) for o in FakeTrading.orders.values() if o["status"] in ("accepted", "new", "partially_filled",
                                                                                  "held")]

    def get_all_positions(self):
        return list(FakeTrading.positions)

    def get_account(self):
        if FakeTrading.account_failures:
            FakeTrading.account_failures -= 1
            raise APIError(503, "service unavailable")
        return {"cash": "1000.5", "equity": "1200.25", "buying_power": "2400.5", "status": "ACTIVE",
                "pattern_day_trader": False, "daytrade_count": 1, "multiplier": "2", "shorting_enabled": True}

    def cancel_order_by_id(self, oid):
        FakeTrading.orders[oid]["status"] = "pending_cancel"

    def cancel_orders(self):
        return [1, 2]

    def get_asset(self, symbol):
        return {"symbol": symbol, "tradable": True, "fractionable": True, "shortable": True, "easy_to_borrow": True,
                "price_increment": "0.01"}


ALPACA_SDK = SimpleNamespace(
    TradingClient=FakeTrading, OrderClass=SimpleNamespace(BRACKET="bracket"),
    OrderSide=SimpleNamespace(BUY="buy", SELL="sell"), QueryOrderStatus=SimpleNamespace(OPEN="open"),
    TimeInForce=SimpleNamespace(DAY="day", GTC="gtc"), GetOrderByIdRequest=Req, GetOrdersRequest=Req,
    LimitOrderRequest=kind("limit"), MarketOrderRequest=kind("market"), StopLimitOrderRequest=kind("stop_limit"),
    StopOrderRequest=kind("stop"), StopLossRequest=Req, TakeProfitRequest=Req)


class AlpacaAdapterTest(unittest.TestCase):
    def setUp(self):
        FakeTrading.reset()
        secrets.set_store(secrets.MemoryStore(KEYS))
        self.addCleanup(secrets.set_store, None)
        self.a = alpaca_paper.AlpacaPaperAdapter(sdk=ALPACA_SDK, gate=fast_gate("alpaca-test"))

    def order(self, **kw):
        o = {"client_order_id": "cid-00000001", "symbol": "SPY", "side": "buy", "qty": 5, "origin": "manual",
             "type": "market", "tif": "day"}
        o.update(kw)
        return o

    def test_paper_endpoint_only(self):
        self.assertEqual(self.a.check_endpoint(), "https://paper-api.alpaca.markets")
        FakeTrading.base = "https://api.alpaca.markets"
        with self.assertRaises(live_guard.LiveTradingDisabled):
            self.a.account()
        with self.assertRaises(live_guard.LiveTradingDisabled):
            self.a.submit(self.order())
        self.assertEqual(FakeTrading.submits, 0)

    def test_idempotent_and_ambiguous_send(self):
        ack = self.a.submit(self.order())
        self.assertEqual((ack["status"], ack["broker_order_id"]), ("accepted", "o1"))
        again = self.a.submit(self.order())
        self.assertEqual((again["broker_order_id"], FakeTrading.submits), ("o1", 1))     # lookup first
        FakeTrading.fail_submit = [TimeoutError("read timed out")]
        ack = self.a.submit(self.order(client_order_id="cid-00000002"))
        self.assertEqual((ack["status"], ack["broker_order_id"], FakeTrading.submits), ("accepted", "o2", 2))
        FakeTrading.fail_submit = [APIError(422, "client_order_id must be unique")]
        ack = self.a.submit(self.order(client_order_id="cid-00000003"))
        self.assertEqual(ack["status"], "accepted")
        FakeTrading.store_before_fail = False
        FakeTrading.fail_submit = [ConnectionError("connection refused")]
        with self.assertRaises(BrokerUnavailable) as cm:
            self.a.submit(self.order(client_order_id="cid-00000004"))
        self.assertEqual(getattr(cm.exception, "kind", None), "connection")

    def test_refusals_are_rejected_acks(self):
        FakeTrading.store_before_fail = False
        FakeTrading.fail_submit = [APIError(403, "trade denied due to pattern day trading protection")]
        ack = self.a.submit(self.order())
        self.assertEqual(ack["status"], "rejected")
        self.assertIn("pattern day trader protection", ack["reject_reason"])
        FakeTrading.fail_submit = [APIError(403, "insufficient buying power")]
        self.assertIn("insufficient buying power", self.a.submit(self.order(client_order_id="cid-0000000b"))
                      ["reject_reason"])
        for bad, why in ((self.order(symbol="BTC-USD"), "crypto"), (self.order(qty=1.5, side="short"), "fractional"),
                         (self.order(qty=None, notional=100, type="limit", limit_price=1), "notional"),
                         (self.order(type="limit"), "limit price"), (self.order(extended_hours=True), "extended"),
                         (self.order(tif="ioc"), "time in force")):
            ack = self.a.submit(dict(bad, client_order_id="cid-0000000x"))
            self.assertEqual(ack["status"], "rejected")
            self.assertIn(why, ack["reject_reason"])

    def test_status_mapping_and_legs(self):
        self.a.submit(self.order(type="bracket", take_profit={"limit_price": 600}, stop_loss={"stop_price": 450}))
        o = FakeTrading.orders["o1"]
        o.update(status="partially_filled", filled_qty="2", filled_avg_price="500.5")
        ack = self.a.order("cid-00000001")
        self.assertEqual((ack["status"], ack["filled_qty"], ack["avg_price"]), ("partially_filled", 2.0, 500.5))
        o.update(status="filled", filled_qty="5", filled_avg_price="501", order_class="bracket",
                 legs=[{"id": "l1", "client_order_id": "x1", "status": "new", "type": "limit", "side": "sell",
                        "qty": "5", "limit_price": "600"},
                       {"id": "l2", "client_order_id": "x2", "status": "held", "type": "stop", "side": "sell",
                        "qty": "5", "stop_price": "450"}])
        ack = self.a.order("cid-00000001")
        self.assertEqual(ack["status"], "filled")
        self.assertEqual([(l["broker_order_id"], l["type"], l["status"]) for l in ack["legs"]],
                         [("l1", "limit", "accepted"), ("l2", "stop", "accepted")])
        for raw, mapped in (("canceled", "cancelled"), ("expired", "expired"), ("rejected", "rejected"),
                            ("pending_new", "accepted"), ("done_for_day", "accepted")):
            o["status"] = raw
            self.assertEqual(self.a.order("cid-00000001")["status"], mapped)
        o["status"] = "weird"
        with self.assertRaises(TypeError):
            self.a.order("cid-00000001")

    def test_reads(self):
        FakeTrading.account_failures = 2                  # 503 twice: the gate retries reads
        acct = self.a.account()
        self.assertEqual((acct["cash"], acct["equity"], acct["cash_basis"]), (1000.5, 1200.25, 1000.5))
        FakeTrading.positions = [{"symbol": "AAPL", "qty": "-3", "side": "short", "avg_entry_price": "200",
                                  "current_price": "190", "market_value": "-570", "unrealized_pl": "30"}]
        p = self.a.positions()[0]
        self.assertEqual((p["symbol"], p["qty"], p["side"]), ("AAPL", -3.0, "short"))
        self.a.submit(self.order())
        oo = self.a.open_orders()
        self.assertEqual([o["client_order_id"] for o in oo], ["cid-00000001"])
        self.assertTrue(self.a.cancel("cid-00000001"))
        self.assertEqual(self.a.cancel_all(), 2)
        self.assertTrue(self.a.instrument("SPY")["tradable"])

    def test_keys_missing(self):
        secrets.set_store(secrets.MemoryStore({}))
        from strategy_platform.core.errors import SecretMissing
        with self.assertRaises(SecretMissing):
            self.a.account()
        ack = self.a.submit(self.order())
        self.assertEqual(ack["status"], "rejected")




if __name__ == "__main__":
    unittest.main()
