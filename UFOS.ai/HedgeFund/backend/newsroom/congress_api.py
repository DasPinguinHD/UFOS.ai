"""HTTP API of the Congress tab (CONGRESS_API.md §6) — behind the api_auth middleware, read-only, no AI.

GET /newsroom/disclosures/congress         list: filters days, code, chamber, party, late, ticker, member, measurable,
                                           limit, offset; "N of M shown" counts; one return chip per row
GET /newsroom/disclosures/congress/{id}    details: both start points, all horizons, official document link
(The congress status is part of GET /newsroom/disclosures/status.)

Every query value is validated; an invalid one is a 400 with the reason (never clamped).

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from strategy_platform.core import calendars

from . import congress
from . import disclosure_db as ddb
from . import disclosure_returns as eng
from . import disclosures_api as base

TRADE_ID_RE = re.compile(r"^congress:[A-Za-z0-9_.\-]{1,120}$")
MEMBER_RE = re.compile(r"^(house|senate)_[a-z0-9_\-]{1,100}$")
NOTES = ["Prices adjusted for splits and dividends (Alpaca IEX feed).",
         "From filing: the open of the first session after the filing date (only the date is published); from "
         "transaction date: not tradable (the member's own timing).",
         "Sales: score = −excess (did the price fall afterwards?).", congress.TERMS]


def filters(q, today: date):
    days = base._int(q, "days", 30, 1, 730)
    codes = base._list(q, "code", ["P", "S"], ["P", "S", "X"])
    where = [("filing_date >= ?", (today - timedelta(days=days)).isoformat()),
             (f"code IN ({', '.join('?' for _ in codes)})", codes)]
    echo = {"days": days, "code": codes}
    chamber = base._choice(q, "chamber", "all", ("all", "house", "senate"))
    if chamber != "all":
        where.append(("chamber = ?", chamber))
    party = base._choice(q, "party", "all", ("all", "D", "R", "I"))
    if party != "all":
        where.append(("party = ?", party))
    late = base._choice(q, "late", "all", ("all", "late", "on_time"))
    if late != "all":
        where.append(("is_late = ?", 1 if late == "late" else 0))
    ticker = (q.get("ticker") or "").strip()
    if ticker:
        if not base.TICKER_RE.match(ticker):
            raise base.BadRequest("ticker must be a stock symbol (letters, digits, '.', '-')")
        where.append(("ticker = ?", ticker.upper()))
        echo["ticker"] = ticker.upper()
    member = (q.get("member") or "").strip()
    if member:
        if not MEMBER_RE.match(member):
            raise base.BadRequest("member must be a filer id like house_<name> or senate_<name>")
        where.append(("owner_cik = ?", member))
        echo["member"] = member
    measurable = base._choice(q, "measurable", "all", ("all", "yes"))
    if measurable == "yes":
        where.append(("measurable = ?", 1))
    echo.update(chamber=chamber, party=party, late=late, measurable=measurable)
    return where, echo, base._int(q, "limit", 100, 1, 500), base._int(q, "offset", 0, 0, 10_000_000)


def compact(t: dict, returns: List[dict]) -> dict:
    return {"id": t["id"], "ticker": t["ticker"], "asset_name": t["issuer_name"], "member_id": t["owner_cik"],
            "member_name": t["owner_name"], "chamber": t["chamber"], "party": t["party"], "state": t["state"],
            "owner_type": t["owner_type"], "code": t["code"], "amount_label": t["amount_label"],
            "amount_low": t["amount_low"], "amount_high": t["amount_high"], "transaction_date": t["trade_date_first"],
            "filing_date": t["filing_date"], "days_to_file": t["days_to_file"],
            "is_late": None if t["is_late"] is None else bool(t["is_late"]), "measurable": bool(t["measurable"]),
            "not_measurable_reason": t["not_measurable_reason"],
            "chip": eng.chip(returns) if returns else None}


def detail(t: dict, returns: List[dict]) -> dict:
    out = compact(t, returns)
    out.update({"official_url": t["official_url"] if str(t.get("official_url") or "").startswith(("https://", "http://"))
                else None, "asset_type": t["asset_type"], "filing_type": t["filing_type"], "comment": t.get("comment"),
                "office": t["office"],
                "returns": {"filing": base._horizons(returns, "filing"), "trade_date": base._horizons(returns, "trade_date")},
                "notes": NOTES})
    return out


def source_state() -> dict:
    st = congress.status()
    return {"updated_through": st["updated_through"], "stale": st["stale"], "last_success": st["last_success"],
            "terms": congress.TERMS}


def list_payload(q, today: date) -> dict:
    where, echo, limit, offset = filters(q, today)
    rows, total, total_all = ddb.query_trades("congress", where, limit=limit, offset=offset,
                                              order="filing_date DESC, id")
    rets = ddb.returns_for([r["id"] for r in rows])
    return {"total_all": total_all, "total_matching": total, "shown": len(rows), "offset": offset, "filters": echo,
            "rows": [compact(r, rets.get(r["id"], [])) for r in rows], "source": source_state()}


def _today() -> date:
    return calendars.ny_date(datetime.now(timezone.utc))


def _h_list(request):
    return list_payload(request.query, _today())


def _h_detail(request):
    tid = request.match_info["id"]
    if not TRADE_ID_RE.match(tid):
        raise base.BadRequest("id must look like congress:<row id>")
    t = ddb.get_trade(tid)
    return detail(t, ddb.returns_for([tid])[tid]) if t else None


def members_payload(q) -> dict:
    query = (q.get("q") or "").strip()
    if len(query) > 60:
        raise base.BadRequest("q must be at most 60 characters")
    watched = set(ddb.watch_get("member"))
    rows = ddb.members(query)
    return {"members": [dict(r, watched=r["id"] in watched) for r in rows], "watched": sorted(watched)}


def watch_action(origin, body):
    """(HTTP status, payload) of PUT /newsroom/disclosures/watch — only the native app (no Origin header)."""
    if origin is not None:
        return 403, {"error": "Only the UFOS.ai app can change the watch list."}
    members = body.get("members") if isinstance(body, dict) else None
    if not isinstance(members, list) or not all(isinstance(m, str) for m in members) or len(members) > 500:
        return 400, {"error": 'Expected a JSON body {"members": ["house_…", …]} (at most 500).'}
    known = {r["id"] for r in ddb.members(limit=100_000)}
    unknown = sorted(m for m in set(members) if m not in known)
    if unknown:
        return 400, {"error": "unknown member id(s): " + ", ".join(unknown[:20]), "unknown": unknown}
    return 200, {"watched": ddb.watch_replace("member", members)}


async def _watch_put(request):
    import asyncio
    from aiohttp import web
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — any unreadable body is a 400
        body = None
    try:
        code, payload = await asyncio.to_thread(watch_action, request.headers.get("Origin"), body)
    except ddb.StoreUnavailable as ex:
        return web.json_response({"error": str(ex), "kind": "StoreUnavailable"}, status=503)
    return web.json_response(payload, status=code)


def _h_members(request):
    return members_payload(request.query)


ROUTES = (
    ("GET", "/newsroom/disclosures/congress", _h_list),
    ("GET", "/newsroom/disclosures/congress/{id}", _h_detail),
    ("GET", "/newsroom/disclosures/members", _h_members),
)


def register(app) -> None:
    for method, path, fn in ROUTES:
        app.router.add_route(method, path, base._respond(fn))
    app.router.add_route("PUT", "/newsroom/disclosures/watch", _watch_put)
