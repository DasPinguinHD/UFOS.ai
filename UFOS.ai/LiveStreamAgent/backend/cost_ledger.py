"""Shared UFOS.ai AI cost ledger (this module's own copy of the writer, by project convention).

Every OpenRouter call made by the LiveStream Agent appends one JSON line to
%LOCALAPPDATA%\\UFOS.ai\\ai-cost-log.jsonl (override: UFOS_AI_COST_LOG = full path).
The line shape is identical in every UFOS.ai module so a single viewer can sum the costs:

  {"ts", "module", "feature", "feature_label", "provider", "model",
   "cost_usd", "prompt_tokens", "completion_tokens", "status"}

cost_usd is OpenRouter's usage.cost (credits; 1 credit = 1 USD). Writing never raises.
"""
import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

MODULE = "LiveStreamAgent"
PROVIDER = "OpenRouter"

FEATURE_LABELS = {
    "verdict": "LiveStream Agent · AI verdict",
    "analysis": "LiveStream Agent · AI analysis",
}


def ledger_path() -> str:
    """Full path of the shared ledger file (env override UFOS_AI_COST_LOG for tests)."""
    override = os.environ.get("UFOS_AI_COST_LOG")
    if override:
        return override
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "UFOS.ai", "ai-cost-log.jsonl")


def _num(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _int(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def usage_info(response) -> dict:
    """Token counts and the cost OpenRouter charged for an `openai`-client response.

    OpenRouter always returns `usage.cost` (credits, 1 credit = 1 USD); the `openai`
    client keeps that non-standard field as an extra attribute. Missing values stay None.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return {"cost_usd": None, "prompt_tokens": None, "completion_tokens": None}
    cost = getattr(usage, "cost", None)
    if cost is None:
        cost = (getattr(usage, "model_extra", None) or {}).get("cost")
    return {
        "cost_usd": _num(cost),
        "prompt_tokens": _int(getattr(usage, "prompt_tokens", None)),
        "completion_tokens": _int(getattr(usage, "completion_tokens", None)),
    }


def record(feature: str, model, usage: "dict | None", status: str = "ok") -> None:
    """Append one ledger line. Never raises - failures are logged as warnings."""
    try:
        usage = usage or {}
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "module": MODULE,
            "feature": feature,
            "feature_label": FEATURE_LABELS.get(feature, f"LiveStream Agent · {feature}"),
            "provider": PROVIDER,
            "model": model or "",
            "cost_usd": _num(usage.get("cost_usd")),
            "prompt_tokens": _int(usage.get("prompt_tokens")),
            "completion_tokens": _int(usage.get("completion_tokens")),
            "status": status,
        }
        path = ledger_path()
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(line)
    except Exception as ex:  # the ledger must never break an AI feature
        logger.warning("AI cost ledger write failed: %s", ex)
