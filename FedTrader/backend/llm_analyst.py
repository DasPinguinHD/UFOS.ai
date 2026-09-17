"""Builds a prompt from live transcript + market snapshot and asks DeepSeek (via OpenRouter)
for a structured long/short stance per asset category."""
import glob
import json
import logging
import os
from collections import deque

from openai import AsyncOpenAI

from config import (
    MarketSnapshot,
    OPENROUTER_API_KEY,
    OPENROUTER_BASE_URL,
    OPENROUTER_MODEL,
    SCENARIOS_DIR,
    TICKERS,
)

logger = logging.getLogger(__name__)

TRANSCRIPT_CONTEXT_LINES = 12

SYSTEM_PROMPT = """You are a macro markets analyst listening live to a Federal Reserve \
press conference or FOMC statement. You are given the recent rolling transcript, a snapshot \
of current market prices, and a set of reference scenario playbooks describing historical \
market reactions to Fed language.

Respond with ONLY a JSON object (no prose, no markdown fences) with this exact shape:
{
  "categories": {
    "<category_name>": {"stance": "long"|"short"|"neutral", "confidence": 0.0-1.0, "rationale": "<one sentence>"}
  },
  "summary": "<one sentence overall read on the latest remarks>"
}
Use exactly these category keys: treasury_yields, bond_proxies, reits, utilities, \
homebuilders, tech_giants, unprofitable_growth, financials."""


def _load_scenarios() -> str:
    parts = []
    for path in sorted(glob.glob(os.path.join(SCENARIOS_DIR, "*.md"))):
        with open(path, encoding="utf-8") as f:
            parts.append(f.read())
    return "\n\n---\n\n".join(parts)


def _format_snapshot(snapshot: MarketSnapshot) -> str:
    lines = []
    for category, symbols in TICKERS.items():
        for symbol in symbols:
            quote = snapshot.quotes.get(symbol)
            if quote is None:
                continue
            lines.append(f"{category}/{symbol}: price={quote.price} change%={quote.change_percent}")
    return "\n".join(lines) if lines else "(no market data available yet)"


class LLMAnalyst:
    def __init__(self) -> None:
        self._client = AsyncOpenAI(api_key=OPENROUTER_API_KEY, base_url=OPENROUTER_BASE_URL)
        self._scenarios = _load_scenarios()
        self._recent_transcript: deque[str] = deque(maxlen=TRANSCRIPT_CONTEXT_LINES)

    def note(self, transcript_segment: str) -> None:
        """Record a segment in the rolling context without triggering an LLM call."""
        self._recent_transcript.append(transcript_segment)

    async def analyze(self, transcript_segment: str, snapshot: MarketSnapshot) -> dict:
        self._recent_transcript.append(transcript_segment)

        user_prompt = (
            f"REFERENCE SCENARIOS:\n{self._scenarios}\n\n"
            f"RECENT TRANSCRIPT (oldest to newest):\n{chr(10).join(self._recent_transcript)}\n\n"
            f"LATEST MARKET SNAPSHOT ({snapshot.timestamp}):\n{_format_snapshot(snapshot)}\n\n"
            f"Analyze the latest transcript line in light of the reference scenarios and market "
            f"snapshot, and produce the JSON response."
        )

        response = await self._client.chat.completions.create(
            model=OPENROUTER_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            max_tokens=1024,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.error("Failed to parse LLM response as JSON: %s", raw)
            return {"categories": {}, "summary": "", "error": "invalid_json", "raw": raw}
