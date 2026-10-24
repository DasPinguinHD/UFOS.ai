"""Virtual sub-ledgers per strategy source (SPEC §17.5).

Every platform account's cash-ledger row is attributed EXACTLY ONCE to the sources
(cursor ``pa_meta sl_cursor`` advanced in the same transaction as the ``sl_entries``
rows):
- ``deposit``/``reset`` → ``_unallocated`` (kind ``capital``);
- ``fill``/``fee``/``liquidation_fee`` → by the OMS order's ``allocations`` (a fill
  smaller than planned pro rata; equities whole shares by the largest remainder,
  crypto 8 decimals; the last source takes the cash remainder, so sums are exact).
  Orders without allocations (broker margin calls, kill-switch closes) go to the
  sources holding the symbol in the reducing direction, pro rata by quantity;
- ``borrow_fee``/``dividend``/``dividend_short``/``funding`` → per share of the
  account's net position × each source's SIGNED quantity (internal netting: a source
  that is short against another's long pays the lender's share; sums exact);
- anything else → ``_unallocated`` (kind ``other``).
Internal crosses (netting, §17.5) and capital transfers (allocator, §17.4) are
written directly as ``cross`` / ``capital`` entries (no account row).

Invariant (tested): Σ source cash = account cash; Σ source qty = account qty per symbol.

Part of UFOS.ai, a proof-of-concept portfolio project; paper trading only.
"""
from __future__ import annotations

import json
import math
from typing import Dict, Iterable, List, Optional, Tuple

from ..storage import db

CURSOR_KEY = "sl_cursor"
CAPITAL_FLOW = ("capital",)
QTY_KINDS = ("fill", "cross", "migration")
COST_KIND = {"fee": "fees", "borrow_fee": "borrow", "funding": "funding", "dividend": "dividends",
             "dividend_short": "dividends", "liquidation_fee": "liquidation"}
EPS = 1e-9


def _crypto(symbol: Optional[str]) -> bool:
    from strategy_platform.core import params as fund_params
    return bool(symbol) and fund_params.is_crypto(symbol)


def _round(symbol: str, x: float) -> float:
    return round(x, 8) if _crypto(symbol) else float(round(x))


def split_qty(symbol: str, total: float, weights: List[Tuple[str, float]]) -> List[Tuple[str, float]]:
    """Splits ``total`` (≥ 0) over [(source, weight ≥ 0)] — equities whole shares (largest
    remainder), crypto 8 decimals; Σ == total exactly (the last share absorbs rounding)."""
    weights = [(s, max(0.0, float(w))) for s, w in weights]
    wsum = math.fsum(w for _s, w in weights)
    if total <= EPS or wsum <= 0 or not weights:
        return [(s, 0.0) for s, _w in weights]
    raw = [(s, total * w / wsum) for s, w in weights]
    if _crypto(symbol):
        out = [(s, round(v, 8)) for s, v in raw[:-1]]
        out.append((raw[-1][0], round(total - math.fsum(v for _s, v in out), 8)))
        return out
    floors = [(s, math.floor(v + 1e-9), v - math.floor(v + 1e-9)) for s, v in raw]
    left = int(round(total)) - sum(f for _s, f, _r in floors)
    order = sorted(range(len(floors)), key=lambda i: (-floors[i][2], i))
    add = set(order[:max(0, left)])
    return [(s, float(f + (1 if i in add else 0))) for i, (s, f, _r) in enumerate(floors)]


def split_amount(amount: float, parts: List[Tuple[str, float]]) -> List[Tuple[str, float]]:
    """Splits a cash amount by weights [(source, weight)]; the last gets the remainder (exact sum)."""
    wsum = math.fsum(abs(w) for _s, w in parts)
    if not parts:
        return []
    if wsum <= 0:
        return [(parts[0][0], amount)] + [(s, 0.0) for s, _w in parts[1:]]
    out = [(s, amount * abs(w) / wsum) for s, w in parts[:-1]]
    out.append((parts[-1][0], amount - math.fsum(v for _s, v in out)))
    return out


def insert_entries(conn, account_id: str, rows: Iterable[dict], ts: Optional[str] = None) -> int:
    n = 0
    for r in rows:
        if abs(float(r.get("amount") or 0.0)) <= 0 and abs(float(r.get("qty") or 0.0)) <= 0 \
                and r["kind"] not in ("migration",):
            continue
        conn.execute("INSERT INTO sl_entries (account_id, source_id, ts, kind, symbol, qty, price, amount, ref, note)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (account_id, r["source_id"], r.get("ts") or ts or db.utc_now_iso(), r["kind"], r.get("symbol"),
                      float(r.get("qty") or 0.0), r.get("price"), float(r.get("amount") or 0.0), r.get("ref"),
                      (r.get("note") or None) and str(r["note"])[:300]))
        n += 1
    return n


def entries(account_id: str, source_id: Optional[str] = None, *, after_id: int = 0, limit: Optional[int] = None,
            newest_first: bool = False) -> List[dict]:
    q = "SELECT * FROM sl_entries WHERE account_id = ? AND id > ?"
    args: list = [account_id, int(after_id)]
    if source_id is not None:
        q += " AND source_id = ?"
        args.append(source_id)
    q += " ORDER BY id" + (" DESC" if newest_first else "")
    if limit is not None:
        q += " LIMIT ?"
        args.append(int(limit))
    with db._connect() as conn:
        return [dict(r) for r in conn.execute(q, args).fetchall()]


def holdings(account_id: str) -> Dict[str, Dict[str, float]]:
    """{source: {symbol: qty}} (nonzero only)."""
    with db._connect() as conn:
        rows = conn.execute("SELECT source_id, symbol, SUM(qty) AS q FROM sl_entries WHERE account_id = ? AND symbol IS"
                            " NOT NULL GROUP BY source_id, symbol", (account_id,)).fetchall()
    out: Dict[str, Dict[str, float]] = {}
    for r in rows:
        q = _round(r["symbol"], float(r["q"] or 0.0)) if not _crypto(r["symbol"]) else round(float(r["q"] or 0.0), 8)
        if abs(q) > EPS:
            out.setdefault(r["source_id"], {})[r["symbol"]] = q
    return out


def source_ids(account_id: str) -> List[str]:
    with db._connect() as conn:
        return [r[0] for r in conn.execute("SELECT DISTINCT source_id FROM sl_entries WHERE account_id = ? ORDER BY"
                                           " source_id", (account_id,)).fetchall()]


# --- attribution of the account ledger -----------------------------------------------------------------

def _oms_allocations(conn, account_id: str, client_order_id: Optional[str]) -> Optional[List[Tuple[str, float]]]:
    if not client_order_id:
        return None
    # a broker fill booked in several deltas carries "<cid>~<n>" on its later rows (SPEC §18.4)
    client_order_id = client_order_id.split("~", 1)[0]
    r = conn.execute("SELECT allocations_json FROM oms_orders WHERE client_order_id = ? AND account_id = ?",
                     (client_order_id, account_id)).fetchone()
    if r is None or not r["allocations_json"]:
        return None
    alloc = json.loads(r["allocations_json"])
    out = [(a["source_id"], float(a["qty"])) for a in alloc if float(a.get("qty") or 0) > 0]
    return out or None


def _by_holdings(hold: Dict[str, Dict[str, float]], symbol: str, side: str) -> List[Tuple[str, float]]:
    """Sources holding ``symbol`` in the direction a reducing ``side`` closes (sell → longs, cover → shorts)."""
    want = 1.0 if side == "sell" else -1.0 if side == "cover" else 0.0
    if want == 0.0:
        return []
    return sorted((s, abs(q.get(symbol, 0.0))) for s, q in hold.items()
                  if q.get(symbol, 0.0) * want > EPS)


def attribute(account) -> int:
    """Attributes the account's new cash-ledger rows (see module docstring). Returns the number of
    rows attributed. Runs under the account broker's lock in ONE transaction."""
    from ..execution.brokers.paper import InternalPaperAdapter
    store = account.store
    with account.broker._lock:
        with store.atomic() as conn:
            cur = int(store.meta_get(CURSOR_KEY) or 0)
            rows = store.cash_entries_after(cur, 100000)
            if not rows:
                return 0
            hold = _holdings_conn(conn, account.id)
            out: List[dict] = []
            order_split: Dict[int, List[Tuple[str, float]]] = {}
            for r in rows:
                kind, sym, amt, ts = r["kind"], r["symbol"], float(r["amount"]), r["ts"]
                ref = f"ledger:{r['id']}"
                if kind in ("deposit", "reset"):
                    out.append({"source_id": account.default_source(), "kind": "capital", "amount": amt,
                                "ref": ref, "ts": ts, "note": r["note"]})
                elif kind in ("fill", "fee", "liquidation_fee") and sym:
                    oid = InternalPaperAdapter.order_ref(r["note"])
                    order = store.get_order(oid) if oid else None
                    if order is None:
                        out.append({"source_id": "_unallocated", "kind": "other", "symbol": sym, "amount": amt,
                                    "ref": ref, "ts": ts, "note": r["note"]})
                        continue
                    split = order_split.get(oid)
                    if split is None:
                        filled = float(order["qty"])
                        alloc = _oms_allocations(conn, account.id, order.get("client_order_id"))
                        if alloc is None:
                            alloc = _by_holdings(hold, sym, order["side"])
                        split = split_qty(sym, filled, alloc) if alloc else []
                        rest = filled - math.fsum(q for _s, q in split)
                        if rest > EPS:
                            split.append(("_unallocated", _round(sym, rest)))
                        order_split[oid] = split
                    sign = 1.0 if order["side"] in ("buy", "cover") else -1.0
                    amounts = split_amount(amt, split)
                    for (src, q), (_s, a) in zip(split, amounts):
                        e = {"source_id": src, "kind": kind, "symbol": sym,
                             "amount": a, "ref": ref, "ts": ts, "note": r["note"]}
                        if kind == "fill":
                            e.update(qty=sign * q, price=order.get("fill_price"))
                            hold.setdefault(src, {})
                            hold[src][sym] = _round(sym, hold[src].get(sym, 0.0) + sign * q)
                        out.append(e)
                elif kind in ("borrow_fee", "dividend", "dividend_short", "funding") and sym:
                    parts = [(s, q[sym]) for s, q in sorted(hold.items()) if abs(q.get(sym, 0.0)) > EPS]
                    net = math.fsum(q for _s, q in parts)
                    if not parts or abs(net) <= EPS:
                        out.append({"source_id": "_unallocated", "kind": "other", "symbol": sym, "amount": amt,
                                    "ref": ref, "ts": ts, "note": r["note"]})
                        continue
                    per_share = amt / net
                    vals = [(s, per_share * q) for s, q in parts[:-1]]
                    vals.append((parts[-1][0], amt - math.fsum(v for _s, v in vals)))
                    for s, v in vals:
                        out.append({"source_id": s, "kind": kind, "symbol": sym, "amount": v, "ref": ref, "ts": ts,
                                    "note": r["note"]})
                else:
                    out.append({"source_id": "_unallocated", "kind": "other", "symbol": sym, "amount": amt,
                                "ref": ref, "ts": ts, "note": f"{kind}: {r['note'] or ''}"})
            insert_entries(conn, account.id, out)
            store.meta_set(CURSOR_KEY, str(rows[-1]["id"]))
            return len(rows)


def _holdings_conn(conn, account_id: str) -> Dict[str, Dict[str, float]]:
    rows = conn.execute("SELECT source_id, symbol, SUM(qty) AS q FROM sl_entries WHERE account_id = ? AND symbol IS NOT"
                        " NULL GROUP BY source_id, symbol", (account_id,)).fetchall()
    out: Dict[str, Dict[str, float]] = {}
    for r in rows:
        q = round(float(r["q"] or 0.0), 8) if _crypto(r["symbol"]) else float(round(float(r["q"] or 0.0)))
        if abs(q) > EPS:
            out.setdefault(r["source_id"], {})[r["symbol"]] = q
    return out


# --- source state (fold) ---------------------------------------------------------------------------------

def fold(rows: List[dict], marks: Optional[Dict[str, Optional[float]]] = None, half_spread=None) -> dict:
    """Folds a source's entries (oldest first) into {cash, capital_in, positions: {symbol: {qty, avg_price}},
    realized, costs, last_entry_id}; with ``marks`` also equity / unrealized / exposure (None when a held
    symbol has no mark). ``half_spread(symbol) -> h`` gives the slippage cost of fills (informational)."""
    cash = math.fsum(float(r["amount"] or 0.0) for r in rows)
    capital_in = math.fsum(float(r["amount"] or 0.0) for r in rows if r["kind"] in CAPITAL_FLOW)
    pos: Dict[str, dict] = {}
    realized = 0.0
    costs = {"fees": 0.0, "slippage": 0.0, "borrow": 0.0, "funding": 0.0, "dividends": 0.0, "liquidation": 0.0}
    for r in rows:
        k = r["kind"]
        if k in COST_KIND:
            v = float(r["amount"] or 0.0)
            if k in ("fee", "borrow_fee", "liquidation_fee"):
                costs[COST_KIND[k]] += -v
                realized += v
            else:
                costs[COST_KIND[k]] += v
                realized += v
        if k in QTY_KINDS and r.get("symbol") and abs(float(r["qty"] or 0.0)) > EPS and r.get("price") is not None:
            sym, dq, p = r["symbol"], float(r["qty"]), float(r["price"])
            if k == "fill" and half_spread is not None:
                h = half_spread(sym)
                if h:
                    ref = p / (1.0 + h) if dq > 0 else p / (1.0 - h)
                    costs["slippage"] += abs(dq) * abs(p - ref)
            st = pos.setdefault(sym, {"qty": 0.0, "avg_price": 0.0})
            q, a = st["qty"], st["avg_price"]
            if abs(q) <= EPS or (q > 0) == (dq > 0):
                nq = q + dq
                st["avg_price"] = (abs(q) * a + abs(dq) * p) / abs(nq) if abs(nq) > EPS else 0.0
                st["qty"] = nq
            else:
                closed = min(abs(dq), abs(q))
                realized += closed * (p - a) * (1.0 if q > 0 else -1.0)
                nq = q + dq
                if abs(nq) <= EPS:
                    st.update(qty=0.0, avg_price=0.0)
                elif (nq > 0) != (q > 0):
                    st.update(qty=nq, avg_price=p)
                else:
                    st["qty"] = nq
            st["qty"] = round(st["qty"], 8) if _crypto(sym) else float(round(st["qty"]))
    positions = {s: v for s, v in pos.items() if abs(v["qty"]) > EPS}
    out = {"cash": cash, "capital_in": capital_in, "positions": positions, "realized": realized,
           "costs": costs, "last_entry_id": rows[-1]["id"] if rows else 0}
    if marks is not None:
        long_mv = short_mv = unreal = 0.0
        valued = True
        for s, v in positions.items():
            m = marks.get(s)
            if m is None:
                valued = False
                continue
            mv = v["qty"] * float(m)
            if mv >= 0:
                long_mv += mv
            else:
                short_mv += -mv
            unreal += v["qty"] * (float(m) - v["avg_price"])
        out.update(valued=valued, long_mv=long_mv if valued else None, short_mv=short_mv if valued else None,
                   equity=(cash + long_mv - short_mv) if valued else None, unrealized=unreal if valued else None)
    return out


def source_state(account_id: str, source_id: str, marks=None, half_spread=None) -> dict:
    return fold(entries(account_id, source_id), marks, half_spread)


def all_states(account_id: str, marks=None, half_spread=None) -> Dict[str, dict]:
    rows = entries(account_id)
    by: Dict[str, List[dict]] = {}
    for r in rows:
        by.setdefault(r["source_id"], []).append(r)
    return {s: fold(v, marks, half_spread) for s, v in sorted(by.items())}


def snapshot(account_id: str, source_id: str, day: str, ts: str, st: dict) -> None:
    if st.get("equity") is None:
        return
    with db._connect() as conn:
        conn.execute("INSERT OR REPLACE INTO sl_snapshots (account_id, source_id, day, ts, equity, cash, long_mv,"
                     " short_mv) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (account_id, source_id, day, ts, float(st["equity"]), float(st["cash"]),
                      float(st.get("long_mv") or 0.0), float(st.get("short_mv") or 0.0)))


def snapshots(account_id: str, source_id: str, since_day: Optional[str] = None) -> List[dict]:
    q = "SELECT * FROM sl_snapshots WHERE account_id = ? AND source_id = ?"
    args: list = [account_id, source_id]
    if since_day:
        q += " AND day >= ?"
        args.append(since_day)
    with db._connect() as conn:
        return [dict(r) for r in conn.execute(q + " ORDER BY day", args).fetchall()]


def capital_flows(account_id: str, source_id: str) -> List[Tuple[str, float]]:
    """[(ts, amount)] of capital entries (allocator transfers / deposits) — excluded from returns."""
    with db._connect() as conn:
        rows = conn.execute("SELECT ts, amount FROM sl_entries WHERE account_id = ? AND source_id = ? AND kind ="
                            " 'capital' ORDER BY id", (account_id, source_id)).fetchall()
    return [(r["ts"], float(r["amount"])) for r in rows]


def daily_returns(account_id: str, source_id: str, since_day: Optional[str] = None) -> List[Tuple[str, float]]:
    """[(day, return)] between consecutive daily snapshots, capital flows removed:
    r = (E_t − flows in (t−1, t]) / E_{t−1} − 1."""
    snaps = snapshots(account_id, source_id, since_day)
    flows = capital_flows(account_id, source_id)
    out = []
    for prev, cur in zip(snaps, snaps[1:]):
        if prev["equity"] <= 0:
            continue
        f = math.fsum(a for ts, a in flows if prev["ts"] < ts <= cur["ts"])
        out.append((cur["day"], (cur["equity"] - f) / prev["equity"] - 1.0))
    return out
