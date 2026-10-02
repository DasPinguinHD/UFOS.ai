"""Entrypoint: wires audio ingestion, transcription, market data, LLM analysis, and broadcast.

2026-09-25: session-spanning local storage (storage.py, SQLite at
data/livestream.db): every AI verdict/analysis (saved by llm_analyst next to
the cost ledger, 90 days), transcript segments (7 days), the latest quote per
symbol (2 days) and market price points (2 years). Retention runs at start and
daily. The recent verdict history is restored at start, sent to every newly
connected WebSocket client as a "verdict_history" message (not yet read by the
WPF window - follow-up there) and served read-only at GET /verdicts and
GET /history/ai.
"""
import asyncio
import collections
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from aiohttp import web

from audio_stream import pcm_chunks
from broadcast import Broadcaster
from config import (YOUTUBE_URL, VERDICT_INTERVAL_SECONDS, VERDICT_MAX_REASON_CHARS, VERDICT_INITIAL_DELAY_SECONDS,
                    MARKET_DATA_POLL_SECONDS)
from config import HTTP_HOST, DEFAULT_ALLOWED_ORIGINS
import api_auth
from llm_analyst import LLMAnalyst
from logger import log_event
from market_data import MarketDataFeed, REFRESH_SOURCE as MARKET_REFRESH_SOURCE
import refresh
import storage
from transcription import transcribe_stream

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# Same cap as the WPF window's in-memory verdict history (LivestreamAgent.xaml.cs).
VERDICT_HISTORY_MAX = 500
# Verdicts sent to a newly connected WebSocket client ("verdict_history" message).
WARM_START_VERDICTS = 20
STORE_RETENTION_INTERVAL_SECONDS = 24 * 3600
# Quotes are persisted at most this often (the feed polls every MARKET_DATA_POLL_SECONDS).
QUOTE_STORE_INTERVAL_SECONDS = 60
# A price point (prices table, kept 2 years) per symbol at most this often, and
# only when the price changed - keeps 17 symbols x 2 years at well under 1M rows.
PRICE_POINT_INTERVAL_SECONDS = 300
PRICE_SOURCE = "yahoo"
# symbol -> (unix time, price) of the last stored price point
_last_price_point: dict = {}
# A cached market snapshot older than this triggers an early poll when a client connects.
MARKET_STALE_SECONDS = 2 * MARKET_DATA_POLL_SECONDS


def _verdict_from_output(entry: dict):
    """Rebuild a broadcast verdict payload from a stored ai_outputs row (None if unusable)."""
    try:
        payload = json.loads(entry.get("output_text") or "")
    except Exception:
        return None
    if not isinstance(payload, dict) or not payload.get("verdict"):
        return None
    if isinstance(entry.get("provenance"), dict):
        payload["provenance"] = entry["provenance"]
    if isinstance(entry.get("usage"), dict):
        payload["usage"] = entry["usage"]
    return payload


def _load_verdict_history(store: "storage.Storage", limit: int) -> list:
    """Stored verdicts (newest first) in the same shape as the live "verdict" messages."""
    rows = store.list_ai_outputs(limit=limit, feature="verdict", include_input=False)
    return [v for v in (_verdict_from_output(r) for r in rows) if v is not None]


def _store_transcript(store: "storage.Storage | None", segment: str) -> None:
    """Keep a transcript segment (retention storage.TRANSCRIPT_RETENTION_DAYS). Never raises."""
    if store is None or not segment:
        return
    try:
        now = storage.utc_now()
        digest = hashlib.sha1(segment.encode("utf-8")).hexdigest()[:12]
        store.upsert_items(storage.SOURCE_TRANSCRIPT, [{
            "item_id": f"{now.strftime('%Y%m%dT%H%M%S.%f')}-{digest}",
            "item_ts": now,
            "payload": {"text": segment},
        }], fetched_ts=now)
    except Exception as ex:
        logger.warning("Could not store transcript segment: %s", ex)


def _store_quotes(store: "storage.Storage | None", payload: dict) -> None:
    """Latest quote per symbol (items, warm start) + price points (prices). Never raises."""
    if store is None:
        return
    try:
        quotes = payload.get("quotes") or {}
        priced = {sym: q for sym, q in quotes.items() if isinstance(q, dict) and q.get("price") is not None}
        if not priced:
            store.mark_failure("quotes", "no prices in snapshot")
            return
        ts = payload.get("timestamp")
        store.upsert_items(storage.SOURCE_QUOTE, [
            {"item_id": sym, "item_ts": ts, "payload": {"symbol": sym, "timestamp": ts, **q}}
            for sym, q in priced.items()
        ])
        now = time.time()
        points = []
        for sym, q in priced.items():
            last = _last_price_point.get(sym)
            if last is not None and (now - last[0] < PRICE_POINT_INTERVAL_SECONDS or last[1] == q["price"]):
                continue
            points.append((sym, ts or storage.utc_now(), q["price"], PRICE_SOURCE))
            _last_price_point[sym] = (now, q["price"])
        store.add_prices(points)
        store.mark_success("quotes", {"symbols": len(priced), "missing": len(quotes) - len(priced)})
    except Exception as ex:
        logger.warning("Could not store quotes: %s", ex)


def _snapshot_age_seconds(timestamp) -> "float | None":
    try:
        dt = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except (TypeError, ValueError):
        return None


def _market_payload(snapshot) -> dict:
    return {
        "timestamp": snapshot.timestamp,
        "quotes": {
            symbol: {"price": q.price, "change_percent": q.change_percent}
            for symbol, q in snapshot.quotes.items()
        },
    }


async def _run_transcription(transcript_queue: "asyncio.Queue[str]") -> None:
    await transcribe_stream(pcm_chunks(YOUTUBE_URL), transcript_queue)


async def _run_analysis_loop(
    transcript_queue: "asyncio.Queue[str]",
    market_feed: MarketDataFeed,
    analyst: LLMAnalyst,
    broadcaster: Broadcaster,
    store: "storage.Storage | None" = None,
) -> None:
    while True:
        segment = await transcript_queue.get()
        log_event("transcript", {"text": segment})
        await broadcaster.broadcast("transcript", {"text": segment})
        _store_transcript(store, segment)

        # If segments piled up while the previous LLM call was running, skip straight to the
        # newest one instead of working through a growing backlog of stale analysis.
        skipped = 0
        while not transcript_queue.empty():
            analyst.note(segment)
            segment = transcript_queue.get_nowait()
            skipped += 1
            log_event("transcript", {"text": segment})
            await broadcaster.broadcast("transcript", {"text": segment})
            _store_transcript(store, segment)
        if skipped:
            logger.warning("Analysis backlog: skipped %d stale segment(s), analyzing latest only", skipped)

        snapshot = await market_feed.get_snapshot()
        analysis = await analyst.analyze(segment, snapshot)
        log_event("analysis", analysis)
        # (the analyst stores every successful analysis itself, next to its cost-ledger entry)
        await broadcaster.broadcast("analysis", analysis)


async def _run_market_broadcast(market_feed: MarketDataFeed, broadcaster: Broadcaster,
                                store: "storage.Storage | None" = None) -> None:
    last_timestamp = None
    last_stored = 0.0
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(1)
        snapshot = await market_feed.get_snapshot()
        if snapshot.timestamp and snapshot.timestamp != last_timestamp:
            last_timestamp = snapshot.timestamp
            payload = _market_payload(snapshot)
            log_event("market_update", payload)
            await broadcaster.broadcast("market_update", payload)
            if loop.time() - last_stored >= QUOTE_STORE_INTERVAL_SECONDS:
                last_stored = loop.time()
                _store_quotes(store, payload)


async def _run_verdict_loop(analyst: LLMAnalyst, market_feed: MarketDataFeed, broadcaster: Broadcaster,
                            history=None) -> None:
    """Periodically request a short verdict from the analyst and broadcast it."""
    logger.info("Verdict loop started (interval=%s seconds)", VERDICT_INTERVAL_SECONDS)
    log_event("verdict_loop", {"action": "started", "interval": VERDICT_INTERVAL_SECONDS})
    # optional initial delay before first verdict so system can gather data; does not affect transcription
    try:
        if VERDICT_INITIAL_DELAY_SECONDS and VERDICT_INITIAL_DELAY_SECONDS > 0:
            logger.info("Delaying first verdict for %s seconds", VERDICT_INITIAL_DELAY_SECONDS)
            await asyncio.sleep(VERDICT_INITIAL_DELAY_SECONDS)
    except Exception:
        pass
    while True:
        try:
            # run immediately, then sleep at end of loop
            snapshot = await market_feed.get_snapshot()
            logger.debug("Requesting verdict from analyst...")
            verdict = await analyst.generate_verdict(snapshot, max_reason_chars=VERDICT_MAX_REASON_CHARS)
            # ensure we always log what we got for offline inspection
            log_event("verdict", verdict if isinstance(verdict, dict) else {"raw": str(verdict)})
            # A failed LLM call must never reach the UI dressed up as a verdict
            # (project convention "AI content labelling", rule 7) — log it only.
            if not isinstance(verdict, dict) or verdict.get("error") or not verdict.get("verdict"):
                logger.warning("Skipping broadcast of failed/empty verdict: %s", verdict)
            else:
                logger.info("Broadcasting verdict: %s", verdict)
                await broadcaster.broadcast("verdict", verdict)
                # (the analyst already stored it, next to its cost-ledger entry)
                if history is not None:
                    history.appendleft(verdict)
        except Exception as ex:
            logger.exception("Verdict loop error: %s", ex)
            try:
                log_event("verdict_error", {"error": str(ex)})
            except Exception:
                pass
        try:
            await asyncio.sleep(VERDICT_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            break


async def main() -> None:
    # Fresh session token for this start (published to data/api-token after binding).
    api_auth.init_token()
    if not YOUTUBE_URL:
        raise SystemExit("YOUTUBE_URL is not set; configure it in your .env file")

    # --- Local storage (storage.py): retention, then restore the recent verdict history ---
    # Storage never raises: an unusable file falls back to an in-memory DB.
    store = storage.Storage()
    pruned = store.run_retention()
    verdict_history = collections.deque(_load_verdict_history(store, VERDICT_HISTORY_MAX), maxlen=VERDICT_HISTORY_MAX)
    logger.info("Local storage%s: pruned %s, restored %d verdict(s) from the last %d days",
                " (in-memory fallback)" if store.in_memory else "", pruned, len(verdict_history),
                storage.AI_OUTPUT_RETENTION_DAYS)

    transcript_queue: "asyncio.Queue[str]" = asyncio.Queue()
    market_feed = MarketDataFeed()
    analyst = LLMAnalyst(store=store)
    broadcaster = Broadcaster()

    async def _on_client_connect() -> list:
        """Warm start for a newly connected WebSocket client (stale-while-revalidate):
        cached data is sent at once; a stale market snapshot triggers an early poll.
        "verdict_history" is currently ignored by the WPF window (follow-up there)."""
        messages = [("verdict_history", {
            "verdicts": list(verdict_history)[:WARM_START_VERDICTS],  # newest first
            "source": "local_storage",
        })]
        snapshot = await market_feed.get_snapshot()
        age = _snapshot_age_seconds(snapshot.timestamp) if snapshot.timestamp else None
        if snapshot.timestamp and snapshot.quotes:
            messages.append(("market_update", _market_payload(snapshot)))
        if age is None or age > MARKET_STALE_SECONDS:
            refresh.request_refresh(MARKET_REFRESH_SOURCE)
        return messages

    broadcaster.on_connect = _on_client_connect

    await broadcaster.start()

    # start a small health endpoint so external scripts can verify backend readiness
    async def _health(request):
        return web.Response(text="ok")

    async def _verdicts(request):
        # Read-only verdict history (newest first), incl. verdicts from before a
        # restart. The WPF window keeps its own history from the "verdict"
        # WebSocket messages and doesn't call this.
        try:
            limit = max(1, min(VERDICT_HISTORY_MAX, int(request.query.get("limit", "100"))))
        except ValueError:
            limit = 100
        return web.json_response(list(verdict_history)[:limit])

    async def _history_ai(request):
        # Read-only stored AI outputs (verdicts + analyses), newest first.
        # ?limit=1..500 (default 50) &feature=verdict|analysis &include_input=1
        try:
            limit = max(1, min(500, int(request.query.get("limit", "50"))))
        except ValueError:
            limit = 50
        feature = request.query.get("feature") or None
        include_input = request.query.get("include_input", "0").lower() in ("1", "true", "yes")
        rows = await asyncio.to_thread(store.list_ai_outputs, limit, feature, None, include_input)
        return web.json_response(rows, dumps=lambda o: json.dumps(o, ensure_ascii=False, default=str))

    async def _retention_daily():
        while True:
            await asyncio.sleep(STORE_RETENTION_INTERVAL_SECONDS)
            logger.info("Local storage daily retention: %s", await asyncio.to_thread(store.run_retention))

    # Token + origin check on every route except /health (api_auth).
    health_app = web.Application(middlewares=[api_auth.http_middleware(api_auth.allowed_origins(DEFAULT_ALLOWED_ORIGINS))])
    health_app.router.add_get('/health', _health)
    health_app.router.add_get('/verdicts', _verdicts)
    health_app.router.add_get('/history/ai', _history_ai)
    health_runner = web.AppRunner(health_app)
    await health_runner.setup()
    health_site = web.TCPSite(health_runner, api_auth.bind_host(HTTP_HOST, "HTTP API"), 8766)
    await health_site.start()
    api_auth.publish_token()  # only now: the port is ours (see api_auth.init_token)
    logger.info("HTTP API listening on %s (/health; /verdicts, /history/ai need the token)", health_site.name)

    # Set a loop-level exception handler so unhandled exceptions are logged instead of silently killing the process.
    loop = asyncio.get_event_loop()
    def _handle_loop_exception(loop, context):
        try:
            logger.exception("Unhandled exception in event loop: %s", context.get("message"))
        except Exception:
            pass
    try:
        loop.set_exception_handler(_handle_loop_exception)
    except Exception:
        pass

    async def _supervise(coro_factory, name: str):
        """Run a coroutine factory repeatedly; on exception log and restart after short delay."""
        while True:
            try:
                logger.info("Starting supervised task: %s", name)
                await coro_factory()
                # if the coroutine returns normally, break out
                logger.info("Supervised task %s returned normally", name)
                break
            except asyncio.CancelledError:
                logger.info("Supervised task %s cancelled", name)
                break
            except Exception as ex:
                logger.exception("Supervised task %s crashed: %s", name, ex)
                try:
                    log_event("task_crash", {"task": name, "error": str(ex)})
                except Exception:
                    pass
                # small backoff before restart
                try:
                    await asyncio.sleep(2)
                except asyncio.CancelledError:
                    break

    tasks = [
        asyncio.create_task(_supervise(market_feed.run, "market_data")),
        asyncio.create_task(_supervise(lambda: _run_market_broadcast(market_feed, broadcaster, store), "market_broadcast")),
        asyncio.create_task(_supervise(lambda: _run_transcription(transcript_queue), "transcription")),
        asyncio.create_task(_supervise(lambda: _run_analysis_loop(transcript_queue, market_feed, analyst, broadcaster, store), "analysis")),
        asyncio.create_task(_supervise(lambda: _run_verdict_loop(analyst, market_feed, broadcaster, verdict_history), "verdict_loop")),
        asyncio.create_task(_supervise(_retention_daily, "local_storage_retention")),
    ]

    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            try: task.cancel()
            except Exception: pass
        await asyncio.gather(*tasks, return_exceptions=True)
        await broadcaster.stop()
        store.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down.")
