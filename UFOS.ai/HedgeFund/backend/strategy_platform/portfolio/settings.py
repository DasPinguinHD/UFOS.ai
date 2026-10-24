"""Per-account trading settings (SPEC §17.6, §17.11): pre-trade checks, post-trade
limits, allocator parameters. Stored insert-only in ``pf_settings`` (the newest row
wins; every change has an actor, a reason and an audit row). Bounds are enforced —
an out-of-range or unknown key is a 400 field error, never clamped.

Part of UFOS.ai, a proof-of-concept portfolio project; paper trading only.
"""
from __future__ import annotations

import json
import math
from typing import Dict, Optional, Tuple

from ..storage import db

# key: (default, min, max, step, unit, label, group)
SPEC = {
    "max_order_notional_pct": (50.0, 1.0, 100.0, 1.0, "%", "Max order notional (% of account equity)", "pre_trade"),
    "max_order_notional_usd": (1_000_000.0, 100.0, 10_000_000.0, 100.0, "USD", "Max order notional (USD)",
                               "pre_trade"),
    # SPEC §18.14: the same cap for an account whose money is USDT (binance-demo) — its own setting, never converted
    "max_order_notional_usdt": (1_000_000.0, 100.0, 10_000_000.0, 100.0, "USDT", "Max order notional (USDT)",
                                "pre_trade"),
    "max_position_pct_equity": (50.0, 1.0, 100.0, 1.0, "%", "Max position per equity/ETF (% of equity)",
                                "pre_trade"),
    "max_position_pct_crypto": (40.0, 1.0, 100.0, 1.0, "%", "Max position per crypto asset (% of equity)",
                                "pre_trade"),
    "price_collar_pct_equity": (5.0, 0.5, 50.0, 0.5, "%", "Price collar vs the reference price (equities)",
                                "pre_trade"),
    "price_collar_pct_crypto": (10.0, 0.5, 50.0, 0.5, "%", "Price collar vs the reference price (crypto)",
                                "pre_trade"),
    "duplicate_window_seconds": (60, 10, 3600, 10, "s", "Duplicate-order window", "pre_trade"),
    "max_orders_per_day": (100, 1, 500, 1, "orders", "Max orders per day (per class)", "pre_trade"),
    "daily_loss_pct": (2.0, 0.5, 20.0, 0.1, "%", "Daily loss limit (no new orders until the next day)",
                       "post_trade"),
    "drawdown_kill_pct": (10.0, 2.0, 50.0, 0.5, "%", "Drawdown from peak → kill switch", "post_trade"),
    "alloc_max_weight": (0.4, 0.05, 1.0, 0.01, "fraction", "Max weight per strategy", "allocator"),
    "alloc_min_weight": (0.05, 0.0, 0.5, 0.01, "fraction", "Min weight per strategy (below → 0)", "allocator"),
    "alloc_max_total": (1.0, 0.1, 1.0, 0.01, "fraction", "Max total allocation of the pool", "allocator"),
    "alloc_lambda": (1.0, 0.0, 5.0, 0.1, "", "Correlation penalty λ", "allocator"),
    "alloc_drift_band": (0.05, 0.0, 0.5, 0.01, "fraction", "Rebalance drift band (of the pool)", "allocator"),
}
INT_KEYS = ("duplicate_window_seconds", "max_orders_per_day")
# the order-notional cap per account currency (SPEC §18.14): an account sees only the key of its own money
NOTIONAL_KEY = {"USD": "max_order_notional_usd", "USDT": "max_order_notional_usdt"}
USDT_ACCOUNTS = ("binance-demo",)


def account_currency(account_id) -> str:
    return "USDT" if account_id in USDT_ACCOUNTS else "USD"


def _hidden(account_id) -> set:
    if account_id is None:
        return set()
    own = NOTIONAL_KEY[account_currency(account_id)]
    return {k for k in NOTIONAL_KEY.values() if k != own}
REASON_MIN, REASON_MAX = 3, 500


def defaults(account_id=None) -> dict:
    hide = _hidden(account_id)
    return {k: v[0] for k, v in SPEC.items() if k not in hide}


def bounds(account_id=None) -> dict:
    hide = _hidden(account_id)
    return {k: {"min": v[1], "max": v[2], "step": v[3], "unit": v[4], "label": v[5], "group": v[6]}
            for k, v in SPEC.items() if k not in hide}


def validate(changes, account_id=None) -> Tuple[dict, Dict[str, str]]:
    """(clean changes, errors) — unknown keys, non-numbers and out-of-bounds values are errors; with ``account_id``
    the notional cap of the other currency is an error too (SPEC §18.14)."""
    errors: Dict[str, str] = {}
    clean: dict = {}
    if not isinstance(changes, dict) or not changes:
        return {}, {"changes": "an object with at least one setting"}
    hide = _hidden(account_id)
    for k, v in changes.items():
        if k not in SPEC:
            errors[str(k)[:60]] = "unknown setting"
            continue
        if k in hide:
            errors[k] = (f"not used by {account_id} — its money is {account_currency(account_id)} "
                         f"(use {NOTIONAL_KEY[account_currency(account_id)]})")
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
            errors[k] = "must be a JSON number"
            continue
        lo, hi = SPEC[k][1], SPEC[k][2]
        if not lo <= float(v) <= hi:
            errors[k] = f"must be between {lo:g} and {hi:g}"
            continue
        if k in INT_KEYS and not float(v).is_integer():
            errors[k] = "must be a whole number"
            continue
        clean[k] = int(v) if k in INT_KEYS else float(v)
    if not errors and "alloc_min_weight" in clean and "alloc_max_weight" in clean \
            and clean["alloc_min_weight"] > clean["alloc_max_weight"]:
        errors["alloc_min_weight"] = "must not exceed alloc_max_weight"
    return clean, errors


def _latest_row(account_id: str) -> Optional[dict]:
    with db._connect() as conn:
        r = conn.execute("SELECT id, created_at, settings_json, actor, reason FROM pf_settings WHERE account_id = ?"
                         " ORDER BY id DESC LIMIT 1", (account_id,)).fetchone()
    return dict(r) if r else None


def get(account_id: str) -> dict:
    """The effective settings payload (§17.11 Settings)."""
    row = _latest_row(account_id)
    settings = defaults()
    version = 0
    if row is not None:
        stored = json.loads(row["settings_json"])
        settings.update({k: v for k, v in stored.items() if k in SPEC})
        with db._connect() as conn:
            version = conn.execute("SELECT COUNT(*) FROM pf_settings WHERE account_id = ?", (account_id,)).fetchone()[0]
    hide = _hidden(account_id)
    return {"account_id": account_id, "version": int(version), "updated_at": row["created_at"] if row else None,
            "updated_by": row["actor"] if row else None, "settings": {k: v for k, v in settings.items() if k not in hide},
            "defaults": defaults(account_id), "bounds": bounds(account_id),
            "currency": account_currency(account_id)}


def values(account_id: str) -> dict:
    return get(account_id)["settings"]


def write(account_id: str, settings: dict, actor: str, reason: Optional[str], conn=None) -> None:
    """Stores a full settings row (+ audit). ``conn``: join an outer transaction."""
    row = (account_id, db.utc_now_iso(), json.dumps(settings, sort_keys=True), actor, reason)
    sql = "INSERT INTO pf_settings (account_id, created_at, settings_json, actor, reason) VALUES (?, ?, ?, ?, ?)"
    audit = ("INSERT INTO audit (ts, actor, action, detail_json) VALUES (?, ?, 'settings_changed', ?)",
             (db.utc_now_iso(), actor, json.dumps({"account_id": account_id, "settings": settings, "reason": reason},
                                                  sort_keys=True)))
    if conn is not None:
        conn.execute(sql, row)
        conn.execute(*audit)
        return
    with db._connect() as c:
        c.execute(sql, row)
        c.execute(*audit)


def put(account_id: str, changes: dict, reason: str, actor: str = "user") -> dict:
    """Validated partial update (callers validate first; ValueError otherwise)."""
    clean, errors = validate(changes, account_id)
    if errors:
        raise ValueError(json.dumps(errors))
    current = values(account_id)
    merged = dict(current, **clean)
    if merged["alloc_min_weight"] > merged["alloc_max_weight"]:
        raise ValueError(json.dumps({"alloc_min_weight": "must not exceed alloc_max_weight"}))
    write(account_id, merged, actor, reason)
    return get(account_id)
