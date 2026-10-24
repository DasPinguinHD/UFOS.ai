"""JSON-lines event logging into data/logs/<date>.jsonl."""
import json
import os
import re
from datetime import date, datetime, timedelta, timezone

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


LOG_RETENTION_DAYS = 30
_DAY_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")


def prune_old_logs(days: int = LOG_RETENTION_DAYS, today=None, log_dir=None) -> list:
    """Removes the event logs data/logs/<yyyy-mm-dd>.jsonl older than ``days`` (stage 10 security audit L6; called
    once at start). Only files named exactly like that; anything else in the folder is left alone. Returns the
    removed names; a file that can't be removed is skipped (tried again at the next start)."""
    folder = log_dir or LOG_DIR
    cutoff = (today or datetime.now(timezone.utc).date()) - timedelta(days=days)
    removed = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return removed
    for name in names:
        m = _DAY_FILE.match(name)
        if not m:
            continue
        try:
            day = date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if day < cutoff:
            try:
                os.remove(os.path.join(folder, name))
                removed.append(name)
            except OSError:
                pass
    return removed
