"""Alpaca asset list for Research (SPEC §4.2 ``GET /research/assets``, §10 MR2 task ``asset_refresh``).

- ``refresh(sources, now)``: ``AlpacaProvider.assets()`` (active US equities + crypto) → normalised rows
  replacing the ``assets`` table of market_research.db in ONE transaction (a failed or empty answer keeps
  the previous list and raises). Platform symbols (``core/instruments``): equities as on the exchange
  (OTC assets are skipped — not on SIP/IEX), crypto ``BASE-USD`` (only ``…/USD`` pairs, Q5). ``kind``:
  ``etf`` when the symbol is a known ETF (``core.instruments.KNOWN_ETFS``) or the name contains the word
  ETF/ETN (Alpaca has no ETF flag — SPEC §11 verify), else ``stock``; crypto ``crypto``. Names are
  untrusted plain text (control characters stripped, ≤ ``MAX_ASSET_NAME_CHARS``).
- ``search(q, limit)``: order exact symbol, symbol prefix, name word prefix, name substring (ties:
  shorter symbol, then alphabetical). Empty table → one refresh first; failing → its ``UserFacingError``.
- ``require(symbol)``: the asset row of a platform symbol or ``SymbolNotCovered`` (404).
- Empty table + failed refresh (review L7): the next on-demand refresh is not tried before
  ``ASSET_EMPTY_RETRY_SECONDS`` (60 s); meanwhile the same error (same kind, same reason) is raised again —
  a search storm cannot turn into a refresh storm.
- ``run(...)``: the supervised daily refresh task (``ASSET_REFRESH_HOURS``); a failed refresh is a state
  (logged, retried after ``ASSET_REFRESH_RETRY_SECONDS``), not a crash.

Part of UFOS.ai, a proof-of-concept portfolio project; research only.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time as _time
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, List, Optional

from strategy_platform.core.clock import iso, to_utc
from strategy_platform.core.errors import DataSourceUnavailable, UserFacingError
from strategy_platform.core.instruments import CRYPTO_RE, EQUITY_RE, KNOWN_ETFS, instrument

from . import limits
from . import store as default_store

logger = logging.getLogger(__name__)

META_REFRESHED_AT = "assets_refreshed_at"
QUERY_RE = re.compile(r"^[A-Za-z0-9 .\-/]{1,%d}$" % limits.ASSET_QUERY_MAX_CHARS)
ETF_WORD = re.compile(r"\b(ETF|ETN)\b", re.IGNORECASE)
CRYPTO_NOT_COVERED = "Not available from Alpaca (the configured crypto source)."


class SymbolNotCovered(UserFacingError):
    """The symbol is valid but not in Alpaca's asset list (404)."""


def not_covered(symbol: str, asset_class: str) -> SymbolNotCovered:
    if asset_class == "crypto":
        return SymbolNotCovered(CRYPTO_NOT_COVERED)
    return SymbolNotCovered(f"Not available from Alpaca: {symbol} is not in Alpaca's list of active US equities.")


def normalise(raw: List[dict], fetched_at: str) -> List[dict]:
    out, seen = [], set()
    for it in raw or []:
        sym = str(it.get("symbol") or "").strip().upper()
        cls = it.get("asset_class")
        exchange = limits.clip(it.get("exchange") or "", 20) or None
        if cls == "crypto":
            if not sym.endswith("/USD"):
                continue
            sym = sym.replace("/", "-")
            if not CRYPTO_RE.match(sym):
                continue
            asset_class, kind = "crypto", "crypto"
        elif cls == "us_equity":
            if not EQUITY_RE.match(sym) or (exchange or "").upper() == "OTC":
                continue
            asset_class = "equity"
        else:
            continue
        name = limits.clip(it.get("name") or "", limits.MAX_ASSET_NAME_CHARS) or sym
        if asset_class == "equity":
            kind = "etf" if sym in KNOWN_ETFS or ETF_WORD.search(name) else "stock"
        if sym in seen:
            continue
        seen.add(sym)
        out.append({"symbol": sym, "name": name, "asset_class": asset_class, "kind": kind, "exchange": exchange,
                    "tradable": bool(it.get("tradable")), "status": limits.clip(it.get("status") or "active", 20),
                    "fetched_at": fetched_at})
    return out


_failure = {"at": None, "error": None}          # last failed refresh (monotonic time, UserFacingError)


def reset_failure() -> None:
    _failure["at"], _failure["error"] = None, None


def refresh(sources, now=None, *, store=default_store) -> int:
    now = to_utc(now or datetime.now(timezone.utc))
    try:
        rows = normalise(sources.assets(), iso(now))
    except UserFacingError as ex:
        _failure["at"], _failure["error"] = _time.monotonic(), ex
        raise
    if not rows:
        ex = DataSourceUnavailable("alpaca", "assets", "the asset list came back empty — the previous list is kept")
        _failure["at"], _failure["error"] = _time.monotonic(), ex
        raise ex
    reset_failure()
    if not rows:
        raise DataSourceUnavailable("alpaca", "assets", "the asset list came back empty — the previous list is kept")
    n = store.assets_replace(rows)
    store.meta_set(META_REFRESHED_AT, iso(now))
    logger.info("Research asset list refreshed: %d assets", n)
    return n


def refreshed_at(store=default_store) -> Optional[str]:
    return store.meta_get(META_REFRESHED_AT)


def _refresh_empty(sources, now, store) -> None:
    """On-demand refresh of an EMPTY table, spaced ≥ ASSET_EMPTY_RETRY_SECONDS after a failure."""
    at, err = _failure["at"], _failure["error"]
    if at is not None and err is not None and _time.monotonic() - at < limits.ASSET_EMPTY_RETRY_SECONDS:
        raise err
    refresh(sources, now, store=store)


def ensure_loaded(sources, *, store=default_store, now=None) -> None:
    if store.assets_count() == 0:
        _refresh_empty(sources, now, store)


def require(symbol: str, sources, *, store=default_store, now=None) -> dict:
    inst = instrument(symbol)               # ValueError → 400 by the caller
    row = store.asset_get(inst.symbol)
    if row is None and store.assets_count() == 0:
        _refresh_empty(sources, now, store)
        row = store.asset_get(inst.symbol)
    if row is None:
        raise not_covered(inst.symbol, inst.asset_class)
    return row


def _word_prefix(name: str, ql: str) -> bool:
    return re.search(r"(^|[\s\-(/.,&])" + re.escape(ql), name.lower()) is not None


def rank(row: dict, q: str) -> Optional[int]:
    qs = q.strip().upper().replace("/", "-")
    ql = q.strip().lower()
    sym, name = row["symbol"], row["name"] or ""
    if sym == qs:
        return 0
    if sym.startswith(qs):
        return 1
    if _word_prefix(name, ql):
        return 2
    if ql in name.lower():
        return 3
    return None


def search(q: str, limit: int, sources, *, store=default_store, now=None) -> dict:
    ensure_loaded(sources, store=store, now=now)
    qn = q.strip()
    cands = store.assets_candidates(qn.upper().replace("/", "-"))
    if "/" in qn or "-" in qn:
        cands += [c for c in store.assets_candidates(qn) if c["symbol"] not in {x["symbol"] for x in cands}]
    scored = []
    for row in cands:
        r = rank(row, qn)
        if r is not None:
            scored.append((r, len(row["symbol"]), row["symbol"], row))
    scored.sort(key=lambda x: x[:3])
    items = [{"symbol": row["symbol"], "name": row["name"], "asset_class": row["asset_class"], "kind": row["kind"],
              "exchange": row["exchange"], "tradable": row["tradable"]} for *_k, row in scored[:limit]]
    return {"items": items, "refreshed_at": refreshed_at(store), "source": "alpaca"}


def due(now, store=default_store) -> float:
    """Seconds until the next refresh is due (0 = now)."""
    last = refreshed_at(store)
    if not last:
        return 0.0
    nxt = to_utc(last) + timedelta(hours=limits.ASSET_REFRESH_HOURS)
    return max(0.0, (nxt - to_utc(now)).total_seconds())


async def run(sources_fn, *, store=default_store, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
              now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
    """Supervised task "asset_refresh": refresh once a day (Research priority, Research pool)."""
    from strategy_platform.data import http_gate
    from .executor import run_blocking
    while True:
        delay = float(limits.STORE_REOPEN_SECONDS)
        if store.is_open():
            try:
                wait = await run_blocking(due, now(), store)
                if wait <= 0:
                    with http_gate.research_priority():
                        await run_blocking(refresh, sources_fn(), now(), store=store)
                    wait = await run_blocking(due, now(), store)
                delay = max(60.0, min(wait, limits.ASSET_REFRESH_HOURS * 3600.0))
            except store.StoreUnavailable as ex:
                logger.warning("asset refresh: %s", ex.user_message)
            except UserFacingError as ex:
                logger.warning("Research asset list refresh failed: %s — next attempt in %d s", ex.user_message,
                               limits.ASSET_REFRESH_RETRY_SECONDS)
                delay = float(limits.ASSET_REFRESH_RETRY_SECONDS)
        await sleep(delay)
