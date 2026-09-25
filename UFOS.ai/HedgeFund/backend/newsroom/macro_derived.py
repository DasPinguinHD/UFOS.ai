"""Derived macro indicators for the Financial Newsroom (2026-09-25).

Computed from the FRED series fred_collector.py already fetches (plus one
extra monthly T10Y2Y request) — no new data source. Each indicator becomes one
"macro" event with the same shape as a raw series (value, change, history,
note, signal) plus "group": "derived" and a "formula", so the WPF view renders
it with the same card/sparkline/colour code.

All thresholds are rules of thumb — a reading aid, not a forecast and not
investment advice. Thresholds + popup texts: macro_rules.py (single source).
"""

DISPLAY_POINTS = 24

# id -> (label, unit, formula, how to read it)
DERIVED = {
    "REAL_POLICY_RATE": (
        "Real Policy Rate", "pp", "Fed funds rate − core PCE inflation (YoY)",
        "How hard the Fed is braking after inflation, measured with the Fed's own inflation gauge. "
        "Above ~2 pp = clearly restrictive (growth risk); below 0 = policy still stimulates "
        "(inflation risk). Falls back to CPI if core PCE is unavailable. Monthly."),
    "CURVE_UNINVERSION": (
        "Yield-Curve Un-inversion", "pp", "10Y − 2Y spread, monthly average, vs. its 24-month low",
        "Recessions historically tended to start around the time an inverted curve turns positive again — "
        "not while it is inverted. Red = inverted within the last 24 months and positive again now. Monthly."),
    "SAHM_RULE": (
        "Sahm Rule", "pp", "Official real-time Sahm indicator (FRED SAHMREALTIME): 3-month avg. unemployment − its 12-month low",
        "Real-time recession indicator (Claudia Sahm): a rise of 0.5 pp or more has marked the start of "
        "every US recession since 1970. Dashed line = 0.5. Monthly."),
    "MISERY_INDEX": (
        "Misery Index", "%", "Unemployment rate + CPI inflation (YoY)",
        "Simple gauge of economic pain for households. Below ~8 is comfortable, 10+ historically "
        "coincided with high dissatisfaction (1970s–80s peaked near 20). Monthly."),
}


from .macro_rules import stability_signal, describe_rules

OK_REASON = "Within its usual range."
_formula_override: dict = {}


def _by_date(points: list) -> dict:
    return {p["date"]: p["value"] for p in points}


def _combine(a: list, b: list, op) -> list:
    """Pointwise combination on dates present in both (monthly series share 1st-of-month dates)."""
    bd = _by_date(b)
    return [{"date": p["date"], "value": round(op(p["value"], bd[p["date"]]), 4)} for p in a if p["date"] in bd]


def _sahm_series(unrate: list) -> list:
    out = []
    avgs = []
    for i in range(3, len(unrate) + 1):
        avgs.append((unrate[i - 1]["date"], sum(p["value"] for p in unrate[i - 3:i]) / 3))
    for i in range(12, len(avgs)):
        low = min(v for _, v in avgs[i - 12:i])
        out.append({"date": avgs[i][0], "value": round(avgs[i][1] - low, 4)})
    return out


def _signal(key: str, full: list) -> dict:
    """Thresholds live in macro_rules.py (single source for logic + popup text)."""
    if key == "CURVE_UNINVERSION":
        v = full[-1]["value"]
        low = min(p["value"] for p in full[-24:])
        level, reason = "ok", OK_REASON
        if v < 0:
            level, reason = "watch", f"curve inverted ({v:+.2f} pp)"
        elif low < 0:
            level, reason = "alert", (f"positive again ({v:+.2f} pp) after an inversion down to {low:+.2f} pp "
                                      "within 24 months — historically the riskier phase")
        return {"level": level, "reason": reason, "rules": describe_rules(key)}
    sig = stability_signal(key, full)
    if sig["level"] == "ok":
        sig["reason"] = OK_REASON
    return {**sig, "rules": describe_rules(key)}


def compute(full: dict, curve_monthly: list, sahm_official: list = None) -> dict:
    """full: series_id -> monthly history (oldest first). Returns key -> derived history.
    Called twice per poll: with the recent histories (cards, signals) and with the
    long monthly histories since 2000 (range switcher, percentiles)."""
    out = {}
    inflation = full.get("PCEPILFE") or full.get("CPIAUCSL")
    if full.get("FEDFUNDS") and inflation:
        # Core PCE lags a month behind Fed funds: _combine keeps the common dates.
        out["REAL_POLICY_RATE"] = _combine(full["FEDFUNDS"], inflation, lambda a, b: a - b)
        _formula_override["REAL_POLICY_RATE"] = (None if full.get("PCEPILFE")
                                                 else "Fed funds rate − CPI inflation (YoY) — core PCE unavailable")
    if curve_monthly:
        out["CURVE_UNINVERSION"] = curve_monthly
    if sahm_official:
        # FRED's official real-time Sahm indicator (SAHMREALTIME, Claudia Sahm) —
        # preferred over our own reconstruction, which is only the fallback.
        out["SAHM_RULE"] = sahm_official
        _formula_override["SAHM_RULE"] = None
    elif full.get("UNRATE"):
        out["SAHM_RULE"] = _sahm_series(full["UNRATE"])
        _formula_override["SAHM_RULE"] = ("3-month avg. unemployment − its low of the previous 12 months "
                                          "(own calculation; official SAHMREALTIME unavailable)")
    if full.get("UNRATE") and full.get("CPIAUCSL"):
        out["MISERY_INDEX"] = _combine(full["UNRATE"], full["CPIAUCSL"], lambda a, b: a + b)
    return {k: v for k, v in out.items() if len(v) >= 2}


def to_macro(key: str, full: list, long_history: list = None) -> dict:
    label, unit, formula, note = DERIVED[key]
    formula = _formula_override.get(key) or formula
    history = full[-DISPLAY_POINTS:]
    latest, previous = history[-1], history[-2]
    return {
        "label": label,
        "macro": {
            "series_id": key,
            "group": "derived",
            "formula": formula,
            "unit": unit,
            "value": latest["value"],
            "date": latest["date"],
            "previous": previous["value"],
            "previous_date": previous["date"],
            "change": round(latest["value"] - previous["value"], 4),
            "history": history,
            "note": note,
            "ref_line": 0.5 if key == "SAHM_RULE" else None,
            "long_history": long_history or [],
            "signal": _signal(key, full),
        },
    }


# --- Macro regime map (2026-09-25, step 3) ---
# Classic 2×2 "growth × inflation" regime view, built on MOMENTUM (is it
# speeding up or slowing down?), not on levels:
#   x (growth)    = 6-month change of the average YoY growth of nonfarm payrolls
#                   (PAYEMS) and industrial production (INDPRO), in pp
#   y (inflation) = 6-month change of core PCE YoY (CPI YoY as fallback), in pp
# Quadrants: Goldilocks (growth ↑, inflation ↓), Reflation (↑, ↑),
# Stagflation (↓, ↑), Slowdown / disinflation (↓, ↓).
# A reading aid for positioning discussions — not a forecast, not advice.

REGIME_MOMENTUM_MONTHS = 6
# Both inputs are smoothed with a 3-month moving average before the momentum is
# taken (2026-09-25): unsmoothed, industrial production made the dot jump
# between quadrants almost every month.
REGIME_SMOOTHING_MONTHS = 3
REGIME_TRAIL_MONTHS = 12

REGIMES = {
    "goldilocks": ("Goldilocks", "ok",
                   "Growth is picking up while inflation cools — historically the friendliest mix for equities."),
    "reflation": ("Reflation", "watch",
                  "Growth and inflation are both accelerating — risk of overheating and higher rates; "
                  "cyclicals and commodities have tended to do better than bonds."),
    "stagflation": ("Stagflation", "alert",
                    "Growth is slowing while inflation rises — the hardest mix for both stocks and bonds, "
                    "and it ties the Fed's hands."),
    "slowdown": ("Slowdown (disinflation)", "watch",
                 "Growth and inflation are both cooling — typically the phase when the Fed eases and "
                 "high-quality bonds do well; recession risk rises if it deepens."),
}


def _smooth(points: list, months: int) -> list:
    return [{"date": points[i]["date"], "value": sum(p["value"] for p in points[i - months + 1:i + 1]) / months}
            for i in range(months - 1, len(points))]


def _momentum(points: list, months: int) -> list:
    return [{"date": points[i]["date"], "value": points[i]["value"] - points[i - months]["value"]}
            for i in range(months, len(points))]


def _quadrant(x: float, y: float) -> str:
    if x >= 0:
        return "reflation" if y >= 0 else "goldilocks"
    return "stagflation" if y >= 0 else "slowdown"


def regime(full: dict, aux: dict):
    """full: raw monthly series; aux: {"PAYEMS": [...], "INDPRO": [...]} as YoY %.
    Returns the regime dict or None when inputs are missing."""
    payems, indpro = aux.get("PAYEMS"), aux.get("INDPRO")
    inflation = full.get("PCEPILFE") or full.get("CPIAUCSL")
    inflation_name = "core PCE" if full.get("PCEPILFE") else "CPI"
    if not (payems and indpro and inflation):
        return None
    growth_level = _smooth(_combine(payems, indpro, lambda a, b: (a + b) / 2), REGIME_SMOOTHING_MONTHS)
    gx = _by_date(_momentum(growth_level, REGIME_MOMENTUM_MONTHS))
    iy = _by_date(_momentum(_smooth(inflation, REGIME_SMOOTHING_MONTHS), REGIME_MOMENTUM_MONTHS))
    dates = sorted(d for d in gx if d in iy)
    if not dates:
        return None
    trail = [{"date": d, "x": round(gx[d], 3), "y": round(iy[d], 3), "quadrant": _quadrant(gx[d], iy[d])}
             for d in dates[-REGIME_TRAIL_MONTHS:]]
    now = trail[-1]
    name, level, description = REGIMES[now["quadrant"]]
    # Momentum near 0 on either axis = the quadrant can flip with the next release.
    borderline = abs(now["x"]) < 0.1 or abs(now["y"]) < 0.1
    if borderline:
        description += " Close to a boundary — the regime can flip with the next data release."
    months_in = 0
    for p in reversed(trail):
        if p["quadrant"] != now["quadrant"]:
            break
        months_in += 1
    return {
        "quadrant": now["quadrant"],
        "label": name,
        "level": level,
        "description": description,
        "date": now["date"],
        "x": now["x"],
        "y": now["y"],
        "months_in_regime": months_in,
        "borderline": borderline,
        "growth_text": (f"Growth {'accelerating' if now['x'] >= 0 else 'slowing'}: "
                        f"{now['x']:+.2f} pp in {REGIME_MOMENTUM_MONTHS} months "
                        "(avg. YoY of payrolls & industrial production)"),
        "inflation_text": (f"Inflation {'rising' if now['y'] >= 0 else 'falling'}: "
                           f"{now['y']:+.2f} pp in {REGIME_MOMENTUM_MONTHS} months ({inflation_name} YoY)"),
        "trail": trail,
        "method": (f"x = {REGIME_MOMENTUM_MONTHS}-month change of the average YoY growth of nonfarm payrolls "
                   f"and industrial production; y = {REGIME_MOMENTUM_MONTHS}-month change of {inflation_name} "
                   f"YoY; both smoothed over {REGIME_SMOOTHING_MONTHS} months. Momentum, not levels — "
                   "a reading aid, not a forecast."),
    }
