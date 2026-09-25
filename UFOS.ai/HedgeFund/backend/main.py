"""HedgeFund backend.

Two parts:
- WebSocket (WS_HOST:WS_PORT): placeholder for the multi-agent pipeline
  (unchanged; agents will publish signals/verdicts here later).
- HTTP API on 127.0.0.1:HEALTH_PORT, used by the WPF window:
    GET  /health                    -> "ok"
    GET  /version                   -> {"code_mtime", "started_at", "pid"} (stale-backend check)
    POST /shutdown                  -> exit (loopback only; used to replace a stale process)
    GET  /newsroom/status           -> per-source status of the Financial Newsroom collectors
    GET  /newsroom/insider          -> recent SEC Form 4 filings (newest first)
    GET  /newsroom/macro            -> latest FRED macro series with history
    GET  /newsroom/calendar         -> upcoming US releases (FRED) + FOMC decisions, next 45 days
    POST /newsroom/insider-summary  -> AI summary of the most notable trades (+ provenance)
    GET  /newsroom/overview         -> "Analysis & Reasoning" tiles + templates + model ids per tier
    POST /newsroom/analysis         -> AI analysis of the selected tiles (+ provenance)

The Financial Newsroom (package newsroom/) was moved here from the Global
Monitoring module on 2026-09-24: insider trades (SEC EDGAR) and macro
indicators (FRED). See newsroom/__init__.py for how to add a section.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone

from aiohttp import web
import websockets

from logging_setup import configure_logging

configure_logging()

from config import WS_HOST, WS_PORT, HEALTH_PORT
from newsroom import ai as newsroom_ai
from newsroom import insider_summary
from newsroom import fred_collector, sec_edgar_collector, macro_derived
from newsroom import calendar_collector
from newsroom import overview as newsroom_overview
from newsroom.store import EventStore, now_iso

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


async def _ws_handler(websocket):
    async for _ in websocket:
        pass


async def _run_collector_forever(name, module, emit, report_status) -> None:
    """One crashing collector must not take the process down: log, report,
    retry after 60s."""
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
    insider_store = EventStore()
    macro_latest: dict = {}   # "macro-<SERIES>" -> latest event (overwritten each poll)
    calendar_latest: dict = {}  # latest calendar_collector poll: {"events", "updated", ...}
    status = {name: {"source": name, "label": SOURCE_LABELS[name], "ok": None,
                     "message": "starting…", "timestamp": None} for name in NEWSROOM_SOURCES}

    def make_report_status(name):
        async def report_status(ok: bool, message: str):
            status[name] = {"source": name, "label": SOURCE_LABELS[name], "ok": ok,
                            "message": message, "timestamp": now_iso()}
        return report_status

    emitters = {
        "sec_edgar": lambda event: insider_store.add(event),
        "fred": lambda event: macro_latest.__setitem__(event["id"], event),
        "calendar": lambda payload: (calendar_latest.clear(), calendar_latest.update(payload)),
    }

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
        return web.json_response(list(status.values()))

    async def _newsroom_insider(request):
        events = [e for e in insider_store.snapshot() if e.get("category") == "insider"]
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
        insider_events = [e for e in insider_store.snapshot() if e.get("category") == "insider"]
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
            return web.json_response({
                "summary": text, "filings": used, "usage": usage,
                "provenance": newsroom_ai.provenance("insider-summary", model),
            })
        except Exception as ex:
            logger.warning("Insider summary failed: %s", ex)
            return web.json_response({"error": str(ex)}, status=502)

    # --- Analysis & Reasoning (2026-09-25) ---
    def _build_overview():
        insider_events = [e for e in insider_store.snapshot() if e.get("category") == "insider"]
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
            return web.json_response({
                "analysis": text, "tiles_used": len(selected), "tier": tier,
                "provenance": newsroom_ai.provenance("analysis", model),
                # What OpenRouter charged for this one request (credits = USD); None if not reported.
                "usage": usage,
            })
        except Exception as ex:
            logger.warning("Analysis failed: %s", ex)
            return web.json_response({"error": str(ex)}, status=502)

    app = web.Application()
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
    # No per-request access log: the newsroom window polls every 10s, which
    # would bury everything else in backend.log.
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", HEALTH_PORT)
    await site.start()
    logger.info("HTTP API listening on http://127.0.0.1:%s (/health, /newsroom/insider, /newsroom/macro, "
                "/newsroom/calendar, /newsroom/insider-summary, /newsroom/overview, /newsroom/analysis, "
                "/newsroom/status)", HEALTH_PORT)

    tasks = [asyncio.create_task(_run_collector_forever(name, module, emitters[name], make_report_status(name)))
             for name, module in NEWSROOM_SOURCES.items()]
    logger.info("Started Financial Newsroom collectors: %s", ", ".join(NEWSROOM_SOURCES))

    async with websockets.serve(_ws_handler, WS_HOST, WS_PORT):
        logger.info("WebSocket listening on ws://%s:%s", WS_HOST, WS_PORT)
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down.")
