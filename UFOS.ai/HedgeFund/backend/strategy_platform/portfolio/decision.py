"""The decision block shared by the backtest and the live/paper runtime (SPEC §17.3 —
"one strategy runtime for backtest, paper and live").

``decide`` is the code that ``backtest/engine.run`` executes at every simulated close
(extracted 2026-10-17 without any behaviour change — the engine's parity and
engine tests pin it) and that ``live/runtime.py`` executes at every real close:
rebalance targets via ``strategy.portfolio.target_weights`` on rebalance rows (or the
first row: the latest rebalance row at/before it), stop-loss / take-profit exits
(symbols stay flat until the next rebalance), the drawdown stop (flat for good), the
vol target (de-levers only). Returns ``(targets, force, reason)``: ``targets`` =
{symbol: weight of the strategy's own equity} or None (no rebalance → hold),
``force`` = symbols to flatten now.

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

import math
from typing import Callable, Dict, List, Optional, Set, Tuple

VOL_TARGET_LOOKBACK = 60
PERIODS_PER_YEAR = {"nyse": 252, "utc": 365}


class DecisionState:
    """Carried between decisions: symbols stopped out (row index of the stop) and the drawdown halt."""

    def __init__(self, stopped: Optional[Dict[str, int]] = None, halted: bool = False):
        self.stopped: Dict[str, int] = dict(stopped or {})
        self.halted = bool(halted)

    def to_dict(self) -> dict:
        return {"stopped": dict(self.stopped), "halted": self.halted}

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "DecisionState":
        d = d or {}
        return cls({str(k): int(v) for k, v in (d.get("stopped") or {}).items()}, bool(d.get("halted")))


def vol_scale(targets: Dict[str, float], i: int, close, col: Dict[str, int], calendar: str, vol_target: float,
              day=None, warnings=None) -> Dict[str, float]:
    """Scales the targets down (never up) to the annualised vol target, from the last
    VOL_TARGET_LOOKBACK daily returns of the ``close`` panel up to row i."""
    import numpy as np
    keep_syms = []
    for s in sorted(targets):
        win = close[i - VOL_TARGET_LOOKBACK:i + 1, col[s]] if i >= VOL_TARGET_LOOKBACK else None
        if win is None or np.isnan(win).any() or (win <= 0).any():
            if warnings is not None:
                warnings.count("vol_target_no_history", s, day, s + " was left out of {count} rebalance(s): fewer "
                               f"than {VOL_TARGET_LOOKBACK} daily returns for the volatility target.")
            continue
        keep_syms.append(s)
    if not keep_syms:
        return {}
    win = close[i - VOL_TARGET_LOOKBACK:i + 1][:, [col[s] for s in keep_syms]]
    rets = win[1:] / win[:-1] - 1.0
    cov = np.atleast_2d(np.cov(rets, rowvar=False, ddof=1))
    w = np.array([targets[s] for s in keep_syms])
    vol = math.sqrt(max(float(w @ cov @ w), 0.0) * PERIODS_PER_YEAR[calendar])
    scale = min(1.0, float(vol_target) / vol) if vol > 0 else 1.0
    return {s: targets[s] * scale for s in keep_syms}


def decide(full: dict, panel, signal, mask, i: int, *, first: bool, last_rebalance: Optional[int],
           state: DecisionState, positions: List[dict], close_price: Callable[[str], Optional[float]],
           drawdown_pct: Optional[float], held: Set[str], calendar: str, day=None, warnings=None,
           stats: Optional[dict] = None) -> Tuple[Optional[Dict[str, float]], Set[str], str]:
    """One decision at the close of panel row ``i`` (see the module docstring).

    positions: the strategy account's open positions ({symbol, qty, avg_price});
    close_price(symbol): the closing price used for the stop checks (None = no check);
    drawdown_pct: the strategy account's drawdown from its peak at this close (None = unvalued);
    held: symbols held now (flattened by the drawdown stop)."""
    from ..strategy import portfolio
    risk = full["risk"] or {}
    sl, tp, dd_stop = risk.get("stop_loss_pct"), risk.get("take_profit_pct"), risk.get("max_drawdown_stop_pct")
    vol_target = risk.get("vol_target_annual")
    stats = stats if stats is not None else {}
    targets: Optional[Dict[str, float]] = None
    reason = "risk exit"
    if mask[i] or first:
        r = i if mask[i] else last_rebalance
        if r is not None:
            targets = portfolio.target_weights(full, signal[r], panel, r)
            stats["rebalances"] = stats.get("rebalances", 0) + 1
            reason = "strategy rebalance"
        for s in [s for s, at in state.stopped.items() if at < i]:
            del state.stopped[s]
    if drawdown_pct is not None and dd_stop and not state.halted and float(drawdown_pct) >= float(dd_stop):
        state.halted = True
        if warnings is not None:
            warnings.add("drawdown_stop", f"Drawdown {drawdown_pct:.2f} % reached the stop of {dd_stop:g} % "
                         f"on {day.isoformat() if day else '?'}: everything is closed at the next execution and the "
                         "strategy stays flat for the rest of the backtest.",
                         date=day.isoformat() if day else None, drawdown_pct=round(float(drawdown_pct), 4))
    if (sl or tp) and not state.halted:
        for p in positions:
            s = p["symbol"]
            avg = float(p.get("avg_price") or 0.0)
            px = close_price(s)
            if s in state.stopped or px is None or avg <= 0:
                continue
            pnl = (px / avg - 1.0) * (1.0 if p["qty"] > 0 else -1.0) * 100.0
            if sl and pnl <= -float(sl):
                state.stopped[s] = i
                stats["stop_exits"] = stats.get("stop_exits", 0) + 1
                if warnings is not None:
                    warnings.count("stop_loss", s, day, s + f": stop loss ({sl:g} %) hit {{count}} time(s).")
            elif tp and pnl >= float(tp):
                state.stopped[s] = i
                stats["take_profit_exits"] = stats.get("take_profit_exits", 0) + 1
                if warnings is not None:
                    warnings.count("take_profit", s, day, s + f": take profit ({tp:g} %) hit {{count}} time(s).")
    if state.halted:
        return {}, set(held), "max drawdown stop"
    force = set(state.stopped)
    if targets is not None:
        targets = {s: w for s, w in targets.items() if s not in state.stopped}
        if vol_target and targets:
            col = {s: j for j, s in enumerate(panel.symbols)}
            targets = vol_scale(targets, i, panel.fields["close"], col, calendar, vol_target, day, warnings)
    return targets, force, reason
