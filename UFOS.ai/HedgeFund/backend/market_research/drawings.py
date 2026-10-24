"""Chart drawings (SPEC §10 MR5, §13 "MR5 backend notes"): validation, CRUD, export/import, split rule.

Drawings are anchored to time + price (``points: [{t, p}]``) so a line drawn on 1H appears on 15m and 1D
as well. Validation is strict (unknown fields, wrong point counts, sizes → ``DrawingInvalid`` = 400 with
``errors = {field: message}``); text is the user's own plain text (control characters refused, rendered
as plain text by the page/WPF).

Split rule (plan §4.2): the chart's equity bars are split-adjusted, so after a split every stored price
is divided by the split factor (``new_rate / old_rate``). ``split_check`` (supervised task
``drawing_split_check``: at start, daily, and after an import) reads Alpaca's split corporate actions via
``sources.AlpacaSources.splits`` (the existing provider, Research priority) and applies every split with
``ex_date > price_basis.as_of`` once per drawing — idempotent through the UNIQUE ``(drawing_id,
action_key)`` of ``drawing_rescales`` and the moved ``as_of``. Every rescale is logged (table + audit).
A failed check changes nothing and is reported in ``split_check`` (never a Research crash).

All functions are BLOCKING (SQLite, Alpaca REST) — routes/tasks call them via ``executor.run_blocking``.

Part of UFOS.ai, a proof-of-concept portfolio project; research only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import unicodedata
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from strategy_platform.core.clock import iso, to_utc
from strategy_platform.core.errors import InvalidRequest, UserFacingError
from strategy_platform.core.instruments import instrument

from . import limits
from . import store as default_store
from .bars import aggregate as agg

logger = logging.getLogger(__name__)

# tool -> (min points, max points); KLineChart overlay names where they exist, custom: rect, measure,
# arrowUp, arrowDown, alertLine (SPEC MR5 notes)
TOOLS: Dict[str, tuple] = {
    "segment": (2, 2), "rayLine": (2, 2), "straightLine": (2, 2),
    "horizontalStraightLine": (1, 1), "horizontalRayLine": (1, 1), "verticalStraightLine": (1, 1),
    "priceLine": (1, 1), "parallelStraightLine": (3, 3), "priceChannelLine": (3, 3),
    "fibonacciLine": (2, 2), "rect": (2, 2), "measure": (2, 2), "simpleAnnotation": (1, 1),
    "simpleTag": (1, 1), "arrowUp": (1, 1), "arrowDown": (1, 1),
    "brush": (2, limits.MAX_BRUSH_POINTS), "alertLine": (1, 1),
}
COLORS = ("accent", "up", "down", "caution", "text", "muted", "blue", "violet")
DASHES = ("solid", "dashed")
DEFAULT_STYLE = {"color": "accent", "width": 1, "dash": "solid", "levels": None}
STYLE_KEYS = tuple(DEFAULT_STYLE)
MAX_FIB_LEVELS = 12
FIB_RANGE = (-100.0, 400.0)
PRICE_MAX = 1e9
T_MIN = datetime(1990, 1, 1, tzinfo=timezone.utc)
T_MAX = datetime(2100, 1, 1, tzinfo=timezone.utc)
ID_RE = re.compile(r"^[0-9a-f]{32}$")
CREATE_FIELDS = ("symbol", "tool", "points", "style", "name", "text", "locked", "visible", "timeframes",
                 "alert_id", "client_id")
UUID_RE = re.compile(r"^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$")
CLIENT_ID_USED = "This client_id already belongs to a drawing of another symbol."
PATCH_FIELDS = ("points", "style", "name", "text", "locked", "visible", "timeframes")
ALERT_ID_MANAGED = "alert_id is managed by alerts"     # since MR7 only the alert transaction writes drawings.alert_id
EXPORT_FIELDS = ("tool", "points", "style", "name", "text", "locked", "visible", "timeframes", "price_basis")
FILE_FIELDS = ("version", "symbol", "exported_at", "drawings")
EXPORT_VERSION = 1
SPLIT_KINDS = ("forward_split", "reverse_split")
META_SPLIT_CHECK = "drawings.split_check"

NOT_FOUND = "Drawing not found."
LOCKED = "The drawing is locked — unlock it first."


def too_many() -> str:
    return f"At most {limits.MAX_DRAWINGS_PER_SYMBOL} drawings per symbol — delete some first."


class DrawingInvalid(InvalidRequest):
    """400 with ``errors = {field: message}``."""

    def __init__(self, errors: Dict[str, str], message: str = "invalid input"):
        super().__init__(message)
        self.errors = errors


class DrawingNotFound(Exception):
    pass


class DrawingConflict(Exception):
    """409 with ``message``."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# --- helpers -------------------------------------------------------------------------------------

def compact(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def compact_size(obj) -> int:
    """UTF-8 size of the compact JSON (NaN/Infinity, which json.loads accepts, count as their literals)."""
    return len(json.dumps(obj, ensure_ascii=False, allow_nan=True, separators=(",", ":")).encode("utf-8"))


def new_id() -> str:
    return uuid.uuid4().hex


def ny_today(now) -> str:
    return to_utc(now).astimezone(agg.NY).date().isoformat()


def symbol_of(raw) -> "instrument":
    if not isinstance(raw, str) or not raw or len(raw) > 20:
        raise ValueError("not a supported symbol")
    return instrument(raw)


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


def _plain(v, key: str, max_chars: int, errors: dict, *, multiline: bool) -> Optional[str]:
    """User plain text (review L7, aligned with ``watchlists.check_name``): Unicode letters, marks, numbers,
    punctuation, symbols and space separators (Zs, e.g. U+00A0 / U+202F) are allowed; control, format,
    private-use and unassigned characters (category C*) and line/paragraph separators (Zl, Zp) are refused —
    except newline and tab in multi-line text. Names are stripped of leading/trailing spaces."""
    if v is None:
        return None
    if not isinstance(v, str):
        errors[key] = "must be a string or null"
        return None
    s = v.replace("\r\n", "\n").replace("\r", "\n")
    if not multiline:
        s = s.strip()
    allowed = "\n\t" if multiline else ""
    for ch in s:
        cat = unicodedata.category(ch)
        if (cat[0] == "C" or cat in ("Zl", "Zp")) and ch not in allowed:
            errors[key] = (f"must not contain control or formatting characters (found U+{ord(ch):04X})"
                           + ("; newline and tab are allowed" if multiline else ""))
            return None
    if not s.strip():
        return None
    if len(s) > max_chars:
        errors[key] = f"at most {max_chars} characters"
        return None
    return s


def _points(tool: str, v, errors: dict, key: str = "points") -> Optional[List[dict]]:
    lo, hi = TOOLS[tool]
    if not isinstance(v, list):
        errors[key] = "must be a list of {t, p}"
        return None
    if not lo <= len(v) <= hi:
        errors[key] = (f"{tool} needs exactly {lo} point(s)" if lo == hi
                       else f"{tool} needs {lo}–{hi} points")
        return None
    out = []
    for i, pt in enumerate(v):
        k = f"{key}[{i}]"
        if not isinstance(pt, dict) or set(pt) != {"t", "p"}:
            errors[k] = "must be an object with exactly t and p"
            return None
        t, p = pt["t"], pt["p"]
        if not isinstance(t, str) or len(t) > 40:
            errors[k + ".t"] = "must be an ISO-8601 time"
            return None
        try:
            tt = to_utc(t)
        except (ValueError, TypeError):
            errors[k + ".t"] = "must be an ISO-8601 time"
            return None
        if not T_MIN <= tt < T_MAX:
            errors[k + ".t"] = "must lie in 1990–2099"
            return None
        if not _num(p) or not 0 < float(p) <= PRICE_MAX:
            errors[k + ".p"] = "must be a finite price > 0 and ≤ 1e9"
            return None
        out.append({"t": iso(tt), "p": float(p)})
    return out


def _style(v, tool: str, base: dict, errors: dict) -> Optional[dict]:
    if v is None:
        return dict(base)
    if not isinstance(v, dict):
        errors["style"] = "must be an object"
        return None
    extra = [k for k in v if k not in STYLE_KEYS]
    if extra:
        errors["style"] = "unknown key(s): " + ", ".join(sorted(str(k)[:20] for k in extra)[:5])
        return None
    out = dict(base)
    out.update(v)
    if out["color"] not in COLORS:
        errors["style.color"] = "one of " + ", ".join(COLORS)
    w = out["width"]
    if not isinstance(w, int) or isinstance(w, bool) or not 1 <= w <= 4:
        errors["style.width"] = "an integer 1–4"
    if out["dash"] not in DASHES:
        errors["style.dash"] = "solid or dashed"
    lv = out["levels"]
    if lv is not None:
        if tool != "fibonacciLine":
            errors["style.levels"] = "only for fibonacciLine (null otherwise)"
        elif (not isinstance(lv, list) or not 1 <= len(lv) <= MAX_FIB_LEVELS
              or not all(_num(x) and FIB_RANGE[0] <= float(x) <= FIB_RANGE[1] for x in lv)
              or len({float(x) for x in lv}) != len(lv)):
            errors["style.levels"] = (f"null or 1–{MAX_FIB_LEVELS} distinct numbers in "
                                      f"[{FIB_RANGE[0]:g}, {FIB_RANGE[1]:g}]")
        else:
            out["levels"] = [float(x) for x in lv]
    return out if not any(k.startswith("style") for k in errors) else None


def _timeframes(v, errors: dict):
    if v == "all":
        return "all"
    if (not isinstance(v, list) or not v or not all(isinstance(x, str) and x in agg.TIMEFRAMES for x in v)
            or len(set(v)) != len(v)):
        errors["timeframes"] = '"all" or a non-empty list of distinct timeframes (' + " ".join(agg.TIMEFRAMES) + ")"
        return None
    return [tf for tf in agg.TIMEFRAMES if tf in v]


def _bool(v, key, errors):
    if not isinstance(v, bool):
        errors[key] = "must be true or false"
        return None
    return v


def _check_fields(body, allowed, errors, prefix=""):
    if not isinstance(body, dict):
        errors[prefix or "_"] = "must be a JSON object"
        return False
    extra = [k for k in body if k not in allowed]
    for k in extra[:20]:
        if not (k == "alert_id" and f"{prefix}alert_id" in errors):
            errors[f"{prefix}{str(k)[:40]}"] = "unknown field"
    return not extra


def _content(body: dict, tool: str, errors: dict, prefix: str = "", base_style: dict = DEFAULT_STYLE) -> dict:
    """The editable content of a new drawing (points, style, name, text, flags) — errors keyed with prefix."""
    e: Dict[str, str] = {}
    out = {"points": _points(tool, body.get("points"), e) if "points" in body else None}
    if "points" not in body:
        e["points"] = "required"
    out["style"] = _style(body.get("style"), tool, base_style, e)
    out["name"] = _plain(body.get("name"), "name", limits.MAX_NAME_CHARS, e, multiline=False)
    out["text"] = _plain(body.get("text"), "text", limits.MAX_NOTE_CHARS, e, multiline=True)
    out["locked"] = _bool(body.get("locked", False), "locked", e)
    out["visible"] = _bool(body.get("visible", True), "visible", e)
    out["timeframes"] = _timeframes(body.get("timeframes", "all"), e)
    if tool == "simpleAnnotation" and "text" not in e and out["text"] is None:
        e["text"] = "a text note needs a text (1–280 characters)"
    errors.update({prefix + k: v for k, v in e.items()})
    return out


def _tool(v, errors, key="tool"):
    if not isinstance(v, str) or v not in TOOLS:      # review L6: a list/dict must not reach the dict lookup
        errors[key] = "one of " + ", ".join(TOOLS)
        return None
    return v


def _client_id(v, errors, key="client_id") -> Optional[str]:
    """Optional idempotency key (review M3): a UUID (36 chars with hyphens or 32 hex), stored lower-case."""
    if v is None:
        return None
    if not isinstance(v, str) or not UUID_RE.match(v):
        errors[key] = "null or a UUID (e.g. 3f2c…-…, 36 characters, or 32 hex digits)"
        return None
    return str(uuid.UUID(v))


def _size_check(d: dict, errors: dict, key: str = "_"):
    n = compact_size({k: d[k] for k in ("tool", "points", "style", "name", "text", "timeframes")})
    if n > limits.MAX_DRAWING_JSON_BYTES:
        errors[key] = f"drawing too large ({n} bytes; at most {limits.MAX_DRAWING_JSON_BYTES})"


# --- validation entry points ------------------------------------------------------------------------

def validate_create(body) -> dict:
    """POST body → a new drawing dict (without id/times/basis). Raises DrawingInvalid."""
    errors: Dict[str, str] = {}
    if not _check_fields(body, CREATE_FIELDS, errors):
        raise DrawingInvalid(errors)
    try:
        inst = symbol_of(body.get("symbol"))
    except ValueError:
        errors["symbol"] = "required: a supported symbol (stocks/ETFs like NVDA, BRK.B; crypto like BTC-USD)"
        inst = None
    tool = _tool(body.get("tool"), errors)
    if tool is None:
        raise DrawingInvalid(errors)
    d = _content(body, tool, errors)
    if "alert_id" in body:
        errors["alert_id"] = ALERT_ID_MANAGED + " (create a line alert for this drawing instead)"
    d["alert_id"] = None
    d["client_id"] = _client_id(body.get("client_id"), errors)
    if errors:
        raise DrawingInvalid(errors)
    d["tool"] = tool
    d["symbol"] = inst.symbol
    d["asset_class"] = inst.asset_class
    _size_check(d, errors)
    if errors:
        raise DrawingInvalid(errors)
    return d


def validate_patch(body, current: dict) -> dict:
    """PATCH body against the current drawing → the changed fields. Raises DrawingInvalid / DrawingConflict."""
    errors: Dict[str, str] = {}
    if isinstance(body, dict) and "alert_id" in body:
        errors["alert_id"] = ALERT_ID_MANAGED + " (link or unlink the line through its alert)"
    if not _check_fields(body, PATCH_FIELDS, errors) or errors:
        raise DrawingInvalid(errors)
    if not body:
        raise DrawingInvalid({"_": "nothing to change"})
    tool = current["tool"]
    ch: Dict[str, object] = {}
    if "points" in body:
        ch["points"] = _points(tool, body["points"], errors)
    if "style" in body:
        if body["style"] is None:
            errors["style"] = "must be an object"
        else:
            ch["style"] = _style(body["style"], tool, current["style"], errors)
    if "name" in body:
        ch["name"] = _plain(body["name"], "name", limits.MAX_NAME_CHARS, errors, multiline=False)
    if "text" in body:
        ch["text"] = _plain(body["text"], "text", limits.MAX_NOTE_CHARS, errors, multiline=True)
        if tool == "simpleAnnotation" and "text" not in errors and ch["text"] is None:
            errors["text"] = "a text note needs a text (1–280 characters)"
    for k in ("locked", "visible"):
        if k in body:
            ch[k] = _bool(body[k], k, errors)
    if "timeframes" in body:
        ch["timeframes"] = _timeframes(body["timeframes"], errors)
    if errors:
        raise DrawingInvalid(errors)
    merged = dict(current)
    merged.update(ch)
    _size_check(merged, errors)
    if errors:
        raise DrawingInvalid(errors)
    if current["locked"] and ch.get("locked", True) and any(k in ch for k in ("points", "style", "text", "name")):
        raise DrawingConflict(LOCKED)
    return ch


# --- CRUD -----------------------------------------------------------------------------------------

def _now(now) -> datetime:
    return to_utc(now() if callable(now) else (now or datetime.now(timezone.utc)))


def list_drawings(symbol: str, *, store=default_store) -> dict:
    inst = symbol_of(symbol)
    rows = store.drawings_list(inst.symbol)
    return {"symbol": inst.symbol, "drawings": rows, "count": len(rows), "limit": limits.MAX_DRAWINGS_PER_SYMBOL,
            "split_check": split_check_status(inst, store=store)}


def create_ex(body, *, store=default_store, now=None):
    """(created, drawing). With a ``client_id`` that is already stored (a retried POST, review M3) nothing is
    created and the existing drawing is returned (created False) — its content is NOT compared."""
    d = validate_create(body)
    t = _now(now)
    d["id"] = new_id()
    d["created_at"] = d["updated_at"] = iso(t)
    d["price_basis"] = {"as_of": ny_today(t) if d["asset_class"] == "equity" else None, "factor": 1.0,
                        "rescales": 0}
    state, ident = store.drawing_insert(d, limits.MAX_DRAWINGS_PER_SYMBOL)
    if state == "full":
        raise DrawingConflict(too_many())
    cur = store.drawing_get(ident)
    if state == "exists" and (cur is None or cur["symbol"] != d["symbol"]):
        raise DrawingConflict(CLIENT_ID_USED)
    return state == "created", cur


def create(body, *, store=default_store, now=None) -> dict:
    return create_ex(body, store=store, now=now)[1]


def _require_id(drawing_id) -> str:
    if not isinstance(drawing_id, str) or not ID_RE.match(drawing_id):
        raise DrawingNotFound(NOT_FOUND)
    return drawing_id


def patch(drawing_id, body, *, store=default_store, now=None) -> dict:
    cur = store.drawing_get(_require_id(drawing_id))
    if cur is None:
        raise DrawingNotFound(NOT_FOUND)
    ch = validate_patch(body, cur)
    t = _now(now)
    basis = None
    if "points" in ch and instrument(cur["symbol"]).asset_class == "equity":
        basis = ny_today(t)       # new points are in the chart's current price basis
    if not store.drawing_update(drawing_id, ch, iso(t), basis_as_of=basis, expected_updated_at=cur["updated_at"]):
        raise DrawingConflict("The drawing was changed at the same time (e.g. by the split check) — reload it.")
    return store.drawing_get(drawing_id)


def delete(drawing_id, *, store=default_store) -> dict:
    cur = store.drawing_get(_require_id(drawing_id))
    if cur is None:
        raise DrawingNotFound(NOT_FOUND)
    if cur["locked"]:
        raise DrawingConflict(LOCKED)
    store.drawing_delete(drawing_id)
    store.add_audit("user", "drawing_deleted", {"id": drawing_id, "symbol": cur["symbol"], "tool": cur["tool"]})
    return {"deleted": drawing_id}


def delete_all(symbol: str, *, store=default_store) -> dict:
    inst = symbol_of(symbol)
    n = store.drawings_delete_symbol(inst.symbol)
    store.add_audit("user", "drawings_deleted_all", {"symbol": inst.symbol, "deleted": n})
    return {"symbol": inst.symbol, "deleted": n}


# --- export / import -----------------------------------------------------------------------------

def export(symbol: str, *, store=default_store, now=None) -> dict:
    inst = symbol_of(symbol)
    items = []
    for d in store.drawings_list(inst.symbol):
        item = {k: d[k] for k in EXPORT_FIELDS if k != "price_basis"}
        item["price_basis"] = {"as_of": d["price_basis"]["as_of"]}
        items.append(item)
    out = {"version": EXPORT_VERSION, "symbol": inst.symbol, "exported_at": iso(_now(now)), "drawings": items}
    n = compact_size(out)
    if n > limits.MAX_IMPORT_BYTES:
        raise DrawingConflict(f"The drawings of {inst.symbol} are larger than the import limit "
                              f"({limits.MAX_IMPORT_BYTES // 1024} KiB) — the export is refused so that the file can "
                              "always be imported again. Delete or simplify some drawings (freehand drawings are "
                              "the largest).")
    return out


def _basis(v, asset_class: str, today: str, errors: dict, key: str) -> Optional[str]:
    if not isinstance(v, dict) or set(v) != {"as_of"}:
        errors[key] = 'must be {"as_of": "YYYY-MM-DD" | null}'
        return None
    a = v["as_of"]
    if asset_class == "crypto":
        if a is not None:
            errors[key + ".as_of"] = "must be null for crypto"
        return None
    if not isinstance(a, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", a):
        errors[key + ".as_of"] = "a date YYYY-MM-DD"
        return None
    try:
        d = date.fromisoformat(a)
    except ValueError:
        errors[key + ".as_of"] = "a date YYYY-MM-DD"
        return None
    if not date(2016, 1, 1) <= d <= date.fromisoformat(today):
        errors[key + ".as_of"] = "between 2016-01-01 and today"
        return None
    return a


def validate_import(body, symbol: str, now, notes: Optional[dict] = None) -> List[dict]:
    """The export file → validated new drawings (all or nothing). Raises DrawingInvalid. An entry's
    ``alert_id`` (managed by alerts since MR7) is stripped, not imported: ``notes["alert_id_ignored"] = True``."""
    inst = symbol_of(symbol)
    n = compact_size(body) if isinstance(body, (dict, list)) else 0
    if n > limits.MAX_IMPORT_BYTES:
        raise DrawingInvalid({"_": f"file too large ({n} bytes; at most {limits.MAX_IMPORT_BYTES})"},
                             f"The drawings file is too large (at most {limits.MAX_IMPORT_BYTES // 1024} KiB).")
    errors: Dict[str, str] = {}
    if not _check_fields(body, FILE_FIELDS, errors):
        raise DrawingInvalid(errors, "Not a UFOS.ai drawings file.")
    for k in ("version", "symbol", "drawings"):
        if k not in body:
            errors[k] = "required"
    if errors:
        raise DrawingInvalid(errors, "Not a UFOS.ai drawings file.")
    if body["version"] != EXPORT_VERSION or isinstance(body["version"], bool):
        raise DrawingInvalid({"version": f"must be {EXPORT_VERSION}"}, "Unsupported drawings file version.")
    try:
        file_sym = symbol_of(body["symbol"]).symbol
    except ValueError:
        raise DrawingInvalid({"symbol": "not a supported symbol"}, "Not a UFOS.ai drawings file.")
    if file_sym != inst.symbol:
        raise DrawingInvalid({"symbol": f"the file is for {file_sym}"},
                             f"This file holds drawings for {file_sym}, not {inst.symbol}.")
    ea = body.get("exported_at")
    if ea is not None:
        try:
            if not isinstance(ea, str) or len(ea) > 40:
                raise ValueError
            to_utc(ea)
        except (ValueError, TypeError):
            errors["exported_at"] = "an ISO-8601 time"
    items = body["drawings"]
    if not isinstance(items, list):
        raise DrawingInvalid({"drawings": "must be a list"}, "Not a UFOS.ai drawings file.")
    if len(items) > limits.MAX_DRAWINGS_PER_SYMBOL:
        raise DrawingInvalid({"drawings": f"at most {limits.MAX_DRAWINGS_PER_SYMBOL} drawings"})
    t = _now(now)
    today = ny_today(t)
    out = []
    for i, it in enumerate(items):
        p = f"drawings[{i}]."
        e: Dict[str, str] = {}
        if isinstance(it, dict) and "alert_id" in it:
            it = {k: v for k, v in it.items() if k != "alert_id"}
            if notes is not None:
                notes["alert_id_ignored"] = True
        if not _check_fields(it, EXPORT_FIELDS, e, p):
            errors.update(e)
            continue
        missing = [k for k in ("tool", "points", "price_basis") if k not in it]
        for k in missing:
            e[p + k] = "required"
        tool = _tool(it.get("tool"), e, p + "tool") if "tool" in it else None
        if tool is None:
            errors.update(e)
            continue
        d = _content(it, tool, e, p)
        basis = _basis(it.get("price_basis"), inst.asset_class, today, e, p + "price_basis") \
            if "price_basis" in it else None
        if not e:
            d.update({"tool": tool, "symbol": inst.symbol, "asset_class": inst.asset_class, "alert_id": None})
            _size_check(d, e, p.rstrip("."))
        if e:
            errors.update(e)
            continue
        d["id"] = new_id()
        d["created_at"] = d["updated_at"] = iso(t)
        d["price_basis"] = {"as_of": basis, "factor": 1.0, "rescales": 0}
        out.append(d)
        if len(errors) > 50:
            break
    if errors:
        raise DrawingInvalid(dict(list(errors.items())[:50]))
    return out


def import_file(body, symbol: str, mode: str = "append", *, store=default_store, now=None,
                client_id: Optional[str] = None) -> dict:
    """Imports an export file (all or nothing). ``client_id`` (optional UUID, review M3) makes a retried
    import idempotent: entry i gets the derived id uuid5(client_id, i); when any of them is already stored
    nothing is written and the stored drawings are returned with ``repeated: true``."""
    if mode not in ("append", "replace"):
        raise DrawingInvalid({"mode": "append or replace"})
    errors: Dict[str, str] = {}
    cid = _client_id(client_id, errors)
    if errors:
        raise DrawingInvalid(errors)
    notes = {"alert_id_ignored": False}
    items = validate_import(body, symbol, now, notes)
    sym = symbol_of(symbol).symbol
    if cid is not None:
        for i, d in enumerate(items):
            d["client_id"] = str(uuid.uuid5(uuid.UUID(cid), str(i)))
    state, replaced = store.drawings_import(sym, items, replace=(mode == "replace"),
                                            max_per_symbol=limits.MAX_DRAWINGS_PER_SYMBOL)
    if state == "exists":
        prev = store.drawings_by_client_ids([d["client_id"] for d in items])
        if any(p["symbol"] != sym for p in prev):
            raise DrawingConflict(CLIENT_ID_USED)
        return {"symbol": sym, "imported": len(prev), "replaced": 0, "repeated": True,
                "alert_id_ignored": notes["alert_id_ignored"], "drawings": prev}
    if state == "full":
        raise DrawingConflict(too_many())
    store.add_audit("user", "drawings_imported", {"symbol": sym, "imported": len(items), "replaced": replaced,
                                                   "mode": mode})
    return {"symbol": sym, "imported": len(items), "replaced": replaced, "repeated": False,
            "alert_id_ignored": notes["alert_id_ignored"], "drawings": [store.drawing_get(d["id"]) for d in items]}


# --- split rule ----------------------------------------------------------------------------------

def action_key(a: dict) -> str:
    return f"{a.get('kind')}:{a.get('ex_date')}:{a.get('old_rate')}:{a.get('new_rate')}"


def split_factor(a: dict) -> Optional[float]:
    """new_rate / old_rate (4:1 forward → 4.0, 1:10 reverse → 0.1); None when unusable."""
    o, n = a.get("old_rate"), a.get("new_rate")
    if not _num(o) or not _num(n) or float(o) <= 0 or float(n) <= 0:
        return None
    f = float(n) / float(o)
    return f if math.isfinite(f) and f > 0 and f != 1.0 else None


def _status_get(store) -> dict:
    raw = store.meta_get(META_SPLIT_CHECK)
    if not raw:
        return {"state": "pending", "checked_at": None, "reason": None}
    try:
        s = json.loads(raw)
        return {"state": s["state"], "checked_at": s.get("checked_at"), "reason": s.get("reason")}
    except (ValueError, KeyError, TypeError):
        return {"state": "pending", "checked_at": None, "reason": None}


def needs_check(status: dict, now=None) -> bool:
    """Review L9: True when the (equity) split check did not run yet today (NY date) — the drawings route then
    asks the supervised task for a check (non-blocking)."""
    if status.get("state") == "not_applicable":
        return False
    at = status.get("checked_at")
    if not at:
        return True
    try:
        return ny_today(to_utc(at)) < ny_today(_now(now))
    except (ValueError, TypeError):
        return True


def split_check_status(inst, *, store=default_store) -> dict:
    if inst.asset_class != "equity":
        return {"state": "not_applicable", "checked_at": None, "reason": None}
    return _status_get(store)


def _status_set(store, state: str, at: datetime, reason: Optional[str]) -> dict:
    s = {"state": state, "checked_at": iso(at), "reason": limits.clip(reason) if reason else None}
    store.meta_set(META_SPLIT_CHECK, compact(s))
    return s


def split_check(sources, now=None, *, store=default_store, symbols: Optional[List[str]] = None) -> dict:
    """Applies pending splits to the stored drawings (BLOCKING; caller: Research pool + research priority).
    Returns ``{state, checked_at, reason, symbols, rescaled, skipped}``. A source failure (UserFacingError
    other than StoreUnavailable) → state ``failed`` with the reason, nothing changed."""
    t = _now(now)
    today = date.fromisoformat(ny_today(t))
    bases = store.drawings_oldest_basis()                 # {symbol: oldest as_of} (equities, as_of not null)
    if symbols is not None:
        bases = {s: v for s, v in bases.items() if s in set(symbols)}
    todo = {s: date.fromisoformat(a) for s, a in bases.items() if date.fromisoformat(a) < today}
    rescaled, skipped = 0, []
    if todo:
        start = min(todo.values()) + timedelta(days=1)
        try:
            actions = sources.splits(sorted(todo), start, today)
        except store.StoreUnavailable:
            raise
        except UserFacingError as ex:
            logger.warning("Drawing split check failed: %s", ex.user_message)
            s = _status_set(store, "failed", t, f"The split check could not read Alpaca's corporate actions: "
                                                f"{ex.user_message}")
            return dict(s, symbols=len(todo), rescaled=0, skipped=[])
        by_sym: Dict[str, List[dict]] = {}
        for a in actions or []:
            if a.get("kind") in SPLIT_KINDS and a.get("symbol") in todo:
                try:
                    ex_d = date.fromisoformat(str(a.get("ex_date"))[:10])
                except ValueError:
                    ex_d = None
                if ex_d is None or ex_d > today:
                    continue
                f = split_factor(a)
                if f is None:
                    skipped.append(action_key(a))
                    logger.warning("Split %s of %s has no usable rates — drawings not rescaled", action_key(a),
                                   a.get("symbol"))
                    store.add_audit("system", "split_unusable", {"symbol": a.get("symbol"), "action": action_key(a)})
                    continue
                by_sym.setdefault(a["symbol"], []).append(dict(a, _ex=ex_d, _f=f))
        for sym, acts in by_sym.items():
            acts.sort(key=lambda a: (a["_ex"], action_key(a)))
            for d in store.drawings_list(sym):
                rescaled += _apply(d, acts, t, store)
    reason = None
    if skipped:
        reason = "Split(s) without usable rates were not applied: " + ", ".join(skipped[:3])
    s = _status_set(store, "ok", t, reason)
    if rescaled:
        logger.info("Drawing split check: %d drawing rescale(s) applied", rescaled)
    return dict(s, symbols=len(todo), rescaled=rescaled, skipped=skipped)


def _apply(d: dict, acts: List[dict], t: datetime, store) -> int:
    as_of = d["price_basis"]["as_of"]
    if as_of is None:
        return 0
    pending = [a for a in acts if a["_ex"] > date.fromisoformat(as_of)]
    if not pending:
        return 0
    pts = [dict(p) for p in d["points"]]
    steps = []
    factor = d["price_basis"]["factor"]
    for a in pending:
        before = [p["p"] for p in pts]
        for p in pts:
            p["p"] = p["p"] / a["_f"]
        factor *= a["_f"]
        steps.append({"action_key": action_key(a), "factor": a["_f"], "ex_date": a["_ex"].isoformat(),
                      "detail": {"kind": a["kind"], "old_rate": a.get("old_rate"), "new_rate": a.get("new_rate"),
                                 "before": before, "after": [p["p"] for p in pts]}})
    ok = store.drawing_apply_rescales(d["id"], as_of, steps, pts, pending[-1]["_ex"].isoformat(), factor, iso(t))
    if not ok:
        return 0                       # changed meanwhile or already applied → next check
    logger.info("Drawing %s (%s) rescaled by %s", d["id"], d["symbol"], ", ".join(s["action_key"] for s in steps))
    return len(steps)


# --- supervised task "drawing_split_check" ---------------------------------------------------------------

_wake: Optional[asyncio.Event] = None


def request_check() -> None:
    """Asks the running split-check task for a check now (e.g. after an import). Loop thread only."""
    if _wake is not None:
        _wake.set()


async def _sleep_or_wake(delay: float, sleep) -> None:
    ev = _wake
    sleeper = asyncio.ensure_future(sleep(delay))
    waiter = asyncio.ensure_future(ev.wait()) if ev is not None else None
    try:
        await asyncio.wait({sleeper} | ({waiter} if waiter else set()), return_when=asyncio.FIRST_COMPLETED)
    finally:
        if not sleeper.done():
            sleeper.cancel()
        if waiter is not None:
            waiter.cancel()
    if ev is not None:
        ev.clear()
    if sleeper.done():                   # finished by itself (not cancelled above): pass its outcome on
        if sleeper.cancelled():
            raise asyncio.CancelledError
        sleeper.result()


async def run(sources_fn, *, store=default_store, sleep: Callable = asyncio.sleep,
              now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
    """Supervised task ``drawing_split_check``: at start (once the store is open), every
    SPLIT_CHECK_INTERVAL_SECONDS, and on ``request_check()``; failures retry after SPLIT_CHECK_RETRY_SECONDS."""
    global _wake
    from strategy_platform.data import http_gate
    from .executor import run_blocking
    _wake = asyncio.Event()
    while True:
        delay = float(limits.STORE_REOPEN_SECONDS)
        if store.is_open():
            try:
                with http_gate.research_priority():
                    res = await run_blocking(split_check, sources_fn(), now(), store=store)
                delay = float(limits.SPLIT_CHECK_INTERVAL_SECONDS if res["state"] == "ok"
                              else limits.SPLIT_CHECK_RETRY_SECONDS)
            except store.StoreUnavailable as ex:
                logger.warning("drawing split check: %s", ex.user_message)
        await _sleep_or_wake(delay, sleep)
