"""Macro agent (rules) — fund/SPEC.md §7.

``evaluate(ctx, symbol) -> AgentSignal`` maps the symbol to an asset class
(``ASSET_CLASS`` / ``asset_class()``) and scores it from the newsroom's macro
regime (growth × inflation momentum quadrant, ``newsroom.macro_derived``) and
a few stress readings. Paper-trading proof of concept — historical
tendencies, not a forecast and not investment advice.

``ctx.macro`` shape (built by ``build_macro_context(macro_latest)`` from
main.py's ``macro_latest`` dict "macro-<SERIES>" -> newsroom event, and by
the backtest from its monthly history)::

    {
      "available": bool,                 # False = no macro data at all
      "as_of": "YYYY-MM-DD" | None,      # newest reference date of the inputs
      "regime": None | {
          "quadrant": "goldilocks" | "reflation" | "stagflation" | "slowdown",
          "label": str, "date": "YYYY-MM-01",   # reference month of the reading
          "borderline": bool,            # momentum near 0 -> can flip next release
          "months_in_regime": int, "x": float | None, "y": float | None},
      "readings": {                      # only the keys that exist
          KEY: {"value": float, "date": "YYYY-MM-DD", "monthly": bool, "unit": str}},
    }

KEY is a FRED id — DGS10, DGS2, T10Y2Y, T10Y3M, VIXCLS, BAMLH0A0HYM2 (daily;
HY spread in %), FEDFUNDS, PCEPILFE, CPIAUCSL, UNRATE (monthly) — or a
derived newsroom id: SAHM_RULE, REAL_POLICY_RATE (monthly).

Staleness: a daily reading is stale when its date is > 45 days old. Monthly
data (and the regime) is published with a lag, so its age is counted from the
first day of the month two months after the reference month (e.g. the July
regime is "due" on Sep 1) — stale when that is > 45 days ago.

Scoring: regime tendency of the asset class (``macro_derived.ASSET_TENDENCIES``:
up +0.5, down −0.5, mixed 0; sub-class tweaks for growth/cyclical/defensive
equities and gold) + stress adjustments (10Y ≥ 5 %: −0.2 equities/bonds/credit;
Sahm ≥ 0.5: −0.3 cyclicals, −0.2 other equities/credit, +0.2 govt bonds,
+0.1 gold; 10Y−3M < 0: −0.1 equities; VIX ≥ 30: −0.15 equities/−0.1 credit,
+0.1 govt bonds; HY spread ≥ 5 %: −0.15 credit, −0.1 equities; real policy
rate ≥ 2 pp: −0.1 equities). Confidence 0.6 (+0.1 when the regime has held
≥ 3 months), ×0.6 when borderline, ×0.6 when stale; readings without a regime
→ ≤ 0.3; no data → 0.

Crypto (``params.is_crypto``, asset class "crypto"): the relation to the
macro regime is weak, so it uses the growth-equities tendency scaled ×0.6
with confidence ×0.6, plus two stress adjustments (10Y ≥ 5 %: −0.2; Sahm ≥
0.5: −0.3); no other stress rules.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Dict, Optional

from .. import params as fund_params
from ..types import AgentSignal

AGENT = "macro"
STALE_DAYS = 45

ASSET_CLASS: Dict[str, str] = {
    "SPY": "equities", "QQQ": "equities", "IWM": "equities", "DIA": "equities",
    "XLK": "equities_growth", "XLY": "equities_growth", "XLC": "equities_growth",
    "XLF": "equities_cyclical", "XLE": "equities_cyclical", "XLB": "equities_cyclical", "XLI": "equities_cyclical",
    "XLV": "equities_defensive", "XLP": "equities_defensive", "XLU": "equities_defensive",
    "TLT": "govt_bonds", "IEF": "govt_bonds", "SHY": "govt_bonds",
    "HYG": "credit", "LQD": "credit",
    "GLD": "gold", "SLV": "gold",
    "USO": "commodities", "UNG": "commodities",
    "UUP": "usd",
}
ASSET_CLASS_LABELS = {
    "equities": "equities", "equities_growth": "growth equities", "equities_cyclical": "cyclical equities",
    "equities_defensive": "defensive equities", "govt_bonds": "government bonds", "credit": "credit",
    "gold": "gold", "commodities": "commodities", "usd": "the US dollar", "crypto": "crypto assets",
}
CRYPTO_SCALE = 0.6          # crypto follows the growth-equity tendency only weakly (score and confidence)
# asset class -> column of macro_derived.ASSET_TENDENCIES
_TENDENCY_COLUMN = {
    "equities": "Equities", "equities_growth": "Equities", "equities_cyclical": "Equities",
    "equities_defensive": "Equities", "govt_bonds": "Govt bonds", "credit": "Credit",
    "gold": "Commodities", "commodities": "Commodities", "usd": "USD", "crypto": "Equities",
}
_DIRECTION_SCORE = {"up": 0.5, "down": -0.5, "mixed": 0.0}
# Fallback copy of macro_derived.ASSET_TENDENCIES directions (used only if the
# newsroom module can't be imported).
_FALLBACK_TENDENCIES = {
    "goldilocks": {"Equities": "up", "Govt bonds": "mixed", "Credit": "up", "USD": "mixed", "Commodities": "mixed"},
    "reflation": {"Equities": "up", "Govt bonds": "down", "Credit": "up", "USD": "mixed", "Commodities": "up"},
    "stagflation": {"Equities": "down", "Govt bonds": "down", "Credit": "down", "USD": "mixed", "Commodities": "up"},
    "slowdown": {"Equities": "down", "Govt bonds": "up", "Credit": "down", "USD": "up", "Commodities": "down"},
}
REGIME_LABELS = {"goldilocks": "Goldilocks", "reflation": "Reflation", "stagflation": "Stagflation",
                 "slowdown": "Slowdown (disinflation)"}

DAILY_READINGS = ("DGS10", "DGS2", "T10Y2Y", "T10Y3M", "VIXCLS", "BAMLH0A0HYM2")
MONTHLY_READINGS = ("FEDFUNDS", "PCEPILFE", "CPIAUCSL", "UNRATE")
DERIVED_READINGS = ("SAHM_RULE", "REAL_POLICY_RATE")


def asset_class(symbol: str) -> str:
    """Asset class of a symbol; crypto symbols (BTC-USD) -> "crypto";
    unknown symbols count as broad equities."""
    if fund_params.is_crypto(symbol):
        return "crypto"
    return ASSET_CLASS.get(str(symbol).upper(), "equities")


def tendencies() -> Dict[str, Dict[str, str]]:
    """quadrant -> {column: "up"|"down"|"mixed"} from macro_derived.ASSET_TENDENCIES."""
    try:
        from newsroom.macro_derived import ASSET_TENDENCIES
        out = {}
        for row in ASSET_TENDENCIES.get("rows") or []:
            out[row["quadrant"]] = {c["asset"]: c["direction"] for c in row.get("cells") or []}
        if out:
            return out
    except Exception:
        pass
    return _FALLBACK_TENDENCIES


# --- context --------------------------------------------------------------------

def _reading(event: Optional[dict], monthly: bool) -> Optional[dict]:
    if not isinstance(event, dict):
        return None
    m = event.get("macro") if isinstance(event.get("macro"), dict) else None
    if not m:
        return None
    try:
        value = float(m.get("value"))
    except (TypeError, ValueError):
        return None
    d = str(m.get("date") or "")[:10]
    if not d:
        return None
    return {"value": value, "date": d, "monthly": monthly, "unit": m.get("unit") or ""}


def build_macro_context(macro_latest: Optional[dict]) -> dict:
    """main.py's ``macro_latest`` ("macro-<SERIES>" -> event) -> ``ctx.macro``
    (shape in the module docstring). Never raises; empty input -> available False."""
    readings: Dict[str, dict] = {}
    regime = None
    src = macro_latest or {}
    try:
        for key in DAILY_READINGS:
            r = _reading(src.get(f"macro-{key}"), monthly=False)
            if r:
                readings[key] = r
        for key in MONTHLY_READINGS:
            r = _reading(src.get(f"macro-{key}"), monthly=True)
            if r:
                readings[key] = r
        for key in DERIVED_READINGS:
            r = _reading(src.get(f"macro-derived-{key}"), monthly=True)
            if r:
                readings[key] = r
        ev = src.get("macro-regime")
        reg = ((ev or {}).get("macro") or {}).get("regime") if isinstance(ev, dict) else None
        if isinstance(reg, dict) and reg.get("quadrant") in REGIME_LABELS:
            regime = {"quadrant": reg["quadrant"], "label": reg.get("label") or REGIME_LABELS[reg["quadrant"]],
                      "date": str(reg.get("date") or "")[:10] or None, "borderline": bool(reg.get("borderline")),
                      "months_in_regime": int(reg.get("months_in_regime") or 0),
                      "x": reg.get("x"), "y": reg.get("y")}
    except Exception:
        pass
    dates = [r["date"] for r in readings.values()] + ([regime["date"]] if regime and regime.get("date") else [])
    return {"available": bool(readings or regime), "as_of": max(dates) if dates else None,
            "regime": regime, "readings": readings}


# --- staleness --------------------------------------------------------------------

def _to_date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    text = str(value)
    try:
        if len(text) > 10:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    return date(d.year + y, m + 1, 1)


def age_days(ref_date, now, monthly: bool) -> Optional[int]:
    """Days since the reading was due (see module docstring)."""
    d, n = _to_date(ref_date), _to_date(now)
    if d is None or n is None:
        return None
    due = _add_months(d.replace(day=1), 2) if monthly else d
    return (n - due).days


def is_stale(ref_date, now, monthly: bool) -> bool:
    a = age_days(ref_date, now, monthly)
    return a is None or a > STALE_DAYS


# --- agent ---------------------------------------------------------------------------

def _val(readings: dict, key: str, now) -> Optional[float]:
    r = readings.get(key)
    if not r or is_stale(r.get("date"), now, bool(r.get("monthly"))):
        return None
    return r.get("value")


def evaluate(ctx, symbol: str) -> AgentSignal:
    macro = getattr(ctx, "macro", None) or {}
    cls = asset_class(symbol)
    now = getattr(ctx, "now", None) or datetime.now(timezone.utc).isoformat()
    readings = macro.get("readings") or {}
    regime = macro.get("regime")
    if not macro.get("available") or (not readings and not regime):
        return AgentSignal(agent=AGENT, symbol=symbol, score=0.0, confidence=0.0,
                           summary="No macro data from the newsroom yet.",
                           details={"asset_class": cls}, source="rules")
    col = _TENDENCY_COLUMN.get(cls, "Equities")
    notes = []
    score = 0.0
    conf = 0.0
    details = {"asset_class": cls}
    stale_regime = False
    if regime and regime.get("quadrant") in REGIME_LABELS:
        q = regime["quadrant"]
        direction = tendencies().get(q, {}).get(col, "mixed")
        base = _DIRECTION_SCORE.get(direction, 0.0)
        if cls == "equities_defensive":
            base = base * 0.5 + (0.2 if q in ("slowdown", "stagflation") else 0.0)
        elif cls in ("equities_growth", "crypto"):
            base += 0.1 if q == "goldilocks" else -0.1 if q == "reflation" else 0.0
        elif cls == "equities_cyclical":
            base += 0.1 if q == "reflation" else -0.1 if q == "slowdown" else 0.0
        elif cls == "gold" and q == "slowdown":
            base = 0.0     # gold has tended to hold up better than industrial commodities
        score = base
        stale_regime = is_stale(regime.get("date"), now, monthly=True)
        conf = 0.6 + (0.1 if int(regime.get("months_in_regime") or 0) >= 3 else 0.0)
        if regime.get("borderline"):
            conf *= 0.6
        if stale_regime:
            conf *= 0.6
        if cls == "crypto":
            base *= CRYPTO_SCALE
            score = base
            conf *= CRYPTO_SCALE
        verb = {"up": "tended to rise", "down": "tended to fall", "mixed": "been mixed"}.get(direction, "been mixed")
        notes.append(f"{regime.get('label') or REGIME_LABELS[q]} regime: {ASSET_CLASS_LABELS.get(cls, cls)} have {verb}"
                     if cls != "usd" else f"{regime.get('label') or REGIME_LABELS[q]} regime: the USD has {verb}")
        details.update({"regime": q, "regime_date": regime.get("date"), "borderline": bool(regime.get("borderline")),
                        "months_in_regime": regime.get("months_in_regime"), "tendency": direction,
                        "regime_stale": stale_regime})
    else:
        conf = 0.3
        notes.append("no regime reading")

    equity = cls.startswith("equities")
    adj = 0.0
    y10 = _val(readings, "DGS10", now)
    if y10 is not None:
        details["ust10y_pct"] = y10
        if y10 >= 5.0 and (equity or cls in ("govt_bonds", "credit", "crypto")):
            adj -= 0.2
            notes.append(f"10Y yield {y10:.2f}% ≥ 5%")
    sahm = _val(readings, "SAHM_RULE", now)
    if sahm is not None:
        details["sahm"] = sahm
        if sahm >= 0.5:
            delta = {"equities_cyclical": -0.3, "govt_bonds": 0.2, "gold": 0.1, "credit": -0.2, "crypto": -0.3}.get(
                cls, -0.2 if equity else 0.0)
            if delta:
                adj += delta
                notes.append(f"Sahm rule {sahm:.2f} ≥ 0.5")
    t3m = _val(readings, "T10Y3M", now)
    if t3m is not None:
        details["t10y3m_pp"] = t3m
        if t3m < 0 and equity:
            adj -= 0.1
            notes.append(f"10Y–3M inverted ({t3m:+.2f} pp)")
    t2y = _val(readings, "T10Y2Y", now)
    if t2y is not None:
        details["t10y2y_pp"] = t2y
    vix = _val(readings, "VIXCLS", now)
    if vix is not None:
        details["vix"] = vix
        if vix >= 30:
            delta = -0.15 if equity else -0.1 if cls == "credit" else 0.1 if cls == "govt_bonds" else 0.0
            if delta:
                adj += delta
                notes.append(f"VIX {vix:.0f} ≥ 30")
    hy = _val(readings, "BAMLH0A0HYM2", now)
    if hy is not None:
        details["hy_spread_pct"] = hy
        if hy >= 5.0 and (equity or cls == "credit"):
            adj += -0.15 if cls == "credit" else -0.1
            notes.append(f"HY spread {hy:.2f}% ≥ 5%")
    real = _val(readings, "REAL_POLICY_RATE", now)
    if real is None:
        ff, pce = _val(readings, "FEDFUNDS", now), _val(readings, "PCEPILFE", now)
        real = (ff - pce) if ff is not None and pce is not None else None
    if real is not None:
        details["real_policy_rate_pp"] = round(real, 3)
        if real >= 2.0 and equity:
            adj -= 0.1
            notes.append(f"real policy rate {real:+.2f} pp (restrictive)")
    for key in ("PCEPILFE", "FEDFUNDS"):
        v = _val(readings, key, now)
        if v is not None:
            details[{"PCEPILFE": "core_pce_pct", "FEDFUNDS": "fed_funds_pct"}[key]] = v

    score = max(-1.0, min(1.0, score + adj))
    if not regime:
        conf = min(conf, 0.3) if adj else 0.1
        if cls == "crypto":
            conf *= CRYPTO_SCALE
    if stale_regime:
        notes.append("regime data stale")
    if regime and regime.get("borderline"):
        notes.append("regime borderline")
    details["stress_adjustment"] = round(adj, 3)
    summary = "; ".join(notes[:4]) + "."
    return AgentSignal(agent=AGENT, symbol=symbol, score=round(score, 4), confidence=round(max(0.0, min(1.0, conf)), 4),
                       summary=summary[0].upper() + summary[1:], details=details, source="rules")
