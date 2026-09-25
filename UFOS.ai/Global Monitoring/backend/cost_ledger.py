"""Shared AI cost ledger writer (2026-09-25).

Every UFOS.ai module appends one JSON line per OpenRouter completion to the
same file, %LOCALAPPDATA%\\UFOS.ai\\ai-cost-log.jsonl, so the costs of all AI
features can be totalled in one place. By project convention each module has
its OWN small copy of this writer (same spec everywhere — keep them in sync):

    {"ts": ISO-8601 UTC, "module": "Global Monitoring", "feature": "<short id>",
     "feature_label": "<human label>", "provider": "OpenRouter",
     "model": "<model id actually used>", "cost_usd": number|null,
     "prompt_tokens": int|null, "completion_tokens": int|null, "status": "ok"}

Whoever makes the OpenRouter call records it — here the Python backend,
right after each successful completion (see assess.py). The env var
UFOS_AI_COST_LOG (full file path) overrides the location, e.g. for tests.
Writing never raises: a failure is only logged as a warning, it must never
break the AI feature itself.
"""
import json
import logging
import os
from datetime import datetime, timezone

MODULE = "Global Monitoring"
PROVIDER = "OpenRouter"

logger = logging.getLogger(__name__)


def ledger_path() -> str:
    override = os.environ.get("UFOS_AI_COST_LOG")
    if override:
        return override
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "UFOS.ai", "ai-cost-log.jsonl")


def record(feature: str, feature_label: str, model: str, usage: dict | None) -> None:
    """Append one ledger line. `usage` is assess.usage_info(response)."""
    try:
        usage = usage or {}
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "module": MODULE,
            "feature": feature,
            "feature_label": feature_label,
            "provider": PROVIDER,
            "model": model,
            "cost_usd": usage.get("cost_usd"),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "status": "ok",
        }
        path = ledger_path()
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        # One write() call in append mode, so concurrent writers from other
        # modules don't interleave within a line.
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(line)
    except Exception as ex:  # never break the AI feature over bookkeeping
        logger.warning("Could not write AI cost ledger entry (%s): %s", feature, ex)
