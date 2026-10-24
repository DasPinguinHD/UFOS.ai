"""Attribution — layer 1 of "Explain" (SPEC §11.1, notes §11.5): WHY a
backtest performed as it did, computed deterministically from the
in-memory engine result and its metrics (pure Python + the standard
library; no network, no AI, no randomness).

``compute(result, metrics=None) -> attribution`` (stored on the run card,
column ``attribution_json``); ``gate_facts(report)`` (validation / holdout
reports); ``with_gate_facts(attribution, report)`` (the API's validation
payload: the base run's attribution + the report's gate facts in ONE fact
table); ``unavailable_reason(card)`` (why a card has none).

Inputs: ``result.dates / equity / benchmark / long_mv / short_mv / fills /
request.capital / range.calendar / benchmark_info.symbol`` and
``result.symbol_pnl`` (engine 3c.2: per valued day and symbol pnl, mtm,
dividends, fees, slippage, borrow, funding, mv — see engine docstring).
Every block is either computed or ``{"unavailable": reason}`` — never a
guessed value (no unrequested fallbacks).

Blocks (money in USD, returns / shares / exposures as FRACTIONS):
- ``decomposition``: daily pairs (j-1, j) where BOTH accounts are valued —
  exactly the pairs of ``metrics.benchmark_stats`` — r_j = E_j/E_(j-1) - 1,
  m_j = B_j/B_(j-1) - 1; beta and alpha_annual from
  ``metrics.benchmark_stats`` (the same OLS, reused), a = alpha_annual / P;
  market_j = beta m_j, residual e_j = r_j - a - market_j. Return space:
  ``*_cum`` = sums over the pairs, ``*_ann`` = mean x P (arithmetic,
  alpha_ann = the metric). Dollar space: ``*_pnl`` = sum of component_j x
  E_(j-1); ``outside_regression_pnl`` = total_pnl - (market + alpha +
  residual) P&L (the first day from the starting capital and days whose
  valuation was paused). P&L sources from symbol_pnl: ``price_pnl`` = sum
  (mtm + slippage) (price P&L before slippage), costs as POSITIVE amounts
  ``slippage``, ``fees`` (incl. liquidation fees), ``borrow``, ``funding``
  (+ = paid), ``dividends_net`` (received - paid on shorts), ``cash_other``
  (symbol ``_cash``: ledger rows without a symbol other than deposit/reset);
  ``total_pnl`` = last valued equity - capital; ``unattributed`` = total_pnl
  - (price_pnl - slippage - fees - borrow - funding + dividends_net +
  cash_other) — float noise only (tested ≤ 0.01 % of capital).
- ``by_symbol``: per symbol pnl, share (= pnl / total_pnl; null when total
  is 0), long_pnl / short_pnl / flat_pnl (a row's side = the sign of the
  position value at the PREVIOUS valued close, else at this close, else
  flat), days_held (valued closes with a position), fills, costs (fees +
  slippage + borrow + funding, positive), dividends. ``top`` = the 10 best
  by pnl (ties: symbol; with fewer than 20 symbols the better half,
  ceil(n/2)), ``bottom`` = the 10 worst of the rest (worst first), ``others`` = the remaining symbols summed; ``reconciliation`` =
  {total_pnl, symbols_pnl, cash_other, unattributed}: top + bottom + others
  + cash_other + unattributed = total_pnl.
- ``by_year``: per calendar year of the simulated days: return = last
  valued equity of the year / the base (last valued equity before the year;
  the starting capital for the first year) - 1; benchmark the same on the
  benchmark account; excess = return - benchmark_return; max_drawdown
  within the year (peak starts at the base); avg_net_exposure = mean
  (long_mv - short_mv) / E over the valued days; pnl = $ change.
- ``by_regime``: benchmark-only and known at t (no look-ahead): the return
  of day j gets the label computed from the benchmark account up to the
  close of day j-1 = t: trend ``up`` when B_t / B_(t-63) - 1 ≥ 0 else
  ``down``; vol_t = std(last 21 daily benchmark returns ending at t, ddof 1)
  x sqrt(P); ``volatile`` when vol_t ≥ the EXPANDING median of vol over all
  t' ≤ t (labelled ones), else ``calm``. t < 63 (the first 63 sessions) =
  warm-up (excluded, counted); a needed benchmark value missing = unknown
  (excluded, counted). Per regime (``up_calm``, ``up_volatile``,
  ``down_calm``, ``down_volatile``): days, share (of labelled days),
  strategy_return_ann / benchmark_return_ann (mean daily return x P,
  arithmetic), hit_rate (strategy days > 0 / days ≠ 0; null without such a
  day). Constants: TREND_WINDOW, VOL_WINDOW, REGIME_WARMUP.
- ``long_short``: long / short / flat pnl (row side rule above), average
  long and short exposure (long_mv / E, short_mv / E over valued days).
- ``periods``: all windows of PERIOD_WINDOW (63) sessions [s, s+63] with
  both ends valued (strategy and benchmark); best 3 and worst 3 picked
  greedily (best/worst first, ties by start) without overlapping return
  days (|s1 - s2| ≥ 63); strategy_return, benchmark_return, excess.
  ``drawdowns``: underwater episodes of the valued equity (peak -> trough
  -> recovery = first close back at ≥ the peak, or null = not recovered),
  the 3 deepest (ties: start); per episode the (up to) 3 symbols with the
  most negative pnl over the days after the peak up to the trough.
- ``costs``: copied from ``metrics.costs`` (fees, liquidation_fees,
  slippage, borrow, funding, total, gross_pnl, net_pnl,
  pct_of_gross_pnl) + each component as % of gross P&L (only when gross >
  0) + ``turnover_annual`` (metrics.exposure).
- ``signal``: copied from ``metrics.signal_quality`` (no recomputation).
- ``facts``: [{id, label, value, unit, display}] for every number above
  (None values are left out); ids and display formats in §11.5. Units:
  usd ("$1,234.56", "-$1,234.56"), fraction (x 100: "12.34 %", "-0.50 %"),
  percent (already in percent: "12.34 %"), number (decimals per fact),
  count ("12"), days ("12.3"), date (ISO), status (text).

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

import copy
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

ATTRIBUTION_VERSION = "sp-attribution/1"
REQUIRED_ENGINE = "sp-backtest/3c.2"          # first engine that records symbol_pnl
OLD_ENGINES = ("sp-backtest/3c.1",)            # engines without symbol_pnl
TOP_N = 10
TREND_WINDOW = 63
VOL_WINDOW = 21
REGIME_WARMUP = 63
REGIMES = ("up_calm", "up_volatile", "down_calm", "down_volatile")
PERIOD_WINDOW = 63
PERIODS_N = 3
DRAWDOWNS_N = 3
DRAWDOWN_SYMBOLS_N = 3
CASH_SYMBOL = "_cash"
OTHERS_ID = "_others"
PERIODS_PER_YEAR = {"nyse": 252, "utc": 365}
NOT_AVAILABLE_OLD = ("Not available for runs created before engine " + REQUIRED_ENGINE
                     + " — run the backtest again.")
GATE_UNITS = {"G1": ("number", 2), "G2": ("number", 2), "G3": ("days", 1), "G4": ("number", 3),
              "G5": ("number", 3), "G6": ("fraction", 2), "G7": ("count", 0), "G8": ("usd", 0), "H": ("number", 2)}


# --- small helpers ---------------------------------------------------------------------------------

def _field(result, key: str, default=None):
    if isinstance(result, dict):
        return result.get(key, default)
    return getattr(result, key, default)


def _num(x) -> bool:
    return x is not None and not isinstance(x, bool) and isinstance(x, (int, float)) and math.isfinite(float(x))


def _pos(x) -> bool:
    return _num(x) and float(x) > 0


def _mean(xs: Sequence[float]) -> Optional[float]:
    return math.fsum(xs) / len(xs) if xs else None


def _std(xs: Sequence[float]) -> Optional[float]:
    n = len(xs)
    if n < 2:
        return None
    m = math.fsum(xs) / n
    return math.sqrt(math.fsum((x - m) ** 2 for x in xs) / (n - 1))


def _median(xs: Sequence[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _z(x: float) -> float:
    """-0.0 -> 0.0 (JSON shows no negative zero)."""
    return x + 0.0


def _na(reason: str) -> dict:
    return {"unavailable": reason}


def display(value, unit: str, decimals: int = 2) -> str:
    """The exact display string of a fact (SPEC §11.5)."""
    if unit in ("date", "status", "text"):
        return str(value)
    v = float(value)
    if unit == "usd":
        s = f"${abs(v):,.{decimals}f}"
        return ("-" + s) if v < 0 and round(abs(v), decimals) != 0 else s
    if unit == "fraction":
        p = 100.0 * v
        if round(p, decimals) == 0:
            p = 0.0
        return f"{p:.{decimals}f} %"
    if unit == "percent":                 # value already in percent (metrics.costs.pct_of_gross_pnl)
        if round(v, decimals) == 0:
            v = 0.0
        return f"{v:.{decimals}f} %"
    if unit == "count":
        return f"{int(round(v)):d}"
    if round(v, decimals) == 0:
        v = 0.0
    return f"{v:.{decimals}f}"


class _Facts:
    def __init__(self):
        self.rows: List[dict] = []
        self.ids: set = set()

    def add(self, fid: str, label: str, value, unit: str, decimals: int = 2) -> None:
        if value is None:
            return
        if unit not in ("date", "status", "text") and not _num(value):
            return
        if fid in self.ids:
            raise ValueError(f"duplicate fact id {fid}")
        self.ids.add(fid)
        if unit == "count":
            value = int(round(float(value)))
        elif unit not in ("date", "status", "text"):
            value = float(value)
        self.rows.append({"id": fid, "label": label, "value": value, "unit": unit,
                          "display": display(value, unit, decimals)})


# --- series --------------------------------------------------------------------------------------------

def _series(result) -> dict:
    dates = list(_field(result, "dates") or [])
    n = len(dates)

    def arr(key):
        xs = list(_field(result, key) or [])
        return [float(x) if _num(x) else None for x in xs] + [None] * (n - len(xs))
    rng = _field(result, "range") or {}
    calendar = rng.get("calendar") or "nyse"
    cap = (_field(result, "request") or {}).get("capital")
    return {"dates": dates, "equity": arr("equity"), "bench": arr("benchmark"), "long": arr("long_mv"),
            "short": arr("short_mv"), "calendar": calendar, "P": PERIODS_PER_YEAR.get(calendar, 252),
            "capital": float(cap) if _num(cap) else None,
            "benchmark": (_field(result, "benchmark_info") or {}).get("symbol")}


NOT_RECORDED = "Per-symbol P&L was not recorded for this run (engine flag record_symbol_pnl off)."


def _no_rows_reason(result) -> str:
    """Old engine -> the §11.4 text; a current engine run with recording off (grid / stress) -> NOT_RECORDED."""
    v = _field(result, "engine_version")
    return NOT_AVAILABLE_OLD if (v is None or v in OLD_ENGINES) else NOT_RECORDED


def _symbol_rows(result) -> Optional[List[dict]]:
    sp = _field(result, "symbol_pnl")
    if not isinstance(sp, dict) or not isinstance(sp.get("fields"), list) or not isinstance(sp.get("rows"), list):
        return None
    f = sp["fields"]
    return [dict(zip(f, r)) for r in sp["rows"]]


def _side_rows(rows: List[dict]) -> List[Tuple[dict, str]]:
    """(row, side) with side = sign of the previous valued close's position value, else this close's, else flat."""
    prev: Dict[str, float] = {}
    out = []
    for r in rows:
        s = r["symbol"]
        was = prev.get(s, 0.0)
        now = float(r.get("mv") or 0.0)
        ref = was if was != 0 else now
        out.append((r, "long" if ref > 0 else ("short" if ref < 0 else "flat")))
        prev[s] = now
    # the engine emits a row for every valued close with a position AND for the close where it went to
    # 0 (mv 0), so ``prev`` is reset there
    return out


# --- blocks ----------------------------------------------------------------------------------------

def _total_pnl(S: dict) -> Optional[float]:
    last = next((e for e in reversed(S["equity"]) if e is not None and e > 0), None)
    if last is None or S["capital"] is None:
        return None
    return last - S["capital"]


def decomposition(result, S: dict, rows: Optional[List[dict]], facts: _Facts) -> dict:
    from . import metrics as metrics_mod
    out: Dict[str, Any] = {}
    total = _total_pnl(S)
    if total is None:
        return _na("no valued day (or no starting capital) — the P&L cannot be decomposed")
    out["total_pnl"] = total
    facts.add("dec.total_pnl", "Total P&L", total, "usd")
    missing = metrics_mod._Missing()
    bstats = metrics_mod.benchmark_stats(result, missing)
    beta, alpha_ann = bstats.get("beta"), bstats.get("alpha_annual")
    E, B, P = S["equity"], S["bench"], S["P"]
    if _num(beta) and _num(alpha_ann):
        a = float(alpha_ann) / P
        mk, al, rs, mk_d, al_d, rs_d = [], [], [], [], [], []
        for j in range(1, len(E)):
            e0, e1, b0, b1 = E[j - 1], E[j], B[j - 1], B[j]
            if all(_pos(x) for x in (e0, e1, b0, b1)):
                r, m = e1 / e0 - 1.0, b1 / b0 - 1.0
                part = beta * m
                mk.append(part)
                al.append(a)
                rs.append(r - a - part)
                mk_d.append(part * e0)
                al_d.append(a * e0)
                rs_d.append((r - a - part) * e0)
        n = len(mk)
        reg = {"benchmark": S["benchmark"], "pairs": n, "beta": float(beta), "alpha_daily": a,
               "market_cum": math.fsum(mk), "alpha_cum": math.fsum(al), "residual_cum": math.fsum(rs),
               "market_ann": _mean(mk) * P, "alpha_ann": float(alpha_ann), "residual_ann": _mean(rs) * P,
               "market_pnl": math.fsum(mk_d), "alpha_pnl": math.fsum(al_d), "residual_pnl": math.fsum(rs_d)}
        reg["outside_regression_pnl"] = total - (reg["market_pnl"] + reg["alpha_pnl"] + reg["residual_pnl"])
        out["regression"] = reg
        facts.add("dec.beta", "Beta vs the benchmark", reg["beta"], "number", 2)
        for k, lab in (("market", "Market part (beta x benchmark)"), ("alpha", "Alpha part"),
                       ("residual", "Residual part")):
            facts.add(f"dec.{k}_cum", lab + ", cumulative return", reg[f"{k}_cum"], "fraction")
            facts.add(f"dec.{k}_ann", lab + ", annualised", reg[f"{k}_ann"], "fraction")
            facts.add(f"dec.{k}_pnl", lab + ", P&L", reg[f"{k}_pnl"], "usd")
        facts.add("dec.outside_regression_pnl", "P&L outside the regression days", reg["outside_regression_pnl"],
                  "usd")
    else:
        out["regression"] = _na("the regression vs the benchmark is not computable: "
                                + (missing.reasons.get("benchmark.beta") or "no beta"))
    if rows is None:
        out["sources"] = _na(S["no_rows"])
        return out
    srows = [r for r in rows if r["symbol"] != CASH_SYMBOL]
    src = {"price_pnl": math.fsum(float(r["mtm"]) + float(r["slippage"]) for r in srows),
           "slippage": math.fsum(float(r["slippage"]) for r in srows),
           "fees": -math.fsum(float(r["fees"]) for r in rows),
           "borrow": -math.fsum(float(r["borrow"]) for r in rows),
           "funding": -math.fsum(float(r["funding"]) for r in rows),
           "dividends_net": math.fsum(float(r["dividends"]) for r in rows),
           "cash_other": math.fsum(float(r["pnl"]) for r in rows if r["symbol"] == CASH_SYMBOL)}
    src["costs_total"] = src["slippage"] + src["fees"] + src["borrow"] + src["funding"]
    explained = src["price_pnl"] - src["costs_total"] + src["dividends_net"] + src["cash_other"]
    src["gross_pnl"] = total + src["costs_total"]
    src["unattributed"] = total - explained
    src = {k: _z(v) for k, v in src.items()}
    out["sources"] = src
    for k, lab, unit in (("price_pnl", "Price P&L before slippage", "usd"), ("slippage", "Slippage", "usd"),
                         ("fees", "Fees", "usd"), ("borrow", "Borrow fees", "usd"),
                         ("funding", "Funding paid", "usd"), ("dividends_net", "Dividends (net)", "usd"),
                         ("cash_other", "Other cash", "usd"), ("costs_total", "Costs total", "usd"),
                         ("gross_pnl", "Gross P&L (before costs)", "usd"), ("unattributed", "Unattributed", "usd")):
        facts.add("dec." + k, lab, src[k], unit)
    return out


def _per_symbol(rows: List[dict], fills: List[dict]) -> Dict[str, dict]:
    nf: Dict[str, int] = {}
    for f in fills or []:
        nf[f.get("symbol")] = nf.get(f.get("symbol"), 0) + 1
    acc: Dict[str, Dict[str, list]] = {}
    for r, side in _side_rows(rows):
        s = r["symbol"]
        if s == CASH_SYMBOL:
            continue
        a = acc.setdefault(s, {"pnl": [], "long": [], "short": [], "flat": [], "cost": [], "div": [], "held": 0})
        p = float(r["pnl"])
        a["pnl"].append(p)
        a[side].append(p)
        a["cost"].append(float(r["slippage"]) - float(r["fees"]) - float(r["borrow"]) - float(r["funding"]))
        a["div"].append(float(r["dividends"]))
        if float(r.get("mv") or 0.0) != 0.0:
            a["held"] += 1
    out = {}
    for s in sorted(acc):
        a = acc[s]
        out[s] = {"symbol": s, "pnl": math.fsum(a["pnl"]), "long_pnl": math.fsum(a["long"]),
                  "short_pnl": math.fsum(a["short"]), "flat_pnl": math.fsum(a["flat"]), "days_held": a["held"],
                  "fills": nf.get(s, 0), "costs": math.fsum(a["cost"]), "dividends": math.fsum(a["div"])}
    return out


def by_symbol(result, S: dict, rows: Optional[List[dict]], facts: _Facts) -> dict:
    if rows is None:
        return _na(S["no_rows"])
    total = _total_pnl(S)
    if total is None:
        return _na("no valued day (or no starting capital)")
    facts.add("sym.top_n", "Symbols listed at most per top / bottom table", TOP_N, "count")
    per = _per_symbol(rows, _field(result, "fills") or [])
    for v in per.values():
        v["share"] = v["pnl"] / total if total != 0 else None
    ordered = sorted(per.values(), key=lambda v: (-v["pnl"], v["symbol"]))
    n_top = min(TOP_N, (len(ordered) + 1) // 2)        # few symbols: the better half is "top", the rest "bottom"
    top = ordered[:n_top]
    rest = ordered[n_top:]
    bottom = sorted(rest, key=lambda v: (v["pnl"], v["symbol"]))[:TOP_N]
    named = {v["symbol"] for v in top} | {v["symbol"] for v in bottom}
    others = [v for v in ordered if v["symbol"] not in named]
    oth = {"count": len(others)}
    for k in ("pnl", "long_pnl", "short_pnl", "flat_pnl", "costs", "dividends"):
        oth[k] = math.fsum(v[k] for v in others)
    oth["share"] = oth["pnl"] / total if total != 0 else None
    cash = math.fsum(float(r["pnl"]) for r in rows if r["symbol"] == CASH_SYMBOL)
    sym_sum = math.fsum(v["pnl"] for v in per.values())
    rec = {"total_pnl": total, "symbols_pnl": sym_sum, "cash_other": cash, "unattributed": total - sym_sum - cash}
    for v in top + bottom:
        base = "sym." + v["symbol"]
        facts.add(base + ".pnl", f"{v['symbol']} P&L", v["pnl"], "usd")
        facts.add(base + ".share", f"{v['symbol']} share of total P&L", v["share"], "fraction")
        facts.add(base + ".long_pnl", f"{v['symbol']} long P&L", v["long_pnl"], "usd")
        facts.add(base + ".short_pnl", f"{v['symbol']} short P&L", v["short_pnl"], "usd")
        facts.add(base + ".days_held", f"{v['symbol']} days held", v["days_held"], "count")
        facts.add(base + ".fills", f"{v['symbol']} fills", v["fills"], "count")
        facts.add(base + ".costs", f"{v['symbol']} costs", v["costs"], "usd")
    if others:
        facts.add("sym._others.pnl", "Other symbols P&L", oth["pnl"], "usd")
        facts.add("sym._others.count", "Other symbols", oth["count"], "count")
        facts.add("sym._others.share", "Other symbols share of total P&L", oth["share"], "fraction")
    facts.add("sym.symbols", "Symbols traded or held", len(per), "count")
    facts.add("sym.unattributed", "Unattributed (by symbol)", rec["unattributed"], "usd")
    return {"symbols": len(per), "top": top, "bottom": bottom, "others": oth, "reconciliation": rec}


def _max_dd(values: List[float], base: Optional[float]) -> float:
    peak = base if base is not None else (values[0] if values else 0.0)
    worst = 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, 1.0 - v / peak)
    return worst


def by_year(S: dict, facts: _Facts) -> dict:
    E, B, dates = S["equity"], S["bench"], S["dates"]
    if S["capital"] is None or not any(_pos(e) for e in E):
        return _na("no valued day (or no starting capital)")
    years: Dict[str, List[int]] = {}
    for i, d in enumerate(dates):
        years.setdefault(d[:4], []).append(i)
    out = []
    base_e = base_b = S["capital"]
    for y in sorted(years):
        idx = years[y]
        ev = [E[i] for i in idx if _pos(E[i])]
        bv = [B[i] for i in idx if _pos(B[i])]
        row: Dict[str, Any] = {"year": int(y), "days": len(idx)}
        row["return"] = ev[-1] / base_e - 1.0 if ev and _pos(base_e) else None
        row["pnl"] = ev[-1] - base_e if ev and _pos(base_e) else None
        row["benchmark_return"] = bv[-1] / base_b - 1.0 if bv and _pos(base_b) else None
        row["excess"] = row["return"] - row["benchmark_return"] \
            if row["return"] is not None and row["benchmark_return"] is not None else None
        row["max_drawdown"] = _max_dd(ev, base_e) if ev else None
        net = [(S["long"][i] - S["short"][i]) / E[i] for i in idx
               if _pos(E[i]) and S["long"][i] is not None and S["short"][i] is not None]
        row["avg_net_exposure"] = _mean(net)
        out.append(row)
        if ev:
            base_e = ev[-1]
        if bv:
            base_b = bv[-1]
        p = f"year.{y}."
        facts.add(p + "return", f"{y} strategy return", row["return"], "fraction")
        facts.add(p + "benchmark", f"{y} benchmark return", row["benchmark_return"], "fraction")
        facts.add(p + "excess", f"{y} excess return", row["excess"], "fraction")
        facts.add(p + "max_drawdown", f"{y} max drawdown", row["max_drawdown"], "fraction")
        facts.add(p + "net_exposure", f"{y} average net exposure", row["avg_net_exposure"], "fraction")
        facts.add(p + "pnl", f"{y} P&L", row["pnl"], "usd")
    return {"years": out}


def regime_labels(bench: Sequence[Optional[float]], periods_per_year: int) -> List[str]:
    """Label of the return of day j (index j) from the benchmark up to the close of day j-1 (SPEC §11.1):
    'warm-up' (j-1 < REGIME_WARMUP or j = 0), 'unknown' (a needed value missing) or one of REGIMES.
    Uses only bench[:j] — no look-ahead (tested)."""
    n = len(bench)
    labels = ["warm-up"] * n
    vols: List[float] = []
    for j in range(1, n):
        t = j - 1
        if t < REGIME_WARMUP:
            continue
        b_t, b_0 = bench[t], bench[t - TREND_WINDOW]
        win = [bench[k] for k in range(t - VOL_WINDOW, t + 1)]
        if not (_pos(b_t) and _pos(b_0)) or not all(_pos(x) for x in win):
            labels[j] = "unknown"
            continue
        rets = [win[k] / win[k - 1] - 1.0 for k in range(1, len(win))]
        vol = _std(rets) * math.sqrt(periods_per_year)
        vols.append(vol)
        trend = "up" if b_t / b_0 - 1.0 >= 0 else "down"
        state = "volatile" if vol >= _median(vols) else "calm"
        labels[j] = f"{trend}_{state}"
    return labels


def by_regime(S: dict, facts: _Facts) -> dict:
    E, B, P = S["equity"], S["bench"], S["P"]
    if len(E) < REGIME_WARMUP + 2:
        return _na(f"needs more than {REGIME_WARMUP + 1} sessions (the first {REGIME_WARMUP} are the warm-up)")
    labels = regime_labels(B, P)
    acc: Dict[str, Tuple[list, list]] = {k: ([], []) for k in REGIMES}
    warm = sum(1 for j in range(1, len(E)) if labels[j] == "warm-up")
    unknown = 0
    for j in range(1, len(E)):
        lab = labels[j]
        if lab == "warm-up":
            continue
        if lab == "unknown" or not all(_pos(x) for x in (E[j - 1], E[j], B[j - 1], B[j])):
            unknown += 1
            continue
        acc[lab][0].append(E[j] / E[j - 1] - 1.0)
        acc[lab][1].append(B[j] / B[j - 1] - 1.0)
    labelled = sum(len(v[0]) for v in acc.values())
    if labelled == 0:
        return _na("no labelled day after the warm-up")
    rows = []
    for k in REGIMES:
        rs, rb = acc[k]
        nz = [x for x in rs if x != 0]
        row = {"regime": k, "days": len(rs), "share": len(rs) / labelled,
               "strategy_return_ann": _mean(rs) * P if rs else None,
               "benchmark_return_ann": _mean(rb) * P if rb else None,
               "hit_rate": (sum(1 for x in nz if x > 0) / len(nz)) if nz else None}
        rows.append(row)
        p = f"regime.{k}."
        facts.add(p + "days", f"{k} days", row["days"], "count")
        facts.add(p + "share", f"{k} share of days", row["share"], "fraction")
        facts.add(p + "strategy_ann", f"{k} strategy return (annualised)", row["strategy_return_ann"], "fraction")
        facts.add(p + "benchmark_ann", f"{k} benchmark return (annualised)", row["benchmark_return_ann"], "fraction")
        facts.add(p + "hit_rate", f"{k} hit rate", row["hit_rate"], "fraction")
    # method constants named in the labels (review L3: the number check must find them as facts)
    facts.add("regime.trend_window", "Trend window (sessions)", TREND_WINDOW, "count")
    facts.add("regime.vol_window", "Volatility window (sessions)", VOL_WINDOW, "count")
    facts.add("regime.warmup_sessions", "Regime warm-up (sessions)", REGIME_WARMUP, "count")
    facts.add("regime.warmup_days", "Warm-up days (no regime)", warm, "count")
    facts.add("regime.unknown_days", "Days without a regime (missing values)", unknown, "count")
    return {"benchmark": S["benchmark"], "trend_window": TREND_WINDOW, "vol_window": VOL_WINDOW,
            "warmup_sessions": REGIME_WARMUP, "warmup_days": warm, "unknown_days": unknown,
            "labelled_days": labelled, "regimes": rows}


def long_short(S: dict, rows: Optional[List[dict]], facts: _Facts) -> dict:
    if rows is None:
        return _na(S["no_rows"])
    sums = {"long": [], "short": [], "flat": []}
    for r, side in _side_rows(rows):
        if r["symbol"] != CASH_SYMBOL:
            sums[side].append(float(r["pnl"]))
    le, se = [], []
    for e, lo, sh in zip(S["equity"], S["long"], S["short"]):
        if _pos(e) and lo is not None and sh is not None:
            le.append(lo / e)
            se.append(sh / e)
    out = {"long_pnl": math.fsum(sums["long"]), "short_pnl": math.fsum(sums["short"]),
           "flat_pnl": math.fsum(sums["flat"]), "avg_long_exposure": _mean(le), "avg_short_exposure": _mean(se)}
    facts.add("ls.long.pnl", "Long side P&L", out["long_pnl"], "usd")
    facts.add("ls.short.pnl", "Short side P&L", out["short_pnl"], "usd")
    facts.add("ls.flat.pnl", "P&L booked without a position at either close", out["flat_pnl"], "usd")
    facts.add("ls.long.exposure", "Average long exposure", out["avg_long_exposure"], "fraction")
    facts.add("ls.short.exposure", "Average short exposure", out["avg_short_exposure"], "fraction")
    return out


def _pick(windows: List[dict], key, n: int) -> List[dict]:
    chosen: List[dict] = []
    for w in sorted(windows, key=key):
        if all(abs(w["_s"] - c["_s"]) >= PERIOD_WINDOW for c in chosen):
            chosen.append(w)
            if len(chosen) == n:
                break
    return [{k: v for k, v in c.items() if k != "_s"} for c in chosen]


def _drawdowns(S: dict) -> List[dict]:
    E, dates = S["equity"], S["dates"]
    pts = [(i, e) for i, e in enumerate(E) if _pos(e)]
    eps = []
    if not pts:
        return eps
    peak_i, peak = pts[0]
    cur = None
    for i, e in pts[1:]:
        if e >= peak:
            if cur is not None:
                cur["recovery_i"] = i
                eps.append(cur)
                cur = None
            peak_i, peak = i, e
            continue
        depth = 1.0 - e / peak
        if cur is None:
            cur = {"peak_i": peak_i, "trough_i": i, "depth": depth, "recovery_i": None}
        elif depth > cur["depth"]:
            cur["trough_i"], cur["depth"] = i, depth
    if cur is not None:
        eps.append(cur)
    return eps


def periods(S: dict, rows: Optional[List[dict]], facts: _Facts) -> dict:
    E, B, dates = S["equity"], S["bench"], S["dates"]
    out: Dict[str, Any] = {"window": PERIOD_WINDOW}
    facts.add("period.window", "Period window (sessions)", PERIOD_WINDOW, "count")
    wins = []
    for s in range(0, len(E) - PERIOD_WINDOW):
        e = s + PERIOD_WINDOW
        if all(_pos(x) for x in (E[s], E[e], B[s], B[e])):
            rs, rb = E[e] / E[s] - 1.0, B[e] / B[s] - 1.0
            wins.append({"_s": s, "start": dates[s], "end": dates[e], "strategy_return": rs,
                         "benchmark_return": rb, "excess": rs - rb})
    if not wins:
        out["best"] = out["worst"] = _na(f"needs a window of {PERIOD_WINDOW} sessions with both accounts valued")
    else:
        out["best"] = _pick(wins, lambda w: (-w["strategy_return"], w["_s"]), PERIODS_N)
        out["worst"] = _pick(wins, lambda w: (w["strategy_return"], w["_s"]), PERIODS_N)
        for kind in ("best", "worst"):
            for k, w in enumerate(out[kind], 1):
                p = f"period.{kind}{k}."
                lab = f"{'Best' if kind == 'best' else 'Worst'} {PERIOD_WINDOW}-session period #{k}"
                facts.add(p + "start", lab + " start", w["start"], "date")
                facts.add(p + "end", lab + " end", w["end"], "date")
                facts.add(p + "strategy", lab + " strategy return", w["strategy_return"], "fraction")
                facts.add(p + "benchmark", lab + " benchmark return", w["benchmark_return"], "fraction")
                facts.add(p + "excess", lab + " excess return", w["excess"], "fraction")
    eps = sorted(_drawdowns(S), key=lambda d: (-d["depth"], d["peak_i"]))[:DRAWDOWNS_N]
    if not eps:
        out["drawdowns"] = []
        return out
    dd_rows = []
    for k, d in enumerate(eps, 1):
        row = {"start": dates[d["peak_i"]], "trough": dates[d["trough_i"]],
               "recovery": dates[d["recovery_i"]] if d["recovery_i"] is not None else None,
               "recovered": d["recovery_i"] is not None, "depth": d["depth"],
               "days_to_trough": d["trough_i"] - d["peak_i"]}
        if rows is None:
            row["contributors"] = _na(S["no_rows"])
        else:
            lo, hi = dates[d["peak_i"]], dates[d["trough_i"]]
            acc: Dict[str, list] = {}
            for r in rows:
                if lo < r["date"] <= hi and r["symbol"] != CASH_SYMBOL:
                    acc.setdefault(r["symbol"], []).append(float(r["pnl"]))
            tot = sorted(((math.fsum(v), s) for s, v in acc.items()), key=lambda x: (x[0], x[1]))
            row["contributors"] = [{"symbol": s, "pnl": p} for p, s in tot if p < 0][:DRAWDOWN_SYMBOLS_N]
        dd_rows.append(row)
        p = f"dd.{k}."
        lab = f"Drawdown #{k}"
        facts.add(p + "depth", lab + " depth", row["depth"], "fraction")
        facts.add(p + "start", lab + " start (peak)", row["start"], "date")
        facts.add(p + "trough", lab + " trough", row["trough"], "date")
        facts.add(p + "recovery", lab + " recovery", row["recovery"] if row["recovered"] else "not recovered",
                  "date" if row["recovered"] else "status")
        facts.add(p + "days_to_trough", lab + " sessions from peak to trough", row["days_to_trough"], "count")
        if isinstance(row["contributors"], list):
            for c in row["contributors"]:
                facts.add(p + "sym." + c["symbol"] + ".pnl", f"{lab}: {c['symbol']} P&L", c["pnl"], "usd")
    out["drawdowns"] = dd_rows
    return out


COST_KEYS = ("fees", "liquidation_fees", "slippage", "borrow", "funding")


def costs_block(metrics: Optional[dict], facts: _Facts) -> dict:
    c = (metrics or {}).get("costs")
    if not isinstance(c, dict):
        return _na("the run's metrics have no cost block")
    out = {k: c.get(k) for k in COST_KEYS + ("total", "gross_pnl", "net_pnl", "pct_of_gross_pnl")}
    gross = c.get("gross_pnl")
    out["pct_of_gross"] = {k: (100.0 * c[k] / gross if _num(c.get(k)) and _pos(gross) else None) for k in COST_KEYS}
    if not _pos(gross):
        out["pct_of_gross_reason"] = ((metrics or {}).get("unavailable") or {}).get("costs.pct_of_gross_pnl") \
            or "the gross P&L (before costs) is not positive"
    out["turnover_annual"] = ((metrics or {}).get("exposure") or {}).get("turnover_annual")
    for k in COST_KEYS + ("total",):
        facts.add("cost." + k, "Costs: " + k.replace("_", " "), out[k], "usd")
    facts.add("cost.pct_of_gross", "Costs as % of gross P&L", out["pct_of_gross_pnl"], "percent", 2)
    for k in COST_KEYS:
        facts.add(f"cost.{k}.pct_of_gross", f"Costs: {k.replace('_', ' ')} as % of gross P&L",
                  out["pct_of_gross"][k], "percent", 2)
    facts.add("cost.turnover", "Turnover per year", out["turnover_annual"], "fraction")
    return out


def signal_block(metrics: Optional[dict], facts: _Facts) -> dict:
    sq = (metrics or {}).get("signal_quality")
    if not isinstance(sq, dict):
        return _na("the run's metrics have no signal quality block")
    mode = sq.get("mode")
    if mode is None:
        return _na(sq.get("reason") or "no signal quality")
    if mode == "cross_sectional":
        hs = [{k: h.get(k) for k in ("horizon", "mean_ic", "icir", "t_stat", "days", "reason")}
              for h in sq.get("horizons") or []]
        out = {"mode": mode, "symbols": sq.get("symbols"), "horizons": hs,
               "half_life_days": sq.get("half_life_days"), "half_life_reason": sq.get("half_life_reason")}
        for h in hs:
            p = f"sig.ic_h{h['horizon']}."
            facts.add(p + "horizon", f"IC horizon {h['horizon']} (days)", h["horizon"], "count")
            facts.add(p + "mean", f"Mean rank IC at {h['horizon']} day(s)", h["mean_ic"], "number", 4)
            facts.add(p + "icir", f"ICIR at {h['horizon']} day(s)", h["icir"], "number", 2)
            facts.add(p + "t_stat", f"IC t-stat (naive) at {h['horizon']} day(s)", h["t_stat"], "number", 2)
        facts.add("sig.half_life_days", "IC half-life (days)", out["half_life_days"], "days", 1)
        return out
    out = {"mode": mode, "symbols": sq.get("symbols"), "window_days": sq.get("window_days"),
           "windows": sq.get("windows"), "positive_share": sq.get("positive_share"), "mean_corr": sq.get("mean_corr"),
           "reason": sq.get("reason")}
    if _num(out["window_days"]):
        facts.add("sig.ts.window_weeks", "Signal correlation window (weeks)", 13, "count")
        facts.add("sig.ts.window_days", "Signal correlation window (days)", out["window_days"], "count")
    facts.add("sig.ts.positive_share", "Share of 13-week windows with a positive signal correlation",
              out["positive_share"], "fraction")
    facts.add("sig.ts.mean_corr", "Mean 13-week signal correlation", out["mean_corr"], "number", 4)
    return out


# --- entry points ------------------------------------------------------------------------------------

def compute(result, metrics: Optional[dict] = None) -> dict:
    """The attribution of one engine result (module docstring)."""
    S = _series(result)
    rows = _symbol_rows(result)
    S["no_rows"] = _no_rows_reason(result)
    facts = _Facts()
    rng = _field(result, "range") or {}
    out: Dict[str, Any] = {
        "version": ATTRIBUTION_VERSION, "engine_version": _field(result, "engine_version"),
        "currency": "USD", "range": {"start": rng.get("start"), "end": rng.get("end"), "calendar": S["calendar"]},
        "capital": S["capital"], "benchmark": S["benchmark"],
        "symbol_pnl": None}
    if S["capital"] is not None:
        facts.add("run.capital", "Starting capital", S["capital"], "usd", 0)
    facts.add("run.periods_per_year", "Periods per year (annualisation)", S["P"], "count")
    if rng.get("start"):
        facts.add("run.start", "First simulated day", rng.get("start"), "date")
        facts.add("run.end", "Last simulated day", rng.get("end"), "date")
    out["decomposition"] = decomposition(result, S, rows, facts)
    out["by_symbol"] = by_symbol(result, S, rows, facts)
    out["by_year"] = by_year(S, facts)
    out["by_regime"] = by_regime(S, facts)
    out["long_short"] = long_short(S, rows, facts)
    out["periods"] = periods(S, rows, facts)
    out["costs"] = costs_block(metrics, facts)
    out["signal"] = signal_block(metrics, facts)
    out["facts"] = facts.rows
    del out["symbol_pnl"]
    if rows is None:
        out["note"] = S["no_rows"]
    return out


def _gate_detail_facts(g: dict, report: dict, facts: _Facts) -> None:
    gate = g.get("gate")
    det = g.get("details") or {}
    p = f"gate.{gate}."
    if gate == "G1":
        for f in report.get("folds") or []:
            k = f.get("fold")
            facts.add(f"val.fold.{k}.sharpe", f"Fold {k} Sharpe", f.get("sharpe"), "number", 2)
            facts.add(f"val.fold.{k}.mean_ic", f"Fold {k} mean IC", f.get("mean_ic"), "number", 4)
            facts.add(f"val.fold.{k}.total_return", f"Fold {k} total return", f.get("total_return"), "fraction")
        facts.add(p + "folds_positive", "G1 folds positive", det.get("folds_positive"), "count")
        facts.add(p + "best_horizon", "G1 best IC horizon (days)", det.get("best_horizon"), "count")
    elif gate == "G2":
        facts.add(p + "alpha_annual", "G2 alpha per year", det.get("alpha_annual"), "fraction")
        facts.add(p + "beta", "G2 beta", det.get("beta"), "number", 2)
    elif gate == "G3":
        facts.add(p + "half_life_days", "G3 IC half-life (days)", det.get("half_life_days"), "days", 1)
        facts.add(p + "avg_holding_days", "G3 average holding period (days)", det.get("avg_holding_days"), "days", 1)
    elif gate == "G4":
        d = report.get("dsr") or {}
        for k, lab, unit, nd in (("sharpe_annual", "Training Sharpe (annual)", "number", 2),
                                 ("sr0_annual", "Expected max Sharpe of the trials (annual)", "number", 2),
                                 ("n_trials", "Trials", "count", 0),
                                 ("trial_variance", "Trial Sharpe variance", "number", 4),
                                 ("skew", "Return skew", "number", 2), ("kurtosis", "Return kurtosis", "number", 2),
                                 ("returns", "Daily returns", "count", 0)):
            facts.add("val.dsr." + k, "DSR input: " + lab, d.get(k), unit, nd)
    elif gate == "G5":
        pb = report.get("pbo") or {}
        facts.add("val.pbo.value", "PBO", pb.get("value"), "number", 3)
        facts.add("val.pbo.configs", "PBO configurations", pb.get("configs"), "count")
        facts.add("val.pbo.combinations", "PBO combinations", pb.get("combinations"), "count")
        facts.add("val.pbo.overfit_combinations", "PBO overfit combinations", pb.get("overfit_combinations"), "count")
        facts.add("val.pbo.mean_logit", "PBO mean logit", pb.get("mean_logit"), "number", 2)
    elif gate == "G6":
        gr = report.get("grid") or {}
        sh = [float(x) for x in (gr.get("sharpes") or []) if _num(x)]
        if sh:
            facts.add("val.grid.sharpe_min", "Grid minimum Sharpe", min(sh), "number", 2)
            facts.add("val.grid.sharpe_median", "Grid median Sharpe", _median(sh), "number", 2)
            facts.add("val.grid.sharpe_max", "Grid maximum Sharpe", max(sh), "number", 2)
        facts.add("val.grid.configs", "Grid configurations", len(gr.get("configs") or []) or None, "count")
        facts.add("val.grid.base_sharpe", "Grid base Sharpe", gr.get("base_sharpe"), "number", 2)
        c = det.get("costs") or {}
        facts.add(p + "costs_sharpe", f"G6 Sharpe with costs x{c.get('multiplier')}", c.get("sharpe"), "number", 2)
        for e in det.get("episodes") or []:
            if e.get("key"):
                facts.add(f"val.stress.{e['key']}.strategy_dd", f"{e.get('label')}: strategy max drawdown",
                          e.get("strategy_dd"), "fraction")
                facts.add(f"val.stress.{e['key']}.benchmark_dd", f"{e.get('label')}: benchmark max drawdown",
                          e.get("benchmark_dd"), "fraction")
        for s in ((report.get("stress") or {}).get("scenarios") or []):
            m = s.get("metrics") or {}
            key = s.get("key")
            if s.get("status") != "done" or not key:
                continue
            st = m.get("strategy") or {}
            facts.add(f"val.stress.{key}.total_return", f"{s.get('label')}: strategy total return",
                      st.get("total_return"), "fraction")
            facts.add(f"val.stress.{key}.sharpe", f"{s.get('label')}: strategy Sharpe", st.get("sharpe"), "number", 2)
            if s.get("kind") != "episode":
                facts.add(f"val.stress.{key}.max_drawdown", f"{s.get('label')}: strategy max drawdown",
                          st.get("max_drawdown"), "fraction")
            if isinstance(m.get("max_drawdown"), dict):          # Monte-Carlo percentiles named in labels
                for pk in ("p5", "p50", "p95"):
                    facts.add(f"val.stress.{key}.percentile_{pk}", f"{s.get('label')}: percentile {pk[1:]}",
                              int(pk[1:]), "count")
            for metric in ("max_drawdown", "cagr"):
                for pk in ("p5", "p50", "p95"):
                    v = (m.get(metric) or {}).get(pk) if isinstance(m.get(metric), dict) else None
                    facts.add(f"val.stress.{key}.{metric}_{pk}", f"{s.get('label')}: {metric} {pk}", v, "fraction")
    elif gate == "G7":
        facts.add(p + "trades_closed", "G7 closed trades", det.get("trades_closed"), "count")
        facts.add(p + "trades_open", "G7 open trades at the end", det.get("trades_open"), "count")
    elif gate == "G8":
        facts.add(p + "capacity_usd", "G8 capacity estimate", det.get("capacity_usd"), "usd", 0)
        facts.add(p + "capital", "G8 tested capital", det.get("capital"), "usd", 0)
    elif gate == "H":
        facts.add(p + "sharpe", "Holdout Sharpe", det.get("sharpe"), "number", 2)
        facts.add(p + "training_sharpe", "Training Sharpe", det.get("training_sharpe"), "number", 2)
        facts.add(p + "mean_ic", "Holdout mean IC", det.get("mean_ic"), "number", 4)
        facts.add(p + "required_ratio", "Required share of the training Sharpe", det.get("required_ratio"), "fraction")
        facts.add(p + "horizon", "Holdout IC horizon (days)", det.get("horizon"), "count")


def gate_facts(report: Optional[dict]) -> List[dict]:
    """Per gate of a validation / holdout report: {gate, name, status, value, threshold, facts}
    (SPEC §11.1; ids in §11.5). Deterministic; recomputed after exceptions are merged."""
    out = []
    for g in (report or {}).get("gates") or []:
        gate = g.get("gate")
        unit, nd = GATE_UNITS.get(gate, ("number", 2))
        if gate == "G1" and (g.get("details") or {}).get("mode") == "time_series":
            unit, nd = "count", 0
        f = _Facts()
        f.add(f"gate.{gate}.status", f"{gate} {g.get('name')} status", g.get("status"), "status")
        f.add(f"gate.{gate}.value", f"{gate} {g.get('name')} value", g.get("value"), unit, nd)
        f.add(f"gate.{gate}.threshold", f"{gate} {g.get('name')} threshold", g.get("threshold"), unit, nd)
        _gate_detail_facts(g, report, f)
        out.append({"gate": gate, "name": g.get("name"), "status": g.get("status"), "value": g.get("value"),
                    "threshold": g.get("threshold"), "facts": f.rows})
    return out


def with_gate_facts(attribution: Optional[dict], report: Optional[dict]) -> Optional[dict]:
    """The validation/holdout payload: a copy of the base run's attribution + ``gate_facts`` (from the
    MERGED report) and their facts appended to ``facts``. None without an attribution."""
    if attribution is None:
        return None
    out = copy.deepcopy(attribution)
    gf = (report or {}).get("gate_facts")
    if gf is None and report is not None:
        gf = gate_facts(report)
    out["gate_facts"] = gf or []
    seen = {f["id"] for f in out.get("facts") or []}
    for g in out["gate_facts"]:
        for f in g.get("facts") or []:
            if f["id"] not in seen:
                out.setdefault("facts", []).append(f)
                seen.add(f["id"])
    return out


def unavailable_reason(card: Optional[dict]) -> Optional[str]:
    """Why a run card has no attribution (None when it has one)."""
    if not card:
        return "Run not found."
    if card.get("attribution") is not None:
        return None
    status = card.get("status")
    if status in ("queued", "running"):
        return "The attribution is computed when the run finishes."
    if status != "done":
        return f"Not available: the run is {status}."
    return NOT_AVAILABLE_OLD
