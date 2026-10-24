"""Fixtures for the stage-8 broker tests (SPEC §18): a simulated broker at the adapter interface (FakeBroker —
orders, partial fills, rejects, dropped connections, foreign trades), the broker factory pointed at it, keys in a
MemoryStore, temp databases (TradingCase). Offline: no network, no SDK. Part of UFOS.ai (paper/demo only)."""
try:
    from sp_trading_testkit import FRI, Quotes, TradingCase, make_strategy  # noqa: F401
except ImportError:  # pragma: no cover
    from tests.sp_trading_testkit import FRI, Quotes, TradingCase, make_strategy  # noqa: F401

from strategy_platform import secrets
from strategy_platform.execution.brokers import alpaca_paper, live_guard, mirror
from strategy_platform.execution.brokers.common import unavailable

KEYS = {secrets.target("alpaca-paper-key-id"): "AKFAKE", secrets.target("alpaca-paper-secret-key"): "SKFAKE",
        secrets.target("binance-demo-api-key"): "BFAKE", secrets.target("binance-demo-api-secret"): "BSFAKE"}


class FakeBroker:
    """A broker at the adapter interface, Alpaca-shaped (spot cash). The Kraken-shaped kind went with the Kraken demo
    adapter (stage 10, S10-K); binance-demo runs its REAL adapter on a fake ccxt exchange (sp_binance_testkit)."""

    def __init__(self, kind="alpaca", cash=100000.0, prices=None):
        if kind != "alpaca":
            raise ValueError(f"FakeBroker kind {kind!r}: only 'alpaca' exists (S10-K)")
        self.kind = kind
        self.name = "alpaca_paper"
        self.capabilities = alpaca_paper.CAPABILITIES
        self.endpoint = live_guard.paper_endpoint(self.name)
        self.cash = float(cash)
        self.pos = {}                 # sym -> [qty, avg]
        self.orders = {}              # broker id -> order dict
        self.by_cid = {}
        self.next_id = 100
        self.prices = dict(prices or {"SPY": 500.0, "QQQ": 400.0, "BTC-USD": 60000.0, "ETH-USD": 3000.0})
        self.auto = "fill"            # fill | none | partial
        self.partial_frac = 0.5
        self.down = False
        self.drop_after_send = 0      # the next N sends reach the broker but the answer is lost
        self.reject_next = None
        self.reject_closes = 0        # the next N sell/cover orders are rejected
        self.cancel_delay = 0         # cancels stay "pending_cancel" for N open-order reads (the qty stays held)
        self.sends = 0
        self.fee_rate = 0.0
        self.installed = True
        self.calls = []

    # --- helpers ---------------------------------------------------------------------------
    def _check(self, what):
        self.calls.append(what)
        if self.down:
            raise unavailable(f"{self.name}: connection reset (fake)", ConnectionResetError("reset"), connection=True)

    OPEN = ("accepted", "partially_filled", "held", "pending_cancel")

    def _held(self, sym, side):
        return sum(o["qty"] - o["filled"] for o in self.orders.values()
                   if o["symbol"] == sym and o["status"] in self.OPEN and o["side"] == side)

    def _ack(self, o):
        st = {"held": "accepted", "pending_cancel": "accepted"}.get(o["status"], o["status"])
        return {"client_order_id": o["cid"], "broker_order_id": o["id"], "status": st, "qty": o["qty"],
                "filled_qty": o["filled"], "avg_price": o["avg"], "fees": o["fees"],
                "notional": o["filled"] * o["avg"] if o["avg"] else None,
                "reject_reason": o.get("reason"), "ts": None, "symbol": o["symbol"], "side": o["side"],
                "type": o["type"], "legs": [self._ack(self.orders[l]) for l in o.get("legs", [])],
                "broker_status": st, "tif": o["tif"], "limit_price": o.get("limit"), "stop_price": o.get("stop"),
                "created_at": None}

    def _apply(self, sym, signed, px, fee):
        q, avg = self.pos.get(sym, [0.0, 0.0])
        realized = 0.0
        new_q = q + signed
        if abs(q) < 1e-12 or (q > 0) == (signed > 0):
            avg = (abs(q) * avg + abs(signed) * px) / abs(new_q)
        else:
            closed = min(abs(signed), abs(q))
            realized = (px - avg) * closed * (1 if q > 0 else -1)
            if abs(signed) > abs(q):
                avg = px
        self.cash -= signed * px
        self.cash -= fee
        new_q = round(new_q, 8)
        if abs(new_q) < 1e-12:
            self.pos.pop(sym, None)
        else:
            self.pos[sym] = [new_q, avg]

    def fill(self, cid_or_id, qty=None, price=None):
        o = self.by_cid.get(cid_or_id) or self.orders[cid_or_id]
        rest = o["qty"] - o["filled"]
        q = rest if qty is None else min(qty, rest)
        px = float(price if price is not None else (o.get("limit") or self.prices[o["symbol"]]))
        fee = abs(q * px * self.fee_rate)
        signed = q if o["side"] in ("buy", "cover") else -q
        self._apply(o["symbol"], signed, px, fee)
        o["avg"] = (o["filled"] * (o["avg"] or 0) + q * px) / (o["filled"] + q)
        o["filled"] = round(o["filled"] + q, 8)
        o["fees"] += fee
        o["status"] = "filled" if o["filled"] >= o["qty"] - 1e-12 else "partially_filled"
        if o["status"] == "filled" and o.get("legs"):
            for lid in o["legs"]:
                self.orders[lid]["status"] = "accepted"
        return o

    def foreign_trade(self, sym, signed, price):
        """A trade the platform did not place (manual at the broker / liquidation)."""
        self._apply(sym, signed, price, 0.0)

    def foreign_order(self, sym="SPY", side="buy", qty=1):
        oid = str(self.next_id)
        self.next_id += 1
        self.orders[oid] = {"id": oid, "cid": f"foreign-{oid}", "symbol": sym, "side": side, "qty": qty, "filled": 0.0,
                            "avg": None, "fees": 0.0, "status": "accepted", "type": "limit", "tif": "day",
                            "limit": 1.0}
        self.by_cid[f"foreign-{oid}"] = self.orders[oid]
        return oid

    # --- adapter interface ---------------------------------------------------------------------
    def sdk_info(self):
        return {"name": "fake", "installed": self.installed, "version": "0",
                "message": None if self.installed else "fake SDK not installed"}

    def check_endpoint(self):
        self._check("check_endpoint")
        return live_guard.assert_paper_url(self.name, self.endpoint)

    def submit(self, order):
        self._check("submit")
        cid = order["client_order_id"]
        if cid in self.by_cid:
            return self._ack(self.by_cid[cid])
        reason = None
        if order["side"] in ("sell", "cover"):
            q = self.pos.get(order["symbol"], [0.0, 0.0])[0]
            avail = abs(q) - self._held(order["symbol"], order["side"])
            if self.reject_closes:
                self.reject_closes -= 1
                reason = "rejected by the broker (fake)"
            elif float(order["qty"]) > avail + 1e-9:
                reason = f"insufficient qty available for order (requested: {order['qty']}, available: {avail:g})"
        if self.reject_next or reason:
            reason, self.reject_next = (self.reject_next or reason), None
            return {"client_order_id": cid, "broker_order_id": None, "status": "rejected", "qty": order.get("qty"),
                    "filled_qty": 0.0, "avg_price": None, "fees": 0.0, "notional": None, "reject_reason": reason,
                    "ts": None, "symbol": order["symbol"], "side": order["side"], "legs": [], "broker_status": "rejected"}
        self.sends += 1
        oid = str(self.next_id)
        self.next_id += 1
        o = {"id": oid, "cid": cid, "symbol": order["symbol"], "side": order["side"], "qty": float(order["qty"]),
             "filled": 0.0, "avg": None, "fees": 0.0, "status": "accepted", "type": order.get("type") or "market",
             "tif": order.get("tif") or "day", "limit": order.get("limit_price"), "stop": order.get("stop_price")}
        self.orders[oid] = o
        self.by_cid[cid] = o
        if o["type"] == "bracket":
            legs = []
            for kind, price_key in (("limit", "take_profit"), ("stop", "stop_loss")):
                lid = str(self.next_id)
                self.next_id += 1
                spec = order.get(price_key) or {}
                self.orders[lid] = {"id": lid, "cid": f"alpaca-leg-{lid}", "symbol": o["symbol"],
                                    "side": "sell" if o["side"] == "buy" else "cover", "qty": o["qty"], "filled": 0.0,
                                    "avg": None, "fees": 0.0, "status": "held", "type": kind, "tif": o["tif"],
                                    "limit": spec.get("limit_price"), "stop": spec.get("stop_price")}
                legs.append(lid)
            o["legs"] = legs
        if self.auto == "fill" and o["type"] in ("market", "bracket"):
            self.fill(cid)
        elif self.auto == "partial" and o["type"] == "market":
            self.fill(cid, qty=round(o["qty"] * self.partial_frac, 0))
        if self.drop_after_send:
            self.drop_after_send -= 1
            raise unavailable(f"{self.name}: the answer was lost (fake)", TimeoutError("read timeout"), connection=True)
        return self._ack(o)

    def order(self, cid):
        self._check("order")
        o = self.by_cid.get(cid)
        return self._ack(o) if o else None

    def cancel(self, cid, reason="cancelled", broker_order_id=None):
        self._check("cancel")
        o = self.orders.get(str(broker_order_id)) if broker_order_id else self.by_cid.get(cid)
        if o is None or o["status"] not in ("accepted", "partially_filled", "held"):
            return False
        self._cancel(o)
        return True

    def _cancel(self, o):
        if self.cancel_delay:
            o["status"], o["cancel_in"] = "pending_cancel", self.cancel_delay
        else:
            o["status"] = "cancelled"
        for lid in o.get("legs", []):
            if self.orders[lid]["status"] in ("accepted", "held"):
                self.orders[lid]["status"] = "cancelled"

    def cancel_all(self):
        self._check("cancel_all")
        n = 0
        for o in self.orders.values():
            if o["status"] in ("accepted", "partially_filled", "held"):
                self._cancel(o)
                n += 1
        return n

    def account(self, prices=None):
        self._check("account")
        mv = sum(q * self.prices.get(s, a) for s, (q, a) in self.pos.items())
        unreal = sum(q * (self.prices.get(s, a) - a) for s, (q, a) in self.pos.items())
        equity = self.cash + mv
        return {"equity": equity, "cash": self.cash, "cash_basis": self.cash, "buying_power": equity * 2,
                "initial_margin": None, "maintenance_margin": None, "unrealized_pnl": unreal, "status": "ACTIVE",
                "pattern_day_trader": False, "daytrade_count": 0, "multiplier": 2.0, "shorting_enabled": True,
                "trading_blocked": False}

    def positions(self, prices=None):
        self._check("positions")
        out = []
        for s, (q, a) in sorted(self.pos.items()):
            px = self.prices.get(s, a)
            out.append({"symbol": s, "broker_symbol": s,
                        "qty": q, "side": "long" if q > 0 else "short", "avg_price": a, "mark_price": px,
                        "market_value": q * px, "unrealized_pnl": q * (px - a), "liquidation_price": None,
                        "initial_margin": None, "maintenance_margin": None, "leverage": None})
        return out

    def open_orders(self):
        self._check("open_orders")
        out = []
        for o in self.orders.values():
            if o["status"] == "pending_cancel":
                o["cancel_in"] -= 1
                if o["cancel_in"] <= 0:
                    o["status"] = "cancelled"
            if o["status"] in self.OPEN:
                a = self._ack(o)
                out.append({"broker_order_id": o["id"], "client_order_id": o["cid"], "symbol": o["symbol"],
                            "broker_symbol": o["symbol"], "side": o["side"], "qty": o["qty"], "filled_qty": o["filled"],
                            "type": o["type"], "limit_price": o.get("limit"), "stop_price": o.get("stop"),
                            "tif": o["tif"], "status": a["status"], "broker_status": o["status"], "created_at": None,
                            "known": False, "legs": []})
        return out

    def snapshot(self):
        return {"account": self.account(), "positions": self.positions(), "open_orders": self.open_orders(),
                "at": "2026-10-02T15:00:00+00:00"}

    def instrument(self, symbol):
        self._check("instrument")
        return {"symbol": symbol, "broker_symbol": symbol, "ccxt_symbol": None, "tradable": True,
                "fractionable": True, "shortable": True, "easy_to_borrow": True, "min_qty": 1.0, "qty_step": 1.0,
                "tick_size": 0.01, "contract_size": None, "message": None}

    def round_qty(self, symbol, qty):
        return qty


class BrokerCase(TradingCase):
    """TradingCase + fake brokers behind the broker factory + keys in a MemoryStore."""

    def setUp(self):
        super().setUp()
        self.alpaca = FakeBroker("alpaca", cash=100000.0)
        try:
            from sp_binance_testkit import make_binance
        except ImportError:  # pragma: no cover
            from tests.sp_binance_testkit import make_binance
        # binance-demo: the REAL adapter on a fake ccxt exchange (SPEC §18.14)
        self.bstate, self.binance = make_binance()
        self.brokers = {"alpaca-paper": self.alpaca, "binance-demo": self.binance}
        self.asked = []                  # every account id the broker factory was asked for (S10-K: never kraken-demo)
        self.store = secrets.MemoryStore(dict(KEYS))
        secrets.set_store(self.store)
        self.addCleanup(secrets.set_store, None)
        mirror.reset_links()
        self.addCleanup(mirror.reset_links)
        mirror.set_adapter_factory(lambda aid: self.asked.append(aid) or self.brokers[aid])
        self.addCleanup(mirror.set_adapter_factory, None)
        for q in ("SPY", "QQQ", "BTC-USD", "ETH-USD"):
            self.quotes.prices.setdefault(q, self.alpaca.prices[q])

    def connect(self, aid="alpaca-paper"):
        from strategy_platform.execution import accounts
        from strategy_platform.execution.brokers import service
        out = service.connect(aid, service.confirm_phrase(aid), "test connect", "user")
        return accounts.get(aid), out

    def bmanual(self, acc, symbol, side, qty, **kw):
        from strategy_platform.execution import oms
        from strategy_platform.portfolio import ledger
        intent = {"symbol": symbol, "side": side, "qty": qty, "origin": "manual",
                  "source_id": kw.pop("source_id", "_unallocated")}
        intent.update(kw)
        o, _c = oms.place(acc, intent)
        ledger.attribute(acc)
        return oms.get(o["client_order_id"])
