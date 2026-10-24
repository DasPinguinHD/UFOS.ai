"""Shadow Account normalisation + FIFO lots (SPEC §19.6).

A normalised transaction (``make_txn``): ``{date, kind, key, symbol, isin, name, asset_class, qty, price, currency,
gross, fee, tax, net, raw_type}`` — ``gross``/``fee``/``tax`` ≥ 0, ``net`` the signed cash effect (buy −(gross+fee);
sell +(gross−fee−tax); dividend / interest +(gross−tax); withholding −tax; fee −fee; deposit +gross; withdrawal
−gross). ``key`` = ISIN, else the symbol (upper case).

``Book`` = FIFO lots per key, used by the diagnostics and the shadow flows: a buy adds a lot
(cost = gross + fee), a sell consumes the oldest lots (proceeds = gross − fee, split pro rata), a sell beyond the
held quantity is ``unmatched`` (no invented cost basis) — or, with ``allow_short`` (platform paper accounts), opens a
short lot that a later buy covers. Every lot carries an ``amounts`` dict (e.g. ``{"ccy": …, "eur": …}``) that is
split pro rata (e.g. values in the base currency).
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from typing import Dict, Iterable, List, Optional

KINDS = ("buy", "sell", "dividend", "interest", "withholding", "fee", "deposit", "withdrawal",
         # stage 9c (SPEC §19.16, Trade Republic): a tax refund as reported, a staking reward (crypto delivered
         # without cash; cost basis = market value at receipt, user decision 2026-10-18), a pending private-market
         # pre-payment (cash out, no position yet), an unpaired corporate action (reported, never closes a position)
         "tax_refund", "staking", "pending_buy", "corporate_action")
ACQUISITIONS = ("buy", "staking")          # kinds that open FIFO lots
ASSET_CLASSES = ("share", "etf", "fund", "bond", "crypto", "derivative", "other")
CURRENCIES = ("EUR", "USD", "GBP", "CHF", "CAD", "JPY", "AUD", "SEK", "NOK", "DKK", "HKD", "PLN", "CZK")
QTY_EPS = 1e-9
ISIN_RE_TEXT = r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$"
NAME_MAX = 200
SYMBOL_MAX = 32


class TxnError(ValueError):
    def __init__(self, field: str, message: str):
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


def _fin(v, field: str, required: bool = False, nonneg: bool = True) -> Optional[float]:
    if v is None:
        if required:
            raise TxnError(field, "required")
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise TxnError(field, "not a number")
    if not math.isfinite(f):
        raise TxnError(field, "not a finite number")
    if nonneg and f < 0:
        raise TxnError(field, "must not be negative")
    if abs(f) > 1e12:
        raise TxnError(field, "too large")
    return f


def clean_text(v, limit: int) -> Optional[str]:
    """Untrusted statement text: stripped, control characters removed, truncated (kept as DATA, never evaluated)."""
    if v is None:
        return None
    s = "".join(ch for ch in str(v) if ch == " " or (ch.isprintable() and ch not in "\r\n\t"))
    s = s.strip()
    return s[:limit] or None


EXT_ID_MAX = 255                     # a composed id ("<warrant id>|<tilg id>") included — never truncated (§19.16)
TS_RE_TEXT = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{9}$"
_TIME_RE_TEXT = r"^(\d{1,2}):?(\d{2})(?::?(\d{2}))?(?:[.,](\d{1,9}))?$"


def time9(raw) -> Optional[str]:
    """A statement time ("10:00", "10:00:05", "100005", "10:00:05.123") as sortable "HH:MM:SS.fffffffff"; empty →
    None; anything else → TxnError (review fix 2026-10-18: same-day order follows the real time)."""
    import re
    s = (str(raw) if raw is not None else "").strip()
    if not s:
        return None
    m = re.match(_TIME_RE_TEXT, s)
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59 or int(m.group(3) or 0) > 60:
        raise TxnError("time", f"not a time ({s[:20]!r})")
    return f"{int(m.group(1)):02d}:{m.group(2)}:{m.group(3) or '00'}.{(m.group(4) or '').ljust(9, '0')}"


def make_txn(*, date_: str, kind: str, symbol=None, isin=None, name=None, asset_class=None, qty=None, price=None,
             currency="EUR", gross=None, fee=None, tax=None, raw_type=None, ext_id=None, original_amount=None,
             original_currency=None, fx_rate=None, note=None, ts=None) -> dict:
    import re
    if kind not in KINDS:
        raise TxnError("type", f"unsupported transaction type ({kind})")
    try:
        d = date.fromisoformat(str(date_)[:10]).isoformat()
    except ValueError:
        raise TxnError("date", "not a valid date")
    if not "1970-01-01" <= d <= "2100-12-31":
        raise TxnError("date", "out of range")
    cur = (clean_text(currency, 3) or "").upper()
    if cur not in CURRENCIES:
        raise TxnError("currency", f"unsupported currency ({cur or 'empty'})")
    sym = clean_text(symbol, SYMBOL_MAX)
    sym = sym.upper() if sym else None
    isn = clean_text(isin, 12)
    isn = isn.upper() if isn else None
    if isn is not None and not re.match(ISIN_RE_TEXT, isn):
        raise TxnError("isin", "not a valid ISIN")
    ac = asset_class if asset_class in ASSET_CLASSES else None
    ext = clean_text(ext_id, 100_000)
    if ext is not None and len(ext) > EXT_ID_MAX:
        raise TxnError("transaction_id", f"longer than {EXT_ID_MAX} characters")
    if ts is not None and not re.match(TS_RE_TEXT, str(ts)):
        raise TxnError("time", "not a sortable timestamp")
    q = _fin(qty, "quantity")
    p = _fin(price, "price")
    g = _fin(gross, "amount")
    f = _fin(fee, "fee") or 0.0
    t = _fin(tax, "tax") or 0.0
    if kind in ("buy", "sell", "staking"):
        if not q or q <= QTY_EPS:
            raise TxnError("quantity", "required (> 0) for a buy/sell")
        if not (isn or sym):
            raise TxnError("isin", "an ISIN or a symbol is required for a buy/sell")
        if g is None and p is not None:
            g = q * p
        if g is None:
            raise TxnError("amount", "price or amount required")
        if p is None:
            p = g / q
        if kind == "staking":
            f, t, net = 0.0, 0.0, 0.0                       # no cash: the lot's cost basis is the receipt value
        else:
            net = -(g + f) if kind == "buy" else (g - f - t)
    else:
        if g is None:
            g = 0.0
        if kind in ("dividend", "interest"):
            net = g - t
        elif kind == "withholding":
            t = t or g
            g = 0.0
            net = -t
        elif kind == "fee":
            f = f or g
            g = 0.0
            net = -f
        elif kind == "deposit":
            net = g
        elif kind == "tax_refund":
            t = t or g
            g = 0.0
            net = t
        elif kind == "pending_buy":
            net = -(g + f)
        elif kind == "corporate_action":
            net = 0.0
        else:
            net = -g
        if kind == "dividend" and not (isn or sym):
            raise TxnError("isin", "an ISIN or a symbol is required for a dividend")
    return {"date": d, "kind": kind, "key": isn or sym, "symbol": sym, "isin": isn,
            "name": clean_text(name, NAME_MAX), "asset_class": ac, "qty": q, "price": p, "currency": cur,
            "gross": g, "fee": f, "tax": t, "net": net, "raw_type": clean_text(raw_type, 60),
            "ext_id": ext, "ts": ts, "original_amount": _fin(original_amount, "original_amount", nonneg=False),
            "original_currency": (clean_text(original_currency, 3) or "").upper() or None,
            "fx_rate": _fin(fx_rate, "fx_rate"), "note": clean_text(note, 200)}


def txn_hash(t: dict, occurrence: int) -> str:
    """Dedupe key of a transaction within an account (same content + same occurrence number = duplicate)."""
    core = [t["date"], t["kind"], t.get("key"), _r(t.get("qty")), _r(t.get("price")), _r(t.get("net")),
            t.get("currency"), int(occurrence)]
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode("utf-8")).hexdigest()[:32]


def ext_hash(ext_id: str) -> str:
    """Dedupe key of a transaction that carries the broker's own transaction id (§19.16)."""
    return hashlib.sha256(("ext:" + ext_id).encode("utf-8")).hexdigest()[:32]


def _r(v):
    return None if v is None else round(float(v), 8)


ORDER = {"deposit": 0, "buy": 1, "staking": 1, "dividend": 2, "interest": 2, "withholding": 3, "fee": 3,
         "tax_refund": 3, "pending_buy": 3, "corporate_action": 3, "sell": 4, "withdrawal": 5}


def sort_key(t: dict):
    """Date order; same day: the statement's own time where it has one (``ts``), then deposits, buys, income, sells,
    withdrawals (§19.6), then the position in the parsed file (``seq`` — e.g. a Trade Republic export sorted by its
    datetime), never the original file line (review fix 2026-10-18, §19.16)."""
    seq = t.get("seq")
    return (t["date"], t.get("ts") or "", ORDER.get(t["kind"], 9), seq if seq is not None else (t.get("row") or 0),
            t.get("id") or 0)


class Book:
    """FIFO lots per key (see module docstring)."""

    def __init__(self, allow_short: bool = False):
        self.allow_short = allow_short
        self.lots: Dict[str, List[dict]] = {}      # key -> [{date, qty (signed), amounts, meta}]
        self.closed: List[dict] = []
        self.unmatched: List[dict] = []

    @staticmethod
    def _part(amounts: Dict[str, float], share: float) -> Dict[str, float]:
        return {k: (v * share if v is not None else None) for k, v in amounts.items()}

    def qty(self, key: str) -> float:
        return math.fsum(l["qty"] for l in self.lots.get(key, []))

    def holdings(self) -> Dict[str, dict]:
        out = {}
        for k, lots in self.lots.items():
            q = math.fsum(l["qty"] for l in lots)
            if abs(q) <= QTY_EPS:
                continue
            amt: Dict[str, float] = {}
            for l in lots:
                for a, v in l["amounts"].items():
                    if v is None or amt.get(a, 0.0) is None:
                        amt[a] = None
                    else:
                        amt[a] = amt.get(a, 0.0) + v
            out[k] = {"qty": q, "amounts": amt, "lots": len(lots), "first_date": lots[0]["date"] if lots else None}
        return out

    def buy(self, key: str, day: str, qty: float, amounts: Dict[str, float], meta: Optional[dict] = None) -> List[dict]:
        """Adds a lot — covering short lots first (allow_short). Returns closed parts (covers)."""
        parts = []
        rest = qty
        lots = self.lots.setdefault(key, [])
        while rest > QTY_EPS and lots and lots[0]["qty"] < -QTY_EPS:
            lot = lots[0]
            q = min(rest, -lot["qty"])
            share_lot = q / -lot["qty"]
            proceeds = self._part(lot["amounts"], share_lot)
            cost = self._part(amounts, q / qty)
            parts.append({"key": key, "qty": q, "open_date": lot["date"], "close_date": day, "short": True,
                          "cost": cost, "proceeds": proceeds, "meta": lot.get("meta"), "close_meta": meta})
            lot["amounts"] = self._part(lot["amounts"], 1.0 - share_lot)
            lot["qty"] += q
            rest -= q
            if abs(lot["qty"]) <= QTY_EPS:
                lots.pop(0)
        if rest > QTY_EPS:
            lots.append({"date": day, "qty": rest, "amounts": self._part(amounts, rest / qty), "meta": meta})
        self.closed.extend(parts)
        return parts

    def sell(self, key: str, day: str, qty: float, amounts: Dict[str, float], meta: Optional[dict] = None) -> List[dict]:
        parts = []
        rest = qty
        lots = self.lots.setdefault(key, [])
        while rest > QTY_EPS and lots and lots[0]["qty"] > QTY_EPS:
            lot = lots[0]
            q = min(rest, lot["qty"])
            share_lot = q / lot["qty"]
            cost = self._part(lot["amounts"], share_lot)
            proceeds = self._part(amounts, q / qty)
            parts.append({"key": key, "qty": q, "open_date": lot["date"], "close_date": day, "short": False,
                          "cost": cost, "proceeds": proceeds, "meta": lot.get("meta"), "close_meta": meta})
            lot["amounts"] = self._part(lot["amounts"], 1.0 - share_lot)
            lot["qty"] -= q
            rest -= q
            if lot["qty"] <= QTY_EPS:
                lots.pop(0)
        if rest > QTY_EPS:
            if self.allow_short:
                lots.append({"date": day, "qty": -rest, "amounts": self._part(amounts, rest / qty), "meta": meta})
            else:
                self.unmatched.append({"date": day, "key": key, "qty": rest,
                                       "proceeds": self._part(amounts, rest / qty), "meta": meta})
        self.closed.extend(parts)
        return parts


def holding_days(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def replay(txns: Iterable[dict]) -> Book:
    """FIFO book of a Shadow account's buy/sell transactions in base-free units (amount key ``ccy``)."""
    book = Book(allow_short=False)
    for t in sorted(txns, key=sort_key):
        if t["kind"] in ACQUISITIONS:
            book.buy(t["key"], t["date"], t["qty"], {"ccy": t["gross"] + t["fee"]}, {"currency": t["currency"]})
        elif t["kind"] == "sell":
            book.sell(t["key"], t["date"], t["qty"], {"ccy": t["gross"] - t["fee"]}, {"currency": t["currency"]})
    return book
