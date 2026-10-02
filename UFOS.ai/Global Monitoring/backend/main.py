"""Entrypoint: runs the four data collectors, broadcasts their events over
WebSocket, and serves a small HTTP API (health, event snapshot, local news).

Insider trades (SEC EDGAR) and macro indicators (FRED) were collected here
until 2026-09-24; they moved to the Hedge Fund module's Financial Newsroom
(HedgeFund/backend/newsroom/).

Architecture (see docs/global-monitoring-system-plan.md, Section 2/3):
- Each collector in collectors/ is an independent async poller.
- New events go through EventStore (dedup + ring buffer) and are broadcast
  to every connected frontend over WebSocket (ws://<WS_HOST>:<WS_PORT>).
- Every poll cycle (success or failure) also broadcasts a "source_status"
  message, which drives the "Ampel pro Feed" traffic-light list in the
  frontend's left sidebar.
- A small aiohttp app serves:
    GET /health              -> "ok"
    GET /events              -> current event snapshot (for a client that
                                 wants it over HTTP instead of waiting for
                                 the WS "snapshot" message); optional
                                 ?category=geopolitics (etc.) filters it
    GET /localnews?city=&country=&lat=&lon=
                              -> real headlines about that city (Google News RSS),
                                 used by the "click a capital star" feature
    POST /assess              -> {category, title, summary, source} in,
                                 {"assessment": "...", "provenance": {...}} out
                                 ("provenance" = machine-readable AI marking,
                                 EU AI Act Art. 50) — the "Assess"/
                                 "Sentiment" button on a marker, via OpenRouter
                                 (see assess.py)

Local SQLite storage (2026-09-25, storage.py + persistence.py, data/gms.db):
cache first, then reconcile. At start, expired rows are pruned and the cached
events are loaded into the EventStore BEFORE the collectors start, so a
connecting frontend gets a snapshot at once; sources with cached events
report "from cache (updated HH:MM) — refreshing…" until their first poll.
Collectors then upsert by their stable event ids; every emitted event is
stored (flights: every polled position, markets: price points), every
removed one deleted; AI outputs are recorded; retention runs daily too.
Requests (GET /events, a new WebSocket client) also wake collectors whose
last poll is older than REFRESH_MIN_SECONDS (refresh.py).
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone

import aiohttp
from aiohttp import web

from logging_setup import configure_logging

configure_logging()

import assess
import localnews
import persistence
import refresh
import storage
from broadcast import Broadcaster
import api_auth
from config import HEALTH_PORT, HTTP_HOST, ASSESS_REUSE_SECONDS, REFRESH_MIN_SECONDS, DEFAULT_ALLOWED_ORIGINS

API_ORIGINS = api_auth.allowed_origins(DEFAULT_ALLOWED_ORIGINS)
from events import EventStore
from logger import log_event
from localnews import fetch_local_news, SUMMARY_MAX_RECORDS
import assess_context

from collectors.gdacs_collector import is_past as _disaster_is_past
from collectors import news_collector, gdacs_collector, opensky_collector, yahoo_collector

logger = logging.getLogger(__name__)

# --- Stale-backend detection (2026-09-24) ---
# The backend is a long-running process: code changes on disk only take
# effect after a restart. Twice now a stale process kept serving old code
# (22.09.: started by hand; 24.09.: orphaned when the Visual Studio debugger
# was stopped, so the window's Closing handler never ran). CODE_MTIME is the
# newest modification time of this backend's .py files AT PROCESS START;
# MainWindow.xaml.cs compares it (GET /version) against the files on disk
# and, if the disk is newer, asks this process to exit (POST /shutdown) and
# starts a fresh one.
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

COLLECTORS = {
    "news": news_collector,
    "gdacs": gdacs_collector,
    "opensky": opensky_collector,
    "yahoo": yahoo_collector,
}

# CORS: no wildcard any more. api_auth.http_middleware adds
# Access-Control-Allow-Origin (+ the preflight headers, incl. X-UFOS-Token)
# only for the allowed page origin (the globe WebView), and answers OPTIONS
# preflights itself. CORS_HEADERS stays as an (empty) name so the handlers
# below didn't need touching.
CORS_HEADERS: dict = {}


DB_PRUNE_INTERVAL_SECONDS = 24 * 3600


async def main() -> None:
    # Fresh session token for this start (published to data/api-token after binding).
    api_auth.init_token()
    # --- Local SQLite storage: retention, then cache first (before any collector starts) ---
    # Storage never raises: a corrupt file is moved aside, an unusable one
    # falls back to an in-memory database.
    db_store = storage.Storage()
    pruned = persistence.apply_retention(db_store)
    store = EventStore()
    cached_events = persistence.warm_start_events(db_store)
    seeded = store.seed(cached_events)
    localnews_loaded = localnews.attach_storage(db_store)
    # Incremental news polling: articles already stored are skipped.
    news_collector.is_known = lambda event_id: db_store.has_item("news", event_id)
    logger.info("Local storage%s: pruned %s, restored %d event(s) and %d local news lookup(s)",
                " (in-memory fallback)" if db_store.in_memory else "", pruned, seeded, localnews_loaded)

    seeded_per_source: dict[str, int] = {}
    for event in store.snapshot():
        src = persistence.SOURCE_BY_CATEGORY.get(event.get("category"))
        if src:
            seeded_per_source[src] = seeded_per_source.get(src, 0) + 1
    source_status: dict[str, dict] = {}
    for name in COLLECTORS:
        state = db_store.get_state(name) or {}
        last_ok = storage.parse_iso(state.get("last_success_ts"))
        last_try = storage.parse_iso(state.get("last_attempt_ts"))
        if last_ok:
            refresh.note_success(name, last_ok.timestamp())
        if last_try:
            refresh.note_attempt(name, max(last_try.timestamp(), last_ok.timestamp() if last_ok else 0.0))
        source_status[name] = (persistence.cached_status(db_store, name, seeded_per_source.get(name, 0))
                               or {"source": name, "ok": None, "message": "starting…", "timestamp": None})

    broadcaster = Broadcaster(origins=API_ORIGINS)

    def _refresh_stale_sources():
        # Stale-while-revalidate: answer from memory now, wake stale collectors.
        for name in COLLECTORS:
            try:
                refresh.request_refresh(name, REFRESH_MIN_SECONDS.get(name, 600))
            except Exception as ex:
                logger.warning("Refresh request for %s failed: %s", name, ex)

    async def _on_connect(ws):
        await broadcaster.send_to(ws, "snapshot", store.snapshot())
        for status in source_status.values():
            await broadcaster.send_to(ws, "source_status", status)
        _refresh_stale_sources()

    broadcaster.on_connect = _on_connect
    await broadcaster.start()

    # --- HTTP API ---
    localnews_session = aiohttp.ClientSession()

    async def _version(request):
        return web.json_response({"code_mtime": CODE_MTIME, "started_at": STARTED_AT, "pid": os.getpid()},
                                 headers=CORS_HEADERS)

    async def _shutdown(request):
        # Token-protected (api_auth) and additionally loopback only: even with
        # ALLOW_REMOTE_ACCESS=true nobody on the LAN may stop it.
        if request.remote not in ("127.0.0.1", "::1"):
            return web.json_response({"error": "forbidden"}, status=403, headers=CORS_HEADERS)
        logger.warning("Shutdown requested by the app (backend code on disk is newer than this process).")
        asyncio.get_running_loop().call_later(0.3, lambda: os._exit(0))
        return web.json_response({"ok": True}, headers=CORS_HEADERS)

    async def _health(request):
        return web.Response(text="ok", headers=CORS_HEADERS)

    async def _events(request):
        category = request.query.get("category", "").strip()
        snapshot = store.snapshot()
        if category:
            snapshot = [e for e in snapshot if e.get("category") == category]
        _refresh_stale_sources()
        return web.json_response(snapshot, headers=CORS_HEADERS)

    async def _localnews(request):
        city = request.query.get("city", "").strip()
        country = request.query.get("country", "").strip()
        if not city:
            return web.json_response({"error": "missing 'city' query parameter"}, status=400, headers=CORS_HEADERS)
        try:
            # fetch_local_news has its own fallback (stories from the loaded
            # world-news feeds) and only raises if that is empty too.
            result = await fetch_local_news(localnews_session, city, country)
            return web.json_response({"city": city, "country": country, **result}, headers=CORS_HEADERS)
        except Exception as ex:
            logger.warning("Local news lookup failed for %s, %s: %s", city, country, ex)
            return web.json_response({"error": str(ex)}, status=502, headers=CORS_HEADERS)

    async def _assess(request):
        if not assess.is_configured():
            return web.json_response(
                {"error": "OPENROUTER_API_KEY is not set in backend/.env — see .env.example."},
                status=503, headers=CORS_HEADERS,
            )
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid JSON body"}, status=400, headers=CORS_HEADERS)

        category = str(body.get("category", "")).strip()
        title = str(body.get("title", "")).strip()
        summary = str(body.get("summary", "")).strip()
        source = str(body.get("source", "")).strip()
        if not title:
            return web.json_response({"error": "missing 'title' in body"}, status=400, headers=CORS_HEADERS)

        # 2026-09-24: look the event up by id so the model gets real context
        # (full article text, location, related live events — see
        # assess_context.py) instead of only the one-line teaser.
        snapshot = store.snapshot()
        event = next((e for e in snapshot if e.get("id") == body.get("id")), None)

        # 2026-09-25: the same marker assessed again within ASSESS_REUSE_SECONDS
        # gets the stored result (clearly flagged "cached": true, original
        # provenance/time kept, no new cost) instead of another paid call.
        cache_key = f"assess:{event['id']}" if event is not None else None
        if cache_key and ASSESS_REUSE_SECONDS > 0:
            cached = persistence.find_recent_ai_output(db_store, "assess", cache_key, ASSESS_REUSE_SECONDS)
            if cached and cached.get("output_text") and isinstance(cached.get("provenance"), dict):
                generated_at = str(cached["provenance"].get("generated_at") or cached["ts"])
                earlier_context = list((cached.get("input") or {}).get("context_used") or [])
                logger.info("Assess request for %r answered from the stored result of %s", title, generated_at)
                return web.json_response({
                    "assessment": cached["output_text"],
                    "provenance": cached["provenance"],
                    "context_used": [f"cached result from {generated_at[11:16]} UTC (no new AI call)"] + earlier_context,
                    "usage": {"cost_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0},
                    "cached": True,
                    "cached_at": cached["ts"],
                }, headers=CORS_HEADERS)

        context, context_used = "", []
        if event is not None:
            source = event.get("source") or source
            if event.get("url"):
                source += f" ({event['url']})"
            try:
                context, context_used = await assess_context.build_context(localnews_session, event, snapshot)
            except Exception as ex:
                logger.warning("Could not build assess context for %r: %s", title, ex)

        logger.info("Assess request: category=%r title=%r context=%s (%d chars)", category, title, context_used, len(context))
        try:
            text, model, usage = await assess.assess_marker(category, title, summary, source, context)
            if not text:
                return web.json_response({"error": "The model returned an empty response."}, status=502, headers=CORS_HEADERS)
            logger.info("Assess request succeeded for %r (model=%s)", title, model)
            # "provenance" = machine-readable AI marking (EU AI Act Art. 50), see assess.provenance().
            prov = assess.provenance("assess", model)
            db_store.save_ai_output("assess", model, text, provenance=prov, usage=usage, input_data={
                "summary": f"[{category}] {title} (id={body.get('id')})", "cache_key": cache_key,
                "event_id": body.get("id"), "context_used": context_used})
            return web.json_response(
                {"assessment": text, "provenance": prov, "context_used": context_used,
                 "usage": usage, "cached": False},  # usage = assess.usage_info(): cost_usd/prompt_tokens/completion_tokens
                headers=CORS_HEADERS,
            )
        except Exception as ex:
            logger.warning("AI assessment failed for %r: %s", title, ex)
            return web.json_response({"error": str(ex)}, status=502, headers=CORS_HEADERS)

    async def _localnews_summary(request):
        """POST /localnews-summary {city, country} — the city panel's
        "✦ AI Sum up local news" button. Reuses the (cached) /localnews
        lookup, with up to SUMMARY_MAX_RECORDS headlines instead of the 8
        shown in the panel."""
        if not assess.is_configured():
            return web.json_response(
                {"error": "OPENROUTER_API_KEY is not set in backend/.env — see .env.example."},
                status=503, headers=CORS_HEADERS,
            )
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid JSON body"}, status=400, headers=CORS_HEADERS)
        city = str(body.get("city", "")).strip()
        country = str(body.get("country", "")).strip()
        if not city:
            return web.json_response({"error": "missing 'city' in body"}, status=400, headers=CORS_HEADERS)
        try:
            news = await fetch_local_news(localnews_session, city, country, limit=SUMMARY_MAX_RECORDS)
        except Exception as ex:
            return web.json_response({"error": f"No local news to summarize: {ex}"}, status=502, headers=CORS_HEADERS)
        articles = news.get("articles") or []
        if not articles:
            return web.json_response({"error": "No local news to summarize."}, status=409, headers=CORS_HEADERS)
        logger.info("Local news summary request: %s, %s (%d headlines)", city, country, len(articles))
        try:
            text, model, usage = await assess.summarize_local_news(city, country, articles)
            if not text:
                return web.json_response({"error": "The model returned an empty response."}, status=502, headers=CORS_HEADERS)
            prov = assess.provenance("localnews-summary", model)
            db_store.save_ai_output("localnews-summary", model, text, provenance=prov, usage=usage, input_data={
                "summary": f"{city}, {country}: {len(articles)} headlines", "city": city, "country": country,
                "headlines": len(articles)})
            return web.json_response({
                "summary": text,
                "provenance": prov,
                "headlines": len(articles),
                "usage": usage,  # assess.usage_info(): cost_usd/prompt_tokens/completion_tokens
            }, headers=CORS_HEADERS)
        except Exception as ex:
            logger.warning("Local news summary failed for %s: %s", city, ex)
            return web.json_response({"error": str(ex)}, status=502, headers=CORS_HEADERS)

    health_app = web.Application(middlewares=[api_auth.http_middleware(API_ORIGINS)])
    health_app.router.add_get("/health", _health)
    health_app.router.add_get("/version", _version)
    health_app.router.add_post("/shutdown", _shutdown)
    health_app.router.add_get("/events", _events)
    health_app.router.add_get("/localnews", _localnews)
    health_app.router.add_post("/assess", _assess)
    health_app.router.add_post("/localnews-summary", _localnews_summary)
    # OPTIONS preflights are answered by the api_auth middleware; this route only makes them routable.
    health_app.router.add_route("OPTIONS", "/{tail:.*}", lambda request: web.Response())
    # access_log_class default already logs one line per request (method,
    # path, status, timing) — explicitly naming the logger here just makes
    # sure it's the same "aiohttp.access" logger configure_logging() already
    # gave a file handler to, so every request (including an OPTIONS
    # preflight for /assess, or its absence) ends up in data/logs/backend.log
    # too, not just the console.
    health_runner = web.AppRunner(health_app, access_log=logging.getLogger("aiohttp.access"))
    await health_runner.setup()
    health_site = web.TCPSite(health_runner, api_auth.bind_host(HTTP_HOST, "HTTP API"), HEALTH_PORT)
    await health_site.start()
    api_auth.publish_token()  # only now: the port is ours (see api_auth.init_token)
    logger.info("HTTP API listening on %s (/health, /events, /localnews, /assess, /localnews-summary; "
                "token required except /health, /version)", health_site.name)

    # --- Collectors ---
    def make_emit(name):
        def emit(event):
            if store.add(event):
                log_event("event", event)
                asyncio.create_task(broadcaster.broadcast("event", event))
                persistence.record_event(db_store, event)
            elif event.get("category") == "flight" or store.contains(event["id"]):
                # Known and still shown: refresh the stored copy (latest
                # quote/last-seen time; flights: every polled position is a
                # track point). Pruned ids are skipped so an ended alert
                # doesn't come back after a restart.
                persistence.record_event(db_store, event)
        return emit

    def make_report_status(name):
        async def report_status(ok: bool, message: str):
            status = {"source": name, "ok": ok, "message": message, "timestamp": store_now()}
            source_status[name] = status
            refresh.note_attempt(name)
            if ok:
                refresh.note_success(name)
                db_store.mark_success(name, status=status)
            else:
                db_store.mark_attempt(name, status=status)
            log_event("source_status", status)
            await broadcaster.broadcast("source_status", status)
        return report_status

    def store_now():
        from events import now_iso
        return now_iso()

    tasks = []
    for name, module in COLLECTORS.items():
        emit = make_emit(name)
        report_status = make_report_status(name)
        tasks.append(asyncio.create_task(_run_collector_forever(name, module, emit, report_status)))

    async def _prune_past_disasters_forever():
        # Disaster alerts that were current when first seen age out while
        # the app keeps running; remove them from the store and tell every
        # connected frontend to drop them ("remove" message, globe.html).
        while True:
            await asyncio.sleep(600)
            removed = store.prune(lambda e: e.get("category") == "disaster" and _disaster_is_past(e))
            if removed:
                logger.info("Pruned %d past disaster alert(s)", len(removed))
                persistence.forget_events(db_store, removed)
                await broadcaster.broadcast("remove", removed)

    tasks.append(asyncio.create_task(_prune_past_disasters_forever()))

    async def _prune_local_store_daily():
        # Retention while running (the startup run covers restarts), see
        # persistence.apply_retention() / config.py.
        while True:
            await asyncio.sleep(DB_PRUNE_INTERVAL_SECONDS)
            try:
                logger.info("Local storage daily retention: %s", persistence.apply_retention(db_store))
            except Exception as ex:
                logger.warning("Local storage daily retention failed: %s", ex)

    tasks.append(asyncio.create_task(_prune_local_store_daily()))

    logger.info("Started %d collector(s): %s", len(COLLECTORS), ", ".join(COLLECTORS.keys()))

    try:
        await asyncio.gather(*tasks)
    finally:
        await localnews_session.close()
        db_store.close()


async def _run_collector_forever(name, module, emit, report_status) -> None:
    """Wraps a collector's run() so one crashing doesn't take the process down —
    it logs the failure, reports it as a source-status error, and retries after
    a short delay instead of propagating the exception to asyncio.gather.
    """
    while True:
        try:
            await module.run(emit, report_status)
            # A well-behaved collector's run() loops forever; if it returns,
            # treat that as a (rare) clean stop and don't spin a hot retry loop.
            logger.warning("Collector %s returned instead of looping forever; restarting in 60s", name)
        except Exception as ex:
            logger.exception("Collector %s crashed: %s", name, ex)
            try:
                await report_status(False, f"crashed: {ex}")
            except Exception:
                pass
        await asyncio.sleep(60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down.")
