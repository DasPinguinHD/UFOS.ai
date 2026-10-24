"""Backtest metrics (SPEC §8.4, notes §8.10) computed from an
``engine.run(...)`` result (``BacktestResult`` or its ``to_dict()``).

Units: returns, drawdowns, exposures and IC are FRACTIONS (0.12 = 12 %);
money in the account currency (USD); durations in simulated days (NYSE
sessions or UTC days). A metric that cannot be computed is ``None`` and its
reason is listed in ``unavailable`` under ``"<group>.<name>"`` — never a
made-up value (no unrequested fallbacks).

Annualisation by the run's calendar: P = 252 (NYSE sessions) or 365 (a
crypto-only run on UTC days) periods per year.

Daily returns: r_j = E_j / E_(j-1) - 1 over consecutive VALID equity points
(a day whose valuation was paused has no equity; the return then spans the
gap — counted in ``valuation_gaps``).

Formulas (E = equity series, r = daily returns, n = len(r), rb = benchmark
returns on the same days):
- total_return = E_last / E_first - 1;
  years = (index of last valid point - index of first) / P;
  cagr = (1 + total_return) ** (1 / years) - 1 (total_return <= -1 -> -1);
- volatility = std(r, ddof=1) * sqrt(P);
- sharpe = mean(r) / std(r, ddof=1) * sqrt(P) — excess over a ZERO rate (the
  platform has no risk-free series yet; stated, not a stand-in);
- sortino = mean(r) / sqrt(mean(min(r, 0) ** 2)) * sqrt(P) (target 0, the
  downside deviation over ALL days);
- max_drawdown = max over t of 1 - E_t / max(E_<=t); duration = the longest
  stretch (days) from a peak until equity is back at that peak (or the run
  end, ``recovered`` false); calmar = cagr / max_drawdown;
- hit_rate_daily = days with P&L > 0 / days with P&L != 0;
  profit_factor_daily = sum of positive daily P&L / |sum of negative daily
  P&L|;
- per trade (``trades()``, user decision 2026-10-04): the strategy
  account's fills matched FIFO per symbol. Every opening fill (or the
  opening remainder of a fill that flips the position) is one trade lot,
  long or short; closing fills consume the oldest lots first (partial
  closes and scale-ins handled). A lot is a CLOSED trade once fully matched:
  P&L = sum over its matched parts of (exit - entry) x qty x direction -
  its entry fee - the pro-rata exit fees (fill prices already hold the
  slippage; dividends, borrow and funding are account-level and NOT
  attributed). Lots still (partly) open at the end are excluded and counted
  in ``trades_open``. hit_rate_trades = closed trades with P&L > 0 / closed
  trades with P&L != 0; profit_factor_trades = sum of winning trade P&L /
  |sum of losing trade P&L|;
- turnover_annual = (sum |fill notional| / 2) / mean(E) / years (one-sided:
  the share of the book replaced per year);
- avg_gross_exposure = mean((long_mv + short_mv) / E), avg_net_exposure =
  mean((long_mv - short_mv) / E) (short_mv is positive in the broker);
- beta = cov(r, rb) / var(rb) (ddof 1), alpha_annual = (mean(r) - beta *
  mean(rb)) * P (arithmetic, zero rate), correlation; pairs need both
  accounts valued on both days;
- costs from the ledger + fills: fees (kind ``fee``), liquidation_fees,
  borrow (``borrow_fee``), funding (``funding``, positive = paid), slippage
  = per fill qty x |fill price - quote| with quote = fill / (1 +- h), h =
  ``core.params.fees_bps_for(symbol, broker_params)`` / 2 / 10000 (the
  broker books half the per-fill cost as adverse price); total = the sum;
  net_pnl = E_last - capital, gross_pnl = net_pnl + total,
  pct_of_gross_pnl = 100 * total / gross_pnl (only when gross_pnl > 0).

Signal quality (needs ``result.analysis``, the engine's 3d hook):
- cross-sectional (>= IC_MIN_SYMBOLS symbols): for each day t of the run the
  Spearman rank correlation (average ranks for ties) between the signal row
  t and the forward return C[t+h] / C[t] - 1 of the ``all`` close series
  (only t + h within the run; >= IC_MIN_SYMBOLS valid pairs, both sides not
  constant), horizons 1/5/10/21/63; per horizon mean_ic, std_ic (ddof 1),
  icir = mean_ic / std_ic (daily, overlapping windows — not annualised),
  t_stat = mean_ic / std_ic * sqrt(days) (naive: ignores the overlap);
  needs >= IC_MIN_DATES days;
- decay curve: the same IC against the ONE-day return at lag h
  (C[t+h] / C[t+h-1] - 1), lags 1/5/10/21/63; half-life by an exponential
  fit IC(h) = a * exp(-lambda h): OLS of ln|IC(h)| on h over the lags whose
  mean IC has the sign of IC(1) (log undefined after a sign change),
  >= 3 points, half_life = ln 2 / lambda (days); lambda <= 0 -> no half-life
  ("does not decay");
- time-series (fewer than IC_MIN_SYMBOLS symbols): per symbol, rolling
  13-week windows (65 NYSE sessions / 91 UTC days, stepping one day) of the
  Spearman correlation between signal_t and the 1-day forward return; the
  positive share = windows with a correlation > 0 / windows (>= TS_MIN_PAIRS
  valid pairs, not constant).

Capacity: for every filled strategy order in an equity, ratio = fill
notional / (1 % x the 20-session average dollar volume known at the
decision — the previous simulated day, as the engine's limit). Order
notionals scale with capital, so the limit binds on 10 % of the orders at
capital / q90(ratio) (linear quantile). Needs >= CAPACITY_MIN_ORDERS
orders; when >= 10 % of the orders were already cut at the tested capital
(engine flag ``fills[].participation_cut``)
the true ratio of those is unknown -> value None, ``at_most`` = capital.

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

import math
from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple

PERIODS_PER_YEAR = {"nyse": 252, "utc": 365}
IC_HORIZONS = (1, 5, 10, 21, 63)
IC_MIN_SYMBOLS = 5
IC_MIN_DATES = 20
TS_WINDOW = {"nyse": 65, "utc": 91}          # 13 weeks
TS_MIN_PAIRS = 20
HALF_LIFE_MIN_POINTS = 3
CAPACITY_BIND_SHARE = 0.10
CAPACITY_MIN_ORDERS = 10
CAPACITY_AT_LIMIT = 0.999                    # ratio >= this: the order was cut at the tested capital
PARTICIPATION_RATE = 0.01
ADV_WINDOW = 20
STRATEGY_ORIGIN = "autopilot"
SUMMARY_KEYS = ("total_return", "cagr", "sharpe", "max_drawdown", "volatility")


def _field(result, key: str, default=None):
    if isinstance(result, dict):
        return result.get(key, default)
    return getattr(result, key, default)


class _Missing:
    def __init__(self):
        self.reasons: Dict[str, str] = {}

    def __call__(self, group: str, name: str, reason: str) -> None:
        self.reasons[f"{group}.{name}"] = reason
        return None


def _finite(x) -> bool:
    return x is not None and isinstance(x, (int, float)) and math.isfinite(float(x))


def _std(xs: Sequence[float]) -> Optional[float]:
    n = len(xs)
    if n < 2:
        return None
    m = math.fsum(xs) / n
    return math.sqrt(math.fsum((x - m) ** 2 for x in xs) / (n - 1))


def _mean(xs: Sequence[float]) -> Optional[float]:
    return math.fsum(xs) / len(xs) if xs else None


# --- equity / returns ------------------------------------------------------------------------------

def valid_points(equity: Sequence[Optional[float]]) -> List[Tuple[int, float]]:
    return [(i, float(e)) for i, e in enumerate(equity) if _finite(e) and float(e) > 0]


def returns_of(points: List[Tuple[int, float]]) -> List[float]:
    return [points[k][1] / points[k - 1][1] - 1.0 for k in range(1, len(points))]


def drawdown(points: List[Tuple[int, float]], dates: Sequence[str]) -> Optional[dict]:
    """Max drawdown (fraction) + longest underwater stretch, see the module docstring."""
    if not points:
        return None
    peak_i, peak = points[0]
    best = {"max_drawdown": 0.0, "peak": dates[peak_i], "trough": dates[peak_i], "recovery": None}
    longest, longest_info = 0, None
    under_since: Optional[int] = None
    for i, e in points:
        if e >= peak:
            if under_since is not None:
                dur = i - under_since
                if dur > longest:
                    longest, longest_info = dur, {"start": dates[under_since], "end": dates[i], "recovered": True}
                under_since = None
            peak_i, peak = i, e
            continue
        if under_since is None:
            under_since = peak_i
        dd = 1.0 - e / peak
        if dd > best["max_drawdown"]:
            best = {"max_drawdown": dd, "peak": dates[peak_i], "trough": dates[i], "recovery": None}
    if under_since is not None:
        dur = points[-1][0] - under_since
        if dur > longest:
            longest, longest_info = dur, {"start": dates[under_since], "end": dates[points[-1][0]],
                                          "recovered": False}
    if best["max_drawdown"] > 0:
        # recovery of the max-drawdown peak
        pk = dates.index(best["peak"])
        level = next(e for i, e in points if i == pk)
        for i, e in points:
            if i > dates.index(best["trough"]) and e >= level:
                best["recovery"] = dates[i]
                break
    best["max_drawdown_duration_days"] = longest
    best["longest_underwater"] = longest_info
    return best


def performance(result, missing: Optional[_Missing] = None) -> dict:
    missing = missing or _Missing()
    g = "performance"
    calendar = (_field(result, "range") or {}).get("calendar", "nyse")
    P = PERIODS_PER_YEAR[calendar]
    dates = list(_field(result, "dates") or [])
    equity = list(_field(result, "equity") or [])
    pts = valid_points(equity)
    r = returns_of(pts)
    gaps = sum(1 for k in range(1, len(pts)) if pts[k][0] - pts[k - 1][0] > 1)
    out: Dict[str, Any] = {"periods_per_year": P, "returns": len(r), "valuation_gaps": gaps,
                           "start_equity": pts[0][1] if pts else None, "end_equity": pts[-1][1] if pts else None}
    if len(pts) < 2:
        for k in ("total_return", "cagr", "volatility", "sharpe", "sortino", "calmar", "max_drawdown",
                  "hit_rate_daily", "profit_factor_daily", "best_day", "worst_day"):
            out[k] = missing(g, k, "fewer than two valued days")
        out["max_drawdown_duration_days"] = None
        return out
    total = pts[-1][1] / pts[0][1] - 1.0
    years = (pts[-1][0] - pts[0][0]) / P
    out["total_return"] = total
    out["years"] = years
    out["cagr"] = -1.0 if total <= -1.0 else (1.0 + total) ** (1.0 / years) - 1.0
    sd = _std(r)
    m = _mean(r)
    if sd is None:
        out["volatility"] = missing(g, "volatility", "fewer than two daily returns")
        out["sharpe"] = missing(g, "sharpe", "fewer than two daily returns")
    elif sd == 0:
        out["volatility"] = 0.0
        out["sharpe"] = missing(g, "sharpe", "the daily returns have zero variance")
    else:
        out["volatility"] = sd * math.sqrt(P)
        out["sharpe"] = m / sd * math.sqrt(P)
    dd_dev = math.sqrt(math.fsum(min(x, 0.0) ** 2 for x in r) / len(r))
    out["sortino"] = m / dd_dev * math.sqrt(P) if dd_dev > 0 else missing(g, "sortino", "no losing days "
                                                                         "(downside deviation is zero)")
    dd = drawdown(pts, dates)
    out["max_drawdown"] = dd["max_drawdown"]
    out["max_drawdown_peak"], out["max_drawdown_trough"] = dd["peak"], dd["trough"]
    out["max_drawdown_recovery"] = dd["recovery"]
    out["max_drawdown_duration_days"] = dd["max_drawdown_duration_days"]
    out["longest_underwater"] = dd["longest_underwater"]
    out["calmar"] = out["cagr"] / dd["max_drawdown"] if dd["max_drawdown"] > 0 else \
        missing(g, "calmar", "no drawdown in the run")
    pnl = [pts[k][1] - pts[k - 1][1] for k in range(1, len(pts))]
    wins = [x for x in pnl if x > 0]
    losses = [x for x in pnl if x < 0]
    out["hit_rate_daily"] = len(wins) / (len(wins) + len(losses)) if (wins or losses) else \
        missing(g, "hit_rate_daily", "no day with a profit or a loss")
    out["profit_factor_daily"] = math.fsum(wins) / -math.fsum(losses) if losses else \
        missing(g, "profit_factor_daily", "no losing days")
    out["best_day"], out["worst_day"] = max(r), min(r)
    return out


SIDE_SIGN = {"buy": 1.0, "cover": 1.0, "sell": -1.0, "short": -1.0}
QTY_EPS = 1e-9


def match_trades(fills: Sequence[dict]) -> dict:
    """FIFO round trips per symbol (module docstring) -> {closed: [trade], open: [lot]}."""
    lots: Dict[str, List[dict]] = {}
    closed: List[dict] = []
    for f in fills:
        sym = f["symbol"]
        sign = SIDE_SIGN.get(f.get("side"))
        if sign is None:
            raise ValueError(f"fill {f.get('id')}: unknown side {f.get('side')!r}")
        qty, px = abs(float(f["qty"])), float(f["fill_price"])
        fee = abs(float(f.get("fees") or 0.0))
        if qty <= QTY_EPS:
            continue
        book = lots.setdefault(sym, [])
        left = qty
        while left > QTY_EPS and book and book[0]["direction"] != sign:
            lot = book[0]
            m = min(left, lot["remaining"])
            lot["pnl"] += (px - lot["entry_price"]) * m * lot["direction"] - fee * m / qty
            lot["remaining"] -= m
            lot["exit_ts"] = f.get("ts")
            left -= m
            if lot["remaining"] <= QTY_EPS:
                book.pop(0)
                closed.append({k: lot[k] for k in ("symbol", "direction", "qty", "entry_price", "entry_ts",
                                                   "exit_ts", "pnl")})
        if left > QTY_EPS:
            book.append({"symbol": sym, "direction": sign, "qty": left, "remaining": left, "entry_price": px,
                         "entry_ts": f.get("ts"), "exit_ts": None, "pnl": -fee * left / qty})
    open_lots = [dict(lot) for sym in sorted(lots) for lot in lots[sym]]
    return {"closed": closed, "open": open_lots}


def trades(result, missing: _Missing) -> dict:
    g = "performance"
    m = match_trades(_field(result, "fills") or [])
    pnl = [t["pnl"] for t in m["closed"]]
    wins = [x for x in pnl if x > 0]
    losses = [x for x in pnl if x < 0]
    return {"trades_closed": len(pnl), "trades_open": len(m["open"]),
            "hit_rate_trades": len(wins) / (len(wins) + len(losses)) if (wins or losses) else
            missing(g, "hit_rate_trades", "no closed trade with a profit or a loss"),
            "profit_factor_trades": math.fsum(wins) / -math.fsum(losses) if losses else
            missing(g, "profit_factor_trades", "no losing closed trade"),
            "avg_trade_pnl": _mean(pnl) if pnl else missing(g, "avg_trade_pnl", "no closed trade")}


def exposure(result, perf: dict, missing: _Missing) -> dict:
    g = "exposure"
    equity = list(_field(result, "equity") or [])
    lmv, smv = list(_field(result, "long_mv") or []), list(_field(result, "short_mv") or [])
    gross, net = [], []
    for e, lo, sh in zip(equity, lmv, smv):
        if _finite(e) and float(e) > 0 and _finite(lo) and _finite(sh):
            gross.append((float(lo) + float(sh)) / float(e))
            net.append((float(lo) - float(sh)) / float(e))
    out = {"avg_gross_exposure": _mean(gross) if gross else missing(g, "avg_gross_exposure", "no valued day"),
           "avg_net_exposure": _mean(net) if net else missing(g, "avg_net_exposure", "no valued day")}
    traded = math.fsum(abs(float(f.get("notional") or 0.0)) for f in _field(result, "fills") or [])
    out["traded_notional"] = traded
    pts = valid_points(equity)
    years = perf.get("years")
    if not years:
        out["turnover_annual"] = missing(g, "turnover_annual", "the run spans less than two valued days")
    else:
        avg_eq = _mean([e for _i, e in pts])
        out["turnover_annual"] = traded / 2.0 / avg_eq / years
    return out


def benchmark_stats(result, missing: _Missing) -> dict:
    g = "benchmark"
    calendar = (_field(result, "range") or {}).get("calendar", "nyse")
    P = PERIODS_PER_YEAR[calendar]
    info = _field(result, "benchmark_info") or {}
    dates = list(_field(result, "dates") or [])
    bench = list(_field(result, "benchmark") or [])
    equity = list(_field(result, "equity") or [])
    bperf = performance({"range": {"calendar": calendar}, "dates": dates, "equity": bench}, _Missing())
    out = {"symbol": info.get("symbol"), "total_return": bperf.get("total_return"), "cagr": bperf.get("cagr"),
           "volatility": bperf.get("volatility"), "max_drawdown": bperf.get("max_drawdown"),
           "sharpe": bperf.get("sharpe")}
    rs, rb = [], []
    for j in range(1, min(len(equity), len(bench))):
        a0, a1, b0, b1 = equity[j - 1], equity[j], bench[j - 1], bench[j]
        if all(_finite(x) and float(x) > 0 for x in (a0, a1, b0, b1)):
            rs.append(float(a1) / float(a0) - 1.0)
            rb.append(float(b1) / float(b0) - 1.0)
    out["pairs"] = len(rs)
    if len(rs) < 2:
        for k in ("beta", "alpha_annual", "correlation"):
            out[k] = missing(g, k, "fewer than two days with both accounts valued")
        return out
    mb, ms = _mean(rb), _mean(rs)
    var_b = math.fsum((x - mb) ** 2 for x in rb) / (len(rb) - 1)
    var_s = math.fsum((x - ms) ** 2 for x in rs) / (len(rs) - 1)
    cov = math.fsum((x - ms) * (y - mb) for x, y in zip(rs, rb)) / (len(rs) - 1)
    if var_b == 0:
        for k in ("beta", "alpha_annual", "correlation"):
            out[k] = missing(g, k, "the benchmark returns have zero variance")
        return out
    beta = cov / var_b
    out["beta"] = beta
    out["alpha_annual"] = (ms - beta * mb) * P
    out["correlation"] = cov / math.sqrt(var_s * var_b) if var_s > 0 else \
        missing(g, "correlation", "the strategy returns have zero variance")
    return out


def costs(result, perf: dict, missing: _Missing) -> dict:
    from strategy_platform.core import params as fund_params
    g = "costs"
    params = _field(result, "broker_params") or {}
    sums: Dict[str, float] = {}
    for row in _field(result, "ledger") or []:
        sums[row.get("kind")] = sums.get(row.get("kind"), 0.0) + float(row.get("amount") or 0.0)
    slippage = []
    for f in _field(result, "fills") or []:
        fp, qty = float(f["fill_price"]), abs(float(f["qty"]))
        h = fund_params.fees_bps_for(f["symbol"], params) / 2.0 / 10000.0
        if f.get("side") in ("buy", "cover"):
            slippage.append(qty * (fp - fp / (1.0 + h)))
        else:
            slippage.append(qty * (fp / (1.0 - h) - fp))
    out = {"fees": -sums.get("fee", 0.0), "liquidation_fees": -sums.get("liquidation_fee", 0.0),
           "slippage": math.fsum(slippage), "borrow": -sums.get("borrow_fee", 0.0),
           "funding": -sums.get("funding", 0.0),
           "dividends_received": sums.get("dividend", 0.0), "dividends_paid_short": -sums.get("dividend_short", 0.0)}
    out["total"] = math.fsum(out[k] for k in ("fees", "liquidation_fees", "slippage", "borrow", "funding"))
    capital = (_field(result, "request") or {}).get("capital")
    end = perf.get("end_equity")
    if not _finite(capital) or end is None:
        out["net_pnl"] = missing(g, "net_pnl", "no valued day")
        out["gross_pnl"] = missing(g, "gross_pnl", "no valued day")
        out["pct_of_gross_pnl"] = missing(g, "pct_of_gross_pnl", "no valued day")
        return out
    out["net_pnl"] = end - float(capital)
    out["gross_pnl"] = out["net_pnl"] + out["total"]
    out["pct_of_gross_pnl"] = 100.0 * out["total"] / out["gross_pnl"] if out["gross_pnl"] > 0 else \
        missing(g, "pct_of_gross_pnl", "the gross P&L (before costs) is not positive — a share of it is not "
                                       "meaningful")
    return out


# --- signal quality ------------------------------------------------------------------------------------

def rank(values):
    """Average ranks (1..n) of a 1-d array; ties share their mean rank."""
    import numpy as np
    a = np.asarray(values, dtype=float)
    order = np.argsort(a, kind="mergesort")
    ranks = np.empty(len(a), dtype=float)
    sa = a[order]
    i = 0
    n = len(a)
    while i < n:
        j = i
        while j + 1 < n and sa[j + 1] == sa[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return ranks


def spearman(x, y, min_pairs: int) -> Optional[float]:
    """Spearman correlation over the finite pairs; None when < min_pairs or a side is constant."""
    import numpy as np
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    ok = np.isfinite(x) & np.isfinite(y)
    if int(ok.sum()) < min_pairs:
        return None
    rx, ry = rank(x[ok]), rank(y[ok])
    rx -= rx.mean()
    ry -= ry.mean()
    den = math.sqrt(float((rx * rx).sum()) * float((ry * ry).sum()))
    if den == 0:
        return None
    return float((rx * ry).sum()) / den


def _ic_stats(ics: List[float]) -> dict:
    n = len(ics)
    out = {"days": n, "mean_ic": None, "std_ic": None, "icir": None, "t_stat": None, "reason": None}
    if n < IC_MIN_DATES:
        out["reason"] = f"only {n} day(s) with a cross-sectional IC (needs {IC_MIN_DATES})"
        return out
    m, sd = _mean(ics), _std(ics)
    out["mean_ic"], out["std_ic"] = m, sd
    if sd == 0:
        out["reason"] = "the daily IC has zero variance"
        return out
    out["icir"] = m / sd
    out["t_stat"] = m / sd * math.sqrt(n)
    return out


def half_life(lags: Sequence[float], ics: Sequence[Optional[float]]) -> Tuple[Optional[float], Optional[str], dict]:
    """(half_life, reason_if_none, fit) — exponential fit by OLS of ln|IC| on the lag (module docstring)."""
    pts = [(float(h), float(v)) for h, v in zip(lags, ics) if v is not None and math.isfinite(v)]
    if not pts or pts[0][1] == 0:
        return None, "no IC at the first lag", {}
    sign = 1.0 if pts[0][1] > 0 else -1.0
    use = [(h, math.log(v * sign)) for h, v in pts if v * sign > 0]
    if len(use) < HALF_LIFE_MIN_POINTS:
        return None, f"fewer than {HALF_LIFE_MIN_POINTS} lags with an IC of the first lag's sign", {}
    mh = _mean([h for h, _ in use])
    my = _mean([y for _, y in use])
    sxx = math.fsum((h - mh) ** 2 for h, _ in use)
    sxy = math.fsum((h - mh) * (y - my) for h, y in use)
    slope = sxy / sxx
    lam = -slope
    fit = {"lambda": lam, "amplitude": sign * math.exp(my - slope * mh), "points": len(use)}
    if lam <= 0:
        return None, "the IC does not decay over the measured lags", fit
    return math.log(2.0) / lam, None, fit


def signal_quality(result, analysis: Optional[dict] = None) -> dict:
    import numpy as np
    analysis = analysis if analysis is not None else getattr(result, "analysis", None)
    if not analysis:
        return {"mode": None, "reason": "the signal panel is not available (engine result without the "
                                        "analysis hook)"}
    S = np.asarray(analysis["signal"], dtype=float)
    C = np.asarray(analysis["close_all"], dtype=float)
    C = np.where(C > 0, C, np.nan)
    i0, i1 = int(analysis["i0"]), int(analysis["i1"])
    nsym = S.shape[1]
    calendar = analysis.get("calendar", "nyse")
    if nsym >= IC_MIN_SYMBOLS:
        horizons, decay = [], []
        for h in IC_HORIZONS:
            ics = []
            for t in range(i0, i1 - h + 1):
                v = spearman(S[t], C[t + h] / C[t] - 1.0, IC_MIN_SYMBOLS)
                if v is not None:
                    ics.append(v)
            horizons.append(dict(_ic_stats(ics), horizon=h))
            lag_ics = []
            for t in range(i0, i1 - h + 1):
                v = spearman(S[t], C[t + h] / C[t + h - 1] - 1.0, IC_MIN_SYMBOLS)
                if v is not None:
                    lag_ics.append(v)
            st = _ic_stats(lag_ics)
            decay.append({"lag": h, "mean_ic": st["mean_ic"], "days": st["days"], "reason": st["reason"]})
        hl, why, fit = half_life([d["lag"] for d in decay], [d["mean_ic"] for d in decay])
        return {"mode": "cross_sectional", "symbols": nsym, "horizons": horizons, "decay": decay,
                "half_life_days": hl, "half_life_reason": why, "half_life_fit": fit}
    W = TS_WINDOW[calendar]
    per = []
    tot_pos = tot_win = 0
    all_corrs = []
    for j, sym in enumerate(analysis["symbols"]):
        fwd = np.full(S.shape[0], np.nan)
        fwd[:-1] = C[1:, j] / C[:-1, j] - 1.0
        corrs, skipped = [], 0
        for end in range(i0 + W - 1, i1):            # pairs t..end, return t -> t+1 <= i1
            v = spearman(S[end - W + 1:end + 1, j], fwd[end - W + 1:end + 1], TS_MIN_PAIRS)
            if v is None:
                skipped += 1
            else:
                corrs.append(v)
        pos = sum(1 for v in corrs if v > 0)
        per.append({"symbol": sym, "windows": len(corrs), "skipped": skipped,
                    "positive_share": pos / len(corrs) if corrs else None,
                    "mean_corr": _mean(corrs) if corrs else None})
        tot_pos += pos
        tot_win += len(corrs)
        all_corrs += corrs
    out = {"mode": "time_series", "symbols": nsym, "window_days": W, "windows": tot_win,
           "positive_share": tot_pos / tot_win if tot_win else None,
           "mean_corr": _mean(all_corrs) if all_corrs else None, "per_symbol": per,
           "reason": None if tot_win else (f"no 13-week window with {TS_MIN_PAIRS} valid, non-constant "
                                           "signal/return pairs")}
    return out


# --- capacity ------------------------------------------------------------------------------------------

def _ny_date(ts: str) -> str:
    from datetime import datetime
    from ..core import calendars
    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    return dt.astimezone(calendars.NY).date().isoformat()


def capacity(result, analysis: Optional[dict] = None) -> dict:
    import numpy as np
    analysis = analysis if analysis is not None else getattr(result, "analysis", None)
    capital = (_field(result, "request") or {}).get("capital")
    base = {"capital": capital, "capacity_usd": None, "at_most": None, "orders": 0, "orders_at_limit": 0,
            "orders_without_volume": 0, "q90_ratio": None, "reason": None}
    if not _finite(capital) or float(capital) <= 0:
        base["reason"] = "the result has no tested capital (request.capital)"
        return base
    capital = float(capital)
    if not analysis:
        base["reason"] = "the volume data is not available (engine result without the analysis hook)"
        return base
    dv = analysis.get("dollar_volume") or {}
    crypto = set(analysis.get("crypto") or [])
    pdates = list(analysis["dates"])
    pidx = {d: k for k, d in enumerate(pdates)}
    sim = list(_field(result, "dates") or [])
    sidx = {d: k for k, d in enumerate(sim)}
    ratios, flags = [], []
    for f in _field(result, "fills") or []:
        sym = f["symbol"]
        if f.get("origin") != STRATEGY_ORIGIN or sym in crypto:
            continue
        k = sidx.get(_ny_date(f["ts"]))
        if not k:
            base["orders_without_volume"] += 1
            continue
        i = pidx.get(sim[k - 1])
        vols = dv.get(sym) or {}
        window = pdates[i + 1 - ADV_WINDOW:i + 1] if i is not None and i + 1 >= ADV_WINDOW else []
        vals = [vols.get(d) for d in window]
        if not window or any(v is None for v in vals) or math.fsum(vals) <= 0:
            base["orders_without_volume"] += 1
            continue
        adv = math.fsum(vals) / ADV_WINDOW
        if "participation_cut" not in f:
            raise ValueError(f"fill {f.get('id')}: no participation_cut flag (engine result older than 3d)")
        ratios.append(abs(float(f["notional"])) / (PARTICIPATION_RATE * adv))
        flags.append(bool(f["participation_cut"]))
    base["orders"] = len(ratios)
    if not ratios:
        base["reason"] = ("no filled equity orders with volume data (crypto has no participation limit, user "
                          "decision 2026-10-03)")
        return base
    # the engine flags orders it cut (their notional is rounded DOWN to whole shares, so a ratio test
    # would miss them — review 2026-10-04); ratio >= CAPACITY_AT_LIMIT also counts as at the limit
    at = sum(1 for r, cut in zip(ratios, flags) if cut or r >= CAPACITY_AT_LIMIT)
    base["orders_at_limit"] = at
    if len(ratios) < CAPACITY_MIN_ORDERS:
        base["reason"] = f"only {len(ratios)} equity order(s) — needs {CAPACITY_MIN_ORDERS} for a 10 % quantile"
        return base
    q90 = float(np.quantile(np.asarray(ratios), 1.0 - CAPACITY_BIND_SHARE))
    base["q90_ratio"] = q90
    if at >= CAPACITY_BIND_SHARE * len(ratios):
        base["at_most"] = capital
        base["reason"] = (f"the 1 % limit already cut {at} of {len(ratios)} orders at the tested capital — "
                          "capacity is at most the tested capital")
        return base
    base["capacity_usd"] = capital / q90 if q90 > 0 else None
    if q90 <= 0:
        base["reason"] = "all order notionals are zero"
    return base


# --- entry point ---------------------------------------------------------------------------------------

def compute(result, *, analysis: Optional[dict] = None, include_signal: bool = True) -> dict:
    """All §8.4 metrics of one engine result (see the module docstring)."""
    missing = _Missing()
    perf = performance(result, missing)
    perf.update(trades(result, missing))
    out = {"calendar": (_field(result, "range") or {}).get("calendar", "nyse"), "performance": perf,
           "exposure": exposure(result, perf, missing), "benchmark": benchmark_stats(result, missing),
           "costs": costs(result, perf, missing)}
    if include_signal:
        out["signal_quality"] = signal_quality(result, analysis)
        out["capacity"] = capacity(result, analysis)
    out["unavailable"] = missing.reasons
    return out


def summary(metrics: Optional[dict]) -> dict:
    """Key metrics for run lists: {total_return, cagr, sharpe, max_drawdown, volatility}."""
    perf = (metrics or {}).get("performance") or {}
    return {k: perf.get(k) for k in SUMMARY_KEYS}
