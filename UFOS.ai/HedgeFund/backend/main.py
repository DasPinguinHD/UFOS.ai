"""HedgeFund backend.

Two parts:
- WebSocket (WS_HOST:WS_PORT): placeholder for the multi-agent pipeline
  (unchanged; agents will publish signals/verdicts here later).
- HTTP API on 127.0.0.1:HEALTH_PORT, used by the WPF window:
    GET  /health                    -> "ok"
    GET  /version                   -> {"code_mtime", "started_at", "pid"} (stale-backend check)
    POST /shutdown                  -> exit (loopback only; used to replace a stale process)
    GET  /newsroom/status           -> {"sources": [per-source status], "storage": {path, size_bytes, rows}}
    GET  /newsroom/insider          -> recent SEC Form 4 filings (newest first)
    GET  /newsroom/macro            -> latest FRED macro series with history
    GET  /newsroom/calendar         -> upcoming US releases (FRED) + FOMC decisions, next 45 days
    POST /newsroom/insider-summary  -> AI summary of the most notable trades (+ provenance)
    GET  /newsroom/overview         -> "Analysis & Reasoning" tiles + templates + model ids per tier
    POST /newsroom/analysis         -> AI analysis of the selected tiles (+ provenance)
    GET  /newsroom/ai-history       -> stored AI outputs, newest first (?feature=&limit=, default 20)
  HedgeFund autonomous loop (2026-09-26, package fund/, contract fund/SPEC.md §11;
  PAPER TRADING ONLY — simulated orders, not investment advice):
    GET  /fund/state                -> market, engine, autopilot, halt, AI budget, account, positions,
                                       pending orders, per-symbol agents + latest recommendation
    GET  /fund/params               -> {version, params, defaults, limits, created_at}
    PUT  /fund/params               -> new params version (applies from the next cycle); 400 {errors}
    POST /fund/run                  -> run a cycle now
    POST /fund/autopilot            -> {enabled} (409 while halted)
    POST /fund/kill | /fund/resume  -> kill switch (halt + liquidate) / clear the halt
    POST /fund/reset                -> fresh paper account {starting_capital}
    GET  /fund/runs, /fund/runs/{id}, /fund/orders, /fund/equity, /fund/events, /fund/audit
    POST /fund/orders               -> manual paper order; POST /fund/recommendations/{id}/execute
    POST /fund/backtest, GET /fund/backtests[/{id}] -> rule-only backtest
    GET  /fund/export/orders.csv, /fund/export/decisions.csv

The fund engine (fund/engine.py) runs as one asyncio task, first cycle 20 s
after the start (after the newsroom cache is loaded); it reads the newsroom's
macro / calendar / insider caches. Without yfinance/network its runs end as
"skipped"/"partial" — the backend keeps working.

Local SQLite store (2026-09-25, newsroom/db.py): insider filings, FRED/Eurostat
observations, the last calendar snapshot and every AI output are kept in
HedgeFund/backend/data/newsroom.db (+ prepared trading tables). At startup the
store is migrated, pruned and loaded, so the API answers from it before the
first poll ("from cache" status); FRED and the calendar skip their first poll
while the cached data is younger than their poll interval.

The Financial Newsroom (package newsroom/) was moved here from the Global
Monitoring module on 2026-09-24: insider trades (SEC EDGAR) and macro
indicators (FRED). See newsroom/__init__.py for how to add a section.
"""
import asyncio
import logging
import os
from datetime import date, datetime, timedelta, timezone

from aiohttp import web
import websockets

from logging_setup import configure_logging

configure_logging()

from config import WS_HOST, WS_PORT, HEALTH_PORT, HTTP_HOST, DEFAULT_ALLOWED_ORIGINS
import api_auth

API_ORIGINS = api_auth.allowed_origins(DEFAULT_ALLOWED_ORIGINS)
from config import (INSIDER_DISPLAY_DAYS, INSIDER_DISPLAY_MAX, INSIDER_RETENTION_DAYS,
                    AI_RESULTS_RETENTION_DAYS, DB_PRUNE_INTERVAL_SECONDS)
from config import FRED_API_KEY, FRED_POLL_SECONDS, CALENDAR_POLL_SECONDS
from newsroom import db as newsroom_db
from newsroom import store as newsroom_store
from newsroom import ai as newsroom_ai
from newsroom import insider_summary
from newsroom import fred_collector, sec_edgar_collector, macro_derived
from newsroom import calendar_collector
from newsroom import overview as newsroom_overview
from newsroom.store import EventStore, now_iso
from fund import store as fund_store
from fund import market as fund_market
from fund import broker as fund_broker
from fund import engine as fund_engine
from fund import api as fund_api

logger = logging.getLogger(__name__)

# --- Stale-backend detection (same mechanism as Global Monitoring) ---
# A running backend keeps the code it was started with. CODE_MTIME = newest
# .py mtime at process start; the WPF side compares it with the files on disk
# (GET /version) and replaces an outdated process (POST /shutdown).
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))


def _newest_py_mtime(root: str) -> float:
    newest = 0.0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".venv", "__pycache__", "data")]
        for f in filenames:
            if f.endswith(".py"):
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(dirpath, f)))
                except OSError:
                    pass
    return newest


CODE_MTIME = _newest_py_mtime(_BACKEND_DIR)
STARTED_AT = datetime.now(timezone.utc).isoformat()

# Financial Newsroom sources: name -> collector module (each has run(emit, report_status)).
NEWSROOM_SOURCES = {
    "sec_edgar": sec_edgar_collector,
    "fred": fred_collector,
    "calendar": calendar_collector,
}
SOURCE_LABELS = {"sec_edgar": "SEC EDGAR (insider trades)", "fred": "FRED (macro indicators)"}
SOURCE_LABELS["calendar"] = "FRED release calendar"


def is_outside_display_window(event: dict, now: datetime = None) -> bool:
    """Insider filings older than INSIDER_DISPLAY_DAYS (by filing time) are not
    shown — they stay archived in the SQLite store."""
    return newsroom_store.filed_before_window(event, INSIDER_DISPLAY_DAYS, now)


# Sources whose first live poll is skipped while the cached data is younger
# than their poll interval (meta "last_ok:<source>"); both need FRED_API_KEY.
SKIP_FIRST_POLL_IF_FRESH = {"fred": FRED_POLL_SECONDS, "calendar": CALENDAR_POLL_SECONDS}


def _parse_iso(text):
    try:
        dt = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def first_poll_delay(last_ok_iso, poll_seconds: int, now: datetime = None) -> float:
    """Seconds to wait before a collector's first live poll: the rest of its
    poll interval since the last successful poll, 0 when unknown/stale."""
    last = _parse_iso(last_ok_iso) if last_ok_iso else None
    if last is None:
        return 0.0
    age = ((now or datetime.now(timezone.utc)) - last).total_seconds()
    return max(0.0, poll_seconds - age) if age >= 0 else 0.0


def cached_status_message(last_ok_iso, next_poll_in: float = 0.0) -> str:
    """'from cache (updated HH:MM) — refreshing…' (local time of the backend)."""
    last = _parse_iso(last_ok_iso) if last_ok_iso else None
    updated = f" (updated {last.astimezone().strftime('%H:%M')})" if last else ""
    if next_poll_in > 0:
        nxt = (datetime.now(timezone.utc) + timedelta(seconds=next_poll_in)).astimezone().strftime("%H:%M")
        return f"from cache{updated} — next refresh {nxt}"
    return f"from cache{updated} — refreshing…"


async def load_from_store(insider_store, macro_latest: dict, calendar_latest: dict) -> bool:
    """Startup: open + migrate + prune the SQLite store, then fill the in-memory
    caches from it so the API answers before the first poll. Never raises;
    False = store unavailable (the backend runs in-memory only)."""
    try:
        if not await asyncio.to_thread(newsroom_db.init):
            return False
        await asyncio.to_thread(newsroom_db.prune, None, INSIDER_RETENTION_DAYS, AI_RESULTS_RETENTION_DAYS)
        since = (datetime.now(timezone.utc) - timedelta(days=INSIDER_DISPLAY_DAYS)).isoformat()
        stored = await asyncio.to_thread(newsroom_db.load_insider, since)
        for event in reversed(stored):  # oldest first; the store keeps the newest
            insider_store.add(event)
        seeded = sec_edgar_collector.seed_details(stored)
        series = await asyncio.to_thread(fred_collector.load_state)
        macro_events = []
        if series:
            macro_events = fred_collector.build_events()[0]
            for event in macro_events:
                macro_latest[event["id"]] = event
        cal = await asyncio.to_thread(newsroom_db.load_calendar)
        if cal:
            today = date.today().isoformat()
            cal["events"] = [e for e in cal.get("events") or [] if (e.get("date") or "")[:10] >= today]
            calendar_latest.clear()
            calendar_latest.update(cal)
        logger.info("Loaded from SQLite store: %d insider filings (%d with details), %d stored series -> %d macro "
                    "events, calendar %s", len(stored), seeded, series, len(macro_events),
                    "yes" if cal else "no")
        return True
    except Exception:
        logger.exception("Loading the SQLite store failed — continuing in-memory")
        return False


async def _prune_forever() -> None:
    """Daily retention run while the backend is up (also runs at startup).
    newsroom_db.prune also covers the fund tables (fund_runs + agent scores
    90 days, fund_events 30 days, prices 2 years)."""
    while True:
        await asyncio.sleep(DB_PRUNE_INTERVAL_SECONDS)
        try:
            await asyncio.to_thread(newsroom_db.prune, None, INSIDER_RETENTION_DAYS, AI_RESULTS_RETENTION_DAYS)
        except Exception:
            logger.exception("SQLite prune failed")


async def _ws_handler(websocket):
    async for _ in websocket:
        pass


async def _run_collector_forever(name, module, emit, report_status, initial_delay: float = 0.0) -> None:
    """One crashing collector must not take the process down: log, report,
    retry after 60s. initial_delay: skip the first poll (fresh cache)."""
    if initial_delay > 0:
        await asyncio.sleep(initial_delay)
    while True:
        try:
            await module.run(emit, report_status)
            logger.warning("Collector %s returned instead of looping forever; restarting in 60s", name)
        except Exception as ex:
            logger.exception("Collector %s crashed: %s", name, ex)
            try:
                await report_status(False, f"crashed: {ex}")
            except Exception:
                pass
        await asyncio.sleep(60)


async def main() -> None:
    # Fresh session token for this start (published to data/api-token after binding).
    api_auth.init_token()
    # Insider Trades shows the last INSIDER_DISPLAY_DAYS (config) — up to INSIDER_DISPLAY_MAX filings.
    newsroom_store.CATEGORY_LIMITS["insider"] = INSIDER_DISPLAY_MAX
    insider_store = EventStore()
    macro_latest: dict = {}   # "macro-<SERIES>" -> latest event (overwritten each poll)
    calendar_latest: dict = {}  # latest calendar_collector poll: {"events", "updated", ...}
    status = {name: {"source": name, "label": SOURCE_LABELS[name], "ok": None,
                     "message": "starting…", "timestamp": None} for name in NEWSROOM_SOURCES}

    def make_report_status(name):
        async def report_status(ok: bool, message: str):
            ts = now_iso()
            status[name] = {"source": name, "label": SOURCE_LABELS[name], "ok": ok,
                            "message": message, "timestamp": ts}
            if ok:
                # Last successful poll: "from cache (updated HH:MM)" + first-poll skip after a restart.
                newsroom_db.meta_set(newsroom_db.last_ok_key(name), ts)
        return report_status

    def _on_insider(event):
        # Every filing goes to the archive (new -> insert, known -> last_seen);
        # only the display window goes to the in-memory store.
        newsroom_db.upsert_insider(event)
        if not is_outside_display_window(event):
            insider_store.add(event)

    def _on_calendar(payload):
        calendar_latest.clear()
        calendar_latest.update(payload)
        newsroom_db.save_calendar(payload)

    emitters = {
        "sec_edgar": _on_insider,
        "fred": lambda event: macro_latest.__setitem__(event["id"], event),
        "calendar": _on_calendar,
    }

    def _insider_events() -> list:
        # Drops filings that aged out of the display window (they stay in the database).
        insider_store.prune(is_outside_display_window)
        return [e for e in insider_store.snapshot() if e.get("category") == "insider"]

    # --- HTTP API ---
    async def _health(request):
        return web.Response(text="ok")

    async def _version(request):
        return web.json_response({"code_mtime": CODE_MTIME, "started_at": STARTED_AT, "pid": os.getpid()})

    async def _shutdown(request):
        if request.remote not in ("127.0.0.1", "::1"):
            return web.json_response({"error": "forbidden"}, status=403)
        logger.warning("Shutdown requested by the app (backend code on disk is newer than this process).")
        asyncio.get_running_loop().call_later(0.3, lambda: os._exit(0))
        return web.json_response({"ok": True})

    async def _newsroom_status(request):
        storage = await asyncio.to_thread(newsroom_db.storage_info)  # None = store disabled (in-memory only)
        return web.json_response({"sources": list(status.values()), "storage": storage})

    async def _newsroom_insider(request):
        events = _insider_events()
        # clusters: several insiders of one company buying/selling within 14 days
        # (insider_summary.find_clusters, 2026-09-25).
        return web.json_response({"items": events, "status": status["sec_edgar"],
                                  "clusters": insider_summary.find_clusters(events)})

    async def _newsroom_macro(request):
        # Raw FRED series first, then the derived indicators (group "derived").
        order = [sid for sid, *_ in fred_collector.SERIES] + list(macro_derived.DERIVED)
        items = sorted(macro_latest.values(),
                       key=lambda e: order.index(e["macro"]["series_id"]) if e["macro"]["series_id"] in order else 99)
        return web.json_response({"items": items, "status": status["fred"]})

    async def _newsroom_calendar(request):
        return web.json_response({"events": calendar_latest.get("events", []),
                                  "updated": calendar_latest.get("updated"),
                                  "window_days": calendar_latest.get("window_days"),
                                  "status": status["calendar"]})

    async def _newsroom_insider_summary(request):
        if not newsroom_ai.is_configured():
            return web.json_response(
                {"error": "OPENROUTER_API_KEY is not set in HedgeFund/backend/.env — see .env.example."}, status=503)
        insider_events = _insider_events()
        table, used = insider_summary.build_table(insider_events)
        if not used:
            return web.json_response(
                {"error": "No insider filings with parsed transaction details yet — wait for the next SEC EDGAR poll."},
                status=409)
        logger.info("Insider summary request: %d filings, %d chars", used, len(table))
        try:
            text, model, usage = await newsroom_ai.summarize_insider_trades(table)
            if not text:
                return web.json_response({"error": "The model returned an empty response."}, status=502)
            prov = newsroom_ai.provenance("insider-summary", model)
            newsroom_db.insert_ai_result("insider-summary", model, None, {"filings": used, "table": table},
                                         text, prov, usage)
            return web.json_response({"summary": text, "filings": used, "usage": usage, "provenance": prov})
        except Exception as ex:
            # details only in the log: provider errors can echo request data / internals
            logger.warning("Insider summary failed: %s", ex)
            return web.json_response(
                {"error": "The AI summary failed (provider error) — see backend.log for details."}, status=502)

    # --- Analysis & Reasoning (2026-09-25) ---
    def _build_overview():
        insider_events = _insider_events()
        return newsroom_overview.build_overview(macro_latest, insider_events, calendar_latest)

    async def _newsroom_overview(request):
        data = _build_overview()
        data["models"] = newsroom_ai.analysis_models()  # model ids only, never keys
        return web.json_response(data)

    async def _newsroom_analysis(request):
        try:
            body = await request.json()
        except Exception:
            body = None
        if not isinstance(body, dict):
            return web.json_response({"error": "Expected a JSON body {\"tile_ids\": [...], \"tier\": ...}."}, status=400)
        tier = body.get("tier") or "standard"
        if tier not in ("standard", "premium"):
            return web.json_response({"error": "tier must be \"standard\" or \"premium\"."}, status=400)
        overview = _build_overview()  # fresh: values may have changed since the view loaded
        tiles_by_id = {t["id"]: t for t in overview["tiles"]}
        selected = []
        for tid in body.get("tile_ids") or []:
            if isinstance(tid, str) and tid in tiles_by_id and tid not in selected:
                selected.append(tid)
        selected = selected[:25]
        if len(selected) < 2:
            return web.json_response({"error": "Select at least two items."}, status=400)
        if not newsroom_ai.is_configured():
            return web.json_response(
                {"error": "OPENROUTER_API_KEY is not set in HedgeFund/backend/.env — see .env.example."}, status=503)
        context = newsroom_overview.build_analysis_context(tiles_by_id, selected, calendar_latest.get("events") or [])
        logger.info("Analysis request: %d tiles, tier %s, %d chars", len(selected), tier, len(context))
        try:
            text, model, usage = await newsroom_ai.analyse_selection(context, tier)
            if not text:
                return web.json_response({"error": "The model returned an empty response."}, status=502)
            prov = newsroom_ai.provenance("analysis", model)
            newsroom_db.insert_ai_result(
                "analysis", model, tier,
                {"tile_ids": selected, "tiles_used": len(selected),
                 "titles": [tiles_by_id[t].get("title") for t in selected]},
                text, prov, usage)
            return web.json_response({
                "analysis": text, "tiles_used": len(selected), "tier": tier,
                "provenance": prov,
                # What OpenRouter charged for this one request (credits = USD); None if not reported.
                "usage": usage,
            })
        except Exception as ex:
            # details only in the log: provider errors can echo request data / internals
            logger.warning("Analysis failed: %s", ex)
            return web.json_response(
                {"error": "The AI analysis failed (provider error) — see backend.log for details."}, status=502)

    async def _newsroom_ai_history(request):
        # Read-only history of stored AI outputs (for later use); original model/time kept in provenance.
        try:
            limit = int(request.query.get("limit") or 20)
        except ValueError:
            return web.json_response({"error": "limit must be an integer."}, status=400)
        feature = request.query.get("feature") or None
        items = await asyncio.to_thread(newsroom_db.ai_history, feature, limit)
        return web.json_response({"items": items, "stored": newsroom_db.is_enabled()})

    await load_from_store(insider_store, macro_latest, calendar_latest)

    # Cache first, then reconcile: sources with cached data say so until their
    # first live poll reports (report_status overwrites the entry).
    has_cache = {"sec_edgar": bool(_insider_events()), "fred": bool(macro_latest),
                 "calendar": bool(calendar_latest.get("events"))}
    delays = {}
    for name in NEWSROOM_SOURCES:
        if not has_cache[name]:
            continue
        last_ok = newsroom_db.meta_get(newsroom_db.last_ok_key(name))
        delay = 0.0
        if name in SKIP_FIRST_POLL_IF_FRESH and FRED_API_KEY:
            delay = first_poll_delay(last_ok, SKIP_FIRST_POLL_IF_FRESH[name])
            if delay > 0:
                delays[name] = delay
                logger.info("%s: cached data is fresh (last poll %s) — first live poll in %.0f min",
                            name, last_ok, delay / 60)
        status[name] = {"source": name, "label": SOURCE_LABELS[name], "ok": True,
                        "message": cached_status_message(last_ok, delay), "timestamp": last_ok, "cached": True}

    # Token + origin check on every route except /health and /version (api_auth).
    app = web.Application(middlewares=[api_auth.http_middleware(API_ORIGINS)])
    app.router.add_get("/health", _health)
    app.router.add_get("/version", _version)
    app.router.add_post("/shutdown", _shutdown)
    app.router.add_get("/newsroom/status", _newsroom_status)
    app.router.add_get("/newsroom/insider", _newsroom_insider)
    app.router.add_get("/newsroom/macro", _newsroom_macro)
    app.router.add_get("/newsroom/calendar", _newsroom_calendar)
    app.router.add_post("/newsroom/insider-summary", _newsroom_insider_summary)
    app.router.add_get("/newsroom/overview", _newsroom_overview)
    app.router.add_post("/newsroom/analysis", _newsroom_analysis)
    app.router.add_get("/newsroom/ai-history", _newsroom_ai_history)

    # HedgeFund autonomous loop (paper trading only). Market data via yfinance
    # (cached in the prices table); the engine never raises into the app.
    fund_feed = fund_market.YFinanceFeed(cache_store=fund_store)
    fund_paper_broker = fund_broker.PaperBroker(fund_store, fund_feed.quote, dividends_fn=fund_feed.dividends,
                                                bars_fn=fund_feed.daily_bars)
    engine = fund_engine.FundEngine(
        fund_feed, fund_paper_broker, fund_store,
        macro_fn=lambda: macro_latest,
        calendar_fn=lambda: list(calendar_latest.get("events") or []),
        insider_fn=_insider_events,
        backtest_feed=fund_market.YFinanceFeed(cache_store=None))   # years of bars, not kept in SQLite
    fund_api.register(app, engine)
    # No per-request access log: the newsroom window polls every 10s, which
    # would bury everything else in backend.log.
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, api_auth.bind_host(HTTP_HOST, "HTTP API"), HEALTH_PORT)
    await site.start()
    api_auth.publish_token()  # only now: the port is ours (see api_auth.init_token)
    logger.info("HTTP API listening on %s (/health, /newsroom/insider, /newsroom/macro, "
                "/newsroom/calendar, /newsroom/insider-summary, /newsroom/overview, /newsroom/analysis, "
                "/newsroom/ai-history, /newsroom/status, /fund/*; token required except /health, /version)",
                site.name)

    # After the newsroom cache is loaded (load_from_store above): forces the
    # autopilot off, first cycle after fund_engine.STARTUP_DELAY_SECONDS.
    engine.start()

    tasks = [asyncio.create_task(_run_collector_forever(name, module, emitters[name], make_report_status(name),
                                                        delays.get(name, 0.0)))
             for name, module in NEWSROOM_SOURCES.items()]
    tasks.append(asyncio.create_task(_prune_forever()))
    logger.info("Started Financial Newsroom collectors: %s", ", ".join(NEWSROOM_SOURCES))

    async with websockets.serve(_ws_handler, api_auth.bind_host(WS_HOST, "WebSocket server"), WS_PORT,
                                process_request=api_auth.ws_process_request(API_ORIGINS)):
        logger.info("WebSocket listening on port %s (token required)", WS_PORT)
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down.")
