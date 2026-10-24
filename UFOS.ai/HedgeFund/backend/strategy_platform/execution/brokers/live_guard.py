"""Live guard (SPEC §18.7) — built, live OFF.

Live trading would require ALL four conditions: (1) the per-broker live flag in settings (``br_live_guard``,
insert-only history), (2) the typed confirmation ``GO LIVE <ACCOUNT-ID IN CAPITALS> WITH REAL MONEY``, (3) the
strategy in lifecycle state ``live``, (4) a capital cap set (``capital_cap_usd`` 100..1,000,000).

This build hard-codes ``BUILD_ALLOWS_LIVE = False``: ``evaluate`` reports every condition and the decision is
ALWAYS refused with ``LIVE_DISABLED``. No live endpoint is configured: ``endpoint_for(broker, live=True)`` raises
``LiveTradingDisabled``; adapters build their clients only through ``paper_endpoint`` and check the client's real
URLs with ``assert_paper_url`` / ``assert_paper_urls``. The known LIVE hosts appear ONLY in ``LIVE_HOSTS`` below
(a test scans the broker sources for them).

Part of UFOS.ai, a proof-of-concept portfolio project; PAPER / DEMO TRADING ONLY.
"""
from __future__ import annotations

import json
import math
from typing import Iterable, List, Optional
from urllib.parse import urlparse

from ...core.errors import UserFacingError

BUILD_ALLOWS_LIVE = False
LIVE_DISABLED = "Live trading is disabled in this build (stage 12 requires the user's explicit go)."

# The ONLY endpoints this build may talk to (scheme + host; paths are the SDKs' own).
PAPER_ENDPOINTS = {
    "alpaca_paper": "https://paper-api.alpaca.markets",
    "binance_demo": "https://demo-api.binance.com",          # Binance Spot Demo Mode (SPEC §18.14)
}
PAPER_HOSTS = {"alpaca_paper": ("paper-api.alpaca.markets",), "binance_demo": ("demo-api.binance.com",)}
# wss hosts a broker may use (allowed by the guard; this build polls REST only — SPEC §18.14)
PAPER_WS_HOSTS = {"binance_demo": ("demo-ws-api.binance.com", "demo-stream.binance.com")}
# Deny-list (documentation + tests): the production trading hosts. Never used to build a client.
LIVE_HOSTS = ("api.alpaca.markets", "futures.kraken.com", "api.kraken.com", "api.binance.com", "stream.binance.com", "ws-api.binance.com")

CAP_MIN, CAP_MAX = 100.0, 1_000_000.0
CONDITIONS = (
    ("live_flag", "Live flag switched on for this broker"),
    ("typed_confirmation", "Typed confirmation"),
    ("strategy_live", "Strategy in state Live"),
    ("capital_cap", "Capital cap set"),
)


class LiveTradingDisabled(UserFacingError):
    """Any attempt to reach a live endpoint or to trade live in this build."""

    def __init__(self, detail: Optional[str] = None):
        super().__init__(LIVE_DISABLED, detail=detail or "")


def confirmation_phrase(account_id: str) -> str:
    return f"GO LIVE {account_id.upper()} WITH REAL MONEY"


def paper_endpoint(broker: str) -> str:
    if broker not in PAPER_ENDPOINTS:
        raise ValueError(f"unknown broker {broker!r}")
    return PAPER_ENDPOINTS[broker]


def endpoint_for(broker: str, live: bool = False) -> str:
    """The endpoint of ``broker``; ``live=True`` always raises (no live endpoint exists in this build)."""
    if live:
        raise LiveTradingDisabled(f"live endpoint requested for {broker}")
    return paper_endpoint(broker)


def assert_paper_url(broker: str, url) -> str:
    """``url`` must be https on one of the broker's paper/demo hosts (or wss on its demo WebSocket hosts); otherwise
    LiveTradingDisabled."""
    try:
        u = urlparse(str(url or ""))
    except ValueError as ex:
        raise LiveTradingDisabled(f"unparsable URL for {broker}") from ex
    host = (u.hostname or "").lower()
    if u.scheme == "wss" and host in PAPER_WS_HOSTS.get(broker, ()):
        return str(url)
    if u.scheme != "https" or host not in PAPER_HOSTS.get(broker, ()):
        raise LiveTradingDisabled(f"{broker}: refused non-paper URL host {host or '?'}")
    return str(url)


def assert_paper_urls(broker: str, urls: Iterable) -> None:
    n = 0
    for u in urls:
        assert_paper_url(broker, u)
        n += 1
    if n == 0:
        raise LiveTradingDisabled(f"{broker}: no endpoint URL to check")


# --- settings (insert-only history) -------------------------------------------------------------------------

def settings(account_id: str) -> dict:
    from ...storage import db
    with db._connect() as conn:
        r = conn.execute("SELECT * FROM br_live_guard WHERE account_id = ? ORDER BY id DESC LIMIT 1",
                         (account_id,)).fetchone()
    if r is None:
        return {"live_flag": False, "capital_cap_usd": None, "updated_at": None, "updated_by": None, "reason": None}
    return {"live_flag": bool(r["live_flag"]), "capital_cap_usd": r["capital_cap_usd"], "updated_at": r["ts"],
            "updated_by": r["actor"], "reason": r["reason"]}


def validate_cap(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)) \
            or not CAP_MIN <= float(v) <= CAP_MAX:
        raise ValueError(f"capital_cap_usd must be null or {CAP_MIN:,.0f}..{CAP_MAX:,.0f}")
    return float(v)


def set_settings(account_id: str, live_flag: bool, capital_cap_usd, reason: str, actor: str = "user") -> dict:
    from ...storage import db
    if not isinstance(live_flag, bool):
        raise ValueError("live_flag must be true or false")
    cap = validate_cap(capital_cap_usd)
    ts = db.utc_now_iso()
    with db._connect() as conn:
        conn.execute("INSERT INTO br_live_guard (account_id, ts, live_flag, capital_cap_usd, actor, reason) VALUES"
                     " (?, ?, ?, ?, ?, ?)", (account_id, ts, 1 if live_flag else 0, cap, actor, reason))
        conn.execute("INSERT INTO audit (ts, actor, action, detail_json) VALUES (?, ?, 'live_guard_settings', ?)",
                     (ts, actor, json.dumps({"account_id": account_id, "live_flag": live_flag,
                                             "capital_cap_usd": cap, "reason": reason,
                                             "build_allows_live": BUILD_ALLOWS_LIVE})))
    return settings(account_id)


# --- evaluation -------------------------------------------------------------------------------------------

def evaluate(account_id: str, *, guard: dict, confirm: Optional[str] = None, strategy_state: Optional[str] = None,
             per_request: bool = True) -> dict:
    """Every condition with ok/message; ``allowed`` is ALWAYS False in this build."""
    conds: List[dict] = []
    flag = bool(guard.get("live_flag"))
    conds.append({"id": "live_flag", "label": CONDITIONS[0][1], "ok": flag,
                  "message": None if flag else "the live flag for this broker is off (Settings → Brokers)"})
    phrase = confirmation_phrase(account_id)
    if per_request:
        ok = isinstance(confirm, str) and confirm.strip() == phrase
        conds.append({"id": "typed_confirmation", "label": CONDITIONS[1][1], "ok": ok,
                      "message": None if ok else f"type exactly: {phrase}"})
        ok = strategy_state == "live"
        conds.append({"id": "strategy_live", "label": CONDITIONS[2][1], "ok": ok,
                      "message": None if ok else f"the strategy is {strategy_state or 'unknown'} — it must be in "
                                                 "state Live"})
    else:
        conds.append({"id": "typed_confirmation", "label": CONDITIONS[1][1], "ok": None,
                      "message": "checked per request"})
        conds.append({"id": "strategy_live", "label": CONDITIONS[2][1], "ok": None, "message": "checked per request"})
    cap = guard.get("capital_cap_usd")
    ok = cap is not None and CAP_MIN <= float(cap) <= CAP_MAX
    conds.append({"id": "capital_cap", "label": CONDITIONS[3][1], "ok": ok,
                  "message": None if ok else f"set a capital cap ({CAP_MIN:,.0f}..{CAP_MAX:,.0f} USD)"})
    failed = [c["id"] for c in conds if c["ok"] is False]
    all_met = not failed and all(c["ok"] for c in conds)
    allowed = bool(all_met and BUILD_ALLOWS_LIVE)
    return {"allowed": allowed, "failed": failed, "conditions": conds, "all_conditions_met": all_met,
            "refusal": None if allowed else LIVE_DISABLED}


def check(account_id: str, *, strategy_id: Optional[str], confirm: Optional[str], actor: str = "user") -> dict:
    """POST …/live-guard/check: evaluates against the stored settings and the strategy's state; audited."""
    from ...storage import db
    state = None
    if strategy_id:
        row = db.strategy_get(strategy_id)
        state = row["state"] if row else None
    out = evaluate(account_id, guard=settings(account_id), confirm=confirm, strategy_state=state)
    try:
        db.add_audit(actor, "live_guard_check", {"account_id": account_id, "strategy_id": strategy_id,
                                                 "failed": out["failed"], "allowed": out["allowed"]})
    except Exception:          # noqa: BLE001 — the answer is a refusal either way
        pass
    return {"allowed": out["allowed"], "failed": out["failed"], "conditions": out["conditions"],
            "refusal": out["refusal"]}


def status(account_id: str) -> dict:
    g = settings(account_id)
    ev = evaluate(account_id, guard=g, per_request=False)
    return {"id": account_id, "build_allows_live": BUILD_ALLOWS_LIVE, "message": LIVE_DISABLED, "settings": g,
            "confirmation_phrase": confirmation_phrase(account_id), "conditions": ev["conditions"]}


def require_paper_order(account_id: str, order: dict) -> None:
    """Called by every real-broker adapter before a send: an order flagged live is refused."""
    if order.get("live") or order.get("mode") == "live":
        raise LiveTradingDisabled(f"{account_id}: an order was flagged live")
