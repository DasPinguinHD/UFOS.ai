"""Shared event schema + a small in-memory store.

Every collector emits events in this exact shape so the frontend (globe.html)
can treat all sources identically — one marker renderer, one Event Feed,
one WebSocket message format.

    {
        "id":        stable string, deduplicates re-polled items,
        "category":  "geopolitics" | "disaster" | "flight" | "markets",
        "lat", "lon": float,
        "title":     short headline / label,
        "summary":   one or two sentences of detail,
        "source":    human-readable source name, e.g. "GDELT",
        "url":       link to the original item, or null,
        "timestamp": ISO-8601 UTC string,
    }

EventStore keeps the last N events per category in memory (newest first),
used both to answer the frontend's initial "/events" snapshot request and to
avoid re-broadcasting an item the collector has already reported.
"""
from collections import deque
from datetime import datetime, timezone
from typing import Callable, Deque, Dict, Optional

MAX_EVENTS_PER_CATEGORY = 60
MAX_EVENT_AGE_HOURS = 48

# Per-category override of MAX_EVENTS_PER_CATEGORY. "flight" is the one
# category that can realistically flood the globe with simultaneous markers
# (OpenSky's is_notable_military() heuristic can match many aircraft in the
# air at once, which is what caused the "too many overlapping circles over
# Europe" clutter) — capped tighter than the default so at most this many
# flight markers are ever shown on the map at the same time. Everything else
# keeps the default.
CATEGORY_LIMITS = {
    "flight": 15,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_event(
    event_id: str,
    category: str,
    title: str,
    summary: str,
    source: str,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    url: Optional[str] = None,
    severity: Optional[str] = None,
) -> dict:
    return {
        "id": event_id,
        "category": category,
        "lat": lat,
        "lon": lon,
        "title": title,
        "summary": summary,
        "source": source,
        "url": url,
        "severity": severity,
        "timestamp": now_iso(),
    }


class EventStore:
    """Per-category ring buffers plus a global seen-id set for dedup."""

    def __init__(self) -> None:
        self._by_category: Dict[str, Deque[dict]] = {}
        self._seen_ids: set = set()

    def has(self, event_id: str) -> bool:
        return event_id in self._seen_ids

    def add(self, event: dict) -> bool:
        """Adds a new event. Returns False (no-op) if this id was already seen."""
        if event["id"] in self._seen_ids:
            return False
        self._seen_ids.add(event["id"])
        limit = CATEGORY_LIMITS.get(event["category"], MAX_EVENTS_PER_CATEGORY)
        bucket = self._by_category.get(event["category"])
        if bucket is None or bucket.maxlen != limit:
            # Re-create with the right maxlen if this is a new category, or if
            # CATEGORY_LIMITS was changed since the bucket was first created
            # (keeps existing items, just re-applies the cap going forward).
            bucket = deque(bucket or (), maxlen=limit)
            self._by_category[event["category"]] = bucket
        bucket.appendleft(event)
        # Cap the seen-id set so a long-running process doesn't grow forever.
        if len(self._seen_ids) > MAX_EVENTS_PER_CATEGORY * 20:
            self._seen_ids = {e["id"] for bucket in self._by_category.values() for e in bucket}
        return True

    def prune(self, should_remove) -> list:
        """Removes every stored event for which should_remove(event) is
        True and returns their ids (so the caller can tell connected
        frontends to drop them too). Ids stay in the seen-set on purpose:
        a pruned event re-appearing in its source feed shouldn't come back."""
        removed = []
        for category, bucket in self._by_category.items():
            keep = [e for e in bucket if not should_remove(e)]
            if len(keep) != len(bucket):
                removed.extend(e["id"] for e in bucket if should_remove(e))
                self._by_category[category] = deque(keep, maxlen=bucket.maxlen)
        return removed

    def snapshot(self) -> list:
        out = []
        for bucket in self._by_category.values():
            out.extend(bucket)
        out.sort(key=lambda e: e["timestamp"], reverse=True)
        return out
