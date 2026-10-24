"""SQLite book of one platform paper account (SPEC §17.1, §17.8) — every function
``execution.paper_broker.PaperBroker`` calls on its store, with the same arguments, return
shapes and semantics as ``fund.store`` (newsroom.db) and ``backtest/memstore.py``,
on the hedgefund.db tables ``pa_orders``, ``pa_positions``, ``pa_cash_ledger``,
``pa_equity``, ``pa_events``, ``pa_meta`` scoped by ``account_id``. So the REAL
broker runs unchanged on a platform account (one cost model for backtest, the old
engine and the platform).

Additions:
- ``atomic()``: every store call inside uses ONE connection and ONE transaction
  (BEGIN IMMEDIATE … COMMIT); the broker adapter wraps each broker call in it, so
  a crash or an error never leaves half a fill (order row without its ledger rows
  or position). Nested ``atomic()`` joins the outer one.
- ``client_order_id(cid)``: context naming the OMS client order id that the next
  ``insert_order`` calls record (the adapter's idempotency key, SPEC §17.7).
- Reads that ``fund.store`` answers with None when the database is unreadable
  (``account_snapshot``, ``cash_balance``, ``booked_*_keys``, ``ledger_sum``,
  ``net_filled_qty_since``) do the same here, so the broker fails closed exactly as
  with the old store (StateUnavailable); other failures raise ``StorageUnavailable``.

Part of UFOS.ai, a proof-of-concept portfolio project; PAPER TRADING ONLY.
"""
from __future__ import annotations

import copy
import json
import logging
import math
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from ..storage import db

logger = logging.getLogger(__name__)

ORDER_UPDATABLE = ("status", "qty", "fill_price", "fees", "filled_at", "reject_reason", "notional",
                   "rationale", "run_id", "recommendation_id", "provenance_json")
ORDER_COLS = ("id", "ts", "symbol", "side", "qty", "order_type", "limit_price", "status", "broker",
              "broker_order_id", "mode", "rationale", "provenance_json", "run_id", "recommendation_id", "origin",
              "fill_price", "fees", "filled_at", "reject_reason", "notional", "client_order_id")
POS_COLS = ("symbol", "qty", "avg_price", "updated_at", "opened_at", "realized_pnl", "borrow_rate_pct", "last_price")
META_KEYS = ("peak_equity", "reset_at", "starting_capital", "last_accrual_date", "last_dividend_date",
             "last_funding_at")


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


def _order_out(r) -> dict:
    d = {k: r[k] for k in ORDER_COLS}
    d["provenance"] = json.loads(d.pop("provenance_json")) if r["provenance_json"] else None
    return d


def _pos_out(r) -> dict:
    d = {k: r[k] for k in POS_COLS}
    d["realized_pnl"] = float(d["realized_pnl"] or 0.0) or 0.0
    return d


class PlatformPaperStore:
    """The book of ONE platform paper account. ``params`` = the account's broker
    params (a full fund params dict, ``pf_accounts.params_json``)."""

    def __init__(self, account_id: str, params: Optional[dict] = None):
        if not isinstance(account_id, str) or not account_id:
            raise ValueError("account_id required")
        self.account_id = account_id
        self._params = copy.deepcopy(params) if params is not None else None
        self._tl = threading.local()

    # --- transactions / client order id ------------------------------------------------------------

    @contextmanager
    def atomic(self):
        if getattr(self._tl, "conn", None) is not None:
            yield self._tl.conn
            return
        with db._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._tl.conn = conn
            try:
                yield conn
            finally:
                self._tl.conn = None

    @contextmanager
    def _c(self):
        conn = getattr(self._tl, "conn", None)
        if conn is not None:
            yield conn
            return
        with db._connect() as conn:
            yield conn

    @contextmanager
    def client_order_id(self, cid: Optional[str]):
        prev = getattr(self._tl, "cid", None)
        self._tl.cid = cid
        try:
            yield
        finally:
            self._tl.cid = prev

    # --- params ---------------------------------------------------------------------------------------

    def effective_params(self) -> Tuple[int, dict]:
        from strategy_platform.core import params as fund_params
        if self._params is None:
            return 0, fund_params.defaults()
        return 0, fund_params.merge(self._params, None)

    # --- orders ---------------------------------------------------------------------------------------

    def insert_order(self, ts, symbol: str, side: str, qty: float, status: str, origin: str,
                     run_id: Optional[int] = None, recommendation_id: Optional[int] = None,
                     rationale: Optional[str] = None, notional: Optional[float] = None,
                     fill_price: Optional[float] = None, fees: Optional[float] = None, filled_at=None,
                     reject_reason: Optional[str] = None, provenance: Optional[dict] = None,
                     *, legacy_id: Optional[int] = None, client_order_id: Optional[str] = None) -> int:
        cid = client_order_id if client_order_id is not None else getattr(self._tl, "cid", None)
        with self._c() as conn:
            cur = conn.execute(
                "INSERT INTO pa_orders (account_id, ts, symbol, side, qty, order_type, status, broker, mode, rationale,"
                " provenance_json, run_id, recommendation_id, origin, fill_price, fees, filled_at, reject_reason,"
                " notional, client_order_id, legacy_id) VALUES (?, ?, ?, ?, ?, 'market', ?, 'paper', 'paper', ?, ?, ?,"
                " ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.account_id, _iso(ts), symbol, side, float(qty), status, rationale,
                 json.dumps(provenance, default=str) if provenance is not None else None, run_id, recommendation_id,
                 origin, fill_price, fees, None if filled_at is None else _iso(filled_at), reject_reason, notional,
                 cid, legacy_id))
            return int(cur.lastrowid)

    def update_order(self, order_id: int, **fields) -> bool:
        if "provenance" in fields:
            v = fields.pop("provenance")
            fields["provenance_json"] = json.dumps(v, default=str) if v is not None else None
        if "filled_at" in fields and fields["filled_at"] is not None:
            fields["filled_at"] = _iso(fields["filled_at"])
        bad = [k for k in fields if k not in ORDER_UPDATABLE]
        if bad:
            raise ValueError(f"not updatable: {bad}")
        if not fields:
            return False
        if "qty" in fields:
            fields["qty"] = float(fields["qty"])
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._c() as conn:
            cur = conn.execute(f"UPDATE pa_orders SET {cols} WHERE id = ? AND account_id = ?",
                               tuple(fields.values()) + (int(order_id), self.account_id))
            return cur.rowcount > 0

    def get_order(self, order_id: int) -> Optional[dict]:
        with self._c() as conn:
            r = conn.execute("SELECT * FROM pa_orders WHERE id = ? AND account_id = ?",
                             (int(order_id), self.account_id)).fetchone()
        return _order_out(r) if r else None

    def order_by_client_id(self, cid: str) -> Optional[dict]:
        with self._c() as conn:
            r = conn.execute("SELECT * FROM pa_orders WHERE account_id = ? AND client_order_id = ? ORDER BY id LIMIT 1",
                             (self.account_id, cid)).fetchone()
        return _order_out(r) if r else None

    def orders_by_client_id(self, cid: str) -> List[dict]:
        with self._c() as conn:
            rows = conn.execute("SELECT * FROM pa_orders WHERE account_id = ? AND client_order_id = ? ORDER BY id",
                                (self.account_id, cid)).fetchall()
        return [_order_out(r) for r in rows]

    def list_orders(self, limit: int = 100, run_id: Optional[int] = None) -> List[dict]:
        limit = max(1, min(int(limit), 100000))
        q = "SELECT * FROM pa_orders WHERE account_id = ?"
        args: list = [self.account_id]
        if run_id is not None:
            q += " AND run_id = ?"
            args.append(int(run_id))
        q += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(limit)
        with self._c() as conn:
            return [_order_out(r) for r in conn.execute(q, args).fetchall()]

    def all_orders(self) -> List[dict]:
        with self._c() as conn:
            return [_order_out(r) for r in conn.execute("SELECT * FROM pa_orders WHERE account_id = ? ORDER BY id",
                                                        (self.account_id,)).fetchall()]

    def orders_after(self, after_id: int, limit: int = 500) -> List[dict]:
        with self._c() as conn:
            rows = conn.execute("SELECT * FROM pa_orders WHERE account_id = ? AND id > ? ORDER BY id LIMIT ?",
                                (self.account_id, int(after_id), int(limit))).fetchall()
        return [_order_out(r) for r in rows]

    def pending_orders(self, symbol: Optional[str] = None) -> List[dict]:
        q = "SELECT * FROM pa_orders WHERE account_id = ? AND status = 'pending'"
        args: list = [self.account_id]
        if symbol:
            q += " AND symbol = ?"
            args.append(symbol)
        q += " ORDER BY id"
        with self._c() as conn:
            return [_order_out(r) for r in conn.execute(q, args).fetchall()]

    # --- positions --------------------------------------------------------------------------------------

    def get_positions(self, include_flat: bool = False) -> List[dict]:
        q = "SELECT * FROM pa_positions WHERE account_id = ?" + ("" if include_flat else " AND qty != 0")
        with self._c() as conn:
            return [_pos_out(r) for r in conn.execute(q + " ORDER BY symbol", (self.account_id,)).fetchall()]

    def get_position(self, symbol: str) -> Optional[dict]:
        with self._c() as conn:
            r = conn.execute("SELECT * FROM pa_positions WHERE account_id = ? AND symbol = ?",
                             (self.account_id, symbol)).fetchone()
        return _pos_out(r) if r else None

    def upsert_position(self, symbol: str, qty: float, avg_price: Optional[float], updated_at=None, opened_at=None,
                        realized_pnl: float = 0.0, borrow_rate_pct: Optional[float] = None,
                        last_price: Optional[float] = None) -> bool:
        with self._c() as conn:
            conn.execute(
                "INSERT INTO pa_positions (account_id, symbol, qty, avg_price, updated_at, opened_at, realized_pnl,"
                " borrow_rate_pct, last_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(account_id, symbol) DO"
                " UPDATE SET qty = excluded.qty, avg_price = excluded.avg_price, updated_at = excluded.updated_at,"
                " opened_at = excluded.opened_at, realized_pnl = excluded.realized_pnl,"
                " borrow_rate_pct = excluded.borrow_rate_pct, last_price = excluded.last_price",
                (self.account_id, symbol, float(qty), avg_price, _iso(updated_at),
                 None if opened_at is None else _iso(opened_at), float(realized_pnl or 0.0), borrow_rate_pct,
                 last_price))
        return True

    def set_last_prices(self, prices: Dict[str, float], updated_at=None) -> int:
        rows = [(float(p), self.account_id, s) for s, p in (prices or {}).items() if p is not None]
        with self._c() as conn:
            conn.executemany("UPDATE pa_positions SET last_price = ? WHERE account_id = ? AND symbol = ?", rows)
        return len(rows)

    def set_borrow_rate(self, symbol: str, rate_pct: Optional[float]) -> bool:
        with self._c() as conn:
            conn.execute("UPDATE pa_positions SET borrow_rate_pct = ? WHERE account_id = ? AND symbol = ?",
                         (rate_pct, self.account_id, symbol))
        return True

    def delete_position(self, symbol: str) -> bool:
        with self._c() as conn:
            cur = conn.execute("DELETE FROM pa_positions WHERE account_id = ? AND symbol = ?", (self.account_id, symbol))
            return cur.rowcount > 0

    def clear_positions(self) -> int:
        with self._c() as conn:
            cur = conn.execute("DELETE FROM pa_positions WHERE account_id = ?", (self.account_id,))
            return cur.rowcount

    # --- cash ledger --------------------------------------------------------------------------------------

    def add_cash_entry(self, ts, kind: str, amount: float, symbol: Optional[str] = None,
                       note: Optional[str] = None, *, legacy_id: Optional[int] = None) -> int:
        a = float(amount)
        if not math.isfinite(a):
            raise ValueError("amount must be finite")
        with self._c() as conn:
            cur = conn.execute("INSERT INTO pa_cash_ledger (account_id, ts, kind, symbol, amount, note, legacy_id)"
                               " VALUES (?, ?, ?, ?, ?, ?, ?)", (self.account_id, _iso(ts), kind, symbol, a, note,
                                                                 legacy_id))
            return int(cur.lastrowid)

    def _ledger_rows(self, conn) -> List[dict]:
        return [dict(r) for r in conn.execute("SELECT id, ts, kind, symbol, amount, note FROM pa_cash_ledger"
                                              " WHERE account_id = ? ORDER BY id", (self.account_id,)).fetchall()]

    def cash_balance(self) -> Optional[float]:
        try:
            with self._c() as conn:
                vals = [r[0] for r in conn.execute("SELECT amount FROM pa_cash_ledger WHERE account_id = ? ORDER BY id",
                                                   (self.account_id,)).fetchall()]
        except db.StorageUnavailable:
            if getattr(self._tl, "conn", None) is not None:
                raise
            return None
        return float(math.fsum(vals))

    def account_snapshot(self) -> Optional[dict]:
        try:
            with self._c() as conn:
                vals = [r[0] for r in conn.execute("SELECT amount FROM pa_cash_ledger WHERE account_id = ? ORDER BY id",
                                                   (self.account_id,)).fetchall()]
                pos = [_pos_out(r) for r in conn.execute("SELECT * FROM pa_positions WHERE account_id = ? AND qty != 0"
                                                         " ORDER BY symbol", (self.account_id,)).fetchall()]
        except db.StorageUnavailable:
            if getattr(self._tl, "conn", None) is not None:
                raise
            return None
        return {"cash": float(math.fsum(vals)), "positions": pos, "ledger_rows": len(vals)}

    def booked_dividend_keys(self, since_day: Optional[str] = None) -> Optional[set]:
        out = set()
        with self._c() as conn:
            rows = conn.execute("SELECT symbol, note FROM pa_cash_ledger WHERE account_id = ? AND kind IN"
                                " ('dividend', 'dividend_short')", (self.account_id,)).fetchall()
        for r in rows:
            note = r["note"] or ""
            if not note.startswith("ex ") or len(note) < 13:
                continue
            day = note[3:13]
            if since_day is None or day >= str(since_day)[:10]:
                out.add((r["symbol"], day))
        return out

    def booked_funding_keys(self, since=None) -> Optional[set]:
        cut = None if since is None else _iso(since)
        out = set()
        with self._c() as conn:
            rows = conn.execute("SELECT ts, symbol, note FROM pa_cash_ledger WHERE account_id = ? AND kind = 'funding'",
                                (self.account_id,)).fetchall()
        for r in rows:
            if cut is not None and r["ts"] < cut:
                continue
            note = r["note"] or ""
            if note.startswith("fw ") and len(note) >= 16:
                out.add((r["symbol"], note[3:16]))
        return out

    def net_filled_qty_since(self, symbol: str, since) -> Optional[float]:
        cut = _iso(since)
        with self._c() as conn:
            rows = conn.execute("SELECT side, qty FROM pa_orders WHERE account_id = ? AND symbol = ? AND"
                                " status = 'filled' AND filled_at IS NOT NULL AND filled_at >= ? ORDER BY id",
                                (self.account_id, symbol, cut)).fetchall()
        return float(math.fsum((r["qty"] if r["side"] in ("buy", "cover") else -r["qty"]) for r in rows))

    def ledger_sum(self, kind: str, since=None) -> Optional[float]:
        cut = None if since is None else _iso(since)
        with self._c() as conn:
            rows = conn.execute("SELECT ts, amount FROM pa_cash_ledger WHERE account_id = ? AND kind = ? ORDER BY id",
                                (self.account_id, str(kind))).fetchall()
        return float(math.fsum(r["amount"] for r in rows if cut is None or r["ts"] >= cut))

    def cash_entries(self, limit: int = 100, kind: Optional[str] = None, since=None) -> List[dict]:
        limit = max(1, min(int(limit), 100000))
        cut = None if since is None else _iso(since)
        with self._c() as conn:
            rows = self._ledger_rows(conn)
        rows = [r for r in rows if (not kind or r["kind"] == kind) and (cut is None or r["ts"] >= cut)]
        return list(reversed(rows))[:limit]

    def all_cash_entries(self) -> List[dict]:
        with self._c() as conn:
            return self._ledger_rows(conn)

    def cash_entries_after(self, after_id: int, limit: int = 5000) -> List[dict]:
        with self._c() as conn:
            rows = conn.execute("SELECT id, ts, kind, symbol, amount, note FROM pa_cash_ledger WHERE account_id = ?"
                                " AND id > ? ORDER BY id LIMIT ?", (self.account_id, int(after_id), int(limit))).fetchall()
        return [dict(r) for r in rows]

    # --- equity curve -------------------------------------------------------------------------------------

    def add_equity_point(self, ts, equity: float, cash: float, long_mv: float, short_mv: float,
                         benchmark: Optional[float] = None, drawdown_pct: Optional[float] = None) -> bool:
        with self._c() as conn:
            conn.execute("INSERT OR REPLACE INTO pa_equity (account_id, ts, equity, cash, long_mv, short_mv, benchmark,"
                         " drawdown_pct) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (self.account_id, _iso(ts), float(equity), float(cash), float(long_mv), float(short_mv),
                          benchmark, drawdown_pct))
        return True

    def equity_points(self, since=None, limit: int = 20000) -> List[dict]:
        limit = max(1, min(int(limit), 200000))
        q = "SELECT ts, equity, cash, long_mv, short_mv, benchmark, drawdown_pct FROM pa_equity WHERE account_id = ?"
        args: list = [self.account_id]
        if since is not None:
            q += " AND ts >= ?"
            args.append(_iso(since))
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with self._c() as conn:
            rows = [dict(r) for r in conn.execute(q, args).fetchall()]
        return list(reversed(rows))

    def equity_point_before(self, ts) -> Optional[dict]:
        with self._c() as conn:
            r = conn.execute("SELECT ts, equity, cash, long_mv, short_mv, benchmark, drawdown_pct FROM pa_equity"
                             " WHERE account_id = ? AND ts < ? ORDER BY ts DESC LIMIT 1",
                             (self.account_id, _iso(ts))).fetchone()
        return dict(r) if r else None

    # --- events ---------------------------------------------------------------------------------------------

    def add_event(self, kind: str, title: str, symbol: Optional[str] = None, body: Optional[str] = None,
                  ts=None) -> int:
        with self._c() as conn:
            cur = conn.execute("INSERT INTO pa_events (account_id, ts, kind, symbol, title, body) VALUES (?, ?, ?, ?, ?, ?)",
                               (self.account_id, _iso(ts), kind, symbol, title, body))
            return int(cur.lastrowid)

    def events_after(self, after_id: int = 0, limit: int = 50) -> List[dict]:
        limit = max(1, min(int(limit), 500))
        with self._c() as conn:
            rows = conn.execute("SELECT id, ts, kind, symbol, title, body FROM pa_events WHERE account_id = ? AND id > ?"
                                " ORDER BY id LIMIT ?", (self.account_id, int(after_id or 0), limit)).fetchall()
        return [dict(r) for r in rows]

    def all_events(self) -> List[dict]:
        return self.events_after(0, 500)

    # --- meta -----------------------------------------------------------------------------------------------

    def meta_get(self, key: str) -> Optional[str]:
        with self._c() as conn:
            r = conn.execute("SELECT value FROM pa_meta WHERE account_id = ? AND key = ?", (self.account_id, key)).fetchone()
        return r["value"] if r else None

    def meta_set(self, key: str, value) -> None:
        with self._c() as conn:
            if value is None:
                conn.execute("DELETE FROM pa_meta WHERE account_id = ? AND key = ?", (self.account_id, key))
            else:
                conn.execute("INSERT INTO pa_meta (account_id, key, value) VALUES (?, ?, ?) ON CONFLICT(account_id, key)"
                             " DO UPDATE SET value = excluded.value", (self.account_id, key, str(value)))

    def _float_meta(self, key: str) -> Optional[float]:
        v = self.meta_get(key)
        return float(v) if v is not None else None

    def get_peak_equity(self) -> Optional[float]:
        return self._float_meta("peak_equity")

    def set_peak_equity(self, value: float) -> bool:
        self.meta_set("peak_equity", repr(float(value)))
        return True

    def get_reset_at(self) -> Optional[str]:
        return self.meta_get("reset_at")

    def set_reset_at(self, ts) -> bool:
        self.meta_set("reset_at", _iso(ts))
        return True

    def get_starting_capital(self) -> Optional[float]:
        return self._float_meta("starting_capital")

    def set_starting_capital(self, value: float) -> bool:
        self.meta_set("starting_capital", repr(float(value)))
        return True

    def get_last_accrual_date(self) -> Optional[str]:
        return self.meta_get("last_accrual_date")

    def set_last_accrual_date(self, day: str) -> bool:
        self.meta_set("last_accrual_date", str(day)[:10])
        return True

    def get_last_dividend_date(self) -> Optional[str]:
        return self.meta_get("last_dividend_date")

    def set_last_dividend_date(self, day: str) -> bool:
        self.meta_set("last_dividend_date", str(day)[:10])
        return True

    def get_last_funding_at(self) -> Optional[str]:
        return self.meta_get("last_funding_at")

    def set_last_funding_at(self, ts) -> bool:
        self.meta_set("last_funding_at", _iso(ts))
        return True
