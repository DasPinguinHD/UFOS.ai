"""Shared data types of the HedgeFund loop (see fund/SPEC.md).

Stdlib only (dataclasses) so every part — agents, judge, broker, engine, tests —
can import it on Python 3.10 without third-party packages. Change only together
with SPEC.md.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

ACTIONS = ("LONG", "SHORT", "FLAT")
DIRECTIONAL_AGENTS = ("technical", "macro", "sentiment")
AGENTS = DIRECTIONAL_AGENTS + ("risk",)
ORDER_SIDES = ("buy", "sell", "short", "cover")
ORDER_ORIGINS = ("autopilot", "manual", "manual-accept", "kill-switch", "drawdown-stop", "margin-call", "reset")


@dataclass
class Bar:
    """One daily bar (adjusted). date = 'YYYY-MM-DD' (exchange date)."""
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class Quote:
    symbol: str
    price: float
    prev_close: Optional[float] = None
    ts: Optional[str] = None       # UTC ISO-8601 of the quote
    delayed: bool = True
    stale: bool = False            # fallback price (e.g. cached daily close), ts = the bar's time

    @property
    def change_pct(self) -> Optional[float]:
        if not self.prev_close:
            return None
        return (self.price / self.prev_close - 1.0) * 100.0


@dataclass
class AgentSignal:
    agent: str                      # technical|macro|sentiment|risk
    symbol: str
    score: float                    # -1..1, + = bullish
    confidence: float               # 0..1
    summary: str                    # one short line for the UI card
    details: Dict[str, Any] = field(default_factory=dict)
    source: str = "rules"           # rules|llm|cached|unavailable
    provenance: Optional[dict] = None
    usage: Optional[dict] = None    # usage_info() of the LLM call (sentiment), if any

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RiskAssessment:
    symbol: str
    veto: bool = False              # no new exposure at all
    veto_short: bool = False        # no short exposure
    size_multiplier: float = 1.0    # 0..1
    block_increase: bool = False    # may hold/reduce, not increase (concentration)
    reasons: List[str] = field(default_factory=list)
    block_short_increase: bool = False  # short exposure cap reached: shorts may be held/reduced, not enlarged

    def as_signal(self) -> AgentSignal:
        text = "; ".join(self.reasons) if self.reasons else "No risk constraints — full size allowed."
        if self.veto:
            text = "Veto: " + text
        return AgentSignal(agent="risk", symbol=self.symbol, score=0.0, confidence=1.0, summary=text,
                           details={"veto": self.veto, "veto_short": self.veto_short,
                                    "size_multiplier": round(self.size_multiplier, 3),
                                    "block_increase": self.block_increase,
                                    "block_short_increase": self.block_short_increase,
                                    "reasons": list(self.reasons)},
                           source="rules")


@dataclass
class PositionView:
    symbol: str
    qty: float                      # < 0 = short
    avg_price: float
    last_price: Optional[float] = None
    opened_at: Optional[str] = None
    realized_pnl: float = 0.0
    borrow_rate_pct: Optional[float] = None

    @property
    def side(self) -> str:
        return "short" if self.qty < 0 else "long"

    @property
    def asset_class(self) -> str:
        """"crypto" | "equity" (see params.asset_type)."""
        from .params import asset_type
        return asset_type(self.symbol)

    @property
    def instrument(self) -> str:
        """"spot" (crypto long), "perp" (crypto short, simulated perpetual
        future) or "stock" (equities/ETFs, long or Reg T short)."""
        if self.asset_class == "crypto":
            return "perp" if self.qty < 0 else "spot"
        return "stock"

    @property
    def market_value(self) -> float:
        """Signed market value (short = negative)."""
        price = self.last_price if self.last_price is not None else self.avg_price
        return self.qty * price

    @property
    def unrealized_pnl(self) -> float:
        price = self.last_price if self.last_price is not None else self.avg_price
        return (price - self.avg_price) * self.qty


@dataclass
class AccountView:
    equity: float
    cash: float
    long_mv: float
    short_mv: float                 # absolute value of short market value
    buying_power: float
    peak_equity: float
    drawdown_pct: float             # 0..100, from peak
    maintenance_required: float
    margin_excess: float
    short_exposure_pct: float       # short_mv / equity * 100
    starting_capital: float
    reset_at: Optional[str] = None
    day_pnl: Optional[float] = None
    total_pnl: Optional[float] = None
    total_pnl_pct: Optional[float] = None
    funding_total: Optional[float] = None         # signed sum of perp funding since the last reset
    crypto_margin_excess: Optional[float] = None  # isolated perp margin excess (None: no crypto shorts)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MarketContext:
    """Everything the agents may look at for one cycle (built by the engine)."""
    now: str                                        # UTC ISO-8601
    params: Dict[str, Any]
    session: Dict[str, Any]                         # market.session_state()
    bars: Dict[str, List[Bar]] = field(default_factory=dict)
    quotes: Dict[str, Quote] = field(default_factory=dict)
    macro: Dict[str, Any] = field(default_factory=dict)       # see agents/macro.py docstring
    insider: List[dict] = field(default_factory=list)         # newsroom insider events (display window)
    calendar: List[dict] = field(default_factory=list)        # newsroom calendar events
    news: Dict[str, List[dict]] = field(default_factory=dict) # symbol -> headlines
    account: Optional[AccountView] = None
    positions: Dict[str, PositionView] = field(default_factory=dict)
    halted: bool = False


@dataclass
class JudgeDecision:
    symbol: str
    action: str                     # LONG|SHORT|FLAT
    confidence: float               # 0..1
    rationale: str
    source: str                     # llm|rules|carried
    rule_score: float = 0.0
    rule_action: str = "FLAT"
    model: Optional[str] = None
    provenance: Optional[dict] = None
    usage: Optional[dict] = None
    carried_from: Optional[int] = None   # recommendation id when carried

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OrderRequest:
    symbol: str
    side: str                       # buy|sell|short|cover
    qty: Optional[float] = None     # whole shares (equities) / fractional, 8 decimals (crypto)
    notional: Optional[float] = None  # USD, converted with the quote
    origin: str = "manual"
    run_id: Optional[int] = None
    recommendation_id: Optional[int] = None
    rationale: Optional[str] = None


@dataclass
class OrderResult:
    order_id: Optional[int]
    symbol: str
    side: str
    qty: float
    status: str                     # filled|pending|rejected|cancelled
    fill_price: Optional[float] = None
    fees: float = 0.0
    notional: Optional[float] = None
    reject_reason: Optional[str] = None
    origin: str = "manual"
    ts: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)
