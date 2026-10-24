"""Shadow Account storage + orchestration (SPEC §19.6–§19.8): accounts, mapping profiles, imports (one transaction
per import, deduped), classifications, valuations, and the entry points for diagnostics, shadow portfolios, the
audit report and the AI commentary facts. All data stays in hedgefund.db on this computer (``sh_*``, insert-only).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

from ...execution.errors import TradingError
from ...storage import db
from ...core.errors import UserFacingError
from . import diagnostics, importers, normalize, pricing, shadow_portfolio

logger = logging.getLogger(__name__)

ID_RE = re.compile(r"^sha-[0-9a-f]{12}$")
PROFILE_ID_RE = re.compile(r"^prf-[0-9a-f]{12}$")
NOT_FOUND = "No such Shadow account."
NOTICE = ("Your statements stay on this computer — nothing is uploaded; an AI commentary sends only numbers. "
          "Diagnostics and shadow portfolios are hypothetical analyses of your own past trades — for your own "
          "review.")          # S10-H context wording (core/notices.REVIEW)
MAX_ACCOUNTS = 50
MAX_VALUATION_PRICES = 500


def _now() -> datetime:
    from ...execution import accounts
    return accounts.now().astimezone(timezone.utc)


def _ts() -> str:
    return _now().isoformat(timespec="seconds")


# --- accounts --------------------------------------------------------------------------------------------------

def _account_row(account_id: str) -> dict:
    if not ID_RE.fullmatch(account_id or ""):
        raise TradingError(404, "not_found", NOT_FOUND)
    with db._connect() as conn:
        r = conn.execute("SELECT * FROM sh_accounts WHERE id = ?", (account_id,)).fetchone()
    if r is None:
        raise TradingError(404, "not_found", NOT_FOUND)
    return dict(r)


def _account_out(r: dict) -> dict:
    with db._connect() as conn:
        n_imp = conn.execute("SELECT COUNT(*) FROM sh_imports WHERE account_id = ?", (r["id"],)).fetchone()[0]
        agg = conn.execute("SELECT COUNT(*), MIN(date), MAX(date) FROM sh_transactions WHERE account_id = ?",
                           (r["id"],)).fetchone()
        tpls = [x[0] for x in conn.execute("SELECT DISTINCT template FROM sh_imports WHERE account_id = ? ORDER BY"
                                           " template", (r["id"],)).fetchall()]
    return {"id": r["id"], "label": r["label"], "base_currency": r["base_currency"],
            "broker_country": r["broker_country"], "created_at": r["created_at"], "imports": int(n_imp),
            "transactions": int(agg[0]), "first_date": agg[1], "last_date": agg[2], "templates": tpls}


def create_account(label: str, base_currency: str, broker_country: str, actor: str = "user") -> dict:
    with db._connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM sh_accounts").fetchone()[0]
    if n >= MAX_ACCOUNTS:
        raise TradingError(409, "limit", f"At most {MAX_ACCOUNTS} Shadow accounts.")
    aid = f"sha-{uuid.uuid4().hex[:12]}"
    label = normalize.clean_text(label, 60) or "Shadow account"
    db.add_audit(actor, "shadow_account_created", {"account_id": aid, "base_currency": base_currency,
                                                   "broker_country": broker_country})
    with db._connect() as conn:
        conn.execute("INSERT INTO sh_accounts (id, label, base_currency, broker_country, created_at, created_by) VALUES"
                     " (?, ?, ?, ?, ?, ?)", (aid, label, base_currency, broker_country, _ts(), actor))
    return _account_out(_account_row(aid))


def accounts_list() -> dict:
    with db._connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM sh_accounts ORDER BY created_at, id").fetchall()]
    return {"accounts": [_account_out(r) for r in rows], "notice": NOTICE}


def account_ids() -> List[dict]:
    with db._connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM sh_accounts ORDER BY created_at, id").fetchall()]


# --- profiles --------------------------------------------------------------------------------------------------

def _profile_out(r) -> dict:
    return {"id": r["id"], "name": r["name"], "version": r["version"], "created_at": r["created_at"],
            "mapping": json.loads(r["mapping_json"])}


def save_profile(name: str, mapping: dict, actor: str = "user") -> dict:
    name = normalize.clean_text(name, 60)
    if not name:
        raise ValueError("name: 1..60 characters")
    pid = f"prf-{uuid.uuid4().hex[:12]}"
    with db._connect() as conn:
        v = conn.execute("SELECT COALESCE(MAX(version), 0) FROM sh_profiles WHERE name = ?", (name,)).fetchone()[0]
    db.add_audit(actor, "shadow_profile_saved", {"profile_id": pid, "name": name, "version": int(v) + 1})
    with db._connect() as conn:
        conn.execute("INSERT INTO sh_profiles (id, name, version, mapping_json, created_at, created_by) VALUES"
                     " (?, ?, ?, ?, ?, ?)", (pid, name, int(v) + 1, json.dumps(mapping, sort_keys=True), _ts(), actor))
        r = conn.execute("SELECT * FROM sh_profiles WHERE id = ?", (pid,)).fetchone()
    return _profile_out(r)


def profiles_list() -> dict:
    with db._connect() as conn:
        rows = conn.execute("SELECT * FROM sh_profiles ORDER BY name, version DESC").fetchall()
    return {"profiles": [_profile_out(r) for r in rows]}


def profile_get(pid: str) -> dict:
    if not PROFILE_ID_RE.fullmatch(pid or ""):
        raise TradingError(404, "not_found", "No such mapping profile.")
    with db._connect() as conn:
        r = conn.execute("SELECT * FROM sh_profiles WHERE id = ?", (pid,)).fetchone()
    if r is None:
        raise TradingError(404, "not_found", "No such mapping profile.")
    return _profile_out(r)


# --- imports ---------------------------------------------------------------------------------------------------

TXN_COLS = ("date", "kind", "key", "symbol", "isin", "name", "asset_class", "qty", "price", "currency", "gross", "fee",
            "tax", "net", "raw_type", "ext_id", "original_amount", "original_currency", "fx_rate", "note", "ts")
POSSIBLE_DUP = "possible duplicate (content matches an earlier import)"


def _core(t: dict) -> tuple:
    """The content of a transaction for the duplicate checks (as ``normalize.txn_hash``)."""
    r = normalize._r
    return (t["date"], t["kind"], t.get("key"), r(t.get("qty")), r(t.get("price")), r(t.get("net")), t.get("currency"))


def _import_out(r) -> dict:
    return {k: r[k] for k in ("id", "account_id", "created_at", "filename", "sha256", "template", "profile_id", "rows",
                              "accepted", "rejected", "duplicates", "skipped")}


def _txn_out(r: dict) -> dict:
    out = {k: r.get(k) for k in ("id",) + TXN_COLS}
    out["import_id"] = r.get("import_id")
    out["row"] = r.get("row_no")
    return out


def import_file(account_id: str, data: bytes, filename: Optional[str], template: str, profile_id: Optional[str],
                dry_run: bool, actor: str = "user") -> dict:
    acc = _account_row(account_id)
    mapping = profile_get(profile_id)["mapping"] if profile_id else None
    if template == "generic" and mapping is None:
        raise importers.ImportRefused(400, "profile_required", "The generic importer needs profile_id (a saved "
                                                               "mapping profile).")
    parsed = importers.parse_file(data, template, mapping)
    with db._connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT hash, ext_id, date, kind, key, qty, price, net, currency FROM sh_transactions WHERE account_id = ?",
            (account_id,)).fetchall()]
    existing = {r["hash"] for r in rows}
    known_ext = {r["ext_id"] for r in rows if r["ext_id"]}
    file_ext = {t["ext_id"] for _n, t in parsed["txns"] if t.get("ext_id")}
    # review fix 2026-10-18: content of earlier rows under ANOTHER identity — rows without an id (an older id-less
    # import) or with an id this file does not carry (a changed id) — for a row with an unknown id; rows WITH an id
    # for a row without one. A match is skipped with a reason, never merged silently.
    other_for_ext: Dict[tuple, int] = {}
    ext_rows: Dict[tuple, int] = {}
    for r in rows:
        c = _core(r)
        if not r["ext_id"] or r["ext_id"] not in file_ext:
            other_for_ext[c] = other_for_ext.get(c, 0) + 1
        if r["ext_id"]:
            ext_rows[c] = ext_rows.get(c, 0) + 1
    occ: Dict[tuple, int] = {}
    occ_new: Dict[tuple, int] = {}
    fresh, dups, known, possible = [], 0, 0, []
    for row_no, t in parsed["txns"]:
        c = _core(t)
        if t.get("ext_id"):
            # §19.16: the broker's transaction id is the dedupe key — an overlapping export imports only new rows
            if t["ext_id"] in known_ext:
                known += 1
                continue
            occ_new[c] = occ_new.get(c, 0) + 1
            if occ_new[c] <= other_for_ext.get(c, 0):
                possible.append({"row": row_no, "reason": POSSIBLE_DUP})
                continue
            known_ext.add(t["ext_id"])
            fresh.append((row_no, normalize.ext_hash(t["ext_id"]), t))
            continue
        occ[c] = occ.get(c, 0) + 1
        h = normalize.txn_hash(t, occ[c])
        if h in existing:
            dups += 1
            continue
        if occ[c] <= ext_rows.get(c, 0):
            possible.append({"row": row_no, "reason": POSSIBLE_DUP})
            continue
        fresh.append((row_no, h, t))
    if possible:
        parsed = dict(parsed, skipped=list(parsed["skipped"]) + possible)
    counts = {"rows": parsed["counts"]["rows"], "accepted": len(fresh), "rejected": parsed["counts"]["rejected"],
              "duplicates": dups, "already_known": known, "possible_duplicates": len(possible),
              "skipped": parsed["counts"]["skipped"] + len(possible),
              "positions": len(parsed.get("positions") or [])}
    warnings = list(parsed["warnings"])
    if possible:
        warnings.append(f"{len(possible)} row(s) skipped: {POSSIBLE_DUP} — check them in the import report; remove "
                        "the earlier import first if this file should replace it.")
    if acc["base_currency"] not in {t["currency"] for _n, _h, t in fresh} and fresh:
        warnings.append(f"No transaction is in the account's base currency {acc['base_currency']} — values are "
                        "converted with FRED DEXUSEU (load the FX rates in Shadow Account → FX rates).")
    preview = [dict(_txn_out(dict(t, row_no=n)), id=None, import_id=None) for n, _h, t in fresh[:importers.PREVIEW_ROWS]]
    out = {"import": None, "detection": parsed["detection"], "preview": preview, "counts": counts,
           "errors": parsed["errors"], "warnings": warnings}
    sha = hashlib.sha256(data).hexdigest()
    with db._connect() as conn:
        prior = conn.execute("SELECT id FROM sh_imports WHERE account_id = ? AND sha256 = ?", (account_id, sha)).fetchone()
    if dry_run:
        if prior is not None:
            out["warnings"].append(f"This exact file was imported already ({prior['id']}) — importing it again is "
                                   "refused (remove that import first).")
        return out
    if prior is not None:
        raise TradingError(409, "duplicate_import", f"This exact file was imported already ({prior['id']}).",
                           import_id=prior["id"])
    if not fresh:
        out["warnings"].append("Nothing new to import (all rows were duplicates, already known, skipped or "
                               "rejected).")
    iid = f"imp-{uuid.uuid4().hex[:12]}"
    fname = normalize.clean_text(filename, 120) or "upload.csv"
    ts = _ts()
    try:
        _write_import(account_id, iid, ts, fname, sha, parsed, profile_id, counts, warnings, fresh, actor)
    except db.StorageUnavailable as ex:
        if "UNIQUE" in str(ex.detail) and "sha256" in str(ex.detail):
            raise TradingError(409, "duplicate_import", "This exact file is being / was imported already.")
        raise
    with db._connect() as conn:
        r = conn.execute("SELECT * FROM sh_imports WHERE id = ?", (iid,)).fetchone()
    out["import"] = _import_out(r)
    return out


def _write_import(account_id, iid, ts, fname, sha, parsed, profile_id, counts, warnings, fresh, actor) -> None:
    """The import row (UNIQUE account + sha256 — a concurrent identical import fails here), its transactions and
    the audit row in ONE transaction (review fix 2026-10-18)."""
    with db._connect() as conn:
        conn.execute("INSERT INTO sh_imports (id, account_id, created_at, filename, sha256, template, profile_id, rows,"
                     " accepted, rejected, duplicates, skipped, report_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (iid, account_id, ts, fname, sha, parsed["detection"]["template"], profile_id, counts["rows"],
                      counts["accepted"], counts["rejected"], counts["duplicates"] + counts.get("already_known", 0),
                      counts["skipped"],
                      json.dumps({"detection": parsed["detection"], "errors": parsed["errors"],
                                  "skipped": parsed["skipped"][:200], "warnings": warnings}, default=str)))
        conn.executemany(f"INSERT INTO sh_transactions (account_id, import_id, row_no, seq, hash, {', '.join(TXN_COLS)})"
                         f" VALUES (?, ?, ?, ?, ?, {', '.join('?' * len(TXN_COLS))})",
                         [(account_id, iid, n, seq, h) + tuple(t.get(c) for c in TXN_COLS)
                          for seq, (n, h, t) in enumerate(fresh)])
        by_day: Dict[str, list] = {}
        for _n, p in parsed.get("positions") or []:        # stage 9b (§20.1): a valuation contained in the import
            by_day.setdefault(p["as_of"], []).append({"key": p["key"], "price": p["price"], "qty": p["qty"],
                                                      "currency": p["currency"], "import_id": iid})
        for day, prices in sorted(by_day.items()):
            conn.execute("INSERT INTO sh_valuations (account_id, as_of, prices_json, ts, actor) VALUES (?, ?, ?, ?, ?)",
                         (account_id, day, json.dumps(prices), ts, "import"))
        db._insert_audit(conn, actor, "shadow_import", {"account_id": account_id, "import_id": iid, "template":
                                                        parsed["detection"]["template"], "sha256": sha, **counts})


REMOVE_CONFIRM = "REMOVE IMPORT"


def remove_import(account_id: str, import_id: str, confirm: str, reason: str, actor: str = "user") -> dict:
    """Deletes ONE import batch (its sh_imports row and its transactions — database rows only, never a file) with a
    removal record and the audit row in one transaction."""
    _account_row(account_id)
    # fix1023: no typed "REMOVE IMPORT" any more (``confirm`` accepted and ignored)
    with db._connect() as conn:
        r = conn.execute("SELECT * FROM sh_imports WHERE id = ? AND account_id = ?", (import_id, account_id)).fetchone()
        if r is None:
            raise TradingError(404, "not_found", "No such import in this Shadow account.")
        n = conn.execute("SELECT COUNT(*) FROM sh_transactions WHERE import_id = ?", (import_id,)).fetchone()[0]
        ts = _ts()
        conn.execute("INSERT INTO sh_import_removals (account_id, import_id, sha256, filename, transactions, ts, actor,"
                     " reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (account_id, import_id, r["sha256"], r["filename"],
                                                                 int(n), ts, actor, reason))
        conn.execute("DELETE FROM sh_transactions WHERE import_id = ? AND account_id = ?", (import_id, account_id))
        conn.execute("DELETE FROM sh_imports WHERE id = ?", (import_id,))
        db._insert_audit(conn, actor, "shadow_import_removed", {"account_id": account_id, "import_id": import_id,
                                                                "sha256": r["sha256"], "transactions": int(n),
                                                                "reason": reason})
    return {"removed": {"import_id": import_id, "transactions": int(n), "ts": ts},
            "account": _account_out(_account_row(account_id))}


def imports_of(account_id: str) -> List[dict]:
    with db._connect() as conn:
        return [_import_out(r) for r in conn.execute("SELECT * FROM sh_imports WHERE account_id = ? ORDER BY created_at",
                                                     (account_id,)).fetchall()]


def classifications() -> Dict[str, dict]:
    with db._connect() as conn:
        rows = conn.execute("SELECT * FROM sh_classifications ORDER BY id").fetchall()
    out: Dict[str, dict] = {}
    for r in rows:
        out[r["key"]] = {"key": r["key"], "asset_class": r["asset_class"], "ts": r["ts"], "reason": r["reason"]}
    return out


def put_classification(key: str, asset_class: str, reason: Optional[str], actor: str = "user") -> dict:
    db.add_audit(actor, "shadow_classification", {"key": key, "asset_class": asset_class})
    ts = _ts()
    with db._connect() as conn:
        conn.execute("INSERT INTO sh_classifications (key, asset_class, ts, actor, reason) VALUES (?, ?, ?, ?, ?)",
                     (key, asset_class, ts, actor, reason))
    return {"key": key, "asset_class": asset_class, "ts": ts, "reason": reason}


def txns(account_id: str) -> List[dict]:
    """All transactions of the account (date order), with the user's classification applied."""
    _account_row(account_id)
    with db._connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM sh_transactions WHERE account_id = ? ORDER BY date, id",
                                              (account_id,)).fetchall()]
    cls = classifications()
    for r in rows:
        r["row"] = r.get("row_no")
        for k in (r.get("isin"), r.get("symbol"), r.get("key")):
            if k and k in cls:
                r["asset_class"] = cls[k]["asset_class"]
                break
    return sorted(rows, key=normalize.sort_key)


def transactions_page(account_id: str, limit: int, offset: int) -> dict:
    rows = txns(account_id)
    return {"transactions": [_txn_out(r) for r in rows[offset:offset + limit]], "total": len(rows)}


def put_valuation(account_id: str, as_of: str, prices: List[dict], actor: str = "user") -> dict:
    _account_row(account_id)
    db.add_audit(actor, "shadow_valuation", {"account_id": account_id, "as_of": as_of, "prices": len(prices)})
    ts = _ts()
    with db._connect() as conn:
        conn.execute("INSERT INTO sh_valuations (account_id, as_of, prices_json, ts, actor) VALUES (?, ?, ?, ?, ?)",
                     (account_id, as_of, json.dumps(prices), ts, actor))
    return {"as_of": as_of, "prices": prices, "ts": ts}


def valuation_rows(account_id: str) -> List[dict]:
    """All valuation rows of the account, oldest first; rows of a REMOVED import are left out (§20.1)."""
    with db._connect() as conn:
        rows = conn.execute("SELECT * FROM sh_valuations WHERE account_id = ? ORDER BY id", (account_id,)).fetchall()
        live = {r[0] for r in conn.execute("SELECT id FROM sh_imports WHERE account_id = ?", (account_id,)).fetchall()}
    out = []
    for r in rows:
        prices = json.loads(r["prices_json"])
        if r["actor"] == "import":
            prices = [p for p in prices if p.get("import_id") in live]
            if not prices:
                continue
        out.append({"as_of": r["as_of"], "prices": prices, "ts": r["ts"],
                    "source": "import" if r["actor"] == "import" else "user"})
    return out


def valuation(account_id: str) -> Optional[dict]:
    rows = valuation_rows(account_id)
    if not rows:
        return None
    r = rows[-1]
    return {"as_of": r["as_of"], "prices": [{k: p[k] for k in ("key", "price", "currency") if k in p}
                                            for p in r["prices"]], "ts": r["ts"],
            "source": r["source"]}


def detail(account_id: str) -> dict:
    acc = _account_row(account_id)
    rows = txns(account_id)
    book = normalize.replay(rows)
    meta: Dict[str, dict] = {}
    for t in rows:
        if t["key"]:
            m = meta.setdefault(t["key"], {})
            for k in ("symbol", "isin", "name", "asset_class", "currency"):
                if t.get(k):
                    m[k] = t[k]
    pos = []
    for key, h in sorted(book.holdings().items()):
        m = meta.get(key, {})
        cost = h["amounts"]["ccy"]
        pos.append({"key": key, "symbol": m.get("symbol"), "isin": m.get("isin"), "name": m.get("name"),
                    "asset_class": m.get("asset_class"), "currency": m.get("currency"), "qty": h["qty"],
                    "cost": cost, "avg_cost": cost / h["qty"] if h["qty"] else None, "lots": h["lots"]})
    return {"account": _account_out(acc), "imports": imports_of(account_id), "positions": pos,
            "unmatched": [{"date": u["date"], "key": u["key"], "qty": u["qty"]} for u in book.unmatched],
            "valuation": valuation(account_id)}


# --- analysis --------------------------------------------------------------------------------------------------

class Context:
    """Everything the analyses of one account need: base-currency transactions, rates, prices, valuation.

    Market prices (review fix H2): ONLY for identified US securities (ISIN starting with "US", not crypto / derivative
    / bond) — with the ticker of the user's symbol mapping (§20.10; any trade currency: the USD close is converted to
    the base currency like every price) or, unmapped, a symbol in the import and trade currency USD; raw closes (as traded) for every
    comparison with a trade price; an instrument with a stock split after its first purchase is excluded (imported
    lots are not split-adjusted). Every other instrument: "not available: no price source for <ISIN/name>" unless the
    user valued it — counted as excluded, never a ticker guess."""

    def __init__(self, account_id: str, benchmark: str = "SPY"):
        from .. import fx, prices
        self.acc = _account_row(account_id)
        self.base = self.acc["base_currency"]
        self.raw = txns(account_id)
        today = _now().date()
        first = date.fromisoformat(self.raw[0]["date"]) if self.raw else today
        self.val = valuation(account_id)
        self.end = self.val["as_of"] if self.val else today.isoformat()
        self.rates = fx.Rates.load(first - timedelta(days=10), max(today, date.fromisoformat(self.end)))
        self.txns_b, self.fx_missing, self.unsupported = diagnostics.to_base(self.raw, self.base, self.rates)
        self.closes = prices.Closes(first - timedelta(days=10), today)
        self.benchmark = benchmark
        self.info: Dict[str, dict] = {}
        for t in self.raw:
            if not t["key"]:
                continue
            m = self.info.setdefault(t["key"], {"key": t["key"], "symbol": None, "isin": None, "name": None,
                                                "currency": t["currency"], "asset_class": None, "first_buy": None})
            for k in ("symbol", "isin", "name", "asset_class"):
                if t.get(k):
                    m[k] = t[k]
            if t["kind"] in normalize.ACQUISITIONS and m["first_buy"] is None:
                m["first_buy"] = t["date"]
        self.excluded: Dict[str, str] = {}
        from . import symbols
        self.maps = symbols.mappings()
        self.via: Dict[str, dict] = {}
        self._plans: Dict[str, object] = {}
        self._bars: Dict[str, Optional[Dict[str, float]]] = {}
        self._bar_errors: Dict[str, str] = {}

    @property
    def ccy_of(self) -> Dict[str, str]:
        return {k: v["currency"] for k, v in self.info.items()}

    def plan(self, key: str):
        """The ONE source that may price ``key`` (``pricing.plan_for``; §20.11) in the TRADE currency, or None
        (reason in ``excluded``). A listing in another currency than the trades is not used: "different
        currencies — not converted"."""
        if key in self._plans:
            return self._plans[key]
        m = self.info.get(key) or {}
        label = m.get("isin") or m.get("name") or key
        p, why = pricing.plan_for(m, self.maps)
        if p is not None and p.currency != m.get("currency"):
            p, why = None, f"{pricing.DIFFERENT} (listing {p.currency}, trades {m.get('currency')})"
        if p is None:
            self.excluded.setdefault(key, f"not available: no price source for {label} — {why}")
        else:
            self.via[key] = {"symbol": p.symbol, "source": p.source, "provider": p.provider,
                             "price_source": p.detail}
        self._plans[key] = p
        return p

    def market_symbol(self, key: str) -> Optional[str]:
        """The Alpaca US symbol that may price ``key`` (or None)."""
        p = self.plan(key)
        return p.symbol if p is not None and p.provider == "alpaca_us" else None

    def _listing_bars(self, key: str, p) -> Optional[Dict[str, float]]:
        """EODHD / Kraken closes of a plan (loaded once per Context; a failure excludes the key with its reason)."""
        if p.symbol not in self._bars:
            try:
                self._bars[p.symbol] = dict(tuple(b) for b in pricing.bars(p))
            except (UserFacingError, ValueError) as ex:
                self._bars[p.symbol] = None
                self._bar_errors[p.symbol] = pricing.reason_of(ex)
        if self._bars[p.symbol] is None:
            self.excluded.setdefault(key, f"not available: {self._bar_errors[p.symbol]}")
        return self._bars[p.symbol]

    def usd_to_base(self, usd: float, day: str) -> Optional[float]:
        miss: List[str] = []
        return self.rates.convert(usd, "USD", self.base, day, miss)     # base is EUR or USD (no FxUnsupported)

    def base_to_usd(self, amount: float, day: str) -> Optional[float]:
        miss: List[str] = []
        return self.rates.convert(amount, self.base, "USD", day, miss)

    def _valuation_price(self, key: str, day: str) -> Optional[float]:
        from .. import fx
        if not (self.val and self.val["as_of"] == day):
            return None
        for p in self.val["prices"]:
            if p["key"] == key:
                miss: List[str] = []
                try:
                    return self.rates.convert(float(p["price"]), p.get("currency") or self.ccy_of.get(key, self.base),
                                              self.base, day, miss)
                except fx.FxUnsupported:
                    return None
        return None

    def price_b(self, key: str, day: str) -> Optional[float]:
        """Price of one unit in the base currency on ``day``: your valuation (on its as-of day) wins, else the RAW
        close of that exact day from the key's ONE source, in the trade currency (then into the base currency like the
        trades themselves, stage-9 normalisation); None = unpriced."""
        v = self._valuation_price(key, day)
        if v is not None:
            return v
        p = self.plan(key)
        if p is None:
            return None
        if p.provider != "alpaca_us":
            bars = self._listing_bars(key, p)
            px = bars.get(day) if bars else None
            if px is None:
                return None
            miss: List[str] = []
            return self.rates.convert(px, p.currency, self.base, day, miss)
        sym = p.symbol
        fb = (self.info.get(key) or {}).get("first_buy") or day
        sp = self.closes.splits_between(sym, fb, day)
        if sp:
            self.excluded.setdefault(key, f"not available: stock split of {sym} on {sp[0][0]} — imported lots are "
                                          "not split-adjusted")
            return None
        if self.closes.splits(sym) is None:
            self.excluded.setdefault(key, f"not available: no corporate actions for {sym} (needed for raw prices)")
            return None
        px = self.closes.raw_on(sym, day)
        if px is None:
            return None
        return self.usd_to_base(px, day)

    def coverage(self) -> dict:
        keys = sorted({t["key"] for t in self.raw if t["kind"] in ("buy", "sell", "staking") and t["key"]})
        vk = {p["key"] for p in (self.val or {}).get("prices", [])}
        priced = []
        for k in keys:
            if k in vk:
                priced.append(k)
                continue
            p = self.plan(k)
            if p is None:
                continue
            if p.provider != "alpaca_us":
                if self._listing_bars(k, p) and k not in self.excluded:
                    priced.append(k)
                continue
            sym = p.symbol
            if self.closes.raw(sym) is not None and k not in self.excluded:
                priced.append(k)
            elif k not in self.excluded:
                self.excluded[k] = f"not available: no market data for {sym} ({self.closes.errors.get(sym, '?')})"
        from .. import prices
        excl = [{"key": k, "name": (self.info.get(k) or {}).get("name"), "reason": self.excluded[k]}
                for k in keys if k not in priced and k in self.excluded]
        sources = [dict(self.via[k], key=k) if k in self.via and k not in vk else
                   {"key": k, "symbol": None, "source": "valuation", "provider": None, "price_source": "your valuation"}
                   for k in priced]
        return {"instruments": len(keys), "priced": len(priced), "excluded": excl[:200],
                "unpriced": [k for k in keys if k not in priced][:200], "price_source": prices.PRICE_SOURCE,
                "price_sources": sources[:200]}

    def warnings(self) -> List[str]:
        w = []
        if self.fx_missing:
            w.append(f"{len(self.fx_missing)} transaction date(s) without an EUR/USD fixing (FRED DEXUSEU, exact date) "
                     "were left out — load the FX rates (Shadow Account → FX rates).")
        if self.unsupported:
            w.append("Transactions in " + ", ".join(self.unsupported) + " were left out (no FX source for them).")
        return w


def diagnose(account_id: str, benchmark: str = "SPY") -> dict:
    ctx = Context(account_id, benchmark)
    if not ctx.txns_b:
        return {"account_id": account_id, "base_currency": ctx.base, "period": None, "coverage": ctx.coverage(),
                "unavailable": "no transactions (or none convertible to the base currency)", "facts": [],
                "warnings": ctx.warnings()}
    bench = ctx.closes.series(benchmark)
    d = diagnostics.compute(ctx.txns_b, price_b=ctx.price_b, bench=bench, bench_symbol=benchmark)
    d.pop("_book", None)
    cov = ctx.coverage()
    if isinstance(d.get("disposition"), dict) and ("unavailable" not in d["disposition"]
                                                   or d["disposition"].get("not_meaningful")):
        d["disposition"]["excluded_instruments"] = len(cov["excluded"])
    out = {"account_id": account_id, "base_currency": ctx.base, **d, "coverage": cov, "warnings": ctx.warnings()}
    out["facts"] = diagnostics.facts(out)
    return out


def _strategy_series(strategy_id: str):
    from ...backtest import runcard
    row = db.strategy_get(strategy_id)
    if row is None:
        raise shadow_portfolio.Unavailable("no such strategy")
    latest = runcard.latest_done(strategy_id, int(row["current_version"]), kind="backtest")
    if latest is None:
        raise shadow_portfolio.Unavailable("the strategy's current version has no finished backtest")
    card = runcard.get(latest["id"]) or {}
    curve = card.get("curve") or {}
    pts = [(str(d)[:10], float(e)) for d, e in zip(curve.get("dates") or [], curve.get("equity") or [])
           if e is not None and float(e) > 0]
    if not pts:
        raise shadow_portfolio.Unavailable("the backtest card has no equity curve")
    return pts


NO_STATEMENTS = "no statements imported"
NO_TRADES = "no trades imported"


def user_end_value(ctx: Context) -> tuple:
    # an empty account has NO value (never a stand-in 0.00 — 9c addendum)
    if not ctx.raw:
        return None, NO_STATEMENTS
    if not ctx.txns_b:
        return None, "no transaction convertible to the base currency (load the FX rates in Shadow Account → FX rates)"
    if not any(t["kind"] in ("buy", "sell", "staking") for t in ctx.txns_b):
        return None, NO_TRADES          # e.g. deposits only: no portfolio to value (review fix 2026-10-18)
    book = normalize.Book()
    for t in ctx.txns_b:
        if t["date"] <= ctx.end:
            diagnostics._book_apply(book, t)
    total, missing = 0.0, []
    for key, h in book.holdings().items():
        if h["qty"] <= 0:
            continue
        px = ctx.price_b(key, ctx.end)
        if px is None:
            missing.append(key)
            continue
        total += h["qty"] * px
    if missing:
        why = "; ".join(f"{k}: {ctx.excluded.get(k, 'no close on that exact day')}" for k in missing[:10])
        return None, (f"{len(missing)} position(s) have no price at {ctx.end} ({why}) — add a valuation (Shadow "
                      "Account → Valuation) to compare your own result")
    return total, None


def shadows(account_id: str, rules: List[dict]) -> dict:
    ctx = Context(account_id)
    uv, msg = user_end_value(ctx)
    res = shadow_portfolio.compare(ctx.txns_b, rules, ctx.end, closes=ctx.closes, strategy_series=_strategy_series,
                                   to_usd=ctx.base_to_usd, to_base=ctx.usd_to_base, user_end_value=uv, user_message=msg)
    if ctx.base != "USD":
        # the ONE conversion that stays (§20.11): the simulated US portfolio receives the account's flows
        for r in res.get("shadows") or []:
            if "unavailable" not in r:
                r["conversion"] = SHADOW_CONVERSION
    return {"account_id": account_id, "base_currency": ctx.base, **res, "warnings": ctx.warnings()}


SHADOW_CONVERSION = "simulated in USD — converted at the FRED rate of each flow date"


def report_facts(account_id: str, benchmark: str = "SPY") -> List[dict]:
    """Numbers only (no names, ISINs, symbols, file names) — the AI commentary's input."""
    d = diagnose(account_id, benchmark)
    rows = list(d.get("facts") or [])
    try:
        sh = shadows(account_id, [{"type": "buy_and_hold", "symbol": benchmark}])
        u = sh["user"]
        if u.get("mwr_annual") is not None:
            rows.append({"id": "user.mwr", "label": "Your money-weighted return per year", "value": u["mwr_annual"],
                         "unit": "fraction", "display": f"{u['mwr_annual'] * 100:.1f} %"})
        for i, s in enumerate(sh["shadows"], start=1):
            if s.get("mwr_annual") is not None:
                rows.append({"id": f"shadow.{i}.mwr", "label": f"Shadow portfolio {i} (buy and hold benchmark) "
                             "money-weighted return per year", "value": s["mwr_annual"], "unit": "fraction",
                             "display": f"{s['mwr_annual'] * 100:.1f} %"})
    except Exception:            # noqa: BLE001 — the shadow comparison is optional for the commentary
        logger.info("shadow facts unavailable", exc_info=True)
    return rows


def _f(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))
