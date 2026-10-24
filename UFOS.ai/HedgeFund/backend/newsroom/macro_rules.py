"""Stability signal (green / orange / red) for the Macro Indicators cards.

Single source of truth (2026-09-25): every threshold lives once in RULES
below. stability_signal() evaluates them and describe_rules() turns the very
same entries into the "Watch (orange)" / "Alert (red)" text of the view's
"i" popup — so the popup can no longer drift away from the logic.

Rules of thumb — a reading aid for the dashboard, NOT a forecast and not
investment advice. The worst level of all rules of a series wins:
  ok    = within its usual range / moving slowly  -> green
  watch = elevated level or unusually fast move    -> orange
  alert = extreme level or very fast move          -> red
Daily series: "recent move" = change across the card window (~5 weeks).
Monthly series: change over 12 months.
"""
from dataclasses import dataclass
from typing import Optional

LEVEL_ORDER = {"ok": 0, "watch": 1, "alert": 2}
OK_REASON = "Within its usual range, moving slowly."


@dataclass(frozen=True)
class Rule:
    kind: str            # above | below | abs_move | rise | claims_rise | sahm_rise
    watch: float
    alert: float
    text: str            # reason template; placeholders: {v} value, {m} move/rise
    window: Optional[int] = None   # observations for moves; None = whole card window
    unit: str = ""       # unit for the popup text ("%", " pp", "K", "")


def _move(history: list, points: Optional[int]) -> float:
    window = history if points is None else history[-(points + 1):]
    return window[-1]["value"] - window[0]["value"] if len(window) > 1 else 0.0


def _metric(rule: Rule, history: list) -> float:
    v = history[-1]["value"]
    if rule.kind in ("above", "below"):
        return v
    if rule.kind in ("abs_move", "rise"):
        return _move(history, rule.window)
    if rule.kind == "claims_rise":
        avgs = [sum(p["value"] for p in history[i - 4:i]) / 4 for i in range(4, len(history) + 1)]
        return (avgs[-1] / min(avgs) - 1) * 100 if avgs and min(avgs) > 0 else 0.0
    if rule.kind == "sahm_rise":
        avgs = [sum(p["value"] for p in history[i - 3:i]) / 3 for i in range(3, len(history) + 1)]
        return avgs[-1] - min(avgs[-13:-1]) if len(avgs) >= 13 else 0.0
    raise ValueError(f"unknown rule kind {rule.kind}")


def _level(rule: Rule, x: float) -> str:
    if rule.kind == "below":
        return "alert" if x <= rule.alert else "watch" if x <= rule.watch else "ok"
    if rule.kind == "abs_move":
        x = abs(x)
    return "alert" if x >= rule.alert else "watch" if x >= rule.watch else "ok"


_DAILY_WINDOW = "~5 weeks"


def _window_text(rule: Rule) -> str:
    return _DAILY_WINDOW if rule.window is None else f"{rule.window} months"


def _num(x: float) -> str:
    return f"{x:g}".replace("-", "−")


def _describe(rule: Rule, which: str) -> str:
    t = rule.watch if which == "watch" else rule.alert
    if rule.kind == "above":
        return f"≥ {_num(t)}{rule.unit}"
    if rule.kind == "below":
        return f"≤ {_num(t)}{rule.unit}"
    if rule.kind == "abs_move":
        return f"±{_num(t)} pp in {_window_text(rule)}"
    if rule.kind == "rise":
        return f"+{_num(t)} pp in {_window_text(rule)}"
    if rule.kind == "claims_rise":
        return f"4-wk avg +{_num(t)} % vs. its 6-month low"
    if rule.kind == "sahm_rise":
        return f"3-mo avg +{_num(t)} pp vs. its 12-mo low"
    return "?"


RULES = {
    "FEDFUNDS": [Rule("abs_move", 1.0, 2.0, "policy rate {m:+.2f} pp in 12 months — fast tightening/easing cycle", window=12)],
    "DGS2": [Rule("above", 5.5, 6.0, "2Y yield at {v:.2f}% — markets price a very tight Fed", unit=" %"),
             Rule("abs_move", 0.5, 0.75, "moved {m:+.2f} pp in ~5 weeks — Fed expectations shifting fast")],
    "DGS10": [Rule("above", 5.0, 5.5, "10Y yield at {v:.2f}% (≥ 5% = stress level for equities and mortgages)", unit=" %"),
              Rule("abs_move", 0.5, 0.75, "moved {m:+.2f} pp in ~5 weeks — unusually fast")],
    "T10Y2Y": [Rule("below", 0.0, -0.5, "curve inverted ({v:+.2f} pp) — historically a recession warning", unit=" pp")],
    "T10Y3M": [Rule("below", 0.0, -0.5, "curve inverted ({v:+.2f} pp) — NY Fed model's recession input", unit=" pp")],
    "CPIAUCSL": [Rule("above", 3.0, 5.0, "inflation {v:.1f}% — above the Fed's ~2% goal", unit=" %"),
                 Rule("below", 1.0, 0.0, "inflation {v:.1f}% — deflation risk", unit=" %")],
    "PCEPILFE": [Rule("above", 2.5, 3.5, "core PCE {v:.1f}% — above the Fed's 2% target", unit=" %"),
                 Rule("below", 1.0, 0.0, "core PCE {v:.1f}% — well below target", unit=" %")],
    "UNRATE": [Rule("sahm_rise", 0.3, 0.5, "3-month average up {m:.2f} pp from its 12-month low (Sahm rule ≥ 0.5 = recession signal)"),
               Rule("above", 6.0, 7.0, "unemployment at {v:.1f}%", unit=" %")],
    "ICSA": [Rule("above", 300, 350, "{v:.0f}K claims in the latest week", unit="K"),
             Rule("claims_rise", 15, 25, "4-week average {m:.0f}% above its 6-month low — layoffs picking up")],
    "BAMLH0A0HYM2": [Rule("above", 4.5, 6.0, "high-yield spread at {v:.2f}% — credit stress", unit=" %"),
                     Rule("rise", 1.0, 1.5, "widened {m:+.2f} pp in ~5 weeks")],
    "VIXCLS": [Rule("above", 20, 30, "VIX at {v:.1f} — elevated equity fear")],
    # Euro area (2026-09-25)
    "ECBDFR": [Rule("above", 3.5, 4.0, "ECB deposit rate at {v:.2f}% — restrictive (2023 peak: 4.0%)", unit=" %")],
    "IRLTLT01DEM156N": [Rule("above", 3.5, 4.0, "10Y Bund at {v:.2f}% — highest range since 2011", unit=" %"),
                        Rule("abs_move", 0.75, 1.25, "moved {m:+.2f} pp in 12 months", window=12)],
    # Derived indicators (macro_derived.py). CURVE_UNINVERSION has a custom rule there
    # (it needs the 24-month history), described in CUSTOM_RULE_TEXT.
    "REAL_POLICY_RATE": [Rule("above", 2.0, 3.0, "{v:+.2f} pp — clearly restrictive, weighs on growth", unit=" pp"),
                         Rule("below", 0.0, -2.0, "{v:+.2f} pp — policy below inflation, stimulates", unit=" pp")],
    "SAHM_RULE": [Rule("above", 0.3, 0.5, "{v:.2f} pp — at/near the Sahm threshold (≥ 0.5 = recession signal)", unit=" pp")],
    "MISERY_INDEX": [Rule("above", 8, 10, "{v:.1f} — elevated economic pain")],
    "CP0000EZ19M086NEST": [Rule("above", 3.0, 5.0, "euro-area inflation {v:.1f}% — above the ECB's 2% target", unit=" %"),
                Rule("below", 1.0, 0.0, "euro-area inflation {v:.1f}% — deflation risk", unit=" %")],
}


def stability_signal(series_id: str, history: list) -> dict:
    worst = ("ok", OK_REASON)
    v = history[-1]["value"]
    for rule in RULES.get(series_id, []):
        m = _metric(rule, history)
        level = _level(rule, m)
        if LEVEL_ORDER[level] > LEVEL_ORDER[worst[0]]:
            worst = (level, rule.text.format(v=v, m=m))
    return {"level": worst[0], "reason": worst[1]}


CUSTOM_RULE_TEXT = {
    "CURVE_UNINVERSION": {"watch": "curve inverted now (< 0)",
                          "alert": "inverted within 24 months, positive again now"},
}


def describe_rules(series_id: str) -> dict:
    if series_id in CUSTOM_RULE_TEXT:
        return dict(CUSTOM_RULE_TEXT[series_id])
    rules = RULES.get(series_id, [])
    return {"watch": " or ".join(_describe(r, "watch") for r in rules),
            "alert": " or ".join(_describe(r, "alert") for r in rules)}
