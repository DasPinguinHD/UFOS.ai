"""Disclosures watchlist alerts (CONGRESS_API.md §5; user decisions 2026-10-24: Monitoring → Alerts only, NO toast).

- ``disclosure_watched_member``: a NEW congress filing of a member on the user's watch list (``disclosure_watch``).
- ``disclosure_held_ticker``: a NEW insider or congress trade (P/S) whose ticker is currently held in an active platform
  account (``portfolio.ledger.holdings``) or in a Shadow Account (its replayed positions with a symbol).

Only rows first seen by the live paths (the 6-hourly congress pull, the live Form 4 hook) alert — the backfill never
does. One alert per (kind, trade id): ``monitoring.alerts.emit`` dedupes on ``disclosure:<kind>:<trade id>``. Texts
are plain backend text (no AI). A failure is logged and kept in ``STATE`` (shown in the status) — it never stops the
pull or the collector.

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

from . import disclosure_db as ddb

logger = logging.getLogger(__name__)

KIND_MEMBER = "disclosure_watched_member"
KIND_HELD = "disclosure_held_ticker"
STATE = {"emitted": 0, "last_at": None, "last_error": None}
HELD_TTL_SECONDS = 600                 # holdings change rarely; the live Form 4 hook may call this often
_held_cache: dict = {"at": 0.0, "value": None}


def held_tickers() -> Dict[str, List[str]]:
    """{TICKER: [where]} over the active platform accounts and the Shadow Accounts."""
    from strategy_platform.execution import accounts
    from strategy_platform.monitoring.shadow import service as shadow
    from strategy_platform.portfolio import ledger
    out: Dict[str, List[str]] = {}
    for r in accounts.rows():
        for _source, symbols in ledger.holdings(r["id"]).items():
            for sym in symbols:
                out.setdefault(str(sym).upper(), []).append(r["id"])
    for a in shadow.account_ids():
        for p in shadow.detail(a["id"])["positions"]:
            if p.get("symbol"):
                out.setdefault(str(p["symbol"]).upper(), []).append(f"Shadow Account {a.get('label') or a['id']}")
    return {k: sorted(set(v)) for k, v in out.items()}


def held_tickers_cached(clock=None) -> Dict[str, List[str]]:
    import time
    now = (clock or time.monotonic)()
    if _held_cache["value"] is None or now - _held_cache["at"] > HELD_TTL_SECONDS:
        _held_cache["value"], _held_cache["at"] = held_tickers(), now
    return _held_cache["value"]


def _money(v) -> str:
    return f"${v:,.0f}" if v else "amount not reported"


def describe(t: dict) -> str:
    side = {"P": "bought", "S": "sold"}.get(t.get("code"), "reported")
    if t.get("source") == "congress":
        who = f"{t.get('owner_name') or t.get('owner_cik')} ({t.get('chamber') or '?'}, {t.get('party') or '?'}-{t.get('state') or '?'})"
        amount = t.get("amount_label") or "amount range not reported"
        return (f"Congress: {who} {side} {t.get('ticker') or t.get('issuer_name') or 'an asset'} ({amount}), "
                f"traded {t.get('trade_date_first') or '?'}, filed {t.get('filing_date')}.")
    who = f"{t.get('owner_name') or 'an insider'} ({t.get('role_class') or 'insider'})"
    return (f"Insider: {who} {side} {t.get('ticker') or t.get('issuer_name')} ({_money(t.get('value_usd'))}), "
            f"traded {t.get('trade_date_first') or '?'}, filed {t.get('filing_date')}.")


def on_new_trades(trades: Iterable[dict], held: Optional[Dict[str, List[str]]] = None,
                  watched: Optional[set] = None, emit=None) -> int:
    """Alerts for trades first seen by a live path; returns how many alerts were stored. Never raises."""
    trades = [t for t in trades if t.get("code") in ("P", "S")]
    if not trades:
        return 0
    try:
        if emit is None:
            from strategy_platform.monitoring import alerts
            emit = alerts.emit
        if watched is None:
            watched = set(ddb.watch_get("member"))
        if held is None:
            held = held_tickers_cached() if any(t.get("ticker") for t in trades) else {}
        n = 0
        for t in trades:
            text = describe(t)
            data = {"trade_id": t["id"], "source": t["source"], "ticker": t.get("ticker")}
            if t.get("source") == "congress" and t.get("owner_cik") in watched:
                if emit(KIND_MEMBER, f"Watched member filed: {t.get('owner_name') or t.get('owner_cik')}", text,
                        f"disclosure:{KIND_MEMBER}:{t['id']}", ref=t["id"], data=data) is not None:
                    n += 1
            where = held.get(str(t.get("ticker") or "").upper())
            if where:
                if emit(KIND_HELD, f"Held ticker in a new filing: {t['ticker']}",
                        f"{text} {t['ticker']} is held in {', '.join(where)}.",
                        f"disclosure:{KIND_HELD}:{t['id']}", ref=t["id"], data=dict(data, held_in=where)) is not None:
                    n += 1
        STATE["emitted"] += n
        if n:
            STATE["last_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return n
    except Exception as ex:  # noqa: BLE001 — recorded for the status endpoint
        STATE["last_error"] = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}: {type(ex).__name__}: {ex}"
        logger.warning("Disclosures alerts not stored: %s", ex)
        return 0
