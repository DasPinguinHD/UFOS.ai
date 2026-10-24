"""Plain-text summary of a spec for the UI (SPEC §7.11 ``summary``):
``summary_texts(spec) -> {universe_text, signal_text, features_text: {name:
text}, portfolio_text, rebalance_text, risk_text}``. Texts are plain text
(external texts inside are never markup). Expects a valid spec; missing
optional blocks take their defaults.

Part of UFOS.ai, a proof-of-concept portfolio project.
"""
from __future__ import annotations

from . import schema
from .dsl import expr_text

_MODE = {"long_only": "Long only", "short_only": "Short only", "long_short": "Long/short"}
_WEIGHT = {"equal": "equal weights", "signal": "weights proportional to |signal|",
           "inverse_vol": "inverse-volatility weights"}
_FREQ = {"daily": "Daily (every trading day)", "weekly": "Weekly (first trading day of the week)",
         "monthly": "Monthly (first trading day of the month)"}


def _pct(v) -> str:
    return f"{v * 100:g}%"


def universe_text(u: dict) -> str:
    from . import universe as uni
    if "preset" in u:
        p = uni.preset(u["preset"])
        name = p["name"] if p else u["preset"]
        classes = u.get("asset_classes") or list(schema.ASSET_CLASSES)
        try:
            n = len(uni.resolve(u))
        except ValueError:
            n = 0
        return f"Preset “{name}” ({', '.join(classes)}): {n} symbols"
    syms = list(u.get("symbols") or [])
    shown = ", ".join(syms[:10]) + (f" +{len(syms) - 10} more" if len(syms) > 10 else "")
    return f"{len(syms)} symbol{'s' if len(syms) != 1 else ''}: {shown}"


def portfolio_text(pf: dict) -> str:
    sel = pf.get("selection") or {}
    t = sel.get("type")
    if t == "top_n":
        s = f"top {sel.get('n')} by signal"
    elif t == "bottom_n":
        s = f"bottom {sel.get('n')} by signal"
    elif t == "top_bottom_n":
        s = f"long top {sel.get('n')}, short bottom {sel.get('n')}"
    elif t == "threshold":
        parts = []
        if sel.get("long_above") is not None:
            parts.append(f"long when signal > {sel['long_above']:g}")
        if sel.get("short_below") is not None:
            parts.append(f"short when signal < {sel['short_below']:g}")
        s = ", ".join(parts) or "threshold"
    else:
        s = "all symbols with a signal"
    w = _WEIGHT.get(pf.get("weighting"), str(pf.get("weighting")))
    if pf.get("weighting") == "inverse_vol":
        w += f" ({pf.get('vol_lookback')}-day volatility)"
    return (f"{_MODE.get(pf.get('mode'), pf.get('mode'))} · {s} · {w} · max {_pct(pf.get('max_weight', 1.0))} "
            f"per position (excess stays in cash) · gross {_pct(pf.get('gross_exposure', 1.0))}")


def rebalance_text(spec: dict) -> str:
    freq = _FREQ.get(spec["rebalance"].get("frequency"), str(spec["rebalance"].get("frequency")))
    ex = spec["execution"]
    timing = {"next_open": "orders at the next open", "next_close": "orders at the next close"}.get(
        ex.get("timing"), str(ex.get("timing")))
    c = spec["costs"]
    costs = ("paper-broker costs" if c.get("model") == "paper_broker"
             else f"costs {c.get('fee_bps', 0):g} bps fee + {c.get('slippage_bps', 0):g} bps slippage")
    return f"{freq} · {ex.get('order_type', 'market')} {timing} · {costs}"


def risk_text(r: dict) -> str:
    parts = []
    if r.get("stop_loss_pct") is not None:
        parts.append(f"stop loss {r['stop_loss_pct']:g}%")
    if r.get("take_profit_pct") is not None:
        parts.append(f"take profit {r['take_profit_pct']:g}%")
    if r.get("vol_target_annual") is not None:
        parts.append(f"volatility target {_pct(r['vol_target_annual'])} p.a.")
    if r.get("max_drawdown_stop_pct") is not None:
        parts.append(f"drawdown stop {r['max_drawdown_stop_pct']:g}%")
    if not parts:
        return "No risk overlays"
    return " · ".join(parts) + " (applied by the backtest/runner, stage 3+)"


def summary_texts(spec: dict) -> dict:
    s = schema.with_defaults(spec)
    return {
        "universe_text": universe_text(s.get("universe") or {}),
        "signal_text": expr_text(s.get("signal")),
        "features_text": {name: expr_text(e) for name, e in (s.get("features") or {}).items()},
        "portfolio_text": portfolio_text(s["portfolio"]),
        "rebalance_text": rebalance_text(s),
        "risk_text": risk_text(s["risk"]),
    }
