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
from broadcast import Broadcaster
from config import HEALTH_PORT
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

CORS_HEADERS = {"Access-Control-Allow-Origin": "*"}
# A POST with a JSON body (the Assess button, see /assess below) is not a
# CORS "simple request" — Content-Type: application/json triggers a browser
# preflight OPTIONS first, and if that preflight response doesn't explicitly
# allow the method/headers, the browser blocks the real request before it
# ever reaches this server. CORS_HEADERS alone (just Allow-Origin) was
# enough for GET requests without custom headers (health/events/localnews),
# which is why those worked while POST /assess silently failed even with a
# valid OPENROUTER_API_KEY configured. This is what the preflight needs.
PREFLIGHT_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "3600",
}


async def main() -> None:
    store = EventStore()
    broadcaster = Broadcaster()
    source_status: dict[str, dict] = {
        name: {"source": name, "ok": None, "message": "starting…", "timestamp": None} for name in COLLECTORS
    }

    async def _on_connect(ws):
        await broadcaster.send_to(ws, "snapshot", store.snapshot())
        for status in source_status.values():
            await broadcaster.send_to(ws, "source_status", status)

    broadcaster.on_connect = _on_connect
    await broadcaster.start()

    # --- HTTP API ---
    localnews_session = aiohttp.ClientSession()

    async def _version(request):
        return web.json_response({"code_mtime": CODE_MTIME, "started_at": STARTED_AT, "pid": os.getpid()},
                                 headers=CORS_HEADERS)

    async def _shutdown(request):
        # Loopback only: the HTTP API listens on 0.0.0.0, and nobody on the
        # LAN should be able to stop it.
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
            return web.json_response(
                {"assessment": text, "provenance": assess.provenance("assess", model), "context_used": context_used,
                 "usage": usage},  # usage = assess.usage_info(): cost_usd/prompt_tokens/completion_tokens
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
            return web.json_response({
                "summary": text,
                "provenance": assess.provenance("localnews-summary", model),
                "headlines": len(articles),
                "usage": usage,  # assess.usage_info(): cost_usd/prompt_tokens/completion_tokens
            }, headers=CORS_HEADERS)
        except Exception as ex:
            logger.warning("Local news summary failed for %s: %s", city, ex)
            return web.json_response({"error": str(ex)}, status=502, headers=CORS_HEADERS)

    async def _options(request):
        return web.Response(headers=PREFLIGHT_HEADERS)

    health_app = web.Application()
    health_app.router.add_get("/health", _health)
    health_app.router.add_get("/version", _version)
    health_app.router.add_post("/shutdown", _shutdown)
    health_app.router.add_get("/events", _events)
    health_app.router.add_get("/localnews", _localnews)
    health_app.router.add_post("/assess", _assess)
    health_app.router.add_post("/localnews-summary", _localnews_summary)
    health_app.router.add_route("OPTIONS", "/{tail:.*}", _options)
    # access_log_class default already logs one line per request (method,
    # path, status, timing) — explicitly naming the logger here just makes
    # sure it's the same "aiohttp.access" logger configure_logging() already
    # gave a file handler to, so every request (including an OPTIONS
    # preflight for /assess, or its absence) ends up in data/logs/backend.log
    # too, not just the console.
    health_runner = web.AppRunner(health_app, access_log=logging.getLogger("aiohttp.access"))
    await health_runner.setup()
    health_site = web.TCPSite(health_runner, "127.0.0.1", HEALTH_PORT)
    await health_site.start()
    logger.info("HTTP API listening on http://127.0.0.1:%s (/health, /events, /localnews, /assess, /localnews-summary)", HEALTH_PORT)

    # --- Collectors ---
    def make_emit(name):
        def emit(event):
            if store.add(event):
                log_event("event", event)
                asyncio.create_task(broadcaster.broadcast("event", event))
        return emit

    def make_report_status(name):
        async def report_status(ok: bool, message: str):
            status = {"source": name, "ok": ok, "message": message, "timestamp": store_now()}
            source_status[name] = status
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
                await broadcaster.broadcast("remove", removed)

    tasks.append(asyncio.create_task(_prune_past_disasters_forever()))

    logger.info("Started %d collector(s): %s", len(tasks) - 1, ", ".join(COLLECTORS.keys()))

    try:
        await asyncio.gather(*tasks)
    finally:
        await localnews_session.close()


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
