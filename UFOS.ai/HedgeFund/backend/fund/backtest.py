"""Rule-only backtest over daily bars — fund/SPEC.md §12.

Paper-trading proof of concept: a simulation of past data with simplified
costs. Past (simulated) results say nothing about future results; not
investment advice.

``run_backtest(bars_by_symbol, macro_history, params=None, start=None,
end=None) -> dict``

- ``bars_by_symbol``: {symbol: [Bar | {date, open, high, low, close}]},
  oldest first, adjusted daily bars. Include ≥ 1 year of bars BEFORE
  ``start`` as indicator warm-up (the default start is 5 years before the
  last bar).
- ``macro_history``: list of monthly regime entries (see
  ``build_macro_history``); ``[]`` = macro agent without data (confidence 0).
- ``params``: fund params (merged over params.DEFAULTS). Used: agent_weights
  (technical, macro — sentiment is never used), min_confidence,
  max_position_pct, max_trades_per_day, min_hold_days, allow_shorts,
  not_shortable, fees_bps, borrow_rate_* , starting_capital.

Mechanics (no look-ahead): signals at day t's close from bars ≤ t and the
macro regime known at t → orders fill at day t+1's open with adverse slippage
fees_bps/2 plus a fee of fees_bps/2 of notional (as the paper broker); whole
shares; shorts pay borrow |short MV| × rate/360 per calendar day
(``params.borrow_rate_for``: ETF default / stock default / override); no
dividends, no margin calls, no Rule 201. Sizing = ``sizing.plan_trade`` with
the rule judge (``judge.rule_decision``) and a simplified risk agent (ATR% only
+ short permissions; no calendar, no drawdown proximity, no concentration).

Crypto symbols (``params.is_crypto``, e.g. BTC-USD) are allowed: their bars
run 7 days a week (the day calendar is the union of all symbols' bar dates;
an order for a symbol without a bar that day waits for its next bar),
fractional quantities (8 decimals, $10 minimum per leg except full closes),
``crypto_fees_bps``, crypto sizing (crypto_max_position_pct, 3 % vol target,
crypto ATR thresholds), no borrow fee but perp funding on shorts (3 windows
per calendar day at ``crypto_funding_rate_pct_8h``; positive = received by
the short), no liquidation model. Sharpe uses 365 periods per year when
crypto is in the test (the equity series then has a point every calendar
day), else 252. min_hold_days counts the symbol's own bars.

``build_macro_history(macro_latest, publication_lag_months=1) -> list`` turns
main.py's ``macro_latest`` (the "macro-regime" event's extended view
``regime.extended.trail_long`` — monthly {date, x, y, quadrant} since 2000 —
plus the monthly ``long_history`` of DGS10, T10Y3M, T10Y2Y, VIXCLS,
BAMLH0A0HYM2, FEDFUNDS, PCEPILFE and the derived SAHM_RULE /
REAL_POLICY_RATE) into entries::

    {"date": "YYYY-MM-01",               # reference month
     "available_from": "YYYY-MM-DD",     # first day the reading may be used
     "quadrant": "goldilocks"|"reflation"|"stagflation"|"slowdown",
     "borderline": bool, "x": float|None, "y": float|None,
     "readings": {KEY: float}}           # monthly values of that month

A month's data is complete at its end and published with a lag, so month M
becomes usable from the first day of month M + 1 + publication_lag_months
(default: July -> September 1). ``macro_context_at(history, day)`` gives the
``ctx.macro`` dict (agents/macro.py shape) as of a day.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Dict, List, Optional

from . import indicators as ind
from . import params as P
from .agents import macro as macro_agent
from .agents import technical
from .agents.risk import atr_thresholds
from .broker import round_qty
from .judge import rule_decision
from .sizing import plan_trade
from .types import Bar, RiskAssessment

READING_KEYS = {  # ctx.macro readings key -> macro_latest event id
    "DGS10": "macro-DGS10", "T10Y3M": "macro-T10Y3M", "T10Y2Y": "macro-T10Y2Y", "VIXCLS": "macro-VIXCLS",
    "BAMLH0A0HYM2": "macro-BAMLH0A0HYM2", "FEDFUNDS": "macro-FEDFUNDS", "PCEPILFE": "macro-PCEPILFE",
    "SAHM_RULE": "macro-derived-SAHM_RULE", "REAL_POLICY_RATE": "macro-derived-REAL_POLICY_RATE",
}
DEFAULT_YEARS = 5


# --- macro history ------------------------------------------------------------------

def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    return date(d.year + y, m + 1, 1)


def _month_start(text) -> Optional[date]:
    try:
        return date.fromisoformat(str(text)[:7] + "-01")
    except ValueError:
        return None


def build_macro_history(macro_latest: Optional[dict], publication_lag_months: int = 1) -> List[dict]:
    """See module docstring. Never raises; no regime data -> []."""
    src = macro_latest or {}
    try:
        reg = ((src.get("macro-regime") or {}).get("macro") or {}).get("regime") or {}
    except AttributeError:
        return []
    ext = reg.get("extended") or {}
    trail = ext.get("trail_long") or ext.get("history") or reg.get("trail") or []
    readings: Dict[str, Dict[str, float]] = {}
    for key, event_id in READING_KEYS.items():
        m = (src.get(event_id) or {}).get("macro") or {}
        pts = m.get("long_history") or m.get("history") or []
        by_month = {}
        for p in pts:
            try:
                by_month[str(p["date"])[:7]] = float(p["value"])
            except (KeyError, TypeError, ValueError):
                continue
        if by_month:
            readings[key] = by_month
    out = []
    for p in trail:
        d = _month_start(p.get("date"))
        q = p.get("quadrant")
        if d is None or q not in macro_agent.REGIME_LABELS:
            continue
        x, y = p.get("x"), p.get("y")
        borderline = (x is not None and abs(float(x)) < 0.1) or (y is not None and abs(float(y)) < 0.1)
        month = d.isoformat()[:7]
        out.append({"date": d.isoformat(),
                    "available_from": _add_months(d, 1 + int(publication_lag_months)).isoformat(),
                    "quadrant": q, "borderline": bool(borderline), "x": x, "y": y,
                    "readings": {k: v[month] for k, v in readings.items() if month in v}})
    out.sort(key=lambda e: e["date"])
    return out


def _prepare_history(history: List[dict]) -> List[dict]:
    hist = sorted([dict(h) for h in history or [] if h.get("quadrant") in macro_agent.REGIME_LABELS],
                  key=lambda h: h["date"])
    run, prev = 0, None
    for h in hist:
        if not h.get("available_from"):
            d = _month_start(h["date"])
            h["available_from"] = _add_months(d, 2).isoformat() if d else h["date"]
        run = run + 1 if h["quadrant"] == prev else 1
        prev = h["quadrant"]
        h["months_in_regime"] = run
    hist.sort(key=lambda h: h["available_from"])
    return hist


def macro_context_at(history: List[dict], day: str) -> dict:
    """ctx.macro (agents/macro.py shape) as known on ``day`` ('YYYY-MM-DD')."""
    hist = history if history and "months_in_regime" in history[0] else _prepare_history(history)
    cur = None
    for h in hist:
        if h["available_from"] <= day:
            cur = h
        else:
            break
    return _ctx_from_entry(cur)


def _ctx_from_entry(cur: Optional[dict]) -> dict:
    if cur is None:
        return {"available": False, "as_of": None, "regime": None, "readings": {}}
    return {"available": True, "as_of": cur["date"],
            "regime": {"quadrant": cur["quadrant"], "label": macro_agent.REGIME_LABELS[cur["quadrant"]],
                       "date": cur["date"], "borderline": bool(cur.get("borderline")),
                       "months_in_regime": int(cur.get("months_in_regime") or 1),
                       "x": cur.get("x"), "y": cur.get("y")},
            "readings": {k: {"value": float(v), "date": cur["date"], "monthly": True, "unit": ""}
                         for k, v in (cur.get("readings") or {}).items() if v is not None}}


# --- helpers ------------------------------------------------------------------------------

def _bar(b) -> Bar:
    if isinstance(b, Bar):
        return b
    return Bar(date=str(b["date"])[:10], open=float(b["open"]), high=float(b["high"]), low=float(b["low"]),
               close=float(b["close"]), volume=float(b.get("volume") or 0.0))


def _years_before(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year - years)
    except ValueError:  # Feb 29
        return d.replace(year=d.year - years, day=28)


def _metrics(dates: List[str], values: List[float], periods_per_year: int = 252) -> dict:
    if len(values) < 2 or values[0] <= 0:
        return {"total_return_pct": 0.0, "cagr_pct": 0.0, "max_drawdown_pct": 0.0, "sharpe": None}
    total = values[-1] / values[0] - 1.0
    days = (date.fromisoformat(dates[-1]) - date.fromisoformat(dates[0])).days
    cagr = (values[-1] / values[0]) ** (365.25 / days) - 1.0 if days > 0 and values[-1] > 0 else None
    sh = ind.sharpe(ind.returns(values), periods_per_year)
    return {"total_return_pct": round(total * 100, 2), "cagr_pct": round(cagr * 100, 2) if cagr is not None else None,
            "max_drawdown_pct": round(ind.max_drawdown(values) * 100, 2),
            "sharpe": round(sh, 2) if sh is not None else None}


def _risk(symbol: str, atrp: Optional[float], params: dict) -> RiskAssessment:
    ra = RiskAssessment(symbol=symbol)
    if not params.get("allow_shorts", True) or symbol in {s.upper() for s in params.get("not_shortable") or []}:
        ra.veto_short = True
        ra.reasons.append("shorts not allowed")
    if params.get("risk_enabled", True) and atrp is not None:
        high, elevated = atr_thresholds(symbol)
        if atrp > high:
            ra.size_multiplier = 0.25
        elif atrp > elevated:
            ra.size_multiplier = 0.5
    return ra


def _qty(symbol: str, x: float) -> float:
    """Rounded toward zero: whole shares / 8 decimals for crypto."""
    sign = 1 if x > 0 else -1 if x < 0 else 0
    return sign * round_qty(symbol, abs(x))


# --- the backtest -----------------------------------------------------------------------------

def run_backtest(bars_by_symbol: Dict[str, list], macro_history: Optional[List[dict]], params: Optional[dict] = None,
                 start=None, end=None) -> dict:
    params = P.merge(None, params or {})
    weights = dict(params.get("agent_weights") or {})
    weights["sentiment"] = 0.0
    params = dict(params, agent_weights=weights)
    capital = float(params.get("starting_capital") or 100000.0)
    notes: List[str] = []

    def half_of(sym: str) -> float:
        return P.fees_bps_for(sym, params) / 2.0 / 1e4
    funding_8h = float(params.get("crypto_funding_rate_pct_8h") or 0.0) / 100.0

    bars: Dict[str, List[Bar]] = {}
    for sym, raw in (bars_by_symbol or {}).items():
        lst = sorted((_bar(b) for b in raw or []), key=lambda b: b.date)
        if len(lst) >= 2:
            bars[str(sym).upper()] = lst
        else:
            notes.append(f"{sym}: no price data — skipped.")
    if not bars:
        return {"error": "no price data", "symbols": [], "notes": notes}
    last_date = max(b[-1].date for b in bars.values())
    end_s = str(end)[:10] if end else last_date
    start_s = str(start)[:10] if start else _years_before(date.fromisoformat(end_s), DEFAULT_YEARS).isoformat()
    cal = sorted({b.date for lst in bars.values() for b in lst if start_s <= b.date <= end_s})
    if len(cal) < 2:
        return {"error": "not enough bars in the requested period", "symbols": sorted(bars), "start": start_s,
                "end": end_s, "notes": notes}
    symbols = list(bars)
    has_crypto = any(P.is_crypto(s) for s in symbols)
    idx = {s: {b.date: i for i, b in enumerate(bars[s])} for s in symbols}
    for s in symbols:
        warm = sum(1 for b in bars[s] if b.date < cal[0])
        if warm < 200:
            notes.append(f"{s}: only {warm} warm-up bars before the start — early signals use less history.")

    hist = _prepare_history(macro_history or [])
    if not hist:
        notes.append("No macro regime history — the macro agent had no data (confidence 0).")

    cash = capital
    qty: Dict[str, float] = {s: 0 for s in symbols}
    avg = {s: 0.0 for s in symbols}
    opened_i = {s: None for s in symbols}      # the symbol's own bar index at the opening fill
    last_close = {s: None for s in symbols}
    prev_action = {s: None for s in symbols}
    pending: Dict[str, float] = {}
    pending_equity = capital
    stats = {s: {"symbol": s, "trades": 0, "closed_trades": 0, "wins": 0, "realized_pnl": 0.0, "fees": 0.0,
                 "borrow_fees": 0.0, "funding": 0.0, "days_long": 0, "days_short": 0} for s in symbols}
    equity_series: List[float] = []
    bench_series: List[float] = []
    exposure_days = 0
    gross_sum = 0.0
    trades_total = 0
    hist_pos = 0
    macro_ctx = _ctx_from_entry(None)
    bench_shares: Dict[str, float] = {}
    prev_day: Optional[str] = None

    for i, day in enumerate(cal):
        # 1) fills at today's open (orders from yesterday's close)
        executed_today = 0
        for s in list(pending):
            j = idx[s].get(day)
            if j is None:
                continue            # no bar today (e.g. an equity on a weekend): waits for its next bar
            tw = pending.pop(s)
            price = bars[s][j].open
            if price <= 0:
                continue
            crypto = P.is_crypto(s)
            half = half_of(s)
            want = _qty(s, tw * pending_equity / price)
            diff = want - qty[s]
            if abs(diff) < 1e-9:
                continue
            legs = []
            if qty[s] != 0 and want != 0 and (qty[s] > 0) != (want > 0):
                legs = [-qty[s], want]
            else:
                legs = [round(diff, P.CRYPTO_QTY_DECIMALS) if crypto else diff]
            if crypto:      # $10 minimum per leg, full closes always allowed
                legs = [leg for leg in legs if abs(leg) * price >= P.CRYPTO_MIN_NOTIONAL
                        or abs(qty[s] + leg) < 1e-9]
            for leg in legs:
                fill = price * (1 + half) if leg > 0 else price * (1 - half)
                fee = abs(leg) * fill * half
                cash -= leg * fill + fee
                st = stats[s]
                st["fees"] += fee
                st["trades"] += 1
                trades_total += 1
                executed_today += 1
                old = qty[s]
                if old != 0 and (old > 0) != (leg > 0):          # reduces / closes
                    closed = min(abs(leg), abs(old))
                    pnl = closed * (fill - avg[s]) * (1 if old > 0 else -1)
                    st["realized_pnl"] += pnl
                    st["closed_trades"] += 1
                    st["wins"] += 1 if pnl > 0 else 0
                    new = old + leg
                    if crypto:
                        new = round(new, P.CRYPTO_QTY_DECIMALS)
                    if abs(new) < 1e-9:
                        new = 0
                        avg[s], opened_i[s] = 0.0, None
                    qty[s] = new
                else:                                             # opens / adds
                    new = old + leg
                    if crypto:
                        new = round(new, P.CRYPTO_QTY_DECIMALS)
                    avg[s] = (avg[s] * abs(old) + fill * abs(leg)) / abs(new)
                    if old == 0:
                        opened_i[s] = j
                    qty[s] = new
        # orders of symbols without a bar today (an equity on a weekend in a crypto mix) keep
        # waiting for their next bar unless tonight's signal of that symbol replaces them

        # 2) mark to market at the close, borrow for shorts
        for s in symbols:
            j = idx[s].get(day)
            if j is not None:
                last_close[s] = bars[s][j].close
        days_elapsed = (date.fromisoformat(day) - date.fromisoformat(prev_day)).days if prev_day else 1
        for s in symbols:
            if qty[s] < 0 and last_close[s]:
                if P.is_crypto(s):
                    # perp funding: 3 windows per calendar day; positive rate = the short receives it
                    fund = abs(qty[s]) * last_close[s] * funding_8h * 3 * days_elapsed
                    cash += fund
                    stats[s]["funding"] += fund
                    continue
                fee = abs(qty[s]) * last_close[s] * P.borrow_rate_for(s, params) / 100.0 / 360.0 * days_elapsed
                cash -= fee
                stats[s]["borrow_fees"] += fee
        long_mv = sum(qty[s] * last_close[s] for s in symbols if qty[s] > 0 and last_close[s])
        short_mv = sum(-qty[s] * last_close[s] for s in symbols if qty[s] < 0 and last_close[s])
        equity = cash + long_mv - short_mv
        equity_series.append(equity)
        gross = long_mv + short_mv
        if gross > 0:
            exposure_days += 1
        gross_sum += gross / equity if equity > 0 else 0.0
        for s in symbols:
            if qty[s] > 0:
                stats[s]["days_long"] += 1
            elif qty[s] < 0:
                stats[s]["days_short"] += 1

        # equal-weight buy & hold benchmark: bought at the first close, fees once
        if i == 0:
            avail = [s for s in symbols if last_close[s]]
            for s in avail:
                bench_shares[s] = capital / len(avail) * (1 - 2 * half_of(s)) / last_close[s]
        bench_series.append(sum(n * last_close[s] for s, n in bench_shares.items() if last_close[s]))

        # 3) signals at the close -> orders for tomorrow's open
        while hist_pos < len(hist) and hist[hist_pos]["available_from"] <= day:
            hist_pos += 1
        macro_ctx = _ctx_from_entry(hist[hist_pos - 1] if hist_pos else None)
        if i < len(cal) - 1:
            planned = {"crypto": 0, "equity": 0}      # daily trade counter per asset class (as live)
            ctx = SimpleNamespace(now=day, macro=macro_ctx, params=params)
            for s in symbols:
                j = idx[s].get(day)
                if j is None:
                    continue
                pending.pop(s, None)        # re-decided tonight
                window = bars[s][max(0, j + 1 - technical.window_for(s)):j + 1]
                sigs = {}
                if weights.get("technical", 0) > 0:
                    sigs["technical"] = technical.evaluate_bars(window, s)
                if weights.get("macro", 0) > 0:
                    sigs["macro"] = macro_agent.evaluate(ctx, s)
                atrp = ind.atr_pct(window[-60:], 14)
                risk = _risk(s, atrp, params)
                dec = rule_decision(s, sigs, risk, prev_action[s], params)
                prev_action[s] = dec.action
                w = qty[s] * last_close[s] / equity if equity > 0 and last_close[s] else 0.0
                target, _reason = plan_trade(dec, risk, {"weight": w}, atrp, params, day, planned[P.asset_type(s)],
                                             enforce_min_hold=False)
                if target is None:
                    continue
                reduces = qty[s] != 0 and ((target > 0) != (qty[s] > 0) or abs(target) < abs(w))
                min_hold = int(params.get("min_hold_days") or 0)
                if reduces and min_hold > 0 and opened_i[s] is not None and j - opened_i[s] < min_hold:
                    continue
                pending[s] = target
                planned[P.asset_type(s)] += 2 if qty[s] != 0 and target != 0 and (target > 0) != (qty[s] > 0) else 1
            pending_equity = equity
        prev_day = day

    # --- results ---
    periods = 365 if has_crypto else 252
    port = _metrics(cal, equity_series, periods)
    closed = sum(st["closed_trades"] for st in stats.values())
    wins = sum(st["wins"] for st in stats.values())
    port.update({"trades": trades_total, "hit_rate_pct": round(wins / closed * 100, 1) if closed else None,
                 "exposure_pct": round(exposure_days / len(cal) * 100, 1),
                 "avg_gross_exposure_pct": round(gross_sum / len(cal) * 100, 2),
                 "final_equity": round(equity_series[-1], 2), "starting_capital": capital})
    bh = {}
    per_symbol = []
    for s in symbols:
        pts = [(b.date, b.close) for b in bars[s] if cal[0] <= b.date <= cal[-1]]
        m = _metrics([d for d, _ in pts], [c for _, c in pts], 365 if P.is_crypto(s) else 252) \
            if len(pts) >= 2 else None
        bh[s] = m
        st = stats[s]
        per_symbol.append({
            "symbol": s, "trades": st["trades"], "closed_trades": st["closed_trades"],
            "hit_rate_pct": round(st["wins"] / st["closed_trades"] * 100, 1) if st["closed_trades"] else None,
            "realized_pnl": round(st["realized_pnl"], 2), "fees": round(st["fees"], 2),
            "borrow_fees": round(st["borrow_fees"], 2), "funding": round(st["funding"], 2),
            "days_long": st["days_long"], "days_short": st["days_short"],
            "asset_class": P.asset_type(s),
            "final_qty": qty[s], "buy_and_hold_return_pct": m["total_return_pct"] if m else None})
    ew = _metrics(cal, bench_series, periods)
    weekly = []
    for k, day in enumerate(cal):
        nxt = cal[k + 1] if k + 1 < len(cal) else None
        if nxt is None or date.fromisoformat(nxt).isocalendar()[:2] != date.fromisoformat(day).isocalendar()[:2]:
            weekly.append({"date": day, "equity": round(equity_series[k], 2),
                           "benchmark": round(bench_series[k], 2)})
    notes += [
        "Rule-only simulation: technical + macro agents and the rule judge; no sentiment and no LLM "
        "(not reproducible and could know the outcome).",
        "Signals at the close, fills at the next open; fees_bps per fill (half slippage, half fee); "
        "short borrow at the default rates; no dividends, no margin calls, no Rule 201.",
        "Simplified risk agent: ATR% sizing and short permissions only (no calendar, drawdown-proximity or "
        "concentration rules, no drawdown stop).",
        "Macro regime of month M is used from the first day of month M+2 (publication lag); "
        "today's revised data is used for past months — a mild look-ahead the live loop doesn't have.",
        "Simulated past results; not indicative of future results and not investment advice.",
    ]
    if has_crypto:
        notes.insert(-1, "Crypto: 7-day bars, fractional quantities, crypto_fees_bps, perp funding on shorts "
                         "(no borrow fee, no liquidation model); equities fill on their own trading days.")
    return {
        "symbols": symbols, "start": cal[0], "end": cal[-1], "days": len(cal),
        "portfolio": port,
        "benchmark": {"buy_and_hold": bh, "equal_weight": ew},
        "per_symbol": per_symbol,
        "equity": weekly,
        "params_used": {k: params.get(k) for k in ("agent_weights", "min_confidence", "max_position_pct",
                                                   "max_trades_per_day", "min_hold_days", "allow_shorts",
                                                   "fees_bps", "starting_capital", "crypto_fees_bps",
                                                   "crypto_max_position_pct", "crypto_funding_rate_pct_8h")},
        "notes": notes,
    }
