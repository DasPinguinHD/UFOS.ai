"""Strategy SDK (SPEC §7.1): ``Strategy.on_bar(ctx) -> weights | None``.

``Context`` is the point-in-time view of bar t: it holds ONLY the panel's
rows ≤ t (``panel.head(t + 1)``), so nothing after t can be read; asking
for a later row raises ``LookAheadError``. ``on_bar`` returns target
weights ``{symbol: weight}`` on a rebalance date or ``None`` (keep the
current positions).

``SpecStrategy(spec)`` runs a StrategySpec through the same evaluator and
portfolio rules; ``run(strategy, panel)`` steps through every bar. The
acceptance test checks that the step-by-step run equals the vectorised
``portfolio.weights_history`` on the full panel.

Python-coded strategies are a research aid only (stage 2 stores and runs
specs — data — not user code).

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from . import portfolio
from .evaluate import evaluate


class LookAheadError(RuntimeError):
    """A strategy tried to read data after the current bar."""


class Context:
    def __init__(self, panel, t: int, *, rebalance: bool = True):
        if not 0 <= t < len(panel.dates):
            raise IndexError("bar index outside the panel")
        self._panel = panel.head(t + 1)
        self.t = t
        self.date = panel.dates[t]
        self.symbols = list(panel.symbols)
        self.rebalance = rebalance

    @property
    def panel(self):
        """The panel up to and including this bar (rows 0..t)."""
        return self._panel

    def history(self, field: str, n: Optional[int] = None):
        """Rows ≤ t of a field (the last n rows when given)."""
        arr = self._panel.fields[field]
        return arr if n is None else arr[max(0, len(arr) - int(n)):]

    def value(self, field: str, symbol: str, t: Optional[int] = None) -> float:
        t = self.t if t is None else int(t)
        if t > self.t:
            raise LookAheadError(f"row {t} lies after the current bar {self.t}")
        if t < 0:
            raise IndexError("negative row")
        return float(self._panel.fields[field][t, self.symbols.index(symbol)])


class Strategy:
    """Base class: override ``on_bar``."""

    def on_bar(self, ctx: Context) -> Optional[Dict[str, float]]:  # pragma: no cover - interface
        raise NotImplementedError


class SpecStrategy(Strategy):
    """A StrategySpec as an SDK strategy (evaluates the prefix panel each bar)."""

    def __init__(self, spec: dict, *, ext_values: Optional[dict] = None):
        self.spec = spec
        self.ext_values = ext_values

    def on_bar(self, ctx: Context) -> Optional[Dict[str, float]]:
        if not ctx.rebalance:
            return None
        sig = evaluate(self.spec, ctx.panel, ext_values=self.ext_values)
        return portfolio.target_weights(self.spec, sig[ctx.t], ctx.panel, ctx.t)


def run(strategy: Strategy, panel, *, spec: Optional[dict] = None, start: int = 0) -> List[tuple]:
    """[(date, weights | None)] for every bar from ``start``. The rebalance
    schedule comes from ``spec`` (or ``strategy.spec``); without one every
    bar is a rebalance bar."""
    spec = spec if spec is not None else getattr(strategy, "spec", None)
    mask = portfolio.rebalance_mask(spec, panel.dates) if spec is not None else [True] * len(panel.dates)
    out = []
    for t in range(start, len(panel.dates)):
        out.append((panel.dates[t], strategy.on_bar(Context(panel, t, rebalance=mask[t]))))
    return out
