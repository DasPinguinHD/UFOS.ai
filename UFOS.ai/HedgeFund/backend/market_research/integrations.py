"""MR8 in-module integrations (SPEC §10 MR8, §13 "MR8 backend notes") — READ-ONLY views of other parts of
the HedgeFund module. BLOCKING (SQLite); the routes run it on the Research pool.

- Portfolio: the HedgeFund paper account (fund tables in ``newsroom.db``: positions, cash ledger, equity
  curve, engine flags). Research has NO order route; Buy/Sell is a hand-over to the existing manual paper
  order panel of the HedgeFund window (WPF only).
- Chart markers of a symbol: Form 4 insider buys/sells and the Newsroom economic calendar (FOMC decisions,
  FRED releases) — from the Newsroom's own stores, no external call. Earnings dates: the Newsroom has none →
  reported as unavailable.
- Backtest markers: the fills (entries/exits) of a Strategy Platform run card + its §11 attribution periods
  and drawdowns (``hedgefund.db`` ``backtest_runs``).
- "Use watchlist as universe": a watchlist as a validated Strategy Platform ``universe.symbols`` list (the
  strategy itself is edited in the Library — Research writes no strategy).

Read-only guarantee: other modules' databases are opened ONLY through ``ro_connect`` — SQLite URI
``mode=ro`` + ``PRAGMA query_only`` (a write raises), never through their store modules' write functions.
External text (insider names, release names) is plain text (``news_text.plain_text``), links http(s) only.
A missing source → ``IntegrationUnavailable`` (503 with the reason) or, for one marker kind,
``unavailable: {kind: reason}`` — never a stand-in value.

Part of UFOS.ai, a proof-of-concept portfolio project; research only, paper trading only.
"""
from __future__ import annotations

import ast
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, List, Optional
from urllib.request import pathname2url

from strategy_platform.core.errors import UserFacingError
from strategy_platform.core.instruments import instrument

from . import limits
from .bars import aggregate as agg
from .news_text import plain_text, safe_link

RUN_ID_RE = re.compile(r"^bt-[0-9a-f]{12}$")
MARKER_KINDS = ("insider", "calendar")
TIME_HINT_RE = re.compile(r"^(\d{1,2}):(\d{2}) ET$")
INSIDER_CODES = {"P": "buy", "S": "sell"}            # open-market purchase / sale (Form 4 codes)
PORTFOLIO_NOTE = ("Read-only view of the HedgeFund paper account. Research places no orders — Buy/Sell opens the "
                  "HedgeFund manual paper order panel (same confirmations and audit). Paper trading only.")
MARKS_NOTE = "Prices are the HedgeFund engine's last marks (written at its runs) — not live quotes."
EARNINGS_UNAVAILABLE = "The Newsroom has no earnings calendar — earnings dates are not shown."
UNIVERSE_NOTE = ("Paste/apply this list as universe.symbols in the Library's strategy editor — Research does not "
                 "change strategies. Mixed stock/crypto universes get the Strategy Platform's calendar warning.")


class IntegrationUnavailable(UserFacingError):
    """503 {error, kind: "IntegrationUnavailable"} — the source is missing / unreadable (reason given)."""


class IntegrationNotFound(UserFacingError):
    """404 {error}."""


# --- read-only access ------------------------------------------------------------------------------------

@contextmanager
def ro_connect(path: Optional[str], label: str):
    """A READ-ONLY connection (``mode=ro`` + ``query_only``); missing file → IntegrationUnavailable."""
    if not path or not os.path.exists(path):
        raise IntegrationUnavailable(f"The {label} is not available (its database file does not exist yet).")
    try:
        conn = sqlite3.connect(f"file:{pathname2url(os.path.abspath(path))}?mode=ro", uri=True, timeout=5,
                               check_same_thread=False)
    except sqlite3.Error as ex:
        raise IntegrationUnavailable(f"The {label} could not be opened read-only ({type(ex).__name__}).") from ex
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = 1")
        yield conn
    except sqlite3.Error as ex:
        raise IntegrationUnavailable(f"The {label} could not be read ({type(ex).__name__}: "
                                     f"{limits.clip(str(ex), 120)}).") from ex
    finally:
        conn.close()


def newsroom_db_path() -> str:
    from newsroom import db as ndb
    return ndb.db_path() or ndb.default_path()


def strategy_db_path() -> str:
    from strategy_platform.storage import db as sdb
    return sdb.db_path() or sdb.default_path()


def _tables(conn) -> set:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _f(v) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


# --- portfolio ---------------------------------------------------------------------------------------------

def portfolio(*, path: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    path = path or newsroom_db_path()
    with ro_connect(path, "HedgeFund paper account (newsroom.db)") as conn:
        have = _tables(conn)
        missing = [t for t in ("positions", "fund_cash_ledger", "equity_curve", "meta") if t not in have]
        if missing:
            raise IntegrationUnavailable("The HedgeFund paper account is not set up yet (tables " + ", ".join(missing)
                                         + " missing — start the HedgeFund backend once).")
        n, cash = conn.execute("SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM fund_cash_ledger").fetchone()
        rows = conn.execute("SELECT * FROM positions WHERE qty != 0 ORDER BY symbol").fetchall()
        eq = conn.execute("SELECT ts, equity, cash, drawdown_pct FROM equity_curve WHERE mode = 'paper' "
                          "ORDER BY ts DESC LIMIT 1").fetchone()
        meta = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM meta WHERE key IN "
                                                 "('fund.autopilot', 'fund.halted', 'fund.starting_capital')")}
    if not n:
        raise IntegrationUnavailable("The HedgeFund paper account has no ledger entries yet — start the HedgeFund "
                                     "engine once (it books the starting capital).")
    cols = rows[0].keys() if rows else []
    positions, long_mv, short_mv, upnl = [], 0.0, 0.0, 0.0
    complete = True
    for r in rows:
        qty, avg = float(r["qty"]), _f(r["avg_price"])
        last = _f(r["last_price"]) if "last_price" in cols else None
        mv = qty * last if last is not None else None
        pnl = (last - avg) * qty if last is not None and avg is not None else None
        pct = round((last / avg - 1.0) * 100.0 * (1 if qty > 0 else -1), 4) if pnl is not None and avg else None
        if mv is None:
            complete = False
        else:
            long_mv, short_mv = (long_mv + mv, short_mv) if qty > 0 else (long_mv, short_mv + mv)
        if pnl is not None:
            upnl += pnl
        positions.append({"symbol": limits.clip(r["symbol"], 20), "qty": qty, "side": "long" if qty > 0 else "short",
                          "avg_price": avg, "last_price": last, "market_value": mv, "unrealized_pnl": pnl,
                          "unrealized_pct": pct,
                          "realized_pnl": _f(r["realized_pnl"]) if "realized_pnl" in cols else None,
                          "opened_at": r["opened_at"] if "opened_at" in cols else None, "updated_at": r["updated_at"],
                          "reason": None if last is not None else "No engine mark for this position yet."})
    halted = None
    try:
        h = json.loads(meta["fund.halted"]) if meta.get("fund.halted") else None
        halted = bool(h.get("active")) if isinstance(h, dict) else False
    except ValueError:
        halted = None
    return {"source": "HedgeFund paper account", "read_only": True, "paper": True,
            "generated_at": _iso(now or datetime.now(timezone.utc)),
            "cash": round(float(cash), 2),
            "equity": ({"value": _f(eq["equity"]), "ts": eq["ts"], "drawdown_pct": _f(eq["drawdown_pct"]),
                        "reason": None} if eq else
                       {"value": None, "ts": None, "drawdown_pct": None,
                        "reason": "No equity snapshot yet — the engine writes one at each run."}),
            "starting_capital": _f(meta.get("fund.starting_capital")),
            "positions": positions,
            "totals": {"positions": len(positions), "long_mv": round(long_mv, 2), "short_mv": round(short_mv, 2),
                       "unrealized_pnl": round(upnl, 2), "complete": complete},
            "engine": {"autopilot": meta.get("fund.autopilot") == "1", "halted": halted},
            "marks_note": MARKS_NOTE, "note": PORTFOLIO_NOTE}


# --- symbol markers (Newsroom) -----------------------------------------------------------------------------

def _day_t(d: date) -> datetime:
    return agg.ny_midnight(d)


def _date(v) -> Optional[date]:
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _hint_t(d: date, hint) -> datetime:
    m = TIME_HINT_RE.match(str(hint or "").strip())
    if not m:
        return _day_t(d)
    return datetime.combine(d, time(int(m.group(1)), int(m.group(2))), tzinfo=agg.NY).astimezone(timezone.utc)


def fomc_meetings() -> List[tuple]:
    """FOMC_MEETINGS of ``newsroom/calendar_collector.py`` (the Newsroom's verified list) — read from the
    module source with ``ast.literal_eval`` (that module imports aiohttp/config; no duplication here)."""
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "newsroom",
                       "calendar_collector.py")
    with open(src, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "FOMC_MEETINGS" for t in node.targets):
            return [tuple(x) for x in ast.literal_eval(node.value)]
    raise ValueError("FOMC_MEETINGS not found")


def _marker(t: datetime, kind: str, label: str, side=None, detail=None, t_end: Optional[datetime] = None) -> dict:
    return {"t": _iso(t), "t_end": _iso(t_end) if t_end else None, "kind": kind,
            "label": plain_text(label, limits.MAX_MARKER_LABEL_CHARS) or kind, "side": side, "detail": detail or {}}


def _insider(conn, sym: str, start: datetime, end: datetime) -> List[dict]:
    out = []
    rows = conn.execute("SELECT accession, filed_at, payload_json FROM insider_filings WHERE issuer_ticker = ? "
                        "ORDER BY filed_at", (sym,)).fetchall()
    for r in rows:
        try:
            ev = json.loads(r["payload_json"])
        except ValueError:
            continue
        d = ev.get("details") if isinstance(ev, dict) else None
        if not isinstance(d, dict):
            continue
        owner = plain_text(d.get("owner_name"), 80) or "Unknown insider"
        roles = [plain_text(x, 40) for x in (d.get("owner_roles") or []) if isinstance(x, str)][:4]
        for tx in d.get("transactions") or []:
            if not isinstance(tx, dict) or tx.get("code") not in INSIDER_CODES or tx.get("derivative"):
                continue
            day = _date(tx.get("date"))
            if day is None:
                continue
            t = _day_t(day)
            if not start <= t < end:
                continue
            side = INSIDER_CODES[tx["code"]]
            shares, price, value = _f(tx.get("shares")), _f(tx.get("price")), _f(tx.get("value_usd"))
            amount = (f" — {shares:,.0f} sh" if shares else "") + (f" @ ${price:,.2f}" if price else "")
            label = f"Insider {side}: {owner}" + (f" ({', '.join(x for x in roles if x)})" if roles else "") + amount
            out.append(_marker(t, "insider", label, side, {
                "owner": owner, "roles": [x for x in roles if x], "shares": shares, "price": price, "value_usd": value,
                "filed_at": r["filed_at"], "accession": limits.clip(r["accession"], 40),
                "url": safe_link(ev.get("url")), "plan_10b5_1": bool(d.get("plan_10b5_1")),
                "source": "SEC EDGAR Form 4 (Newsroom)"}))
    return out


def _calendar(conn, start: datetime, end: datetime, unavailable: dict) -> List[dict]:
    out, fomc_days = [], set()
    try:
        for first, second, sep in fomc_meetings():
            d = _date(second)
            t = _hint_t(d, "14:00 ET") if d else None
            if t is None or not start <= t < end:
                continue
            fomc_days.add(second)
            out.append(_marker(t, "fomc", "FOMC rate decision" + (" + projections" if sep else ""), None,
                               {"meeting_start": first, "sep": bool(sep), "importance": "high",
                                "source": "Federal Reserve (Newsroom list)"}))
    except (OSError, ValueError, SyntaxError) as ex:
        unavailable["fomc"] = f"The Newsroom's FOMC list could not be read ({type(ex).__name__})."
    snap = conn.execute("SELECT payload_json, fetched_at FROM calendar_snapshot WHERE id = 1").fetchone() \
        if "calendar_snapshot" in _tables(conn) else None
    if snap is None:
        unavailable["releases"] = ("The Newsroom has no economic calendar snapshot yet (FRED key missing or the first "
                                   "poll is pending) — release dates are not shown.")
        return out
    try:
        events = (json.loads(snap["payload_json"]) or {}).get("events") or []
    except ValueError:
        unavailable["releases"] = "The Newsroom's calendar snapshot could not be read."
        return out
    for e in events:
        if not isinstance(e, dict):
            continue
        d = _date(e.get("date"))
        if d is None or (e.get("kind") == "fomc" and e.get("date") in fomc_days):
            continue
        t = _hint_t(d, e.get("time_hint"))
        if not start <= t < end:
            continue
        kind = "fomc" if e.get("kind") == "fomc" else "release"
        out.append(_marker(t, kind, str(e.get("name") or "Release"), None, {
            "importance": e.get("importance") if e.get("importance") in ("high", "medium") else None,
            "series_id": plain_text(e.get("series_id"), 20), "why": plain_text(e.get("why"), 300),
            "url": safe_link(e.get("release_link")), "source": "FRED release calendar (Newsroom)"}))
    return out


def symbol_markers(symbol: str, start: datetime, end: datetime, kinds: List[str], *, path: Optional[str] = None,
                   insider_days: Optional[int] = None) -> dict:
    inst = instrument(symbol)
    unavailable: Dict[str, str] = {"earnings": EARNINGS_UNAVAILABLE}
    items: List[dict] = []
    path = path or newsroom_db_path()
    with ro_connect(path, "Financial Newsroom store (newsroom.db)") as conn:
        if "insider" in kinds:
            if inst.asset_class == "crypto":
                unavailable["insider"] = "Form 4 insider filings exist only for US-listed companies."
            elif "insider_filings" not in _tables(conn):
                unavailable["insider"] = "The Newsroom has no insider filings table yet."
            else:
                items += _insider(conn, inst.symbol, start, end)
        if "calendar" in kinds:
            items += _calendar(conn, start, end, unavailable)
    if insider_days is None:
        try:
            from newsroom import db as ndb
            insider_days = ndb.INSIDER_RETENTION_DAYS
        except ImportError:   # pragma: no cover
            insider_days = None
    items.sort(key=lambda m: (m["t"], m["kind"]))
    truncated = len(items) > limits.MAX_MARKERS
    return {"symbol": inst.symbol, "from": _iso(start), "to": _iso(end), "kinds": kinds,
            "items": items[:limits.MAX_MARKERS], "truncated": truncated, "unavailable": unavailable,
            "coverage": {"insider": f"Form 4 filings the Newsroom keeps (last {insider_days} days of filings)"
                                    if insider_days else "Form 4 filings the Newsroom keeps",
                         "calendar": "FOMC decisions from the Newsroom's list; FRED release dates of its latest "
                                     "calendar snapshot (upcoming window only)"}}


# --- backtest markers (Strategy Platform run card, read-only) ----------------------------------------------

def _period_t(v, crypto: bool) -> Optional[datetime]:
    d = _date(v)
    if d is None:
        return None
    return agg.utc_midnight(d) if crypto else _day_t(d)


def backtest_markers(run_id: str, symbol: str, start: Optional[datetime], end: Optional[datetime], *,
                     path: Optional[str] = None) -> dict:
    if not RUN_ID_RE.match(str(run_id)):
        raise IntegrationNotFound("Backtest run not found.")
    inst = instrument(symbol)
    crypto = inst.asset_class == "crypto"
    with ro_connect(path or strategy_db_path(), "Strategy Platform store (hedgefund.db)") as conn:
        if "backtest_runs" not in _tables(conn):
            raise IntegrationUnavailable("The Strategy Platform store has no run cards yet.")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(backtest_runs)")}
        sel = ["id", "status", "strategy_id", "version", "trades_json", "range_json"] + \
              [c for c in ("kind", "attribution_json") if c in cols]
        r = conn.execute("SELECT " + ", ".join(sel) + " FROM backtest_runs WHERE id = ?", (run_id,)).fetchone()
    if r is None:
        raise IntegrationNotFound("Backtest run not found.")
    if r["status"] != "done":
        raise IntegrationUnavailable(f"The run is {r['status']} — markers exist only for finished runs.")
    unavailable: Dict[str, str] = {}
    items: List[dict] = []

    def keep(t: Optional[datetime]) -> bool:
        return t is not None and (start is None or t >= start) and (end is None or t < end)
    try:
        trades = json.loads(r["trades_json"] or "null") or {}
    except ValueError:
        trades = {}
    fills = trades.get("fills_by_symbol")
    if fills is None:
        unavailable["fills"] = ("This run card was created before entries/exits were stored on cards (Strategy "
                                "Platform, 2026-10-17) — run the backtest again.")
    else:
        for row in fills.get(inst.symbol) or []:
            try:
                ts, side, qty, price = row[0], str(row[1]), float(row[2]), float(row[3])
                t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            except (TypeError, ValueError, IndexError):
                continue
            t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
            if side not in ("buy", "sell", "short", "cover") or not keep(t):
                continue
            entry = side in ("buy", "short")
            items.append(_marker(t, "fill", f"{side.capitalize()} {qty:,.8g} @ {price:,.2f}", side,
                                 {"qty": qty, "price": price, "entry": entry}))
        if trades.get("fills_truncated"):
            unavailable["fills_after_cap"] = ("The run had more fills than a card stores — later fills are not "
                                              "shown.")
    attr = None
    if "attribution_json" in r.keys():
        try:
            attr = json.loads(r["attribution_json"] or "null")
        except ValueError:
            attr = None
    periods = (attr or {}).get("periods") if isinstance(attr, dict) else None
    if not isinstance(periods, dict) or "unavailable" in (periods or {}):
        unavailable["periods"] = ((periods or {}).get("unavailable") if isinstance(periods, dict) else None) or \
            "No attribution on this run card (runs created before attribution existed) — run the backtest again."
    else:
        for kind in ("best", "worst"):
            for p in periods.get(kind) or []:
                t0, t1 = _period_t(p.get("start"), crypto), _period_t(p.get("end"), crypto)
                if t0 is None or not (keep(t0) or keep(t1)):
                    continue
                sr, br = _f(p.get("strategy_return")), _f(p.get("benchmark_return"))
                items.append(_marker(t0, f"period_{kind}", f"{'Best' if kind == 'best' else 'Worst'} 63-session window"
                                     + (f": {sr * 100:+.1f} %" if sr is not None else ""), None,
                                     {"strategy_return": sr, "benchmark_return": br, "excess": _f(p.get("excess"))},
                                     t_end=t1))
        for dd in periods.get("drawdowns") or []:
            t0 = _period_t(dd.get("start"), crypto)
            tt, tr_ = _period_t(dd.get("trough"), crypto), _period_t(dd.get("recovery"), crypto)
            if t0 is None or not (keep(t0) or keep(tt) or keep(tr_)):
                continue
            depth = _f(dd.get("depth"))
            contrib = [{"symbol": limits.clip(c.get("symbol"), 20), "pnl": _f(c.get("pnl"))}
                       for c in dd.get("contributors") or [] if isinstance(c, dict)][:3]
            items.append(_marker(t0, "drawdown", "Drawdown" + (f" {depth * 100:.1f} %" if depth is not None else "")
                                 + ("" if tr_ else " (not recovered)"), None,
                                 {"trough": _iso(tt) if tt else None, "depth": depth, "recovered": tr_ is not None,
                                  "contributors": contrib,
                                  "symbol_contributed": any(c["symbol"] == inst.symbol for c in contrib)},
                                 t_end=tr_))
    items.sort(key=lambda m: (m["t"], m["kind"]))
    return {"run_id": run_id, "strategy_id": limits.clip(r["strategy_id"], 64), "version": r["version"],
            "symbol": inst.symbol, "items": items[:limits.MAX_MARKERS], "truncated": len(items) > limits.MAX_MARKERS,
            "unavailable": unavailable, "read_only": True}


# --- watchlist → Strategy Platform universe ------------------------------------------------------------------

def universe(wlist: dict) -> dict:
    from strategy_platform.strategy import limits as sp_limits
    symbols, excluded, seen = [], [], set()
    for it in wlist.get("items") or []:
        sym = it.get("symbol")
        try:
            sym = instrument(sym).symbol
        except (ValueError, TypeError):
            excluded.append({"symbol": limits.clip(sym, 20), "reason": "Not a supported symbol."})
            continue
        if it.get("covered") is False:
            excluded.append({"symbol": sym, "reason": "No longer in Alpaca's asset list."})
            continue
        if sym in seen:
            continue
        seen.add(sym)
        symbols.append(sym)
    if len(symbols) > sp_limits.MAX_SYMBOLS:
        excluded += [{"symbol": s, "reason": f"Beyond the Strategy Platform limit of {sp_limits.MAX_SYMBOLS} symbols."}
                     for s in symbols[sp_limits.MAX_SYMBOLS:]]
        symbols = symbols[:sp_limits.MAX_SYMBOLS]
    classes = sorted({instrument(s).asset_class for s in symbols})
    return {"id": wlist["id"], "name": wlist["name"], "universe": {"symbols": symbols} if symbols else None,
            "count": len(symbols), "asset_classes": classes, "excluded": excluded,
            "max_symbols": sp_limits.MAX_SYMBOLS,
            "reason": None if symbols else "The watchlist has no usable symbols — a universe needs at least one.",
            "note": UNIVERSE_NOTE}
