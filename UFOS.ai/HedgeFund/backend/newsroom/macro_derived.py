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
        "(inflation risk). Not shown when core PCE is missing (no CPI substitute, 2026-10-02). Monthly."),
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


class DerivedSeries(dict):
    """key -> derived history, plus .formulas: key -> formula text that differs
    from DERIVED, and .unavailable: key -> "unavailable: <input> missing" for
    indicators that could not be computed (no substitute input — user
    decision 2026-10-02). Returned per compute() call instead of
    the former module-level _formula_override dict, whose content depended on
    which of the two compute() calls ran last (Copilot review 2026-09-25,
    "SAHM ... initialization order")."""

    def __init__(self, *args, formulas: dict = None, unavailable: dict = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.formulas = dict(formulas or {})
        self.unavailable = dict(unavailable or {})


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
    out, formulas, missing = {}, {}, {}
    # No substitute inputs (user decision 2026-10-02): core PCE is required for the real
    # policy rate (no CPI stand-in), the official SAHMREALTIME for the Sahm rule (no own
    # reconstruction). A missing input -> the indicator is "unavailable: <input> missing".
    if full.get("FEDFUNDS") and full.get("PCEPILFE"):
        # Core PCE lags a month behind Fed funds: _combine keeps the common dates.
        out["REAL_POLICY_RATE"] = _combine(full["FEDFUNDS"], full["PCEPILFE"], lambda a, b: a - b)
    else:
        missing["REAL_POLICY_RATE"] = _missing(("FEDFUNDS", "fed funds rate"), ("PCEPILFE", "core PCE"), src=full)
    if curve_monthly:
        out["CURVE_UNINVERSION"] = curve_monthly
    else:
        missing["CURVE_UNINVERSION"] = "unavailable: monthly 10Y−2Y spread (T10Y2Y) missing"
    if sahm_official:
        out["SAHM_RULE"] = sahm_official      # FRED's official real-time Sahm indicator (Claudia Sahm)
    else:
        missing["SAHM_RULE"] = "unavailable: official Sahm indicator (SAHMREALTIME) missing"
    if full.get("UNRATE") and full.get("CPIAUCSL"):
        out["MISERY_INDEX"] = _combine(full["UNRATE"], full["CPIAUCSL"], lambda a, b: a + b)
    else:
        missing["MISERY_INDEX"] = _missing(("UNRATE", "unemployment rate"), ("CPIAUCSL", "CPI"), src=full)
    kept = {k: v for k, v in out.items() if len(v) >= 2}
    for k in out:
        if k not in kept:
            missing[k] = "unavailable: not enough history of its inputs"
    return DerivedSeries(kept, formulas={k: f for k, f in formulas.items() if k in kept}, unavailable=missing)


def _missing(*inputs, src: dict) -> str:
    gone = [f"{label} ({sid})" for sid, label in inputs if not src.get(sid)]
    return "unavailable: " + " and ".join(gone) + " missing"


def to_macro(key: str, full: list, long_history: list = None, formula: str = None) -> dict:
    """formula: the text from compute()'s .formulas (None = the default from DERIVED)."""
    label, unit, default_formula, note = DERIVED[key]
    formula = formula or default_formula
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


def _axes(payems: list, indpro: list, inflation: list) -> tuple:
    """(growth momentum by date, inflation momentum by date) — the one method
    behind the map's point, its trails and the euro-area point."""
    growth_level = _smooth(_combine(payems, indpro, lambda a, b: (a + b) / 2), REGIME_SMOOTHING_MONTHS)
    gx = _by_date(_momentum(growth_level, REGIME_MOMENTUM_MONTHS))
    iy = _by_date(_momentum(_smooth(inflation, REGIME_SMOOTHING_MONTHS), REGIME_MOMENTUM_MONTHS))
    return gx, iy


def _trail(gx: dict, iy: dict, since: str = None) -> list:
    return [{"date": d, "x": round(gx[d], 3), "y": round(iy[d], 3), "quadrant": _quadrant(gx[d], iy[d])}
            for d in sorted(d for d in gx if d in iy) if since is None or d >= since]


def regime(full: dict, aux: dict):
    """full: raw monthly series; aux: {"PAYEMS": [...], "INDPRO": [...]} as YoY %.
    Returns the regime dict or None when inputs are missing."""
    payems, indpro = aux.get("PAYEMS"), aux.get("INDPRO")
    inflation = full.get("PCEPILFE")          # core PCE only — no CPI stand-in (2026-10-02)
    inflation_name = "core PCE"
    if not (payems and indpro and inflation):
        return None
    gx, iy = _axes(payems, indpro, inflation)
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


# --- Extended regime view (2026-09-25, WPF "Expand") ---
# Everything here reuses _axes()/_smooth()/_momentum() above, so the long
# trail, the components and the euro-area point are built exactly like the
# card's point. Static texts only; no AI.

EXTENDED_SINCE = "2000-01-01"
EURO_TRAIL_MONTHS = 12
EURO_INPUTS = "Eurostat industrial production (euro area, YoY) × HICP inflation (YoY)"
EURO_SOURCE_URL = "https://ec.europa.eu/eurostat/databrowser/view/STS_INPR_M/default/table"
EXTENDED_NOTE = (f"Component values are {REGIME_SMOOTHING_MONTHS}-month moving averages of the YoY rates — the "
                 "smoothed values the map's axes are built from; change = latest minus six months earlier "
                 f"({REGIME_MOMENTUM_MONTHS}-month momentum). The growth axis is the average of the payroll and "
                 "industrial-production momentum. Shaded periods: NBER-based US recession indicator (FRED USREC). "
                 "A reading aid, not a forecast.")

_ASSETS = ("Equities", "Government bonds", "Credit", "US dollar", "Commodities", "Sectors")
_PHASES = {
    "goldilocks": ("Growth picking up while inflation cools — historically the most benign mix for risk assets.", (
        "Historically the friendliest phase; broad equity markets have tended to do well, often led by cyclical "
        "and growth-oriented areas.",
        "Mixed: cooling inflation has tended to support bonds, while improving growth can push yields up; returns "
        "have tended to be modest.",
        "Has tended to do well as default risk eases; spreads have usually been stable or narrowing.",
        "No consistent pattern; it has often been softer when global growth broadens.",
        "Mixed: cooling inflation has tended to cap gains, although industrial metals can benefit from stronger "
        "growth.",
        "Cyclical and technology/growth sectors have tended to lead; defensive sectors have tended to lag.")),
    "reflation": ("Growth and inflation both accelerating — supportive for real activity, with the risk of "
                  "overheating and rising rates.", (
        "Have tended to rise, but with more volatility as rate expectations climb; value and cyclical stocks have "
        "often done better than long-duration growth stocks.",
        "Have tended to struggle as yields rise with inflation and expected policy tightening.",
        "Often supported by strong growth, with spreads tending to stay tight, though higher rates weigh on total "
        "returns.",
        "Mixed; it has depended largely on whether US policy tightens faster than elsewhere.",
        "Historically one of the stronger phases for commodities, especially energy and industrial metals.",
        "Energy, materials, industrials and financials have tended to outperform; utilities and other "
        "rate-sensitive defensives have tended to lag.")),
    "stagflation": ("Growth slowing while inflation rises — historically the most difficult mix for traditional "
                    "portfolios.", (
        "Have historically tended to struggle, facing weaker earnings expectations and higher discount rates at the "
        "same time.",
        "Have also tended to struggle, since sticky inflation limits how far yields can fall; the usual "
        "stock–bond diversification has often been weak.",
        "Spreads have tended to widen as growth slows while financing costs stay high; lower-quality credit has "
        "been most exposed.",
        "Mixed: safe-haven demand can support it, while high US inflation can weigh on it.",
        "Commodities and other real assets have tended to hold up relatively better, especially when the "
        "inflation came from supply shocks such as energy.",
        "Energy and defensive sectors (consumer staples, health care, utilities) have tended to hold up better "
        "than cyclical and long-duration growth sectors.")),
    "slowdown": ("Growth and inflation both cooling — the phase in which central banks have typically eased; "
                 "recession risk rises if it deepens.", (
        "Have tended to be choppy: earnings expectations often fall, while the prospect of central-bank easing "
        "can provide support.",
        "Historically the strongest phase for high-quality government bonds, as yields fall with inflation and "
        "expected rate cuts.",
        "Investment grade has tended to hold up better than high yield; spreads have tended to widen if the "
        "slowdown deepened toward recession.",
        "Often firm in risk-off episodes (safe-haven demand), although falling US rates can weigh on it.",
        "Have tended to weaken with slowing demand; gold has sometimes held up better than industrial "
        "commodities.",
        "Defensive sectors (consumer staples, health care, utilities) and higher-quality companies have tended to "
        "outperform cyclicals.")),
}
PHASE_BEHAVIOUR = {q: {"summary": summary,
                       "assets": [{"asset": a, "tendency": t} for a, t in zip(_ASSETS, tendencies)]}
                   for q, (summary, tendencies) in _PHASES.items()}
PHASE_BEHAVIOUR["disclaimer"] = "Historical tendencies, not a forecast — information, not a recommendation."


def recessions(usrec: list, since: str = EXTENDED_SINCE) -> list:
    """FRED USREC (monthly 0/1) -> [{"start": "YYYY-MM", "end": "YYYY-MM"}].
    A recession still running at the last observation ends at that month."""
    out, start, last = [], None, None
    for p in sorted(usrec or [], key=lambda p: p["date"]):
        if p["value"] >= 0.5:
            start = start or p["date"]
            last = p["date"]
        elif start:
            out.append((start, last))
            start = None
    if start:
        out.append((start, last))
    return [{"start": s[:7], "end": e[:7]} for s, e in out if e >= since]


def _component(name: str, axis: str, points: list):
    s = _smooth(points or [], REGIME_SMOOTHING_MONTHS)
    if len(s) <= REGIME_MOMENTUM_MONTHS:
        return None
    latest, before = s[-1], s[-1 - REGIME_MOMENTUM_MONTHS]
    return {"name": name, "axis": axis, "latest": round(latest["value"], 3),
            "six_months_ago": round(before["value"], 3),
            "change": round(latest["value"] - before["value"], 3), "unit": "%", "date": latest["date"]}


def euro_point(ip_yoy: list, hicp_yoy: list):
    """Euro-area point: Eurostat industrial production YoY × HICP YoY, same
    smoothing/momentum as the US point (single growth input). None if short."""
    if not ip_yoy or not hicp_yoy:
        return None
    gx = _by_date(_momentum(_smooth(ip_yoy, REGIME_SMOOTHING_MONTHS), REGIME_MOMENTUM_MONTHS))
    iy = _by_date(_momentum(_smooth(hicp_yoy, REGIME_SMOOTHING_MONTHS), REGIME_MOMENTUM_MONTHS))
    trail = _trail(gx, iy)[-EURO_TRAIL_MONTHS:]
    if not trail:
        return None
    now = trail[-1]
    return {"x": now["x"], "y": now["y"], "quadrant": now["quadrant"], "label": REGIMES[now["quadrant"]][0],
            "date": now["date"], "trail": trail, "inputs": EURO_INPUTS, "source_url": EURO_SOURCE_URL}


def regime_unavailable_reason(full: dict, aux: dict) -> str:
    """Why regime() returned None: "unavailable: <inputs> missing"."""
    src = {"PAYEMS": aux.get("PAYEMS"), "INDPRO": aux.get("INDPRO"), "PCEPILFE": full.get("PCEPILFE")}
    if all(src.values()):
        return "unavailable: no common months of its inputs"
    return _missing(("PAYEMS", "nonfarm payrolls"), ("INDPRO", "industrial production"), ("PCEPILFE", "core PCE"),
                    src=src)


def regime_extended(long_inputs: dict, usrec: list = None, euro_ip: list = None, euro_hicp: list = None) -> dict:
    """long_inputs: monthly YoY histories since 2000 — "PAYEMS", "INDPRO",
    "PCEPILFE" (core PCE; no CPI substitute since 2026-10-02 — missing → no
    trail). Contract: see README "Macro regime — extended view"."""
    payems, indpro = long_inputs.get("PAYEMS") or [], long_inputs.get("INDPRO") or []
    core = long_inputs.get("PCEPILFE")
    inflation = core or []
    trail_long = []
    if payems and indpro and inflation:
        trail_long = _trail(*_axes(payems, indpro, inflation), since=EXTENDED_SINCE)
    components = [c for c in (
        _component("Nonfarm payrolls (YoY)", "growth", payems),
        _component("Industrial production (YoY)", "growth", indpro),
        _component("Core PCE inflation (YoY)" if core else "CPI inflation (YoY)", "inflation", inflation),
    ) if c]
    euro, euro_error = None, None
    try:
        euro = euro_point(euro_ip, euro_hicp)
    except Exception as ex:          # said so, not silently "no euro point"
        euro_error = f"unavailable: euro-area point could not be computed ({type(ex).__name__})"
    return {"trail_long": trail_long, "recessions": recessions(usrec), "components": components,
            "euro": euro, "euro_error": euro_error, "phase_behaviour": PHASE_BEHAVIOUR, "note": EXTENDED_NOTE,
            # Added for the expanded map (2026-09-25, second pass):
            "episodes": episodes(trail_long),
            "history": [{"date": p["date"], "quadrant": p["quadrant"]} for p in trail_long],
            "component_series": component_series(payems, indpro, inflation,
                                                 "core PCE" if core else "CPI"),
            "asset_tendencies": ASSET_TENDENCIES}


# --- Expanded map, second pass (2026-09-25): episodes, component sparklines,
# compact asset-tendency table. Pure functions over the long histories above.

COMPONENT_SERIES_MONTHS = 36

# Reference months for past phases. Each marker is the regime reading of that
# month, computed with exactly the same method as today's point (read from
# trail_long) — so it only appears when the month is in the data.
EPISODES = (
    ("Dot-com bust", "2001-11-01"),
    ("Mid-2000s expansion", "2005-06-01"),
    ("GFC trough", "2009-03-01"),
    ("COVID shock", "2020-04-01"),
    ("Inflation peak", "2022-06-01"),
)


def _month_name(date: str) -> str:
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    return f"{months[int(date[5:7]) - 1]} {date[:4]}"


def episodes(trail_long: list) -> list:
    """[{label, date, x, y, quadrant}] for the EPISODES months present in trail_long."""
    by_date = {p["date"]: p for p in trail_long or []}
    out = []
    for name, date in EPISODES:
        p = by_date.get(date)
        if p:
            out.append({"label": f"{name} ({_month_name(date)})", "date": date,
                        "x": p["x"], "y": p["y"], "quadrant": p["quadrant"]})
    return out


def _tail(points: list, months: int = COMPONENT_SERIES_MONTHS) -> list:
    return [{"date": p["date"], "value": round(p["value"], 3)} for p in (points or [])[-months:]]


def component_series(payems: list, indpro: list, inflation: list, inflation_name: str) -> dict:
    """Monthly inputs for the sparklines (last 36 months each): raw YoY rates and
    the two smoothed momentum series the map's axes are made of."""
    growth_m, inflation_m = [], []
    if payems and indpro:
        growth_level = _smooth(_combine(payems, indpro, lambda a, b: (a + b) / 2), REGIME_SMOOTHING_MONTHS)
        growth_m = _momentum(growth_level, REGIME_MOMENTUM_MONTHS)
    if inflation:
        inflation_m = _momentum(_smooth(inflation, REGIME_SMOOTHING_MONTHS), REGIME_MOMENTUM_MONTHS)
    return {"payrolls_yoy": _tail(payems), "indpro_yoy": _tail(indpro),
            "inflation_yoy": _tail(inflation), "inflation_name": inflation_name,
            "growth_momentum": _tail(growth_m), "inflation_momentum": _tail(inflation_m)}


# Compact "how asset classes have tended to behave" table (single source; the
# WPF view only renders it). direction: "up" | "down" | "mixed". Deliberately
# coarse and hedged — textbook tendencies, not a forecast, not advice.
_TENDENCY_ASSETS = ("Equities", "Govt bonds", "Credit", "USD", "Commodities")
_TENDENCY_TEXT = {"up": "↑ tended to rise", "down": "↓ tended to fall", "mixed": "mixed"}
_TENDENCY_ROWS = (
    ("goldilocks", "Goldilocks", (("up", ""), ("mixed", ""), ("up", ""), ("mixed", ""), ("mixed", ""))),
    ("reflation", "Reflation", (("up", "led by cyclicals"), ("down", ""), ("up", ""), ("mixed", ""), ("up", ""))),
    ("stagflation", "Stagflation", (("down", ""), ("down", ""), ("down", ""), ("mixed", ""), ("up", ""))),
    ("slowdown", "Slowdown", (("down", "or mixed"), ("up", ""), ("down", ""), ("up", ""), ("down", ""))),
)
ASSET_TENDENCIES = {
    "title": "How asset classes have tended to behave in each regime",
    "columns": list(_TENDENCY_ASSETS),
    "rows": [{"quadrant": q, "label": label,
              "cells": [{"asset": a, "direction": d,
                         "text": _TENDENCY_TEXT[d] + (f" ({note})" if note else "")}
                        for a, (d, note) in zip(_TENDENCY_ASSETS, cells)]}
             for q, label, cells in _TENDENCY_ROWS],
    "disclaimer": "Historical tendencies, simplified; not a forecast — information, not a recommendation.",
}
