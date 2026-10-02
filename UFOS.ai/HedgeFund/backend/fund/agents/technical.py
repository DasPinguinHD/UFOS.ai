"""Technical agent (rules, deterministic) — fund/SPEC.md §7.

``evaluate(ctx, symbol) -> AgentSignal`` reads only ``ctx.bars[symbol]``
(daily bars, oldest first; at least MIN_BARS, else confidence 0 "not enough
data"). Intraday quotes are deliberately NOT used, so the score only moves
with the daily bars (fewer spurious re-judgements).

Components (score in [-1, 1], + = bullish):
- trend: sign(close − SMA50), sign(SMA50 − SMA200), sign(close − SMA200)
  averaged (SMA200 terms only with ≥ 200 bars);
- momentum: 0.5·tanh(r20 / 5 %) + 0.5·tanh(r60 / 10 %) (20- and 60-day returns);
- raw = 0.55·trend + 0.45·momentum, + 0.1·(2·p − 1) with p = position of the
  close within the 52-week (252-bar) low..high range;
- RSI14 > 70 dampens a long score (×0.6), RSI14 < 30 dampens a short score;
- confidence: 0.35 + 0.3·[trend and momentum agree] + 0.35·min(1, |score|/0.6),
  lowered by volatility (ATR% above 2 %) and with < 200 bars.

Crypto (``params.is_crypto``) has daily bars 7 days a week (365 per year):
the 52-week range uses 365 bars (``window_for``), the SMA50/200 and the
20/60-day returns count bars = calendar days (the usual convention for
crypto charts), and the volatility dampening of the confidence starts at 4 %
daily ATR instead of 2 % (crypto's normal range is about twice as wide; the
sizing vol target is doubled too).

A reading aid for a paper-trading proof of concept — not investment advice.
"""
from __future__ import annotations

import math

from .. import indicators as ind
from .. import params as fund_params
from ..types import AgentSignal

AGENT = "technical"
MIN_BARS = 60
WINDOW = 260          # bars used (52 weeks + margin); keeps the backtest fast
CRYPTO_WINDOW = 370   # crypto: 365 bars per 52 weeks (+ margin)
YEAR_BARS = 252
CRYPTO_YEAR_BARS = 365


def window_for(symbol: str) -> int:
    return CRYPTO_WINDOW if fund_params.is_crypto(symbol) else WINDOW


def _sign(x: float) -> float:
    return 1.0 if x > 0 else -1.0 if x < 0 else 0.0


def _r(x, digits=4):
    return None if x is None else round(float(x), digits)


def evaluate_bars(bars, symbol: str) -> AgentSignal:
    """The agent on a plain bar list (used by evaluate() and the backtest)."""
    crypto = fund_params.is_crypto(symbol)
    year = CRYPTO_YEAR_BARS if crypto else YEAR_BARS
    bars = list(bars or [])[-window_for(symbol):]
    if len(bars) < MIN_BARS:
        return AgentSignal(agent=AGENT, symbol=symbol, score=0.0, confidence=0.0,
                           summary=f"Not enough data ({len(bars)} of {MIN_BARS} daily bars).",
                           details={"bars": len(bars)}, source="rules")
    closes = [float(b.close) for b in bars]
    close = closes[-1]
    sma50 = ind.sma(closes, 50)
    sma200 = ind.sma(closes, 200)
    terms = [_sign(close - sma50)]
    if sma200 is not None:
        terms += [_sign(sma50 - sma200), _sign(close - sma200)]
    trend = sum(terms) / len(terms)

    r20 = ind.pct_change(closes, 20) or 0.0
    r60 = ind.pct_change(closes, min(60, len(closes) - 1)) or 0.0
    momentum = 0.5 * math.tanh(r20 / 0.05) + 0.5 * math.tanh(r60 / 0.10)

    raw = 0.55 * trend + 0.45 * momentum
    highs = [float(b.high) for b in bars[-year:]]
    lows = [float(b.low) for b in bars[-year:]]
    hi, lo = max(highs), min(lows)
    pos = (close - lo) / (hi - lo) if hi > lo else 0.5
    raw += 0.1 * (2 * pos - 1)

    rsi14 = ind.rsi(closes, 14)
    rsi_note = None
    if rsi14 is not None and rsi14 > 70 and raw > 0:
        raw *= 0.6
        rsi_note = "overbought"
    elif rsi14 is not None and rsi14 < 30 and raw < 0:
        raw *= 0.6
        rsi_note = "oversold"
    score = ind.clamp(raw, -1.0, 1.0)

    atrp = ind.atr_pct(bars, 14)
    agree = _sign(trend) == _sign(momentum) and _sign(trend) != 0
    conf = 0.35 + (0.3 if agree else 0.0) + 0.35 * min(1.0, abs(score) / 0.6)
    calm = 0.04 if crypto else 0.02
    if atrp is not None and atrp > calm:
        conf *= max(0.4, 1.0 - (atrp - calm) * 15.0)
    if sma200 is None:
        conf *= 0.85
    conf = ind.clamp(conf, 0.0, 1.0)

    if trend > 0.3:
        t_text = "uptrend" + (" above SMA50/200" if sma200 is not None and trend >= 0.99 else "")
    elif trend < -0.3:
        t_text = "downtrend" + (" below SMA50/200" if sma200 is not None and trend <= -0.99 else "")
    else:
        t_text = "no clear trend"
    summary = (f"{t_text[0].upper() + t_text[1:]}, 20d {r20 * 100:+.1f}% / 60d {r60 * 100:+.1f}%"
               + (f", RSI {rsi14:.0f}" if rsi14 is not None else "")
               + (f" ({rsi_note})" if rsi_note else "")
               + (f", ATR {atrp * 100:.1f}%" if atrp is not None else "") + ".")
    details = {
        "close": _r(close, 4), "sma50": _r(sma50, 4), "sma200": _r(sma200, 4),
        "trend": _r(trend, 3), "momentum": _r(momentum, 3),
        "return_20d_pct": _r(r20 * 100, 2), "return_60d_pct": _r(r60 * 100, 2),
        "rsi14": _r(rsi14, 1), "atr_pct": _r(atrp * 100 if atrp is not None else None, 2),
        "from_52w_high_pct": _r((close / hi - 1) * 100, 2) if hi > 0 else None,
        "from_52w_low_pct": _r((close / lo - 1) * 100, 2) if lo > 0 else None,
        "bars": len(bars),
    }
    return AgentSignal(agent=AGENT, symbol=symbol, score=round(score, 4), confidence=round(conf, 4),
                       summary=summary, details=details, source="rules")


def evaluate(ctx, symbol: str) -> AgentSignal:
    return evaluate_bars((ctx.bars or {}).get(symbol) or [], symbol)
