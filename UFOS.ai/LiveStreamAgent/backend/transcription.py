"""Streams PCM audio to Deepgram and emits finalized transcript segments onto a queue."""
import asyncio
import logging
from collections.abc import AsyncIterator

from deepgram import AsyncDeepgramClient
from deepgram.core.events import EventType

import credstore
from config import AUDIO_SAMPLE_RATE

KEY_NAME = "deepgram-api-key"            # Credential Manager (⚙ Keys & data), stage 10 S10-E

logger = logging.getLogger(__name__)


async def transcribe_stream(
    audio_chunks: AsyncIterator[bytes],
    transcript_queue: "asyncio.Queue[str]",
) -> None:
    """Consumes PCM chunks and pushes each finalized transcript segment to transcript_queue. Raises
    credstore.SecretMissing / SecretStoreUnavailable (user-facing message) when the Deepgram key can't be read."""
    client = AsyncDeepgramClient(api_key=credstore.require_secret(KEY_NAME))

    async with client.listen.v1.connect(
        model="nova-2",
        language="en-US",
        encoding="linear16",
        sample_rate=AUDIO_SAMPLE_RATE,
        smart_format=True,
        interim_results=True,
    ) as socket:

        async def on_message(message) -> None:
            if isinstance(message, bytes) or not getattr(message, "is_final", False):
                return
            alternatives = message.channel.alternatives
            text = alternatives[0].transcript.strip() if alternatives else ""
            if text:
                await transcript_queue.put(text)

        async def on_error(exc) -> None:
            logger.error("Deepgram error: %s", exc)

        socket.on(EventType.MESSAGE, on_message)
        socket.on(EventType.ERROR, on_error)

        listen_task = asyncio.create_task(socket.start_listening())
        try:
            async for chunk in audio_chunks:
                await socket.send_media(chunk)
        finally:
            await socket.send_close_stream()
            await listen_task
