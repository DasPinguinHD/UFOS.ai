"""A fake ccxt ``binance`` exchange for the stage-8b tests (SPEC §18.14) — offline, no SDK, no network.

It mirrors the parts of ccxt 4.5.84 the adapter relies on: ``urls['api']`` (live) / ``urls['demo']`` and
``enable_demo_trading``; every call builds its URL from ``urls['api'][<api>]`` (a missing API → ``NotSupported``,
like ccxt's ``sign``) and goes through ``self.fetch`` (so the adapter's per-request URL guard applies); spot
balances (free / locked), market + limit orders with ``newClientOrderId``, fills with a commission in the quote
asset, the bought asset or BNB, trades per order, cancels, foreign orders, outages and lost answers.
Part of UFOS.ai (paper/demo only)."""
import json
from types import SimpleNamespace

from strategy_platform.data.http_gate import HttpGate
from strategy_platform.execution.brokers import binance_demo


class CcxtError(Exception):
    pass


class NetworkError(CcxtError):
    pass


class RequestTimeout(NetworkError):
    pass


class ExchangeError(CcxtError):
    pass


class AuthenticationError(ExchangeError):
    pass


class InvalidOrder(ExchangeError):
    pass


class OrderNotFound(InvalidOrder):
    pass


class DuplicateOrderId(InvalidOrder):
    pass


class InsufficientFunds(ExchangeError):
    pass


class NotSupported(ExchangeError):
    pass


LIVE_API = {"public": "https://api.binance.com/api/v3", "private": "https://api.binance.com/api/v3",
            "v1": "https://api.binance.com/api/v1", "sapi": "https://api.binance.com/sapi/v1",
            "fapiPublic": "https://fapi.binance.com/fapi/v1"}
DEMO_API = {"public": "https://demo-api.binance.com/api/v3", "private": "https://demo-api.binance.com/api/v3",
            "v1": "https://demo-api.binance.com/api/v1", "fapiPublic": "https://demo-fapi.binance.com/fapi/v1",
            "dapiPublic": "https://demo-dapi.binance.com/dapi/v1"}


def market(base, step, tick, min_cost=5.0, market_max=None, quote="USDT", spot=True):
    filters = [{"filterType": "LOT_SIZE", "stepSize": str(step), "minQty": str(step), "maxQty": "9000"},
               {"filterType": "PRICE_FILTER", "tickSize": str(tick)}, {"filterType": "NOTIONAL",
                                                                       "minNotional": str(min_cost)}]
    if market_max:
        filters.append({"filterType": "MARKET_LOT_SIZE", "maxQty": str(market_max)})
    sym = f"{base}/{quote}" + ("" if spot else f":{quote}")
    return {"id": f"{base}{quote}", "symbol": sym, "base": base, "quote": quote, "spot": spot, "active": True,
            "precision": {"amount": step, "price": tick},
            "limits": {"amount": {"min": step, "max": 9000.0}, "price": {"min": tick, "max": 1_000_000.0},
                       "cost": {"min": min_cost, "max": 9_000_000.0}},
            "info": {"filters": filters}}


MARKETS = {"BTC/USDT": market("BTC", 0.00001, 0.01, market_max=50.0), "ETH/USDT": market("ETH", 0.0001, 0.01),
           "SOL/USDT": market("SOL", 0.001, 0.01), "BTC/USDC": market("BTC", 0.00001, 0.01, quote="USDC"),
           "BTC/USDT:USDT": market("BTC", 0.001, 0.1, spot=False)}


class State:
    """The simulated demo account behind every FakeBinance instance of one test."""

    def __init__(self, balances=None, prices=None):
        self.balances = {k: [float(v), 0.0] for k, v in (balances or {"USDT": 5000.0, "BTC": 0.05, "ETH": 1.0,
                                                                         "BNB": 2.0}).items()}
        self.prices = dict(prices or {"BTC/USDT": 60000.0, "ETH/USDT": 3000.0, "SOL/USDT": 150.0})
        self.orders = {}
        self.trades = {}
        self.next_id = 1000
        self.fee_rate = 0.001
        self.buy_fee_asset = "base"           # base | quote | BNB
        self.fill = "market"                  # market: market orders fill, limits rest | none: nothing fills
        self.down = False
        self.auth_bad = False
        self.drop_after_send = 0
        self.sent = []                        # (method, url) of every request that went out
        self.calls = []
        self.time_ms = 1_792_404_000_000         # 2026-10-19 10:00 UTC

    def bal(self, a):
        return self.balances.setdefault(a, [0.0, 0.0])

    def add_foreign(self, symbol="BTC/USDT", side="buy", qty=0.001, price=50000.0, cid="web_abc123"):
        return self._new(symbol, "limit", side, qty, price, cid)

    def _new(self, symbol, typ, side, qty, price, cid):
        self.next_id += 1
        oid = str(self.next_id)
        o = {"id": oid, "clientOrderId": cid, "symbol": symbol, "type": typ, "side": side, "amount": qty,
             "filled": 0.0, "average": None, "price": price, "status": "open", "timestamp": self.time_ms,
             "datetime": "2026-10-19T10:00:00.000Z", "lastTradeTimestamp": None,
             "info": {"status": "NEW", "symbol": symbol.replace("/", "")}}
        self.orders[oid] = o
        base, quote = symbol.split("/")
        if typ == "limit":                 # lock what the order needs
            if side == "buy":
                b = self.bal(quote)
                b[0] -= qty * price
                b[1] += qty * price
            else:
                b = self.bal(base)
                b[0] -= qty
                b[1] += qty
        return o

    def fill_order(self, oid, qty=None, price=None):
        o = self.orders[oid]
        base, quote = o["symbol"].split("/")
        q = qty if qty is not None else o["amount"] - o["filled"]
        px = price or (o["price"] if o["type"] == "limit" else self.prices[o["symbol"]])
        if o["type"] == "limit":            # release the lock first
            if o["side"] == "buy":
                b = self.bal(quote)
                b[1] -= q * o["price"]
                b[0] += q * o["price"]
            else:
                b = self.bal(base)
                b[1] -= q
                b[0] += q
        notional = q * px
        if o["side"] == "buy":
            self.bal(quote)[0] -= notional
            if self.buy_fee_asset == "base":
                fee, cur = q * self.fee_rate, base
                self.bal(base)[0] += q - fee
            else:
                fee, cur = (notional * self.fee_rate, quote) if self.buy_fee_asset == "quote" else (0.01, "BNB")
                self.bal(base)[0] += q
                self.bal(cur)[0] -= fee
        else:
            self.bal(base)[0] -= q
            fee, cur = notional * self.fee_rate, quote
            self.bal(quote)[0] += notional - fee
        prev = o["filled"]
        o["filled"] = prev + q
        o["average"] = ((o["average"] or 0.0) * prev + px * q) / o["filled"]
        o["lastTradeTimestamp"] = self.time_ms
        done = o["filled"] >= o["amount"] - 1e-12
        o["status"] = "closed" if done else "open"
        o["info"]["status"] = "FILLED" if done else "PARTIALLY_FILLED"
        self.trades.setdefault(oid, []).append({"order": oid, "symbol": o["symbol"], "amount": q, "price": px,
                                                "fee": {"cost": fee, "currency": cur}})


class FakeBinance:
    """ccxt.binance stand-in (constructor takes the config dict like ccxt)."""

    state: State = None

    def __init__(self, cfg):
        self.cfg = dict(cfg)
        self.apiKey, self.secret = cfg.get("apiKey"), cfg.get("secret")
        self.options = dict(cfg.get("options") or {})
        self.urls = {"api": dict(LIVE_API), "demo": dict(DEMO_API)}
        self.isSandboxModeEnabled = False
        self.precisionMode = 4                # ccxt TICK_SIZE (binance in ccxt 4.5.84)
        self.has = {"fetchMyTrades": True}

    def enable_demo_trading(self, enable):
        if enable:
            self.urls["apiBackupDemoTrading"] = self.urls["api"]
            self.urls["api"] = self.urls["demo"]
        self.options["enableDemoTrading"] = enable

    def fetch(self, url, method="GET", headers=None, body=None):
        self.state.sent.append((method, url))
        return "{}"

    def _req(self, api, path, method="GET"):
        st = self.state
        st.calls.append(path)
        if api not in self.urls["api"]:
            raise NotSupported(f"binance does not have a testnet/sandbox URL for {api} endpoints")
        self.fetch(self.urls["api"][api] + "/" + path, method)
        if st.down:
            raise NetworkError("connection reset (fake)")
        if st.auth_bad and api == "private":
            raise AuthenticationError("Invalid API-key, IP, or permissions for action")

    # --- public
    def fetch_time(self, params=None):
        self._req("public", "time")
        return self.state.time_ms

    def load_markets(self, reload=False, params=None):
        self._req("public", "exchangeInfo")
        types = (self.options.get("fetchMarkets") or {}).get("types") or ["spot", "linear"]
        return {k: json.loads(json.dumps(v)) for k, v in MARKETS.items() if (v["spot"] and "spot" in types) or
                (not v["spot"] and "linear" in types)}

    def fetch_ticker(self, symbol, params=None):
        self._req("public", "ticker/24hr")
        return {"symbol": symbol, "last": self.state.prices.get(symbol)}

    # --- private
    def fetch_balance(self, params=None):
        self._req("private", "account")
        b = self.state.balances
        return {"info": {}, "free": {a: v[0] for a, v in b.items()}, "used": {a: v[1] for a, v in b.items()},
                "total": {a: v[0] + v[1] for a, v in b.items()}}

    def fetch_open_orders(self, symbol=None, since=None, limit=None, params=None):
        self._req("private", "openOrders")
        return [json.loads(json.dumps(o)) for o in self.state.orders.values() if o["status"] == "open"]

    def _find(self, id, params):
        cid = (params or {}).get("origClientOrderId")
        for o in self.state.orders.values():
            if (id is not None and o["id"] == str(id)) or (id is None and cid and o["clientOrderId"] == cid):
                return o
        raise OrderNotFound("Order does not exist.")

    def fetch_order(self, id, symbol=None, params=None):
        self._req("private", "order")
        return json.loads(json.dumps(self._find(id, params)))

    def create_order(self, symbol, type, side, amount, price=None, params=None):
        st = self.state
        self._req("private", "order", "POST")
        cid = (params or {}).get("newClientOrderId")
        if any(o["clientOrderId"] == cid for o in st.orders.values()):
            raise DuplicateOrderId("Duplicate order sent.")
        base, quote = symbol.split("/")
        need = amount * (price or st.prices[symbol])
        if side == "buy" and st.bal(quote)[0] < need - 1e-9:
            raise InsufficientFunds("Account has insufficient balance for requested action.")
        if side == "sell" and st.bal(base)[0] < amount - 1e-12:
            raise InsufficientFunds("Account has insufficient balance for requested action.")
        o = st._new(symbol, type, side, amount, price, cid)
        if type == "market" and st.fill == "market":
            st.fill_order(o["id"])
        if st.drop_after_send > 0:
            st.drop_after_send -= 1
            raise RequestTimeout("binance POST https://demo-api.binance.com/api/v3/order timed out")
        return json.loads(json.dumps(o))

    def cancel_order(self, id, symbol=None, params=None):
        st = self.state
        self._req("private", "order", "DELETE")
        o = self._find(id, params)
        if o["status"] != "open":
            raise OrderNotFound("Unknown order sent.")
        base, quote = o["symbol"].split("/")
        rest = o["amount"] - o["filled"]
        if o["type"] == "limit":
            if o["side"] == "buy":
                b = st.bal(quote)
                b[1] -= rest * o["price"]
                b[0] += rest * o["price"]
            else:
                b = st.bal(base)
                b[1] -= rest
                b[0] += rest
        o["status"] = "canceled"
        o["info"]["status"] = "CANCELED"
        return json.loads(json.dumps(o))

    def fetch_my_trades(self, symbol=None, since=None, limit=None, params=None):
        self._req("private", "myTrades")
        return [dict(t) for t in self.state.trades.get(str((params or {}).get("orderId")), [])]


def fast_gate(name="binance-test"):
    return HttpGate(name, 6000, capacity=100, sleep=lambda s: None)


def make_binance(state=None, **kw):
    """(state, adapter): the REAL BinanceDemoAdapter on the fake exchange."""
    st = state or State()

    class Bound(FakeBinance):
        pass
    Bound.state = st
    sdk = SimpleNamespace(binance=Bound, __version__="4.5.84")
    return st, binance_demo.BinanceDemoAdapter(sdk=sdk, gate_obj=fast_gate(), **kw)
