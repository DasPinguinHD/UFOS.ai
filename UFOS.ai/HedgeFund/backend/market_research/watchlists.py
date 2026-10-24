"""Watchlists of the symbol workspace (SPEC §10 MR4, exact JSON in §13 "MR4 backend notes").

- A watchlist = one group of the panel: ``{id, name, position, collapsed, created_at, updated_at, items}``;
  items ``{symbol, position, added_at, name, asset_class, kind, exchange, covered}`` (asset fields joined
  from the ``assets`` table — untrusted plain text, already sanitised there). No list is seeded.
- Limits: ``MAX_WATCHLISTS`` lists, ``MAX_WATCHLIST_ITEMS`` symbols per list, names 1–``MAX_NAME_CHARS``
  characters (trimmed, no control/format characters, unique case-insensitively). Names are stored exactly as
  given (parameterised SQL, no escaping) and shown as plain text by the WPF.
- Symbols: platform symbols (``core/instruments``; ``BTC/USD`` → ``BTC-USD``). A symbol NEW to a list must be
  in Alpaca's asset list (``assets.require``); symbols already in it are kept as they are (a reorder never
  fails because a symbol was delisted meanwhile).
- Every function is BLOCKING (SQLite) — the routes run them on the Research pool (SPEC §5.1). Writes are
  serialised by one lock; each answer carries a sequence number so the in-memory copy for the hub
  (``Workspace``) never goes back to an older state.
- ``Workspace``: the in-memory ``{list id: [symbols]}`` the market-data hub uses to expand the
  ``watchlist:<id>`` WebSocket topic (demand registration of visible watchlists; loaded lazily from the
  store on the loop's Research pool, updated after every change).

UI preferences of the single local user — no audit rows (SPEC notes). Part of UFOS.ai, a proof-of-concept
portfolio project; research only.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time as _time
import unicodedata
import uuid
from typing import Callable, Dict, List, Optional, Tuple

from strategy_platform.core.instruments import instrument

from . import limits
from . import store as default_store

logger = logging.getLogger(__name__)

ID_RE = re.compile(r"^[0-9a-f]{32}$")
NOT_FOUND = "Watchlist not found."
SYMBOL_HINT = "not a supported symbol (stocks/ETFs like NVDA, BRK.B; crypto like BTC-USD)"


class WorkspaceError(Exception):
    """A client-visible refusal: (HTTP status, JSON payload)."""

    def __init__(self, status: int, payload: dict):
        super().__init__(payload.get("error"))
        self.status, self.payload = status, payload


def bad(field: str, message: str) -> WorkspaceError:
    return WorkspaceError(400, {"error": "invalid input", "errors": {field: message}})


def conflict(message: str) -> WorkspaceError:
    return WorkspaceError(409, {"error": message})


def not_found(message: str = NOT_FOUND) -> WorkspaceError:
    return WorkspaceError(404, {"error": message})


# --- validation ----------------------------------------------------------------------------------------

def check_id(value) -> str:
    if not isinstance(value, str) or not ID_RE.match(value):
        raise not_found()
    return value


def check_name(value, field: str = "name") -> str:
    if not isinstance(value, str):
        raise bad(field, "must be a string")
    name = value.strip()
    if not 1 <= len(name) <= limits.MAX_NAME_CHARS:
        raise bad(field, f"1–{limits.MAX_NAME_CHARS} characters (leading/trailing spaces are removed)")
    for ch in name:
        cat = unicodedata.category(ch)
        if cat[0] == "C" or cat in ("Zl", "Zp"):
            raise bad(field, "must not contain control or formatting characters")
    return name


def check_symbol(value, field: str = "symbol") -> Tuple[str, str]:
    """-> (platform symbol, asset class) or a 400."""
    if not isinstance(value, str) or not 1 <= len(value) <= 20:
        raise bad(field, SYMBOL_HINT)
    try:
        inst = instrument(value.replace("/", "-"))
    except ValueError:
        raise bad(field, SYMBOL_HINT) from None
    return inst.symbol, inst.asset_class


def asset_class_of(symbol: str) -> Optional[str]:
    try:
        return instrument(symbol).asset_class
    except ValueError:
        return None


# --- store access ----------------------------------------------------------------------------------------

_lock = threading.RLock()
_seq = 0


def _bump() -> int:
    global _seq
    _seq += 1
    return _seq


def _now() -> str:
    return default_store.utc_now_iso()


def _read(conn) -> List[dict]:
    lists = conn.execute("SELECT id, name, position, collapsed, created_at, updated_at FROM watchlists "
                         "ORDER BY position, created_at, id").fetchall()
    rows = conn.execute("SELECT i.list_id, i.symbol, i.position, i.added_at, a.name AS a_name, a.kind AS a_kind, "
                        "a.exchange AS a_exchange, a.asset_class AS a_class, a.symbol AS a_symbol "
                        "FROM watchlist_items i LEFT JOIN assets a ON a.symbol = i.symbol "
                        "ORDER BY i.list_id, i.position, i.symbol").fetchall()
    items: Dict[str, List[dict]] = {}
    for r in rows:
        covered = r["a_symbol"] is not None
        items.setdefault(r["list_id"], []).append({
            "symbol": r["symbol"], "position": int(r["position"]), "added_at": r["added_at"],
            "name": r["a_name"] if covered else None,
            "asset_class": r["a_class"] if covered else asset_class_of(r["symbol"]),
            "kind": r["a_kind"] if covered else None,
            "exchange": r["a_exchange"] if covered else None,
            "covered": covered})
    return [{"id": r["id"], "name": r["name"], "position": int(r["position"]), "collapsed": bool(r["collapsed"]),
             "created_at": r["created_at"], "updated_at": r["updated_at"], "items": items.get(r["id"], [])}
            for r in lists]


def _snapshot(store) -> Tuple[int, List[dict]]:
    with store._connect() as conn:
        return _bump(), _read(conn)


def list_all(store=default_store) -> Tuple[int, List[dict]]:
    """(sequence number, all lists by position)."""
    with _lock:
        return _snapshot(store)


def get_list(list_id: str, store=default_store) -> dict:
    check_id(list_id)
    with _lock:
        _seq_no, lists = _snapshot(store)
    for wl in lists:
        if wl["id"] == list_id:
            return wl
    raise not_found()


def _row(conn, list_id: str):
    row = conn.execute("SELECT id, name FROM watchlists WHERE id = ?", (list_id,)).fetchone()
    if row is None:
        raise not_found()
    return row


def _name_taken(conn, name: str, except_id: Optional[str] = None) -> bool:
    key = name.casefold()
    for r in conn.execute("SELECT id, name FROM watchlists"):
        if r["id"] != except_id and r["name"].casefold() == key:
            return True
    return False


def _compact(conn) -> None:
    ids = [r[0] for r in conn.execute("SELECT id FROM watchlists ORDER BY position, created_at, id")]
    for pos, i in enumerate(ids):
        conn.execute("UPDATE watchlists SET position = ? WHERE id = ?", (pos, i))


def create(name, store=default_store) -> Tuple[str, int, List[dict]]:
    name = check_name(name)
    with _lock:
        with store._connect() as conn:
            store._begin(conn)
            n = conn.execute("SELECT COUNT(*) FROM watchlists").fetchone()[0]
            if n >= limits.MAX_WATCHLISTS:
                raise conflict(f"At most {limits.MAX_WATCHLISTS} watchlists — delete one first.")
            if _name_taken(conn, name):
                raise conflict(f'A watchlist named "{name}" already exists.')
            new_id = uuid.uuid4().hex
            now = _now()
            conn.execute("INSERT INTO watchlists(id, name, position, collapsed, created_at, updated_at) "
                         "VALUES(?, ?, ?, 0, ?, ?)", (new_id, name, n, now, now))
        seq, lists = _snapshot(store)
    return new_id, seq, lists


def patch(list_id: str, changes: dict, store=default_store) -> Tuple[int, List[dict]]:
    """``changes`` already type-checked by the route: name (str), collapsed (bool), position (int)."""
    check_id(list_id)
    name = check_name(changes["name"]) if "name" in changes else None
    with _lock:
        with store._connect() as conn:
            store._begin(conn)
            _row(conn, list_id)
            sets, args = ["updated_at = ?"], [_now()]
            if name is not None:
                if _name_taken(conn, name, except_id=list_id):
                    raise conflict(f'A watchlist named "{name}" already exists.')
                sets.append("name = ?")
                args.append(name)
            if "collapsed" in changes:
                sets.append("collapsed = ?")
                args.append(1 if changes["collapsed"] else 0)
            conn.execute(f"UPDATE watchlists SET {', '.join(sets)} WHERE id = ?", args + [list_id])
            if "position" in changes:
                ids = [r[0] for r in conn.execute("SELECT id FROM watchlists ORDER BY position, created_at, id")]
                pos = changes["position"]
                if not 0 <= pos < len(ids):
                    raise bad("position", f"must be 0–{len(ids) - 1}")
                ids.remove(list_id)
                ids.insert(pos, list_id)
                for p, i in enumerate(ids):
                    conn.execute("UPDATE watchlists SET position = ? WHERE id = ?", (p, i))
        return _snapshot(store)


def delete(list_id: str, store=default_store) -> Tuple[int, List[dict]]:
    check_id(list_id)
    with _lock:
        with store._connect() as conn:
            store._begin(conn)
            _row(conn, list_id)
            conn.execute("DELETE FROM watchlist_items WHERE list_id = ?", (list_id,))
            conn.execute("DELETE FROM watchlists WHERE id = ?", (list_id,))
            _compact(conn)
        return _snapshot(store)


def _items(conn, list_id: str) -> List[tuple]:
    return [(r["symbol"], r["added_at"]) for r in
            conn.execute("SELECT symbol, added_at FROM watchlist_items WHERE list_id = ? ORDER BY position, symbol",
                         (list_id,))]


def _write_items(conn, list_id: str, symbols: List[str], added: Dict[str, str]) -> None:
    now = _now()
    conn.execute("DELETE FROM watchlist_items WHERE list_id = ?", (list_id,))
    conn.executemany("INSERT INTO watchlist_items(list_id, symbol, position, added_at) VALUES(?, ?, ?, ?)",
                     [(list_id, s, p, added.get(s, now)) for p, s in enumerate(symbols)])
    conn.execute("UPDATE watchlists SET updated_at = ? WHERE id = ?", (now, list_id))


def _not_covered(missing: List[str], first_error: str) -> WorkspaceError:
    return WorkspaceError(404, {"error": first_error, "kind": "SymbolNotCovered", "symbols": missing})


def _check_covered(symbols: List[str], require: Callable[[str], dict]) -> None:
    """``require`` = assets.require bound to the sources (SymbolNotCovered for a symbol not on Alpaca)."""
    from .assets import SymbolNotCovered
    missing, first = [], None
    for s in symbols:
        try:
            require(s)
        except SymbolNotCovered as ex:
            missing.append(s)
            first = first or ex.user_message
    if missing:
        raise _not_covered(missing, first)


def normalise_symbols(values) -> List[str]:
    if not isinstance(values, list):
        raise bad("symbols", "must be a list of symbols")
    if len(values) > limits.MAX_WATCHLIST_ITEMS:
        raise bad("symbols", f"at most {limits.MAX_WATCHLIST_ITEMS} symbols per watchlist")
    out, seen = [], set()
    for k, v in enumerate(values):
        sym, _ac = check_symbol(v, f"symbols[{k}]")
        if sym in seen:
            raise bad("symbols", f"{sym} is listed twice.")
        seen.add(sym)
        out.append(sym)
    return out


def replace_items(list_id: str, symbols: List[str], require: Callable[[str], dict],
                  store=default_store) -> Tuple[int, List[dict]]:
    """PUT …/items: ``symbols`` from ``normalise_symbols`` (ordered, unique)."""
    check_id(list_id)
    with _lock:
        with store._connect() as conn:
            _row(conn, list_id)
            existing = dict(_items(conn, list_id))
        _check_covered([s for s in symbols if s not in existing], require)
        with store._connect() as conn:
            store._begin(conn)
            _row(conn, list_id)
            existing = dict(_items(conn, list_id))
            _write_items(conn, list_id, symbols, existing)
        return _snapshot(store)


def add_item(list_id: str, symbol: str, position: Optional[int], require: Callable[[str], dict],
             store=default_store) -> Tuple[int, List[dict]]:
    check_id(list_id)
    with _lock:
        with store._connect() as conn:
            _row(conn, list_id)
            current = _items(conn, list_id)
        syms = [s for s, _a in current]
        if symbol in syms:
            raise conflict(f"{symbol} is already in this watchlist.")
        if len(syms) >= limits.MAX_WATCHLIST_ITEMS:
            raise conflict(f"A watchlist holds at most {limits.MAX_WATCHLIST_ITEMS} symbols.")
        if position is not None and not 0 <= position <= len(syms):
            raise bad("position", f"must be 0–{len(syms)}")
        _check_covered([symbol], require)
        syms.insert(len(syms) if position is None else position, symbol)
        with store._connect() as conn:
            store._begin(conn)
            _row(conn, list_id)
            _write_items(conn, list_id, syms, dict(current))
        return _snapshot(store)


def remove_item(list_id: str, symbol: str, store=default_store) -> Tuple[int, List[dict]]:
    check_id(list_id)
    with _lock:
        with store._connect() as conn:
            store._begin(conn)
            _row(conn, list_id)
            current = _items(conn, list_id)
            syms = [s for s, _a in current]
            if symbol not in syms:
                raise not_found(f"{symbol} is not in this watchlist.")
            syms.remove(symbol)
            _write_items(conn, list_id, syms, dict(current))
        return _snapshot(store)


def memberships(store=default_store) -> Dict[str, List[str]]:
    """{symbol: [list ids in panel order]} (palette hint)."""
    with store._connect() as conn:
        rows = conn.execute("SELECT i.symbol, i.list_id FROM watchlist_items i JOIN watchlists w ON w.id = i.list_id "
                            "ORDER BY w.position, w.id").fetchall()
    out: Dict[str, List[str]] = {}
    for r in rows:
        out.setdefault(r["symbol"], []).append(r["list_id"])
    return out


def payload(lists: List[dict]) -> dict:
    return {"lists": lists, "limits": {"max_lists": limits.MAX_WATCHLISTS, "max_items": limits.MAX_WATCHLIST_ITEMS,
                                       "max_name_chars": limits.MAX_NAME_CHARS}}


# --- in-memory copy for the hub (watchlist:<id> topics) ------------------------------------------------

class Workspace:
    """{list id: [symbols]} for the hub's ``watchlist:<id>`` expansion. Event-loop side only."""

    def __init__(self, hub=None, *, store=default_store, monotonic: Callable[[], float] = _time.monotonic):
        self.hub = hub
        self.store = store
        self._mono = monotonic
        self._symbols: Optional[Dict[str, List[str]]] = None
        self._seq = -1
        self._loading = False
        self._failed_at: Optional[float] = None

    @property
    def loaded(self) -> bool:
        return self._symbols is not None

    def apply(self, seq: int, lists: List[dict]) -> bool:
        """Takes a store answer (newer than the last one applied) and lets the hub re-expand. True if applied."""
        if seq < self._seq:
            return False
        self._seq = seq
        new = {wl["id"]: [i["symbol"] for i in wl["items"]] for wl in lists}
        changed = new != self._symbols
        self._symbols = new
        if changed and self.hub is not None:
            try:
                self.hub.refresh()
            except Exception:   # noqa: BLE001 — a hub problem never fails a watchlist request
                logger.exception("Research hub refresh after a watchlist change failed")
        return True

    def symbols_for(self, list_id: str) -> List[str]:
        if self._symbols is None:
            self._schedule_load()
            return []
        return list(self._symbols.get(list_id, ()))

    def _schedule_load(self) -> None:
        if self._loading:
            return
        if self._failed_at is not None and self._mono() - self._failed_at < limits.WORKSPACE_RELOAD_SECONDS:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._loading = True
        loop.create_task(self.load())

    async def load(self) -> None:
        """Reads all lists on the Research pool and applies them (never raises)."""
        from .executor import run_blocking
        self._loading = True
        try:
            seq, lists = await run_blocking(list_all, self.store)
        except asyncio.CancelledError:
            raise
        except Exception as ex:   # noqa: BLE001 — store unavailable etc.: retried on a later expansion
            self._failed_at = self._mono()
            logger.warning("Research watchlists could not be loaded for the live hub: %s",
                           getattr(ex, "user_message", None) or type(ex).__name__)
        else:
            self._failed_at = None
            self.apply(seq, lists)
        finally:
            self._loading = False
