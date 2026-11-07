"""Stage 11 Overview API (SP §26): ``/v2/overview/{markets,risk,activity}`` — READ-ONLY dashboards behind the
backend's token check like every ``/v2`` route. Nothing here trades, writes, or touches the client heartbeat.

Rules (contract ``UFOS.ai/docs/tasks/STAGE11_OVERVIEW_TASK.md`` §1): every value has exactly ONE source; a value that
cannot be read is ``available: false`` / ``null`` WITH the reason — no fallback, no stand-in, no second source, no
interpolation; one failing item / source never fails the others.

* ``GET /v2/overview/markets`` — SPY, QQQ, IWM, GLD, BTC (Alpaca through the Market Research data layer, Research
  rate budget, no own client), US 10Y (FRED DGS10) and EUR/USD (FRED DEXUSEU) from the Newsroom FRED store, VIX from
  the OS2 archive (SPY surface ``vix_structure.vix``). Server-side cache 60 s (at most one Alpaca round per minute).
* ``GET /v2/overview/risk`` — VaR 99 % / ES 97.5 % (1 day, historical simulation), exposure, max drawdown, vol 20 d
  of the ACTIVE paper account (old engine until the legacy account is migrated, then the platform); maths in
  ``portfolio/overview_risk.py``.
* ``GET /v2/overview/activity?limit=20`` — merged feed (newest first) of OMS orders, fills, monitoring alerts,
  finished loops, options-archive runs (failed / partial + the newest ok), Council runs and old-engine events, with
  a per-source status.

Part of UFOS.ai, a proof-of-concept portfolio project; paper trading only, research only.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Mapping, Optional, Tuple

from .trading_routes import _bad

logger = logging.getLogger(__name__)

MARKETS_CACHE_SECONDS = 60
EQUITY_SYMBOLS = (("SPY", "SPY"), ("QQQ", "QQQ"), ("IWM", "IWM"), ("GLD", "Gold (GLD)"))
CRYPTO_KEY, CRYPTO_SYMBOL, CRYPTO_LABEL = "BTC", "BTC-USD", "BTC"
FRED_ITEMS = (("US10Y", "US 10Y", "DGS10", "bp"), ("EURUSD", "EUR/USD", "DEXUSEU", "pct"))
FRED_SERIES_POINTS = 30
EQUITY_BARS_LIMIT = 80          # one regular session = 78 five-minute bars
CRYPTO_BARS_LIMIT = 100         # 24 h = 96 fifteen-minute bars (+ the 24-h reference)
VIX_HISTORY_DAYS = 45            # ≈ 30 trading days of distinct VIX closes
VIX_SERIES_POINTS = 30
ACTIVITY_LIMIT_DEFAULT, ACTIVITY_LIMIT_MAX = 20, 100
TITLE_MAX, DETAIL_MAX = 160, 300
LINKS = {"orders": "trading/orders", "monitoring": "trading/monitoring", "loop": "strategies/loop",
         "legacy": "trading/legacy", "archive": "settings/options-archive"}


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return None if dt is None else _utc(dt).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(ts) -> Optional[datetime]:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return _utc(ts)
    try:
        return _utc(datetime.fromisoformat(str(ts).strip().replace("Z", "+00:00")))
    except ValueError:
        return None


class SourceUnavailable(Exception):
    """Raised by a reader with a message written for the UI (safe: no path, no SQL text)."""


class SourceRetired(Exception):
    """Raised by the Council / old-engine readers while retired (SP §22.15): the source state reads "retired" (not an
    error); ``items`` = the history still listed (past Council runs)."""

    def __init__(self, items: Optional[List[dict]] = None):
        super().__init__("retired")
        self.items = list(items or [])


def _retired() -> bool:
    """The retirement marker (SP §22.15); raises SourceUnavailable when unreadable (said, never guessed)."""
    from ..council import retirement
    try:
        return retirement.is_retired_if_stored()
    except retirement.RetirementUnreadable as ex:
        raise SourceUnavailable(f"the retirement state is unreadable ({ex})") from ex


def _why(ex: BaseException) -> str:
    """A safe, short reason: a UserFacingError's ``user_message`` or a SourceUnavailable's own text; anything else
    → only the exception type + a fixed hint (no paths / SQLite text in the UI; the full text goes to backend.log)."""
    msg = getattr(ex, "user_message", None)
    if msg:
        return str(msg)[:300]
    if isinstance(ex, SourceUnavailable):
        return str(ex)[:300]
    logger.warning("overview: %s: %s", type(ex).__name__, str(ex)[:500])
    return f"{type(ex).__name__} (details in backend.log)"


def _text(v, n: int) -> Optional[str]:
    if v is None:
        return None
    s = " ".join(str(v).split())
    return s[:n]


def _finite(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _item(key: str, label: str, source: str, change_unit: str, session: str) -> dict:
    return {"key": key, "label": label, "available": False, "reason": None, "value": None, "change": None,
            "change_unit": change_unit, "series": [], "series_reason": None, "source": source, "as_of": None,
            "age_minutes": None, "session": session, "note": None}


def _age(now: datetime, as_of: Optional[datetime]) -> Optional[float]:
    return None if as_of is None else round((now - as_of).total_seconds() / 60.0, 1)


# --- market items ---------------------------------------------------------------------------------------------------

def equity_item(data, key: str, label: str, symbol: str, now: datetime) -> dict:
    """One ETF from the Market Research data layer (blocking; call inside ``research_priority``)."""
    from ..core import calendars
    it = _item(key, label, "Alpaca", "pct", "open" if calendars.is_open(now) else "closed")
    try:
        snap = data.snapshot(symbol)
    except Exception as ex:      # noqa: BLE001 — the item says why; the others still return
        it["reason"] = f"Alpaca snapshot failed: {_why(ex)}"
        return it
    last, cp = snap.get("last") or {}, snap.get("change_pct") or {}
    if last.get("value") is None:
        it["reason"] = last.get("reason") or "no last trade"
        return it
    as_of = _parse(last.get("as_of"))
    it.update(available=True, value=last["value"], change=cp.get("value"), as_of=_iso(as_of),
              age_minutes=_age(now, as_of))
    prev = snap.get("prev_close") or {}
    note = f"last: {last.get('feed_label')}; change vs previous close ({prev.get('feed_label')})"
    if cp.get("value") is None:
        note += f"; change not available ({cp.get('reason') or 'no previous close'})"
    it["note"] = note
    try:
        bars = (data.bars(symbol, "5m", "rth", None, EQUITY_BARS_LIMIT) or {}).get("bars") or []
    except Exception as ex:      # noqa: BLE001
        it["series_reason"] = f"Alpaca 5-min bars failed: {_why(ex)}"
        return it
    if not bars:
        it["series_reason"] = "no 5-min bars of the regular session"
        return it
    day = _parse(bars[-1]["t"]).astimezone(calendars.NY).date()
    it["series"] = [{"t": b["t"], "v": b["c"]} for b in bars if _parse(b["t"]).astimezone(calendars.NY).date() == day]
    return it


def crypto_item(data, now: datetime) -> dict:
    it = _item(CRYPTO_KEY, CRYPTO_LABEL, "Alpaca", "pct", "open")
    try:
        snap = data.snapshot(CRYPTO_SYMBOL)
    except Exception as ex:      # noqa: BLE001
        it["reason"] = f"Alpaca crypto snapshot failed: {_why(ex)}"
        return it
    last = snap.get("last") or {}
    if last.get("value") is None:
        it["reason"] = last.get("reason") or "no last crypto trade"
        return it
    as_of = _parse(last.get("as_of"))
    it.update(available=True, value=last["value"], as_of=_iso(as_of), age_minutes=_age(now, as_of))
    try:
        bars = (data.bars(CRYPTO_SYMBOL, "15m", None, None, CRYPTO_BARS_LIMIT) or {}).get("bars") or []
    except Exception as ex:      # noqa: BLE001
        it["series_reason"] = f"Alpaca crypto 15-min bars failed: {_why(ex)}"
        it["note"] = "change over 24 h not available (the 15-min bars could not be read)"
        return it
    cut = now - timedelta(hours=24)
    it["series"] = [{"t": b["t"], "v": b["c"]} for b in bars if _parse(b["t"]) >= cut]
    if not it["series"]:
        it["series_reason"] = "no 15-min crypto bars in the last 24 h"
    ref = None
    for b in bars:
        end = _parse(b["t"]) + timedelta(minutes=15)
        if cut - timedelta(minutes=15) < end <= cut:        # the bar ending within 15 min before now − 24 h
            ref = b
    if ref is None or not _finite(ref.get("c")):
        it["note"] = ("change over 24 h not available (no 15-min close ending within 15 min before "
                      f"{_iso(cut)})")
        return it
    ref_end = _parse(ref["t"]) + timedelta(minutes=15)
    it["change"] = (float(last["value"]) / float(ref["c"]) - 1.0) * 100.0
    it["note"] = (f"change over 24 h: last trade ({last.get('feed_label')}) vs the 15-min close ending "
                  f"{_iso(ref_end)}")
    return it


def fred_item(points_fn: Callable[[str], list], key: str, label: str, series_id: str, change_unit: str) -> dict:
    it = _item(key, label, "FRED", change_unit, "daily")
    try:
        pts = list(points_fn(series_id) or [])
    except Exception as ex:      # noqa: BLE001
        it["reason"] = f"Newsroom FRED store unreadable: {_why(ex)}"
        return it
    pts = [p for p in pts if _finite(p.get("value")) is not None and p.get("date")]
    if not pts:
        it["reason"] = (f"no stored FRED observations of {series_id} yet (FRED key missing, first poll pending or "
                        "the fetch failed)")
        return it
    last = pts[-1]
    it.update(available=True, value=float(last["value"]), as_of=last["date"],
              series=[{"t": p["date"], "v": float(p["value"])} for p in pts[-FRED_SERIES_POINTS:]])
    if len(pts) < 2:
        it["note"] = f"FRED {series_id} observation {last['date']}; change not available (no previous observation)"
        return it
    prev = pts[-2]
    if change_unit == "bp":
        it["change"] = (float(last["value"]) - float(prev["value"])) * 100.0
    elif float(prev["value"]) != 0:
        it["change"] = (float(last["value"]) / float(prev["value"]) - 1.0) * 100.0
    it["note"] = f"FRED {series_id} observation {last['date']}; change vs the previous observation {prev['date']}"
    return it


def vix_item(ctx, conn, now: datetime) -> dict:
    """VIX from the OS2 archive (SPY surface ``vix_structure.vix`` = the FRED VIXCLS daily close the archive held at
    that slot), treated as a DAILY series: ``as_of`` = the close date, ``slot_age_minutes`` = the slot's age; change vs
    the newest archived slot whose VIX date is EARLIER than the current one (two slots of the same date are never
    compared); series = one point per distinct VIX date (last ≤ 30). ``conn`` = the read-only options_structure.db
    connection (None = no OS2 results)."""
    from ..options_structure import store
    from ..options_structure.config import MODEL_VERSION
    it = _item("VIX", "VIX", "Archive", "pt", "daily")
    it["slot_age_minutes"] = None
    if conn is None:
        it["reason"] = "no OS2 results yet (options_structure.db does not exist — the OS2 job has not run)"
        return it
    now_ms = int(now.timestamp() * 1000)
    row = store.latest(conn, "SPY", "surface", now_ms, MODEL_VERSION, ctx[2])
    if row is None:
        it["reason"] = "no OS2 SPY surface available under the current assumptions"
        return it
    slot = datetime.fromtimestamp(row["valid_for_ms"] / 1000, timezone.utc)
    prov = bool(row["provisional"] or ctx[3])
    it["provisional"] = prov
    it["slot_age_minutes"] = _age(now, slot)

    def vix_of(r) -> Optional[Tuple[str, float]]:
        x = ((r["payload"] or {}).get("vix_structure") or {}).get("vix")
        if not isinstance(x, dict) or not isinstance(x.get("date"), str) or _finite(x.get("value")) is None:
            return None
        return x["date"][:10], float(x["value"])

    cur = vix_of(row)
    if cur is None:
        vs = (row["payload"] or {}).get("vix_structure") or {}
        it["reason"] = f"the newest OS2 SPY slot ({_iso(slot)}) has no VIX: {vs.get('reason') or 'not archived'}"
        return it
    it.update(available=True, value=cur[1], as_of=cur[0])
    rows = store.history(conn, "SPY", "surface", row["valid_for_ms"] - VIX_HISTORY_DAYS * 86400000, now_ms,
                         MODEL_VERSION, ctx[2])
    by_date: dict = {}
    for r in rows:                                    # oldest first → the newest slot of each date wins
        v = vix_of(r)
        if v is not None and v[0] <= cur[0]:
            by_date[v[0]] = (v[1], r["valid_for_ms"])
    by_date[cur[0]] = (cur[1], row["valid_for_ms"])
    dates = sorted(by_date)[-VIX_SERIES_POINTS:]
    it["series"] = [{"t": d, "v": by_date[d][0]} for d in dates]
    note = f"FRED VIXCLS close {cur[0]} as archived in the OS2 SPY slot {_iso(slot)}" + (" · provisional" if prov else "")
    earlier = [d for d in by_date if d < cur[0]]
    if not earlier:
        it["note"] = note + (f"; change not available (no archived slot with an earlier VIX date in the last "
                             f"{VIX_HISTORY_DAYS} days)")
        return it
    d0 = max(earlier)
    it["change"] = cur[1] - by_date[d0][0]
    it["note"] = note + (f"; change vs the close {d0} (archived slot "
                         f"{_iso(datetime.fromtimestamp(by_date[d0][1] / 1000, timezone.utc))})")
    return it


# --- activity sources -------------------------------------------------------------------------------------------------

def _act(ts, kind: str, title, detail, severity: str, link: str) -> Optional[dict]:
    dt = _parse(ts)
    if dt is None:
        return None
    return {"ts": _iso(dt), "kind": kind, "title": _text(title, TITLE_MAX) or kind,
            "detail": _text(detail, DETAIL_MAX), "severity": severity, "link": link}


def _num(v, fmt="{:,.4g}") -> str:
    f = _finite(v)
    return "?" if f is None else fmt.format(f)


def src_oms_orders(limit: int) -> List[dict]:
    """OMS order events: placed (the first event; a first event straight to rejected / cancelled / expired is labelled
    with that state), cancelled, rejected, expired; filled / partially filled only for broker accounts (internal
    paper fills come from the fills source — each fill is listed once). Filtered in SQL, so ``limit`` is exact."""
    from ..execution import accounts
    from ..storage import db
    brokers = tuple(accounts.BROKER_IDS)
    q = ("SELECT e.id, e.ts, e.from_state, e.to_state, o.account_id, o.symbol, o.side, o.qty, o.filled_qty, "
         "o.avg_fill_price, o.reject_reason FROM oms_order_events e JOIN oms_orders o ON "
         "o.client_order_id = e.client_order_id WHERE "
         "((e.from_state IS NULL AND e.to_state NOT IN ('filled', 'partially_filled')) "
         " OR (e.from_state IS NOT e.to_state AND e.to_state IN ('cancelled', 'rejected', 'expired')) "
         f" OR (e.from_state IS NOT e.to_state AND e.to_state IN ('filled', 'partially_filled') "
         f"     AND o.account_id IN ({','.join('?' * len(brokers))}))) "
         "ORDER BY e.id DESC LIMIT ?")
    with db._connect() as conn:
        rows = conn.execute(q, (*brokers, int(limit))).fetchall()
    out = []
    for r in rows:
        to = r["to_state"]
        what = "placed" if (r["from_state"] is None and to in ("new", "submitted", "accepted")) else to.replace("_", " ")
        detail = f"{r['account_id']} · {r['side']} {_num(r['qty'])} {r['symbol']}"
        if to in ("filled", "partially_filled"):
            detail += f" · {_num(r['filled_qty'])} @ {_num(r['avg_fill_price'])}"
        if to == "rejected" and r["reject_reason"]:
            detail += f" · {r['reject_reason']}"
        a = _act(r["ts"], "order", f"Order {what}: {r['side']} {r['symbol']}", detail,
                 "warning" if to == "rejected" else "info", LINKS["orders"])
        if a:
            out.append(a)
    return out


def src_fills(limit: int) -> List[dict]:
    """Fills booked by the internal paper accounts (their ledgers; broker-account fills are OMS order events)."""
    from ..execution import accounts
    from ..execution.brokers.paper import _ORDER_REF
    from ..execution.paper_store import PlatformPaperStore
    out = []
    for row in accounts.rows():
        if accounts.is_broker(row["id"]):
            continue
        st = PlatformPaperStore(row["id"])
        for e in st.cash_entries(limit, "fill"):
            m = _ORDER_REF.search(e.get("note") or "")
            o = st.get_order(int(m.group(1))) if m else None
            side = (o or {}).get("side") or "fill"
            detail = f"{row['id']} · {side} {_num((o or {}).get('qty'))} {e.get('symbol')} @ " \
                     f"{_num((o or {}).get('fill_price'))} · notional {_num(abs(_finite(e.get('amount')) or 0), '{:,.2f}')}"
            a = _act(e.get("ts"), "fill", f"Fill: {side} {e.get('symbol')}", detail, "info", LINKS["orders"])
            if a:
                out.append(a)
    return out


def src_alerts(limit: int) -> List[dict]:
    from ..monitoring import alerts
    sev = {"critical": "error", "warning": "warning"}
    out = []
    for al in alerts.list_alerts(0, limit)["alerts"]:
        a = _act(al.get("ts"), "alert", al.get("title"), al.get("body"), sev.get(al.get("severity"), "info"),
                 LINKS["monitoring"])
        if a:
            out.append(a)
    return out


def src_loops(limit: int) -> List[dict]:
    """Finished loops (done / failed / cancelled); ts = the terminal status event."""
    from ..discovery import loop
    out = []
    for lp in loop.list_loops(min(limit, 20))["loops"]:
        if lp.get("status") not in loop.TERMINAL:
            continue
        ends = [e for e in loop._events(lp["id"]) if e.get("event") in loop.TERMINAL]
        if not ends:
            continue
        st = lp["status"]
        detail = f"{lp.get('finalists') or 0} finalist(s) · {lp.get('generations_done') or 0} generation(s)"
        if lp.get("status_reason"):
            detail += f" · {lp['status_reason']}"
        a = _act(ends[-1].get("created_at"), "loop", f"Loop {st}: {lp['id']}", detail,
                 "error" if st == "failed" else "info", LINKS["loop"])
        if a:
            out.append(a)
    return out


def src_archive(limit: int, data_dir) -> List[dict]:
    """Options-archive runs: failed / partial, plus the newest ok one."""
    from ..options_archive import config as oconfig
    from ..options_archive import db as odb
    from ..options_archive import report
    conn = odb.open_ro(oconfig.paths(data_dir)["db"])
    if conn is None:
        raise SourceUnavailable("the options archive has no database yet")
    try:
        runs = report.runs(conn, 200)
    finally:
        conn.close()
    out, ok_seen = [], False
    for r in runs:
        st = r.get("status")
        if st == "ok":
            if ok_seen:
                continue
            ok_seen = True
        elif st not in ("failed", "partial"):
            continue
        detail = f"{r.get('kind')} slot {r.get('slot_ny')}"
        if r.get("error"):
            detail += f" · {r['error']}"
        a = _act(r.get("finished_at") or r.get("started_at"), "archive", f"Options archive run {st}", detail,
                 {"failed": "error", "partial": "warning"}.get(st, "info"), LINKS["archive"])
        if a:
            out.append(a)
        if len(out) >= limit:
            break
    return out


def src_council(limit: int) -> List[dict]:
    from ..council import store
    out = []
    for r in store.runs(min(limit, 200)):
        st = r.get("status") or "running"
        detail = f"trigger {r.get('trigger')}"
        if r.get("cost_usd") is not None:
            detail += f" · AI cost ${_num(r['cost_usd'], '{:,.4f}')}"
        a = _act(r.get("finished_at") or r.get("started_at"), "council", f"Council run #{r.get('id')} {st}", detail,
                 "warning" if st in ("failed", "error") else "info", LINKS["legacy"])
        if a:
            out.append(a)
    try:
        retired = _retired()
    except SourceUnavailable:
        retired = False                  # the past runs do not depend on the marker; listed as before
    if retired:
        raise SourceRetired(out)         # past runs stay listed, the source reads "retired"
    return out


def src_old_engine(limit: int) -> List[dict]:
    """Old-engine events through the sanctioned bridge ``execution.control.legacy_engine()`` (the engine main.py
    registers; its ``store`` = the old engine's own store) — no ``fund`` import from the platform (SP §22.7 guard).
    Retired (SP §22.15) → the source reads "retired" (no items), not "not running"."""
    from ..execution import control
    if _retired():
        raise SourceRetired()
    eng = control.legacy_engine()
    if eng is None:
        raise SourceUnavailable("the old engine is not running in this backend")
    reader = getattr(eng.store, "events_latest", None)
    if reader is None:
        raise SourceUnavailable("the old-engine store has no event reader")
    rows = reader(limit)
    if rows is None:
        raise SourceUnavailable("the old-engine store is disabled or unreadable")
    out = []
    for e in rows:
        title = e.get("title")
        if e.get("symbol"):
            title = f"{title} ({e['symbol']})"
        a = _act(e.get("ts"), f"engine:{e.get('kind')}", title, e.get("body"),
                 "warning" if e.get("kind") == "halt" else "info", LINKS["legacy"])
        if a:
            out.append(a)
    return out


# --- risk inputs ------------------------------------------------------------------------------------------------------

def active_account_set() -> dict:
    """THE active paper account set of the Overview (SP §26.2; lead decision 2026-11-06) — the only place it is
    decided. ``{"mode": "legacy"|"platform", "legacy_status", "engine", "accounts": [Account…], "excluded": [ids]}``.

    * ``legacy_account.status`` (``execution/migration.legacy_state``) not ``migrated`` → ``legacy``: the old engine
      (reached through the bridge ``execution.control.legacy_engine()``; ``engine`` None when not registered).
    * migrated → ``platform``: the platform's INTERNAL paper ledger accounts (USD; ``execution.accounts.INTERNAL_IDS``)
      only. ASSUMPTION (user may revisit): broker mirror accounts — alpaca-paper, binance-demo, kraken-demo, any
      broker-backed account — and anything not marked paper are EXCLUDED: a mirror is a different book (the broker's)
      and would double-count / mix currencies."""
    from ..execution import accounts, control, migration
    eng = control.legacy_engine()
    legacy = migration.legacy_state(eng)
    if legacy.get("status") != "migrated":
        return {"mode": "legacy", "legacy_status": legacy.get("status"), "engine": eng, "accounts": [],
                "excluded": []}
    inc, exc = [], []
    for a in accounts.all_accounts():
        internal = (a.id in accounts.INTERNAL_IDS and not accounts.is_broker(a.id)
                    and not getattr(a, "is_broker", False)
                    and getattr(a, "broker_name", "internal_paper") == "internal_paper"
                    and getattr(a, "paper", True) is not False)
        (inc if internal else exc).append(a)
    return {"mode": "platform", "legacy_status": legacy.get("status"), "engine": None, "accounts": inc,
            "excluded": [a.id for a in exc]}


def _summary(ids, n, summed, as_of, vals=None, reason=None) -> dict:
    ok = reason is None and vals is not None
    vals = vals or {}
    return {"ids": list(ids), "n": n, "available": ok, "equity": vals.get("equity") if ok else None,
            "day_pnl": vals.get("day_pnl") if ok else None, "total_pnl": vals.get("total_pnl") if ok else None,
            "as_of": as_of, "summed": bool(summed), "currency": "USD", "reason": None if ok else reason}


def _sum_fields(views_by_id: dict) -> Tuple[Optional[dict], Optional[str]]:
    """Point-in-time USD amounts summed over the explicit set; a field missing on any account → whole summary
    unavailable naming it (no partial sums)."""
    out, missing = {}, []
    for f in ("equity", "day_pnl", "total_pnl"):
        tot = 0.0
        for aid, v in views_by_id.items():
            x = _finite(v.get(f))
            if x is None:
                missing.append(f"{aid}: {f} not available")
                break
            tot += x
        out[f] = tot
    return (None, "; ".join(missing)) if missing else (out, None)


def risk_inputs(now: Optional[datetime] = None) -> dict:
    """The active paper account set's equity series + state + point-in-time summary (blocking). {account,
    account_ids, excluded_ids, points, equity, long_mv, short_mv, exposure_reason, series_reason, summary}."""
    now = now or datetime.now(timezone.utc)
    aset = active_account_set()
    out = {"account": aset["mode"], "account_ids": [], "excluded_ids": aset["excluded"], "points": [], "equity": None,
           "long_mv": None, "short_mv": None, "exposure_reason": None, "series_reason": None,
           "legacy_status": aset["legacy_status"], "summary": None}
    if aset["mode"] == "legacy":
        eng = aset["engine"]
        out["account_ids"] = ["old-engine"]
        if eng is None:
            why = "the old engine is not running in this backend"
            out.update(exposure_reason=why, series_reason=why, summary=_summary(["old-engine"], 1, False, None,
                                                                                 reason=why))
            return out
        st = eng.state() or {}
        acct = st.get("account") or {}
        as_of = st.get("now")
        if acct.get("unreadable"):
            why = acct.get("error") or "the old-engine account state is unreadable"
            out["exposure_reason"] = why
            out["summary"] = _summary(["old-engine"], 1, False, as_of, reason=why)
        else:
            out.update(equity=acct.get("equity"), long_mv=acct.get("long_mv"), short_mv=acct.get("short_mv"))
            vals, why = _sum_fields({"old-engine": acct})
            out["summary"] = _summary(["old-engine"], 1, False, as_of, vals, why)
        pts = eng.store.equity_points(since=eng.store.get_reset_at(), limit=200000)
        if pts is None:
            out["series_reason"] = "the old-engine equity history is unreadable"
        else:
            out["points"] = pts
        return out
    from ..execution.paper_broker import StateUnavailable
    from ..portfolio import views
    accs = aset["accounts"]
    ids = [a.id for a in accs]
    out["account_ids"] = ids
    as_of = _iso(now)
    if not accs:
        why = "no platform internal paper account is open"
        out.update(exposure_reason=why, series_reason=why, summary=_summary([], 0, False, as_of, reason=why))
        return out
    got, bad = {}, []
    for a in accs:
        try:
            v = a.broker.account(views.quotes_for(a))
            got[a.id] = {"equity": v.equity, "long_mv": v.long_mv, "short_mv": abs(v.short_mv),
                         "day_pnl": v.day_pnl, "total_pnl": v.total_pnl}
        except StateUnavailable as ex:
            bad.append(f"{a.id}: valuation paused / unreadable ({_why(ex)})")
        except Exception as ex:      # noqa: BLE001 — this account is named; nothing is summed without it
            bad.append(f"{a.id}: account unreadable ({_why(ex)})")
    summed = len(accs) > 1
    if bad:
        why = "; ".join(bad)
        out["exposure_reason"] = why
        out["summary"] = _summary(ids, len(accs), summed, as_of, reason=why)
    else:
        out.update(equity=sum(g["equity"] for g in got.values()), long_mv=sum(g["long_mv"] for g in got.values()),
                   short_mv=sum(g["short_mv"] for g in got.values()))
        vals, why = _sum_fields(got)
        out["summary"] = _summary(ids, len(accs), summed, as_of, vals, why)
    if len(accs) > 1:
        out["series_reason"] = (f"no platform equity history yet — the active set has {len(accs)} internal paper "
                                f"accounts ({', '.join(ids)}) and no portfolio-level equity series is stored "
                                "(per-account histories are not summed)")
    else:
        a = accs[0]
        try:
            pts = a.store.equity_points(since=a.store.get_reset_at(), limit=200000)
        except Exception as ex:      # noqa: BLE001
            out["series_reason"] = f"{a.id}: equity history unreadable ({_why(ex)})"
            return out
        if not pts:
            out["series_reason"] = "no platform equity history yet"
        else:
            out["points"] = pts
    return out


class OverviewRoutesMixin:
    """Handlers of ``/v2/overview/*``. ``data_dir`` / ``now_fn`` shared with the OS mixins (tests set them); the
    ``_ov_*`` hooks are the single place each source is read (tests replace them with fakes)."""

    def _ov_now(self) -> datetime:
        return _utc(self.now_fn()) if getattr(self, "now_fn", None) else datetime.now(timezone.utc)

    # hooks ------------------------------------------------------------------------------------------------------------
    def _ov_research_data(self):
        """The Market Research ``DataService`` (process-wide) or None."""
        import market_research
        svc = market_research.service()
        return None if svc is None else svc.data

    async def _ov_research_call(self, fn, *args):
        """Runs a blocking Research call on the Research pool with Research priority (shared Alpaca budget)."""
        from market_research.executor import run_blocking
        from ..data import http_gate
        with http_gate.research_priority():
            return await run_blocking(fn, *args)

    def _ov_fred_points(self, series_id: str) -> list:
        from newsroom import fred_collector
        return fred_collector.stored_points(series_id)

    def _ov_vix(self, now: datetime) -> dict:
        try:
            ctx = self._os2_ctx()
        except (ValueError, OSError) as ex:
            it = _item("VIX", "VIX", "Archive", "pt", "daily")
            it["reason"] = f"options structure config: {_why(ex)}"
            return it
        conn = self._os2_store()
        try:
            return vix_item(ctx, conn, now)
        finally:
            if conn is not None:
                conn.close()

    def _ov_risk_inputs(self) -> dict:
        return risk_inputs(self._ov_now())

    def _ov_activity_sources(self) -> List[Tuple[str, Callable[[int], List[dict]]]]:
        dd = getattr(self, "data_dir", None)
        return [("oms_orders", src_oms_orders), ("fills", src_fills), ("alerts", src_alerts), ("loops", src_loops),
                ("options_archive", lambda n: src_archive(n, dd)), ("council", src_council),
                ("old_engine", src_old_engine)]

    # GET /v2/overview/markets --------------------------------------------------------------------------------------
    async def overview_markets(self, query: Mapping, body, match: Mapping):
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        loop = asyncio.get_running_loop()
        held = getattr(self, "_ov_markets_lock", None)
        if held is None or held[0] is not loop:          # one lock per event loop (tests run several loops)
            held = (loop, asyncio.Lock())
            self._ov_markets_lock = held
        async with held[1]:
            cached = getattr(self, "_ov_markets_cache", None)
            mono = time.monotonic()
            if cached is not None and mono - cached[0] < MARKETS_CACHE_SECONDS:
                return 200, dict(cached[1], cached=True)
            payload = await self._ov_build_markets()
            self._ov_markets_cache = (mono, payload)
            return 200, dict(payload, cached=False)

    async def _ov_build_markets(self) -> dict:
        now = self._ov_now()
        items = []
        try:
            data = self._ov_research_data()
            data_err = None if data is not None else "the Market Research data layer is not installed in this backend"
        except Exception as ex:      # noqa: BLE001
            data, data_err = None, f"the Market Research data layer is unavailable: {_why(ex)}"
        for key, label in EQUITY_SYMBOLS:
            if data is None:
                it = _item(key, label, "Alpaca", "pct", "closed")
                it["reason"] = data_err
            else:
                try:
                    it = await self._ov_research_call(equity_item, data, key, label, key, now)
                except Exception as ex:      # noqa: BLE001
                    it = _item(key, label, "Alpaca", "pct", "closed")
                    it["reason"] = f"Alpaca read failed: {_why(ex)}"
            items.append(it)
        if data is None:
            it = _item(CRYPTO_KEY, CRYPTO_LABEL, "Alpaca", "pct", "open")
            it["reason"] = data_err
        else:
            try:
                it = await self._ov_research_call(crypto_item, data, now)
            except Exception as ex:      # noqa: BLE001
                it = _item(CRYPTO_KEY, CRYPTO_LABEL, "Alpaca", "pct", "open")
                it["reason"] = f"Alpaca read failed: {_why(ex)}"
        items.append(it)
        for key, label, sid, unit in FRED_ITEMS:
            items.append(await asyncio.to_thread(fred_item, self._ov_fred_points, key, label, sid, unit))
        try:
            items.append(await asyncio.to_thread(self._ov_vix, now))
        except Exception as ex:      # noqa: BLE001
            it = _item("VIX", "VIX", "Archive", "pt", "daily")
            it["reason"] = f"OS2 archive read failed: {_why(ex)}"
            items.append(it)
        return {"generated_at": _iso(now), "cache_seconds": MARKETS_CACHE_SECONDS, "items": items}

    # GET /v2/overview/risk ----------------------------------------------------------------------------------------
    async def overview_risk(self, query: Mapping, body, match: Mapping):
        from ..portfolio import overview_risk as orisk
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        now = self._ov_now()
        try:
            inp = await asyncio.to_thread(self._ov_risk_inputs)
        except Exception as ex:      # noqa: BLE001 — said, never guessed
            logger.warning("/v2/overview/risk: inputs unreadable: %s", _why(ex))
            why = f"account state unreadable: {_why(ex)}"
            inp = {"account": None, "account_ids": [], "points": [], "equity": None, "long_mv": None,
                   "short_mv": None, "exposure_reason": why, "series_reason": why,
                   "summary": _summary([], 0, False, None, reason=why)}
        pts = inp.get("points") or []
        figs, n, details = orisk.series_figures(pts, now, inp.get("series_reason"))
        figs.update(orisk.exposure_figures(inp.get("equity"), inp.get("long_mv"), inp.get("short_mv"),
                                           inp.get("exposure_reason")))
        order = ("var99_1d", "es975_1d", "net_exposure_pct", "gross_exposure_pct", "max_drawdown_pct", "vol20_ann_pct")
        last_ts = _parse(pts[-1].get("ts")) if pts else None
        summary = inp.get("summary") or _summary(inp.get("account_ids") or [], len(inp.get("account_ids") or []),
                                                 False, None, reason="no account summary from the risk source")
        return 200, {"account": inp.get("account"), "account_ids": inp.get("account_ids") or [],
                     "excluded_ids": inp.get("excluded_ids") or [], "account_summary": summary,
                     "as_of": _iso(last_ts), "generated_at": _iso(now), "n_daily_returns": n,
                     "details": details, "figures": {k: figs[k] for k in order}}

    # GET /v2/overview/activity ------------------------------------------------------------------------------------
    async def overview_activity(self, query: Mapping, body, match: Mapping):
        errors = {k: "unknown parameter" for k in query if k != "limit"}
        limit = ACTIVITY_LIMIT_DEFAULT
        if "limit" in query:
            raw = str(query["limit"])
            if not raw.isdigit() or not 1 <= int(raw) <= ACTIVITY_LIMIT_MAX:
                errors["limit"] = f"1..{ACTIVITY_LIMIT_MAX}"
            else:
                limit = int(raw)
        if errors:
            return _bad(errors)

        def go():
            items, sources = [], {}
            for name, fn in self._ov_activity_sources():
                try:
                    got = fn(limit)
                    items.extend(x for x in got if x)
                    sources[name] = "ok"
                except SourceRetired as r:   # SP §22.15: "retired" is a state, not an error
                    items.extend(x for x in r.items if x)
                    sources[name] = "retired"
                except Exception as ex:      # noqa: BLE001 — this source says why; the rest still returns
                    logger.warning("/v2/overview/activity: source %s failed: %s", name, _why(ex))
                    sources[name] = f"not available ({_why(ex)})"
            items.sort(key=lambda x: x["ts"], reverse=True)
            return {"generated_at": _iso(self._ov_now()), "limit": limit, "items": items[:limit],
                    "sources": sources}
        return 200, await asyncio.to_thread(go)


OVERVIEW_ROUTES = (
    ("GET", "/v2/overview/markets", "overview_markets", False),
    ("GET", "/v2/overview/risk", "overview_risk", False),
    ("GET", "/v2/overview/activity", "overview_activity", False),
)
