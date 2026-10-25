"""Disclosed trades — the measured unit of the Disclosures section (INSIDER_RET_API.md §1, accepted 2026-10-24).

One trade = one Form 4 accession x one open-market code: P (purchase) or S (sale), non-derivative rows only. The
rows of that code are aggregated (shares summed, value = sum of shares x price, average price = value / shares,
first and last transaction date). Every other code, derivative rows and derivative-only filings are not stored.
Amendments (4/A) are stored but not measured, so the same trades are never counted twice.

Also here: the role class (CEO / CFO / Director / 10% owner / Other) and the point-in-time cluster flag: a trade
is a cluster trade when, at its own filing, at least 2 different insiders (purchases) or 3 (unplanned sales) of the
same issuer had traded in the same direction within the 14 days up to its trade date — the existing
``insider_summary`` thresholds, evaluated without look-ahead (later filings never relabel an earlier trade).

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Set

from . import insider_summary

ROLE_CLASSES = ("CEO", "CFO", "Director", "10% owner", "Other")
_CEO_RE = re.compile(r"chief\s+executive|\bceo\b|principal\s+executive", re.IGNORECASE)
_CFO_RE = re.compile(r"chief\s+financial|\bcfo\b|principal\s+financial", re.IGNORECASE)
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_NO_TICKER = {"NONE", "N/A", "NA", "NULL"}
OPEN_MARKET_CODES = ("P", "S")
AMENDMENT_REASON = "amendment (4/A) — not measured, the original filing is"


def role_class(roles: Iterable[str]) -> str:
    """One class from the parsed roles ("Director", "10% owner", an officer title, other text), precedence
    CEO > CFO > Director > 10% owner > Other."""
    roles = [str(r or "").strip() for r in roles or [] if str(r or "").strip()]
    titles = [r for r in roles if r not in ("Director", "10% owner")]
    if any(_CEO_RE.search(t) for t in titles):
        return "CEO"
    if any(_CFO_RE.search(t) for t in titles):
        return "CFO"
    if "Director" in roles:
        return "Director"
    if roles == ["10% owner"]:
        return "10% owner"
    return "Other"


def clean_ticker(value) -> Optional[str]:
    t = str(value or "").strip().upper()
    if not t or t in _NO_TICKER or not _TICKER_RE.match(t):
        return None
    return t


def edgar_index_url(issuer_cik, accession: str) -> Optional[str]:
    """The filing's index page on sec.gov (the official document link)."""
    acc = str(accession or "").strip()
    try:
        cik = int(str(issuer_cik).strip())
    except (TypeError, ValueError):
        return None
    if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", acc):
        return None
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{acc}-index.htm"


def _iso_utc(value) -> Optional[str]:
    if value in (None, ""):
        return None
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    return dt.isoformat(timespec="seconds")


def _day(value) -> Optional[str]:
    try:
        return date.fromisoformat(str(value or "")[:10]).isoformat()
    except ValueError:
        return None


def insider_trades(accession: str, details: dict, *, form: str, accepted_at, filing_date, origin: str,
                   url: Optional[str] = None, now: Optional[str] = None) -> List[dict]:
    """Trades (0-2) of one parsed Form 4 (``sec_edgar_collector._parse_form4`` shape). ``form`` "4" or "4/A";
    ``accepted_at`` the EDGAR acceptance time (None when unknown); ``filing_date`` YYYY-MM-DD."""
    now = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    d = details or {}
    ticker = clean_ticker(d.get("issuer_ticker"))
    roles = list(d.get("owner_roles") or [])
    out = []
    for code in OPEN_MARKET_CODES:
        txs = [t for t in d.get("transactions") or [] if not t.get("derivative") and t.get("code") == code]
        if not txs:
            continue
        shares = sum(float(t["shares"]) for t in txs if t.get("shares") is not None) or None
        priced = [(float(t["shares"]), float(t["price"])) for t in txs
                  if t.get("shares") is not None and t.get("price") is not None and t["price"] > 0]
        value = round(sum(s * p for s, p in priced), 2) if priced else None
        priced_shares = sum(s for s, _p in priced)
        avg_price = round(value / priced_shares, 4) if value is not None and priced_shares > 0 else None
        dates = sorted(x for x in (_day(t.get("date")) for t in txs) if x)
        if form not in ("4", "4/A"):
            raise ValueError(f"form must be '4' or '4/A', got {form!r}")
        reason = None
        if form == "4/A":
            reason = AMENDMENT_REASON
        elif ticker is None:
            reason = "no ticker"
        out.append({
            "id": f"insider:{accession}:{code}", "source": "insider", "accession": accession, "form": form,
            "accepted_at": _iso_utc(accepted_at),
            # 'found'; a backfilled data-set row without a time waits for the issuer lookup ('pending')
            "acceptance_state": "found" if _iso_utc(accepted_at) else ("pending" if origin == "backfill" else "missing"),
            "filing_date": _day(filing_date),
            "issuer_cik": str(d.get("issuer_cik") or "").lstrip("0") or None, "ticker": ticker,
            "issuer_name": d.get("issuer_name") or None,
            "owner_cik": str(d.get("owner_cik") or "").lstrip("0") or None, "owner_name": d.get("owner_name") or None,
            "role_class": role_class(roles), "roles_raw": ", ".join(roles) or None,
            "co_reporters": int(d.get("co_reporters") or 0), "code": code,
            "shares": shares, "value_usd": value, "avg_price": avg_price,
            "trade_date_first": dates[0] if dates else None, "trade_date_last": dates[-1] if dates else None,
            "plan_10b5_1": 1 if d.get("plan_10b5_1") else 0, "plan_source": d.get("plan_10b5_1_source"),
            "cluster": 0, "measurable": 0 if reason else 1, "not_measurable_reason": reason,
            "official_url": url or edgar_index_url(d.get("issuer_cik"), accession),
            "origin": origin, "first_seen": now, "updated_at": now,
        })
    return out


def cluster_ids(trades: Iterable[dict], window_days: int = insider_summary.CLUSTER_WINDOW_DAYS) -> Set[str]:
    """Ids of the point-in-time cluster trades among ``trades`` (insider rows; amendments and rows without an
    issuer, owner or trade date never count)."""
    mins = insider_summary._CLUSTER_MIN_INSIDERS
    groups: Dict[tuple, List[dict]] = {}
    for t in trades:
        if t.get("source") != "insider" or t.get("form") != "4":
            continue
        if t.get("code") == "P":
            kind = "buy"
        elif t.get("code") == "S" and not t.get("plan_10b5_1"):
            kind = "sell"
        else:
            continue
        issuer = t.get("issuer_cik") or t.get("ticker")
        owner = t.get("owner_cik") or (t.get("owner_name") or "").strip().upper()
        when = _day(t.get("trade_date_first"))
        filed = t.get("accepted_at") or t.get("filing_date")
        if not issuer or not owner or not when or not filed:
            continue
        groups.setdefault((issuer, kind), []).append(
            {"id": t["id"], "owner": owner, "when": date.fromisoformat(when), "filed": str(filed)})
    out: Set[str] = set()
    for (_issuer, kind), rows in groups.items():
        need = mins[kind]
        for r in rows:
            lo = r["when"] - timedelta(days=window_days)
            owners = {x["owner"] for x in rows
                      if lo <= x["when"] <= r["when"] and x["filed"] <= r["filed"]}
            if len(owners) >= need:
                out.add(r["id"])
    return out
