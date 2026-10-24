"""numpy evaluator: Panel -> signal panel (SPEC §7.7). Causal by
construction: row t of every operator depends only on rows ≤ t (time-series
windows look back, recursive smoothers start at the panel's first row,
cross-sectional ops use row t only), so ``evaluate(spec, panel[:t+1])[t] ==
evaluate(spec, panel)[t]`` (tested for every operator and library entry).

``evaluate(spec, panel, *, ext_values=None, param_values=None) -> ndarray``
(dates × symbols, NaN = no value). The spec must be valid (validator.validate);
anything malformed raises ``EvaluationError``.

- Parameters take their spec ``value`` (``param_values`` may override, e.g.
  for a later parameter search).
- ``ext_values``: ``{name: values}`` pre-loaded external features, where
  values is an ndarray of the panel's shape, ``{symbol: {date: value}}``
  (``ExtFeature.values_for``) or ``{date: value}`` (global). Missing names
  are loaded via ``strategy.features.get(name).values_for(symbols, start,
  end)`` (start = first panel date − EXT_HISTORY_DAYS). Values are aligned
  as of each panel date: the newest value dated ≤ the row's date (providers
  date a value by when it was known, §7.8). A failing provider raises its
  UserFacingError — no stand-in.
- Work budget: ``estimate_cost`` (Σ per-cell operator cost × dates × symbols)
  above ``limits.MAX_EVAL_CELL_OPS`` → ``EvaluationTooExpensive``
  (UserFacingError) before anything is computed. Rolling windows are
  processed in bounded chunks (memory O(dates × symbols)).
- Time-series ops need all n values of their window (else NaN); division by
  0, log ≤ 0, sqrt < 0 and any non-finite result give NaN.

Part of UFOS.ai, a proof-of-concept portfolio project.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Dict, Optional

from ..core.errors import UserFacingError
from . import dsl
from .limits import MAX_EVAL_CELL_OPS, ROLLING_CHUNK_ELEMENTS

EXT_HISTORY_DAYS = 400


class EvaluationError(ValueError):
    """The spec cannot be evaluated (it was not validated, or is malformed)."""


class EvaluationTooExpensive(UserFacingError):
    """The estimated work of one evaluation exceeds limits.MAX_EVAL_CELL_OPS."""


# per-cell cost of an operator (beyond its arguments): window ops ≈ n, recursive
# smoothers and cross-sectional ranks ≈ a small constant, everything else 1
_RECURSIVE_COST = 10
_CS_COST = 10


def estimate_cost(spec: dict, shape, *, param_values: Optional[dict] = None, all_features: bool = False) -> int:
    """Estimated cell-operations of evaluating ``spec`` on a panel of ``shape``
    (each feature counted once, as the evaluator memoises references)."""
    T, S = int(shape[0]), int(shape[1])
    params = {k: (p.get("value") if isinstance(p, dict) else None) for k, p in (spec.get("parameters") or {}).items()}
    params.update(param_values or {})
    features = spec.get("features") or {}
    seen: set = set()

    def num(node) -> float:
        k = dsl.node_kind(node)
        if k == "number":
            return float(node)
        if k == "param" and isinstance(params.get(node.get("param")), (int, float)):
            return float(params[node["param"]])
        return 1.0

    per_cell = 0.0
    stack = [spec.get("signal")] + ([{"ref": n} for n in features] if all_features else [])
    while stack:
        node = stack.pop()
        kind = dsl.node_kind(node)
        if kind == "ref":
            name = node.get("ref")
            if name in features and name not in seen:
                seen.add(name)
                stack.append(features[name])
            continue
        per_cell += 1
        if kind != "op":
            continue
        op = dsl.OPS.get(node.get("op"))
        if op is None:
            continue
        args = node.get("args") or []
        for i, a in enumerate(args):
            k = op.kind_at(i)
            if k == "x":
                stack.append(a)
            elif k == "window" and op.lookback == "window":
                per_cell += abs(num(a)) * (2 if op.name in ("ts_corr", "ts_cov") else 1)
        if op.lookback in ("wilder", "ewm"):
            per_cell += _RECURSIVE_COST
        if op.category == dsl.CS:
            per_cell += _CS_COST
    return int(per_cell * T * S)


def check_cost(spec: dict, panel, *, param_values: Optional[dict] = None, all_features: bool = False) -> int:
    cost = estimate_cost(spec, panel.shape, param_values=param_values, all_features=all_features)
    if cost > MAX_EVAL_CELL_OPS:
        raise EvaluationTooExpensive(
            f"This strategy is too expensive to evaluate ({cost:.3g} estimated cell-operations > limit "
            f"{MAX_EVAL_CELL_OPS:.3g} for {panel.shape[1]} symbols × {panel.shape[0]} days) — use fewer "
            "symbols, shorter windows or fewer nested windows.")
    return cost


def _np():
    import numpy as np
    return np


# --- ext alignment ---------------------------------------------------------------------

def _to_date(k) -> date:
    if isinstance(k, datetime):
        return k.date()
    if isinstance(k, date):
        return k
    return date.fromisoformat(str(k)[:10])


def _asof_series(mapping, pord):
    np = _np()
    items = []
    for k, v in (mapping or {}).items():
        if v is None:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            raise EvaluationError(f"external feature value {v!r} is not a number") from None
        items.append((_to_date(k).toordinal(), fv))
    out = np.full(len(pord), np.nan)
    if not items:
        return out
    items.sort()
    ords = np.array([i[0] for i in items])
    vals = np.array([i[1] for i in items])
    idx = np.searchsorted(ords, pord, side="right") - 1
    ok = idx >= 0
    out[ok] = vals[idx[ok]]
    out[~np.isfinite(out)] = np.nan
    return out


def align_ext(values, panel):
    """values (ndarray | {symbol: {date: v}} | {date: v}) -> float ndarray of the panel's shape."""
    np = _np()
    if hasattr(values, "shape"):
        arr = np.asarray(values, dtype=float)
        if arr.shape != panel.shape:
            raise EvaluationError(f"external values have shape {arr.shape}, the panel {panel.shape}")
        return arr
    if not isinstance(values, dict):
        raise EvaluationError("external values must be an array or a mapping")
    pord = np.array([d.toordinal() for d in panel.dates], dtype=np.int64)
    out = np.full(panel.shape, np.nan)
    if values and all(isinstance(v, dict) for v in values.values()):
        for j, s in enumerate(panel.symbols):
            out[:, j] = _asof_series(values.get(s), pord)
    else:
        col = _asof_series(values, pord)
        out[:] = col[:, None]
    return out


def load_ext(name: str, panel):
    """Load one external feature for the panel's symbols/dates via the registry (B2)."""
    from . import features
    try:
        feat = features.get(name)
    except KeyError:
        raise EvaluationError(f"unknown external feature {name!r}") from None
    if not getattr(feat, "point_in_time", False):
        raise EvaluationError(f"external feature {name!r} is not point-in-time")
    if not panel.dates:
        return {}
    start = panel.dates[0] - timedelta(days=EXT_HISTORY_DAYS)
    return feat.values_for(list(panel.symbols), start, panel.dates[-1])


# --- operator implementations ------------------------------------------------------------

def _clean(a):
    np = _np()
    a = np.asarray(a, dtype=float)
    a[~np.isfinite(a)] = np.nan
    return a


def _nan_window_mask(x, n):
    """True where the window of n rows ending at t contains a NaN or t < n-1."""
    np = _np()
    T = x.shape[0]
    bad = np.ones(x.shape, dtype=bool)
    if T < n:
        return bad
    c = np.concatenate([np.zeros((1,) + x.shape[1:], dtype=np.int64), np.cumsum(np.isnan(x), axis=0)])
    cnt = c[n:] - c[:-n]
    bad[n - 1:] = cnt > 0
    return bad


def _chunks(T: int, S: int, n: int):
    """(row0, row1, col0, col1) blocks of OUTPUT rows (window ends n-1+row) and
    columns so that each window view holds ≤ ROLLING_CHUNK_ELEMENTS values —
    memory stays O(T·S) whatever the window (review fix 2026-10-02: one view of
    (T-n+1)·S·n values plus fn's temporaries could take gigabytes). Every window
    is still reduced on its own, so results do not depend on the chunking."""
    rows_out = T - n + 1
    max_cells = max(1, ROLLING_CHUNK_ELEMENTS // n)
    sc = min(S, max_cells)
    tc = max(1, max_cells // max(sc, 1))
    for c0 in range(0, S, sc):
        for r0 in range(0, rows_out, tc):
            yield r0, min(rows_out, r0 + tc), c0, min(S, c0 + sc)


def _rolling(x, n, fn):
    """fn(window_view (rows, cols, n)) -> (rows, cols); NaN where the window is incomplete."""
    np = _np()
    out = np.full(x.shape, np.nan)
    T, S = x.shape
    if T >= n and S:
        swv = np.lib.stride_tricks.sliding_window_view
        with np.errstate(all="ignore"):
            for r0, r1, c0, c1 in _chunks(T, S, n):
                out[n - 1 + r0:n - 1 + r1, c0:c1] = fn(swv(x[r0:r1 + n - 1, c0:c1], n, axis=0))
    out[_nan_window_mask(x, n)] = np.nan
    return out


def _rolling2(x, y, n, fn):
    np = _np()
    out = np.full(x.shape, np.nan)
    T, S = x.shape
    if T >= n and S:
        swv = np.lib.stride_tricks.sliding_window_view
        with np.errstate(all="ignore"):
            for r0, r1, c0, c1 in _chunks(T, S, n):
                out[n - 1 + r0:n - 1 + r1, c0:c1] = fn(swv(x[r0:r1 + n - 1, c0:c1], n, axis=0),
                                                       swv(y[r0:r1 + n - 1, c0:c1], n, axis=0))
    out[_nan_window_mask(x, n) | _nan_window_mask(y, n)] = np.nan
    return out


def _lag(x, n):
    np = _np()
    out = np.full(x.shape, np.nan)
    if n < x.shape[0]:
        out[n:] = x[:-n]
    return out


def _div(a, b):
    np = _np()
    with np.errstate(all="ignore"):
        return np.where(b == 0, np.nan, a / np.where(b == 0, 1.0, b))


def _ts_rank(v):
    np = _np()
    n = v.shape[-1]
    cur = v[..., -1:]
    less = (v < cur).sum(-1)
    eq = (v == cur).sum(-1)
    rank = less + (eq + 1) / 2.0
    return (rank - 1.0) / (n - 1.0)


def _ts_std(v):
    return v.std(axis=-1, ddof=1)


def _ts_zscore(v):
    np = _np()
    sd = v.std(axis=-1, ddof=1)
    return np.where(sd > 0, (v[..., -1] - v.mean(axis=-1)) / np.where(sd > 0, sd, 1.0), np.nan)


def _ts_skew(v):
    np = _np()
    m = v.mean(axis=-1, keepdims=True)
    d = v - m
    m2 = (d * d).mean(axis=-1)
    m3 = (d * d * d).mean(axis=-1)
    return np.where(m2 > 0, m3 / np.where(m2 > 0, m2, 1.0) ** 1.5, np.nan)


def _ts_cov(vx, vy):
    mx = vx.mean(axis=-1, keepdims=True)
    my = vy.mean(axis=-1, keepdims=True)
    n = vx.shape[-1]
    return ((vx - mx) * (vy - my)).sum(-1) / (n - 1)


def _ts_corr(vx, vy):
    np = _np()
    mx = vx.mean(axis=-1, keepdims=True)
    my = vy.mean(axis=-1, keepdims=True)
    dx, dy = vx - mx, vy - my
    sxx = (dx * dx).sum(-1)
    syy = (dy * dy).sum(-1)
    den = np.sqrt(sxx * syy)
    return np.where(den > 0, (dx * dy).sum(-1) / np.where(den > 0, den, 1.0), np.nan)


def _bars_since(v, use_max):
    r = v[..., ::-1]
    return (r.argmax(-1) if use_max else r.argmin(-1)).astype(float)


def _decay_linear(v):
    np = _np()
    n = v.shape[-1]
    w = np.arange(1, n + 1, dtype=float)
    return (v * w).sum(-1) / w.sum()


def _ewm(x, halflife):
    np = _np()
    alpha = 1.0 - 0.5 ** (1.0 / float(halflife))
    out = np.full(x.shape, np.nan)
    m = np.full(x.shape[1:], np.nan)
    for t in range(x.shape[0]):
        xt = x[t]
        bad = np.isnan(xt)
        fresh = np.isnan(m) & ~bad
        upd = ~np.isnan(m) & ~bad
        m = np.where(upd, alpha * xt + (1.0 - alpha) * m, m)
        m = np.where(fresh, xt, m)
        m = np.where(bad, np.nan, m)      # restart after a missing value
        out[t] = m
    return out


def _wilder(series, n, *, first_valid_row=1):
    """Wilder smoothing: seed = mean of the first n consecutive valid values,
    then avg = (avg·(n−1) + v)/n. A missing value resets (NaN until n new
    values). Returns the smoothed array (NaN while not ready)."""
    np = _np()
    T = series.shape[0]
    shape = series.shape[1:]
    out = np.full(series.shape, np.nan)
    cnt = np.zeros(shape, dtype=np.int64)
    acc = np.zeros(shape)
    avg = np.full(shape, np.nan)
    for t in range(first_valid_row, T):
        v = series[t]
        bad = np.isnan(v)
        ready = cnt >= n
        avg = np.where(ready & ~bad, (avg * (n - 1) + v) / n, avg)
        seeding = ~ready & ~bad
        acc = np.where(seeding, acc + v, acc)
        cnt = np.where(seeding, cnt + 1, cnt)
        just = seeding & (cnt == n)
        avg = np.where(just, acc / n, avg)
        cnt = np.where(bad, 0, cnt)
        acc = np.where(bad, 0.0, acc)
        avg = np.where(bad, np.nan, avg)
        out[t] = np.where(cnt >= n, avg, np.nan)
    return out


def _rsi(close, n):
    np = _np()
    d = np.full(close.shape, np.nan)
    d[1:] = close[1:] - close[:-1]
    g = np.where(np.isnan(d), np.nan, np.maximum(d, 0.0))
    l = np.where(np.isnan(d), np.nan, np.maximum(-d, 0.0))
    ag = _wilder(g, n)
    al = _wilder(l, n)
    with np.errstate(all="ignore"):
        rs = 100.0 - 100.0 / (1.0 + ag / np.where(al == 0, 1.0, al))
    out = np.where(al == 0, np.where(ag == 0, 50.0, 100.0), rs)
    out[np.isnan(ag) | np.isnan(al)] = np.nan
    return out


def _atr(high, low, close, n):
    np = _np()
    prev = _lag(close, 1)
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev), np.abs(low - prev)))
    tr[np.isnan(high) | np.isnan(low) | np.isnan(prev)] = np.nan
    return _wilder(tr, n)


def _cs_stats(x):
    np = _np()
    valid = ~np.isnan(x)
    cnt = valid.sum(axis=1)
    with np.errstate(all="ignore"):
        mean = np.where(valid, x, 0.0).sum(axis=1) / cnt
        var = np.where(valid, (x - mean[:, None]) ** 2, 0.0).sum(axis=1) / cnt
    return mean, np.sqrt(var), cnt


def _cs_rank(x):
    np = _np()
    out = np.full(x.shape, np.nan)
    for t in range(x.shape[0]):
        row = x[t]
        idx = np.flatnonzero(~np.isnan(row))
        m = idx.size
        if m == 0:
            continue
        if m == 1:
            out[t, idx] = 0.5
            continue
        _u, inv, counts = np.unique(row[idx], return_inverse=True, return_counts=True)
        start = np.cumsum(counts) - counts
        rank = start + (counts + 1) / 2.0
        out[t, idx] = (rank[inv.reshape(-1)] - 1.0) / (m - 1.0)
    return out


def _cs_zscore(x):
    np = _np()
    mean, sd, _cnt = _cs_stats(x)
    with np.errstate(all="ignore"):
        return np.where((sd > 0)[:, None], (x - mean[:, None]) / np.where(sd > 0, sd, 1.0)[:, None], np.nan)


def _cs_demean(x):
    mean, _sd, _cnt = _cs_stats(x)
    return x - mean[:, None]


def _cs_scale(x):
    np = _np()
    s = np.where(np.isnan(x), 0.0, np.abs(x)).sum(axis=1)
    return np.where((s > 0)[:, None], x / np.where(s > 0, s, 1.0)[:, None], np.nan)


def _cs_winsorize(x, k):
    np = _np()
    mean, sd, _cnt = _cs_stats(x)
    lo = (mean - k * sd)[:, None]
    hi = (mean + k * sd)[:, None]
    return np.minimum(np.maximum(x, lo), hi)


def _bool(a):
    return (a != 0)


def _cmp(fn, a, b):
    np = _np()
    out = fn(a, b).astype(float)
    out[np.isnan(a) | np.isnan(b)] = np.nan
    return out


# --- evaluator ---------------------------------------------------------------------------

class Evaluator:
    def __init__(self, spec: dict, panel, *, ext_values: Optional[dict] = None,
                 param_values: Optional[dict] = None):
        self.spec = spec
        self.panel = panel
        self.features = spec.get("features") or {}
        self.params = {k: (p.get("value") if isinstance(p, dict) else None)
                       for k, p in (spec.get("parameters") or {}).items()}
        for k, v in (param_values or {}).items():
            if k not in self.params:
                raise EvaluationError(f"unknown parameter {k!r}")
            self.params[k] = v
        self.ext_values = dict(ext_values or {})
        self._ext_aligned: Dict[str, object] = {}
        self._refs: Dict[str, object] = {}
        self._active: set = set()

    def full(self, v):
        np = _np()
        return np.full(self.panel.shape, float(v))

    def const(self, node) -> float:
        kind = dsl.node_kind(node)
        if kind == "number":
            return float(node)
        if kind == "param":
            v = self.params.get(node["param"])
            if v is None:
                raise EvaluationError(f"unknown parameter {node['param']!r}")
            return float(v)
        raise EvaluationError("expected a constant or a parameter")

    def window(self, node) -> int:
        v = self.const(node)
        if v != int(v) or v < 1:
            raise EvaluationError(f"invalid window {v!r}")
        return int(v)

    def feature(self, name: str):
        if name in self._refs:
            return self._refs[name]
        if name not in self.features:
            raise EvaluationError(f"unknown feature {name!r}")
        if name in self._active:
            raise EvaluationError(f"feature reference cycle at {name!r}")
        self._active.add(name)
        try:
            self._refs[name] = self.eval(self.features[name])
        finally:
            self._active.discard(name)
        return self._refs[name]

    def ext(self, name: str):
        if name not in self._ext_aligned:
            raw = self.ext_values[name] if name in self.ext_values else load_ext(name, self.panel)
            self._ext_aligned[name] = align_ext(raw, self.panel)
        return self._ext_aligned[name]

    def eval(self, node):
        kind = dsl.node_kind(node)
        if kind == "number":
            return self.full(node)
        if kind == "param":
            return self.full(self.const(node))
        if kind == "ref":
            return self.feature(node["ref"])
        if kind == "field":
            if node["field"] not in dsl.FIELDS:
                raise EvaluationError(f"unknown field {node['field']!r}")
            return self.panel.fields[node["field"]]
        if kind == "ext":
            return self.ext(node["ext"])
        if kind != "op":
            raise EvaluationError("malformed expression node")
        op = dsl.OPS.get(node["op"])
        if op is None:
            raise EvaluationError(f"unknown operator {node['op']!r}")
        args = node.get("args") or []
        if not (op.min_args <= len(args) <= op.max_args):
            raise EvaluationError(f"{op.name}: wrong number of arguments")
        np = _np()
        with np.errstate(all="ignore"):
            out = self._apply(op.name, args)
        return _clean(out)

    def _apply(self, name, args):
        np = _np()
        X = lambda i: self.eval(args[i])  # noqa: E731
        P = self.panel.fields
        if name in ("add", "mul", "min", "max"):
            vals = [X(i) for i in range(len(args))]
            fn = {"add": np.add, "mul": np.multiply, "min": np.minimum, "max": np.maximum}[name]
            out = vals[0].copy()
            for v in vals[1:]:
                out = fn(out, v)
            return out
        if name in ("and", "or"):
            vals = [X(i) for i in range(len(args))]
            nan = np.zeros(self.panel.shape, dtype=bool)
            acc = _bool(vals[0])
            nan |= np.isnan(vals[0])
            for v in vals[1:]:
                acc = (acc & _bool(v)) if name == "and" else (acc | _bool(v))
                nan |= np.isnan(v)
            out = acc.astype(float)
            out[nan] = np.nan
            return out
        if name == "sub":
            return X(0) - X(1)
        if name == "div":
            return _div(X(0), X(1))
        if name == "neg":
            return -X(0)
        if name == "abs":
            return np.abs(X(0))
        if name == "sign":
            return np.sign(X(0))
        if name == "log":
            x = X(0)
            return np.where(x > 0, np.log(np.where(x > 0, x, 1.0)), np.nan)
        if name == "sqrt":
            x = X(0)
            return np.where(x >= 0, np.sqrt(np.where(x >= 0, x, 0.0)), np.nan)
        if name == "exp":
            return np.exp(X(0))
        if name == "tanh":
            return np.tanh(X(0))
        if name == "pow":
            return np.power(X(0), self.const(args[1]))
        if name == "signed_power":
            x = X(0)
            return np.sign(x) * np.power(np.abs(x), self.const(args[1]))
        if name == "clip":
            return np.minimum(np.maximum(X(0), X(1)), X(2))
        if name == "where":
            c, a, b = X(0), X(1), X(2)
            out = np.where(_bool(c), a, b)
            out[np.isnan(c)] = np.nan
            return out
        if name in ("gt", "ge", "lt", "le", "eq", "ne"):
            fn = {"gt": np.greater, "ge": np.greater_equal, "lt": np.less, "le": np.less_equal,
                  "eq": np.equal, "ne": np.not_equal}[name]
            return _cmp(fn, X(0), X(1))
        if name == "not":
            x = X(0)
            out = (x == 0).astype(float)
            out[np.isnan(x)] = np.nan
            return out
        if name == "is_nan":
            return np.isnan(X(0)).astype(float)
        if name == "fill_nan":
            x, v = X(0), X(1)
            return np.where(np.isnan(x), v, x)
        if name == "nan":
            return self.full(np.nan)
        # time series
        if name == "lag":
            return _lag(X(0), self.window(args[1]))
        if name == "delta":
            x = X(0)
            return x - _lag(x, self.window(args[1]))
        if name == "pct_change":
            x = X(0)
            return _div(x, _lag(x, self.window(args[1]))) - 1.0
        if name == "returns":
            c = P["close"]
            return _div(c, _lag(c, self.window(args[0]))) - 1.0
        if name in ("ts_mean", "sma"):
            return _rolling(X(0), self.window(args[1]), lambda v: v.mean(axis=-1))
        if name == "ts_std":
            return _rolling(X(0), self.window(args[1]), _ts_std)
        if name == "ts_sum":
            return _rolling(X(0), self.window(args[1]), lambda v: v.sum(axis=-1))
        if name == "ts_min":
            return _rolling(X(0), self.window(args[1]), lambda v: v.min(axis=-1))
        if name == "ts_max":
            return _rolling(X(0), self.window(args[1]), lambda v: v.max(axis=-1))
        if name == "ts_argmin":
            return _rolling(X(0), self.window(args[1]), lambda v: _bars_since(v, False))
        if name == "ts_argmax":
            return _rolling(X(0), self.window(args[1]), lambda v: _bars_since(v, True))
        if name == "ts_rank":
            return _rolling(X(0), self.window(args[1]), _ts_rank)
        if name == "ts_zscore":
            return _rolling(X(0), self.window(args[1]), _ts_zscore)
        if name == "ts_product":
            return _rolling(X(0), self.window(args[1]), lambda v: v.prod(axis=-1))
        if name == "ts_skew":
            return _rolling(X(0), self.window(args[1]), _ts_skew)
        if name == "ts_corr":
            return _rolling2(X(0), X(1), self.window(args[2]), _ts_corr)
        if name == "ts_cov":
            return _rolling2(X(0), X(1), self.window(args[2]), _ts_cov)
        if name == "decay_linear":
            return _rolling(X(0), self.window(args[1]), _decay_linear)
        if name == "ewm_mean":
            hl = self.const(args[1])
            if not hl > 0:
                raise EvaluationError("half-life must be > 0")
            return _ewm(X(0), hl)
        if name == "rsi":
            return _rsi(P["close"], self.window(args[0]))
        if name == "atr":
            return _atr(P["high"], P["low"], P["close"], self.window(args[0]))
        # cross-sectional
        if name == "cs_rank":
            return _cs_rank(X(0))
        if name == "cs_zscore":
            return _cs_zscore(X(0))
        if name == "cs_demean":
            return _cs_demean(X(0))
        if name == "cs_scale":
            return _cs_scale(X(0))
        if name == "cs_winsorize":
            return _cs_winsorize(X(0), self.const(args[1]))
        raise EvaluationError(f"operator {name!r} has no implementation")


def evaluate(spec: dict, panel, *, ext_values: Optional[dict] = None, param_values: Optional[dict] = None):
    """Signal ndarray[dates, symbols] of a valid spec on a panel."""
    if "signal" not in spec:
        raise EvaluationError("the spec has no signal")
    check_cost(spec, panel, param_values=param_values)
    return Evaluator(spec, panel, ext_values=ext_values, param_values=param_values).eval(spec["signal"])


def evaluate_with_features(spec: dict, panel, *, ext_values=None, param_values=None):
    """(signal, {feature name: ndarray}) — every declared feature evaluated."""
    check_cost(spec, panel, param_values=param_values, all_features=True)
    ev = Evaluator(spec, panel, ext_values=ext_values, param_values=param_values)
    sig = ev.eval(spec["signal"])
    return sig, {name: ev.feature(name) for name in ev.features}


def evaluate_node(node, panel, *, features: Optional[dict] = None, parameters: Optional[dict] = None,
                  ext_values: Optional[dict] = None):
    """Evaluate one expression (tests, editor tools)."""
    spec = {"signal": node, "features": features or {}, "parameters": parameters or {}}
    return evaluate(spec, panel, ext_values=ext_values)
