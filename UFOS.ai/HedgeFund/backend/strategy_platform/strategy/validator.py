"""Spec validator (SPEC §7.5): ``validate(spec) -> ValidationResult``.

Errors ``{path, code, message}`` (JSONPath-style paths, e.g.
``$.signal.args[0].args[1]``), warnings ``{path, code, message}``, facts
``{max_lookback, required_fields, required_ext, symbols_count,
asset_classes, param_count, node_count}``. Codes are stable (tested).

Look-ahead ban: unknown operators are errors; every shift/window must be a
positive integer (0 or negative → ``look_ahead``); no operator reads bars
after t (the operator set has none); ``execution.timing`` must be
``next_open``/``next_close`` (``bad_timing``); ``{"ext": …}`` only for
providers with ``point_in_time = True`` (``not_point_in_time``).

External features come from ``strategy.features.catalogue()`` (lazy import);
tests and callers may pass ``ext_catalogue`` (a list of ``{name,
point_in_time, …}`` dicts) instead.

Part of UFOS.ai, a proof-of-concept portfolio project.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import dsl, limits, schema
from .limits import SpecParseError

SHORT_HISTORY_BARS = 756


@dataclass
class ValidationResult:
    ok: bool
    errors: List[dict] = field(default_factory=list)
    warnings: List[dict] = field(default_factory=list)
    facts: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "errors": list(self.errors), "warnings": list(self.warnings),
                "facts": dict(self.facts)}

    def codes(self) -> List[str]:
        return [e["code"] for e in self.errors]


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _key_path(path: str, key: str) -> str:
    return f"{path}.{key}" if key.replace("_", "a").isalnum() and not key[:1].isdigit() else f"{path}[{key!r}]"


def _normalise_catalogue(cat) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    if cat is None:
        return out
    items = cat.values() if isinstance(cat, dict) else cat
    for it in items:
        if isinstance(it, dict):
            name = it.get("name")
            pit = bool(it.get("point_in_time"))
        else:
            name = getattr(it, "name", None)
            pit = bool(getattr(it, "point_in_time", False))
        if isinstance(name, str):
            out[name] = {"name": name, "point_in_time": pit}
    return out


def ext_catalogue_default() -> Dict[str, dict]:
    from . import features   # lazy: B2's registry (SPEC §7.8)
    return _normalise_catalogue(features.catalogue())


class _V:
    def __init__(self, spec: dict, ext_catalogue):
        self.spec = spec
        self.errors: List[dict] = []
        self.warnings: List[dict] = []
        self.ext_cat_raw = ext_catalogue
        self._ext_cat: Optional[Dict[str, dict]] = None
        self.params: Dict[str, dict] = {}
        self.features: Dict[str, object] = {}
        self.used_params: set = set()
        self.fields: set = set()
        self.ext: set = set()
        self.feature_refs: Dict[str, set] = {}
        self.signal_refs: set = set()

    # --- helpers ---
    def err(self, path, code, message):
        self.errors.append({"path": path, "code": code, "message": message})

    def warn(self, path, code, message):
        self.warnings.append({"path": path, "code": code, "message": message})

    @property
    def ext_cat(self) -> Dict[str, dict]:
        if self._ext_cat is None:
            self._ext_cat = (_normalise_catalogue(self.ext_cat_raw) if self.ext_cat_raw is not None
                             else ext_catalogue_default())
        return self._ext_cat

    def keys(self, obj, path, allowed, required=()) -> bool:
        if not isinstance(obj, dict):
            self.err(path, "bad_type", "Must be an object.")
            return False
        for k in obj:
            if k not in allowed:
                self.err(_key_path(path, str(k)), "unknown_key", f"Unknown key {k!r}.")
        for k in required:
            if k not in obj:
                self.err(_key_path(path, k), "missing_key", f"Missing key {k!r}.")
        return True

    def text(self, v, path, max_len, *, required=False) -> None:
        if not isinstance(v, str):
            self.err(path, "bad_type", "Must be a text.")
        elif required and not v.strip():
            self.err(path, "out_of_range", "Must not be empty.")
        elif len(v) > max_len:
            self.err(path, "text_too_long", f"Text too long ({len(v)} characters; at most {max_len}).")

    def enum(self, v, path, allowed) -> bool:
        if not isinstance(v, str) or v not in allowed:
            self.err(path, "out_of_range", f"Must be one of: {', '.join(allowed)}.")
            return False
        return True

    def number(self, v, path, lo=None, hi=None, *, lo_open=False, integer=False, allow_null=False) -> bool:
        if v is None and allow_null:
            return True
        if not _is_num(v) or (integer and not _is_int(v)):
            self.err(path, "bad_type", "Must be an integer." if integer else "Must be a number.")
            return False
        if not math.isfinite(v):
            self.err(path, "bad_type", "Must be a finite number.")
            return False
        if lo is not None and (v <= lo if lo_open else v < lo):
            self.err(path, "out_of_range", f"Must be {'>' if lo_open else '≥'} {lo}.")
            return False
        if hi is not None and v > hi:
            self.err(path, "out_of_range", f"Must be ≤ {hi}.")
            return False
        return True

    # --- blocks ---
    def run(self):
        s = self.spec
        self.keys(s, "$", schema.TOP_KEYS, [k for k, req in schema.TOP_KEYS.items() if req])
        if "schema" in s and s["schema"] != schema.SCHEMA_ID:
            self.err("$.schema", "out_of_range", f"Unsupported schema; expected {schema.SCHEMA_ID!r}.")
        if "name" in s:
            self.text(s["name"], "$.name", limits.MAX_NAME_LEN, required=True)
        self.text(s.get("description", ""), "$.description", limits.MAX_DESCRIPTION_LEN)
        tags = s.get("tags", [])
        if not isinstance(tags, list):
            self.err("$.tags", "bad_type", "Must be a list of texts.")
        else:
            if len(tags) > limits.MAX_TAGS:
                self.err("$.tags", "limit_exceeded", f"Too many tags ({len(tags)}; at most {limits.MAX_TAGS}).")
            for i, t in enumerate(tags):
                self.text(t, f"$.tags[{i}]", limits.MAX_TAG_LEN, required=True)
        self.enum(s.get("category"), "$.category", schema.CATEGORIES)
        self.enum(s.get("timeframe"), "$.timeframe", schema.TIMEFRAMES)
        self.symbols = self.universe(s.get("universe"))
        self.parameters(s.get("parameters"))
        self.features_block(s.get("features"))
        if "signal" in s:
            self.signal_refs = self.expr(s["signal"], "$.signal")
        self.cycles()
        self.portfolio(s.get("portfolio"))
        rb = s.get("rebalance")
        if self.keys(rb, "$.rebalance", schema.REBALANCE_KEYS, ("frequency",)) and "frequency" in rb:
            self.enum(rb["frequency"], "$.rebalance.frequency", schema.FREQUENCIES)
        self.risk(s.get("risk"))
        ex = s.get("execution")
        if self.keys(ex, "$.execution", schema.EXECUTION_KEYS, schema.EXECUTION_KEYS):
            if "order_type" in ex:
                self.enum(ex["order_type"], "$.execution.order_type", schema.ORDER_TYPES)
            if "timing" in ex and ex["timing"] not in schema.TIMINGS:
                self.err("$.execution.timing", "bad_timing",
                         "Orders fill at the next open or next close only (no same-bar fills): "
                         "use 'next_open' or 'next_close'.")
        self.costs(s.get("costs"))
        if "benchmark" in s:
            self.benchmark(s["benchmark"])
        if "provenance" in s and not isinstance(s["provenance"], dict):
            self.err("$.provenance", "bad_type", "Must be an object.")

    def benchmark(self, b) -> None:
        """Optional backtest benchmark (SPEC §8.3): one supported symbol."""
        from ..core.instruments import instrument
        try:
            if not isinstance(b, str):
                raise ValueError
            instrument(b)
        except ValueError:
            self.err("$.benchmark", "bad_symbol", f"Not a supported symbol: {b!r}.")

    def universe(self, u) -> List[str]:
        from ..core.instruments import instrument
        from . import universe as uni
        if not self.keys(u, "$.universe", schema.UNIVERSE_KEYS):
            return []
        has_p, has_s = "preset" in u, "symbols" in u
        if has_p == has_s:
            self.err("$.universe", "missing_key" if not has_p else "bad_type",
                     "Give either 'preset' or 'symbols' (exactly one).")
            return []
        if has_p:
            if not isinstance(u["preset"], str) or uni.preset(u["preset"]) is None:
                self.err("$.universe.preset", "out_of_range",
                         f"Unknown preset; known: {', '.join(uni.PRESETS)}.")
                return []
            ac = u.get("asset_classes", list(schema.ASSET_CLASSES))
            if not isinstance(ac, list) or not ac:
                self.err("$.universe.asset_classes", "bad_type", "Must be a non-empty list.")
                return []
            ok = True
            for i, a in enumerate(ac):
                ok = self.enum(a, f"$.universe.asset_classes[{i}]", schema.ASSET_CLASSES) and ok
            if len(set(map(str, ac))) != len(ac):
                self.err("$.universe.asset_classes", "out_of_range", "Duplicate asset class.")
                ok = False
            if not ok:
                return []
            syms = uni.resolve(u)
            self.warn("$.universe.preset", "survivorship",
                      "Preset universes are today's list of liquid funds — backtests on them carry a mild "
                      "survivorship/selection bias.")
            return syms
        if "asset_classes" in u:
            self.err("$.universe.asset_classes", "bad_type", "'asset_classes' filters a preset only.")
        syms = u["symbols"]
        if not isinstance(syms, list) or not syms:
            self.err("$.universe.symbols", "bad_type", "Must be a non-empty list of symbols.")
            return []
        if len(syms) > limits.MAX_SYMBOLS:
            self.err("$.universe.symbols", "too_many_symbols",
                     f"Too many symbols ({len(syms)}; at most {limits.MAX_SYMBOLS}).")
            return []
        out, seen = [], set()
        for i, s in enumerate(syms):
            try:
                if not isinstance(s, str):
                    raise ValueError
                sym = instrument(s).symbol
            except ValueError:
                self.err(f"$.universe.symbols[{i}]", "bad_symbol", f"Not a supported symbol: {s!r}.")
                continue
            if sym in seen:
                self.err(f"$.universe.symbols[{i}]", "bad_symbol", f"Duplicate symbol {sym}.")
                continue
            seen.add(sym)
            out.append(sym)
        return out

    def parameters(self, ps):
        if not isinstance(ps, dict):
            self.err("$.parameters", "bad_type", "Must be an object.")
            return
        if len(ps) > limits.MAX_PARAMS:
            self.err("$.parameters", "limit_exceeded", f"Too many parameters ({len(ps)}; at most {limits.MAX_PARAMS}).")
            return
        for name, p in ps.items():
            path = _key_path("$.parameters", name)
            if not limits.PARAM_NAME_RE.match(name):
                self.err(path, "bad_type", "Parameter names: lower-case letters, digits, '_' (start with a letter, "
                                           "at most 40 characters).")
                continue
            if not self.keys(p, path, schema.PARAM_KEYS, ("value", "min", "max")):
                continue
            ok = all(self.number(p[k], f"{path}.{k}") for k in ("value", "min", "max") if k in p)
            integer = p.get("integer", False)
            if not isinstance(integer, bool):
                self.err(f"{path}.integer", "bad_type", "Must be true or false.")
                integer = False
            if "step" in p:
                self.number(p["step"], f"{path}.step", 0, lo_open=True)
            if "description" in p:
                self.text(p["description"], f"{path}.description", limits.MAX_PARAM_DESCRIPTION_LEN)
            if not ok or not all(k in p for k in ("value", "min", "max")):
                continue
            if integer:
                for k in ("value", "min", "max"):
                    if not _is_int(p[k]):
                        self.err(f"{path}.{k}", "bad_type", "An integer parameter needs integer value/min/max.")
                        ok = False
            if ok and p["min"] > p["max"]:
                self.err(path, "out_of_range", "min must be ≤ max.")
            elif ok and not (p["min"] <= p["value"] <= p["max"]):
                self.err(f"{path}.value", "out_of_range", f"Must lie in [{p['min']}, {p['max']}].")
            self.params[name] = {"value": p["value"], "min": p["min"], "max": p["max"], "integer": integer and ok}

    def features_block(self, fs):
        if not isinstance(fs, dict):
            self.err("$.features", "bad_type", "Must be an object.")
            return
        if len(fs) > limits.MAX_FEATURES:
            self.err("$.features", "limit_exceeded", f"Too many features ({len(fs)}; at most {limits.MAX_FEATURES}).")
            return
        for name in fs:
            if not limits.FEATURE_NAME_RE.match(name):
                code = "text_too_long" if len(name) > limits.MAX_FEATURE_NAME_LEN else "bad_type"
                self.err(_key_path("$.features", name), code,
                         "Feature names: lower-case letters, digits, '_' (start with a letter, at most 40 characters).")
        self.features = {n: e for n, e in fs.items() if limits.FEATURE_NAME_RE.match(n)}
        for name, expr in self.features.items():
            self.feature_refs[name] = self.expr(expr, _key_path("$.features", name))

    # --- expressions ---
    def expr(self, node, path) -> set:
        """Validate an expression; returns the feature names it references directly."""
        refs: set = set()
        stack = [(node, path)]
        while stack:
            n, p = stack.pop()
            kind = dsl.node_kind(n)
            if kind == "number":
                if not math.isfinite(n):
                    self.err(p, "bad_type", "Numbers must be finite.")
                continue
            if kind == "invalid":
                self.err(p, "bad_type", "Expected a number or an object with one of op/param/ref/field/ext.")
                continue
            if kind != "op":
                extra = [k for k in n if k != kind]
                for k in extra:
                    self.err(_key_path(p, str(k)), "unknown_key", f"Unknown key {k!r} in a {kind} node.")
                v = n[kind]
                if kind == "param":
                    if not isinstance(v, str) or v not in self.params_declared():
                        self.err(p, "unknown_param", f"Unknown parameter {v!r}.")
                    else:
                        self.used_params.add(v)
                elif kind == "ref":
                    fs = self.spec.get("features")
                    if not isinstance(v, str) or not isinstance(fs, dict) or v not in fs:
                        self.err(p, "unknown_ref", f"Unknown feature {v!r}.")
                    else:
                        refs.add(v)
                elif kind == "field":
                    if not isinstance(v, str) or v not in dsl.FIELDS:
                        self.err(p, "unknown_field", f"Unknown field {v!r}; fields: {', '.join(dsl.FIELDS)}.")
                    else:
                        self.fields.add(v)
                elif kind == "ext":
                    info = self.ext_cat.get(v) if isinstance(v, str) else None
                    if info is None:
                        self.err(p, "unknown_ext", f"Unknown external feature {v!r}.")
                    elif not info["point_in_time"]:
                        self.err(p, "not_point_in_time",
                                 f"External feature {v!r} is not point-in-time — it could leak future data.")
                    else:
                        self.ext.add(v)
                continue
            for k in n:
                if k not in ("op", "args"):
                    self.err(_key_path(p, str(k)), "unknown_key", f"Unknown key {k!r} in an op node.")
            name = n["op"]
            op = dsl.OPS.get(name) if isinstance(name, str) else None
            if op is None:
                self.err(f"{p}.op", "unknown_op", f"Unknown operator {name!r}.")
                continue
            args = n.get("args", [] if op.min_args == 0 else None)
            if not isinstance(args, list):
                self.err(f"{p}.args", "missing_key" if "args" not in n else "bad_type", "'args' must be a list.")
                continue
            if not (op.min_args <= len(args) <= op.max_args):
                want = (f"{op.min_args}" if op.min_args == op.max_args else f"{op.min_args}–{op.max_args}")
                self.err(f"{p}.args", "bad_arity", f"{name} takes {want} argument(s), got {len(args)}.")
                continue
            self.fields.update(op.fields)
            for i, a in enumerate(args):
                ap = f"{p}.args[{i}]"
                k = op.kind_at(i)
                if k == "x":
                    stack.append((a, ap))
                elif k == "window":
                    self.window_arg(a, ap, op)
                elif k == "halflife":
                    self.halflife_arg(a, ap)
                else:
                    self.const_arg(a, ap)
        return refs

    def params_declared(self) -> dict:
        ps = self.spec.get("parameters")
        return ps if isinstance(ps, dict) else {}

    def _param_for(self, a, ap):
        v = a.get("param")
        for k in a:
            if k != "param":
                self.err(_key_path(ap, str(k)), "unknown_key", f"Unknown key {k!r} in a param node.")
        if not isinstance(v, str) or v not in self.params_declared():
            self.err(ap, "unknown_param", f"Unknown parameter {v!r}.")
            return None
        self.used_params.add(v)
        return self.params.get(v)   # None when the declaration itself was invalid (error already given)

    def window_arg(self, a, ap, op):
        kind = dsl.node_kind(a)
        if kind == "number":
            if not _is_int(a):
                self.err(ap, "bad_window", "A window must be a whole number of bars.")
            elif a <= 0:
                self.err(ap, "look_ahead", f"{op.name}: a window/shift of {a} would read the current or future "
                                           "bars — it must be ≥ 1.")
            elif a < op.min_window:
                self.err(ap, "bad_window", f"{op.name} needs a window of at least {op.min_window}.")
            elif a > limits.MAX_WINDOW:
                self.err(ap, "bad_window", f"Window too long (at most {limits.MAX_WINDOW} bars).")
        elif kind == "param":
            p = self._param_for(a, ap)
            if p is None:
                return
            if not p["integer"]:
                self.err(ap, "bad_window", "A window parameter needs \"integer\": true with integer value/min/max.")
            elif p["min"] <= 0:
                self.err(ap, "look_ahead", f"{op.name}: the parameter allows a window ≤ 0 (min {p['min']}) — "
                                           "windows must be ≥ 1.")
            elif p["min"] < op.min_window or p["max"] > limits.MAX_WINDOW:
                self.err(ap, "bad_window", f"{op.name} needs a window in [{op.min_window}, {limits.MAX_WINDOW}] "
                                           f"for every parameter value.")
        else:
            self.err(ap, "bad_window", "A window must be an integer constant or an integer parameter.")

    def halflife_arg(self, a, ap):
        kind = dsl.node_kind(a)
        if kind == "number":
            lo, hi = a, a
        elif kind == "param":
            p = self._param_for(a, ap)
            if p is None:
                return
            lo, hi = p["min"], p["max"]
        else:
            self.err(ap, "bad_window", "A half-life must be a constant or a parameter.")
            return
        if not math.isfinite(lo) or not math.isfinite(hi):
            self.err(ap, "bad_type", "Numbers must be finite.")
        elif lo <= 0:
            self.err(ap, "look_ahead", "A half-life must be > 0.")
        elif hi > limits.MAX_WINDOW:
            self.err(ap, "bad_window", f"Half-life too long (at most {limits.MAX_WINDOW} bars).")

    def const_arg(self, a, ap):
        kind = dsl.node_kind(a)
        if kind == "number":
            if not math.isfinite(a):
                self.err(ap, "bad_type", "Numbers must be finite.")
        elif kind == "param":
            self._param_for(a, ap)
        else:
            self.err(ap, "bad_type", "Must be a constant number or a parameter.")

    def cycles(self):
        graph = {n: set(r) for n, r in self.feature_refs.items()}
        state: Dict[str, int] = {}
        reported = set()
        for start in sorted(graph):
            if state.get(start):
                continue
            stack = [(start, iter(sorted(graph.get(start, ()))))]
            state[start] = 1
            path = [start]
            while stack:
                node, it = stack[-1]
                nxt = next(it, None)
                if nxt is None:
                    state[node] = 2
                    stack.pop()
                    path.pop()
                    continue
                if state.get(nxt) == 1:
                    cyc = path[path.index(nxt):] + [nxt]
                    key = frozenset(cyc)
                    if key not in reported:
                        reported.add(key)
                        self.err(_key_path("$.features", nxt), "ref_cycle",
                                 "Feature references form a cycle: " + " → ".join(cyc) + ".")
                elif not state.get(nxt) and nxt in graph:
                    state[nxt] = 1
                    path.append(nxt)
                    stack.append((nxt, iter(sorted(graph.get(nxt, ())))))

    def portfolio(self, pf):
        if not self.keys(pf, "$.portfolio", schema.PORTFOLIO_KEYS):
            return
        mode = pf.get("mode")
        self.enum(mode, "$.portfolio.mode", schema.MODES)
        sel = pf.get("selection")
        if self.keys(sel, "$.portfolio.selection", schema.SELECTION_KEYS, ("type",)) and "type" in sel:
            t = sel["type"]
            if self.enum(t, "$.portfolio.selection.type", schema.SELECTION_TYPES):
                if t in ("top_n", "bottom_n", "top_bottom_n"):
                    if "n" not in sel:
                        self.err("$.portfolio.selection.n", "missing_key", f"{t} needs 'n'.")
                    else:
                        self.number(sel["n"], "$.portfolio.selection.n", 1, limits.MAX_SYMBOLS, integer=True)
                elif "n" in sel:
                    self.err("$.portfolio.selection.n", "unknown_key", f"'n' is not used by selection {t!r}.")
                if t == "threshold":
                    for k in ("long_above", "short_below"):
                        if k in sel:
                            self.number(sel[k], f"$.portfolio.selection.{k}")
                    need = {"long_only": ("long_above",), "short_only": ("short_below",)}.get(mode)
                    if need and need[0] not in sel:
                        self.err(f"$.portfolio.selection.{need[0]}", "missing_key", f"Mode {mode} needs {need[0]!r}.")
                    if mode == "long_short" and "long_above" not in sel and "short_below" not in sel:
                        self.err("$.portfolio.selection", "missing_key",
                                 "threshold needs 'long_above' and/or 'short_below'.")
                    if mode == "long_only" and "short_below" in sel:
                        self.err("$.portfolio.selection.short_below", "unknown_key", "Not used in mode long_only.")
                    if mode == "short_only" and "long_above" in sel:
                        self.err("$.portfolio.selection.long_above", "unknown_key", "Not used in mode short_only.")
                    if (mode == "long_short" and _is_num(sel.get("long_above")) and _is_num(sel.get("short_below"))
                            and sel["short_below"] > sel["long_above"]):
                        self.err("$.portfolio.selection", "out_of_range", "short_below must be ≤ long_above.")
                else:
                    for k in ("long_above", "short_below"):
                        if k in sel:
                            self.err(f"$.portfolio.selection.{k}", "unknown_key", f"{k!r} is only used by threshold.")
                if t == "top_bottom_n" and mode != "long_short":
                    self.err("$.portfolio.selection.type", "out_of_range", "top_bottom_n needs mode long_short.")
        self.enum(pf.get("weighting"), "$.portfolio.weighting", schema.WEIGHTINGS)
        if pf.get("weighting") == "inverse_vol" and pf.get("vol_lookback") is None:
            self.err("$.portfolio.vol_lookback", "missing_key", "inverse_vol weighting needs 'vol_lookback'.")
        if pf.get("vol_lookback") is not None:
            self.number(pf["vol_lookback"], "$.portfolio.vol_lookback", 2, limits.MAX_WINDOW, integer=True)
        self.number(pf.get("max_weight"), "$.portfolio.max_weight", 0, 1, lo_open=True)
        self.number(pf.get("gross_exposure"), "$.portfolio.gross_exposure", 0, 3, lo_open=True)

    def risk(self, r):
        if not self.keys(r, "$.risk", schema.RISK_KEYS):
            return
        for k in ("stop_loss_pct", "take_profit_pct", "max_drawdown_stop_pct"):
            self.number(r.get(k), f"$.risk.{k}", 0, 100, lo_open=True, allow_null=True)
        self.number(r.get("vol_target_annual"), "$.risk.vol_target_annual", 0, 5, lo_open=True, allow_null=True)

    def costs(self, c):
        if not self.keys(c, "$.costs", schema.COSTS_KEYS, ("model",)) or "model" not in c:
            return
        if not self.enum(c["model"], "$.costs.model", schema.COST_MODELS):
            return
        if c["model"] == "custom":
            for k in ("fee_bps", "slippage_bps"):
                if k not in c:
                    self.err(f"$.costs.{k}", "missing_key", f"The custom cost model needs {k!r}.")
                else:
                    self.number(c[k], f"$.costs.{k}", 0, 1000)
        else:
            for k in ("fee_bps", "slippage_bps"):
                if k in c:
                    self.err(f"$.costs.{k}", "unknown_key", f"{k!r} is only used by the custom cost model.")


# --- lookback ------------------------------------------------------------------------

def _window_value(a, params: dict, use_max=True) -> float:
    if dsl.node_kind(a) == "param":
        p = params.get(a["param"]) or {}
        return float(p.get("max" if use_max else "value", 0))
    return float(a)


def node_lookback(node, features: dict, params: dict, memo: Optional[dict] = None) -> int:
    """Bars of history before t a (valid) node reads — window parameters at
    their max. Iterative over refs via memo (call only on a cycle-free spec)."""
    memo = {} if memo is None else memo

    def lb(n, depth=0) -> int:
        kind = dsl.node_kind(n)
        if kind == "ref":
            name = n["ref"]
            if name not in memo:
                memo[name] = 0          # guards against cycles if called on an invalid spec
                memo[name] = lb(features[name], depth + 1)
            return memo[name]
        if kind != "op":
            return 0
        op = dsl.OPS[n["op"]]
        args = n.get("args") or []
        inner = 0
        for i, a in enumerate(args):
            if op.kind_at(i) == "x":
                inner = max(inner, lb(a, depth + 1))
        w = next((a for i, a in enumerate(args) if op.kind_at(i) in ("window", "halflife")), None)
        if w is None or not op.lookback:
            return inner
        v = _window_value(w, params)
        extra = {"lag": v, "window": v - 1, "wilder": 4 * v, "ewm": math.ceil(4 * v)}[op.lookback]
        return int(inner + extra)

    return lb(node)


# --- absolute price levels (review 2026-10-04, SPEC §8.2 / §8.10) -------------------------------------
PRICE_FIELDS = ("open", "high", "low", "close", "vwap")
# ops whose result carries the price scale when (any of) their x arguments do
LEVEL_ANY_ARG = ("add", "sub", "min", "max", "mul", "neg", "abs", "lag", "delta", "ts_mean", "sma", "ts_std",
                 "ts_sum", "ts_min", "ts_max", "decay_linear", "ewm_mean", "ts_cov")
LEVEL_FIRST_ARG = ("clip", "fill_nan")
COMPARE_OPS = ("gt", "ge", "lt", "le", "eq", "ne")
ABSOLUTE_LEVEL_MESSAGE = (
    "This compares a price level ({field}) with a fixed number or parameter. The backtest's adjusted "
    "('all') prices are back-adjusted: their LEVEL depends on dividends and splits that happen later and is "
    "rebased at every new dividend (look-ahead), and levels before 2016-01-04 are Tiingo prices scaled by "
    "the seam factor — compare ratios or changes (e.g. close / sma(close, n)) instead.")


class _Levels:
    """Which expressions carry the price scale (a multiplicative rescaling of prices rescales them)."""

    def __init__(self, features: dict):
        self.features = features if isinstance(features, dict) else {}
        self.memo: Dict[str, Optional[str]] = {}

    def level(self, n, depth: int = 0) -> Optional[str]:
        """The price field behind a level-carrying node, else None."""
        if depth > 200 or not isinstance(n, dict):
            return None
        if "field" in n:
            return n["field"] if n["field"] in PRICE_FIELDS else None
        if "ref" in n:
            name = n["ref"]
            if name not in self.memo:
                self.memo[name] = None                       # cycle guard (validated cycle-free anyway)
                self.memo[name] = self.level(self.features.get(name), depth + 1)
            return self.memo[name]
        name, args = n.get("op"), n.get("args") or []
        if name == "atr":
            return "high/low/close"
        if name == "div":
            a = self.level(args[0], depth + 1) if args else None
            b = self.level(args[1], depth + 1) if len(args) > 1 else None
            return a if a and not b else None              # a ratio of two levels is scale-free
        if name == "where":
            for a in args[1:3]:
                f = self.level(a, depth + 1)
                if f:
                    return f
            return None
        if name in LEVEL_FIRST_ARG:
            return self.level(args[0], depth + 1) if args else None
        if name in LEVEL_ANY_ARG:
            for a in args:
                f = self.level(a, depth + 1)
                if f:
                    return f
        return None

    @staticmethod
    def constant(n) -> bool:
        """A non-zero number or a parameter (comparing with 0 is scale-invariant)."""
        if isinstance(n, (int, float)) and not isinstance(n, bool):
            return n != 0
        return isinstance(n, dict) and "param" in n

    def scan(self, node, path: str, out: List[Tuple[str, str]]) -> None:
        stack = [(node, path)]
        while stack:
            n, p = stack.pop()
            if not isinstance(n, dict) or "op" not in n:
                continue
            name, args = n.get("op"), n.get("args") or []
            hit = None
            if name in COMPARE_OPS or name in ("min", "max"):
                if any(self.constant(a) for a in args):
                    hit = next((f for f in (self.level(a) for a in args) if f), None)
            elif name == "clip" and args and any(self.constant(a) for a in args[1:]):
                hit = self.level(args[0])
            if hit:
                out.append((p, hit))
            for i, a in enumerate(args):
                stack.append((a, f"{p}.args[{i}]"))


def absolute_level_uses(spec: dict) -> List[Tuple[str, str]]:
    """[(path, price field)] of comparisons between a price level and a non-zero constant/parameter."""
    lv = _Levels(spec.get("features"))
    out: List[Tuple[str, str]] = []
    if "signal" in spec:
        lv.scan(spec["signal"], "$.signal", out)
    for name, expr in sorted((spec.get("features") or {}).items()):
        lv.scan(expr, _key_path("$.features", name), out)
    sel = (spec.get("portfolio") or {}).get("selection") or {}
    if isinstance(sel, dict) and sel.get("type") == "threshold" and "signal" in spec:
        # the threshold selection compares the signal itself with constants
        if any(_Levels.constant(sel.get(k)) for k in ("long_above", "short_below")):
            f = lv.level(spec["signal"])
            if f:
                out.append(("$.portfolio.selection", f))
    return sorted(set(out))


def validate(spec, *, ext_catalogue=None) -> ValidationResult:
    """Validate a spec dict (missing optional blocks take their documented
    defaults, schema.with_defaults). ``ext_catalogue``: optional list of
    external feature descriptions; default = strategy.features.catalogue()."""
    if not isinstance(spec, dict):
        return ValidationResult(False, [{"path": "$", "code": "bad_type", "message": "The spec must be an object."}])
    msg = limits.check_value_limits(spec)
    if msg:
        return ValidationResult(False, [{"path": "$", "code": "limit_exceeded", "message": msg}])
    bad = limits.find_invalid_text(spec)
    if bad:
        return ValidationResult(False, [{"path": bad.split(" ")[0], "code": "bad_type",
                                         "message": "Invalid Unicode text (a lone surrogate is not a character)."}])
    full = schema.with_defaults(spec)
    v = _V(full, ext_catalogue)
    v.run()
    facts = {"max_lookback": 0, "required_fields": sorted(v.fields), "required_ext": sorted(v.ext),
             "symbols_count": len(v.symbols), "asset_classes": [],
             "param_count": len(full.get("parameters") or {}) if isinstance(full.get("parameters"), dict) else 0,
             "node_count": limits.value_stats(spec)[1]}
    try:
        from ..core.instruments import instrument
        facts["asset_classes"] = sorted({instrument(s).asset_class for s in v.symbols})
    except ValueError:
        pass
    if not v.errors and "signal" in full:
        memo: dict = {}
        look = node_lookback(full["signal"], v.features, v.params, memo)
        pf = full.get("portfolio") or {}
        if pf.get("weighting") == "inverse_vol" and _is_int(pf.get("vol_lookback")):
            look = max(look, pf["vol_lookback"])
            v.fields.add("close")
            facts["required_fields"] = sorted(v.fields)
        facts["max_lookback"] = int(look)
        if look > limits.MAX_LOOKBACK:
            v.err("$.signal", "limit_exceeded",
                  f"The signal needs {look} bars of history — at most {limits.MAX_LOOKBACK} are allowed "
                  "(nested lags/windows add up).")
        # warnings
        reachable, todo = set(), list(v.signal_refs)
        while todo:
            r = todo.pop()
            if r not in reachable:
                reachable.add(r)
                todo.extend(v.feature_refs.get(r, ()))
        for name in v.features:
            if name not in reachable:
                v.warn(_key_path("$.features", name), "unused_feature", f"Feature {name!r} is not used by the signal.")
        for name in v.params:
            if name not in v.used_params:
                v.warn(_key_path("$.parameters", name), "unused_param", f"Parameter {name!r} is not used.")
        if look > SHORT_HISTORY_BARS:
            v.warn("$.signal", "short_history",
                   f"The signal needs {look} bars of history (> {SHORT_HISTORY_BARS}): backtests start late and "
                   "the IEX history (since 2016) is short for it.")
        for path, fld in absolute_level_uses(full):
            v.warn(path, "absolute_price_level", ABSOLUTE_LEVEL_MESSAGE.format(field=fld))
        classes = facts["asset_classes"]
        if len(classes) > 1:
            v.warn("$.universe", "mixed_calendars",
                   "Equities and crypto trade on different calendars: the panel uses NYSE sessions and each "
                   "crypto value is the last daily bar known at that session's close.")
        if "crypto" in classes and pf.get("mode") in ("short_only", "long_short"):
            v.warn("$.portfolio.mode", "crypto_short_selling",
                   "Crypto shorts are only possible with perpetual futures (funding costs, different "
                   "instrument) — spot crypto cannot be sold short.")
    return ValidationResult(not v.errors, v.errors, v.warnings, facts)


def parse_and_validate(text, *, ext_catalogue=None) -> Tuple[Optional[dict], ValidationResult]:
    """(parsed spec or None, result). Parse errors (§7.2) become one error
    with code ``parse_error`` / ``limit_exceeded`` (+ line/column for JSON
    syntax errors)."""
    try:
        spec = limits.parse_spec_text(text)
    except SpecParseError as ex:
        return None, ValidationResult(False, [ex.as_error()])
    return spec, validate(spec, ext_catalogue=ext_catalogue)


def validate_text(text, *, ext_catalogue=None) -> ValidationResult:
    return parse_and_validate(text, ext_catalogue=ext_catalogue)[1]
