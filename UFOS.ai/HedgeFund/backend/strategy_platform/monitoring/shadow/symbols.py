"""User symbol mapping ISIN → US exchange ticker (SPEC §20.10, user decision 2026-10-18, option (a)).

Trade Republic (and other ISIN-only exports) carry no ticker, so no position could be priced from market data. The
user maps an ISIN to its US listing ONCE per instrument; the mapping is GLOBAL (an ISIN identifies the instrument in
every Shadow account) and insert-only (``sh_symbol_maps``: the newest row per ISIN wins, a removal is a row with
``symbol`` null; every change is audited).

A mapping is accepted only when (1) the ISIN is valid (format + check digit) and a US security (``US…``), (2) the
user confirms that the ticker is the US listing of that ISIN, and (3) the ticker is in the Alpaca asset list as an
ACTIVE, TRADABLE US equity / ETF on an exchange (not OTC). Otherwise it is refused with the reason. Never an
automatic mapping, never a guess from names (the asset name is only shown back to the user). When the asset list
cannot be read, nothing is saved.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Callable, Dict, List, Optional

from ...execution.errors import TradingError
from ...storage import db

ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
SCOPE = "global"
NOTICE = ("Your mapping ISIN → listing applies to every Shadow account: a US ticker (Alpaca, USD, only US ISINs) or "
          "a listing from the EODHD search (its own currency). Prices are never converted — a listing in another "
          "currency than your cost shows its value without P&L.")
ASSETS_TTL_SECONDS = 6 * 3600
NOT_FOUND = "No symbol mapping for this ISIN."

_source: Optional[Callable[[], List[dict]]] = None
_cache: Dict[str, object] = {"at": 0.0, "rows": None}
_lock = threading.Lock()


def set_assets_source(fn: Optional[Callable[[], List[dict]]]) -> None:
    """Tests: ``fn() -> [{symbol, name, asset_class, exchange, tradable, status}]`` instead of Alpaca."""
    global _source
    _source = fn
    _cache.update(at=0.0, rows=None)


def isin_ok(isin: str) -> bool:
    """Format + ISIN check digit (Luhn over the letters-as-numbers expansion)."""
    if not ISIN_RE.fullmatch(isin or ""):
        return False
    digits = "".join(str(int(c, 36)) for c in isin[:-1])
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - total % 10) % 10 == int(isin[-1])


def _assets() -> Dict[str, dict]:
    if _source is not None:
        rows = _source()
    else:
        with _lock:
            if _cache["rows"] is None or time.monotonic() - float(_cache["at"]) > ASSETS_TTL_SECONDS:
                from ...data.providers.alpaca import AlpacaProvider
                _cache.update(rows=AlpacaProvider().assets(), at=time.monotonic())
            rows = _cache["rows"]
    return {str(r.get("symbol") or "").upper(): r for r in rows or []}


def check_ticker(symbol: str) -> dict:
    """The Alpaca asset of ``symbol`` when it may price a US security, else TradingError with the reason."""
    try:
        assets = _assets()
    except Exception as ex:  # noqa: BLE001 — never save an unverified mapping
        msg = getattr(ex, "user_message", None) or str(ex) or type(ex).__name__
        raise TradingError(503, "symbol_check_unavailable", "Cannot check the ticker against the Alpaca asset list ("
                           f"{msg[:200]}) — the mapping was not saved; try again later.")
    a = assets.get(symbol)
    if a is None:
        raise TradingError(409, "symbol_refused", f"{symbol} is not in the Alpaca asset list of active US equities / "
                                                  "ETFs — check the ticker of the US listing (Alpaca spelling, e.g. "
                                                  "BRK.B).")
    cls = str(a.get("asset_class") or "")
    if cls != "us_equity":
        raise TradingError(409, "symbol_refused", f"{symbol} is a {cls or 'non-equity'} asset at Alpaca, not a US "
                                                  "equity / ETF.")
    if str(a.get("status") or "").lower() != "active":
        raise TradingError(409, "symbol_refused", f"{symbol} is not active at Alpaca ({a.get('status')}).")
    if not a.get("tradable"):
        raise TradingError(409, "symbol_refused", f"{symbol} is not tradable at Alpaca.")
    if str(a.get("exchange") or "").upper() in ("OTC", ""):
        raise TradingError(409, "symbol_refused", f"{symbol} is not listed on a US exchange "
                                                  f"({a.get('exchange') or 'no exchange'}) — OTC symbols are not "
                                                  "used for prices.")
    return a


def _out(r) -> dict:
    ps = r["price_source"] or "alpaca_us"
    # fix1023 round 2: provenance as stored — rows before v24 (NULL) were saved with the then required flag
    via = r["confirmed_via"] or CONFIRM_FLAG
    return {"isin": r["isin"], "symbol": r["symbol"], "exchange": r["exchange"], "asset_name": r["asset_name"],
            "confirmed_us_listing": ps == "alpaca_us" and bool(r["confirmed_us_listing"]),
            "confirmed_listing": ps == "eodhd" and via == CONFIRM_FLAG, "confirmed_via": via,
            "source": "user_mapping", "price_source": ps, "listing": r["listing"] or r["symbol"],
            "currency": r["currency"] or "USD",
            "validated_against": "eodhd_search" if ps == "eodhd" else "alpaca_assets", "created_at": r["ts"],
            "reason": r["reason"]}


def mappings() -> Dict[str, dict]:
    """Current mappings {isin: mapping} (newest row per ISIN; removed ones left out)."""
    with db._connect() as conn:
        rows = conn.execute("SELECT * FROM sh_symbol_maps ORDER BY id").fetchall()
    cur: Dict[str, dict] = {}
    for r in rows:
        if r["symbol"] is None:
            cur.pop(r["isin"], None)
        else:
            cur[r["isin"]] = _out(r)
    return cur


def symbol_for(isin: Optional[str]) -> Optional[str]:
    m = mappings().get(isin or "")
    return m["symbol"] if m else None


def payload() -> dict:
    return {"scope": SCOPE, "notice": NOTICE, "mappings": sorted(mappings().values(), key=lambda m: m["isin"])}


CONFIRM_FLAG = "confirm_flag"      # the request carried confirm_us_listing / confirm_listing: true
DIALOG = "dialog"                  # saved from the app's summary dialog without a flag (fix1023)


def lookup(symbol: str, isin: Optional[str] = None) -> dict:
    """Read-only check of a typed US ticker for the mapping dialog (fix1023 round 2): the Alpaca asset BEFORE
    saving — never writes. A refused ticker is ``usable: false`` + the reason; an unreadable asset list is a 503."""
    symbol = (symbol or "").strip().upper()
    isin = (isin or "").strip().upper() or None
    try:
        a = _assets().get(symbol)
    except Exception as ex:  # noqa: BLE001 — no guess: the dialog says the check is unavailable
        msg = getattr(ex, "user_message", None) or str(ex) or type(ex).__name__
        raise TradingError(503, "symbol_check_unavailable", f"Cannot read the Alpaca asset list ({msg[:200]}).")
    usable, why = True, None
    try:
        check_ticker(symbol)
    except TradingError as ex:
        if ex.status == 503:
            raise
        usable, why = False, ex.user_message
    maps = mappings()
    mapped = next((m["isin"] for m in maps.values() if m["price_source"] == "alpaca_us" and m["symbol"] == symbol),
                  None)
    if usable and mapped is not None and mapped != isin:
        usable, why = False, f"{symbol} is already mapped to {mapped} — remove that mapping first."
    cur = maps.get(isin) if isin else None
    return {"ticker": symbol, "found": a is not None, "asset_name": (a or {}).get("name"),
            "exchange": (a or {}).get("exchange"), "status": (a or {}).get("status"),
            "tradable": bool((a or {}).get("tradable")), "asset_class": (a or {}).get("asset_class"),
            "usable": usable, "refused_reason": why, "mapped_to_isin": mapped,
            "isin": isin, "isin_valid": isin_ok(isin) if isin else None,
            "isin_us": isin.startswith("US") if isin else None,
            "current_mapping": ({"symbol": cur["symbol"], "price_source": cur["price_source"]} if cur else None)}


def put(isin: str, symbol: str, confirm_us_listing, reason: Optional[str], actor: str = "user") -> dict:
    isin = (isin or "").strip().upper()
    symbol = (symbol or "").strip().upper()
    if not isin_ok(isin):
        raise TradingError(400, "invalid_isin", f"{isin[:20] or 'empty'} is not a valid ISIN (format or check digit).")
    if not isin.startswith("US"):
        raise TradingError(409, "non_us_isin", f"{isin} is not a US security — US market data can only price US "
                                               "securities (ISIN starting with US). Enter a valuation price for it "
                                               "instead.")
    if not TICKER_RE.fullmatch(symbol):
        raise TradingError(400, "invalid_symbol", "a US ticker in Alpaca spelling (e.g. SPGI, BRK.B)")
    via = CONFIRM_FLAG if confirm_us_listing is True else DIALOG     # fix1023: provenance, no longer required
    other = next((m for m in mappings().values() if m["price_source"] == "alpaca_us" and m["symbol"] == symbol
                  and m["isin"] != isin), None)
    if other is not None:
        raise TradingError(409, "symbol_refused", f"{symbol} is already mapped to {other['isin']} — remove that "
                                                  "mapping first.")
    a = check_ticker(symbol)
    from .service import _ts
    ts = _ts()
    name = (str(a.get("name") or "")[:200]) or None
    with db._connect() as conn:
        conn.execute("INSERT INTO sh_symbol_maps (isin, symbol, exchange, asset_name, confirmed_us_listing, ts, actor,"
                     " reason, price_source, listing, currency, confirmed_via) VALUES (?, ?, ?, ?, ?, ?, ?, ?,"
                     " 'alpaca_us', ?, 'USD', ?)",
                     (isin, symbol, str(a.get("exchange") or "")[:20], name, 1 if via == CONFIRM_FLAG else 0, ts,
                      actor, reason, symbol, via))
        db._insert_audit(conn, actor, "shadow_symbol_map", {"isin": isin, "symbol": symbol,
                                                            "exchange": a.get("exchange"), "reason": reason,
                                                            "confirmed_via": via})
    return mappings()[isin]


# --- EODHD listings (§20.11) -----------------------------------------------------------------------------------

def _eodhd_error(ex: BaseException) -> TradingError:
    from ...core.errors import SecretMissing
    from ...data.providers import eodhd
    if isinstance(ex, eodhd.EodhdLimitReached):
        return TradingError(409, "eodhd_limit", ex.user_message, budget=eodhd.budget())
    if isinstance(ex, SecretMissing):
        return TradingError(424, "key_missing", eodhd.MISSING_KEY_MESSAGE)
    return TradingError(503, "source_unavailable", str(getattr(ex, "user_message", None) or ex)[:300])


def listings(isin: str, refresh: bool = False) -> dict:
    """EODHD listing candidates of an ISIN (1 call, or 0 when cached) — the user picks one; never automatic."""
    from ...core.errors import UserFacingError
    from ...data.providers import eodhd
    isin = (isin or "").strip().upper()
    if not isin_ok(isin):
        raise TradingError(400, "invalid_isin", f"{isin[:20] or 'empty'} is not a valid ISIN (format or check digit).")
    try:
        res = eodhd.search_isin(isin, refresh)
    except UserFacingError as ex:
        raise _eodhd_error(ex)
    return {"isin": isin, "source": "eodhd", "cached": res["cached"], "searched_at": res["searched_at"],
            "candidates": res["candidates"], "budget": eodhd.budget()}


def refresh_listing(listing: str, actor: str = "user") -> dict:
    """Fetches a listing's EOD closes again NOW — costs 1 EODHD call (shown with the counter); audited."""
    from ...core.errors import UserFacingError
    from ...data.providers import eodhd
    listing = (listing or "").strip().upper()
    if not eodhd.LISTING_RE.fullmatch(listing):
        raise TradingError(400, "invalid_listing", "a listing CODE.EXCHANGE (e.g. SAP.XETRA)")
    if not any(m["price_source"] == "eodhd" and m["listing"] == listing for m in mappings().values()):
        raise TradingError(404, "not_found", f"{listing} is not one of your EODHD listings.")
    db.add_audit(actor, "shadow_eodhd_refresh", {"listing": listing})
    try:
        bars = eodhd.closes(listing, refresh=True)
    except eodhd.EodhdFetchFailed as ex:
        return {"listing": listing, "ok": False, "reason": ex.user_message, "closes": 0, "last": None,
                "cost": "1 EODHD call", "budget": eodhd.budget()}
    except UserFacingError as ex:
        raise _eodhd_error(ex)
    return {"listing": listing, "ok": True, "reason": None, "closes": len(bars),
            "last": {"date": bars[-1][0], "close": bars[-1][1]} if bars else None, "cost": "1 EODHD call",
            "budget": eodhd.budget()}


def put_listing(isin: str, listing: str, confirm_listing, reason: Optional[str], actor: str = "user") -> dict:
    """Maps an ISIN to ONE EODHD listing the user picked from the cached search candidates (no network call)."""
    from ...data.providers import eodhd
    isin = (isin or "").strip().upper()
    listing = (listing or "").strip().upper()
    if not isin_ok(isin):
        raise TradingError(400, "invalid_isin", f"{isin[:20] or 'empty'} is not a valid ISIN (format or check digit).")
    if not eodhd.LISTING_RE.fullmatch(listing):
        raise TradingError(400, "invalid_listing", "a listing CODE.EXCHANGE from the search (e.g. SAP.XETRA)")
    via = CONFIRM_FLAG if confirm_listing is True else DIALOG         # fix1023: provenance, no longer required
    cached = eodhd.cached_search(isin)
    if cached is None:
        raise TradingError(409, "listing_refused", f"search the listings of {isin} first")
    cand = next((c for c in cached["candidates"] if c["listing"] == listing), None)
    if cand is None:
        raise TradingError(409, "listing_refused", f"{listing} is not a listing of {isin} in the EODHD search")
    if not cand.get("currency"):
        raise TradingError(409, "listing_refused", f"EODHD names no currency for {listing} — it cannot be used")
    from .service import _ts
    ts = _ts()
    with db._connect() as conn:
        conn.execute("INSERT INTO sh_symbol_maps (isin, symbol, exchange, asset_name, confirmed_us_listing, ts, actor,"
                     " reason, price_source, listing, currency, confirmed_via) VALUES (?, ?, ?, ?, 0, ?, ?, ?, 'eodhd',"
                     " ?, ?, ?)",
                     (isin, cand["code"], cand["exchange"], cand.get("name"), ts, actor, reason, listing,
                      cand["currency"], via))
        db._insert_audit(conn, actor, "shadow_symbol_map", {"isin": isin, "source": "eodhd", "listing": listing,
                                                            "currency": cand["currency"], "reason": reason,
                                                            "confirmed_via": via})
    return mappings()[isin]


def delete(isin: str, reason: Optional[str], actor: str = "user") -> dict:
    isin = (isin or "").strip().upper()
    cur = mappings().get(isin)
    if cur is None:
        raise TradingError(404, "not_found", NOT_FOUND)
    from .service import _ts
    ts = _ts()
    with db._connect() as conn:
        conn.execute("INSERT INTO sh_symbol_maps (isin, symbol, exchange, asset_name, confirmed_us_listing, ts, actor,"
                     " reason) VALUES (?, NULL, NULL, NULL, 0, ?, ?, ?)", (isin, ts, actor, reason))
        db._insert_audit(conn, actor, "shadow_symbol_unmap", {"isin": isin, "symbol": cur["symbol"],
                                                              "reason": reason})
    return {"removed": {"isin": isin, "symbol": cur["symbol"], "ts": ts}}


def price_sources() -> dict:
    """Provider status for the WPF (§20.11) — never a key value."""
    from ... import secrets as sec
    from ...data.providers import eodhd, kraken_public
    try:
        present, err = bool(sec.get_secret(eodhd.SECRET)), None
    except Exception as ex:  # noqa: BLE001 — the Credential Manager could not be read
        present, err = None, getattr(ex, "user_message", None) or "The Credential Manager could not be read."
    return {"order": ["valuation", "listing_same_currency", "listing_other_currency", "not_available"],
            "sources": [
                {"id": "alpaca_us", "label": "Alpaca — US listings (USD)", "currency": "USD", "key_name": None},
                {"id": "eodhd", "label": "EODHD — end-of-day closes, stocks / ETFs incl. EU venues (listing currency)",
                 "currency": "listing", "key_name": eodhd.SECRET, "key_present": present, "key_error": err,
                 "budget": eodhd.budget(), "cached_listings_today": eodhd.cached_listings_today(),
                 "failures_today": eodhd.failures_today(),
                 "refresh": "POST /v2/shadow/price-sources/eodhd/refresh {listing} — costs 1 EODHD call",
                 "terms": eodhd.TERMS},
                {"id": "kraken_public", "label": "Kraken public market data — crypto in EUR", "currency": "EUR",
                 "key_name": None, "pairs": {k: v[1] for k, v in kraken_public.PAIRS.items()}}],
            "no_source": ["issuer derivatives (turbos, calls) — enter a valuation",
                          "bonds (e.g. US Treasury) — enter a valuation", "private funds — enter a valuation",
                          "other crypto coins — enter a valuation"],
            "later": ["OpenFIGI ISIN → ticker lookup (not built)"]}
