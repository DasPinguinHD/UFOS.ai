"""In-memory stand-in for ``fund.store`` used by the backtest engine
(SPEC §8.3): every function ``execution.paper_broker.PaperBroker`` calls on its store,
with the same arguments, return shapes and semantics as the SQLite
implementation (orders / positions / cash ledger / equity curve / events /
meta keys) — so the REAL broker runs unchanged on a simulated account.

Differences that do not change broker behaviour:
- no ``db`` attribute (PaperBroker.ensure_initialized then skips the
  "store enabled" check — an in-memory store is always enabled);
- reads never fail, so the "None when unreadable" branches of fund.store are
  never taken;
- sums use ``math.fsum`` (exactly rounded); SQLite's SUM may differ in the
  last bits — the parity test compares money with a 1e-6 tolerance.

Rows returned are copies (callers may mutate them). Timestamps are
normalised exactly like fund.store (UTC ISO-8601, seconds). Not thread-safe
on its own: the broker serialises all calls with its lock and the engine
runs one simulation per store.

Part of UFOS.ai, a proof-of-concept portfolio project; paper trading only.
"""
from __future__ import annotations

import copy
import math
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

ORDER_UPDATABLE = ("status", "qty", "fill_price", "fees", "filled_at", "reject_reason", "notional",
                   "rationale", "run_id", "recommendation_id", "provenance_json")


def _iso(ts) -> str:
    """datetime/str -> UTC ISO-8601 (seconds), like fund.store._iso (None -> now)."""
    if ts is None:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc).isoformat(timespec="seconds")
    dt = datetime.fromisoformat(str(ts).strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class MemStore:
    """One simulated paper account. ``params`` = the full fund params dict
    returned by ``effective_params()`` (the engine passes ``params_fn`` to the
    broker anyway)."""

    def __init__(self, params: Optional[dict] = None):
        self._params = copy.deepcopy(params) if params is not None else None
        self._orders: List[dict] = []
        self._positions: Dict[str, dict] = {}
        self._ledger: List[dict] = []
        self._equity: Dict[str, dict] = {}
        self._events: List[dict] = []
        self._meta: Dict[str, object] = {}

    # --- params ---------------------------------------------------------------

    def effective_params(self) -> Tuple[int, dict]:
        from strategy_platform.core import params as fund_params
        if self._params is None:
            return 0, fund_params.defaults()
        return 0, fund_params.merge(self._params, None)

    # --- orders ---------------------------------------------------------------

    def insert_order(self, ts, symbol: str, side: str, qty: float, status: str, origin: str,
                     run_id: Optional[int] = None, recommendation_id: Optional[int] = None,
                     rationale: Optional[str] = None, notional: Optional[float] = None,
                     fill_price: Optional[float] = None, fees: Optional[float] = None, filled_at=None,
                     reject_reason: Optional[str] = None, provenance: Optional[dict] = None) -> int:
        oid = len(self._orders) + 1
        self._orders.append({
            "id": oid, "ts": _iso(ts), "symbol": symbol, "side": side, "qty": float(qty), "order_type": "market",
            "limit_price": None, "status": status, "broker": "paper", "broker_order_id": None, "mode": "paper",
            "rationale": rationale, "provenance": copy.deepcopy(provenance), "run_id": run_id,
            "recommendation_id": recommendation_id, "origin": origin, "fill_price": fill_price, "fees": fees,
            "filled_at": None if filled_at is None else _iso(filled_at), "reject_reason": reject_reason,
            "notional": notional})
        return oid

    def update_order(self, order_id: int, **fields) -> bool:
        if "provenance" in fields:
            fields["provenance_json"] = fields.pop("provenance")
        if "filled_at" in fields and fields["filled_at"] is not None:
            fields["filled_at"] = _iso(fields["filled_at"])
        bad = [k for k in fields if k not in ORDER_UPDATABLE]
        if bad:
            raise ValueError(f"not updatable: {bad}")
        if not fields:
            return False
        i = int(order_id) - 1
        if not 0 <= i < len(self._orders):
            return False
        row = self._orders[i]
        for k, v in fields.items():
            if k == "provenance_json":
                row["provenance"] = copy.deepcopy(v)
            elif k == "qty":
                row[k] = float(v)
            else:
                row[k] = v
        return True

    def get_order(self, order_id: int) -> Optional[dict]:
        i = int(order_id) - 1
        return dict(self._orders[i]) if 0 <= i < len(self._orders) else None

    def list_orders(self, limit: int = 100, run_id: Optional[int] = None) -> List[dict]:
        """Newest first (ts DESC, id DESC), like fund.store.list_orders."""
        limit = max(1, min(int(limit), 100000))
        rows = [o for o in self._orders if run_id is None or o["run_id"] == int(run_id)]
        rows = sorted(rows, key=lambda o: (o["ts"], o["id"]), reverse=True)
        return [dict(o) for o in rows[:limit]]

    def all_orders(self) -> List[dict]:
        """Every order, oldest first (id order) — engine result extraction."""
        return [dict(o) for o in self._orders]

    def pending_orders(self, symbol: Optional[str] = None) -> List[dict]:
        return [dict(o) for o in self._orders if o["status"] == "pending" and (not symbol or o["symbol"] == symbol)]

    # --- positions ------------------------------------------------------------

    def get_positions(self, include_flat: bool = False) -> List[dict]:
        return [dict(self._positions[s]) for s in sorted(self._positions)
                if include_flat or self._positions[s]["qty"] != 0]

    def get_position(self, symbol: str) -> Optional[dict]:
        p = self._positions.get(symbol)
        return dict(p) if p is not None else None

    def upsert_position(self, symbol: str, qty: float, avg_price: Optional[float], updated_at=None, opened_at=None,
                        realized_pnl: float = 0.0, borrow_rate_pct: Optional[float] = None,
                        last_price: Optional[float] = None) -> bool:
        self._positions[symbol] = {
            "symbol": symbol, "qty": float(qty), "avg_price": avg_price, "updated_at": _iso(updated_at),
            "opened_at": None if opened_at is None else _iso(opened_at),
            "realized_pnl": float(realized_pnl or 0.0) or 0.0, "borrow_rate_pct": borrow_rate_pct,
            "last_price": last_price}
        return True

    def set_last_prices(self, prices: Dict[str, float], updated_at=None) -> int:
        rows = [(float(p), s) for s, p in (prices or {}).items() if p is not None]
        for p, s in rows:
            if s in self._positions:
                self._positions[s]["last_price"] = p
        return len(rows)

    def set_borrow_rate(self, symbol: str, rate_pct: Optional[float]) -> bool:
        if symbol in self._positions:
            self._positions[symbol]["borrow_rate_pct"] = rate_pct
        return True

    def delete_position(self, symbol: str) -> bool:
        return self._positions.pop(symbol, None) is not None

    def clear_positions(self) -> int:
        n = len(self._positions)
        self._positions.clear()
        return n

    # --- cash ledger ----------------------------------------------------------

    def add_cash_entry(self, ts, kind: str, amount: float, symbol: Optional[str] = None,
                       note: Optional[str] = None) -> int:
        rid = len(self._ledger) + 1
        self._ledger.append({"id": rid, "ts": _iso(ts), "kind": kind, "symbol": symbol, "amount": float(amount),
                             "note": note})
        return rid

    def cash_balance(self) -> float:
        return float(math.fsum(r["amount"] for r in self._ledger))

    def account_snapshot(self) -> dict:
        return {"cash": self.cash_balance(), "positions": self.get_positions(), "ledger_rows": len(self._ledger)}

    def booked_dividend_keys(self, since_day: Optional[str] = None) -> set:
        out = set()
        for r in self._ledger:
            if r["kind"] not in ("dividend", "dividend_short"):
                continue
            note = r["note"] or ""
            if not note.startswith("ex ") or len(note) < 13:
                continue
            day = note[3:13]
            if since_day is None or day >= str(since_day)[:10]:
                out.add((r["symbol"], day))
        return out

    def booked_funding_keys(self, since=None) -> set:
        cut = None if since is None else _iso(since)
        out = set()
        for r in self._ledger:
            if r["kind"] != "funding" or (cut is not None and r["ts"] < cut):
                continue
            note = r["note"] or ""
            if note.startswith("fw ") and len(note) >= 16:
                out.add((r["symbol"], note[3:16]))
        return out

    def net_filled_qty_since(self, symbol: str, since) -> float:
        cut = _iso(since)
        vals = [(o["qty"] if o["side"] in ("buy", "cover") else -o["qty"]) for o in self._orders
                if o["symbol"] == symbol and o["status"] == "filled" and o["filled_at"] is not None
                and o["filled_at"] >= cut]
        return float(math.fsum(vals))

    def ledger_sum(self, kind: str, since=None) -> float:
        cut = None if since is None else _iso(since)
        return float(math.fsum(r["amount"] for r in self._ledger
                               if r["kind"] == str(kind) and (cut is None or r["ts"] >= cut)))

    def cash_entries(self, limit: int = 100, kind: Optional[str] = None, since=None) -> List[dict]:
        """Newest first (id DESC), like fund.store.cash_entries."""
        limit = max(1, min(int(limit), 100000))
        cut = None if since is None else _iso(since)
        rows = [r for r in self._ledger if (not kind or r["kind"] == kind) and (cut is None or r["ts"] >= cut)]
        return [dict(r) for r in reversed(rows)][:limit]

    def all_cash_entries(self) -> List[dict]:
        """Every ledger row, oldest first — engine result extraction."""
        return [dict(r) for r in self._ledger]

    # --- equity curve ---------------------------------------------------------

    def add_equity_point(self, ts, equity: float, cash: float, long_mv: float, short_mv: float,
                         benchmark: Optional[float] = None, drawdown_pct: Optional[float] = None) -> bool:
        key = _iso(ts)
        self._equity[key] = {"ts": key, "equity": float(equity), "cash": float(cash), "long_mv": float(long_mv),
                             "short_mv": float(short_mv), "benchmark": benchmark, "drawdown_pct": drawdown_pct}
        return True

    def equity_points(self, since=None, limit: int = 20000) -> List[dict]:
        limit = max(1, min(int(limit), 200000))
        cut = None if since is None else _iso(since)
        rows = [dict(self._equity[k]) for k in sorted(self._equity) if cut is None or k >= cut]
        return rows[-limit:]

    def equity_point_before(self, ts) -> Optional[dict]:
        cut = _iso(ts)
        keys = [k for k in self._equity if k < cut]
        return dict(self._equity[max(keys)]) if keys else None

    # --- events ---------------------------------------------------------------

    def add_event(self, kind: str, title: str, symbol: Optional[str] = None, body: Optional[str] = None,
                  ts=None) -> int:
        eid = len(self._events) + 1
        self._events.append({"id": eid, "ts": _iso(ts), "kind": kind, "symbol": symbol, "title": title,
                             "body": body})
        return eid

    def events_after(self, after_id: int = 0, limit: int = 50) -> List[dict]:
        limit = max(1, min(int(limit), 500))
        return [dict(e) for e in self._events if e["id"] > int(after_id or 0)][:limit]

    def all_events(self) -> List[dict]:
        return [dict(e) for e in self._events]

    # --- meta keys (fund.* state) -----------------------------------------------

    def get_peak_equity(self) -> Optional[float]:
        return self._meta.get("peak_equity")

    def set_peak_equity(self, value: float) -> bool:
        self._meta["peak_equity"] = float(repr(float(value)))
        return True

    def get_reset_at(self) -> Optional[str]:
        return self._meta.get("reset_at")

    def set_reset_at(self, ts) -> bool:
        self._meta["reset_at"] = _iso(ts)
        return True

    def get_starting_capital(self) -> Optional[float]:
        return self._meta.get("starting_capital")

    def set_starting_capital(self, value: float) -> bool:
        self._meta["starting_capital"] = float(repr(float(value)))
        return True

    def get_last_accrual_date(self) -> Optional[str]:
        return self._meta.get("last_accrual_date")

    def set_last_accrual_date(self, day: str) -> bool:
        self._meta["last_accrual_date"] = str(day)[:10]
        return True

    def get_last_dividend_date(self) -> Optional[str]:
        return self._meta.get("last_dividend_date")

    def set_last_dividend_date(self, day: str) -> bool:
        self._meta["last_dividend_date"] = str(day)[:10]
        return True

    def get_last_funding_at(self) -> Optional[str]:
        return self._meta.get("last_funding_at")

    def set_last_funding_at(self, ts) -> bool:
        self._meta["last_funding_at"] = _iso(ts)
        return True
