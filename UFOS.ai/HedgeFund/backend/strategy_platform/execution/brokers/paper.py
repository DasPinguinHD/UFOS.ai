"""Internal paper broker adapter (SPEC §17.7) — the ONLY adapter of stage 7.

Wraps the REAL ``execution.paper_broker.PaperBroker`` running on a ``PlatformPaperStore``
(hedgefund.db ``pa_*`` tables of one account). Every broker call runs under the
broker's lock inside ONE SQLite transaction (``store.atomic()``): an error or a
crash rolls the whole call back, so the OMS never sees half a fill. The OMS
client order id is written onto the broker's order row (``pa_orders.
client_order_id``) in that same transaction — that is the idempotency key:
``submit`` with a known id returns the stored order instead of sending again.

OMS origins map to the broker's origins: runtime → ``autopilot`` (cancelled by
``cancel_pending("autopilot")`` like the old engine's orders), manual → ``manual``,
kill → ``kill-switch``, drawdown → ``drawdown-stop``, margin → ``margin-call``
(protective origins are never replaced and queue while the market is closed).

Part of UFOS.ai, a proof-of-concept portfolio project; PAPER TRADING ONLY.
"""
from __future__ import annotations

import logging
import re
from typing import Callable, List, Optional

from ..errors import BrokerUnavailable
from .base import BrokerAdapter

logger = logging.getLogger(__name__)

BROKER_ORIGIN = {"runtime": "autopilot", "manual": "manual", "kill": "kill-switch", "drawdown": "drawdown-stop",
                 "margin": "margin-call", "council": "council"}      # council: SP §22.5
OMS_ORIGIN = {v: k for k, v in BROKER_ORIGIN.items()}
_ORDER_REF = re.compile(r"\(order (\d+)\)\s*$")


def ack_of(row: dict) -> dict:
    """Broker order row -> ack dict (base.py)."""
    st = row["status"]
    status = {"pending": "accepted", "filled": "filled", "rejected": "rejected", "cancelled": "cancelled"}.get(st, st)
    filled = float(row["qty"]) if st == "filled" else 0.0
    return {"client_order_id": row.get("client_order_id"), "broker_order_id": str(row["id"]), "status": status,
            "qty": float(row["qty"]), "filled_qty": filled, "avg_price": row.get("fill_price") if st == "filled" else None,
            "fees": float(row.get("fees") or 0.0) if st == "filled" else 0.0, "notional": row.get("notional"),
            "reject_reason": row.get("reject_reason"), "ts": row.get("filled_at") or row.get("ts"),
            "origin": OMS_ORIGIN.get(row.get("origin") or "", row.get("origin")), "symbol": row["symbol"],
            "side": row["side"]}


class InternalPaperAdapter(BrokerAdapter):
    name = "internal_paper"
    paper = True
    capabilities = {"fractional": "crypto only (8 decimals)", "shorting": True, "crypto_perps": True,
                    "hours": "equities: NYSE regular session (after a 5-minute opening grace); crypto 24/7",
                    "order_types": ["market"]}

    def __init__(self, broker, store, clock: Optional[Callable] = None):
        self.broker = broker
        self.store = store
        self.clock = clock or broker.clock

    # --- helpers ------------------------------------------------------------------------------

    def _call(self, fn, *args, **kwargs):
        """fn under the broker lock in ONE store transaction; storage failures -> BrokerUnavailable."""
        from ...storage.db import StorageUnavailable
        try:
            with self.broker._lock:
                with self.store.atomic():
                    return fn(*args, **kwargs)
        except StorageUnavailable as ex:
            raise BrokerUnavailable(f"The paper account's book could not be written ({ex.reason}) — retried.",
                                    detail=str(ex)) from ex

    # --- interface ------------------------------------------------------------------------------

    def account(self, prices: Optional[dict] = None) -> dict:
        return self.broker.account(prices or {}).to_dict()

    def positions(self, prices: Optional[dict] = None) -> List[dict]:
        out = []
        for p in self.broker.positions(prices or {}, True):
            out.append({"symbol": p.symbol, "qty": p.qty, "avg_price": p.avg_price, "last_price": p.last_price,
                        "opened_at": p.opened_at, "realized_pnl": p.realized_pnl, "borrow_rate_pct": p.borrow_rate_pct,
                        "asset_class": p.asset_class, "instrument": p.instrument})
        return out

    def submit(self, order: dict) -> dict:
        from strategy_platform.execution.paper_broker import STATE_UNAVAILABLE
        from strategy_platform.core.types import OrderRequest
        cid = order["client_order_id"]

        def run():
            known = self.store.order_by_client_id(cid)
            if known is not None:
                return ack_of(known)
            req = OrderRequest(symbol=order["symbol"], side=order["side"], qty=order.get("qty"),
                               notional=order.get("notional"), origin=BROKER_ORIGIN[order["origin"]],
                               rationale=(order.get("rationale") or None))
            with self.store.client_order_id(cid):
                r = self.broker.submit(req, now=order.get("now"), prices=order.get("prices"),
                                       allow_reduce=bool(order.get("allow_reduce")))
            if r.order_id is None:
                if r.reject_reason == STATE_UNAVAILABLE:
                    raise BrokerUnavailable("The paper account state could not be read — retried.")
                return {"client_order_id": cid, "broker_order_id": None, "status": "rejected", "qty": r.qty,
                        "filled_qty": 0.0, "avg_price": None, "fees": 0.0, "notional": None,
                        "reject_reason": r.reject_reason, "ts": r.ts, "origin": order["origin"],
                        "symbol": order["symbol"], "side": order["side"]}
            row = self.store.get_order(r.order_id)
            return ack_of(row)
        return self._call(run)

    def cancel(self, client_order_id: str, reason: str = "cancelled") -> bool:
        def run():
            row = self.store.order_by_client_id(client_order_id)
            if row is None or row["status"] != "pending":
                return False
            return self.store.update_order(row["id"], status="cancelled", reject_reason=reason[:200])
        return bool(self._call(run))

    def order(self, client_order_id: str) -> Optional[dict]:
        row = self.store.order_by_client_id(client_order_id)
        return ack_of(row) if row is not None else None

    def fills(self, after: int = 0, limit: int = 500) -> List[dict]:
        """Fills in booking order: one per broker ``fill`` ledger row (id = that row's id, monotonic)."""
        out = []
        for r in self.store.cash_entries_after(after, limit * 3):
            if r["kind"] != "fill":
                continue
            m = _ORDER_REF.search(r.get("note") or "")
            o = self.store.get_order(int(m.group(1))) if m else None
            out.append({"id": r["id"], "ts": r["ts"], "symbol": r["symbol"],
                        "client_order_id": (o or {}).get("client_order_id"),
                        "broker_order_id": str(o["id"]) if o else None,
                        "side": (o or {}).get("side"), "qty": (o or {}).get("qty"),
                        "price": (o or {}).get("fill_price"), "fees": (o or {}).get("fees"),
                        "notional": abs(float(r["amount"])),
                        "origin": OMS_ORIGIN.get((o or {}).get("origin") or "", (o or {}).get("origin"))})
            if len(out) >= limit:
                break
        return out

    # --- broker housekeeping (the old engine's cycle steps, each in one transaction) -------------

    def daily_accruals(self, now, prices) -> dict:
        return self._call(self.broker.daily_accruals, now, None, prices)

    def process_pending(self, now, prices) -> list:
        return self._call(self.broker.process_pending, now, None, prices)

    def check_margin(self, now, prices) -> list:
        return self._call(self.broker.check_margin, now, None, prices)

    def snapshot_equity(self, now, prices, benchmark_price=None) -> dict:
        return self._call(self.broker.snapshot_equity, now, benchmark_price, prices)

    def cancel_pending_all(self, reason: str) -> List[dict]:
        def run():
            out = []
            for o in self.store.pending_orders():
                if self.store.update_order(o["id"], status="cancelled", reject_reason=reason[:200]):
                    out.append(o)
            return out
        return self._call(run)

    @staticmethod
    def order_ref(note: Optional[str]) -> Optional[int]:
        m = _ORDER_REF.search(note or "")
        return int(m.group(1)) if m else None
