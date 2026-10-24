"""Indicator series for the chart (SPEC §10 MR5, §13 "MR5 backend notes", decision Q8).

Q8: the definitions are the Strategy Platform DSL's — every DSL-based indicator is computed by the DSL
evaluator itself (``strategy_platform.strategy.evaluate.evaluate_node``) on a one-column ``Panel`` built
from the chart's bars, so a value in Research never differs from the same expression in a backtest:

- SMA(n, src)      = ``sma(src, n)``
- EMA(n, src)      = ``ewm_mean(src, halflife)`` with halflife = ln 2 / −ln(1 − 2/(n+1))  (α = 2/(n+1))
- Bollinger(n, k)  = ``sma(src, n)`` ± k · ``ts_std(src, n)`` (sample std, n − 1)
- RSI(n)           = ``rsi(n)`` (Wilder, close)
- MACD(f, s, sig)  = EMA(f) − EMA(s); signal = ``ewm_mean(macd, hl(sig))``; hist = macd − signal
- sources hl2 / hlc3 / ohlc4 = DSL ``div(add(…), k)`` of the fields.

Research-only (no DSL operator exists — documented in SPEC §12): session VWAP Σ(vwap·v)/Σv from the
session start, and the compare overlay (% from the window's first bar).

Warm-up: older bars are loaded before the window (``warmup_bars``), the series is computed on prefix +
window and cut; a value whose look-back is not complete inside the loaded bars is ``None`` (no look-ahead —
the DSL evaluator is causal). BLOCKING (bars may come from Alpaca) — the route runs it on the Research pool
inside ``http_gate.research_priority()``.

Part of UFOS.ai, a proof-of-concept portfolio project; research only.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Dict, List, Optional, Tuple

from strategy_platform.core.clock import iso, to_utc
from strategy_platform.core.instruments import instrument

from . import limits
from .bars import aggregate as agg
from .bars.series import BadParam, buckets_per_session, max_limit

SOURCES = ("close", "open", "high", "low", "hl2", "hlc3", "ohlc4")
SUB_KINDS = ("rsi", "macd")
INT_RE = re.compile(r"^[0-9]{1,3}$")
K_RE = re.compile(r"^[0-9]{1,2}(\.[0-9]{1,3})?$")
FIELD_OF = {"open": "o", "high": "h", "low": "l", "close": "c", "volume": "v", "vwap": "vwap"}
NOTES = {
    "bb": "Bands use the sample standard deviation (n − 1), as the Strategy Platform's ts_std.",
    "ema": "Seeded from the loaded history (warm-up bars are loaded before the window).",
    "rsi": "Wilder's RSI, seeded from the loaded history (warm-up bars are loaded before the window).",
    "macd": "EMAs seeded from the loaded history (warm-up bars are loaded before the window).",
    "vwap": ("Session VWAP of the chart's bars (rth from 09:30, eth from 04:00 ET; crypto from 00:00 UTC). A bar "
             "without a VWAP ends the line for the rest of its session — no stand-in value."),
    "cmp": ("% change from the first bar of this window; the page may rebase to its visible left edge: "
            "(1 + p(t)/100) / (1 + p(t0)/100) − 1."),
}


@dataclass
class Spec:
    key: str
    kind: str
    params: dict
    pane: str
    outputs: List[str]
    label: str
    warmup_fetch: int
    other: Optional[str] = None            # cmp: the compare symbol
    extra: dict = field(default_factory=dict)


def _int(s: str, lo: int, hi: int, what: str) -> int:
    if not INT_RE.match(s) or not lo <= int(s) <= hi:
        raise BadParam("ind", f"{what} must be an integer {lo}–{hi}")
    return int(s)


def _src(s: Optional[str], token: str) -> str:
    src = (s or "close").lower()
    if src not in SOURCES:
        raise BadParam("ind", f"'{token}': source must be one of {', '.join(SOURCES)}")
    return src


def _k(s: str) -> float:
    if not K_RE.match(s) or not 0.1 <= float(s) <= 10:
        raise BadParam("ind", "Bollinger k must be a number 0.1–10")
    return float(s)


def hl_of(n: float) -> float:
    """DSL ewm_mean half-life with α = 2/(n+1) (EMA(n))."""
    return math.log(2.0) / -math.log(1.0 - 2.0 / (n + 1.0))


def parse_one(token: str, chart_symbol: Optional[str] = None) -> Spec:
    raw = token.strip()
    if not raw or len(raw) > 40:
        raise BadParam("ind", "empty or too long indicator spec")
    parts = raw.split(":")
    kind = parts[0].lower()
    a = parts[1:]
    show = raw[:40]
    if kind in ("sma", "ema"):
        if not 1 <= len(a) <= 2:
            raise BadParam("ind", f"'{show}': {kind}:<n>[:<source>]")
        n = _int(a[0], 1 if kind == "sma" else 2, 500, f"{kind} n")
        src = _src(a[1] if len(a) > 1 else None, show)
        key = f"{kind}:{n}:{src}"
        warm = n - 1 if kind == "sma" else limits.EMA_WARMUP_FACTOR * n
        return Spec(key, kind, {"n": n, "source": src}, "overlay", [key], f"{kind.upper()} {n} ({src})", warm)
    if kind == "bb":
        if len(a) > 3:
            raise BadParam("ind", f"'{show}': bb[:<n>[:<k>[:<source>]]]")
        n = _int(a[0], 2, 500, "bb n") if len(a) > 0 else 20
        k = _k(a[1]) if len(a) > 1 else 2.0
        src = _src(a[2] if len(a) > 2 else None, show)
        key = f"bb:{n}:{k:g}:{src}"
        return Spec(key, kind, {"n": n, "k": k, "source": src}, "overlay",
                    [f"{key}:mid", f"{key}:upper", f"{key}:lower"], f"BB {n} {k:g} ({src})", n - 1)
    if kind == "rsi":
        if len(a) > 1:
            raise BadParam("ind", f"'{show}': rsi[:<n>]")
        n = _int(a[0], 2, 100, "rsi n") if a else 14
        key = f"rsi:{n}"
        return Spec(key, kind, {"n": n}, "sub", [key], f"RSI {n}", limits.WILDER_WARMUP_FACTOR * n)
    if kind == "macd":
        if len(a) not in (0, 3):
            raise BadParam("ind", f"'{show}': macd or macd:<fast>:<slow>:<signal>")
        f, s, g = ((_int(a[0], 2, 200, "macd fast"), _int(a[1], 2, 200, "macd slow"), _int(a[2], 2, 200, "macd signal"))
                   if a else (12, 26, 9))
        if f >= s:
            raise BadParam("ind", "macd fast must be smaller than slow")
        key = f"macd:{f}:{s}:{g}"
        warm = limits.EMA_WARMUP_FACTOR * (s + g)
        return Spec(key, kind, {"fast": f, "slow": s, "signal": g}, "sub",
                    [f"{key}:macd", f"{key}:signal", f"{key}:hist"], f"MACD {f} {s} {g}", warm)
    if kind == "vwap":
        if a:
            raise BadParam("ind", "'vwap' takes no parameters")
        return Spec("vwap", kind, {}, "overlay", ["vwap"], "VWAP (session)", 0)
    if kind == "cmp":
        if len(a) != 1 or not a[0] or len(a[0]) > 20:
            raise BadParam("ind", f"'{show}': cmp:<SYMBOL>")
        try:
            other = instrument(a[0].replace("/", "-")).symbol
        except ValueError:
            raise BadParam("ind", f"'{show}': not a supported compare symbol")
        if chart_symbol is not None and other == chart_symbol:
            raise BadParam("ind", "the compare symbol must differ from the chart symbol")
        key = f"cmp:{other}"
        return Spec(key, kind, {"symbol": other}, "overlay", [f"{key}:pct", f"{key}:price"], f"vs {other} (%)", 0,
                    other=other)
    raise BadParam("ind", f"unknown indicator '{show}' (sma, ema, bb, rsi, macd, vwap, cmp)")


def parse_list(items, chart_symbol: Optional[str] = None) -> List[Spec]:
    """``items``: the comma-separated ``ind`` string or a list of spec strings. Enforces the limits
    (≤ MAX_INDICATORS, ≤ MAX_SUB_PANES sub-pane specs, ≤ MAX_COMPARE compares, no duplicates)."""
    if isinstance(items, str):
        if len(items) > limits.IND_QUERY_MAX_CHARS:
            raise BadParam("ind", f"at most {limits.IND_QUERY_MAX_CHARS} characters")
        tokens = items.split(",")
    elif isinstance(items, list) and all(isinstance(x, str) for x in items):
        tokens = list(items)
    else:
        raise BadParam("ind", "a list of indicator specs")
    if not tokens or (len(tokens) == 1 and not tokens[0].strip()):
        raise BadParam("ind", "at least one indicator spec")
    if len(tokens) > limits.MAX_INDICATORS:
        raise BadParam("ind", f"at most {limits.MAX_INDICATORS} indicators")
    specs = [parse_one(t, chart_symbol) for t in tokens]
    keys = [s.key for s in specs]
    if len(set(keys)) != len(keys):
        raise BadParam("ind", "duplicate indicator " + next(k for k in keys if keys.count(k) > 1))
    if sum(1 for s in specs if s.pane == "sub") > limits.MAX_SUB_PANES:
        raise BadParam("ind", f"at most {limits.MAX_SUB_PANES} sub-pane indicators (rsi, macd)")
    if sum(1 for s in specs if s.kind == "cmp") > limits.MAX_COMPARE:
        raise BadParam("ind", f"at most {limits.MAX_COMPARE} compare overlays")
    return specs


# --- DSL nodes ------------------------------------------------------------------------------------

def src_node(src: str):
    f = lambda n: {"field": n}  # noqa: E731
    if src in ("open", "high", "low", "close"):
        return f(src)
    parts = {"hl2": ("high", "low"), "hlc3": ("high", "low", "close"), "ohlc4": ("open", "high", "low", "close")}[src]
    return {"op": "div", "args": [{"op": "add", "args": [f(p) for p in parts]}, float(len(parts))]}


def ema_node(x, n):
    return {"op": "ewm_mean", "args": [x, hl_of(n)]}


def nodes(spec: Spec) -> Dict[str, object]:
    """{output key: DSL expression} of a DSL-based spec."""
    p = spec.params
    if spec.kind == "sma":
        return {spec.key: {"op": "sma", "args": [src_node(p["source"]), p["n"]]}}
    if spec.kind == "ema":
        return {spec.key: ema_node(src_node(p["source"]), p["n"])}
    if spec.kind == "bb":
        x = src_node(p["source"])
        mid = {"op": "sma", "args": [x, p["n"]]}
        dev = {"op": "mul", "args": [p["k"], {"op": "ts_std", "args": [x, p["n"]]}]}
        return {f"{spec.key}:mid": mid, f"{spec.key}:upper": {"op": "add", "args": [mid, dev]},
                f"{spec.key}:lower": {"op": "sub", "args": [mid, dev]}}
    if spec.kind == "rsi":
        return {spec.key: {"op": "rsi", "args": [p["n"]]}}
    if spec.kind == "macd":
        c = {"field": "close"}
        macd = {"op": "sub", "args": [ema_node(c, p["fast"]), ema_node(c, p["slow"])]}
        signal = ema_node(macd, p["signal"])
        return {f"{spec.key}:macd": macd, f"{spec.key}:signal": signal,
                f"{spec.key}:hist": {"op": "sub", "args": [macd, signal]}}
    return {}


def warmup_nulls(spec: Spec) -> Dict[str, int]:
    """{output key: number of leading loaded bars without a value} (beyond what the DSL already leaves NaN)."""
    p = spec.params
    if spec.kind in ("sma", "ema"):
        return {spec.key: p["n"] - 1}
    if spec.kind == "bb":
        return {k: p["n"] - 1 for k in spec.outputs}
    if spec.kind == "rsi":
        return {spec.key: p["n"]}
    if spec.kind == "macd":
        k = spec.key
        sig = p["slow"] - 1 + p["signal"] - 1
        return {f"{k}:macd": p["slow"] - 1, f"{k}:signal": sig, f"{k}:hist": sig}
    return {}


def panel_of(bars: List[dict], symbol: str, calendar: str):
    """One-column DSL Panel of the chart's bars (rows = bars; None values → NaN)."""
    import numpy as np
    from strategy_platform.strategy.panel import Panel
    fields = {}
    for name, k in FIELD_OF.items():
        fields[name] = np.array([[float(b[k]) if b.get(k) is not None else np.nan] for b in bars],
                                dtype=float).reshape(len(bars), 1)
    return Panel([to_utc(b["t"]) for b in bars], [symbol], fields, calendar)


def dsl_series(spec: Spec, panel) -> Dict[str, list]:
    """{output key: float ndarray (len = panel rows, NaN = no value)} via the DSL evaluator."""
    from strategy_platform.strategy.evaluate import evaluate_node
    out = {}
    for key, node in nodes(spec).items():
        out[key] = evaluate_node(node, panel)[:, 0].copy()
    for key, n in warmup_nulls(spec).items():
        out[key][:max(0, n)] = float("nan")
    return out


# --- Research-only series ----------------------------------------------------------------------------

def session_key(t: str, asset_class: str) -> str:
    dt = to_utc(t)
    if asset_class == "crypto":
        return dt.date().isoformat()
    return dt.astimezone(agg.NY).date().isoformat()


def session_anchor(day: str, session: str, asset_class: str) -> Optional[str]:
    """UTC ISO start of the session (first bucket) of NY / UTC date ``day``."""
    from datetime import date
    d = date.fromisoformat(day)
    if asset_class == "crypto":
        return iso(agg.utc_midnight(d))
    w = agg.session_window(d, session)
    return iso(w[0]) if w else None


def session_vwap(bars: List[dict], asset_class: str, session: str, first_complete: bool) -> List[Optional[float]]:
    """Σ(vwap·v)/Σv from the session start, reset per session (NY date / UTC day). A bar without vwap/v ends
    the line for the rest of its session; the session of the first loaded bar is incomplete (None) unless
    ``first_complete`` or that bar is the session's first bucket."""
    out: List[Optional[float]] = []
    cur, num, den, broken = None, 0.0, 0.0, False
    first_key = session_key(bars[0]["t"], asset_class) if bars else None
    skip_first = False
    if bars and not first_complete:
        skip_first = bars[0]["t"] != session_anchor(first_key, session, asset_class)
    for b in bars:
        k = session_key(b["t"], asset_class)
        if k != cur:
            cur, num, den, broken = k, 0.0, 0.0, False
        v, w = b.get("v"), b.get("vwap")
        if v is None or w is None or not math.isfinite(float(v)) or not math.isfinite(float(w)):
            broken = True
        if not broken:
            num += float(w) * float(v)
            den += float(v)
        if broken or den <= 0 or (skip_first and k == first_key):
            out.append(None)
        else:
            out.append(num / den)
    return out


def compare_series(window: List[dict], other: List[dict], base: Optional[dict] = None
                   ) -> Tuple[List[Optional[float]], List[Optional[float]], Optional[dict]]:
    """(pct, price, base) — pct = (c_o(t)/c_o(base) − 1)·100, price = c_self(base)·c_o(t)/c_o(base). Without a
    given ``base`` ({t, close, other_close}) it is the first window bar where the other symbol has a bar (None
    values before it); with one (``cmp_base``, review H1) every window bar with an other-symbol bar has a value
    — the same t gives the same value in every request. None where the other symbol has no bar."""
    oc = {b["t"]: float(b["c"]) for b in other if b.get("c") is not None}
    pct: List[Optional[float]] = []
    price: List[Optional[float]] = []
    for b in window:
        c = oc.get(b["t"])
        if base is None and c is not None and c > 0:
            base = {"t": b["t"], "close": float(b["c"]), "other_close": c}
        if base is None or c is None:
            pct.append(None)
            price.append(None)
            continue
        r = c / base["other_close"]
        pct.append((r - 1.0) * 100.0)
        price.append(base["close"] * r)
    return pct, price, base


# --- the route's computation -------------------------------------------------------------------------

def _clean(arr) -> List[Optional[float]]:
    return [float(x) if x is not None and math.isfinite(float(x)) else None for x in arr]


def _load_prefix(data, symbol, tf, session, first_t: str, n: int):
    """(older bars before first_t, more history beyond them?) — at most n bars."""
    if n <= 0:
        return [], True
    try:
        p = data.bars(symbol, tf, session, to_utc(first_t), min(n, max_limit(tf)))
    except BadParam:
        return [], False                       # e.g. the 30-day intraday bound: no older bars
    bars = [b for b in p["bars"] if b["t"] < first_t]
    return bars, bool(p["has_more_left"])


def _load_other(data, other: str, tf, session, window: List[dict], explicit_before):
    """The compare symbol's bars covering the window (≤ 3 pages)."""
    first, last = window[0]["t"], window[-1]["t"]
    before = to_utc(explicit_before) if explicit_before is not None else None
    lim = min(max_limit(tf), len(window) + 50)
    out: List[dict] = []
    for _ in range(3):
        p = data.bars(other, tf, session, before, lim)
        bars = p["bars"]
        out = bars + out
        if not bars or bars[0]["t"] <= first or not p["has_more_left"]:
            break
        before = to_utc(bars[0]["t"])
        lim = max_limit(tf)
    return [b for b in out if first <= b["t"] <= last]


def _bar_at(data, symbol: str, tf: str, session, t, who: str) -> dict:
    """The bar of ``symbol`` starting exactly at ``t`` (cmp_base) — else BadParam (no stand-in bar)."""
    want = iso(t)
    try:
        p = data.bars(symbol, tf, session, to_utc(t) + timedelta(seconds=1), 1)
    except BadParam as ex:
        raise BadParam("cmp_base", f"no {tf} bar of {symbol} at {want} ({ex.user_message})")
    bars = p["bars"]
    if not bars or bars[-1]["t"] != want or bars[-1].get("c") is None or not float(bars[-1]["c"]) > 0:
        raise BadParam("cmp_base", f"{who} {symbol} has no {tf} bar starting at {want} — use the t of a loaded bar")
    return bars[-1]


def compute(data, symbol: str, tf: str, session: Optional[str], before, limit: Optional[int],
            specs: List[Spec], cmp_base=None) -> dict:
    """The /indicators payload (SPEC MR5 notes). ``data``: ``data.DataService`` (blocking)."""
    a = data.asset(symbol)
    sym, ac = a["symbol"], a["asset_class"]
    for s in specs:
        if s.kind == "vwap" and tf not in agg.INTRADAY_TFS:
            raise BadParam("ind", "vwap is a session indicator — intraday timeframes only (1m–4H)")
        if s.kind == "cmp":
            if s.other == sym:
                raise BadParam("ind", "the compare symbol must differ from the chart symbol")
            if instrument(s.other).asset_class != ac:
                raise BadParam("ind", f"compare needs a symbol of the same asset class ({s.other} is not "
                                      f"{'crypto' if ac == 'crypto' else 'a stock/ETF'})")
    win = data.bars(sym, tf, session, before, limit)
    bars = win["bars"]
    eff_session = win["session"]
    result = {"symbol": sym, "tf": tf, "session": eff_session, "t": [b["t"] for b in bars], "series": {},
              "indicators": [], "has_more_left": win["has_more_left"], "sip_through": win["sip_through"],
              "tz": win["tz"], "warmup_loaded": 0, "last_final": bool(bars[-1]["final"]) if bars else True}
    if not bars:
        for s in specs:
            for k in s.outputs:
                result["series"][k] = []
        result["indicators"] = [_meta(s, None) for s in specs]
        return result
    want = 0
    for s in specs:
        w = s.warmup_fetch
        if s.kind == "vwap":
            w = buckets_per_session(tf, eff_session) if ac == "equity" else 1440 // agg.TF_MINUTES[tf]
        want = max(want, w)
    want = min(want, max_limit(tf))
    prefix, more_left = [], False
    if want > 0 and win["has_more_left"]:
        prefix, more_left = _load_prefix(data, sym, tf, session, bars[0]["t"], want)
    loaded = prefix + bars
    off = len(prefix)
    result["warmup_loaded"] = off
    panel = None
    for s in specs:
        base = None
        if s.kind in ("sma", "ema", "bb", "rsi", "macd"):
            if panel is None:
                panel = panel_of(loaded, sym, "utc" if ac == "crypto" else "nyse")
            for k, arr in dsl_series(s, panel).items():
                result["series"][k] = _clean(arr[off:])
        elif s.kind == "vwap":
            vals = session_vwap(loaded, ac, eff_session if ac == "equity" else "utc", first_complete=not more_left)
            result["series"]["vwap"] = vals[off:]
        elif s.kind == "cmp":
            other = _load_other(data, s.other, tf, session, bars, before)
            given = None
            if cmp_base is not None:
                mine = next((b for b in bars if b["t"] == iso(cmp_base)), None) or \
                    _bar_at(data, sym, tf, session, cmp_base, "the chart symbol")
                theirs = next((b for b in other if b["t"] == iso(cmp_base)), None) or \
                    _bar_at(data, s.other, tf, session, cmp_base, "the compare symbol")
                given = {"t": iso(cmp_base), "close": float(mine["c"]), "other_close": float(theirs["c"])}
                if not (given["close"] > 0 and given["other_close"] > 0):
                    raise BadParam("cmp_base", f"the base bar at {given['t']} has no usable close")
            pct, price, base = compare_series(bars, other, given)
            result["series"][f"{s.key}:pct"] = pct
            result["series"][f"{s.key}:price"] = price
        result["indicators"].append(_meta(s, base))
    return result


def _meta(s: Spec, base) -> dict:
    m = {"key": s.key, "kind": s.kind, "pane": s.pane, "outputs": list(s.outputs), "params": dict(s.params),
         "label": s.label, "warmup_bars": max(warmup_nulls(s).values()) if warmup_nulls(s) else 0,
         "note": NOTES.get(s.kind)}
    if s.kind == "cmp":
        m["base"] = base
    return m
