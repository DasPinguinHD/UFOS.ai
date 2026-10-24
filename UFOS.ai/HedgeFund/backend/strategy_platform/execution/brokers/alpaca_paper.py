"""Alpaca PAPER trading adapter (SPEC §18.3) — alpaca-py ``TradingClient(paper=True)``.

- Endpoint: ONLY ``https://paper-api.alpaca.markets`` (``live_guard.paper_endpoint``); the client's real base URL
  is asserted after construction (a live URL → ``LiveTradingDisabled``). Keys ``alpaca-paper-key-id`` /
  ``alpaca-paper-secret-key`` read per call (never logged). Hardened client (no SDK retries, 20 s timeout).
- Every request goes through the ONE shared Alpaca gate (``http_gate.gate_for("alpaca")``, 180/min, normal
  priority, data type ``trading``). Reads are retried by the gate on 429 / 5xx / timeouts; a SEND is never
  retried by the gate: an ambiguous failure (timeout, dropped connection, 5xx, HTTP 422 "client_order_id must be
  unique") is followed by a lookup of the client order id — found → its ack, not found → ``BrokerUnavailable``
  (the OMS re-sends later with the SAME id, looking it up first every time) — so an order is never duplicated.
- Fills by polling the order status (cumulative ``filled_qty`` / ``filled_avg_price``, bracket legs nested); the
  trade-update stream is not used in stage 8. Fees 0 (commission-free; SEC/TAF on paper → verify).
- Rules: whole shares from the platform; fractional / notional only market|limit DAY and never short; extended
  hours not enabled in stage 8; PDT and buying-power refusals come back as ``rejected`` acks with Alpaca's reason.

Part of UFOS.ai, a proof-of-concept portfolio project; PAPER TRADING ONLY.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Callable, List, Optional

from ...core.errors import DataSourceUnavailable, SecretMissing, SecretStoreUnavailable, UserFacingError
from ...data import http_gate
from ...data.http_gate import Retry
from ..errors import BrokerUnavailable
from . import live_guard
from .base import BrokerAdapter
from .common import (BrokerAuthRejected, BrokerFormatError, BrokerNotInstalled, BrokerNotRetryable, EPS, fnum,
                     rejected_ack, unavailable, validate_order_shape)

logger = logging.getLogger(__name__)

BROKER = "alpaca_paper"
GATE_SOURCE = "alpaca"
DATA_TYPE = "trading"
KEY_ID_SECRET = "alpaca-paper-key-id"
SECRET_KEY_SECRET = "alpaca-paper-secret-key"
REQUEST_TIMEOUT_SECONDS = 20.0
AUTH_MESSAGE = "Alpaca rejected the paper keys — check them in Hedge Fund → Settings → Keys."
NOT_INSTALLED = ("alpaca-py is not installed in the HedgeFund backend environment (restart the app so it "
                 "installs requirements.txt)")
STATUS_MAP = {
    "new": "accepted", "accepted": "accepted", "pending_new": "accepted", "accepted_for_bidding": "accepted",
    "calculated": "accepted", "held": "accepted", "pending_replace": "accepted", "replaced": "accepted",
    "pending_cancel": "accepted", "done_for_day": "accepted", "stopped": "accepted",
    "partially_filled": "partially_filled", "filled": "filled", "canceled": "cancelled", "cancelled": "cancelled",
    "expired": "expired", "rejected": "rejected", "suspended": "rejected",
}
OPEN_STATUSES = ("accepted", "partially_filled")
TYPES = ("market", "limit", "stop", "stop_limit", "bracket")
TIFS = ("day", "gtc")
CAPABILITIES = {"order_types": list(TYPES), "tifs": list(TIFS), "fractional": "not used (whole shares)",
                "shorting": "easy-to-borrow only", "crypto_perps": False,
                "hours": "regular NYSE session (extended hours not enabled in stage 8)", "extended_hours": False,
                "bracket": True}
NOTES = ["Pattern-day-trader protection: Alpaca refuses a 4th day trade within 5 business days while equity is "
         "below $25,000.",
         "Alpaca allows up to 4x intraday / 2x overnight buying power; the platform's own check stays Reg T 2x of "
         "its book.",
         "Shorts: easy-to-borrow stocks only (Alpaca refuses hard-to-borrow).",
         "Fees: commission-free; whether paper simulates SEC/TAF fees, dividends and borrow fees — verify (any such "
         "cash change shows up as a reconciliation mismatch)."]
TERMS = {"summary": "Alpaca paper trading account: free, simulated money, under Alpaca's customer terms; the free "
                    "market-data plan is for the account holder's own use (no redistribution).",
         "verify": ["Alpaca's current terms for paper accounts and API use (alpaca.markets/disclosures)",
                    "whether paper simulates regulatory fees, dividends and borrow fees"]}

_SDK = None
_SDK_LOCK = threading.Lock()


def load_sdk() -> SimpleNamespace:
    global _SDK
    from ...data.providers.alpaca import _SDK_LOCK as DATA_LOCK
    with DATA_LOCK:             # the same import lock as the data provider (Windows / 3.14 import deadlock)
        if _SDK is None:
            try:
                from alpaca.trading.client import TradingClient
                from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
                from alpaca.trading.requests import (GetOrderByIdRequest, GetOrdersRequest, LimitOrderRequest,
                                                     MarketOrderRequest, StopLimitOrderRequest, StopLossRequest,
                                                     StopOrderRequest, TakeProfitRequest)
            except ImportError as ex:
                raise BrokerNotInstalled(NOT_INSTALLED) from ex
            _SDK = SimpleNamespace(TradingClient=TradingClient, OrderClass=OrderClass, OrderSide=OrderSide,
                                   QueryOrderStatus=QueryOrderStatus, TimeInForce=TimeInForce,
                                   GetOrderByIdRequest=GetOrderByIdRequest, GetOrdersRequest=GetOrdersRequest,
                                   LimitOrderRequest=LimitOrderRequest, MarketOrderRequest=MarketOrderRequest,
                                   StopLimitOrderRequest=StopLimitOrderRequest, StopLossRequest=StopLossRequest,
                                   StopOrderRequest=StopOrderRequest, TakeProfitRequest=TakeProfitRequest)
        return _SDK


def sdk_info() -> dict:
    try:
        load_sdk()
    except BrokerNotInstalled as ex:
        return {"name": "alpaca-py", "installed": False, "version": None, "message": ex.user_message}
    try:
        from importlib.metadata import version
        v = version("alpaca-py")
    except Exception:      # noqa: BLE001 — informational
        v = None
    return {"name": "alpaca-py", "installed": True, "version": v, "message": None}


# --- error mapping ------------------------------------------------------------------------------------------

def http_status(ex) -> Optional[int]:
    from ...data.providers.alpaca import http_status as hs
    return hs(ex)


def _api_message(ex) -> str:
    from ...data.providers.alpaca import _api_message as am
    return am(ex)


class _NotFound(UserFacingError):
    pass


class _Refused(UserFacingError):
    def __init__(self, status: int, message: str):
        super().__init__(message, detail=f"HTTP {status}")
        self.status = status


class _SendFailed(UserFacingError):
    """A send failed — the caller decides (refusal vs ambiguous) from the original exception."""

    def __init__(self, original: BaseException):
        super().__init__("send failed", detail=type(original).__name__)
        self.original = original


def classify_read(ex: BaseException, data_type: str):
    status = http_status(ex)
    if status is not None:
        msg = _api_message(ex)
        if status in (401, 403):
            return BrokerAuthRejected(AUTH_MESSAGE, detail=f"HTTP {status}")
        if status == 404:
            return _NotFound("not found", detail=msg)
        if status == 429:
            return Retry("Alpaca rate limit reached (HTTP 429)")
        if 500 <= status <= 599:
            return Retry(f"Alpaca server error (HTTP {status})")
        return _Refused(status, f"Alpaca refused the request (HTTP {status}: {msg})")
    names = [c.__name__ for c in type(ex).__mro__]
    if isinstance(ex, TimeoutError) or any("Timeout" in n for n in names):
        return Retry("the request to Alpaca timed out")
    if isinstance(ex, (ValueError, KeyError, TypeError, AttributeError)) and not isinstance(ex, OSError):
        return BrokerFormatError(f"Alpaca answered in an unexpected format — not a connection problem "
                                 f"({type(ex).__name__} while parsing)")
    return unavailable("network/TLS error — paper-api.alpaca.markets could not be reached", ex)


def _classify_send(ex: BaseException, data_type: str):
    return _SendFailed(ex)


def _refusal_text(status: int, msg: str) -> str:
    low = msg.lower()
    if "pattern day" in low:
        return f"Alpaca refused: pattern day trader protection (equity below $25,000) — {msg}"
    if "buying power" in low:
        return f"Alpaca refused: insufficient buying power — {msg}"
    if "not shortable" in low or "hard to borrow" in low or "htb" in low:
        return f"Alpaca refused: not shortable (hard to borrow) — {msg}"
    if "fractional" in low:
        return f"Alpaca refused: fractional rule — {msg}"
    return f"Alpaca refused the order (HTTP {status}: {msg})"


def _raw_dict(v) -> dict:
    if isinstance(v, dict):
        return v
    for attr in ("model_dump", "dict"):
        fn = getattr(v, attr, None)
        if callable(fn):
            d = fn()
            if isinstance(d, dict):
                return d
    raise TypeError(f"expected a JSON object from Alpaca, got {type(v).__name__}")


def _enum_str(v) -> Optional[str]:
    if v is None:
        return None
    v = getattr(v, "value", v)
    return str(v).lower()


def platform_symbol(alpaca_symbol: str) -> str:
    return str(alpaca_symbol or "").upper()


class AlpacaPaperAdapter(BrokerAdapter):
    name = BROKER
    paper = True
    capabilities = CAPABILITIES
    endpoint = live_guard.paper_endpoint(BROKER)

    def __init__(self, *, sdk=None, gate=None, secrets_module=None, timeout: float = REQUEST_TIMEOUT_SECONDS,
                 clock: Optional[Callable[[], datetime]] = None):
        self._sdk = sdk
        self._gate_obj = gate
        self._secrets = secrets_module
        self.timeout = float(timeout)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # --- plumbing -------------------------------------------------------------------------------------

    def _load(self):
        return self._sdk if self._sdk is not None else load_sdk()

    def _gate(self):
        return self._gate_obj if self._gate_obj is not None else http_gate.gate_for(GATE_SOURCE)

    def _keys(self):
        sec = self._secrets
        if sec is None:
            from ... import secrets as sec
        return sec.require_secret(KEY_ID_SECRET), sec.require_secret(SECRET_KEY_SECRET)

    def client(self):
        """A hardened TradingClient on the PAPER endpoint (asserted)."""
        from ...data.providers.alpaca import harden_client
        sdk = self._load()
        key_id, secret = self._keys()
        c = sdk.TradingClient(api_key=key_id, secret_key=secret, paper=True, raw_data=True)
        base = getattr(c, "_base_url", None)
        base = getattr(base, "value", base)
        live_guard.assert_paper_url(BROKER, base)
        return harden_client(c, self.timeout, on_send=getattr(self._gate(), "note_send", None))

    def check_endpoint(self) -> str:
        """Builds the client (keys + the paper URL assertion) without a request."""
        self.client()
        return self.endpoint

    def _read(self, fn, what: str):
        try:
            return self._gate().call(fn, data_type=DATA_TYPE, classify=classify_read)
        except (BrokerUnavailable, BrokerAuthRejected, _NotFound, _Refused, SecretMissing, SecretStoreUnavailable,
                live_guard.LiveTradingDisabled, BrokerNotInstalled, BrokerNotRetryable):
            raise
        except DataSourceUnavailable as ex:
            raise unavailable(f"Alpaca paper: {what} failed — {ex.reason}", ex,
                              connection=getattr(ex, "kind", None) == "connection") from ex

    # --- parsing -------------------------------------------------------------------------------------

    def _ack(self, raw, cid: Optional[str] = None) -> dict:
        r = _raw_dict(raw)
        st_raw = _enum_str(r.get("status")) or ""
        st = STATUS_MAP.get(st_raw)
        if st is None:
            raise TypeError(f"unknown Alpaca order status {st_raw!r}")
        filled = fnum(r.get("filled_qty")) or 0.0
        avg = fnum(r.get("filled_avg_price"))
        legs = []
        for leg in r.get("legs") or []:
            lr = _raw_dict(leg)
            lst = STATUS_MAP.get(_enum_str(lr.get("status")) or "", "accepted")
            ltype = _enum_str(lr.get("type") or lr.get("order_type")) or "market"
            legs.append({"client_order_id": lr.get("client_order_id"), "broker_order_id": str(lr.get("id")),
                         "type": "stop" if ltype in ("stop", "stop_limit") else ltype,
                         "side": _enum_str(lr.get("side")), "qty": fnum(lr.get("qty")), "status": lst,
                         "filled_qty": fnum(lr.get("filled_qty")) or 0.0,
                         "avg_price": fnum(lr.get("filled_avg_price")), "fees": 0.0,
                         "broker_status": _enum_str(lr.get("status")),
                         "limit_price": fnum(lr.get("limit_price")), "stop_price": fnum(lr.get("stop_price"))})
        reason = None
        if st == "rejected":
            reason = f"Alpaca rejected the order ({st_raw})"
        elif st == "cancelled":
            reason = "cancelled at Alpaca"
        elif st == "expired":
            reason = "expired at Alpaca"
        return {"client_order_id": r.get("client_order_id") or cid, "broker_order_id": str(r.get("id")),
                "status": st, "qty": fnum(r.get("qty")), "filled_qty": filled, "avg_price": avg, "fees": 0.0,
                "notional": (filled * avg) if (avg is not None and filled > 0) else None, "reject_reason": reason,
                "ts": r.get("filled_at") or r.get("updated_at") or r.get("submitted_at"),
                "symbol": platform_symbol(r.get("symbol")), "side": _enum_str(r.get("side")),
                "type": _enum_str(r.get("type") or r.get("order_type")), "legs": legs, "broker_status": st_raw,
                "tif": _enum_str(r.get("time_in_force")), "limit_price": fnum(r.get("limit_price")),
                "stop_price": fnum(r.get("stop_price")), "created_at": r.get("created_at"),
                "order_class": _enum_str(r.get("order_class"))}

    @staticmethod
    def _order_view(a: dict, known: bool = False) -> dict:
        return {"broker_order_id": a["broker_order_id"], "client_order_id": a["client_order_id"],
                "symbol": a["symbol"], "broker_symbol": a["symbol"], "side": a["side"], "qty": a["qty"],
                "filled_qty": a["filled_qty"], "type": a.get("type"), "limit_price": a.get("limit_price"),
                "stop_price": a.get("stop_price"), "tif": a.get("tif"), "status": a["status"],
                "broker_status": a.get("broker_status"), "created_at": a.get("created_at"), "known": known,
                "legs": [l.get("broker_order_id") for l in a.get("legs") or []]}

    # --- interface: reads -------------------------------------------------------------------------------

    def _lookup(self, cid: str) -> Optional[dict]:
        sdk = self._load()
        c = self.client()
        try:
            raw = self._read(lambda: c.get_order_by_client_id(cid), "order lookup")
        except _NotFound:
            return None
        ack = self._ack(raw, cid)
        if ack.get("order_class") == "bracket" and not ack["legs"]:
            req = sdk.GetOrderByIdRequest(nested=True)
            raw = self._read(lambda: c.get_order_by_id(ack["broker_order_id"], filter=req), "order lookup")
            ack = self._ack(raw, cid)
        return ack

    def order(self, client_order_id: str) -> Optional[dict]:
        return self._lookup(client_order_id)

    def account(self, prices: Optional[dict] = None) -> dict:
        c = self.client()
        r = _raw_dict(self._read(lambda: c.get_account(), "account"))
        cash = fnum(r.get("cash"))
        if cash is None or fnum(r.get("equity")) is None:
            raise BrokerFormatError("Alpaca paper: the account has no cash/equity value (unexpected response)")
        return {"equity": fnum(r.get("equity")), "cash": cash, "cash_basis": cash,
                "buying_power": fnum(r.get("buying_power")), "initial_margin": fnum(r.get("initial_margin")),
                "maintenance_margin": fnum(r.get("maintenance_margin")), "unrealized_pnl": None,
                "status": _enum_str(r.get("status")) and str(_enum_str(r.get("status"))).upper(),
                "pattern_day_trader": bool(r.get("pattern_day_trader")) if r.get("pattern_day_trader") is not None
                else None,
                "daytrade_count": int(r["daytrade_count"]) if fnum(r.get("daytrade_count")) is not None else None,
                "multiplier": fnum(r.get("multiplier")),
                "shorting_enabled": bool(r.get("shorting_enabled")) if r.get("shorting_enabled") is not None else None,
                "trading_blocked": bool(r.get("trading_blocked") or r.get("account_blocked"))}

    def positions(self, prices: Optional[dict] = None) -> List[dict]:
        c = self.client()
        rows = self._read(lambda: c.get_all_positions(), "positions")
        if not isinstance(rows, list):
            raise BrokerFormatError("Alpaca paper: positions are not a list (unexpected response)")
        out = []
        for raw in rows:
            r = _raw_dict(raw)
            q = fnum(r.get("qty"))
            if q is None:
                raise BrokerFormatError("Alpaca paper: a position without quantity (unexpected response)")
            side = _enum_str(r.get("side")) or ("short" if q < 0 else "long")
            qty = -abs(q) if side == "short" else abs(q)
            out.append({"symbol": platform_symbol(r.get("symbol")), "broker_symbol": r.get("symbol"), "qty": qty,
                        "side": side, "avg_price": fnum(r.get("avg_entry_price")),
                        "mark_price": fnum(r.get("current_price")), "market_value": fnum(r.get("market_value")),
                        "unrealized_pnl": fnum(r.get("unrealized_pl")), "liquidation_price": None,
                        "initial_margin": None, "maintenance_margin": None, "leverage": None})
        return out

    def open_orders(self) -> List[dict]:
        sdk = self._load()
        c = self.client()
        req = sdk.GetOrdersRequest(status=sdk.QueryOrderStatus.OPEN, limit=500, nested=True)
        rows = self._read(lambda: c.get_orders(filter=req), "open orders")
        if not isinstance(rows, list):
            raise BrokerFormatError("Alpaca paper: open orders are not a list (unexpected response)")
        out = []
        for raw in rows:
            a = self._ack(raw)
            out.append(self._order_view(a))
            for leg in a["legs"]:
                if leg["status"] in OPEN_STATUSES:
                    out.append({"broker_order_id": leg["broker_order_id"], "client_order_id": leg["client_order_id"],
                                "symbol": a["symbol"], "broker_symbol": a["symbol"], "side": leg["side"],
                                "qty": leg["qty"], "filled_qty": leg["filled_qty"], "type": leg["type"],
                                "limit_price": leg.get("limit_price"), "stop_price": leg.get("stop_price"),
                                "tif": a.get("tif"), "status": leg["status"], "broker_status": leg["broker_status"],
                                "created_at": a.get("created_at"), "known": False, "legs": [],
                                "parent_broker_order_id": a["broker_order_id"]})
        return out

    def snapshot(self) -> dict:
        acct = self.account()
        pos = self.positions()
        oo = self.open_orders()
        return {"account": acct, "positions": pos, "open_orders": oo,
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    def instrument(self, symbol: str) -> dict:
        c = self.client()
        try:
            r = _raw_dict(self._read(lambda: c.get_asset(symbol), "asset"))
        except _NotFound:
            return {"symbol": symbol, "broker_symbol": symbol, "ccxt_symbol": None, "tradable": False,
                    "fractionable": None, "shortable": None, "easy_to_borrow": None, "min_qty": None,
                    "qty_step": None, "tick_size": None, "contract_size": None,
                    "message": f"{symbol} is not an Alpaca asset"}
        return {"symbol": symbol, "broker_symbol": r.get("symbol") or symbol, "ccxt_symbol": None,
                "tradable": bool(r.get("tradable")), "fractionable": r.get("fractionable"),
                "shortable": r.get("shortable"), "easy_to_borrow": r.get("easy_to_borrow"), "min_qty": 1.0,
                "qty_step": 1.0, "tick_size": fnum(r.get("price_increment")), "contract_size": None,
                "message": None if r.get("tradable") else f"{symbol} is not tradable at Alpaca"}

    # --- interface: writes ------------------------------------------------------------------------------

    def _request(self, order: dict):
        sdk = self._load()
        t = order.get("type") or "market"
        side = sdk.OrderSide.BUY if order["side"] in ("buy", "cover") else sdk.OrderSide.SELL
        tif = sdk.TimeInForce.GTC if (order.get("tif") or "day") == "gtc" else sdk.TimeInForce.DAY
        kw = {"symbol": order["symbol"], "side": side, "time_in_force": tif,
              "client_order_id": order["client_order_id"]}
        qty, notional = fnum(order.get("qty")), fnum(order.get("notional"))
        if qty is not None and qty > 0:
            kw["qty"] = qty
        elif notional is not None and notional > 0:
            kw["notional"] = round(notional, 2)
        if t == "market":
            return sdk.MarketOrderRequest(**kw)
        if t == "limit":
            return sdk.LimitOrderRequest(limit_price=float(order["limit_price"]), **kw)
        if t == "stop":
            return sdk.StopOrderRequest(stop_price=float(order["stop_price"]), **kw)
        if t == "stop_limit":
            return sdk.StopLimitOrderRequest(stop_price=float(order["stop_price"]),
                                             limit_price=float(order["limit_price"]), **kw)
        tp = sdk.TakeProfitRequest(limit_price=float(order["take_profit"]["limit_price"]))
        sl_d = order["stop_loss"]
        sl = sdk.StopLossRequest(stop_price=float(sl_d["stop_price"]), limit_price=fnum(sl_d.get("limit_price")))
        entry = {"order_class": sdk.OrderClass.BRACKET, "take_profit": tp, "stop_loss": sl}
        if fnum(order.get("limit_price")):
            return sdk.LimitOrderRequest(limit_price=float(order["limit_price"]), **kw, **entry)
        return sdk.MarketOrderRequest(**kw, **entry)

    def _shape_refusal(self, order: dict) -> Optional[str]:
        from strategy_platform.core import params as fund_params
        if fund_params.is_crypto(order.get("symbol")):
            return "crypto is not traded on alpaca-paper (crypto goes to binance-demo)"
        why = validate_order_shape(order, types=TYPES, tifs=TIFS)
        if why:
            return why
        qty, notional = fnum(order.get("qty")), fnum(order.get("notional"))
        t = order.get("type") or "market"
        if order.get("extended_hours"):
            return "extended-hours orders are not enabled in stage 8"
        if qty is None and notional is not None:
            if t != "market" or (order.get("tif") or "day") != "day" or order["side"] in ("short",):
                return "notional orders are only allowed as market DAY orders and never short"
        elif qty is not None and abs(qty - round(qty)) > EPS:
            if t not in ("market", "limit") or (order.get("tif") or "day") != "day" or order["side"] == "short":
                return "fractional quantities are only allowed as market/limit DAY orders and never short"
        elif qty is None or qty <= 0:
            return "a quantity (or a notional) is required"
        return None

    def submit(self, order: dict) -> dict:
        live_guard.require_paper_order(BROKER, order)
        cid = order["client_order_id"]
        why = self._shape_refusal(order)
        if why:
            return rejected_ack(order, why)
        try:
            known = self._lookup(cid)               # idempotent: never send an id twice
        except (BrokerAuthRejected, SecretMissing, SecretStoreUnavailable, BrokerNotInstalled) as ex:
            return rejected_ack(order, getattr(ex, "user_message", str(ex)))
        if known is not None:
            return known
        req = self._request(order)
        c = self.client()
        try:
            raw = self._gate().call(lambda: c.submit_order(order_data=req), data_type=DATA_TYPE,
                                    classify=_classify_send)
            return self._ack(raw, cid)
        except _SendFailed as ex:
            orig = ex.original
            status = http_status(orig)
            if status is not None and 400 <= status < 500 and status not in (408, 409, 429):
                msg = _api_message(orig)
                if status == 401:
                    return rejected_ack(order, AUTH_MESSAGE)
                if not (status == 422 and "client_order_id" in msg.lower()):
                    return rejected_ack(order, _refusal_text(status, msg))
            why_amb = f"HTTP {status}" if status is not None else type(orig).__name__
            logger.warning("Alpaca paper submit %s ambiguous (%s) — looking the id up", cid, why_amb)
        except DataSourceUnavailable as ex:          # the gate could not even start the send (rate-limit wait)
            raise unavailable(f"Alpaca paper: the order could not be sent ({ex.reason})", ex) from ex
        found = self._lookup(cid)                   # ambiguous: did it reach Alpaca?
        if found is not None:
            return found
        raise unavailable("Alpaca paper: the order did not reach the broker (connection) — it is re-sent with the "
                          "same id after a lookup", connection=True)

    def cancel(self, client_order_id: str, reason: str = "cancelled", broker_order_id: Optional[str] = None) -> bool:
        ack = self._lookup(client_order_id) if broker_order_id is None else None
        oid = broker_order_id or (ack or {}).get("broker_order_id")
        if oid is None:
            return False
        if ack is not None and ack["status"] not in OPEN_STATUSES:
            return False
        c = self.client()
        try:
            self._read(lambda: c.cancel_order_by_id(oid), "cancel")
        except (_NotFound, _Refused):
            return False
        return True

    def cancel_all(self) -> int:
        c = self.client()
        res = self._read(lambda: c.cancel_orders(), "cancel all")
        return len(res) if isinstance(res, list) else 0

    def fills(self, after: int = 0, limit: int = 500) -> List[dict]:
        return []           # fills are derived from order status (mirror.book_ack); see the module docstring
