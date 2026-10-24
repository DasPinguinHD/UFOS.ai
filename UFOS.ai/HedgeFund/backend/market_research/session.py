"""Workspace session restore + the Ctrl+K palette's recent symbols (SPEC §10 MR4, §13 "MR4 backend notes").

- Session = ``{tabs: [{symbol, tf, type, session}] (≤ MAX_TABS), active, sidebar: {collapsed, section}}`` in
  ONE row (``session.id = 1``), replaced on every PUT. Tab symbols are checked by syntax only (restoring must
  work without the network; an uncovered symbol shows the chart's own 404 text). ``session`` = ``rth``/``eth``
  for stocks/ETFs, ``utc`` for crypto (what ``/bars`` echoes).
- Recent symbols (newest first, ≤ ``MAX_RECENT_SYMBOLS``) and the palette search: ``/research/assets``
  ranking (exact, symbol prefix, name word, name substring) with recent symbols first inside each group.

All functions are BLOCKING (SQLite; the palette may refresh an empty asset list through the gate) — the
routes run them on the Research pool inside research priority. UI preferences: no audit rows.
Part of UFOS.ai, a proof-of-concept portfolio project; research only.
"""
from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional

from . import assets as assets_mod
from . import limits
from . import store as default_store
from . import watchlists
from .bars import aggregate as agg
from .watchlists import bad

logger = logging.getLogger(__name__)

TAB_KEYS = {"symbol", "tf", "type", "session"}
BODY_KEYS = {"tabs", "active", "sidebar"}
SIDEBAR_KEYS = {"collapsed", "section"}
DEFAULT_SIDEBAR = {"collapsed": False, "section": "overview"}
MATCHES = ("exact", "symbol_prefix", "name_word", "name_substring")


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def check_state(body) -> dict:
    """The PUT body -> the normalised state (raises a 400 WorkspaceError)."""
    if not isinstance(body, dict):
        raise bad("_", "a JSON object body is required")
    extra = sorted(str(k)[:40] for k in body if k not in BODY_KEYS)
    if extra:
        raise watchlists.WorkspaceError(400, {"error": "invalid input",
                                              "errors": {k: "unknown field" for k in extra[:20]}})
    missing = sorted(BODY_KEYS - set(body))
    if missing:
        raise watchlists.WorkspaceError(400, {"error": "invalid input",
                                              "errors": {k: "required" for k in missing}})
    tabs = body["tabs"]
    if not isinstance(tabs, list):
        raise bad("tabs", "must be a list")
    if len(tabs) > limits.MAX_TABS:
        raise bad("tabs", f"at most {limits.MAX_TABS} tabs")
    out_tabs = []
    for k, t in enumerate(tabs):
        f = f"tabs[{k}]"
        if not isinstance(t, dict) or set(t) != TAB_KEYS:
            raise bad(f, "must be an object with exactly symbol, tf, type, session")
        sym, ac = watchlists.check_symbol(t["symbol"], f + ".symbol")
        if t["tf"] not in agg.TIMEFRAMES:
            raise bad(f + ".tf", "must be one of " + " ".join(agg.TIMEFRAMES))
        if t["type"] not in limits.TAB_TYPES:
            raise bad(f + ".type", "must be one of " + " ".join(limits.TAB_TYPES))
        allowed = ("utc",) if ac == "crypto" else agg.SESSIONS
        if t["session"] not in allowed:
            raise bad(f + ".session", f"must be {' or '.join(allowed)} for {sym}")
        out_tabs.append({"symbol": sym, "tf": t["tf"], "type": t["type"], "session": t["session"]})
    active = body["active"]
    if not out_tabs:
        if active is not None:
            raise bad("active", "must be null when there are no tabs")
    elif not _is_int(active) or not 0 <= active < len(out_tabs):
        raise bad("active", f"must be a tab index 0–{len(out_tabs) - 1}")
    sb = body["sidebar"]
    if not isinstance(sb, dict) or set(sb) != SIDEBAR_KEYS:
        raise bad("sidebar", "must be an object with exactly collapsed, section")
    if not isinstance(sb["collapsed"], bool):
        raise bad("sidebar.collapsed", "must be true or false")
    if sb["section"] not in limits.SIDEBAR_SECTIONS:
        raise bad("sidebar.section", "must be one of " + " ".join(limits.SIDEBAR_SECTIONS))
    return {"tabs": out_tabs, "active": active, "sidebar": {"collapsed": sb["collapsed"], "section": sb["section"]}}


def _payload(state: Optional[dict], updated_at: Optional[str], note: Optional[str] = None) -> dict:
    st = state or {"tabs": [], "active": None, "sidebar": dict(DEFAULT_SIDEBAR)}
    return {"schema": limits.SESSION_SCHEMA, "tabs": st["tabs"], "active": st["active"], "sidebar": st["sidebar"],
            "saved": state is not None, "updated_at": updated_at if state is not None else None, "note": note,
            "limits": {"max_tabs": limits.MAX_TABS}}


def get(store=default_store) -> dict:
    with store._connect() as conn:
        row = conn.execute("SELECT state_json, updated_at FROM session WHERE id = 1").fetchone()
    if row is None:
        return _payload(None, None)
    try:
        state = check_state(json.loads(row["state_json"]))
    except (ValueError, watchlists.WorkspaceError) as ex:     # written by another version / edited by hand
        logger.warning("Research session in %s is not readable (%s) — ignored", limits.DB_FILENAME, ex)
        return _payload(None, None, "The saved session could not be read and was ignored.")
    return _payload(state, row["updated_at"])


def put(state: dict, store=default_store) -> dict:
    """``state`` from ``check_state``."""
    now = store.utc_now_iso()
    text = json.dumps(state, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    with store._connect() as conn:
        conn.execute("INSERT INTO session(id, state_json, updated_at) VALUES(1, ?, ?) ON CONFLICT(id) DO UPDATE "
                     "SET state_json = excluded.state_json, updated_at = excluded.updated_at", (text, now))
    return _payload(state, now)


# --- recent symbols ---------------------------------------------------------------------------------------

def recent(store=default_store) -> List[str]:
    with store._connect() as conn:
        return [r[0] for r in conn.execute("SELECT symbol FROM recent_symbols ORDER BY seq DESC LIMIT ?",
                                           (limits.MAX_RECENT_SYMBOLS,))]


def add_recent(symbol: str, require, store=default_store) -> List[str]:
    """``symbol`` normalised by the route; ``require`` = assets.require (SymbolNotCovered → 404)."""
    require(symbol)
    with store._connect() as conn:
        store._begin(conn)
        seq = (conn.execute("SELECT COALESCE(MAX(seq), 0) FROM recent_symbols").fetchone()[0] or 0) + 1
        conn.execute("INSERT INTO recent_symbols(symbol, seq, used_at) VALUES(?, ?, ?) ON CONFLICT(symbol) DO UPDATE "
                     "SET seq = excluded.seq, used_at = excluded.used_at", (symbol, seq, store.utc_now_iso()))
        conn.execute("DELETE FROM recent_symbols WHERE symbol NOT IN (SELECT symbol FROM recent_symbols "
                     "ORDER BY seq DESC LIMIT ?)", (limits.MAX_RECENT_SYMBOLS,))
    return recent(store)


def clear_recent(store=default_store) -> List[str]:
    with store._connect() as conn:
        conn.execute("DELETE FROM recent_symbols")
    return []


# --- palette ----------------------------------------------------------------------------------------------

def _item(row: dict, match: str, is_recent: bool, member: Dict[str, List[str]]) -> dict:
    return {"symbol": row["symbol"], "name": row["name"], "asset_class": row["asset_class"], "kind": row["kind"],
            "exchange": row["exchange"], "tradable": bool(row["tradable"]), "match": match, "recent": is_recent,
            "watchlists": list(member.get(row["symbol"], []))}


def palette(q: Optional[str], limit: int, sources, *, store=default_store, now=None) -> dict:
    rec = recent(store)
    pos = {s: i for i, s in enumerate(rec)}
    member = watchlists.memberships(store)
    qs = (q or "").strip()
    items = []
    if not qs:
        for s in rec:
            row = store.asset_get(s)
            if row is not None:
                items.append(_item(row, "recent", True, member))
        items = items[:limit]
        refreshed = assets_mod.refreshed_at(store)
    else:
        res = assets_mod.search(qs, limits.ASSET_LIMIT_MAX, sources, store=store, now=now)
        rows = {it["symbol"]: it for it in res["items"]}
        for s in rec:                      # a recent symbol outside the top 50 still competes in its group
            if s not in rows:
                row = store.asset_get(s)
                if row is not None and assets_mod.rank(row, qs) is not None:
                    rows[s] = row
        scored = []
        for s, row in rows.items():
            r = assets_mod.rank(row, qs)
            if r is None:
                continue
            scored.append(((r, 0 if s in pos else 1, pos.get(s, 0), len(s), s), row, r))
        scored.sort(key=lambda x: x[0])
        items = [_item(row, MATCHES[r], row["symbol"] in pos, member) for _k, row, r in scored[:limit]]
        refreshed = res["refreshed_at"]
    return {"q": qs, "recent": rec, "items": items, "refreshed_at": refreshed, "source": "alpaca"}
