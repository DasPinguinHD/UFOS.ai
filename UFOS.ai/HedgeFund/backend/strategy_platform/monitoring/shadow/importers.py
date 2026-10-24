"""Shadow Account CSV importers (SPEC §19.6): limits, encoding / delimiter detection, schema detection, the
templates Trade Republic (the REAL transaction export, §19.16 — written from a real file), Scalable Capital and IBKR
Flex (both marked "verify against a real export" — written from public descriptions, not from a real file) and the
generic column-mapping profile.

Security: every cell is TEXT (never evaluated); a numeric field that does not parse strictly rejects the row (so
``=cmd|' /C calc'!A0`` in a number column is an error, in a text column it stays inert text that every export
neutralises); limits on bytes, rows, columns and field length; NUL bytes refused. Nothing leaves the computer.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime
from typing import Callable, Dict, List, Optional, Tuple

from . import normalize

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 50_000
MAX_COLUMNS = 64
MAX_FIELD_CHARS = 2048
MAX_ERRORS = 200
PREVIEW_ROWS = 20

LIMITS = {"max_bytes": MAX_BYTES, "max_rows": MAX_ROWS, "max_columns": MAX_COLUMNS, "max_field_chars": MAX_FIELD_CHARS}
CANONICAL = ("date", "type", "symbol", "isin", "name", "quantity", "price", "amount", "fee", "tax", "currency",
             "asset_class", "time", "status")
VERIFY_NOTE = ("Format written from public descriptions, not from a real export — verify against your own export "
               "(check a few rows in the preview before importing).")


class ImportRefused(Exception):
    """The whole file is refused (HTTP 400/409): ``code`` + message (+ ``detection``)."""

    def __init__(self, status: int, code: str, message: str, detection: Optional[dict] = None):
        super().__init__(message)
        self.status, self.code, self.message, self.detection = status, code, message, detection


# --- low level --------------------------------------------------------------------------------------------------

def decode(data: bytes, encoding: str = "auto") -> Tuple[str, str]:
    if len(data) > MAX_BYTES:
        raise ImportRefused(400, "too_large", f"The file is larger than {MAX_BYTES // (1024 * 1024)} MB — split it.")
    if not data.strip():
        raise ImportRefused(400, "empty", "The file is empty.")
    if b"\x00" in data:
        raise ImportRefused(400, "binary", "The file contains NUL bytes — it is not a CSV text file.")
    if encoding in ("auto", "utf-8"):
        try:
            return data.decode("utf-8-sig"), "utf-8"
        except UnicodeDecodeError:
            if encoding == "utf-8":
                raise ImportRefused(400, "encoding", "The file is not valid UTF-8.")
    return data.decode("cp1252", errors="strict") if _cp1252_ok(data) else _bad_encoding(), "cp1252"


def _cp1252_ok(data: bytes) -> bool:
    try:
        data.decode("cp1252")
        return True
    except UnicodeDecodeError:
        return False


def _bad_encoding():
    raise ImportRefused(400, "encoding", "The file is neither UTF-8 nor Windows-1252 text.")


def sniff_delimiter(first_line: str) -> str:
    counts = {d: first_line.count(d) for d in (";", ",", "\t")}
    best = max(counts, key=lambda d: counts[d])
    if counts[best] == 0:
        raise ImportRefused(400, "malformed", "No delimiter (; , or tab) found in the header line.")
    return best


def read_rows(text: str, delimiter: str) -> List[List[str]]:
    rows: List[List[str]] = []
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, quotechar='"', strict=False)
    try:
        for row in reader:
            if len(rows) > MAX_ROWS + 1:
                raise ImportRefused(400, "too_many_rows", f"More than {MAX_ROWS:,} rows — split the file.")
            if len(row) > MAX_COLUMNS:
                raise ImportRefused(400, "too_many_columns", f"Line {reader.line_num} has more than {MAX_COLUMNS} "
                                                             "columns.")
            rows.append(row)
    except csv.Error as ex:
        raise ImportRefused(400, "malformed", f"Malformed CSV near line {reader.line_num}: {str(ex)[:120]}")
    while rows and not any(c.strip() for c in rows[-1]):
        rows.pop()
    if len(rows) - 1 > MAX_ROWS:
        raise ImportRefused(400, "too_many_rows", f"More than {MAX_ROWS:,} rows — split the file.")
    return rows


def norm_header(h: str) -> str:
    return re.sub(r"[\s_]+", "", (h or "").strip().strip("﻿").lower())


_CUR_RE = re.compile(r"^(EUR|USD|€|\$)\s*|\s*(EUR|USD|€|\$)$", re.IGNORECASE)
_NUM_RE = re.compile(r"^[+-]?\d+(\.\d+)?$")


def parse_number(raw: Optional[str], decimal: str, thousands: str) -> Optional[float]:
    """Strict: '1.234,56' (decimal ',', thousands '.'), '-12.5', '€ 3,10'; anything else → ValueError."""
    if raw is None:
        return None
    s = raw.strip().replace(" ", " ").replace("−", "-")
    if s == "":
        return None
    s = _CUR_RE.sub("", s).strip()
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1].strip()
    if thousands:
        s = s.replace(thousands, "")
    s = s.replace(" ", "") if thousands in (" ", "") else s
    if decimal == ",":
        if "." in s and thousands != ".":
            raise ValueError("unexpected '.'")
        s = s.replace(",", ".")
    elif "," in s:
        raise ValueError("unexpected ','")
    if not _NUM_RE.match(s):
        raise ValueError("not a number")
    v = float(s)
    return -v if neg else v


def parse_date(raw: Optional[str], formats: Tuple[str, ...]) -> str:
    """The date of ``raw`` with one of ``formats``; date-times ("2024-01-15T10:00:00Z", "20240115;103000",
    "2024-01-15, 10:00:00") are cut at the first T / space / ; / , after the date part."""
    s = (raw or "").strip()
    if not s:
        raise ValueError("empty")
    cands = [s]
    m = re.match(r"^([^T ;,]+)[T ;,]", s)
    if m:
        cands.append(m.group(1))
    for c in cands:
        for f in formats:
            try:
                return datetime.strptime(c, f).date().isoformat()
            except ValueError:
                continue
    raise ValueError("not a date")


def _date(raw: Optional[str], formats: Tuple[str, ...]) -> str:
    try:
        return parse_date(raw, formats)
    except ValueError:
        raise normalize.TxnError("date", f"not a date ({(raw or '')[:30]!r}; expected {' or '.join(formats)})")


def _gross(kind: str, qty, price, amount, fee: float, tax: float, mode: str) -> Optional[float]:
    """Gross value: qty × price when both exist, else from the amount (``net`` = signed cash, ``gross``)."""
    if qty is not None and price is not None and kind in ("buy", "sell"):
        return abs(qty) * abs(price)
    if amount is None:
        return None
    a = abs(amount)
    if mode == "gross":
        return a
    if kind == "buy":
        return max(0.0, a - fee)
    if kind == "sell":
        return a + fee + tax
    if kind in ("dividend", "interest"):
        return a + tax
    return a


# --- templates ---------------------------------------------------------------------------------------------------

class Template:
    id = ""
    label = ""
    verify = True
    required: Tuple[Tuple[str, ...], ...] = ()       # each entry: alternative header names (normalised)
    type_map: Dict[str, str] = {}

    def describe(self) -> dict:
        return {"id": self.id, "label": self.label, "verify": self.verify, "verify_note": VERIFY_NOTE,
                "required_columns": [alts[0] for alts in self.required], "type_map": dict(self.type_map)}

    def score(self, header: List[str]) -> Tuple[int, List[str]]:
        h = {norm_header(x) for x in header}
        missing = [alts[0] for alts in self.required if not any(norm_header(a) in h for a in alts)]
        return len(self.required) - len(missing), missing

    def parse(self, rows: List[List[str]], emit: Callable, skip: Callable, error: Callable) -> List[str]:
        raise NotImplementedError


def _index(header: List[str]) -> Dict[str, int]:
    return {norm_header(h): i for i, h in enumerate(header)}


def _cell(row: List[str], idx: Dict[str, int], *names: str) -> Optional[str]:
    for n in names:
        i = idx.get(norm_header(n))
        if i is not None and i < len(row):
            v = row[i]
            if len(v) > MAX_FIELD_CHARS:
                raise normalize.TxnError(n, f"field longer than {MAX_FIELD_CHARS} characters")
            return v
    return None


def _num(row, idx, field, decimal, thousands, *names):
    raw = _cell(row, idx, *names)
    try:
        return parse_number(raw, decimal, thousands)
    except ValueError as ex:
        raise normalize.TxnError(field, f"{ex} ({(raw or '')[:40]!r})")


# country code + check digits + 10–30 alphanumerics, compact (any case) or with SINGLE spaces / dashes anywhere
# (blocks of four, uneven blocks, dashes — review fix 2026-10-18); separated forms are matched in upper case only, so
# the next lower-case word is never swallowed
IBAN_RE = re.compile(r"(?i:\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b)|\b[A-Z]{2}\d{2}(?:[ -]?[A-Z0-9]){10,30}\b")
_ISIN_TOKEN_RE = re.compile(r"(?i)^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


def scrub_iban(text: Optional[str]) -> Optional[str]:
    """Removes IBAN-like strings from text that is kept (SPEC §19.16). ISINs stay: a 12-character ISIN is too short to
    match on its own, and a match that STARTS with a whole ISIN token ("US0378331005 XF000…") keeps that token and
    is scanned again after it."""
    if text is None:
        return None
    out, pos = [], 0
    while True:
        m = IBAN_RE.search(text, pos)
        if m is None:
            out.append(text[pos:])
            return "".join(out)
        first = re.split(r"[ -]", m.group(0), maxsplit=1)[0]
        if _ISIN_TOKEN_RE.match(first):
            end = m.start() + len(first)
            out.append(text[pos:end])
            pos = end
            continue
        out.append(text[pos:m.start()])
        out.append("[removed]")
        pos = m.end()


_TR_DT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:?\d{2})?$")


def _tr_when(raw: Optional[str]) -> Tuple[str, str, str, str, str]:
    """Sortable form of a TR datetime ("2024-01-05T10:00:00.123Z" — 3 or 6 fractional digits)."""
    m = _TR_DT_RE.match((raw or "").strip())
    if not m:
        raise normalize.TxnError("datetime", f"not an ISO date-time ({(raw or '')[:40]!r})")
    if m.group(6) not in (None, "Z", "+00:00", "+0000"):
        raise normalize.TxnError("datetime", f"unexpected time zone ({m.group(6)!r}; expected Z)")
    return (m.group(1), m.group(2), m.group(3), m.group(4), (m.group(5) or "").ljust(9, "0"))


def _ts9(when: Tuple[str, str, str, str, str]) -> str:
    """The sortable timestamp of a TR datetime (``_tr_when``) — stored as the transaction's ``ts``."""
    return f"{when[0]}T{when[1]}:{when[2]}:{when[3]}.{when[4]}"


def _split_dt(raw: Optional[str]) -> Tuple[str, str]:
    """A date-time cell ("20240110;103000", "2024-01-10, 10:30:00", "2024-01-10T10:30:00") → (date part, time part)."""
    s = (raw or "").strip()
    m = re.match(r"^([^T ;,]+)[T ;,]+\s*(.*)$", s)
    return (m.group(1), m.group(2).strip()) if m else (s, "")


def _ts_of(d: str, time_raw: Optional[str]) -> Optional[str]:
    t = normalize.time9(time_raw)
    return f"{d}T{t}" if t else None


class TradeRepublic(Template):
    """The REAL Trade Republic transaction export (SPEC §19.16, 2026-10-18): UTF-8, comma, every field quoted, 23
    columns, NOT chronological (sorted here by ``datetime``), dot decimals, signed values (a sale's shares and every
    fee are negative, ``amount`` is the cash — negative for a purchase; tax negative when paid, positive when
    refunded). ``transaction_id`` is the dedupe key across overlapping exports.

    Privacy (user decision 2026-10-18): ``counterparty_name``, ``counterparty_iban``, ``payment_reference`` and
    ``mcc_code`` are never read; ``description`` is never stored; transfers keep only date, kind, amount and currency
    (plus the transaction id); IBAN-like text is removed from every kept name."""
    id = "trade_republic"
    label = "Trade Republic (transaction export CSV — datetime,date,account_type,category,type,asset_class,…)"
    verify = False
    COLUMNS = ("datetime", "date", "account_type", "category", "type", "asset_class", "name", "symbol", "shares",
               "price", "amount", "fee", "tax", "currency", "original_amount", "original_currency", "fx_rate",
               "description", "transaction_id", "counterparty_name", "counterparty_iban", "payment_reference",
               "mcc_code")
    required = tuple((c,) for c in COLUMNS)
    ASSET = {"STOCK": "share", "FUND": "fund", "BOND": "bond", "CRYPTO": "crypto", "DERIVATIVE": "derivative",
             "PRIVATE_FUND": "other"}
    TRANSFERS = {"TRANSFER_INSTANT_INBOUND": "deposit", "CUSTOMER_INPAYMENT": "deposit",
                 "TRANSFER_INSTANT_OUTBOUND": "withdrawal"}
    # (category, type) → the handler below; anything else is skipped with its reason
    type_map = {"TRADING/BUY": "buy", "TRADING/SELL": "sell", "CASH/DIVIDEND": "dividend",
                "CASH/INTEREST_PAYMENT": "interest", "CASH/TAX_OPTIMIZATION": "tax_refund",
                "CASH/PRIVATE_MARKET_BUY": "pending_buy", "CASH/TILG": "sell (with WARRANT_EXERCISE)",
                "CORPORATE_ACTION/WARRANT_EXERCISE": "sell (with TILG)", "DELIVERY/FREE_RECEIPT": "staking",
                "CASH/TRANSFER_INSTANT_INBOUND": "deposit", "CASH/CUSTOMER_INPAYMENT": "deposit",
                "CASH/TRANSFER_INSTANT_OUTBOUND": "withdrawal"}
    PAIR_DAYS = 31                                   # a TILG further away than this is no partner
    ID_MAX = 120                                     # a longer transaction id is a row error (never truncated)

    def describe(self) -> dict:
        d = super().describe()
        d["privacy"] = ("counterparty_name, counterparty_iban, payment_reference, mcc_code and description are never "
                        "stored; transfers keep only date, kind, amount and currency.")
        return d

    def parse(self, rows, emit, skip, error):
        idx = _index(rows[0])
        recs = []
        for n, row in enumerate(rows[1:], start=2):
            if not any(c.strip() for c in row):
                continue
            try:
                r = {c: (_cell(row, idx, c) or "").strip() for c in self.COLUMNS[:19]}   # never the counterparty part
                when = _tr_when(r["datetime"])
                if len(r["transaction_id"]) > self.ID_MAX:
                    raise normalize.TxnError("transaction_id", f"longer than {self.ID_MAX} characters")
                recs.append((when, n, r))
            except normalize.TxnError as ex:
                error(n, ex.field, ex.message)
        recs.sort(key=lambda x: (x[0], x[1]))
        # a transaction id twice in ONE file (review fix 2026-10-18): the same content → skipped (imported once);
        # different content → a row error — never silently one of them
        seen: Dict[str, dict] = {}
        kept = []
        for when, n, r in recs:
            tid = r["transaction_id"]
            if tid and tid in seen:
                if seen[tid] == r:
                    skip(n, "repeated row (same transaction_id and content) — imported once")
                else:
                    error(n, "transaction_id", "duplicate transaction_id with different content")
                continue
            if tid:
                seen[tid] = r
            kept.append((when, n, r))
        recs = kept
        warrants, tilgs, unknown = [], [], {}
        out = []                                      # (sort, n, txn)
        for when, n, r in recs:
            ct = f"{r['category'].upper()}/{r['type'].upper()}"
            try:
                if ct == "CORPORATE_ACTION/WARRANT_EXERCISE":
                    warrants.append((when, n, r))
                elif ct == "CASH/TILG":
                    tilgs.append((when, n, r))
                elif ct in self.type_map:
                    for t in self._row(ct, r):
                        out.append((when, n, dict(t, ts=_ts9(when))))
                else:
                    unknown[ct] = unknown.get(ct, 0) + 1
                    skip(n, f"unsupported Trade Republic type {ct[:60]} — not imported")
            except normalize.TxnError as ex:
                error(n, ex.field, ex.message)
        for item in self._pairs(warrants, tilgs, error):
            out.append(item)
        out.sort(key=lambda x: (x[0], x[1]))
        for _w, n, t in out:
            emit(n, t)
        warnings = []
        if unknown:
            warnings.append("Not imported (unsupported Trade Republic types): "
                            + ", ".join(f"{k} × {v}" for k, v in sorted(unknown.items())))
        return warnings

    # --- helpers -------------------------------------------------------------------------------------------------
    @staticmethod
    def _n(r: dict, col: str) -> Optional[float]:
        raw = r.get(col)
        try:
            return parse_number(raw, ".", "")
        except ValueError as ex:
            raise normalize.TxnError(col, f"{ex} ({(raw or '')[:40]!r})")

    def _date(self, r: dict) -> str:
        return _date(r["date"] or r["datetime"], ("%Y-%m-%d",))

    def _ids(self, r: dict) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """(isin, symbol, asset_class): the symbol of a security is its ISIN; a crypto's is its ticker."""
        ac = self.ASSET.get(r["asset_class"].upper())
        sym = r["symbol"] or None
        if ac == "crypto":
            return None, sym, ac
        if sym and re.match(normalize.ISIN_RE_TEXT, sym.upper()):
            return sym.upper(), None, ac
        return None, sym, ac

    def _extra_tax(self, base: dict, d: str, r: dict, tax: float, suffix: str) -> List[dict]:
        """A tax amount the main transaction cannot carry: a refund (positive) or a charge (negative)."""
        if not tax:
            return []
        kind = "tax_refund" if tax > 0 else "withholding"
        return [normalize.make_txn(date_=d, kind=kind, isin=base.get("isin"), symbol=base.get("symbol"),
                                   name=base.get("name"), asset_class=base.get("asset_class"), currency=base["currency"],
                                   tax=abs(tax), raw_type=f"{r['category']}/{r['type']} tax",
                                   ext_id=f"{r['transaction_id']}|{suffix}" if r["transaction_id"] else None)]

    def _row(self, ct: str, r: dict) -> List[dict]:
        d = self._date(r)
        cur = r["currency"] or None
        if cur is None:
            raise normalize.TxnError("currency", "empty")
        tid = r["transaction_id"] or None
        amount, fee, tax = self._n(r, "amount"), self._n(r, "fee") or 0.0, self._n(r, "tax") or 0.0
        raw = ct
        if ct in ("CASH/TRANSFER_INSTANT_INBOUND", "CASH/CUSTOMER_INPAYMENT", "CASH/TRANSFER_INSTANT_OUTBOUND"):
            kind = self.TRANSFERS[ct.split("/", 1)[1]]
            if amount is None:
                raise normalize.TxnError("amount", "required")
            return [normalize.make_txn(date_=d, kind=kind, currency=cur, gross=abs(amount), raw_type=raw, ext_id=tid)]
        if ct == "CASH/INTEREST_PAYMENT":
            if amount is None:
                raise normalize.TxnError("amount", "required")
            t = normalize.make_txn(date_=d, kind="interest", currency=cur, gross=abs(amount),
                                   tax=abs(tax) if tax < 0 else 0.0, raw_type=raw, ext_id=tid)
            return [t] + self._extra_tax(t, d, r, tax if tax > 0 else 0.0, "tax")
        if ct == "CASH/TAX_OPTIMIZATION":
            if not tax:
                raise normalize.TxnError("tax", "a tax optimisation without a tax amount")
            kind = "tax_refund" if tax > 0 else "withholding"
            return [normalize.make_txn(date_=d, kind=kind, currency=cur, tax=abs(tax), raw_type=raw, ext_id=tid)]
        isin, sym, ac = self._ids(r)
        name = scrub_iban(r["name"] or None)
        if ct in ("TRADING/BUY", "TRADING/SELL"):
            kind = "buy" if ct.endswith("BUY") else "sell"
            qty, price = self._n(r, "shares"), self._n(r, "price")
            if amount is None:
                raise normalize.TxnError("amount", "required")
            if not qty:
                raise normalize.TxnError("shares", "required")
            # the cash amount is the cost basis / the proceeds for EVERY class (a bond's includes accrued interest)
            t = normalize.make_txn(date_=d, kind=kind, isin=isin, symbol=sym, name=name, asset_class=ac,
                                   qty=abs(qty), price=abs(price) if price is not None else None, currency=cur,
                                   gross=abs(amount), fee=abs(fee),
                                   tax=abs(tax) if (kind == "sell" and tax < 0) else 0.0, raw_type=raw, ext_id=tid)
            rest = tax if not (kind == "sell" and tax < 0) else 0.0
            return [t] + self._extra_tax(t, d, r, rest, "tax")
        if ct == "CASH/DIVIDEND":
            if amount is None:
                raise normalize.TxnError("amount", "required")
            t = normalize.make_txn(date_=d, kind="dividend", isin=isin, symbol=sym, name=name, asset_class=ac,
                                   currency=cur, gross=abs(amount), tax=abs(tax) if tax < 0 else 0.0, raw_type=raw,
                                   ext_id=tid, original_amount=self._n(r, "original_amount"),
                                   original_currency=r["original_currency"] or None, fx_rate=self._n(r, "fx_rate"))
            out = [t] + self._extra_tax(t, d, r, tax if tax > 0 else 0.0, "tax")
            if fee:
                out.append(normalize.make_txn(date_=d, kind="fee", isin=isin, symbol=sym, name=name, currency=cur,
                                              fee=abs(fee), raw_type=f"{raw} fee",
                                              ext_id=f"{tid}|fee" if tid else None))
            return out
        if ct == "CASH/PRIVATE_MARKET_BUY":
            if amount is None:
                raise normalize.TxnError("amount", "required")
            return [normalize.make_txn(date_=d, kind="pending_buy", isin=isin, symbol=sym, name=name,
                                       asset_class=ac or "other", currency=cur, gross=abs(amount), fee=abs(fee),
                                       raw_type=raw, ext_id=tid,
                                       note="pending private-market order (pre-payment) — not a position")]
        if ct == "DELIVERY/FREE_RECEIPT":
            qty, price = self._n(r, "shares"), self._n(r, "price")
            if not qty or qty <= 0:
                raise normalize.TxnError("shares", "a free receipt needs a positive quantity")
            if price is None or price <= 0:
                raise normalize.TxnError("price", "a staking reward needs its price at receipt (the cost basis)")
            if ac != "crypto":
                raise normalize.TxnError("asset_class", f"a free receipt of {r['asset_class'][:20] or 'nothing'} — "
                                                        "only crypto staking rewards are supported")
            return [normalize.make_txn(date_=d, kind="staking", isin=isin, symbol=sym, name=name, asset_class=ac,
                                       qty=qty, price=price, gross=qty * price, currency=cur, raw_type=raw,
                                       ext_id=tid, note="staking reward")]
        raise normalize.TxnError("type", f"unsupported {ct}")    # pragma: no cover (type_map guards)

    def _pairs(self, warrants, tilgs, error):
        """WARRANT_EXERCISE (shares −n, no amount) + the TILG repayment of the same ISIN (same or nearest date) =
        ONE disposal with the TILG amount as proceeds. A partner that is missing is reported, never guessed: the
        position stays open and the insights list the reason."""
        free = list(tilgs)
        out = []
        for when, n, r in warrants:
            try:
                d = self._date(r)
                isin, sym, ac = self._ids(r)
                qty = self._n(r, "shares")
                if not qty:
                    raise normalize.TxnError("shares", "a warrant exercise without a quantity")
                best = None
                for j, (tw, tn, tr) in enumerate(free):
                    if (tr["symbol"] or "").upper() != (r["symbol"] or "").upper():
                        continue
                    gap = abs((date.fromisoformat(self._date(tr)) - date.fromisoformat(d)).days)
                    if gap <= self.PAIR_DAYS and (best is None or gap < best[0]):
                        best = (gap, j)
                name = scrub_iban(r["name"] or None)
                if best is None:
                    out.append((when, n, normalize.make_txn(
                        ts=_ts9(when), date_=d, kind="corporate_action", isin=isin, symbol=sym, name=name, asset_class=ac,
                        qty=abs(qty), currency=r["currency"] or "EUR", raw_type="CORPORATE_ACTION/WARRANT_EXERCISE",
                        ext_id=r["transaction_id"] or None,
                        note="warrant exercise without its TILG repayment in this file — import the export that "
                             "contains it")))
                    continue
                tw, tn, tr = free.pop(best[1])
                amount, fee, tax = self._n(tr, "amount"), self._n(tr, "fee") or 0.0, self._n(tr, "tax") or 0.0
                if amount is None:
                    raise normalize.TxnError("amount", f"the TILG repayment (line {tn}) has no amount")
                ext = f"{r['transaction_id']}|{tr['transaction_id']}" if r["transaction_id"] else None
                t = normalize.make_txn(date_=self._date(tr), kind="sell", isin=isin, symbol=sym, name=name,
                                       asset_class=ac, qty=abs(qty), currency=tr["currency"] or r["currency"] or "EUR",
                                       gross=abs(amount), fee=abs(fee), tax=abs(tax) if tax < 0 else 0.0,
                                       raw_type="WARRANT_EXERCISE+TILG", ext_id=ext,
                                       note=f"warrant exercise paired with the TILG repayment (line {tn})")
                w = max(when, tw)
                out.append((w, n, dict(t, ts=_ts9(w))))
                for x in self._extra_tax(t, self._date(tr), tr, tax if tax > 0 else 0.0, "tax"):
                    out.append((w, n, dict(x, ts=_ts9(w))))
            except normalize.TxnError as ex:
                error(n, ex.field, ex.message)
        for tw, tn, tr in free:
            try:
                isin, sym, ac = self._ids(tr)
                out.append((tw, tn, normalize.make_txn(
                    ts=_ts9(tw), date_=self._date(tr), kind="corporate_action", isin=isin, symbol=sym,
                    name=scrub_iban(tr["name"] or None), asset_class=ac, currency=tr["currency"] or "EUR",
                    gross=abs(self._n(tr, "amount") or 0.0), raw_type="CASH/TILG", ext_id=tr["transaction_id"] or None,
                    note="TILG repayment without its WARRANT_EXERCISE in this file — not booked")))
            except normalize.TxnError as ex:
                error(tn, ex.field, ex.message)
        return out


class Scalable(Template):
    id = "scalable"
    label = "Scalable Capital (Transactions CSV — date;time;status;reference;description;assetType;type;isin;…)"
    required = (("date",), ("time",), ("status",), ("description",), ("assetType",), ("type",), ("isin",),
                ("shares",), ("amount",), ("currency",))
    type_map = {"buy": "buy", "savingsplan": "buy", "sparplan": "buy", "sell": "sell", "distribution": "dividend",
                "dividend": "dividend", "interest": "interest", "deposit": "deposit", "withdrawal": "withdrawal",
                "fee": "fee", "taxes": "withholding", "tax": "withholding"}
    DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y")
    OK_STATUS = ("executed", "ausgeführt", "done", "")

    def parse(self, rows, emit, skip, error):
        idx = _index(rows[0])
        for n, row in enumerate(rows[1:], start=2):
            if not any(c.strip() for c in row):
                continue
            try:
                status = norm_header(_cell(row, idx, "status") or "")
                if status not in self.OK_STATUS:
                    skip(n, f"status {status!r}")
                    continue
                raw_type = (_cell(row, idx, "type") or "").strip()
                kind = self.type_map.get(norm_header(raw_type))
                if kind is None:
                    raise normalize.TxnError("type", f"unknown type {raw_type[:40]!r} — map it with a generic profile")
                d = _date(_cell(row, idx, "date"), self.DATE_FORMATS)
                ts = _ts_of(d, _cell(row, idx, "time"))
                qty = _num(row, idx, "quantity", ",", ".", "shares")
                price = _num(row, idx, "price", ",", ".", "price")
                amount = _num(row, idx, "amount", ",", ".", "amount")
                fee = abs(_num(row, idx, "fee", ",", ".", "fee") or 0.0)
                tax = abs(_num(row, idx, "tax", ",", ".", "tax") or 0.0)
                if kind == "withholding" and not tax:
                    tax = abs(amount or 0.0)
                at = norm_header(_cell(row, idx, "assetType") or "")
                ac = {"crypto": "crypto", "etf": "etf", "fund": "fund", "bond": "bond", "stock": "share",
                      "share": "share", "derivative": "derivative"}.get(at)
                t = normalize.make_txn(date_=d, kind=kind, isin=_cell(row, idx, "isin") or None,
                                       name=_cell(row, idx, "description"), asset_class=ac,
                                       qty=abs(qty) if qty else None, price=abs(price) if price else None,
                                       currency=_cell(row, idx, "currency") or "EUR",
                                       gross=_gross(kind, qty, price, amount, fee, tax, "net"), fee=fee, tax=tax,
                                       raw_type=raw_type, ts=ts)
                emit(n, t)
            except normalize.TxnError as ex:
                error(n, ex.field, ex.message)
        return []


class IbkrFlex(Template):
    id = "ibkr_flex"
    label = "Interactive Brokers Flex Query CSV (sections Trades + Cash Transactions)"
    required = (("TradeDate",), ("Buy/Sell",), ("Quantity",), ("TradePrice",), ("CurrencyPrimary", "Currency"))
    CASH_REQUIRED = (("Type",), ("Amount",), ("CurrencyPrimary", "Currency"))
    type_map = {"buy": "buy", "sell": "sell", "dividends": "dividend", "paymentinlieuofdividends": "dividend",
                "withholdingtax": "withholding", "brokerinterestreceived": "interest",
                "bondinterestreceived": "interest", "brokerinterestpaid": "fee", "otherfees": "fee",
                "commissionadjustments": "fee", "deposits/withdrawals": "deposit|withdrawal"}
    ASSET = {"STK": "share", "OPT": "derivative", "FUT": "derivative", "FOP": "derivative", "CFD": "derivative",
             "WAR": "derivative", "BOND": "bond", "BILL": "bond", "FUND": "fund", "CRYPTO": "crypto"}
    DATE_FORMATS = ("%Y%m%d", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y")
    SECTION_CODES = ("HEADER", "DATA", "BOF", "EOF", "BOA", "EOA", "BOS", "EOS")

    def score(self, header):
        h = [c for c in header]
        if h and h[0].strip().upper() == "HEADER" and len(h) > 2:
            h = h[2:]
        s, missing = super().score(h)
        hs = {norm_header(x) for x in h}
        cash_missing = [a[0] for a in self.CASH_REQUIRED if not any(norm_header(x) in hs for x in a)]
        if not cash_missing and ({"datetime", "date/time", "settledate", "reportdate"} & hs):
            return len(self.required), []
        if self._section(h) == "positions":                  # a file starting with "Open Positions" (§20.1)
            return len(self.required), []
        return s, missing

    def _section(self, cells):
        hs = {norm_header(x) for x in cells}
        if {"tradedate", "buy/sell", "quantity"} <= hs:
            return "trades"
        if {"type", "amount"} <= hs and ({"datetime", "date/time", "settledate", "reportdate"} & hs):
            return "cash"
        if {"markprice", "quantity", "symbol"} <= hs and "tradedate" not in hs:
            return "positions"                               # Flex "Open Positions" (stage 9b, SPEC §20.1)
        return None

    def parse(self, rows, emit, skip, error):
        warnings: List[str] = []
        section, idx = None, {}
        for n, raw in enumerate(rows, start=1):
            row = list(raw)
            if not any(c.strip() for c in row):
                continue
            code = row[0].strip().upper() if row else ""
            if code in self.SECTION_CODES:
                if code in ("HEADER", "DATA") and len(row) > 2:
                    row = row[2:]
                else:
                    continue
            sec = self._section(row)
            if sec is not None:
                section, idx = sec, _index(row)
                continue
            if section is None:
                skip(n, "outside a Trades / Cash Transactions section")
                continue
            try:
                if section == "trades":
                    self._trade(n, row, idx, emit, skip)
                elif section == "positions":
                    self._position(n, row, idx, emit, skip)
                else:
                    self._cash(n, row, idx, emit, skip)
            except normalize.TxnError as ex:
                error(n, ex.field, ex.message)
        return warnings

    def _position(self, n, row, idx, emit, skip):
        """One open position with the broker's mark price and its report date → a valuation from the import."""
        lod = norm_header(_cell(row, idx, "LevelOfDetail") or "")
        if lod and lod != "summary":
            skip(n, f"position level of detail {lod!r} (only SUMMARY rows are used)")
            return
        if (_cell(row, idx, "AssetClass") or "").strip().upper() == "CASH":
            skip(n, "cash balance row")
            return
        qty = normalize._fin(_num(row, idx, "quantity", ".", ",", "Quantity"), "quantity", required=True, nonneg=False)
        price = normalize._fin(_num(row, idx, "price", ".", ",", "MarkPrice"), "price", required=True)
        if qty < 0:
            raise normalize.TxnError("quantity", "short positions are not supported (negative Quantity)")
        if qty <= normalize.QTY_EPS or price <= 0:
            raise normalize.TxnError("price", "an open position needs a positive Quantity and MarkPrice")
        d = _date((_cell(row, idx, "ReportDate", "Date") or "").split(";")[0].split(",")[0].strip(), self.DATE_FORMATS)
        isin = (normalize.clean_text(_cell(row, idx, "ISIN"), 32) or "").upper() or None
        if isin is not None and not re.match(normalize.ISIN_RE_TEXT, isin):
            raise normalize.TxnError("isin", "not a valid ISIN")
        sym = normalize.clean_text(_cell(row, idx, "Symbol"), normalize.SYMBOL_MAX)
        cur = (normalize.clean_text(_cell(row, idx, "CurrencyPrimary", "Currency"), 3) or "").upper()
        if cur not in normalize.CURRENCIES:
            raise normalize.TxnError("currency", f"unsupported currency ({cur or 'empty'})")
        key = isin or (sym or "").upper()
        if not key:
            raise normalize.TxnError("isin", "an ISIN or a symbol is required for a position")
        emit(n, {"_position": True, "key": key, "symbol": (sym or "").upper() or None, "qty": float(qty),
                 "price": float(price), "currency": cur, "as_of": d})

    def _trade(self, n, row, idx, emit, skip):
        lod = norm_header(_cell(row, idx, "LevelOfDetail") or "")
        if lod and lod != "execution":
            skip(n, f"level of detail {lod!r} (only EXECUTION rows are imported)")
            return
        ac_raw = (_cell(row, idx, "AssetClass") or "").strip().upper()
        if ac_raw == "CASH":
            skip(n, "FX conversion trade")
            return
        side = norm_header(_cell(row, idx, "Buy/Sell") or "")
        kind = "buy" if side.startswith("buy") else ("sell" if side.startswith("sell") else None)
        if kind is None:
            raise normalize.TxnError("type", f"unknown Buy/Sell value {side[:20]!r}")
        dpart, tpart = _split_dt(_cell(row, idx, "TradeDate"))
        d = _date(dpart, self.DATE_FORMATS)
        ts = _ts_of(d, _cell(row, idx, "TradeTime") or tpart)
        qty = _num(row, idx, "quantity", ".", ",", "Quantity")
        price = _num(row, idx, "price", ".", ",", "TradePrice")
        comm = abs(_num(row, idx, "fee", ".", ",", "IBCommission") or 0.0)
        ac = self.ASSET.get(ac_raw)
        if ac == "share" and norm_header(_cell(row, idx, "SubCategory") or "") == "etf":
            ac = "etf"
        t = normalize.make_txn(date_=d, kind=kind, symbol=_cell(row, idx, "Symbol"), isin=_cell(row, idx, "ISIN")
                               or None, name=_cell(row, idx, "Description"), asset_class=ac,
                               qty=abs(qty) if qty else None, price=abs(price) if price is not None else None,
                               currency=_cell(row, idx, "CurrencyPrimary", "Currency"), fee=comm,
                               raw_type=side, ts=ts)
        emit(n, t)

    def _cash(self, n, row, idx, emit, skip):
        raw_type = (_cell(row, idx, "Type") or "").strip()
        k = self.type_map.get(norm_header(raw_type))
        if k is None:
            raise normalize.TxnError("type", f"unknown cash type {raw_type[:40]!r} — map it with a generic profile")
        amount = _num(row, idx, "amount", ".", ",", "Amount")
        if amount is None:
            raise normalize.TxnError("amount", "required")
        if k == "deposit|withdrawal":
            k = "deposit" if amount >= 0 else "withdrawal"
        if k == "withholding" and amount > 0:
            raise normalize.TxnError("amount", "withholding-tax refund (positive amount) is not supported — remove or "
                                               "map the row")
        raw_d = (_cell(row, idx, "Date/Time", "DateTime", "SettleDate", "ReportDate") or "")
        dpart, tpart = _split_dt(raw_d)
        d = _date(dpart, self.DATE_FORMATS)
        ts = _ts_of(d, tpart)
        kw = {"gross": abs(amount)}
        if k == "withholding":
            kw = {"tax": abs(amount)}
        elif k == "fee":
            kw = {"fee": abs(amount)}
        t = normalize.make_txn(date_=d, kind=k, symbol=_cell(row, idx, "Symbol") or None,
                               isin=_cell(row, idx, "ISIN") or None, name=_cell(row, idx, "Description"),
                               currency=_cell(row, idx, "CurrencyPrimary", "Currency"), raw_type=raw_type, ts=ts,
                               **kw)
        emit(n, t)


class Generic(Template):
    id = "generic"
    label = "Generic (your saved column mapping)"
    verify = False

    def __init__(self, mapping: Optional[dict] = None):
        self.mapping = mapping or {}

    def describe(self):
        return {"id": self.id, "label": self.label, "verify": False,
                "verify_note": "Map the columns of your file to the canonical fields once; save it as a profile.",
                "required_columns": ["date", "type"], "type_map": {}}

    def score(self, header):
        cols = self.mapping.get("columns") or {}
        h = {norm_header(x) for x in header}
        missing = [f for f in ("date", "type") if norm_header(cols.get(f) or "") not in h]
        missing += [f"{f} ({cols[f]})" for f in cols if f not in ("date", "type") and norm_header(cols[f]) not in h]
        return (2 if not missing else 0), missing

    def parse(self, rows, emit, skip, error):
        m = self.mapping
        cols = m.get("columns") or {}
        idx = _index(rows[0])
        dec, th = m.get("decimal", "."), m.get("thousands", "")
        fmt = (m.get("date_format") or "%Y-%m-%d",)
        tmap = {norm_header(k): v for k, v in (m.get("type_map") or {}).items()}
        mode = "net" if m.get("amount_sign", "signed") == "signed" else "gross"

        def c(row, f):
            return _cell(row, idx, cols[f]) if cols.get(f) else None

        def num(row, f):
            if not cols.get(f):
                return None
            return _num(row, idx, f, dec, th, cols[f])

        for n, row in enumerate(rows[1:], start=2):
            if not any(x.strip() for x in row):
                continue
            try:
                raw_type = (c(row, "type") or "").strip()
                kind = tmap.get(norm_header(raw_type))
                if kind == "ignore":
                    skip(n, f"type {raw_type[:40]!r} mapped to ignore")
                    continue
                if kind is None:
                    raise normalize.TxnError("type", f"type {raw_type[:40]!r} is not in the profile's type map")
                d = _date(c(row, "date"), fmt)
                ts = _ts_of(d, c(row, "time"))
                qty, price, amount = num(row, "quantity"), num(row, "price"), num(row, "amount")
                fee, tax = abs(num(row, "fee") or 0.0), abs(num(row, "tax") or 0.0)
                ac = norm_header(c(row, "asset_class") or "")
                kw = {"gross": _gross(kind, qty, price, amount, fee, tax, mode)}
                if kind == "withholding":
                    kw = {"tax": tax or abs(amount or 0.0)}
                elif kind == "fee":
                    kw = {"fee": fee or abs(amount or 0.0)}
                    fee = 0.0
                t = normalize.make_txn(date_=d, kind=kind, symbol=c(row, "symbol") or None,
                                       isin=c(row, "isin") or None, name=c(row, "name"),
                                       asset_class=ac if ac in normalize.ASSET_CLASSES else None,
                                       qty=abs(qty) if qty else None, price=abs(price) if price else None,
                                       currency=c(row, "currency") or m.get("default_currency") or "EUR",
                                       raw_type=raw_type, ts=ts, **({"fee": fee, "tax": tax} if kind not in
                                                                    ("withholding", "fee") else {}), **kw)
                emit(n, t)
            except normalize.TxnError as ex:
                error(n, ex.field, ex.message)
        return []


TEMPLATES = {t.id: t for t in (TradeRepublic(), Scalable(), IbkrFlex())}


def templates_payload() -> dict:
    return {"templates": [t.describe() for t in TEMPLATES.values()] + [Generic().describe()],
            "canonical_fields": list(CANONICAL), "kinds": list(normalize.KINDS) + ["ignore"],
            "asset_classes": list(normalize.ASSET_CLASSES), "limits": dict(LIMITS),
            "notice": "Your statements stay on this computer — nothing is uploaded; an AI commentary sends only "
                      "numbers."}


# --- profiles -------------------------------------------------------------------------------------------------

PROFILE_KEYS = ("delimiter", "decimal", "thousands", "date_format", "encoding", "columns", "type_map",
                "default_currency", "amount_sign")


def check_mapping(m) -> Dict[str, str]:
    if not isinstance(m, dict):
        return {"mapping": "expected an object"}
    e = {f"mapping.{k}"[:80]: "unknown field" for k in m if k not in PROFILE_KEYS}
    if m.get("delimiter", "auto") not in (";", ",", "\t", "auto"):
        e["mapping.delimiter"] = '";" | "," | "\\t" | "auto"'
    if m.get("decimal", ".") not in (",", "."):
        e["mapping.decimal"] = '"," | "."'
    if m.get("thousands", "") not in ("", ".", ",", " ", "'"):
        e["mapping.thousands"] = '"" | "." | "," | " " | "\'"'
    if m.get("decimal", ".") == m.get("thousands", ""):
        e["mapping.thousands"] = "must differ from the decimal separator"
    df = m.get("date_format", "%Y-%m-%d")
    if not isinstance(df, str) or not 2 <= len(df) <= 30 or "%" not in df or not re.fullmatch(r"[%A-Za-z./\- :]+", df):
        e["mapping.date_format"] = 'a strptime pattern such as "%d.%m.%Y"'
    if m.get("encoding", "auto") not in ("auto", "utf-8", "cp1252"):
        e["mapping.encoding"] = '"auto" | "utf-8" | "cp1252"'
    if m.get("default_currency", "EUR") not in ("EUR", "USD"):
        e["mapping.default_currency"] = '"EUR" | "USD"'
    if m.get("amount_sign", "signed") not in ("signed", "positive"):
        e["mapping.amount_sign"] = '"signed" | "positive"'
    cols = m.get("columns")
    if not isinstance(cols, dict) or not cols:
        e["mapping.columns"] = "an object {canonical field: header}"
    else:
        for k, v in cols.items():
            if k not in CANONICAL:
                e[f"mapping.columns.{k}"[:80]] = "unknown canonical field"
            elif not isinstance(v, str) or not 1 <= len(v) <= 100:
                e[f"mapping.columns.{k}"] = "a header name (1..100 characters)"
        for req in ("date", "type"):
            if req not in cols:
                e[f"mapping.columns.{req}"] = "required"
    tm = m.get("type_map")
    if not isinstance(tm, dict) or not tm or len(tm) > 100:
        e["mapping.type_map"] = "an object {raw type: kind} (1..100 entries)"
    else:
        for k, v in tm.items():
            if not isinstance(k, str) or not 1 <= len(k) <= 60 or v not in normalize.KINDS + ("ignore",):
                e[f"mapping.type_map.{str(k)[:40]}"] = "kind: " + " | ".join(normalize.KINDS + ("ignore",))
    return e


# --- the import ---------------------------------------------------------------------------------------------------

def parse_file(data: bytes, template: str = "auto", mapping: Optional[dict] = None) -> dict:
    """bytes -> {detection, txns: [(row, txn)], errors, skipped, counts, warnings}; raises ImportRefused."""
    enc = (mapping or {}).get("encoding", "auto") if template == "generic" else "auto"
    text, encoding = decode(data, enc)
    first = text.splitlines()[0] if text.splitlines() else ""
    delim = (mapping or {}).get("delimiter", "auto") if template == "generic" else "auto"
    delimiter = sniff_delimiter(first) if delim == "auto" else delim
    rows = read_rows(text, delimiter)
    if not rows:
        raise ImportRefused(400, "empty", "The file has no rows.")
    header = [h.strip() for h in rows[0]]
    if template == "generic":
        if mapping is None:
            raise ImportRefused(400, "profile_required", "The generic importer needs a saved mapping profile.")
        tpl = Generic(mapping)
        score, missing = tpl.score(header)
        detection = {"template": "generic", "score": score, "encoding": encoding, "delimiter": delimiter,
                     "header": header[:MAX_COLUMNS], "missing": missing}
        if missing:
            raise ImportRefused(409, "unknown_format", "The file does not have the profile's columns: "
                                + ", ".join(missing[:10]), detection)
    else:
        cands = [TEMPLATES[template]] if template in TEMPLATES else list(TEMPLATES.values())
        best, best_score, best_missing = None, -1, []
        for t in cands:
            s, missing = t.score(header)
            if not missing and s > best_score:
                best, best_score, best_missing = t, s, []
            elif best is None and s > best_score:
                best_score, best_missing = s, missing
        detection = {"template": best.id if best else None, "score": max(best_score, 0), "encoding": encoding,
                     "delimiter": delimiter, "header": header[:MAX_COLUMNS], "missing": best_missing}
        if best is None:
            raise ImportRefused(409, "unknown_format", "Unknown file format" + (f" (not a {template} export)"
                                if template in TEMPLATES else "") + " — map the columns with the generic importer.",
                                detection)
        tpl = best
    txns: List[Tuple[int, dict]] = []
    errors: List[dict] = []
    skipped: List[dict] = []
    nerr = [0]

    positions: List[Tuple[int, dict]] = []

    def emit(n, t):
        (positions if t.get("_position") else txns).append((n, t))

    def skip(n, why):
        skipped.append({"row": n, "reason": why[:120]})

    def error(n, field, msg):
        nerr[0] += 1
        if len(errors) < MAX_ERRORS:
            errors.append({"row": n, "field": field, "message": str(msg)[:200]})

    warnings = tpl.parse(rows, emit, skip, error) or []
    if tpl.verify:
        warnings.insert(0, VERIFY_NOTE)
    data_rows = sum(1 for r in rows[1:] if any(c.strip() for c in r))
    return {"detection": detection, "txns": txns, "errors": errors, "skipped": skipped, "positions": positions,
            "counts": {"rows": data_rows, "parsed": len(txns), "rejected": nerr[0], "skipped": len(skipped),
                       "positions": len(positions)},
            "warnings": warnings}
