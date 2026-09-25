"""Shared AI cost ledger writer (2026-09-25) — HedgeFund's own copy.

Every UFOS.ai module that calls OpenRouter appends one line per successful
completion to the shared ledger %LOCALAPPDATA%\\UFOS.ai\\ai-cost-log.jsonl
(one JSON object per line, UTF-8). By project convention each module keeps
its own small copy of this writer; the format is identical everywhere:

  {"ts": "2026-09-25T15:12:03.123456+00:00", "module": "HedgeFund",
   "feature": "analysis", "feature_label": "Analysis & Reasoning",
   "provider": "OpenRouter", "model": "<model id actually used>",
   "cost_usd": 0.0123 | null, "prompt_tokens": 1512 | null,
   "completion_tokens": 874 | null, "status": "ok"}

Rule: whoever makes the OpenRouter call records it — here the Python backend
(newsroom/ai.py). The WPF side never writes the ledger.
The env var UFOS_AI_COST_LOG overrides the path (used by the tests).
record() never raises — a failed write is only logged as a warning.
"""
import json
import logging
import os
from datetime import datetime, timezone

MODULE = "HedgeFund"
PROVIDER = "OpenRouter"

logger = logging.getLogger(__name__)


def ledger_path() -> str:
    override = os.environ.get("UFOS_AI_COST_LOG")
    if override:
        return override
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "UFOS.ai", "ai-cost-log.jsonl")


def _int_or_none(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _float_or_none(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def record(feature: str, feature_label: str, model: str, usage: dict) -> None:
    """Append one ledger line for a successful OpenRouter completion.
    `usage` is newsroom.ai.usage_info(response) → cost_usd/prompt_tokens/completion_tokens."""
    try:
        usage = usage or {}
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "module": MODULE,
            "feature": feature,
            "feature_label": feature_label,
            "provider": PROVIDER,
            "model": model,
            "cost_usd": _float_or_none(usage.get("cost_usd")),
            "prompt_tokens": _int_or_none(usage.get("prompt_tokens")),
            "completion_tokens": _int_or_none(usage.get("completion_tokens")),
            "status": "ok",
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        path = ledger_path()
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(line)
    except OSError as ex:
        logger.warning("AI cost ledger: could not write %s: %s", feature, ex)
    except Exception as ex:  # never let bookkeeping break an AI response
        logger.warning("AI cost ledger: unexpected error for %s: %s", feature, ex)
