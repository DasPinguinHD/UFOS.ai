"""Council of AIs params (SP §22.4) — ONE source of truth per value.

- ``COUNCIL_KEYS`` live in hedgefund.db ``cl_params`` (versioned, insert-only; copied ONCE from the latest old
  ``fund_params`` version at the switch-over, D3). Same limits as the old ``/fund/params`` (``core.params.LIMITS``),
  except ``interval_minutes``: 5 … 60, multiples of 5.
- ``BROKER_KEYS`` are ``paper-council``'s broker params (migrated by SP §17.9) overlaid with the latest
  ``cl_broker_params`` override (D9; same bounds as ``/fund/params`` today).
- ``drawdown_stop_pct`` is NOT a Council param: the risk agent's "drawdown ≥ 75 % of the stop" rule reads
  ``pf_settings.drawdown_kill_pct`` of ``paper-council`` (the post-trade kill limit) — ``agent_view`` passes it in.
- ``autopilot_heartbeat_minutes`` → the platform setting ``autotrade.heartbeat_minutes`` (``execution/control.py``);
  ``closed_interval_minutes`` / ``crypto_risk_watch`` / ``starting_capital`` are dropped.
"""
from __future__ import annotations

import copy
import math
from typing import Any, Dict, List, Optional, Tuple

from strategy_platform.core import params as P

COUNCIL_KEYS = ("watchlist", "interval_minutes", "min_confidence", "max_position_pct", "crypto_max_position_pct",
                "max_trades_per_day", "agent_weights", "risk_enabled", "judge_model_mode", "change_threshold",
                "judge_stale_hours", "min_hold_days", "ai_daily_budget_usd")
BROKER_KEYS = ("fees_bps", "crypto_fees_bps", "allow_shorts", "not_shortable", "short_exposure_max_pct",
               "initial_margin_pct", "maintenance_margin_pct", "borrow_rate_default_pct", "borrow_rate_stock_pct",
               "borrow_rate_overrides", "crypto_initial_margin_pct", "crypto_maintenance_margin_pct",
               "crypto_funding_rate_pct_8h", "crypto_liquidation_fee_pct")
DROPPED_KEYS = ("drawdown_stop_pct", "autopilot_heartbeat_minutes", "closed_interval_minutes", "crypto_risk_watch",
                "starting_capital")
INTERVAL_MIN, INTERVAL_MAX, INTERVAL_STEP = 5, 60, 5


def limits() -> Dict[str, Dict[str, Any]]:
    out = {k: copy.deepcopy(P.LIMITS[k]) for k in COUNCIL_KEYS}
    out["interval_minutes"].update(min=INTERVAL_MIN, max=INTERVAL_MAX, step=INTERVAL_STEP,
                                   description="Council cycles run only in the US session, after the opening grace "
                                               "(multiples of 5 min).")
    return out


def broker_limits() -> Dict[str, Dict[str, Any]]:
    return {k: copy.deepcopy(P.LIMITS[k]) for k in BROKER_KEYS}


def defaults() -> Dict[str, Any]:
    d = P.defaults()
    out = {k: copy.deepcopy(d[k]) for k in COUNCIL_KEYS}
    out["interval_minutes"] = _round_interval(out["interval_minutes"])[0]
    return out


def _round_interval(v) -> Tuple[int, Optional[str]]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return INTERVAL_MIN, f"interval_minutes {v!r} unreadable → {INTERVAL_MIN}"
    if not math.isfinite(f):
        return INTERVAL_MIN, f"interval_minutes {v!r} not finite → {INTERVAL_MIN}"
    n = int(math.ceil(f / INTERVAL_STEP) * INTERVAL_STEP)
    n = max(INTERVAL_MIN, min(INTERVAL_MAX, n))
    return n, (None if n == f else f"interval_minutes {f:g} → {n} (multiples of {INTERVAL_STEP}, {INTERVAL_MIN} … "
                                    f"{INTERVAL_MAX}; rounded up)")


def _params_dict(old: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Refuses the stored version ROW ({"version", "params", …}) — it would silently copy the defaults (found by the
    parity harness). Callers pass ``fund_store.effective_params()[1]``."""
    if isinstance(old, dict) and isinstance(old.get("params"), dict) and not set(old) & set(P.DEFAULTS):
        raise ValueError("expected the old params dict (fund_store.effective_params()[1]), not the stored version row")
    return old or {}


def from_old(old: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Any], List[str]]:
    """The Council params copied from an old ``fund_params`` dict (D3) + notes for the copy row."""
    old = _params_dict(old)
    merged = P.merge(None, old)
    out = {k: copy.deepcopy(merged[k]) for k in COUNCIL_KEYS}
    notes: List[str] = []
    out["interval_minutes"], note = _round_interval(merged.get("interval_minutes"))
    if note:
        notes.append(note)
    dropped = sorted(k for k in (old or {}) if k in DROPPED_KEYS)
    if dropped:
        notes.append("not copied (other owner / dropped, SP §22.4): " + ", ".join(dropped))
    return out, notes


def broker_from_old(old: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    merged = P.merge(None, _params_dict(old))
    return {k: copy.deepcopy(merged[k]) for k in BROKER_KEYS}


def _check_interval(clean: Dict[str, Any], errors: Dict[str, str]) -> None:
    if "interval_minutes" in clean:
        v = clean["interval_minutes"]
        if not (INTERVAL_MIN <= v <= INTERVAL_MAX) or int(v) != v or int(v) % INTERVAL_STEP:
            errors["interval_minutes"] = (f"must be {INTERVAL_MIN} … {INTERVAL_MAX} minutes in steps of "
                                          f"{INTERVAL_STEP}")


def validate(partial, current: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """A partial Council params update → (clean, errors). Unknown / non-Council keys are errors (strict)."""
    if not isinstance(partial, dict):
        return {}, {"_": "parameters must be a JSON object"}
    errors = {str(k)[:40]: ("not a Council parameter (SP §22.4)" if k in P.DEFAULTS else "unknown parameter")
              for k in partial if k not in COUNCIL_KEYS}
    body = {k: v for k, v in partial.items() if k in COUNCIL_KEYS}
    if "interval_minutes" in body and isinstance(body["interval_minutes"], (int, float)) and \
            not isinstance(body["interval_minutes"], bool):
        lim = P.LIMITS["interval_minutes"]
        if body["interval_minutes"] > lim["max"] or body["interval_minutes"] < lim["min"]:
            errors["interval_minutes"] = (f"must be {INTERVAL_MIN} … {INTERVAL_MAX} minutes in steps of "
                                          f"{INTERVAL_STEP}")
            body.pop("interval_minutes")
    clean, errs = P.validate(body, P.merge(None, current or {}))
    errors.update(errs)
    if not errors:
        _check_interval(clean, errors)
    return (clean if not errors else {}), errors


def merge(current: Optional[Dict[str, Any]], partial: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    full = P.merge(None, current or {})
    full = P.merge(full, partial or {})
    return {k: full[k] for k in COUNCIL_KEYS}


def validate_broker(partial, current: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """A partial broker-params override (D9) → (clean, errors); cross-field margin rules against the EFFECTIVE
    values (``current``)."""
    if not isinstance(partial, dict):
        return {}, {"_": "parameters must be a JSON object"}
    errors = {str(k)[:40]: "not a broker parameter of paper-council (SP §22.4)" for k in partial
              if k not in BROKER_KEYS}
    clean, errs = P.validate({k: v for k, v in partial.items() if k in BROKER_KEYS}, P.merge(None, current))
    errors.update(errs)
    return (clean if not errors else {}), errors


def effective_broker(migrated: Optional[Dict[str, Any]], override: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """paper-council's broker params: the migrated values (§17.9) with the latest override (D9) on top."""
    full = P.merge(None, migrated or {})
    full = P.merge(full, {k: v for k, v in (override or {}).items() if k in BROKER_KEYS})
    return {k: full[k] for k in BROKER_KEYS}


def agent_view(council: Dict[str, Any], broker: Dict[str, Any], drawdown_kill_pct: float) -> Dict[str, Any]:
    """The full params dict the moved agents / judge / sizing read (their code is unchanged): the Council params,
    the EFFECTIVE broker params and ``drawdown_stop_pct`` = ``pf_settings.drawdown_kill_pct`` (one source each)."""
    out = P.merge(None, {**broker, **council})
    out["drawdown_stop_pct"] = float(drawdown_kill_pct)
    return out
