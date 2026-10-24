"""Expression DSL of StrategySpec v1 — operator registry, catalogue,
``expr_text`` (SPEC §7.4).

Nodes (JSON data, never code):
- a finite number literal (bool is not a number);
- ``{"param": name}``, ``{"ref": feature_name}``, ``{"field": f}``,
  ``{"ext": name}`` (external feature, §7.8);
- ``{"op": name, "args": [...]}``.

Argument kinds of an operator:
- ``x``        any expression (a dates × symbols panel);
- ``window``   integer number of bars: a positive int literal or an integer
               parameter, ``min_window`` ≤ n ≤ MAX_WINDOW;
- ``const``    a number literal or a parameter (e.g. ``pow``'s exponent);
- ``halflife`` a positive number literal or parameter ≤ MAX_WINDOW.

Window semantics (all per symbol, only bars ≤ t are read): ``lag(x, n)`` =
x n bars earlier; ``ts_*(x, n)`` use the n bars t-n+1..t and need all n
values valid (else NaN). Lookback (facts.max_lookback, the history to load):
lag-like ops add n, window ops add n-1, Wilder ops (rsi, atr) add 4n and
``ewm_mean`` adds ceil(4·halflife) as warm-up for the recursive smoothing.

Part of UFOS.ai, a proof-of-concept portfolio project.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Tuple

FIELDS = ("open", "high", "low", "close", "volume", "vwap", "trade_count")

EW = "element-wise"
TS = "time-series"
HELPER = "helper"
CS = "cross-sectional"


@dataclass(frozen=True)
class Op:
    name: str
    category: str
    args: Tuple[Tuple[str, str], ...]     # (arg name, kind)
    description: str
    variadic_max: int = 0                  # > 0: the LAST kind repeats up to this many args in total
    min_window: int = 1
    lookback: str = ""                     # "" | lag | window | wilder | ewm
    fields: Tuple[str, ...] = ()           # fields read implicitly (helpers)

    @property
    def min_args(self) -> int:
        return len(self.args)

    @property
    def max_args(self) -> int:
        return self.variadic_max or len(self.args)

    def kind_at(self, i: int) -> str:
        return self.args[min(i, len(self.args) - 1)][1]

    def signature(self) -> str:
        parts = [f"{n}" if k == "x" else f"{n}: {k}" for n, k in self.args]
        if self.variadic_max:
            parts.append("…")
        return f"{self.name}({', '.join(parts)})"


def _ops() -> Dict[str, Op]:
    X, W, C, H = "x", "window", "const", "halflife"
    o: List[Op] = []
    for name, sym in (("add", "+"), ("mul", "×"), ("min", "min"), ("max", "max"), ("and", "and"), ("or", "or")):
        desc = {"add": "Sum of the arguments.", "mul": "Product of the arguments.",
                "min": "Element-wise minimum.", "max": "Element-wise maximum.",
                "and": "1 where all arguments are non-zero, else 0 (NaN if any is NaN).",
                "or": "1 where any argument is non-zero, else 0 (NaN if any is NaN)."}[name]
        o.append(Op(name, EW, (("a", X), ("b", X)), desc, variadic_max=16))
    o += [
        Op("sub", EW, (("a", X), ("b", X)), "a − b."),
        Op("div", EW, (("a", X), ("b", X)), "a / b; division by 0 gives NaN."),
        Op("neg", EW, (("x", X),), "−x."),
        Op("abs", EW, (("x", X),), "|x|."),
        Op("sign", EW, (("x", X),), "−1, 0 or 1."),
        Op("log", EW, (("x", X),), "Natural log; x ≤ 0 gives NaN."),
        Op("sqrt", EW, (("x", X),), "Square root; x < 0 gives NaN."),
        Op("exp", EW, (("x", X),), "e^x (overflow gives NaN)."),
        Op("tanh", EW, (("x", X),), "Hyperbolic tangent (squashes to −1..1)."),
        Op("pow", EW, (("x", X), ("exponent", C)), "x^exponent; the exponent is a constant; invalid results NaN."),
        Op("signed_power", EW, (("x", X), ("exponent", C)), "sign(x)·|x|^exponent (exponent constant)."),
        Op("clip", EW, (("x", X), ("lo", X), ("hi", X)), "x limited to [lo, hi]."),
        Op("where", EW, (("cond", X), ("a", X), ("b", X)), "a where cond ≠ 0, else b (NaN where cond is NaN)."),
        Op("gt", EW, (("a", X), ("b", X)), "1 if a > b else 0."),
        Op("ge", EW, (("a", X), ("b", X)), "1 if a ≥ b else 0."),
        Op("lt", EW, (("a", X), ("b", X)), "1 if a < b else 0."),
        Op("le", EW, (("a", X), ("b", X)), "1 if a ≤ b else 0."),
        Op("eq", EW, (("a", X), ("b", X)), "1 if a = b else 0."),
        Op("ne", EW, (("a", X), ("b", X)), "1 if a ≠ b else 0."),
        Op("not", EW, (("x", X),), "1 where x = 0, else 0."),
        Op("is_nan", EW, (("x", X),), "1 where x has no value, else 0."),
        Op("fill_nan", EW, (("x", X), ("value", X)), "x, with missing values replaced by value (same date)."),
        Op("nan", EW, (), "A missing value (excludes the symbol where selected, e.g. where(c, x, nan()))."),
        Op("lag", TS, (("x", X), ("n", W)), "x n bars earlier (n ≥ 1).", lookback="lag"),
        Op("delta", TS, (("x", X), ("n", W)), "x − lag(x, n).", lookback="lag"),
        Op("pct_change", TS, (("x", X), ("n", W)), "x / lag(x, n) − 1.", lookback="lag"),
        Op("ts_mean", TS, (("x", X), ("n", W)), "Mean of the last n values.", lookback="window"),
        Op("sma", TS, (("x", X), ("n", W)), "Simple moving average (= ts_mean).", lookback="window"),
        Op("ts_std", TS, (("x", X), ("n", W)), "Sample standard deviation of the last n values.", min_window=2,
           lookback="window"),
        Op("ts_sum", TS, (("x", X), ("n", W)), "Sum of the last n values.", lookback="window"),
        Op("ts_min", TS, (("x", X), ("n", W)), "Minimum of the last n values.", lookback="window"),
        Op("ts_max", TS, (("x", X), ("n", W)), "Maximum of the last n values.", lookback="window"),
        Op("ts_argmin", TS, (("x", X), ("n", W)), "Bars since the window minimum (0 = this bar; ties: most recent).",
           lookback="window"),
        Op("ts_argmax", TS, (("x", X), ("n", W)), "Bars since the window maximum (0 = this bar; ties: most recent).",
           lookback="window"),
        Op("ts_rank", TS, (("x", X), ("n", W)), "Rank of the current value within the last n values, 0..1 "
           "(average ties).", min_window=2, lookback="window"),
        Op("ts_zscore", TS, (("x", X), ("n", W)), "(x − ts_mean) / ts_std over the last n values.", min_window=2,
           lookback="window"),
        Op("ts_product", TS, (("x", X), ("n", W)), "Product of the last n values.", lookback="window"),
        Op("ts_skew", TS, (("x", X), ("n", W)), "Skewness (Fisher–Pearson) of the last n values.", min_window=3,
           lookback="window"),
        Op("ts_corr", TS, (("x", X), ("y", X), ("n", W)), "Pearson correlation of x and y over n bars.",
           min_window=2, lookback="window"),
        Op("ts_cov", TS, (("x", X), ("y", X), ("n", W)), "Sample covariance of x and y over n bars.", min_window=2,
           lookback="window"),
        Op("decay_linear", TS, (("x", X), ("n", W)), "Linearly weighted mean (weight n for this bar … 1).",
           lookback="window"),
        Op("ewm_mean", TS, (("x", X), ("halflife", H)), "Exponentially weighted mean with the given half-life "
           "(bars); restarts after a missing value.", lookback="ewm"),
        Op("returns", HELPER, (("n", W),), "pct_change(close, n).", lookback="lag", fields=("close",)),
        Op("rsi", HELPER, (("n", W),), "Wilder's RSI of the close, 0..100.", min_window=2, lookback="wilder",
           fields=("close",)),
        Op("atr", HELPER, (("n", W),), "Wilder's average true range (high, low, close).", lookback="wilder",
           fields=("high", "low", "close")),
        Op("cs_rank", CS, (("x", X),), "Rank across symbols on each date, 0..1 (average ties; one value → 0.5)."),
        Op("cs_zscore", CS, (("x", X),), "(x − mean) / std across symbols on each date."),
        Op("cs_demean", CS, (("x", X),), "x − mean across symbols on each date."),
        Op("cs_scale", CS, (("x", X),), "x / Σ|x| across symbols on each date (Σ|result| = 1)."),
        Op("cs_winsorize", CS, (("x", X), ("k", C)), "x clipped to mean ± k·std across symbols on each date."),
    ]
    return {op.name: op for op in o}


OPS: Dict[str, Op] = _ops()
CATEGORY_ORDER = (EW, TS, HELPER, CS)


def catalogue() -> List[dict]:
    """Operator help for the editor: [{name, category, signature, description}]."""
    out = []
    for cat in CATEGORY_ORDER:
        for op in sorted((o for o in OPS.values() if o.category == cat), key=lambda o: o.name):
            out.append({"name": op.name, "category": op.category, "signature": op.signature(),
                        "description": op.description})
    return out


def node_kind(node) -> str:
    """number | param | ref | field | ext | op | invalid."""
    if isinstance(node, bool):
        return "invalid"
    if isinstance(node, (int, float)):
        return "number"
    if isinstance(node, dict):
        for k in ("param", "ref", "field", "ext", "op"):
            if k in node:
                return k
    return "invalid"


# --- readable text ----------------------------------------------------------------

_INFIX = {"add": ("+", 1), "sub": ("-", 1), "mul": ("*", 2), "div": ("/", 2),
          "gt": (">", 0), "ge": (">=", 0), "lt": ("<", 0), "le": ("<=", 0), "eq": ("==", 0), "ne": ("!=", 0)}


def _num(v) -> str:
    if isinstance(v, int):
        return str(v)
    if math.isfinite(v) and v == int(v) and abs(v) < 1e15:
        return str(int(v)) if abs(v) >= 1 or v == 0 else repr(v)
    return repr(v)


def expr_text(node) -> str:
    """One-line readable form, e.g. ``cs_rank(pct_change(close, 252) - pct_change(close, 21))``.
    Parameters print as ``$name``, external features as ``ext:name``, feature
    references by name. Never raises (malformed parts print as ``?``)."""
    return _text(node, 0)[0]


def _text(node, depth: int):
    """(text, precedence) — precedence 9 = atom."""
    if depth > 64:
        return "…", 9
    kind = node_kind(node)
    if kind == "number":
        s = _num(node)
        return (s, 9) if not s.startswith("-") else (f"({s})", 9)
    if kind == "param":
        return f"${node.get('param')}", 9
    if kind == "ref":
        return str(node.get("ref")), 9
    if kind == "field":
        return str(node.get("field")), 9
    if kind == "ext":
        return f"ext:{node.get('ext')}", 9
    if kind != "op":
        return "?", 9
    name = node.get("op")
    args = node.get("args") if isinstance(node.get("args"), list) else []
    if name in _INFIX and len(args) >= 2:
        sym, prec = _INFIX[name]
        parts = []
        for i, a in enumerate(args):
            t, p = _text(a, depth + 1)
            # left-assoc: right operands of − and / need parentheses at equal precedence
            need = p < prec or (p == prec and i > 0 and name in ("sub", "div")) or (prec == 0 and p == 0)
            parts.append(f"({t})" if need else t)
        return f" {sym} ".join(parts), prec
    if name == "neg" and len(args) == 1:
        t, p = _text(args[0], depth + 1)
        return (f"-{t}" if p >= 3 else f"-({t})"), 3
    inner = ", ".join(_text(a, depth + 1)[0] for a in args)
    return f"{name}({inner})", 9
