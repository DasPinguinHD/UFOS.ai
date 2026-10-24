"""Position sizing and the trade decision — fund/SPEC.md §9.

Paper-trading proof of concept; simulated sizes, not investment advice.

- ``target_weight(decision, risk, atr_pct, params, current_weight) -> float``
  signed target weight (fraction of equity, short < 0):
  ``sign × max_position_pct/100 × confidence × vol_scaler × risk.size_multiplier``
  with ``vol_scaler = min(1, 0.015 / max(atr_pct, 1e-6))`` (``atr_pct`` as a
  FRACTION, e.g. 0.02 = 2 % daily ATR; None -> scaler 1).
  * FLAT -> 0 (closes; regardless of confidence).
  * confidence < min_confidence/100 -> no new position (0 when flat); an
    existing position is kept (current weight) — also when the low-confidence
    action points the other way (checked BEFORE the short permission, so a
    weak SHORT signal on a non-shortable symbol never closes a long).
  * SHORT with ``veto_short`` / ``allow_shorts`` off / symbol not shortable -> 0.
  * SHORT with ``risk.block_short_increase`` (short exposure cap reached) ->
    an existing short may be held or reduced (min of current and computed),
    no new short; a long is closed (0), not reversed.
  * ``risk.veto`` -> no new exposure: an existing same-direction position may
    only shrink (min of current and computed), everything else -> 0.
  * ``risk.block_increase`` (asset-class concentration) -> same as veto for
    increases: hold or reduce only, no new/reversed position.
- ``plan_trade(decision, risk, position, atr_pct, params, now, trades_today,
  equity=None, enforce_min_hold=True, enforce_trade_limit=True)
  -> (target_weight | None, skip_reason | None)``: target None = no order.
  Trade only if |target − current| ≥ 0.01 or the sign changes; positions
  opened fewer than ``min_hold_days`` NY trading days ago are not
  reduced/closed/reversed (``enforce_min_hold=False`` for manual-accept,
  kill switch etc.); no new orders once ``trades_today`` ≥
  ``max_trades_per_day`` (skip reason "daily trade limit"). A reversal
  (long <-> short) needs two orders: with only one order left today the
  target becomes 0 (close only) and the second value is the note
  "daily trade limit: only the close fits today" (target not None).
  ``position``: None, a ``PositionView`` (needs ``equity`` for the weight) or
  a dict ``{"weight", "opened_at"}`` (``weight`` signed fraction).

Crypto (``params.is_crypto(decision.symbol)``, SPEC §9): ``crypto_max_position_pct``
instead of ``max_position_pct``, vol target 3 % daily ATR (``CRYPTO_VOL_TARGET``)
instead of 1.5 %, and ``min_hold_days`` counts UTC calendar days (24/7
market) instead of NY trading days.
"""
from __future__ import annotations

from typing import Optional, Tuple

from strategy_platform.data import market_feed as market
from strategy_platform.core import params as fund_params
from strategy_platform.core.types import JudgeDecision, PositionView, RiskAssessment

VOL_TARGET = 0.015
CRYPTO_VOL_TARGET = 0.03
REBALANCE_THRESHOLD = 0.01
EPS = 1e-9


def _sign(x: float) -> int:
    return 1 if x > EPS else -1 if x < -EPS else 0


def vol_scaler(atr_pct: Optional[float], symbol: Optional[str] = None) -> float:
    if atr_pct is None:
        return 1.0
    target = CRYPTO_VOL_TARGET if symbol is not None and fund_params.is_crypto(symbol) else VOL_TARGET
    return min(1.0, target / max(float(atr_pct), 1e-6))


def _shortable(decision: JudgeDecision, risk: Optional[RiskAssessment], params: dict) -> bool:
    if risk is not None and risk.veto_short:
        return False
    if not params.get("allow_shorts", True):
        return False
    return decision.symbol.upper() not in {str(s).upper() for s in params.get("not_shortable") or []}


def target_weight(decision: JudgeDecision, risk: Optional[RiskAssessment], atr_pct: Optional[float],
                  params: dict, current_weight: float = 0.0) -> float:
    params = params or {}
    current = float(current_weight or 0.0)
    if decision.action == "FLAT":
        return 0.0
    sign = 1 if decision.action == "LONG" else -1 if decision.action == "SHORT" else 0
    if sign == 0:
        return 0.0
    min_conf = float(params.get("min_confidence", 75)) / 100.0
    if decision.confidence < min_conf:
        return current if _sign(current) != 0 else 0.0
    if sign < 0 and not _shortable(decision, risk, params):
        return 0.0
    mult = risk.size_multiplier if risk is not None else 1.0
    w = sign * fund_params.max_position_pct_for(decision.symbol, params) / 100.0 * float(decision.confidence) \
        * vol_scaler(atr_pct, decision.symbol) * max(0.0, min(1.0, mult))
    no_increase = risk is not None and (risk.veto or risk.block_increase
                                        or (sign < 0 and getattr(risk, "block_short_increase", False)))
    if no_increase:
        if _sign(current) == sign:
            return sign * min(abs(current), abs(w))
        return 0.0
    return w


def _current(position, equity: Optional[float]) -> Tuple[float, Optional[str]]:
    if position is None:
        return 0.0, None
    if isinstance(position, dict):
        return float(position.get("weight") or 0.0), position.get("opened_at")
    if isinstance(position, PositionView):
        if not position.qty:
            return 0.0, None
        if not equity or equity <= 0:
            raise ValueError("plan_trade needs equity to size a PositionView")
        if position.market_value is None:
            raise ValueError(f"plan_trade needs a current price for {position.symbol} (valuation paused)")
        return position.market_value / float(equity), position.opened_at
    return float(position), None


def plan_trade(decision: JudgeDecision, risk: Optional[RiskAssessment], position, atr_pct: Optional[float],
               params: dict, now, trades_today: int, equity: Optional[float] = None,
               enforce_min_hold: bool = True, enforce_trade_limit: bool = True
               ) -> Tuple[Optional[float], Optional[str]]:
    params = params or {}
    current, opened_at = _current(position, equity)
    target = target_weight(decision, risk, atr_pct, params, current)
    sign_change = _sign(target) != _sign(current)
    if abs(target - current) < REBALANCE_THRESHOLD and not sign_change:
        if _sign(current) == 0 and decision.action != "FLAT":
            min_conf = float(params.get("min_confidence", 75))
            if decision.confidence * 100.0 < min_conf:
                return None, f"confidence {decision.confidence * 100:.0f}% below minimum {min_conf:.0f}%"
            if decision.action == "SHORT" and not _shortable(decision, risk, params):
                return None, "short not allowed"
            if risk is not None and risk.veto:
                return None, "risk veto"
            if risk is not None and risk.block_increase:
                return None, "asset-class concentration limit"
            if decision.action == "SHORT" and risk is not None and getattr(risk, "block_short_increase", False):
                return None, "short exposure cap reached"
        return None, "no change needed"
    reduces = _sign(current) != 0 and (sign_change or abs(target) < abs(current) - EPS)
    min_hold = int(params.get("min_hold_days", 0) or 0)
    if enforce_min_hold and reduces and min_hold > 0 and opened_at:
        if fund_params.is_crypto(decision.symbol):
            held = market.utc_days_between(opened_at, now)
            if held < min_hold:
                return None, f"min hold {min_hold} day(s) (held {held}, UTC days)"
        else:
            held = market.trading_days_between(opened_at, now)
            if held < min_hold:
                return None, f"min hold {min_hold} trading day(s) (held {held})"
    if enforce_trade_limit:
        remaining = int(params.get("max_trades_per_day", 6)) - int(trades_today or 0)
        if remaining <= 0:
            return None, "daily trade limit"
        reverses = _sign(current) != 0 and _sign(target) != 0 and _sign(target) != _sign(current)
        if reverses and remaining < 2:
            return 0.0, "daily trade limit: only the close fits today"
    return round(target, 6), None
