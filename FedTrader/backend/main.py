"""Entrypoint: wires audio ingestion, transcription, market data, LLM analysis, and broadcast."""
import asyncio
import logging

from audio_stream import pcm_chunks
from broadcast import Broadcaster
from config import YOUTUBE_URL
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


async def main() -> None:
    if not YOUTUBE_URL:
        raise SystemExit("YOUTUBE_URL is not set; configure it in your .env file")

    transcript_queue: "asyncio.Queue[str]" = asyncio.Queue()
    market_feed = MarketDataFeed()
    analyst = LLMAnalyst()
    broadcaster = Broadcaster()

    await broadcaster.start()

    tasks = [
        asyncio.create_task(market_feed.run(), name="market_data"),
        asyncio.create_task(_run_market_broadcast(market_feed, broadcaster), name="market_broadcast"),
        asyncio.create_task(_run_transcription(transcript_queue), name="transcription"),
        asyncio.create_task(
            _run_analysis_loop(transcript_queue, market_feed, analyst, broadcaster), name="analysis"
        ),
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
