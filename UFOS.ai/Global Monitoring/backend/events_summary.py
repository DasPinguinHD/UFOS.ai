"""GET /events/summary - read-only tile summary of the Global Monitoring backend (contract GM_SUMMARY_API, 2026-10-24).

Events of the last N hours counted by category, plus the feed status - for the main window's module tile. A pure
read: no collector wake-up (unlike GET /events), no network, no storage, no AI call. It works only on the in-memory
event list and the in-memory source status that main.py already keeps.

Part of UFOS.ai, a proof-of-concept portfolio project.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping, Optional

import storage

SCHEMA = 1
HOURS_DEFAULT, HOURS_MIN, HOURS_MAX = 24, 1, 168
HOURS_ERROR = f"hours must be an integer {HOURS_MIN}-{HOURS_MAX}"
# Fixed tile order: category -> its feed (persistence.SOURCE_BY_CATEGORY, repeated here so this module stays import-light)
CATEGORIES = (("geopolitics", "news"), ("disaster", "gdacs"), ("flight", "opensky"), ("markets", "yahoo"))
STATUS_KEYS = ("source", "ok", "message", "timestamp", "cached")


def parse_hours(raw: Optional[str]) -> int:
    """The ``hours`` query value: missing/empty -> 24; an integer 1-168; anything else -> ValueError (never clamped)."""
    if raw is None or str(raw).strip() == "":
        return HOURS_DEFAULT
    text = str(raw).strip()
    if not re.fullmatch(r"[0-9]{1,4}", text):
        raise ValueError(HOURS_ERROR)
    hours = int(text)
    if not HOURS_MIN <= hours <= HOURS_MAX:
        raise ValueError(HOURS_ERROR)
    return hours


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def event_time(event: Mapping) -> Optional[datetime]:
    """``observed_at`` first (flights), then ``timestamp``; None when neither is usable."""
    for key in ("observed_at", "timestamp"):
        norm = storage.norm_ts(event.get(key))
        if norm:
            dt = storage.parse_iso(norm)
            if dt is not None:
                return dt
    return None


def _status(name: str, status: Optional[Mapping]) -> dict:
    status = dict(status or {})
    out = {k: status.get(k) for k in STATUS_KEYS}
    out["source"] = name
    out["cached"] = bool(status.get("cached", False))
    if not status:
        out["message"] = "starting…"
    return out


def summarize(events: Iterable[Mapping], source_status: Mapping[str, Mapping], collectors: Iterable[str],
              hours: int = HOURS_DEFAULT, now: Optional[datetime] = None) -> dict:
    """The response body of GET /events/summary (field rules: GM_SUMMARY_API.md)."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    since = now - timedelta(hours=hours)
    counts = {cat: 0 for cat, _src in CATEGORIES}
    sources = dict(CATEGORIES)
    undated = 0
    for event in events:
        when = event_time(event)
        if when is None:
            undated += 1
            continue
        if when < since:
            continue
        cat = str(event.get("category") or "unknown")         # no category: counted as "unknown", never dropped
        counts[cat] = counts.get(cat, 0) + 1
    known = [cat for cat, _src in CATEGORIES]
    extra = sorted(cat for cat in counts if cat not in sources)
    categories = [{"category": cat, "source": sources.get(cat), "count": counts[cat]} for cat in known + extra]
    return {
        "schema": SCHEMA,
        "as_of": _iso(now),
        "window_hours": hours,
        "since": _iso(since),
        "total": sum(c["count"] for c in categories),
        "undated": undated,
        "categories": categories,
        "sources": [_status(name, source_status.get(name)) for name in collectors],
    }
