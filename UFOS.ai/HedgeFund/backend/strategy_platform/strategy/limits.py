"""All size limits of strategy specs (one place) + the safe JSON parser
(SPEC §7.2).

``parse_spec_text(text) -> dict`` accepts a spec only as JSON DATA: no eval,
no pickle, nothing executed. Rejected (``SpecParseError``, a ValueError with
a stable ``code`` and, for JSON syntax errors, ``line``/``column``):

- ``limit_exceeded``: not a str is ``parse_error``; > MAX_TEXT_BYTES UTF-8
  bytes, nesting deeper than MAX_DEPTH, more than MAX_NODES JSON values;
- ``parse_error``: invalid JSON, duplicate keys, NaN/Infinity literals, a
  root that is not an object.

Depth is measured on the TEXT before ``json.loads`` runs (iterative scan of
brackets outside strings), so a hostile nesting never reaches the recursive
JSON decoder. Root object = depth 1; a value inside it = depth 2.

Part of UFOS.ai, a proof-of-concept portfolio project.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

MAX_TEXT_BYTES = 262144
MAX_DEPTH = 32
MAX_NODES = 2000
MAX_WINDOW = 2520
MAX_LOOKBACK = 2 * MAX_WINDOW          # history a whole spec may need (validator facts.max_lookback)
# Evaluation work budget (review fix 2026-10-02): estimated cell-operations of one
# evaluation = Σ over evaluated nodes of (per-cell cost × dates × symbols), see
# evaluate.estimate_cost. 2e10 lets every library alpha run on 500 symbols × 2600
# days (worst library entry, the committee, ≈ 0.9e3 per cell → 1.2e9) with headroom.
MAX_EVAL_CELL_OPS = 20_000_000_000
# Rolling windows are processed in chunks of at most this many window elements
# (8 bytes each → 16 MB per window view; a few temporaries of that size per op).
ROLLING_CHUNK_ELEMENTS = 2_000_000
MAX_SYMBOLS = 500
MAX_PARAMS = 32
MAX_FEATURES = 64
MAX_TAGS = 16
MAX_NAME_LEN = 80
MAX_TAG_LEN = 32
MAX_DESCRIPTION_LEN = 4000
MAX_PARAM_DESCRIPTION_LEN = 300
MAX_FEATURE_NAME_LEN = 40
FEATURE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
PARAM_NAME_RE = FEATURE_NAME_RE          # same rule for parameter names (B1 note, SPEC §7)

_TOKEN_RE = re.compile(r'"(?:[^"\\]|\\.)*"|[\[\]{}]', re.S)


class SpecParseError(ValueError):
    """The submitted text is not an acceptable spec document."""

    def __init__(self, code: str, message: str, line: Optional[int] = None, column: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.line = line
        self.column = column

    def as_error(self) -> dict:
        err = {"path": "$", "code": self.code, "message": self.message}
        if self.line is not None:
            err["line"] = self.line
            err["column"] = self.column
        return err


def _line_col(text: str, pos: int):
    line = text.count("\n", 0, pos) + 1
    col = pos - (text.rfind("\n", 0, pos) + 1) + 1
    return line, col


def text_depth(text: str):
    """(max bracket depth, position where MAX_DEPTH was first exceeded or None)
    outside JSON strings. Iterative; unbalanced brackets are left to json."""
    depth = best = 0
    for m in _TOKEN_RE.finditer(text):
        tok = m.group(0)
        if tok in "[{":
            depth += 1
            if depth > best:
                best = depth
                if best > MAX_DEPTH:
                    return best, m.start()
        elif tok in "]}":
            depth -= 1
    return best, None


def value_stats(value: Any):
    """(max depth, node count) of a parsed JSON value — iterative walk.
    Scalars count as nodes at their container's depth + 1 (root = 1)."""
    nodes = 0
    best = 0
    stack = [(value, 1)]
    while stack:
        v, d = stack.pop()
        nodes += 1
        if d > best:
            best = d
        if isinstance(v, dict):
            stack.extend((x, d + 1) for x in v.values())
        elif isinstance(v, (list, tuple)):
            stack.extend((x, d + 1) for x in v)
    return best, nodes


def check_value_limits(value: Any) -> Optional[str]:
    """Message when a parsed value exceeds MAX_DEPTH / MAX_NODES, else None.
    Containers count for depth: {"a": 1} has depth 2 (object + scalar) but
    only container nesting is limited, as in the text scan."""
    depth, nodes = _container_depth(value), value_stats(value)[1]
    if depth > MAX_DEPTH:
        return f"The spec is nested too deeply ({depth} levels; at most {MAX_DEPTH})."
    if nodes > MAX_NODES:
        return f"The spec has too many values ({nodes}; at most {MAX_NODES})."
    return None


def _container_depth(value: Any) -> int:
    best = 0
    stack = [(value, 1)]
    while stack:
        v, d = stack.pop()
        if isinstance(v, dict):
            best = max(best, d)
            stack.extend((x, d + 1) for x in v.values())
        elif isinstance(v, (list, tuple)):
            best = max(best, d)
            stack.extend((x, d + 1) for x in v)
    return best


def find_invalid_text(value: Any) -> Optional[str]:
    """JSONPath of the first key or string that is not valid Unicode text
    (a lone UTF-16 surrogate such as "\\ud800" cannot be encoded as UTF-8 and
    would break hashing/storage), else None. Iterative."""
    stack = [(value, "$")]
    while stack:
        v, path = stack.pop()
        if isinstance(v, str):
            if not _encodable(v):
                return path
        elif isinstance(v, dict):
            for k, x in v.items():
                kp = f"{path}[{k!r}]" if isinstance(k, str) and _encodable(k) else path
                if isinstance(k, str) and not _encodable(k):
                    return f"{path} (a key)"
                stack.append((x, kp))
        elif isinstance(v, (list, tuple)):
            stack.extend((x, f"{path}[{i}]") for i, x in enumerate(v))
    return None


def _encodable(s: str) -> bool:
    try:
        s.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


def _no_duplicates(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise SpecParseError("parse_error", f"Duplicate key {k!r} in a JSON object.")
        out[k] = v
    return out


def _no_constant(name):
    raise SpecParseError("parse_error", f"{name} is not allowed — all numbers must be finite.")


def parse_spec_text(text) -> dict:
    """The spec dict from a submitted JSON text, or SpecParseError."""
    if not isinstance(text, str):
        raise SpecParseError("parse_error", "The spec must be a JSON text.")
    size = len(text.encode("utf-8", "surrogatepass"))
    if size > MAX_TEXT_BYTES:
        raise SpecParseError("limit_exceeded",
                             f"The spec text is too large ({size} bytes; at most {MAX_TEXT_BYTES}).")
    depth, pos = text_depth(text)
    if pos is not None:
        line, col = _line_col(text, pos)
        raise SpecParseError("limit_exceeded",
                             f"The spec is nested too deeply (more than {MAX_DEPTH} levels).", line, col)
    try:
        value = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
    except SpecParseError:
        raise
    except json.JSONDecodeError as ex:
        raise SpecParseError("parse_error", f"Invalid JSON: {ex.msg} (line {ex.lineno}, column {ex.colno}).",
                             ex.lineno, ex.colno) from None
    except RecursionError:  # pragma: no cover - prevented by the depth scan
        raise SpecParseError("limit_exceeded", f"The spec is nested too deeply (more than {MAX_DEPTH} levels).")
    except ValueError as ex:   # e.g. invalid surrogates
        raise SpecParseError("parse_error", f"Invalid JSON: {ex}.") from None
    if not isinstance(value, dict):
        raise SpecParseError("parse_error", "The spec must be a JSON object ({ … }).")
    msg = check_value_limits(value)
    if msg:
        raise SpecParseError("limit_exceeded", msg)
    bad = find_invalid_text(value)
    if bad:
        raise SpecParseError("parse_error", f"Invalid Unicode text at {bad}: a lone surrogate escape "
                                            "(e.g. \\ud800) is not a character.")
    return value
