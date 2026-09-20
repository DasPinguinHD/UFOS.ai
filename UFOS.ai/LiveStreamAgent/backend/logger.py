"""JSON-lines event logging into data/logs/<date>.jsonl."""
import json
import os
from datetime import datetime, timezone

from config import LOG_DIR

os.makedirs(LOG_DIR, exist_ok=True)


def _log_path() -> str:
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return os.path.join(LOG_DIR, f"{date_str}.jsonl")


def log_event(event_type: str, payload: dict) -> None:
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": event_type,
        "payload": payload,
    }
    with open(_log_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
