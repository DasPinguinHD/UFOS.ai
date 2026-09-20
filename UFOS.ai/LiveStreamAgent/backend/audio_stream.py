"""Resolves a live YouTube stream to a direct URL and transcodes it to raw PCM via ffmpeg."""
import asyncio
import logging
import shutil
from collections.abc import AsyncIterator

import yt_dlp

from config import AUDIO_CHANNELS, AUDIO_SAMPLE_RATE

logger = logging.getLogger(__name__)

CHUNK_SIZE = 4096  # bytes read per PCM chunk (~64ms of 16kHz mono 16-bit audio)


def resolve_stream_url(youtube_url: str) -> str:
    """Returns a direct, playable audio stream URL for a live YouTube broadcast."""
    ydl_opts: dict = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
        info = ydl.extract_info(youtube_url, download=False)
        url = info.get("url") if info else None
        if not url:
            raise RuntimeError(f"yt-dlp could not resolve a direct stream URL for {youtube_url}")
        return url


async def pcm_chunks(youtube_url: str) -> AsyncIterator[bytes]:
    """Yields raw PCM16 mono audio chunks from the live stream until it ends or is cancelled."""
    stream_url = await asyncio.to_thread(resolve_stream_url, youtube_url)

    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "ffmpeg was not found on PATH. Install it (e.g. 'winget install ffmpeg' or "
            "download from https://ffmpeg.org/download.html) and restart the terminal."
        )

    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-loglevel", "error",
        "-i", stream_url,
        "-f", "s16le",
        "-acodec", "pcm_s16le",
        "-ar", str(AUDIO_SAMPLE_RATE),
        "-ac", str(AUDIO_CHANNELS),
        "pipe:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None

    try:
        while True:
            chunk = await process.stdout.read(CHUNK_SIZE)
            if not chunk:
                break
            yield chunk
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
