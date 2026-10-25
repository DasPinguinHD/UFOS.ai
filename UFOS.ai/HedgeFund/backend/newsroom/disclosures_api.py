"""HTTP API of the Disclosures section (INSIDER_RET_API.md §7, accepted 2026-10-24) — behind the api_auth middleware.

GET /newsroom/disclosures/insider            list (Insider tab): filters, "N of M shown" counts, one chip per row
GET /newsroom/disclosures/insider/{id}       details: both start points, all horizons, 10b5-1, official link
GET /newsroom/disclosures/track-record       statistics per group (n, mean/median score, hit rate, small sample)
GET /newsroom/disclosures/markers            chart markers per ticker, dated by the filing day, merged per day/side
GET /newsroom/disclosures/status             daily job, live collection, counts, unmapped SIC codes, DB sizes
POST /newsroom/disclosures/backfill          {"action": "start"|"pause"|"resume"} — the user's button (slice c)

Every query value is validated; an invalid one is a 400 with the reason (never clamped). Read-only; no AI.

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

import asyncio
import os
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

from strategy_platform.core import calendars

from . import db as newsroom_db
from . import disclosure_db as ddb
from . import disclosure_returns as eng
from . import disclosures_backfill
from . import disclosures_service as svc
from . import sector_map

ROLE_PARAM = {"CEO": "CEO", "CFO": "CFO", "Director": "Director", "10pct": "10% owner", "Other": "Other"}
GROUPS = ("direction", "plan", "role", "cluster")
TICKER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9.\-]{0,9}$")
TRADE_ID_RE = re.compile(r"^insider:\d{10}-\d{2}-\d{6}:[PS]$")
NOTES = ["Prices adjusted for splits and dividends (Alpaca IEX feed).",
         "Sales: score = −excess (did the price fall afterwards?).",
         "From filing: first tradable price after the EDGAR acceptance time; from trade date: not tradable "
         "(insider's own timing).",
         sector_map.CAVEAT]


class BadRequest(ValueError):
    pass


def _z(value) -> Optional[str]:
    if not value:
        return value
    text = str(value)
    return text[:-6] + "Z" if text.endswith("+00:00") else text


def _int(q, name, default, lo, hi) -> int:
    raw = q.get(name)
    if raw is None or raw == "":
        return default
    if not re.fullmatch(r"\d{1,6}", raw) or not lo <= int(raw) <= hi:
        raise BadRequest(f"{name} must be an integer {lo}-{hi}")
    return int(raw)


def _choice(q, name, default, allowed) -> str:
    raw = q.get(name, default) or default
    if raw not in allowed:
        raise BadRequest(f"{name} must be one of: {', '.join(allowed)}")
    return raw


def _list(q, name, default, allowed) -> List[str]:
    raw = q.get(name)
    items = default if raw in (None, "") else [x.strip() for x in raw.split(",") if x.strip()]
    bad = [x for x in items if x not in allowed]
    if bad or not items:
        raise BadRequest(f"{name} must be a comma list of: {', '.join(allowed)}")
    return items


def _day(q, name) -> Optional[date]:
    raw = q.get(name)
    if raw in (None, ""):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as ex:
        raise BadRequest(f"{name} must be a date YYYY-MM-DD") from ex


def insider_filters(q, today: date):
    days = _int(q, "days", 30, 1, 730)
    codes = _list(q, "code", ["P", "S"], ["P", "S"])
    where = [("filing_date >= ?", (today - timedelta(days=days)).isoformat()),
             (f"code IN ({', '.join('?' for _ in codes)})", codes)]
    echo = {"days": days, "code": codes}
    if q.get("role"):
        roles = _list(q, "role", [], list(ROLE_PARAM))
        where.append((f"role_class IN ({', '.join('?' for _ in roles)})", [ROLE_PARAM[r] for r in roles]))
        echo["role"] = roles
    plan = _choice(q, "plan", "all", ("all", "planned", "unplanned"))
    if plan != "all":
        where.append(("plan_10b5_1 = ?", 1 if plan == "planned" else 0))
    cluster = _choice(q, "cluster", "all", ("all", "cluster", "single"))
    if cluster != "all":
        where.append(("cluster = ?", 1 if cluster == "cluster" else 0))
    ticker = (q.get("ticker") or "").strip()
    if ticker:
        if not TICKER_RE.match(ticker):
            raise BadRequest("ticker must be a stock symbol (letters, digits, '.', '-')")
        where.append(("ticker = ?", ticker.upper()))
        echo["ticker"] = ticker.upper()
    measurable = _choice(q, "measurable", "all", ("all", "yes"))
    if measurable == "yes":
        where.append(("measurable = ?", 1))
    amendments = _choice(q, "amendments", "hide", ("hide", "show"))
    if amendments == "hide":
        where.append(("form = ?", "4"))
    echo.update(plan=plan, cluster=cluster, measurable=measurable, amendments=amendments)
    limit = _int(q, "limit", 100, 1, 500)
    offset = _int(q, "offset", 0, 0, 10_000_000)
    return where, echo, limit, offset


def _delay_days(t: dict) -> Optional[int]:
    accepted = svc._parse(t.get("accepted_at"))
    first = t.get("trade_date_first")
    if not accepted or not first:
        return None
    try:
        return (calendars.ny_date(accepted) - date.fromisoformat(first)).days
    except ValueError:
        return None


def compact(t: dict, returns: List[dict]) -> dict:
    return {"id": t["id"], "ticker": t["ticker"], "issuer_name": t["issuer_name"], "owner_name": t["owner_name"],
            "role_class": t["role_class"], "code": t["code"], "value_usd": t["value_usd"], "shares": t["shares"],
            "trade_date_first": t["trade_date_first"], "accepted_at": _z(t["accepted_at"]),
            "filing_date": t["filing_date"], "reporting_delay_days": _delay_days(t),
            "plan_10b5_1": bool(t["plan_10b5_1"]), "cluster": bool(t["cluster"]), "form": t["form"],
            "measurable": bool(t["measurable"]), "not_measurable_reason": t["not_measurable_reason"],
            "chip": eng.chip(returns) if returns else None}


def _horizons(rows: List[dict], start: str) -> Optional[dict]:
    mine = sorted((r for r in rows if r["start"] == start), key=lambda r: r["horizon"])
    if not mine:
        return None
    first = mine[0]
    return {"entry_ts": _z(first["entry_ts"]), "entry_basis": first["entry_basis"], "entry_price": first["entry_price"],
            "etf": first["etf"], "etf_entry_price": first["etf_entry_price"],
            "horizons": [{"h": r["horizon"], "state": r["state"], "sessions_done": r["sessions_done"],
                          "exit_date": r["exit_date"], "r_stock": r["r_stock"], "r_etf": r["r_etf"],
                          "excess": r["excess"], "score": r["score"], "reason": r["reason"]} for r in mine]}


def detail(t: dict, returns: List[dict]) -> dict:
    out = compact(t, returns)
    sic = ddb.sic_get(t["issuer_cik"]) if t.get("issuer_cik") else None
    etf, etf_reason = sector_map.etf_for(sic["sic"]) if sic else (None, "SIC not loaded yet")
    out.update({"roles_raw": t["roles_raw"], "co_reporters": t["co_reporters"], "avg_price": t["avg_price"],
                "trade_date_last": t["trade_date_last"], "plan_source": t["plan_source"],
                "official_url": t["official_url"] if str(t.get("official_url") or "").startswith(("https://", "http://"))
                else None,
                "issuer_cik": t["issuer_cik"], "sic": sic["sic"] if sic else None,
                "sic_description": sic["sic_description"] if sic else None, "etf": etf, "etf_reason": etf_reason,
                "returns": {"filing": _horizons(returns, "filing"), "trade_date": _horizons(returns, "trade_date")},
                "notes": NOTES})
    return out


def list_payload(q, today: date) -> dict:
    where, echo, limit, offset = insider_filters(q, today)
    rows, total, total_all = ddb.query_trades("insider", where, limit=limit, offset=offset)
    rets = ddb.returns_for([r["id"] for r in rows])
    return {"total_all": total_all, "total_matching": total, "shown": len(rows), "offset": offset, "filters": echo,
            "rows": [compact(r, rets.get(r["id"], [])) for r in rows], "source": source_state()}


def _group_key(group: str, r: dict) -> Optional[str]:
    if r.get("form") != "4":
        return None
    if group == "direction":
        return {"P": "Purchases", "S": "Sales"}.get(r["code"])
    if group == "plan":
        return "10b5-1 planned" if r["plan_10b5_1"] else "not planned"
    if group == "role":
        return r["role_class"] or "Other"
    if group == "cluster":
        return "cluster" if r["cluster"] else "single"
    return None


def track_record_payload(q, today: date) -> dict:
    source = _choice(q, "source", "insider", ("insider",))
    horizon = int(_choice(q, "horizon", "20", tuple(str(h) for h in eng.HORIZONS)))
    start = _choice(q, "start", "filing", eng.STARTS)
    group = _choice(q, "group", "direction", GROUPS)
    days = _int(q, "days", 730, 1, 730)
    rows = ddb.returns_rows(source, start, horizon, (today - timedelta(days=days)).isoformat())
    groups = {r["trade_id"]: k for r in rows if (k := _group_key(group, r))}
    return {"caveat": eng.TRACK_RECORD_CAVEAT, "source": source, "horizon": horizon, "start": start, "group": group,
            "days": days, "groups": eng.group_stats(rows, groups, horizon, start)}


def markers_payload(q) -> dict:
    ticker = (q.get("ticker") or "").strip()
    if not TICKER_RE.match(ticker):
        raise BadRequest("ticker is required (a stock symbol)")
    first, last = _day(q, "from"), _day(q, "to")
    if first is None or last is None or first > last:
        raise BadRequest("from and to are required dates YYYY-MM-DD with from <= to")
    kinds = _list(q, "kinds", ["insider"], ["insider"])
    found, _total, _all = ddb.query_trades("insider", [("ticker = ?", ticker.upper()), ("form = ?", "4"),
                                                      ("filing_date >= ?", (first - timedelta(days=1)).isoformat()),
                                                      ("filing_date <= ?", (last + timedelta(days=1)).isoformat())],
                                           limit=100_000)
    merged: Dict[tuple, dict] = {}
    for t in found:
        accepted = svc._parse(t.get("accepted_at"))
        day = calendars.ny_date(accepted) if accepted else date.fromisoformat(t["filing_date"])
        if not first <= day <= last:
            continue
        side = "buy" if t["code"] == "P" else "sell"
        m = merged.setdefault((day, side), {"date": day.isoformat(), "kind": "insider", "side": side, "count": 0,
                                            "ids": [], "value_usd": 0.0})
        m["count"] += 1
        m["ids"].append(t["id"])
        m["value_usd"] += t["value_usd"] or 0.0
    return {"ticker": ticker.upper(), "kinds": kinds, "dated_by": "filing day",
            "markers": [merged[k] for k in sorted(merged)]}


def _size(path) -> Optional[int]:
    try:
        return os.path.getsize(path) if path else None
    except OSError:
        return None


def source_state() -> dict:
    job = ddb.job_get(svc.JOB_DAILY) or {}
    return {"backfill": disclosures_backfill.BACKFILL.state().get("state"),
            "last_daily_job": _z((job.get("progress") or {}).get("finished")),
            "daily_job_state": job.get("state")}


def status_payload() -> dict:
    from strategy_platform.storage import db as platform_db
    job = ddb.job_get(svc.JOB_DAILY) or {}
    now = datetime.now(timezone.utc)
    unmapped = Counter({sic: n for sic, n in ddb.sic_counts("insider").items()
                        if sector_map.etf_for(sic)[0] is None})
    return {
        "backfill": disclosures_backfill.status(),
        "daily_job": {"state": job.get("state"), "last_run": _z((job.get("progress") or {}).get("finished")),
                      "next_run": _z(svc._iso(svc.next_slot(now))), "progress": job.get("progress") or {},
                      "last_error": job.get("last_error")},
        "live": dict(svc.LIVE_STATE),
        "counts": ddb.counts("insider"),
        "sic_unmapped": [{"sic": k, "n": n} for k, n in unmapped.most_common()],
        "storage": {"newsroom_db_bytes": _size(newsroom_db.db_path()),
                    "hedgefund_db_bytes": _size(platform_db.db_path())},
        "sec_interval_seconds": svc.LIMITER.interval(),
    }


def backfill_action(origin, body):
    """(HTTP status, payload) of POST /newsroom/disclosures/backfill. Only the native app may control it: a request
    from a web page (Origin header) is refused, as for PUT /ai/budgets."""
    if origin is not None:
        return 403, {"error": "Only the UFOS.ai app can control the backfill."}
    action = body.get("action") if isinstance(body, dict) else None
    if action not in ("start", "pause", "resume"):
        return 400, {"error": 'Expected a JSON body {"action": "start" | "pause" | "resume"}.'}
    ok, message = disclosures_backfill.BACKFILL.control(action)
    status = disclosures_backfill.status()
    if not ok:
        return 409, {"error": message, "backfill": status}
    return 200, {"message": message, "backfill": status}


async def _backfill_post(request):
    from aiohttp import web
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — any unreadable body is a 400
        body = None
    try:
        code, payload = await asyncio.to_thread(backfill_action, request.headers.get("Origin"), body)
    except ddb.StoreUnavailable as ex:
        return web.json_response({"error": str(ex), "kind": "StoreUnavailable"}, status=503)
    return web.json_response(payload, status=code)


def _respond(fn, *args):
    async def handler(request):
        from aiohttp import web          # lazy: the payload functions are tested without aiohttp
        try:
            payload = await asyncio.to_thread(fn, request, *args)
        except BadRequest as ex:
            return web.json_response({"error": str(ex)}, status=400)
        except ddb.StoreUnavailable as ex:
            return web.json_response({"error": str(ex), "kind": "StoreUnavailable"}, status=503)
        if payload is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(payload)
    return handler


def _today() -> date:
    return calendars.ny_date(datetime.now(timezone.utc))


def _h_list(request):
    return list_payload(request.query, _today())


def _h_detail(request):
    tid = request.match_info["id"]
    if not TRADE_ID_RE.match(tid):
        raise BadRequest("id must look like insider:<accession>:<P|S>")
    t = ddb.get_trade(tid)
    return detail(t, ddb.returns_for([tid])[tid]) if t else None


def _h_track(request):
    return track_record_payload(request.query, _today())


def _h_markers(request):
    return markers_payload(request.query)


def _h_status(request):
    return status_payload()


ROUTES = (
    ("GET", "/newsroom/disclosures/insider", _h_list),
    ("GET", "/newsroom/disclosures/insider/{id}", _h_detail),
    ("GET", "/newsroom/disclosures/track-record", _h_track),
    ("GET", "/newsroom/disclosures/markers", _h_markers),
    ("GET", "/newsroom/disclosures/status", _h_status),
)


def register(app) -> None:
    for method, path, fn in ROUTES:
        app.router.add_route(method, path, _respond(fn))
    app.router.add_route("POST", "/newsroom/disclosures/backfill", _backfill_post)
