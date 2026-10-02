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
        # 2026-09-25: ids loaded from the local SQLite cache at startup
        # (seed()). The first time a collector reports such an id again, the
        # fresh copy replaces the cached one (and is broadcast) instead of
        # being dropped as a duplicate — otherwise a cached market quote or
        # flight position would stay frozen for the whole session.
        self._seeded_ids: set = set()
        # Optional callback(event_id) for events silently pushed out of a
        # full per-category ring buffer (main.py deletes them from the DB).
        self.on_drop: Optional[Callable[[str], None]] = None

    def has(self, event_id: str) -> bool:
        return event_id in self._seen_ids

    def contains(self, event_id: str) -> bool:
        """True if the event is currently held (not pruned/evicted)."""
        return any(e["id"] == event_id for bucket in self._by_category.values() for e in bucket)

    def seed(self, events: list) -> int:
        """Loads cached events (oldest first) without treating them as
        final: see _seeded_ids. Returns how many were added."""
        added = 0
        for event in events:
            if self.add(event):
                self._seeded_ids.add(event["id"])
                added += 1
        return added

    def add(self, event: dict) -> bool:
        """Adds a new event. Returns False (no-op) if this id was already seen
        (unless it was only seeded from the cache — then it's refreshed)."""
        if event["id"] in self._seeded_ids:
            self._seeded_ids.discard(event["id"])
            for bucket in self._by_category.values():
                for old in [e for e in bucket if e["id"] == event["id"]]:
                    bucket.remove(old)
            self._seen_ids.discard(event["id"])
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
        dropped = bucket[-1] if bucket.maxlen is not None and len(bucket) >= bucket.maxlen else None
        bucket.appendleft(event)
        if dropped is not None:
            self._seeded_ids.discard(dropped["id"])
            if self.on_drop is not None:
                try:
                    self.on_drop(dropped["id"])
                except Exception:
                    pass
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
        self._seeded_ids.difference_update(removed)
        return removed

    def snapshot(self) -> list:
        out = []
        for bucket in self._by_category.values():
            out.extend(bucket)
        out.sort(key=lambda e: e["timestamp"], reverse=True)
        return out
