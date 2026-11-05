"""Gate rules of the UFOS validation (SPEC §9.4) for the parts this study covers, with the presets.

In scope (time-series strategies):

* **G1** consistency: fold Sharpe > 0 in >= ``g1_folds_min`` of 5 walk-forward folds
  (the cross-sectional rank-IC form is out of scope: the study's rule is single-asset);
* **G2** alpha: OLS of strategy on benchmark returns, alpha > 0 AND Newey-West t >= ``g2_alpha_t``;
* **G4** deflated Sharpe >= ``g4_dsr_min``;
* **G5** PBO (CSCV, S = 10) <= ``g5_pbo_max``; fewer than 4 configurations -> fail;
* **G6** (grid-neighbour part only): base Sharpe > 0, at least one neighbour, and every
  neighbour keeps >= ``g6_neighbour_ratio`` x the base Sharpe;
* **G7** closed trades >= ``g7_min_trades``;
* **H** holdout: net Sharpe > 0 AND >= 50 % of the training Sharpe (fixed, not in the presets).

Out of scope: G3 (signal decay; "not applicable" for a single-asset rule in UFOS), the
cost-multiplier and stress-episode parts of G6, G8 (capacity).

A gate whose metric cannot be computed fails (never a default pass), as in UFOS. Every
gate returns ``(passed: bool, value)``.
"""
from __future__ import annotations

from typing import Dict, Optional, Sequence

PRESET_NAMES = ("lenient", "standard", "strict")
#: Thresholds of the in-scope gates, identical to the UFOS presets (SPEC §9.4, gates.PRESETS).
PRESETS: Dict[str, Dict[str, float]] = {
    "lenient": {"g1_folds_min": 3, "g2_alpha_t": 1.5, "g4_dsr_min": 0.90, "g5_pbo_max": 0.50,
                "g6_neighbour_ratio": 0.40, "g7_min_trades": 50},
    "standard": {"g1_folds_min": 4, "g2_alpha_t": 2.0, "g4_dsr_min": 0.95, "g5_pbo_max": 0.30,
                 "g6_neighbour_ratio": 0.50, "g7_min_trades": 100},
    "strict": {"g1_folds_min": 5, "g2_alpha_t": 3.0, "g4_dsr_min": 0.99, "g5_pbo_max": 0.20,
               "g6_neighbour_ratio": 0.60, "g7_min_trades": 200},
}
H_TRAINING_RATIO = 0.50
K_FOLDS = 5
PBO_MIN_CONFIGS = 4
IN_SCOPE = ("G1", "G2", "G4", "G5", "G6", "G7")
OUT_OF_SCOPE = {"G1 (cross-sectional)": "rank-IC form: the study's rule is single-asset (time-series form only)",
                "G3": "signal decay: not applicable to single-asset rules in UFOS (counts as pass there)",
                "G6 costs": "the costs x multiplier run", "G6 episodes": "the stress-episode drawdown check",
                "G8": "capacity"}


def g1(fold_sharpes: Sequence[Optional[float]], th: dict):
    if len(fold_sharpes) != K_FOLDS:
        return False, None
    pos = sum(1 for s in fold_sharpes if s is not None and s > 0)
    return pos >= int(th["g1_folds_min"]), pos


def g2(alpha: dict, th: dict):
    t = alpha.get("value")
    if t is None:
        return False, None
    return bool(alpha["alpha_daily"] > 0 and t >= th["g2_alpha_t"]), t


def g4(dsr: dict, th: dict):
    v = dsr.get("value")
    if v is None:
        return False, None
    return bool(v >= th["g4_dsr_min"]), v


def g5(pbo: dict, th: dict):
    if pbo.get("configs", 0) < PBO_MIN_CONFIGS:
        return False, None
    v = pbo.get("value")
    if v is None:
        return False, None
    return bool(v <= th["g5_pbo_max"]), v


def g6_neighbours(base_sharpe: Optional[float], neighbour_sharpes: Sequence[Optional[float]], th: dict):
    """Grid-neighbour part of G6 -> (passed, worst ratio)."""
    if base_sharpe is None or base_sharpe <= 0 or not neighbour_sharpes:
        return False, None
    if any(s is None for s in neighbour_sharpes):
        return False, None
    worst = min(s / base_sharpe for s in neighbour_sharpes)
    return bool(worst >= th["g6_neighbour_ratio"]), worst


def g7(trades_closed: Optional[int], th: dict):
    if trades_closed is None:
        return False, None
    return bool(trades_closed >= int(th["g7_min_trades"])), trades_closed


def holdout(sharpe: Optional[float], training_sharpe: Optional[float]):
    """Gate H (time-series form) -> (passed, holdout Sharpe)."""
    if sharpe is None or training_sharpe is None:
        return False, sharpe
    return bool(sharpe > 0 and sharpe >= H_TRAINING_RATIO * training_sharpe), sharpe
