"""Entrypoint: wires audio ingestion, transcription, market data, LLM analysis, and broadcast."""
import asyncio
import logging
from aiohttp import web

from audio_stream import pcm_chunks
from broadcast import Broadcaster
from config import YOUTUBE_URL, VERDICT_INTERVAL_SECONDS, VERDICT_MAX_REASON_CHARS, VERDICT_INITIAL_DELAY_SECONDS
from llm_analyst import LLMAnalyst
from logger import log_event
from market_data import MarketDataFeed
from transcription import transcribe_stream

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


async def _run_transcription(transcript_queue: "asyncio.Queue[str]") -> None:
    await transcribe_stream(pcm_chunks(YOUTUBE_URL), transcript_queue)


async def _run_analysis_loop(
    transcript_queue: "asyncio.Queue[str]",
    market_feed: MarketDataFeed,
    analyst: LLMAnalyst,
    broadcaster: Broadcaster,
) -> None:
    while True:
        segment = await transcript_queue.get()
        log_event("transcript", {"text": segment})
        await broadcaster.broadcast("transcript", {"text": segment})

        # If segments piled up while the previous LLM call was running, skip straight to the
        # newest one instead of working through a growing backlog of stale analysis.
        skipped = 0
        while not transcript_queue.empty():
            analyst.note(segment)
            segment = transcript_queue.get_nowait()
            skipped += 1
            log_event("transcript", {"text": segment})
            await broadcaster.broadcast("transcript", {"text": segment})
        if skipped:
            logger.warning("Analysis backlog: skipped %d stale segment(s), analyzing latest only", skipped)

        snapshot = await market_feed.get_snapshot()
        analysis = await analyst.analyze(segment, snapshot)
        log_event("analysis", analysis)
        await broadcaster.broadcast("analysis", analysis)


async def _run_market_broadcast(market_feed: MarketDataFeed, broadcaster: Broadcaster) -> None:
    last_timestamp = None
    while True:
        await asyncio.sleep(1)
        snapshot = await market_feed.get_snapshot()
        if snapshot.timestamp and snapshot.timestamp != last_timestamp:
            last_timestamp = snapshot.timestamp
            payload = {
                "timestamp": snapshot.timestamp,
                "quotes": {
                    symbol: {"price": q.price, "change_percent": q.change_percent}
                    for symbol, q in snapshot.quotes.items()
                },
            }
            log_event("market_update", payload)
            await broadcaster.broadcast("market_update", payload)


async def _run_verdict_loop(analyst: LLMAnalyst, market_feed: MarketDataFeed, broadcaster: Broadcaster) -> None:
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
            logger.info("Broadcasting verdict: %s", verdict)
            await broadcaster.broadcast("verdict", verdict)
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
    if not YOUTUBE_URL:
        raise SystemExit("YOUTUBE_URL is not set; configure it in your .env file")

    transcript_queue: "asyncio.Queue[str]" = asyncio.Queue()
    market_feed = MarketDataFeed()
    analyst = LLMAnalyst()
    broadcaster = Broadcaster()

    await broadcaster.start()

    # start a small health endpoint so external scripts can verify backend readiness
    async def _health(request):
        return web.Response(text="ok")

    health_app = web.Application()
    health_app.router.add_get('/health', _health)
    health_runner = web.AppRunner(health_app)
    await health_runner.setup()
    health_site = web.TCPSite(health_runner, '127.0.0.1', 8766)
    await health_site.start()
    logger.info("Health endpoint listening on http://127.0.0.1:8766/health")

    tasks = [
        asyncio.create_task(market_feed.run(), name="market_data"),
        asyncio.create_task(_run_market_broadcast(market_feed, broadcaster), name="market_broadcast"),
        asyncio.create_task(_run_transcription(transcript_queue), name="transcription"),
        asyncio.create_task(
            _run_analysis_loop(transcript_queue, market_feed, analyst, broadcaster), name="analysis"
        ),
        asyncio.create_task(_run_verdict_loop(analyst, market_feed, broadcaster), name="verdict_loop"),
    ]

    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await broadcaster.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down.")
