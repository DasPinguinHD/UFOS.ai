"""Broker accounts and their mirror book (SPEC §18.1, §18.4).

A broker account (``alpaca-paper`` / ``binance-demo``; an old ``kraken-demo`` row is retired — blocked, no adapter since
stage 10) is a platform account whose book in ``pa_*`` is the
platform's EXPECTED state, written only from broker fills (``book_ack``) and from a reconciliation the user
resolved (``reconcile.resolve``). It is VALUED with the real ``paper_broker.PaperBroker`` code (``MirrorBroker`` —
every simulated execution path is disabled there), so views, sub-ledgers, risk and checks work unchanged. The
real broker is reached through ``MirroredAdapter`` → the broker adapter (``alpaca_paper`` / ``binance_demo``),
with the link state (weak connections) kept per broker.

Fill booking: per broker order id (parent and bracket legs) the last booked cumulative (filled_qty, avg, fees)
is kept in ``br_fill_marks``; an ack books only the delta (exact price (q1·p1 − q0·p0)/(q1 − q0)) in ONE
transaction: a ``pa_orders`` row (client id ``<cid>`` for the first delta, ``<cid>~<n>`` later), cash ``fill`` +
``fee`` rows and the position (signed math: adds average in, reductions realise P&L, a flip opens the rest at the
fill price).

Part of UFOS.ai, a proof-of-concept portfolio project; PAPER / DEMO TRADING ONLY.
"""
from __future__ import annotations

import json
import logging
import math
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from ...storage import db
from .. import accounts
from ..errors import BrokerUnavailable
from .base import BrokerAdapter
from .common import EPS, Link, classify_failure, fnum, unavailable

logger = logging.getLogger(__name__)

BROKER_IDS = accounts.BROKER_IDS        # the active brokers + the retired kraken-demo (history rows, blocked)
BROKER_OF = {"alpaca-paper": "alpaca_paper", "kraken-demo": "kraken_demo", "binance-demo": "binance_demo"}
LABELS = {"alpaca-paper": "Alpaca paper account", "kraken-demo": "Kraken Futures demo account",
          "binance-demo": "Binance Spot demo account"}
CRYPTO_VENUE = "binance-demo"           # crypto broker since 2026-10-19 (the Kraken demo is retired, SPEC §18.14)
BASELINE_KEY = "baseline"               # binance-demo: the confirmed starting balances {ASSET: total} (§18.14 item 7)
RECONCILE_KEY = "reconcile"
SNAPSHOT_KEY = "broker_snapshot"
KILL_PENDING_KEY = "kill_pending"
RECONCILE_MAX_AGE = timedelta(minutes=15)
BROKER_ORIGIN = {"runtime": "autopilot", "manual": "manual", "kill": "kill-switch", "drawdown": "drawdown-stop",
                 "margin": "margin-call", "council": "council"}      # council: SP §22.5

_links: Dict[str, Link] = {}
_links_lock = threading.Lock()
_factory: Optional[Callable[[str], BrokerAdapter]] = None


def link(account_id: str) -> Link:
    with _links_lock:
        lk = _links.get(account_id)
        if lk is None:
            lk = _links[account_id] = Link(account_id, clock=accounts.now)
        return lk


def reset_links() -> None:
    with _links_lock:
        _links.clear()


def set_adapter_factory(fn: Optional[Callable[[str], BrokerAdapter]]) -> None:
    """Tests: ``fn(account_id) -> adapter`` (fakes). None = the real adapters."""
    global _factory
    _factory = fn
    accounts.reset_cache()


class SandboxBlockedAdapter(BrokerAdapter):
    """Sandbox profile (SPEC "Sandbox profile"): stands in for EVERY broker adapter (also a test fake) — no client is
    built and every interface call raises ``BrokerSetupError`` "blocked in sandbox", so no request can be sent."""

    def __init__(self, account_id: str):
        self.account_id = account_id

    def _blocked(self, *a, **k):
        import sandbox_profile
        from .common import BrokerSetupError
        raise BrokerSetupError(f"{self.account_id}: {sandbox_profile.BROKER_BLOCKED}")

    account = positions = submit = cancel = order = fills = snapshot = open_orders = cancel_all = _blocked
    instrument = _blocked


class RetiredAdapter(SandboxBlockedAdapter):
    """A RETIRED broker account (SPEC §18.13; the Kraken adapter was removed in stage 10, S10-K): stands in for the
    adapter of an old history row — no client exists and every interface call raises ``BrokerSetupError``."""

    def _blocked(self, *a, **k):
        from .common import BrokerSetupError
        raise BrokerSetupError(f"{self.account_id}: {accounts.retired_reason(self.account_id)}")

    account = positions = submit = cancel = order = fills = snapshot = open_orders = cancel_all = _blocked
    instrument = _blocked


def remote_adapter(account_id: str) -> BrokerAdapter:
    if accounts.retired_reason(account_id):
        return RetiredAdapter(account_id)             # retired: never a broker client (real or fake)
    if accounts.sandbox_block_reason(account_id):
        return SandboxBlockedAdapter(account_id)      # sandbox profile: never a broker client (real or fake)
    if _factory is not None:
        return _factory(account_id)
    if account_id == "alpaca-paper":
        from .alpaca_paper import AlpacaPaperAdapter
        return AlpacaPaperAdapter()
    if account_id == "binance-demo":
        from .binance_demo import BinanceDemoAdapter
        return BinanceDemoAdapter()
    raise ValueError(f"unknown broker account {account_id!r}")


def is_broker_id(account_id) -> bool:
    return account_id in BROKER_IDS


# --- the valuation broker --------------------------------------------------------------------------------

def _mirror_broker_class():
    from strategy_platform.execution.paper_broker import PaperBroker

    class MirrorBroker(PaperBroker):
        """PaperBroker used ONLY to value a mirror book — it never executes."""

        def _refuse(self, *a, **k):
            raise RuntimeError("mirror book of a broker account: simulated execution is disabled (SPEC §18.1)")

        submit = target = liquidate_all = process_pending = daily_accruals = check_margin = reset = _refuse
        cancel_pending = expire_pending = _refuse

    return MirrorBroker


# --- reconciliation state (pa_meta) ------------------------------------------------------------------------

def reconcile_state(store) -> dict:
    v = store.meta_get(RECONCILE_KEY)
    try:
        d = json.loads(v) if v else None
    except ValueError:
        d = None
    if not isinstance(d, dict):
        return {"status": "never", "id": None, "at": None, "trigger": None, "reason": None, "halted": False,
                "halted_at": None, "mismatch_id": None}
    d.setdefault("halted", False)
    return d


def set_reconcile_state(store, **fields) -> dict:
    st = reconcile_state(store)
    st.update(fields)
    store.meta_set(RECONCILE_KEY, json.dumps(st))
    return st


class BrokerAccount(accounts.Account):
    """A connected broker account (see the module docstring)."""

    is_broker = True

    def __init__(self, row: dict):          # noqa: super().__init__ builds an internal broker — not wanted here
        from ..paper_store import PlatformPaperStore
        self.id = row["id"]
        self.row = row
        self.broker_name = row["broker"]
        self.params = json.loads(row["params_json"])
        self.store = PlatformPaperStore(self.id, self.params)
        kw = {}
        if accounts._cfg.get("session_fn") is not None:
            kw["session_fn"] = accounts._cfg["session_fn"]
        cls = _mirror_broker_class()
        self.remote = remote_adapter(self.id)
        # binance-demo (SPEC §18.14): the book is USDT — valued with the demo's own BASE/USDT prices, never USD quotes
        own = self.broker_name == "binance_demo" and callable(getattr(self.remote, "quote", None))
        qfn = self._own_quote if own else (accounts._cfg.get("quote_fn") or (lambda s: None))
        if own:
            self.own_quote = self._own_quote
        self.broker = cls(self.store, qfn, clock=accounts._cfg.get("clock"),
                          dividends_fn=None, bars_fn=accounts._cfg.get("bars_fn"), params_fn=lambda: self.params, **kw)
        bind = getattr(self.remote, "bind_baseline", None)
        if callable(bind):                   # binance-demo: positions = balance − confirmed baseline (§18.14)
            bind(lambda: baseline(self.store))
        self.link = link(self.id)
        self.adapter = MirroredAdapter(self)

    @property
    def currency(self) -> str:
        return "USDT" if self.broker_name == "binance_demo" else "USD"

    def can_hold(self, symbol: str) -> bool:
        """Whether this venue can hold ``symbol`` (retest 2026-10-23: crypto venues never value SPY & co.)."""
        from strategy_platform.core import params as fund_params
        crypto = fund_params.is_crypto(symbol)
        if self.broker_name == "binance_demo":
            return crypto
        return not crypto if self.broker_name == "alpaca_paper" else True

    def _own_quote(self, symbol: str):
        """The demo's last BASE/USDT price as a Quote (None when not readable — valuation pauses, no stand-in)."""
        if accounts.no_call_reason(self.id) or not self.can_hold(symbol):
            return None                    # a symbol the venue cannot hold: no quote, no error log
        try:
            return self.remote.quote(symbol)
        except Exception as ex:      # noqa: BLE001 — unvalued: the callers refuse to act on it
            logger.info("%s: no own quote for %s (%s)", self.id, symbol, getattr(ex, "user_message", type(ex).__name__))
            return None

    @property
    def origin(self) -> str:
        return "connected"

    def default_source(self) -> str:
        return accounts.UNALLOCATED

    def halt(self) -> dict:
        rs = reconcile_state(self.store)
        if rs.get("halted"):
            return {"active": True, "kind": "reconcile", "reason": rs.get("reason") or "reconciliation mismatch",
                    "at": rs.get("halted_at") or rs.get("at")}
        return super().halt()

    def default_tif(self, origin: str) -> Optional[str]:
        return "gtc" if self.broker_name == "binance_demo" else None

    def round_qty(self, symbol: str, qty: float) -> float:
        if accounts.no_call_reason(self.id):
            return qty                   # no market lookup at a retired / sandbox-blocked broker
        fn = getattr(self.remote, "round_qty", None)
        if fn is None:
            return qty
        try:
            return float(fn(symbol, qty))
        except Exception:        # noqa: BLE001 — markets not loadable now: the venue check reports it
            return qty

    # --- pre-trade checks of a broker account (SPEC §18.4) -------------------------------------------------

    def pre_trade_checks(self, order: dict, now: datetime) -> List[dict]:
        from strategy_platform.core import params as fund_params
        out: List[dict] = []
        sym, side = order["symbol"], order["side"]
        why = accounts.no_call_reason(self.id)          # retired, or blocked in sandbox
        if why:
            return [_res("venue", False, f"{self.id}: {why}")]
        crypto = fund_params.is_crypto(sym)
        if self.broker_name == "alpaca_paper" and crypto:
            return [_res("venue", False, f"{sym}: crypto is traded on {CRYPTO_VENUE}, not on alpaca-paper")]
        if self.broker_name == "binance_demo" and not crypto:
            return [_res("venue", False, f"{sym}: stocks/ETFs are traded on alpaca-paper, not on {self.id}")]
        long_only = bool((getattr(self.remote, "capabilities", {}) or {}).get("long_only"))
        if long_only and side in ("short", "cover"):
            why = (f"{self.id} is spot, long only — a {side} of {sym} is not possible (blocked, not converted to flat "
                   "or long)")
            self._blocked_event(sym, why, order)
            return [_res("venue", False, why)]
        caps = getattr(self.remote, "capabilities", {}) or {}
        t = order.get("type") or "market"
        if caps.get("order_types") and t not in caps["order_types"]:
            return [_res("venue", False, f"order type {t} is not supported on {self.id} (supported: "
                                         f"{', '.join(caps['order_types'])})")]
        tif = order.get("tif")
        if tif and caps.get("tifs") and tif not in caps["tifs"]:
            return [_res("venue", False, f"time in force {tif} is not supported on {self.id}")]
        qty = float(order.get("qty") or 0.0)
        if qty <= EPS:
            return [_res("venue", False, f"size below {_venue_name(self) if crypto else 'the broker'}'s minimum (the "
                                         "quantity rounds to 0 at the broker's size step)")]
        if not crypto and abs(qty - round(qty)) > EPS:
            return [_res("venue", False, "whole shares only on alpaca-paper (stage 8)")]
        if crypto and qty > 0:
            try:
                info = self.remote.instrument(sym)
            except Exception as ex:      # noqa: BLE001 — shown as the check message
                return [_res("venue", False, f"{sym}: the broker's sizes could not be read "
                                             f"({getattr(ex, 'user_message', type(ex).__name__)})")]
            if not info.get("tradable"):
                return [_res("venue", False, info.get("message") or f"{sym} is not tradable on {self.id}")]
            mn = fnum(info.get("min_qty"))
            if mn is not None and qty < mn - EPS:
                return [_res("venue", False, f"size below {_venue_name(self)}'s minimum ({mn:g} {sym[:-4]})")]
        out.append(_res("venue", True))
        held = float((self.store.get_position(sym) or {}).get("qty") or 0.0)
        bad = None
        if side == "buy" and held < -EPS:
            bad = f"{sym} is held short — cover it first"
        elif side == "short" and held > EPS:
            bad = f"{sym} is held long — sell it first"
        elif side == "sell" and qty > held + EPS:
            bad = f"sell {qty:g} {sym} exceeds the long position {max(held, 0):g}"
            if long_only:
                bad += f" — {self.id} is long only (a sell below 0 is not possible; blocked, not converted)"
                self._blocked_event(sym, bad, order)
        elif side == "cover" and qty > -held + EPS:
            bad = f"cover {qty:g} {sym} exceeds the short position {max(-held, 0):g}"
        if t == "bracket" and side not in ("buy", "short"):
            bad = "a bracket order opens a position (buy or short)"
        if bad:
            out.append(_res("position_side", False, bad))
            return out
        out.append(_res("position_side", True))
        why = link_block_reason(self, now)
        out.append(_res("broker_link", why is None, why))
        return out


    def _blocked_event(self, sym: str, why: str, order: dict) -> None:
        """A visible account event for an order a long-only broker cannot take (SPEC §18.14 item 5)."""
        try:
            self.store.add_event("order", f"Order blocked: short not possible on {self.id}", sym,
                                 f"{why}. Origin: {order.get('origin') or '?'}. Paper/demo trading · simulated money.")
        except Exception:        # noqa: BLE001 — the check result itself already blocks the order
            logger.exception("blocked-order event failed")


def _venue_name(acc) -> str:
    return {"binance_demo": "Binance"}.get(acc.broker_name, "the broker")


def baseline(store) -> Optional[dict]:
    """binance-demo: the starting balances confirmed at the first connect (None before)."""
    v = store.meta_get(BASELINE_KEY)
    try:
        d = json.loads(v) if v else None
    except ValueError:
        d = None
    return d if isinstance(d, dict) else None


def _res(check: str, ok: bool, message: Optional[str] = None) -> dict:
    return {"check": check, "ok": bool(ok), "message": message}


def link_block_reason(acc, now: Optional[datetime] = None) -> Optional[str]:
    """Why new orders must wait for the broker (SPEC §18.4 ``broker_link``), else None."""
    from . import service
    now = now or accounts.now()
    if service.connection(acc.id)["status"] != "connected":
        return f"{acc.id} is not connected — connect it in Settings → Brokers"
    if acc.broker_name == "binance_demo" and baseline(acc.store) is None:
        return f"{acc.id}: {service.BASELINE_MISSING}"
    msg = acc.link.blocked()
    if msg:
        return f"{msg} — no new orders until the broker answers and reconciles"
    if acc.link.state not in ("ok", "idle"):
        return f"{acc.link.message()} — no new orders until the broker answers and reconciles"
    rs = reconcile_state(acc.store)
    if rs.get("status") != "ok" or not rs.get("at"):
        return (f"the last reconciliation is {rs.get('status')} — no new orders until a reconciliation is ok "
                "(Settings → Brokers)")
    try:
        age = now - datetime.fromisoformat(rs["at"])
    except ValueError:
        age = RECONCILE_MAX_AGE * 2
    if age > RECONCILE_MAX_AGE:
        return f"the last reconciliation is {int(age.total_seconds() // 60)} min old — waiting for a fresh one"
    return None


# --- the adapter the OMS / runtime see --------------------------------------------------------------------

class MirroredAdapter(BrokerAdapter):
    """OMS-facing adapter of a broker account: sends to the real broker (with link bookkeeping), books fills into
    the mirror, and makes the runtime's PaperBroker housekeeping steps no-ops (the broker does those itself)."""

    paper = True

    def __init__(self, acc: BrokerAccount):
        self.acc = acc
        self.name = acc.broker_name
        self.capabilities = getattr(acc.remote, "capabilities", {})
        self.endpoint = getattr(acc.remote, "endpoint", None)

    _tl = threading.local()

    @contextmanager
    def user_attempt(self):
        """A user-triggered action (manual reconcile, resolve, kill) makes ONE real attempt even while the link
        backs off: every call inside tries the broker until the first failure; after it the rest of the action
        is blocked as usual (SPEC §18.13 review fix L-a)."""
        prev = getattr(self._tl, "mode", None)
        self._tl.mode = "trying" if prev is None else prev
        try:
            yield
        finally:
            self._tl.mode = prev

    def remote_call(self, fn_name: str, *args, user: bool = False, **kw):
        why = accounts.no_call_reason(self.acc.id)
        if why:                          # retired / sandbox-blocked broker: NO call at all, not even a user one
            from .common import BrokerSetupError
            raise BrokerSetupError(f"{self.acc.id}: {why}")
        lk = self.acc.link
        mode = getattr(self._tl, "mode", None)
        if mode == "trying":
            user = True
        if not user:
            msg = lk.blocked()
            if msg:
                raise unavailable(msg, connection=True)
        try:
            out = getattr(self.acc.remote, fn_name)(*args, **kw)
        except Exception as ex:      # noqa: BLE001 — the state is recorded, then re-raised as the OMS expects
            state = classify_failure(ex)
            msg = getattr(ex, "user_message", None) or f"{type(ex).__name__}"
            lk.fail(state, msg)
            if mode == "trying":
                self._tl.mode = "failed"
            if isinstance(ex, BrokerUnavailable):
                raise
            raise unavailable(f"{self.acc.id}: {msg}", ex, connection=(state == "waiting")) from ex
        lk.ok()
        return out

    # mirror-book views (the OMS values the account with these)
    def account(self, prices: Optional[dict] = None) -> dict:
        return self.acc.broker.account(prices or {}).to_dict()

    def positions(self, prices: Optional[dict] = None) -> List[dict]:
        from .paper import InternalPaperAdapter
        return InternalPaperAdapter.positions(self, prices)  # type: ignore[arg-type]

    @property
    def broker(self):
        return self.acc.broker

    def submit(self, order: dict) -> dict:
        from . import live_guard
        live_guard.require_paper_order(self.acc.id, order)
        o = dict(order)
        o.pop("prices", None)
        o.pop("now", None)
        # protective orders (kill / drawdown) always try the broker, even while the link backs off
        return self.remote_call("submit", o, user=o.get("origin") in ("kill", "drawdown"))

    def _oms_row(self, cid: str) -> Optional[dict]:
        from .. import oms
        return oms.get(cid)

    def order(self, client_order_id: str) -> Optional[dict]:
        row = self._oms_row(client_order_id)
        parent = (row or {}).get("parent_client_order_id")
        if parent:
            pa = self.remote_call("order", parent)
            if pa is None:
                return None
            for leg in pa.get("legs") or []:
                if str(leg.get("broker_order_id")) == str(row.get("broker_order_id")):
                    return dict(leg, client_order_id=client_order_id, symbol=pa.get("symbol"))
            return None
        return self.remote_call("order", client_order_id)

    def cancel(self, client_order_id: str, reason: str = "cancelled") -> bool:
        row = self._oms_row(client_order_id) or {}
        return bool(self.remote_call("cancel", client_order_id, reason, broker_order_id=row.get("broker_order_id"),
                                     user=True))

    def fills(self, after: int = 0, limit: int = 500) -> List[dict]:
        from .paper import InternalPaperAdapter
        return InternalPaperAdapter.fills(self, after, limit)  # type: ignore[arg-type]

    @property
    def store(self):
        return self.acc.store

    # runtime housekeeping (SPEC §17.3) — the broker accrues, fills and margins itself
    def daily_accruals(self, now, prices) -> dict:
        return {}

    def process_pending(self, now, prices) -> list:
        return []

    def check_margin(self, now, prices) -> list:
        return []

    def snapshot_equity(self, now, prices, benchmark_price=None) -> dict:
        with self.acc.broker._lock:
            with self.acc.store.atomic():
                from strategy_platform.execution.paper_broker import PaperBroker
                return PaperBroker.snapshot_equity(self.acc.broker, now, benchmark_price, prices)

    def cancel_pending_all(self, reason: str) -> List[dict]:
        return []

    def cancel_all(self, user: bool = True) -> int:
        return int(self.remote_call("cancel_all", user=user) or 0)

    def snapshot(self, user: bool = False) -> dict:
        return self.remote_call("snapshot", user=user)


# --- fill booking -----------------------------------------------------------------------------------------

def _apply_position(store, sym: str, signed: float, px: float, fee: float, ts: str, oid: int, side: str) -> None:
    from strategy_platform.core import params as fund_params
    from strategy_platform.execution.paper_broker import fmt_qty
    crypto = fund_params.is_crypto(sym)
    notional = abs(signed) * px
    sign = -1.0 if signed > 0 else 1.0           # cash leaves on a buy/cover
    tag = f"{side} {fmt_qty(abs(signed))} {sym} @ {px:.4f} (order {oid})"
    store.add_cash_entry(ts, "fill", sign * notional, symbol=sym, note=tag)
    if fee:
        store.add_cash_entry(ts, "fee", -fee, symbol=sym, note=tag)
    pos = store.get_position(sym) or {}
    q = float(pos.get("qty") or 0.0)
    avg = float(pos.get("avg_price") or 0.0)
    realized = float(pos.get("realized_pnl") or 0.0) - fee
    opened_at = pos.get("opened_at")
    new_q = q + signed
    if abs(q) < EPS or (q > 0) == (signed > 0):
        avg = (abs(q) * avg + abs(signed) * px) / abs(new_q) if abs(new_q) > EPS else 0.0
        if abs(q) < EPS:
            opened_at = ts
    else:
        closed = min(abs(signed), abs(q))
        realized += (px - avg) * closed * (1.0 if q > 0 else -1.0)
        if abs(signed) > abs(q) + EPS:          # flipped: the rest opens at the fill price
            avg, opened_at = px, ts
    if crypto:
        new_q = round(new_q, fund_params.CRYPTO_QTY_DECIMALS)
    else:
        new_q = float(round(new_q, 6))
    if abs(new_q) < EPS:
        new_q, opened_at = 0.0, None
    store.upsert_position(sym, new_q, avg if new_q else (avg or None), updated_at=ts, opened_at=opened_at,
                          realized_pnl=realized, borrow_rate_pct=None, last_price=px)


def _book_delta(acc: BrokerAccount, cid: str, side: str, origin: str, order_type: str, a: dict) -> int:
    boid = a.get("broker_order_id")
    if not boid or boid == "None":
        return 0
    fq = fnum(a.get("filled_qty")) or 0.0
    if fq <= EPS:
        return 0
    avg = fnum(a.get("avg_price"))
    if avg is None or avg <= 0:
        logger.warning("%s: fill of %s without a price — booked when the broker reports it", acc.id, cid)
        return 0
    fees = fnum(a.get("fees")) or 0.0
    sym = a.get("symbol")
    store = acc.store
    with acc.broker._lock:
        with store.atomic() as conn:
            m = conn.execute("SELECT * FROM br_fill_marks WHERE account_id = ? AND broker_order_id = ?",
                             (acc.id, str(boid))).fetchone()
            q0 = float(m["filled_qty"]) if m else 0.0
            p0 = float(m["avg_price"] or 0.0) if m else 0.0
            f0 = float(m["fees"] or 0.0) if m else 0.0
            n0 = int(m["bookings"]) if m else 0
            dq = fq - q0
            if dq <= EPS:
                return 0
            px = (fq * avg - q0 * p0) / dq
            if not math.isfinite(px) or px <= 0:
                px = avg
            dfee = max(0.0, fees - f0)
            # binance-demo (SPEC §18.14): a buy's commission taken from the bought asset (cumulative
            # ``fee_base_qty``) — this delta's share is booked net; its value is part of ``fees``
            fbq = fnum(a.get("fee_base_qty")) or 0.0
            dfb = fbq * dq / fq if (fbq > 0 and side == "buy") else 0.0
            n = n0 + 1
            pcid = cid if n == 1 else f"{cid}~{n}"
            ts = accounts.now().astimezone(timezone.utc).isoformat(timespec="seconds")
            row = conn.execute("SELECT symbol FROM oms_orders WHERE client_order_id = ?", (cid,)).fetchone()
            sym = (row["symbol"] if row else None) or sym
            if not sym:
                raise ValueError("fill without a symbol")
            cur = conn.execute(
                "INSERT INTO pa_orders (account_id, ts, symbol, side, qty, order_type, status, broker, broker_order_id,"
                " mode, rationale, origin, fill_price, fees, filled_at, notional, client_order_id) VALUES"
                " (?, ?, ?, ?, ?, ?, 'filled', ?, ?, 'paper', ?, ?, ?, ?, ?, ?, ?)",
                (acc.id, ts, sym, side, dq - dfb, order_type or "market", acc.broker_name, str(boid),
                 f"broker fill of {cid}" + (f" (net of a {dfb:.8g} {sym[:-4]} commission)" if dfb else ""),
                 BROKER_ORIGIN.get(origin, origin), px, dfee, ts, (dq - dfb) * px, pcid))
            oid = int(cur.lastrowid)
            signed = (dq - dfb) if side in ("buy", "cover") else -dq
            _apply_position(store, sym, signed, px, dfee, ts, oid, side)
            conn.execute("INSERT INTO br_fill_marks (account_id, broker_order_id, client_order_id, filled_qty,"
                         " avg_price, fees, bookings, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                         " ON CONFLICT(account_id, broker_order_id) DO UPDATE SET filled_qty = excluded.filled_qty,"
                         " avg_price = excluded.avg_price, fees = excluded.fees, bookings = excluded.bookings,"
                         " updated_at = excluded.updated_at", (acc.id, str(boid), cid, fq, avg, max(fees, f0), n, ts))
    return 1


LEG_SUFFIX = {"limit": "tp", "stop": "sl", "stop_limit": "sl"}
LEG_SIDE = {"buy": "sell", "short": "cover"}


def _ensure_legs(acc: BrokerAccount, parent: dict, legs: List[dict]) -> List[str]:
    """Imports bracket legs as OMS orders ``<parent>.tp`` / ``.sl`` (once). Returns their client ids."""
    from .. import oms
    out = []
    for leg in legs:
        kind = LEG_SUFFIX.get(str(leg.get("type") or ""), "leg")
        cid = f"{parent['client_order_id']}.{kind}"
        if oms.get(cid) is None:
            ts = accounts.now().astimezone(timezone.utc).isoformat(timespec="seconds")
            qty = fnum(leg.get("qty")) or parent.get("qty")
            alloc = parent.get("allocations") or []
            row = {"client_order_id": cid, "account_id": acc.id, "created_at": ts, "updated_at": ts,
                   "symbol": parent["symbol"], "side": LEG_SIDE.get(parent["side"], "sell"), "qty": qty,
                   "origin": parent["origin"], "tif": parent["tif"],
                   "reason": f"bracket {('take profit' if kind == 'tp' else 'stop loss')} of {parent['client_order_id']}",
                   "state": "accepted", "filled_qty": 0.0, "fees": 0.0, "attempts": 1, "allow_reduce": 1,
                   "allocations_json": json.dumps(alloc), "checks_json": "[]",
                   "broker_order_id": str(leg.get("broker_order_id")),
                   "order_type": "limit" if kind == "tp" else "stop",
                   "limit_price": fnum(leg.get("limit_price")), "stop_price": fnum(leg.get("stop_price")),
                   "parent_client_order_id": parent["client_order_id"], "execution_id": parent.get("execution_id")}
            with db._connect() as conn:
                oms._insert(conn, row)
        out.append((cid, leg))
    return out


def book_ack(order: dict, ack: dict) -> None:
    """Books the fills an ack reports (parent + legs) into the account's mirror book (idempotent)."""
    acc = accounts.get(order["account_id"])
    if not getattr(acc, "is_broker", False):
        return
    a = dict(ack, symbol=order["symbol"])
    _book_delta(acc, order["client_order_id"], order["side"], order["origin"], order.get("order_type"), a)
    legs = ack.get("legs") or []
    if legs and not order.get("parent_client_order_id"):
        from .. import oms
        for cid, leg in _ensure_legs(acc, order, legs):
            oms.apply_ack(cid, dict(leg, client_order_id=cid, symbol=order["symbol"]))


def fills_signature(account_id: str) -> int:
    with db._connect() as conn:
        r = conn.execute("SELECT COALESCE(SUM(bookings), 0) AS n FROM br_fill_marks WHERE account_id = ?",
                         (account_id,)).fetchone()
    return int(r["n"] or 0)
