"""Council Rules (no AI) — the rule-based part of the Council of AIs
(fund/agents: Technical + Macro + Risk rules) as a library strategy (SPEC
§7.9). Contains no AI: the live loop's LLM sentiment agent and judge cannot
be backtested. Renamed from "Agentic Committee (rules)" (user, 2026-10-06);
the library key ``agentic_committee_rules`` stays (stable id).

Mirrors (daily bars, NYSE panel):
- Technical agent (fund/agents/technical.py): trend = mean of sign(close −
  SMA50), sign(SMA50 − SMA200), sign(close − SMA200); momentum = 0.5·tanh(r20
  / 5 %) + 0.5·tanh(r60 / 10 %); raw = 0.55·trend + 0.45·momentum +
  0.1·(2·p − 1), p = position of the close in the 252-bar low..high range
  (0.5 when flat); RSI14 > 70 damps a long score ×0.6, < 30 a short score;
  score clipped to ±1; confidence 0.35 + 0.3·[trend and momentum agree] +
  0.35·min(1, |score|/0.6), × max(0.4, 1 − (ATR% − 2 %)·15) when ATR% > 2 %.
- Macro agent: ``{"ext": "macro.regime_score"}`` (B2's point-in-time score
  of the rule-based macro agent per asset class) with the agent's base
  confidence 0.6 (parameter ``macro_confidence``).
- Rule score as in the judge's input (fund/SPEC.md §8.1): Σ(w·score·conf) /
  Σ(w·conf), weights technical 1, macro 1 (the AI sentiment agent and the
  LLM judge are not part of a rule strategy).
- Risk agent multipliers: high-impact US release / FOMC today or next
  trading day (``{"ext": "calendar.event_risk"}`` = 1) → ×0.5; ATR% > 3 % →
  ×0.5, > 5 % → ×0.25.
Portfolio: long when the scaled score > 0.20, short when < −0.20 (the
judge's rule-suggestion thresholds), weights ∝ |score|, ≤ 10 % each.

Differences (documented, accepted for a spec): SMA200/52-week terms need
their full windows (the agent drops SMA200 terms with < 200 bars); the
crypto-specific ATR thresholds (4 %/6 %/9 %) and 365-bar year are not used
(one rule set for all symbols); Wilder RSI/ATR start at the panel's first
bar instead of a 260-bar window; risk multipliers scale the score, so they
also act on the thresholds.
"""
from ._build import ALL_PRESET, entry, ext, f, op, p, param, r, spec

C, H, L = f("close"), f("high"), f("low")

FEATURES = {
    "sma50": op("sma", C, 50),
    "sma200": op("sma", C, 200),
    "trend": op("div", op("add", op("sign", op("sub", C, r("sma50"))), op("sign", op("sub", r("sma50"), r("sma200"))),
                          op("sign", op("sub", C, r("sma200")))), 3),
    "momentum": op("add", op("mul", 0.5, op("tanh", op("div", op("returns", 20), 0.05))),
                   op("mul", 0.5, op("tanh", op("div", op("returns", 60), 0.10)))),
    "hi52": op("ts_max", H, 252),
    "lo52": op("ts_min", L, 252),
    "pos52": op("where", op("gt", r("hi52"), r("lo52")),
                op("div", op("sub", C, r("lo52")), op("sub", r("hi52"), r("lo52"))), 0.5),
    "raw": op("add", op("mul", 0.55, r("trend")), op("mul", 0.45, r("momentum")),
              op("mul", 0.1, op("sub", op("mul", 2, r("pos52")), 1))),
    "rsi14": op("rsi", 14),
    "damped": op("where", op("and", op("gt", r("rsi14"), 70), op("gt", r("raw"), 0)), op("mul", r("raw"), 0.6),
                 op("where", op("and", op("lt", r("rsi14"), 30), op("lt", r("raw"), 0)), op("mul", r("raw"), 0.6),
                    r("raw"))),
    "tech_score": op("clip", r("damped"), -1, 1),
    "atr_pct": op("div", op("atr", 14), C),
    "agree": op("and", op("eq", op("sign", r("trend")), op("sign", r("momentum"))), op("ne", op("sign", r("trend")), 0)),
    "tech_conf": op("clip", op("mul",
                               op("add", 0.35, op("mul", 0.3, r("agree")),
                                  op("mul", 0.35, op("min", 1, op("div", op("abs", r("tech_score")), 0.6)))),
                               op("where", op("gt", r("atr_pct"), 0.02),
                                  op("max", 0.4, op("sub", 1, op("mul", op("sub", r("atr_pct"), 0.02), 15))), 1)),
                    0, 1),
    "macro_score": ext("macro.regime_score"),
    "rule_score": op("div", op("add", op("mul", r("tech_score"), r("tech_conf")),
                               op("mul", r("macro_score"), p("macro_confidence"))),
                     op("add", r("tech_conf"), p("macro_confidence"))),
    "event_mult": op("sub", 1, op("mul", 0.5, ext("calendar.event_risk"))),
    "vol_mult": op("where", op("gt", r("atr_pct"), 0.05), 0.25, op("where", op("gt", r("atr_pct"), 0.03), 0.5, 1)),
}

ENTRIES = [
    entry("agentic_committee_rules", "composite", "UFOS.ai HedgeFund Council of AIs (fund/agents, 2026) — "
          "its rule-based technical, macro and risk agents", 5,
          "Contains no AI. The Council of AIs' rule agents as one spec: technical score × confidence and the "
          "macro regime score combined like the judge's rule score, scaled by the risk agent's event and "
          "volatility multipliers. The live loop adds an LLM sentiment agent and an LLM judge, which can't be "
          "backtested and are not included. See alphas/composite.py for the documented differences.",
          spec(name="Council Rules (no AI)", category="composite",
               description="Rule-based part of the Council of AIs (Technical + Macro + Risk rules) — contains no "
                           "AI; the live loop adds an LLM sentiment agent and judge, which can't be backtested.",
               tags=["committee", "technical", "macro", "risk"], universe=ALL_PRESET,
               parameters={"macro_confidence": param(0.6, 0.0, 1.0, step=0.1, integer=False,
                                                     description="Confidence given to the macro regime score "
                                                                 "(the macro agent's base confidence).")},
               features=dict(FEATURES),
               signal=op("mul", r("rule_score"), r("event_mult"), r("vol_mult")),
               portfolio={"mode": "long_short", "selection": {"type": "threshold", "long_above": 0.2,
                                                              "short_below": -0.2},
                          "weighting": "signal", "max_weight": 0.1},
               frequency="daily")),
]
