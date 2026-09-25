"""Builds a prompt from live transcript + market snapshot and asks DeepSeek (via OpenRouter)
for a structured long/short stance per asset category."""
import glob
import json
import logging
import os
from collections import deque
from datetime import datetime, timezone

from openai import AsyncOpenAI

import cost_ledger

from config import (
    MarketSnapshot,
    OPENROUTER_API_KEY,
    OPENROUTER_BASE_URL,
    OPENROUTER_MODEL,
    SCENARIOS_DIR,
    iter_ticker_groups,
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
Use exactly these category keys: treasury_yields_core, liquidity_anchors, equity_indices, banking_stress, \
bond_proxies, reits, utilities, homebuilders, tech_giants_extended, unprofitable_growth, financials_broad, credit_risk."""

SYSTEM_PROMPT_VERDICT = """You are a pragmatic market analyst. Given a transcript excerpt and a market snapshot, recommend exactly ONE action for a single ticker: either LONG or SHORT. Provide a concise verdict and a substantive rationale. The rationale should be evidence-based, include multiple supporting points when available, and end with a clear weighing statement (e.g., "Overall: favor LONG because ..." or "Overall: favor SHORT because ..."). Return ONLY a JSON object with the exact shape:
{
  "verdict": "LONG"|"SHORT",
  "ticker": "<SYMBOL>",
  "confidence": 0-100,
  "reason": "<detailed explanation, up to configurable length>"
}
No extra text, no markdown. Make the reason as informative as possible while staying factual and concise."""


def provenance(feature: str, model: "str | None") -> dict:
    """Machine-readable marking for AI-generated text (EU AI Act Art. 50(2)).

    Attached as "provenance" to every broadcast payload that carries LLM output
    (verdict, analysis), so the WPF window can show
    "✦ AI-GENERATED · <model> · <time>". Same shape in every UFOS.ai backend —
    see the "AI content labelling" section in CLAUDE.md before changing it.
    """
    return {
        "ai_generated": True,
        "generator": f"UFOS.ai/LiveStreamAgent/{feature}",
        "provider": "OpenRouter",
        "model": model or OPENROUTER_MODEL,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "human_reviewed": False,
        "label": "AI-generated content (EU AI Act Art. 50)",
    }


def _load_scenarios() -> str:
    parts = []
    for path in sorted(glob.glob(os.path.join(SCENARIOS_DIR, "*.md"))):
        with open(path, encoding="utf-8") as f:
            parts.append(f.read())
    return "\n\n---\n\n".join(parts)


def _format_snapshot(snapshot: MarketSnapshot) -> str:
    lines = []
    for section_name, group_name, symbols in iter_ticker_groups():
        for symbol in symbols:
            quote = snapshot.quotes.get(symbol)
            if quote is None:
                continue
            lines.append(f"{section_name}/{group_name}/{symbol}: price={quote.price} change%={quote.change_percent}")
    return "\n".join(lines) if lines else "(no market data available yet)"


class LLMAnalyst:
    def __init__(self) -> None:
        self._client = AsyncOpenAI(api_key=OPENROUTER_API_KEY, base_url=OPENROUTER_BASE_URL)
        self._scenarios = _load_scenarios()
        self._recent_transcript: deque[str] = deque(maxlen=TRANSCRIPT_CONTEXT_LINES)

    def note(self, transcript_segment: str) -> None:
        """Record a segment in the rolling context without triggering an LLM call."""
        self._recent_transcript.append(transcript_segment)

    async def generate_verdict(self, snapshot: MarketSnapshot, max_reason_chars: int = 300) -> dict:
        """Ask the LLM for a single verdict JSON. Return a validated dict or an error dict."""
        # build concise user prompt
        transcript_text = chr(10).join(self._recent_transcript)
        if not transcript_text:
            transcript_text = "(no transcript yet)"

        user_prompt = (
            f"RECENT_TRANSCRIPT:\n{transcript_text}\n\n"
            f"MARKET_SNAPSHOT ({snapshot.timestamp}):\n{_format_snapshot(snapshot)}\n\n"
            f"Provide an evidence-based, multi-point rationale and conclude with an explicit overall weighing sentence."
            f"Respond with JSON only. Limit 'reason' to {max_reason_chars} characters."
        )

        try:
            response = await self._client.chat.completions.create(
                model=OPENROUTER_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT_VERDICT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.0,
                max_tokens=512,
                response_format={"type": "json_object"},
            )
            # Whoever makes the OpenRouter call records its cost (shared AI cost ledger).
            used_model = getattr(response, "model", None) or OPENROUTER_MODEL
            usage = cost_ledger.usage_info(response)
            cost_ledger.record("verdict", used_model, usage)
            raw = response.choices[0].message.content
            # parse and validate
            parsed = json.loads(raw)
            # basic validation and normalization
            verdict = parsed.get("verdict")
            ticker = parsed.get("ticker")
            confidence = parsed.get("confidence")
            reason = parsed.get("reason", "")
            if not isinstance(verdict, str) or verdict.upper() not in ("LONG", "SHORT"):
                raise ValueError("invalid verdict")
            if not isinstance(ticker, str) or not ticker:
                raise ValueError("invalid ticker")
            try:
                confidence = int(float(confidence))
            except Exception:
                confidence = 0
            reason = (reason or "").strip()
            if len(reason) > max_reason_chars:
                reason = reason[:max_reason_chars].rstrip()

            return {
                "verdict": verdict.upper(),
                "ticker": ticker,
                "confidence": confidence,
                "reason": reason,
                "provenance": provenance("verdict", getattr(response, "model", None)),
                # Optional cost/token info for the WPF window ("Cost: $0.0021 · 812 in / 190 out").
                "usage": usage,
            }
        except Exception as ex:
            logger.exception("LLM generate_verdict failed: %s", ex)
            return {"error": "llm_failed", "detail": str(ex)}

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
        usage = cost_ledger.usage_info(response)
        cost_ledger.record("analysis", getattr(response, "model", None) or OPENROUTER_MODEL, usage)
        raw = response.choices[0].message.content
        try:
            result = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.error("Failed to parse LLM response as JSON: %s", raw)
            return {"categories": {}, "summary": "", "error": "invalid_json", "raw": raw}
        if isinstance(result, dict):
            result["provenance"] = provenance("analysis", getattr(response, "model", None))
            result["usage"] = usage
        return result
