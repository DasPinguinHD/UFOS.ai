"""Parameters of the HedgeFund loop: defaults, UI limits, validation, merge
(fund/SPEC.md §3).

Proof of concept, paper trading only — nothing here is investment advice.
Parameters are stored versioned in the ``fund_params`` table (fund/store.py);
a cycle reads the latest version once at its start. Percentages are stored as
the UI shows them (5.0 = 5 %). Stdlib only.

Asset classes (SPEC §3): symbols matching ``CRYPTO_RE`` (``BTC-USD``) are
crypto — traded 24/7, fractional quantities, spot longs and shorts simulated
as perpetual futures with their own ``crypto_*`` parameters; everything else
is an equity/ETF (NYSE session, whole shares, Reg T shorts).
"""
from __future__ import annotations

import copy
import math
import re
from typing import Any, Dict, List, Optional, Tuple

DEFAULTS: Dict[str, Any] = {
    "watchlist": ["SPY", "QQQ", "IWM", "TLT", "GLD", "USO", "XLE", "XLF", "BTC-USD", "ETH-USD", "SOL-USD"],
    "interval_minutes": 5,                  # 1..60, scheduled full cycles during the US regular session
    # 5..240: interval of the crypto "risk watch" outside the US session (no
    # agents/LLM/new orders; SPEC §10). Outside the session there are no
    # scheduled full cycles — only "Run now".
    "closed_interval_minutes": 30,
    "min_confidence": 75,                   # %, 50..95
    "max_position_pct": 5.0,                # % of equity per symbol, 1..15
    "max_trades_per_day": 6,                # autopilot orders per NY day, 1..20
    "drawdown_stop_pct": 8.0,               # % from peak equity, 2..20
    "agent_weights": {"technical": 1.0, "macro": 1.0, "sentiment": 0.5},  # 0..1 each, 0 = off
    "risk_enabled": True,
    "judge_model_mode": "standard",         # standard|premium|premium_on_flip
    "ai_daily_budget_usd": 1.0,             # 0..50; 0 = never call the LLM
    "fees_bps": 5.0,                        # commission+slippage per fill, 0..50
    "allow_shorts": True,
    "short_exposure_max_pct": 30.0,         # gross short MV / equity, 0..100
    "initial_margin_pct": 50.0,             # Reg T, 25..100
    "maintenance_margin_pct": 30.0,         # 25..100, <= initial
    "borrow_rate_default_pct": 0.3,         # annual, easy-to-borrow ETFs
    "borrow_rate_stock_pct": 3.0,           # annual, default for non-ETF symbols
    "borrow_rate_overrides": {},            # {"SYMBOL": pct}
    "not_shortable": [],                    # symbols without a locate
    "min_hold_days": 1,                     # 0..10, NY trading days
    "change_threshold": 0.15,               # agent score change that re-triggers the judge, 0.01..1
    "judge_stale_hours": 6,                 # re-judge after this many hours anyway, 1..48
    "starting_capital": 100000.0,           # used only by POST /fund/reset
    # Guardrail (dead-man switch): the autopilot switches itself off when no
    # client (the WPF window) has polled /fund/state or /fund/events for this
    # many minutes, 5..240. A future truly headless mode would replace this
    # with an explicit opt-in.
    "autopilot_heartbeat_minutes": 15,
    # --- crypto (24/7; SPEC §3/§6) ---
    "crypto_fees_bps": 20.0,                # per fill, half slippage in the price + half fee, 0..100
    "crypto_max_position_pct": 3.0,         # % of equity per crypto symbol, 0.5..15
    "crypto_initial_margin_pct": 50.0,      # perp shorts: isolated initial margin, 10..100
    "crypto_maintenance_margin_pct": 25.0,  # perp shorts: liquidation below this, 5..100, < initial
    "crypto_funding_rate_pct_8h": 0.01,     # perp funding per 8 h window, -0.5..0.5; + = longs pay shorts
    "crypto_liquidation_fee_pct": 0.5,      # extra fee of the notional on a forced perp cover, 0..5
    "crypto_risk_watch": True,              # light risk watch outside the US session (SPEC §10)
}

AGENT_WEIGHT_KEYS = ("technical", "macro", "sentiment")
JUDGE_MODEL_MODES = ("standard", "premium", "premium_on_flip")
WATCHLIST_MAX = 25
SYMBOL_LIST_MAX = 100           # not_shortable / borrow_rate_overrides
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")        # equities / ETFs
CRYPTO_RE = re.compile(r"^[A-Z0-9]{2,10}-USD$")            # crypto vs USD (Yahoo naming)
WATCHLIST_PATTERN = r"^(?:[A-Z][A-Z0-9.\-]{0,9}|[A-Z0-9]{2,10}-USD)$"
# Well-known crypto symbols (documentation / UI hints only — detection is by CRYPTO_RE).
KNOWN_CRYPTO = ("BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "ADA-USD", "DOGE-USD", "AVAX-USD", "LINK-USD",
                "DOT-USD", "LTC-USD", "BCH-USD")
CRYPTO_ROLLOUT_SYMBOLS = ("BTC-USD", "ETH-USD", "SOL-USD")
CRYPTO_QTY_DECIMALS = 8                 # fractional crypto quantities are rounded DOWN to 8 decimals
CRYPTO_MIN_NOTIONAL = 10.0              # USD; smaller crypto orders are rejected ("size below minimum")

# Served to the UI (GET /fund/params). type: int|float|bool|enum|symbols|weights|rate_map.
LIMITS: Dict[str, Dict[str, Any]] = {
    "watchlist": {"type": "symbols", "min_items": 1, "max_items": WATCHLIST_MAX, "pattern": WATCHLIST_PATTERN},
    "interval_minutes": {"type": "int", "min": 1, "max": 60, "step": 1, "unit": "min",
                         "label": "Cycle interval (US session)",
                         "description": "Scheduled full cycles run only during the US regular session."},
    "closed_interval_minutes": {"type": "int", "min": 5, "max": 240, "step": 5, "unit": "min",
                                "label": "Risk-watch interval (outside the US session)",
                                "description": "Outside the US session there are no scheduled full cycles "
                                               "(use Run now). While crypto positions are open, a light risk "
                                               "watch (mark to market, funding, liquidation and drawdown checks; "
                                               "no agents, no AI, no new orders) runs at this interval."},
    "min_confidence": {"type": "int", "min": 50, "max": 95, "step": 1, "unit": "%"},
    "max_position_pct": {"type": "float", "min": 1.0, "max": 15.0, "step": 0.5, "unit": "%"},
    "max_trades_per_day": {"type": "int", "min": 1, "max": 20, "step": 1},
    "drawdown_stop_pct": {"type": "float", "min": 2.0, "max": 20.0, "step": 0.5, "unit": "%"},
    "agent_weights": {"type": "weights", "keys": list(AGENT_WEIGHT_KEYS), "min": 0.0, "max": 1.0, "step": 0.05},
    "risk_enabled": {"type": "bool"},
    "judge_model_mode": {"type": "enum", "options": list(JUDGE_MODEL_MODES)},
    "ai_daily_budget_usd": {"type": "float", "min": 0.0, "max": 50.0, "step": 0.1, "unit": "USD"},
    "fees_bps": {"type": "float", "min": 0.0, "max": 50.0, "step": 0.5, "unit": "bps"},
    "allow_shorts": {"type": "bool"},
    "short_exposure_max_pct": {"type": "float", "min": 0.0, "max": 100.0, "step": 1.0, "unit": "%"},
    "initial_margin_pct": {"type": "float", "min": 25.0, "max": 100.0, "step": 1.0, "unit": "%"},
    "maintenance_margin_pct": {"type": "float", "min": 25.0, "max": 100.0, "step": 1.0, "unit": "%"},
    "borrow_rate_default_pct": {"type": "float", "min": 0.0, "max": 100.0, "step": 0.1, "unit": "%/yr"},
    "borrow_rate_stock_pct": {"type": "float", "min": 0.0, "max": 100.0, "step": 0.1, "unit": "%/yr"},
    "borrow_rate_overrides": {"type": "rate_map", "min": 0.0, "max": 100.0, "step": 0.1,
                              "max_items": SYMBOL_LIST_MAX, "unit": "%/yr"},
    "not_shortable": {"type": "symbols", "min_items": 0, "max_items": SYMBOL_LIST_MAX, "pattern": WATCHLIST_PATTERN},
    "min_hold_days": {"type": "int", "min": 0, "max": 10, "step": 1, "unit": "trading days"},
    "change_threshold": {"type": "float", "min": 0.01, "max": 1.0, "step": 0.01},
    "judge_stale_hours": {"type": "int", "min": 1, "max": 48, "step": 1, "unit": "h"},
    "starting_capital": {"type": "float", "min": 1000.0, "max": 10000000.0, "step": 1000.0, "unit": "USD"},
    "autopilot_heartbeat_minutes": {"type": "int", "min": 5, "max": 240, "step": 5, "unit": "min"},
    "crypto_fees_bps": {"type": "float", "min": 0.0, "max": 100.0, "step": 1.0, "unit": "bps",
                        "label": "Crypto fees", "description": "Per crypto fill: half slippage, half fee."},
    "crypto_max_position_pct": {"type": "float", "min": 0.5, "max": 15.0, "step": 0.5, "unit": "%",
                                "label": "Crypto max position", "description": "% of equity per crypto symbol."},
    "crypto_initial_margin_pct": {"type": "float", "min": 10.0, "max": 100.0, "step": 1.0, "unit": "%",
                                  "label": "Crypto perp initial margin",
                                  "description": "Isolated margin posted for a crypto short (perpetual future)."},
    "crypto_maintenance_margin_pct": {"type": "float", "min": 5.0, "max": 100.0, "step": 1.0, "unit": "%",
                                      "label": "Crypto perp maintenance margin",
                                      "description": "Below it the perp short is liquidated (forced cover plus "
                                                     "the liquidation fee); must be below the initial margin."},
    "crypto_funding_rate_pct_8h": {"type": "float", "min": -0.5, "max": 0.5, "step": 0.005, "unit": "%/8h",
                                   "label": "Crypto funding rate",
                                   "description": "Paid every 8 h (00/08/16 UTC) on perp shorts. Positive = "
                                                  "longs pay shorts, so the simulated shorts receive it."},
    "crypto_liquidation_fee_pct": {"type": "float", "min": 0.0, "max": 5.0, "step": 0.1, "unit": "%",
                                   "label": "Crypto liquidation fee",
                                   "description": "Extra fee of the notional on a forced perp cover."},
    "crypto_risk_watch": {"type": "bool", "label": "Crypto risk watch",
                          "description": "Outside the US session: mark to market, funding, liquidation and "
                                         "drawdown checks every closed_interval_minutes while crypto positions "
                                         "are open (no agents, no AI, no new orders)."},
}

# Known (easy-to-borrow) ETFs -> borrow_rate_default_pct; everything else is a
# "stock" with borrow_rate_stock_pct unless overridden.
ETF_SYMBOLS = frozenset(
    "SPY QQQ IWM DIA TLT IEF SHY GLD SLV USO UNG XLE XLF XLK XLV XLI XLY XLP XLU XLB XLRE XLC "
    "EEM EFA HYG LQD UUP VNQ".split())


def defaults() -> Dict[str, Any]:
    """A deep copy of DEFAULTS (safe to mutate)."""
    return copy.deepcopy(DEFAULTS)


def normalise_symbol(value) -> Optional[str]:
    """'spy ' -> 'SPY', 'btc-usd' -> 'BTC-USD'; None when the value is not a
    valid equity ticker or crypto symbol."""
    if not isinstance(value, str):
        return None
    sym = value.strip().upper()
    return sym if (SYMBOL_RE.match(sym) or CRYPTO_RE.match(sym)) else None


def is_crypto(symbol) -> bool:
    """True for crypto symbols (``^[A-Z0-9]{2,10}-USD$``, e.g. BTC-USD)."""
    return isinstance(symbol, str) and bool(CRYPTO_RE.match(symbol.strip().upper()))


def asset_type(symbol) -> str:
    """"crypto" | "equity" (equities and ETFs) — the trading class of a symbol."""
    return "crypto" if is_crypto(symbol) else "equity"


def fees_bps_for(symbol, params: Dict[str, Any]) -> float:
    """Fees per fill in bps for the symbol's class (crypto_fees_bps / fees_bps)."""
    key = "crypto_fees_bps" if is_crypto(symbol) else "fees_bps"
    v = (params or {}).get(key)
    return float(DEFAULTS[key] if v is None else v)


def max_position_pct_for(symbol, params: Dict[str, Any]) -> float:
    """Max position in % of equity for the symbol's class."""
    key = "crypto_max_position_pct" if is_crypto(symbol) else "max_position_pct"
    v = (params or {}).get(key)
    return float(DEFAULTS[key] if v is None else v)


def margin_pcts_for(symbol, params: Dict[str, Any]) -> Tuple[float, float]:
    """(initial, maintenance) margin in % for a short in the symbol's class
    (crypto perps vs Reg T equity shorts)."""
    p = params or {}
    if is_crypto(symbol):
        keys = ("crypto_initial_margin_pct", "crypto_maintenance_margin_pct")
    else:
        keys = ("initial_margin_pct", "maintenance_margin_pct")
    return tuple(float(DEFAULTS[k] if p.get(k) is None else p.get(k)) for k in keys)


def normalise_symbols(values) -> Tuple[List[str], List[str]]:
    """Upper-cases, validates and de-duplicates (first occurrence wins, order
    kept). Returns (symbols, invalid_inputs)."""
    out: List[str] = []
    bad: List[str] = []
    if not isinstance(values, (list, tuple)):
        return out, [repr(values)]
    for v in values:
        sym = normalise_symbol(v)
        if sym is None:
            bad.append(str(v)[:20])
        elif sym not in out:
            out.append(sym)
    return out, bad


def normalise_watchlist(values) -> Tuple[List[str], Optional[str]]:
    """Watchlist rule: 1..25 valid symbols after de-duplication.
    Returns (symbols, error_message_or_None)."""
    syms, bad = normalise_symbols(values)
    if bad:
        return syms, "invalid symbol(s): " + ", ".join(bad[:5])
    if not 1 <= len(syms) <= WATCHLIST_MAX:
        return syms, f"watchlist needs 1..{WATCHLIST_MAX} symbols"
    return syms, None


def _number(key: str, value, errors: Dict[str, str]):
    lim = LIMITS[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors[key] = "must be a number"
        return None
    if isinstance(value, float) and not math.isfinite(value):
        errors[key] = "must be a finite number"
        return None
    if lim["type"] == "int":
        if isinstance(value, float) and not value.is_integer():
            errors[key] = "must be a whole number"
            return None
        value = int(value)
    else:
        try:
            value = float(value)
        except OverflowError:          # a huge JSON integer (e.g. 10**400)
            errors[key] = f"must be between {lim['min']} and {lim['max']}"
            return None
    if value < lim["min"] or value > lim["max"]:
        errors[key] = f"must be between {lim['min']} and {lim['max']}"
        return None
    return value


def _finite_or_none(value) -> Optional[float]:
    """float(value) for real numbers (no bool), None for non-numbers, huge
    integers (OverflowError) and NaN/Infinity."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        v = float(value)
    except OverflowError:
        return None
    return v if math.isfinite(v) else None


def _rate(value) -> Optional[float]:
    lim = LIMITS["borrow_rate_overrides"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:              # a huge JSON integer
        return None
    if not math.isfinite(value) or value < lim["min"] or value > lim["max"]:
        return None
    return value


def validate(partial, current: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Validates a partial parameter update (PUT /fund/params body).

    Returns (clean_partial, errors) where errors maps key -> message; the
    caller must reject the whole update when errors is non-empty. Unknown
    keys are errors. Cross-field rule maintenance_margin_pct <=
    initial_margin_pct is checked against ``merge(current, clean)`` (current
    defaults to DEFAULTS). agent_weights may be partial (merged per agent);
    borrow_rate_overrides and the symbol lists replace the old value."""
    errors: Dict[str, str] = {}
    clean: Dict[str, Any] = {}
    if not isinstance(partial, dict):
        return {}, {"_": "parameters must be a JSON object"}
    for key, value in partial.items():
        if key not in DEFAULTS:
            errors[str(key)[:40]] = "unknown parameter"
            continue
        kind = LIMITS[key]["type"]
        if kind in ("int", "float"):
            v = _number(key, value, errors)
            if v is not None:
                clean[key] = v
        elif kind == "bool":
            if isinstance(value, bool):
                clean[key] = value
            else:
                errors[key] = "must be true or false"
        elif kind == "enum":
            if value in LIMITS[key]["options"]:
                clean[key] = value
            else:
                errors[key] = "must be one of " + ", ".join(LIMITS[key]["options"])
        elif key == "watchlist":
            syms, err = normalise_watchlist(value)
            if err:
                errors[key] = err
            else:
                clean[key] = syms
        elif kind == "symbols":
            syms, bad = normalise_symbols(value)
            if bad:
                errors[key] = "invalid symbol(s): " + ", ".join(bad[:5])
            elif len(syms) > LIMITS[key]["max_items"]:
                errors[key] = f"at most {LIMITS[key]['max_items']} symbols"
            else:
                clean[key] = syms
        elif kind == "weights":
            if not isinstance(value, dict):
                errors[key] = "must be an object {agent: weight}"
                continue
            weights = {}
            for agent, w in value.items():
                if agent not in AGENT_WEIGHT_KEYS:
                    errors[key] = f"unknown agent '{str(agent)[:20]}'"
                    break
                wf = _finite_or_none(w)
                if wf is None or not 0.0 <= wf <= 1.0:
                    errors[key] = f"weight of '{str(agent)[:20]}' must be between 0 and 1"
                    break
                weights[agent] = wf
            else:
                clean[key] = weights
        elif kind == "rate_map":
            if not isinstance(value, dict):
                errors[key] = "must be an object {SYMBOL: pct}"
                continue
            if len(value) > LIMITS[key]["max_items"]:
                errors[key] = f"at most {LIMITS[key]['max_items']} symbols"
                continue
            rates = {}
            for sym, rate in value.items():
                s = normalise_symbol(sym)
                r = _rate(rate)
                if s is None or r is None:
                    errors[key] = f"invalid entry '{str(sym)[:20]}' (symbol, rate 0..100 %)"
                    break
                rates[s] = r
            else:
                clean[key] = rates
    if not errors:
        merged = merge(current or DEFAULTS, clean)
        if merged["maintenance_margin_pct"] > merged["initial_margin_pct"]:
            key = "maintenance_margin_pct" if "maintenance_margin_pct" in clean else "initial_margin_pct"
            errors[key] = "maintenance margin must not exceed the initial margin"
        # perps: strictly below — at maintenance == initial a fresh short would start at the
        # liquidation threshold (fees) and the autopilot would churn
        if merged["crypto_maintenance_margin_pct"] >= merged["crypto_initial_margin_pct"]:
            key = "crypto_maintenance_margin_pct" if "crypto_maintenance_margin_pct" in clean \
                else "crypto_initial_margin_pct"
            errors[key] = "crypto maintenance margin must be below the crypto initial margin"
    return (clean if not errors else {}), errors


def merge(current: Optional[Dict[str, Any]], partial: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """New full parameter dict = DEFAULTS <- current <- partial (inputs are not
    mutated). Keys unknown to DEFAULTS are dropped (old stored versions stay
    readable when a parameter is removed; new ones get their default).
    agent_weights is merged per agent; every other key is replaced."""
    out = defaults()
    for src in (current or {}, partial or {}):
        for key, value in src.items():
            if key not in DEFAULTS:
                continue
            if key == "agent_weights" and isinstance(value, dict):
                w = dict(out["agent_weights"])
                w.update({k: v for k, v in value.items() if k in AGENT_WEIGHT_KEYS})
                out[key] = w
            else:
                out[key] = copy.deepcopy(value)
    return out


def is_etf(symbol: str) -> bool:
    return str(symbol).upper() in ETF_SYMBOLS


def borrow_rate_for(symbol: str, params: Dict[str, Any]) -> float:
    """Annual borrow rate in % for a short in ``symbol``:
    override -> known ETF default -> stock default. Crypto: 0 (shorts are
    perpetual futures — no borrow, funding instead)."""
    sym = str(symbol).upper()
    if is_crypto(sym):
        return 0.0
    overrides = params.get("borrow_rate_overrides") or {}
    if sym in overrides:
        return float(overrides[sym])
    if sym in ETF_SYMBOLS:
        return float(params.get("borrow_rate_default_pct", DEFAULTS["borrow_rate_default_pct"]))
    return float(params.get("borrow_rate_stock_pct", DEFAULTS["borrow_rate_stock_pct"]))
