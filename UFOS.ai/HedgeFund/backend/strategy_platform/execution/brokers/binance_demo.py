"""Binance Spot DEMO MODE adapter (SPEC §18.14) — ccxt ``binance`` with ``enable_demo_trading(True)``.

- ccxt is the pinned optional package (``ccxt==4.5.84``, requirements-broker.txt), imported lazily
  (``ccxt_support.load_sdk``); not installed → ``BrokerNotInstalled``, shown as the broker's state.
- Endpoint: ccxt 4.5.84 maps demo mode to ``urls['demo']`` (source read 2026-10-19): spot ``public`` / ``private`` →
  ``https://demo-api.binance.com/api/v3``, ``v1`` → ``…/api/v1``. ``set_sandbox_mode`` (Testnet) is NEVER used.
  ``urls['api']`` is cut down to those three entries (ccxt's ``sign`` raises ``NotSupported`` for any other API —
  sapi / futures — before a request) and each is asserted on ``demo-api.binance.com``; every request's full URL is
  asserted again before it is sent (``ccxt_support.harden_exchange``). Own gate ``binance_demo`` (60/min, burst 5);
  reads retried on 429/5xx/timeouts, sends never retried by the gate (looked up by ``origClientOrderId`` first).
- Keys: ``binance-demo-api-key`` / ``binance-demo-api-secret`` (Credential Manager), read per call, HMAC only.
- Spot, LONG ONLY, quote USDT: ``BASE-USD`` ↔ ``BASE/USDT`` ↔ ``BASEUSDT``; amounts stay USDT (never converted).
- The account's "positions" are balance − BASELINE per asset (the starting balances the user confirmed at the
  first connect, ``bind_baseline``); before the first connect there are none. Pre-funded holdings are never traded.
- Orders: market + limit (GTC); ``newClientOrderId = "UFOS_" + client_order_id``; filters checked before sending
  (LOT_SIZE, MARKET_LOT_SIZE, PRICE_FILTER, NOTIONAL / MIN_NOTIONAL — the refusal names the filter). Fees from the
  order's trades: USDT → fee; base asset (buys) → net quantity + fee valued at the fill price; other (BNB) → not
  booked (the reconciliation reports the change).

Part of UFOS.ai, a proof-of-concept portfolio project; DEMO TRADING ONLY (simulated funds).
"""
from __future__ import annotations

import logging
import math
import threading
import time as _time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from ...core.errors import DataSourceUnavailable, SecretMissing, SecretStoreUnavailable, UserFacingError
from ...data.http_gate import HttpGate, Retry
from ..errors import BrokerUnavailable
from . import ccxt_support, live_guard
from .base import BrokerAdapter
from .common import (BrokerAuthRejected, BrokerFormatError, BrokerNotInstalled, BrokerNotRetryable, BrokerSetupError,
                     EPS, fnum, rejected_ack, unavailable, validate_order_shape)

logger = logging.getLogger(__name__)

BROKER = "binance_demo"
ACCOUNT_ID = "binance-demo"
LABEL = "Binance Spot demo"
GATE_SOURCE = "binance_demo"
DATA_TYPE = "trading"
RATE_PER_MINUTE = 60
CAPACITY = 5
KEY_SECRET = "binance-demo-api-key"
SECRET_SECRET = "binance-demo-api-secret"
TIMEOUT_MS = 20000
MARKETS_TTL_SECONDS = 6 * 3600
PRICE_TTL_SECONDS = 60.0
PREFIX = "UFOS_"
MAX_CLIENT_ID = 36                       # Binance newClientOrderId: ^[.A-Z:/a-z0-9_-]{1,36}$
QUOTE = "USDT"
CURRENCY_NOTE = "USDT — not converted"
CHIP = "DEMO · SIMULATED FUNDS"
DECIMAL_PLACES, TICK_SIZE = 2, 4        # ccxt.base.decimal_to_precision constants (ccxt 4.5.84)
SPOT_APIS = ("public", "private", "v1")
AUTH_MESSAGE = ("Binance Spot demo rejected the API keys — check them in Hedge Fund → Settings → Keys (keys from "
                "demo.binance.com → API Management).")
HMAC_ONLY = ("HMAC keys only — the Binance demo secret looks like a private key (RSA / Ed25519); create a "
             "system-generated (HMAC) API key at demo.binance.com → API Management")
FORMAT_MSG = "Binance answered in an unexpected format — not a connection problem"
TYPES = ("market", "limit")
TIFS = ("gtc",)
CAPABILITIES = {"order_types": list(TYPES), "tifs": list(TIFS), "fractional": "LOT_SIZE step of each market",
                "shorting": False, "crypto_perps": False, "hours": "24/7", "extended_hours": False, "bracket": False,
                "long_only": True, "currency": QUOTE}
NOTES = ["Spot only, LONG ONLY: no short, no margin, no leverage — a short (or a sell below 0) is blocked, never "
         "converted.",
         "Quote currency USDT (BASE-USD → BASE/USDT); the account's amounts are USDT — not converted to USD.",
         "Baseline: the starting balances confirmed at the first connect are not the platform's; its positions are "
         "the changes since then. A balance reset in the demo UI shows as a reconciliation mismatch.",
         "Orders: market and limit (GTC); Binance's filters (LOT_SIZE, PRICE_FILTER, NOTIONAL) are checked before "
         "sending; only orders with the UFOS_ prefix are the platform's (others are reported as untouched).",
         "Fees: from the order's trades — USDT, or the bought asset (booked net). Switch off \"pay fees with BNB\" in "
         "the demo: BNB fees are not booked (verify)."]
TERMS = {"summary": "Binance Spot Demo Mode (demo.binance.com): simulated funds, prices similar to the live exchange, "
                    "same filters and limits as live; the balance can be reset in the UI. Spot only — Binance stopped "
                    "derivatives for German users in 2021.",
         "verify": ["Binance's terms for Demo Mode and its API",
                    "fee reporting on the demo (commission asset; BNB fee payment switched off)",
                    "MiCA / German availability of Binance spot before any live use (stage 12)"],
         "sources": ["https://developers.binance.com/en/docs/products/spot/demo-mode/general-info",
                     "https://github.com/ccxt/ccxt/issues/27039"]}
OPEN_CCXT = ("open",)
BASELINE_MISSING = ("binance-demo: baseline missing — confirm the starting balances (Settings → Brokers → Confirm starting "
                    "balances…); never assumed empty")

_GATE: Optional[HttpGate] = None
_GATE_LOCK = threading.Lock()


def load_sdk():
    return ccxt_support.load_sdk("Binance")


def sdk_info() -> dict:
    return ccxt_support.sdk_info("Binance")


def gate() -> HttpGate:
    global _GATE
    with _GATE_LOCK:
        if _GATE is None:
            _GATE = HttpGate(GATE_SOURCE, RATE_PER_MINUTE, capacity=CAPACITY)
        return _GATE


# --- symbols / ids ------------------------------------------------------------------------------------------

def ccxt_symbol(symbol: str) -> str:
    """'BTC-USD' -> 'BTC/USDT' (ValueError for anything that is not BASE-USD crypto)."""
    from strategy_platform.core import params as fund_params
    s = str(symbol or "").strip().upper()
    if not fund_params.is_crypto(s):
        raise ValueError(f"{symbol!r} is not a crypto symbol (BASE-USD)")
    return f"{s[:-4]}/{QUOTE}"


def binance_id(symbol: str) -> str:
    return ccxt_symbol(symbol).replace("/", "")


def platform_symbol(ccxt_sym) -> Optional[str]:
    """'BTC/USDT' -> 'BTC-USD'; None for anything else (another quote, a derivative)."""
    from strategy_platform.core import params as fund_params
    s = str(ccxt_sym or "").upper()
    if ":" in s or not s.endswith(f"/{QUOTE}"):
        return None
    sym = f"{s.split('/')[0]}-USD"
    return sym if fund_params.is_crypto(sym) else None


def asset_symbol(asset: str) -> Optional[str]:
    from strategy_platform.core import params as fund_params
    sym = f"{str(asset or '').upper()}-USD"
    return sym if fund_params.is_crypto(sym) else None


def to_broker_id(cid: str) -> str:
    out = f"{PREFIX}{cid}"
    if len(out) > MAX_CLIENT_ID:
        raise ValueError(f"client order id too long for Binance ({len(out)} > {MAX_CLIENT_ID})")
    return out


def from_broker_id(coid) -> Optional[str]:
    s = str(coid or "")
    return s[len(PREFIX):] if s.startswith(PREFIX) and len(s) > len(PREFIX) else None


def step_round_down(qty: float, step: Optional[float]) -> float:
    """``qty`` rounded DOWN to a multiple of ``step`` — in decimal arithmetic (8999 at step 0.00001 stays 8999; binary
    floats made it 8998.99999, review fix 2026-10-19)."""
    from decimal import ROUND_FLOOR, Decimal
    if step is None or step <= 0:
        return qty
    q, st = Decimal(repr(float(qty))), Decimal(repr(float(step)))
    return float((q / st).to_integral_value(rounding=ROUND_FLOOR) * st)


def on_tick(price: float, tick: Optional[float]) -> bool:
    if not tick or tick <= 0:
        return True
    n = price / tick
    return abs(n - round(n)) <= 1e-6


# --- errors ----------------------------------------------------------------------------------------------

class _NotFound(UserFacingError):
    pass


class _Refused(UserFacingError):
    pass


class _SendFailed(UserFacingError):
    def __init__(self, original: BaseException):
        super().__init__("send failed", detail=type(original).__name__)
        self.original = original


# --- Binance error codes (live test 2026-10-23: "rejected the API keys" said nothing about WHY) ---------------------
# ccxt raises e.g. AuthenticationError('binance {"code":-2015,"msg":"Invalid API-key, IP, or permissions for action,
# request ip: …"}') (binance.handle_errors, ccxt 4.5.84); -1021 arrives as InvalidNonce (a NetworkError — waiting does
# not fix a clock). Only the code and the SANITISED msg are shown / logged: no URL, query, signature, header, key or IP.
BINANCE_HINTS = {
    -2015: "key not valid on Binance Demo (keys from binance.com live or testnet.binance.vision do not work), IP "
           "restriction, or missing permission 'Enable Reading'/'Spot trading'",
    -1022: "signature mismatch — secret wrong or HMAC vs Ed25519/RSA key type",
    -2014: "key format",
    -1021: "PC clock out of sync",
}
_KNOWN_MSGS = (("invalid api-key, ip, or permissions", -2015), ("signature for this request is not valid", -1022),
               ("api-key format invalid", -2014), ("outside of the recvwindow", -1021),
               ("ahead of the server's time", -1021))


def sanitize(text, hide=()) -> str:
    """A Binance message safe to show and log: whitespace folded, the given secrets, URLs, query strings,
    key=value secrets, IP addresses and long tokens (keys, signatures) removed; ≤ 160 characters."""
    import re
    t = " ".join(str(text or "").split())
    for h in hide:
        if h and len(str(h)) >= 4:
            t = t.replace(str(h), "<redacted>")
    t = re.sub(r"(?i)\b(?:https?|wss?)://\S+", "<url>", t)
    t = re.sub(r"\?\S*", "", t)
    t = re.sub(r"(?i)\b(signature|x-mbx-apikey|apikey|api_key|secret|timestamp|recvwindow)\s*[=:]\s*\S+",
               r"\1=<redacted>", t)
    t = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "<ip>", t)
    t = re.sub(r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{0,4}\b", "<ip>", t)
    t = re.sub(r"[A-Za-z0-9+/=_-]{24,}", "<redacted>", t)
    return t[:160]


def binance_error(ex: BaseException, hide=()):
    """(code: int | None, msg: str | None) of a ccxt exception carrying Binance's JSON body — sanitised."""
    import json
    import re
    text = str(ex)
    code, msg = None, None
    i = text.find("{")
    if i >= 0:
        try:
            obj, _end = json.JSONDecoder().raw_decode(text[i:])
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            c = str(obj.get("code"))
            code = int(c) if re.fullmatch(r"-?\d{1,6}", c) else None
            msg = obj.get("msg") if isinstance(obj.get("msg"), str) else None
    if msg is None and i < 0:
        rest = text[len("binance "):] if text.startswith("binance ") else text
        msg = rest or None
    if code is None and msg:
        low = msg.lower()
        code = next((c for m, c in _KNOWN_MSGS if m in low), None)
    return code, (sanitize(msg, hide) if msg else None)


def describe_error(ex: BaseException, hide=()) -> str:
    """"code -2015: <msg> · likely: <hint>" (or "" when Binance sent nothing usable)."""
    code, msg = binance_error(ex, hide)
    if code is None and not msg:
        return ""
    parts = [f"code {code}" if code is not None else "no code"]
    if msg:
        parts[0] += f": {msg}"
    hint = BINANCE_HINTS.get(code)
    return parts[0] + (f" · likely: {hint}" if hint else "")


def auth_message(ex: BaseException, hide=()) -> str:
    d = describe_error(ex, hide)
    return (f"Binance Spot demo rejected the API keys ({d}). Check them in Hedge Fund → Settings → Keys." if d
            else AUTH_MESSAGE)


def _log_failure(ex: BaseException, hide=()) -> None:
    code, msg = binance_error(ex, hide)
    logger.warning("Binance demo request refused: %s code=%s msg=%s", type(ex).__name__, code, msg)


def classify_read(ex: BaseException, data_type: str, hide=()):
    k = ccxt_support.ccxt_kind(ex)
    code, _msg = binance_error(ex, hide)
    if k != "not_found" and (k in ("auth", "refused", "duplicate") or code is not None):
        _log_failure(ex, hide)               # ONE redacted line per failure (backend.log); "not found" is a normal lookup
    if code == -1021 or "InvalidNonce" in [c.__name__ for c in type(ex).__mro__]:
        return BrokerSetupError(f"Binance Spot demo refused the request ({describe_error(ex, hide) or 'code -1021'}"
                                + ("" if code == -1021 else " · likely: PC clock out of sync") + ") — set the PC clock "
                                "(Windows: Settings → Time → Sync now), then test again.",
                                detail=f"{type(ex).__name__} code={code}")
    if k == "auth":
        return BrokerAuthRejected(auth_message(ex, hide), detail=f"{type(ex).__name__} code={code}")
    if k == "not_found":
        return _NotFound("not found", detail=describe_error(ex, hide) or type(ex).__name__)
    if k == "rate_limit":
        return Retry("Binance rate limit reached")
    if k == "transient":
        return Retry(f"Binance Spot demo did not answer ({type(ex).__name__})")
    if k in ("refused", "duplicate"):
        return _Refused(f"Binance refused the request ({describe_error(ex, hide) or type(ex).__name__})")
    if isinstance(ex, (ValueError, KeyError, TypeError, AttributeError)):
        return BrokerFormatError(f"{FORMAT_MSG} ({type(ex).__name__} while parsing)")
    return unavailable(f"Binance Spot demo error ({type(ex).__name__}"
                       + (f", {describe_error(ex, hide)}" if code is not None else "") + ")", ex)

def _classify_send(ex: BaseException, data_type: str):
    return _SendFailed(ex)


class BinanceDemoAdapter(BrokerAdapter):
    name = BROKER
    paper = True
    capabilities = CAPABILITIES
    endpoint = live_guard.paper_endpoint(BROKER)

    def __init__(self, *, sdk=None, gate_obj=None, secrets_module=None,
                 clock: Optional[Callable[[], datetime]] = None, monotonic: Callable[[], float] = _time.monotonic,
                 symbol_lookup: Optional[Callable[[str], Optional[str]]] = None):
        self._sdk = sdk
        self._gate_obj = gate_obj
        self._secrets = secrets_module
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._mono = monotonic
        self._symbol_lookup = symbol_lookup
        self._baseline_fn: Optional[Callable[[], Optional[dict]]] = None
        self._markets: Optional[Dict[str, dict]] = None
        self._markets_at = 0.0
        self._prices: Dict[str, tuple] = {}
        self._fees: Dict[tuple, tuple] = {}
        self._ex = None
        self._ex_keys = None
        self.last_untouched = 0             # open orders without the UFOS_ prefix the last cancel_all left alone
        self.last_foreign_open = 0
        self.last_refused: List[dict] = []    # cancels the broker refused in the last cancel_all [{client_order_id, reason}]
        self._precision_mode = None
        self._lock = threading.RLock()

    # --- plumbing -------------------------------------------------------------------------------------

    def sdk_info(self) -> dict:
        if self._sdk is not None:
            return {"name": "ccxt", "installed": True, "version": getattr(self._sdk, "__version__", None),
                    "message": None}
        return sdk_info()

    def bind_baseline(self, fn: Optional[Callable[[], Optional[dict]]]) -> None:
        """The connected account's confirmed starting balances ``{ASSET: total}`` (None before the first connect)."""
        self._baseline_fn = fn

    def baseline(self) -> Optional[Dict[str, float]]:
        fn = self._baseline_fn
        v = fn() if fn is not None else None
        if not isinstance(v, dict):
            return None
        return {str(k).upper(): float(x) for k, x in v.items() if fnum(x) is not None}

    def _gate(self):
        return self._gate_obj if self._gate_obj is not None else gate()

    def _keys(self):
        sec = self._secrets
        if sec is None:
            from ... import secrets as sec
        key, secret = sec.require_secret(KEY_SECRET), sec.require_secret(SECRET_SECRET)
        if "PRIVATE KEY" in str(secret):
            raise BrokerSetupError(HMAC_ONLY)
        return key, secret

    def exchange(self):
        """ccxt binance in DEMO MODE, spot only, ``urls['api']`` cut to the spot APIs, each on the demo host."""
        ccxt = self._sdk if self._sdk is not None else load_sdk()
        key, secret = self._keys()
        with self._lock:
            if self._ex is not None and self._ex_keys == (key, secret):
                return self._ex
            ex = ccxt.binance({"apiKey": key, "secret": secret, "enableRateLimit": True, "timeout": TIMEOUT_MS,
                               "options": {"defaultType": "spot", "fetchMarkets": {"types": ["spot"]},
                                           "fetchMargins": False, "fetchCurrencies": False,
                                           "fetchOpenOrders": {"warnWithoutSymbol": False},
                                           "warnOnFetchOpenOrdersWithoutSymbol": False}})
            if getattr(ex, "isSandboxModeEnabled", False):
                raise live_guard.LiveTradingDisabled(f"{BROKER}: sandbox (Testnet) mode is never used")
            ex.enable_demo_trading(True)
            api = ex.urls.get("api")
            if not isinstance(api, dict) or any(k not in api for k in SPOT_APIS):
                raise live_guard.LiveTradingDisabled(f"{BROKER}: the demo URLs of the spot APIs are missing")
            ex.urls["api"] = {k: api[k] for k in SPOT_APIS}
            self._precision_mode = getattr(ex, "precisionMode", None)
            live_guard.assert_paper_urls(BROKER, ex.urls["api"].values())
            if callable(getattr(ex, "fetch", None)):
                ccxt_support.harden_exchange(ex, BROKER, FORMAT_MSG, "Binance demo")
            self._ex, self._ex_keys = ex, (key, secret)
            return ex

    def check_endpoint(self) -> str:
        """Builds the demo-mode exchange (keys + every API URL asserted on the demo host) without a request."""
        self.exchange()
        return self.endpoint

    def _read(self, fn, what: str):
        try:
            hide = self._ex_keys or ()
            return self._gate().call(fn, data_type=DATA_TYPE, classify=lambda ex, dt: classify_read(ex, dt, hide))
        except (BrokerUnavailable, BrokerAuthRejected, _NotFound, _Refused, SecretMissing, SecretStoreUnavailable,
                live_guard.LiveTradingDisabled, BrokerNotInstalled, BrokerNotRetryable):
            raise
        except DataSourceUnavailable as ex:
            raise unavailable(f"Binance Spot demo: {what} failed — {ex.reason}", ex,
                              connection=getattr(ex, "kind", None) == "connection" or "did not answer" in ex.reason) \
                from ex

    # --- public reads ---------------------------------------------------------------------------------

    def server_time(self) -> dict:
        ex = self.exchange()
        ms = fnum(self._read(lambda: ex.fetch_time(), "server time"))
        if ms is None:
            raise BrokerFormatError(f"{FORMAT_MSG}: the server time is not a number")
        local = self._clock().timestamp() * 1000
        return {"server_time": datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds"),
                "skew_ms": int(local - ms)}

    def markets(self) -> Dict[str, dict]:
        with self._lock:
            if self._markets is not None and self._mono() - self._markets_at < MARKETS_TTL_SECONDS:
                return self._markets
        ex = self.exchange()
        mk = self._read(lambda: ex.load_markets(), "markets")
        if not isinstance(mk, dict):
            raise BrokerFormatError("Binance Spot demo: markets are not a mapping (unexpected response)")
        with self._lock:
            self._markets, self._markets_at = mk, self._mono()
        return mk

    def usdt_markets(self) -> int:
        return sum(1 for m in self.markets().values() if isinstance(m, dict) and m.get("spot")
                   and m.get("active", True) and str(m.get("quote") or "").upper() == QUOTE)

    def market(self, symbol: str) -> Optional[dict]:
        m = self.markets().get(ccxt_symbol(symbol))
        if not m or not m.get("spot") or not m.get("active", True) or str(m.get("quote") or "").upper() != QUOTE:
            return None
        return m

    def _increment(self, v):
        """A precision value as an increment, by the exchange's ``precisionMode`` (review fix 2026-10-19 — no guessing):
        TICK_SIZE (binance in ccxt 4.5.84) → the value IS the step (e.g. 10 or 0.001); DECIMAL_PLACES → 10^-v; any
        other mode → BrokerFormatError (refused, no heuristic)."""
        x = fnum(v)
        if x is None:
            return None
        mode = self._precision_mode
        if mode == TICK_SIZE:
            return x if x > 0 else None
        if mode == DECIMAL_PLACES:
            return 10 ** (-int(x))
        raise BrokerFormatError(f"Binance Spot demo: unsupported ccxt precision mode {mode!r} (expected TICK_SIZE)")

    def filters(self, m: dict) -> dict:
        prec = m.get("precision") or {}
        lim = m.get("limits") or {}
        step = self._increment(prec.get("amount"))
        tick = self._increment(prec.get("price"))
        market_max = None
        for f in ((m.get("info") or {}).get("filters") or []):
            if isinstance(f, dict) and f.get("filterType") == "MARKET_LOT_SIZE":
                v = fnum(f.get("maxQty"))
                market_max = v if v and v > 0 else None
        mn = fnum((lim.get("amount") or {}).get("min"))
        return {"step": step, "min_qty": mn if mn is not None else step,
                "max_qty": fnum((lim.get("amount") or {}).get("max")) or None, "market_max_qty": market_max,
                "tick": tick, "min_price": fnum((lim.get("price") or {}).get("min")) or None,
                "max_price": fnum((lim.get("price") or {}).get("max")) or None,
                "min_notional": fnum((lim.get("cost") or {}).get("min")) or None,
                "max_notional": fnum((lim.get("cost") or {}).get("max")) or None}

    def instrument(self, symbol: str) -> dict:
        m = self.market(symbol)
        if m is None:
            return {"symbol": symbol, "broker_symbol": binance_id(symbol), "ccxt_symbol": ccxt_symbol(symbol),
                    "tradable": False, "fractionable": None, "shortable": False, "easy_to_borrow": None,
                    "min_qty": None, "qty_step": None, "tick_size": None, "contract_size": None, "min_notional": None,
                    "quote": QUOTE, "message": f"{symbol} ({ccxt_symbol(symbol)}) is not traded on Binance Spot demo"}
        f = self.filters(m)
        return {"symbol": symbol, "broker_symbol": m.get("id"), "ccxt_symbol": m.get("symbol"), "tradable": True,
                "fractionable": True, "shortable": False, "easy_to_borrow": None, "min_qty": f["min_qty"],
                "qty_step": f["step"], "tick_size": f["tick"], "contract_size": None,
                "min_notional": f["min_notional"], "max_qty": f["max_qty"], "quote": QUOTE, "message": None}

    def round_qty(self, symbol: str, qty: float) -> float:
        m = self.market(symbol)
        if m is None:
            return qty
        step = self.filters(m)["step"]
        return step_round_down(qty, step) if step else qty

    def last_price(self, m: dict) -> float:
        sym = m["symbol"]
        with self._lock:
            hit = self._prices.get(sym)
            if hit is not None and self._mono() - hit[0] < PRICE_TTL_SECONDS:
                return hit[1]
        ex = self.exchange()
        t = self._read(lambda: ex.fetch_ticker(sym), "price")
        px = fnum((t or {}).get("last")) if isinstance(t, dict) else None
        if px is None or px <= 0:
            raise BrokerFormatError(f"Binance Spot demo: no last price for {sym} (unexpected response)")
        with self._lock:
            self._prices[sym] = (self._mono(), px)
        return px

    def quote(self, symbol: str):
        """The demo's last BASE/USDT price as a ``core.types.Quote`` (USDT — the account's own money, never a USD quote);
        None when the symbol is not traded there. Read errors propagate (the caller pauses valuation)."""
        from strategy_platform.core.types import Quote
        m = self.market(symbol)
        if m is None:
            return None
        px = self.last_price(m)
        return Quote(symbol=symbol, price=px, ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     delayed=False)

    def filter_refusal(self, order: dict, m: dict, qty: float) -> Optional[str]:
        """The violated Binance filter (named) for this order, else None. Market orders use the demo's last price for
        the notional check (unreadable → refused, no stand-in)."""
        f = self.filters(m)
        base = order["symbol"][:-4]
        t = order.get("type") or "market"
        if f["min_qty"] is not None and qty < f["min_qty"] - EPS:
            return f"LOT_SIZE: quantity {qty:g} {base} is below the minimum {f['min_qty']:g}"
        if f["max_qty"] is not None and qty > f["max_qty"] + EPS:
            return f"LOT_SIZE: quantity {qty:g} {base} is above the maximum {f['max_qty']:g}"
        if t == "market" and f["market_max_qty"] is not None and qty > f["market_max_qty"] + EPS:
            return f"MARKET_LOT_SIZE: quantity {qty:g} {base} is above the maximum {f['market_max_qty']:g} for market orders"
        if t == "limit":
            px = float(order["limit_price"])
            if not on_tick(px, f["tick"]):
                return f"PRICE_FILTER: limit price {px:g} is not a multiple of the tick size {f['tick']:g}"
            if f["min_price"] is not None and px < f["min_price"] - EPS:
                return f"PRICE_FILTER: limit price {px:g} is below the minimum {f['min_price']:g}"
            if f["max_price"] is not None and px > f["max_price"] + EPS:
                return f"PRICE_FILTER: limit price {px:g} is above the maximum {f['max_price']:g}"
        else:
            if f["min_notional"] is None and f["max_notional"] is None:
                return None
            try:
                px = self.last_price(m)
            except (BrokerUnavailable, BrokerNotRetryable, _Refused) as ex:
                return f"NOTIONAL: the demo's price for {m['symbol']} could not be read ({getattr(ex, 'user_message', ex)})"
        notional = qty * px
        if f["min_notional"] is not None and notional < f["min_notional"] - EPS:
            return f"NOTIONAL: {notional:,.2f} {QUOTE} is below the minimum {f['min_notional']:,.2f} {QUOTE}"
        if f["max_notional"] is not None and notional > f["max_notional"] + EPS:
            return f"NOTIONAL: {notional:,.2f} {QUOTE} is above the maximum {f['max_notional']:,.2f} {QUOTE}"
        return None

    # --- parsing -------------------------------------------------------------------------------------

    def _trade_fees(self, o: dict, filled: float, avg: Optional[float]):
        """(fee in USDT incl. base fees valued at ``avg``, base-asset fee, {other asset: amount}) from the order's
        trades (cached per order id + filled quantity)."""
        oid, sym = o.get("id"), o.get("symbol")
        key = (str(oid), round(filled, 12))
        with self._lock:
            if key in self._fees:
                return self._fees[key]
        ex = self.exchange()
        trades = self._read(lambda: ex.fetch_my_trades(sym, None, None, {"orderId": oid}), "trades") or []
        if not isinstance(trades, list):
            raise BrokerFormatError("Binance Spot demo: trades are not a list (unexpected response)")
        base = str(sym).split("/")[0].upper()
        quote_fee, base_fee, other = 0.0, 0.0, {}
        for t in trades:
            fee = (t or {}).get("fee") if isinstance(t, dict) else None
            if not isinstance(fee, dict):
                continue
            cost = fnum(fee.get("cost")) or 0.0
            cur = str(fee.get("currency") or "").upper()
            if cur == QUOTE:
                quote_fee += cost
            elif cur == base:
                base_fee += cost
            elif cost:
                other[cur] = other.get(cur, 0.0) + cost
        out = (quote_fee + base_fee * float(avg or 0.0), base_fee, other)
        with self._lock:
            self._fees[key] = out
        return out

    def _ack(self, o: dict, cid: Optional[str] = None, with_fees: bool = True) -> dict:
        if not isinstance(o, dict):
            raise TypeError("expected an order object from ccxt")
        st = str(o.get("status") or "").lower()
        amount = fnum(o.get("amount"))
        filled = fnum(o.get("filled")) or 0.0
        avg = fnum(o.get("average"))
        if st in OPEN_CCXT:
            status = "partially_filled" if filled > EPS else "accepted"
        elif st == "closed":
            status = "filled"
        elif st in ("canceled", "cancelled"):
            status = "cancelled"
        elif st == "expired":
            status = "expired"
        elif st == "rejected":
            status = "rejected"
        else:
            raise TypeError(f"unknown ccxt order status {st!r}")
        side = str(o.get("side") or "").lower()
        fees, fee_base, note = 0.0, 0.0, None
        if with_fees and filled > EPS:
            fees, base_fee, other = self._trade_fees(o, filled, avg)
            if side == "buy" and base_fee:
                fee_base = base_fee          # taken from the bought asset: the mirror books filled − this (net)
            if other:
                note = "fee paid in " + ", ".join(sorted(other)) + " — not booked"
        reason = None
        if status == "rejected":
            reason = f"Binance rejected the order ({(o.get('info') or {}).get('status') or st})"
        elif status == "cancelled":
            reason = "cancelled at Binance"
        ts = o.get("lastTradeTimestamp") or o.get("timestamp")
        return {"client_order_id": from_broker_id(o.get("clientOrderId")) or cid, "broker_order_id": str(o.get("id")),
                "status": status, "qty": amount, "filled_qty": filled, "fee_base_qty": fee_base, "avg_price": avg,
                "fees": float(fees or 0.0),
                "notional": (filled * avg) if (avg is not None and filled > 0) else None, "reject_reason": reason,
                "ts": datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat(timespec="seconds") if ts else None,
                "symbol": platform_symbol(o.get("symbol")), "side": side, "type": o.get("type"), "legs": [],
                "broker_status": str((o.get("info") or {}).get("status") or st)[:40], "tif": "gtc",
                "limit_price": fnum(o.get("price")) if o.get("type") == "limit" else None, "stop_price": None,
                "created_at": o.get("datetime"), "broker_symbol": (o.get("info") or {}).get("symbol"),
                "fee_note": note, "currency": QUOTE}

    @staticmethod
    def _order_view(a: dict) -> dict:
        return {"broker_order_id": a["broker_order_id"], "client_order_id": a["client_order_id"],
                "symbol": a["symbol"], "broker_symbol": a.get("broker_symbol"), "side": a["side"], "qty": a["qty"],
                "filled_qty": a["filled_qty"], "type": a.get("type"), "limit_price": a.get("limit_price"),
                "stop_price": None, "tif": "gtc", "status": a["status"], "broker_status": a.get("broker_status"),
                "created_at": a.get("created_at"), "known": False, "legs": []}

    # --- account ---------------------------------------------------------------------------------------

    def balances(self) -> Dict[str, dict]:
        """{ASSET: {free, locked, total}} of every non-zero balance."""
        ex = self.exchange()
        raw = self._read(lambda: ex.fetch_balance(), "balances")
        if not isinstance(raw, dict) or not isinstance(raw.get("total"), dict):
            from .common import shape_of
            raise BrokerFormatError(f"{FORMAT_MSG}: unexpected balance shape: {shape_of(raw)}")
        free, used = raw.get("free") or {}, raw.get("used") or {}
        out = {}
        for asset, total in raw["total"].items():
            t = fnum(total)
            if t is None or abs(t) < 1e-12:
                continue
            out[str(asset).upper()] = {"free": fnum(free.get(asset)) or 0.0, "locked": fnum(used.get(asset)) or 0.0,
                                       "total": t}
        return out

    @staticmethod
    def balance_list(bals: Dict[str, dict]) -> List[dict]:
        return [dict(asset=a, **v) for a, v in sorted(bals.items())]

    def account(self, prices: Optional[dict] = None) -> dict:
        bals = self.balances()
        u = bals.get(QUOTE) or {"free": 0.0, "locked": 0.0, "total": 0.0}
        return {"equity": None, "cash": u["free"], "cash_basis": u["total"], "buying_power": u["free"],
                "initial_margin": None, "maintenance_margin": None, "unrealized_pnl": None, "status": None,
                "pattern_day_trader": None, "daytrade_count": None, "multiplier": 1.0, "shorting_enabled": False,
                "trading_blocked": False, "currency": QUOTE, "currency_note": CURRENCY_NOTE,
                "balances": self.balance_list(bals), "baseline_set": self.baseline() is not None}

    def _kill_skip(self, sym: Optional[str], asset: str, qty: float) -> Optional[str]:
        """Why the kill does not sell this delta (dust / below baseline / not a platform asset), else None."""
        if sym is None:
            return f"{asset} is not an asset the platform trades (e.g. a BNB fee) — not sold"
        if qty < 0:
            return f"{asset} is below the starting balance — nothing of the platform's to sell"
        m = self.market(sym)
        if m is None:
            return f"{sym} is not traded on Binance Spot demo — not sold"
        f = self.filters(m)
        q = step_round_down(qty, f["step"]) if f["step"] else qty
        if f["min_qty"] is not None and q < f["min_qty"] - EPS:
            return f"dust: {qty:g} {asset} is below LOT_SIZE (minimum {f['min_qty']:g})"
        if f["min_notional"] is not None:
            try:
                px = self.last_price(m)
            except (BrokerUnavailable, BrokerNotRetryable, _Refused):
                return None              # unknown: the kill tries to sell; a filter refusal is reported
            if q * px < f["min_notional"] - EPS:
                return f"dust: {q * px:,.2f} {QUOTE} of {asset} is below NOTIONAL (minimum {f['min_notional']:,.2f})"
        return None

    def positions(self, prices: Optional[dict] = None) -> List[dict]:
        base = self.baseline()
        if base is None:
            if self._baseline_fn is not None:     # a connected account WITHOUT its baseline: blocked, never assumed empty
                raise BrokerSetupError(BASELINE_MISSING)
            return []                    # before the first connect (not bound) nothing is the platform's
        bals = self.balances()
        out = []
        for asset in sorted(set(bals) | set(base)):
            if asset == QUOTE:
                continue
            total = (bals.get(asset) or {}).get("total", 0.0)
            delta = round(total - base.get(asset, 0.0), 12)
            if abs(delta) < 1e-12:
                continue
            sym = asset_symbol(asset)
            skip = self._kill_skip(sym, asset, delta)
            out.append({"symbol": sym or f"UNMAPPED:{asset}", "broker_symbol": f"{asset}{QUOTE}", "qty": delta,
                        "side": "long" if delta > 0 else "short", "avg_price": None, "mark_price": None,
                        "market_value": None, "unrealized_pnl": None, "asset": asset, "balance": total,
                        "baseline": base.get(asset, 0.0), "kill_skip": skip, "currency": QUOTE})
        return out

    def _all_open(self) -> List[dict]:
        ex = self.exchange()
        rows = self._read(lambda: ex.fetch_open_orders(), "open orders")
        if not isinstance(rows, list):
            raise BrokerFormatError("Binance Spot demo: open orders are not a list (unexpected response)")
        return [o for o in rows if isinstance(o, dict)]

    def open_orders(self) -> List[dict]:
        """The platform's open orders (client id ``UFOS_…`` on a USDT spot market); others are counted only."""
        rows = self._all_open()
        mine = [o for o in rows if from_broker_id(o.get("clientOrderId")) and platform_symbol(o.get("symbol"))]
        self.last_foreign_open = len(rows) - len(mine)
        return [self._order_view(self._ack(o, with_fees=False)) for o in mine]

    def foreign_open_orders(self) -> int:
        self.open_orders()
        return self.last_foreign_open

    def snapshot(self) -> dict:
        acct = self.account()
        pos = self.positions()
        oo = self.open_orders()
        return {"account": acct, "positions": pos, "open_orders": oo,
                "foreign_open_orders": self.last_foreign_open, "at": datetime.now(timezone.utc).isoformat(
                    timespec="seconds")}

    # --- orders ----------------------------------------------------------------------------------------

    def _symbol_of(self, cid: str) -> Optional[str]:
        if self._symbol_lookup is not None:
            return self._symbol_lookup(cid)
        from .. import oms
        row = oms.get(cid)
        return (row or {}).get("symbol")

    def order(self, client_order_id: str, symbol: Optional[str] = None) -> Optional[dict]:
        sym = symbol or self._symbol_of(client_order_id)
        if not sym:
            return None
        ex = self.exchange()
        cs = ccxt_symbol(sym)
        try:
            o = self._read(lambda: ex.fetch_order(None, cs, {"origClientOrderId": to_broker_id(client_order_id)}),
                           "order status")
        except _NotFound:
            return None
        if not isinstance(o, dict) or not o.get("status"):
            return None
        return self._ack(o, client_order_id)

    def _refusal(self, order: dict) -> Optional[str]:
        from strategy_platform.core import params as fund_params
        if not fund_params.is_crypto(order.get("symbol")):
            return "only crypto (BASE-USD) is traded on binance-demo (stocks/ETFs go to alpaca-paper)"
        if order.get("side") in ("short", "cover"):
            return ("binance-demo is spot, long only — a short is not possible (blocked, not converted to flat or "
                    "long)")
        if order.get("side") not in ("buy", "sell"):
            return f"side {order.get('side')} is not possible on binance-demo"
        why = validate_order_shape(dict(order, tif=order.get("tif") or "gtc"), types=TYPES, tifs=TIFS)
        if why:
            return why
        if order.get("qty") is None:
            return "a quantity is required on Binance Spot demo (no notional orders)"
        try:
            to_broker_id(order["client_order_id"])
        except ValueError as ex:
            return str(ex)
        return None

    def submit(self, order: dict) -> dict:
        live_guard.require_paper_order(BROKER, order)
        cid = order["client_order_id"]
        why = self._refusal(order)
        if why:
            return rejected_ack(order, why)
        try:
            known = self.order(cid, order["symbol"])
        except (BrokerAuthRejected, SecretMissing, SecretStoreUnavailable, BrokerNotInstalled) as ex:
            return rejected_ack(order, getattr(ex, "user_message", str(ex)))
        except BrokerNotRetryable as ex:
            return rejected_ack(order, ex.user_message)
        if known is not None:
            return known
        m = self.market(order["symbol"])
        if m is None:
            return rejected_ack(order, f"{order['symbol']} ({ccxt_symbol(order['symbol'])}) is not traded on Binance "
                                       "Spot demo")
        step = self.filters(m)["step"]
        qty = step_round_down(float(order["qty"]), step) if step else float(order["qty"])
        bad = self.filter_refusal(order, m, qty)
        if bad:
            return rejected_ack(order, bad)
        t = order.get("type") or "market"
        params = {"newClientOrderId": to_broker_id(cid)}
        price = None
        if t == "limit":
            price = float(order["limit_price"])
            params["timeInForce"] = "GTC"
        ex = self.exchange()
        try:
            raw = self._gate().call(lambda: ex.create_order(m["symbol"], t, order["side"], qty, price, params),
                                    data_type=DATA_TYPE, classify=_classify_send)
            if isinstance(raw, dict) and raw.get("id"):
                try:
                    full = self._read(lambda: ex.fetch_order(raw["id"], m["symbol"]), "order status")
                    if isinstance(full, dict) and full.get("status"):
                        raw = dict(full, clientOrderId=full.get("clientOrderId") or to_broker_id(cid))
                except (_NotFound, BrokerUnavailable):
                    pass
            if isinstance(raw, dict) and raw.get("status"):
                return self._ack(raw, cid)
        except _SendFailed as ex_:
            kind = ccxt_support.ccxt_kind(ex_.original)
            if kind == "auth":
                _log_failure(ex_.original, self._ex_keys or ())
                return rejected_ack(order, auth_message(ex_.original, self._ex_keys or ()))
            if kind == "refused":
                _log_failure(ex_.original, self._ex_keys or ())
                return rejected_ack(order, f"Binance refused the order "
                                           f"({describe_error(ex_.original, self._ex_keys or ()) or type(ex_.original).__name__})")
            logger.warning("Binance demo submit %s ambiguous (%s) — looking the id up", cid,
                           type(ex_.original).__name__)
        except DataSourceUnavailable as ex2:
            raise unavailable(f"Binance Spot demo: the order could not be sent ({ex2.reason})", ex2) from ex2
        except BrokerNotRetryable as ex3:     # unreadable answer to a send: ambiguous — never re-sent blindly
            logger.warning("Binance demo submit %s: unreadable answer (%s) — looking the id up", cid,
                           type(ex3).__name__)
            try:
                found = self.order(cid, order["symbol"])
            except BrokerNotRetryable as ex4:
                raise unavailable(f"Binance Spot demo: the send's answer and the lookup were unreadable "
                                  f"({ex4.user_message})", connection=False) from ex4
            if found is not None:
                return found
            raise unavailable("Binance Spot demo: the send's answer was unreadable and the order is not found — "
                              "looked up again before any re-send", connection=False) from ex3
        found = self.order(cid, order["symbol"])
        if found is not None:
            return found
        raise unavailable("Binance Spot demo: the order did not reach the broker (connection) — it is re-sent with "
                          "the same id after a lookup", connection=True)

    def cancel(self, client_order_id: str, reason: str = "cancelled", broker_order_id: Optional[str] = None) -> bool:
        sym = self._symbol_of(client_order_id)
        if not sym:
            return False
        ex = self.exchange()
        cs = ccxt_symbol(sym)
        params = {} if broker_order_id else {"origClientOrderId": to_broker_id(client_order_id)}
        try:
            self._read(lambda: ex.cancel_order(broker_order_id, cs, params), "cancel")
        except (_NotFound, _Refused):
            return False
        return True

    def cancel_all(self) -> int:
        """Cancels the platform's open orders (``UFOS_`` prefix) ONE BY ONE; any other open order on the demo account
        is left untouched and counted (``last_untouched``) — ``cancel_all_orders`` would cancel them too."""
        ex = self.exchange()
        rows = self._all_open()
        mine = [o for o in rows if from_broker_id(o.get("clientOrderId"))]
        self.last_untouched = len(rows) - len(mine)
        if self.last_untouched:
            logger.info("Binance demo cancel all: %d order(s) without the UFOS_ prefix left untouched",
                        self.last_untouched)
        n = 0
        self.last_refused = []
        for o in mine:
            try:
                self._read(lambda o=o: ex.cancel_order(o["id"], o.get("symbol")), "cancel")
                n += 1
            except _NotFound:
                continue                 # already gone — the kill's open-order check sees the rest
            except _Refused as r:        # counted and reported (review fix 2026-10-19), never dropped silently
                self.last_refused.append({"client_order_id": from_broker_id(o.get("clientOrderId")),
                                          "broker_order_id": str(o.get("id")), "reason": r.user_message[:200]})
        return n

    def fills(self, after: int = 0, limit: int = 500) -> List[dict]:
        return []           # fills are derived from order status (mirror.book_ack)
