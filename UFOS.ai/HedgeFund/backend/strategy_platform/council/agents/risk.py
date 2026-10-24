"""[Council of AIs, SP §22.1 — stage 10 S10-C R1] The logic of ``fund/agents/risk.py`` MOVED here unchanged
(FUND §7–§9 stay the behavioural contract); the old file runs the old engine until R4, the parity test
(SP §22.10) proves both equal.

Risk agent (rules, not directional) — fund/SPEC.md §7.

``assess(ctx, symbol, params) -> RiskAssessment`` (``.as_signal()`` gives the
UI card: agent "risk", score 0, confidence 1, details incl. the multiplier).

Rules (multipliers multiply):
- halted -> veto (no new exposure at all);
- account drawdown ≥ 75 % of ``drawdown_stop_pct`` -> ×0.5;
- high-impact US release or FOMC decision today or on the next trading day
  (newsroom calendar events ``{date, kind: release|fomc, importance, name}``)
  -> ×0.5;
- ATR% (14) > 3 % -> ×0.5, > 5 % -> ×0.25 (crypto: > 6 % -> ×0.5, > 9 % ->
  ×0.25 — crypto's normal daily range is about twice that of equities);
- gross exposure of the symbol's asset class (agents.macro.asset_class) ≥
  2 × ``max_position_pct`` of equity (crypto: ``crypto_max_position_pct``)
  -> ``block_increase`` (hold/reduce only);
- shorts disabled or symbol in ``not_shortable`` -> ``veto_short`` (shorts
  only: no short at all, an existing short is closed);
- gross short exposure cap (``short_exposure_max_pct``) reached ->
  ``block_short_increase``: existing shorts may be held or reduced, no new or
  larger short (the cap must not close shorts that are already open).

``risk_enabled = False`` switches the soft rules off (multiplier 1, no
concentration block); the hard limits (halt, short permissions) stay.
Paper-trading proof of concept, not investment advice.
"""
from __future__ import annotations

from datetime import date
from typing import List, Optional

from strategy_platform.council import indicators as ind
from strategy_platform.data import market_feed as market
from strategy_platform.core import params as fund_params
from strategy_platform.core.types import RiskAssessment
from strategy_platform.strategy.features.macro_agent import asset_class

HIGH_IMPACT = ("high",)


def _ny_today(now) -> date:
    return market.ny_date(market._to_utc(now))


def calendar_events_near(calendar, now) -> List[dict]:
    """High-impact US releases / FOMC decisions on the NY date of ``now`` or
    the next trading day."""
    today = _ny_today(now)
    nxt = market.next_trading_day(today)
    days = {today.isoformat(), nxt.isoformat()}
    out = []
    for e in calendar or []:
        if not isinstance(e, dict):
            continue
        d = str(e.get("date") or "")[:10]
        if d not in days:
            continue
        if e.get("kind") == "fomc" or str(e.get("importance") or "").lower() in HIGH_IMPACT:
            out.append(e)
    return out


def atr_thresholds(symbol: str):
    """(high, elevated) daily ATR fractions: ×0.25 above high, ×0.5 above
    elevated. Equities 5 % / 3 %, crypto 9 % / 6 %."""
    return (0.09, 0.06) if fund_params.is_crypto(symbol) else (0.05, 0.03)


def _class_gross_pct(ctx, cls: str) -> Optional[float]:
    acct = getattr(ctx, "account", None)
    equity = getattr(acct, "equity", None) if acct is not None else None
    if not equity or equity <= 0:
        return None
    gross = 0.0
    for sym, pos in (getattr(ctx, "positions", None) or {}).items():
        if asset_class(sym) != cls:
            continue
        try:
            gross += abs(float(pos.market_value))
        except Exception:
            continue
    return gross / equity * 100.0


def assess(ctx, symbol: str, params: Optional[dict] = None) -> RiskAssessment:
    params = params if params is not None else (getattr(ctx, "params", None) or {})
    sym = str(symbol).upper()
    ra = RiskAssessment(symbol=sym)
    mult = 1.0
    reasons: List[str] = []

    if getattr(ctx, "halted", False):
        ra.veto = True
        reasons.append("trading halted (kill switch / drawdown stop)")

    # short permissions (hard limits)
    if not params.get("allow_shorts", True):
        ra.veto_short = True
        reasons.append("shorts disabled")
    elif sym in {str(s).upper() for s in params.get("not_shortable") or []}:
        ra.veto_short = True
        reasons.append("not shortable (no locate)")
    else:
        acct = getattr(ctx, "account", None)
        cap = params.get("short_exposure_max_pct")
        if acct is not None and cap is not None and getattr(acct, "short_exposure_pct", None) is not None:
            if float(acct.short_exposure_pct) >= float(cap):
                ra.block_short_increase = True
                reasons.append(f"short exposure cap {float(cap):.0f}% reached — no new or larger shorts")

    details = {}
    if params.get("risk_enabled", True):
        acct = getattr(ctx, "account", None)
        stop = float(params.get("drawdown_stop_pct") or 0.0)
        dd = getattr(acct, "drawdown_pct", None) if acct is not None else None
        if dd is not None and stop > 0 and float(dd) >= 0.75 * stop:
            mult *= 0.5
            reasons.append(f"drawdown {float(dd):.1f}% near the {stop:.1f}% stop (×0.5)")
        cal_err = getattr(ctx, "calendar_error", None)
        near = []
        if cal_err:
            ra.unavailable = f"unavailable: calendar data could not be loaded ({str(cal_err)[:160]})"
        else:
            try:
                near = calendar_events_near(getattr(ctx, "calendar", None), ctx.now)
            except Exception as ex:      # never "no events" as a stand-in (user decision 2026-10-02)
                ra.unavailable = f"unavailable: calendar data could not be read ({type(ex).__name__})"
        if near:
            e = sorted(near, key=lambda x: (x.get("kind") != "fomc", str(x.get("date"))))[0]
            name = "FOMC decision" if e.get("kind") == "fomc" else (e.get("name") or "high-impact release")
            more = f" +{len(near) - 1} more" if len(near) > 1 else ""
            mult *= 0.5
            reasons.append(f"{name} on {str(e.get('date'))[:10]}{more} (×0.5)")
        bars = (getattr(ctx, "bars", None) or {}).get(sym) or []
        atrp = ind.atr_pct(bars[-60:], 14) if len(bars) >= 15 else None
        if atrp is not None:
            details["atr_pct"] = round(atrp * 100, 2)
            high, elevated = atr_thresholds(sym)
            if atrp > high:
                mult *= 0.25
                reasons.append(f"high volatility, ATR {atrp * 100:.1f}% (×0.25)")
            elif atrp > elevated:
                mult *= 0.5
                reasons.append(f"elevated volatility, ATR {atrp * 100:.1f}% (×0.5)")
        cls = asset_class(sym)
        gross = _class_gross_pct(ctx, cls)
        max_pos = fund_params.max_position_pct_for(sym, params)
        if gross is not None:
            details["class_gross_pct"] = round(gross, 2)
            if max_pos > 0 and gross >= 2 * max_pos:
                ra.block_increase = True
                reasons.append(f"{cls.replace('_', ' ')} exposure {gross:.1f}% ≥ {2 * max_pos:.0f}% — no increase")
    else:
        reasons.append("risk agent off — hard limits only")

    ra.size_multiplier = max(0.0, min(1.0, mult))
    ra.reasons = reasons
    ra.extra = details          # not part of the dataclass; merged into the UI card by assess_signal()
    return ra


def assess_signal(ctx, symbol: str, params: Optional[dict] = None):
    """(RiskAssessment, its AgentSignal for the UI card with extra details)."""
    ra = assess(ctx, symbol, params)
    sig = ra.as_signal()
    sig.details.update(getattr(ra, "extra", None) or {})
    return ra, sig
