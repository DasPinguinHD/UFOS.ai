"""Global Monitoring glue between the collectors/EventStore and storage.py
(2026-09-25): which event goes where, retention (backend start + daily),
cache-first warm start, flight tracks, price points and the Assess reuse
lookup. Every function here is fail-soft (never raises into a collector).

Where emitted events go:

- geopolitics -> ``items`` source ``news``   (id = event id, item_ts = ``published``,
                 else the event timestamp)
- disaster    -> ``items`` source ``gdacs``  (id = event id, item_ts = last seen;
                 payload keeps ``ends_at`` for gdacs_collector.is_past())
- flight      -> ``flight_positions``        (one row per icao24 + observed_at, i.e.
                 every polled position; payload = the marker event — the newest
                 row per aircraft is the marker at warm start)
- markets     -> ``items`` source ``yahoo``  (latest quote event per symbol) and
                 ``prices`` (symbol, quote market time, price, "yahoo")

``items`` source ``localnews`` is written by localnews.py, ``ai_outputs`` by
main.py (Assess/Sentiment, local-news summaries).
"""
import logging
from datetime import datetime, timedelta
from typing import Callable, Optional

from config import (NEWS_MAX_AGE_HOURS, EVENT_RETENTION_DAYS, PRICE_RETENTION_DAYS, FLIGHT_TRACK_RETENTION_HOURS,
                    LOCALNEWS_RETENTION_HOURS, AI_OUTPUT_RETENTION_DAYS, FLIGHT_CACHE_MAX_AGE_HOURS)
from events import CATEGORY_LIMITS, MAX_EVENTS_PER_CATEGORY
from storage import Storage, iso, norm_ts, parse_iso, utc_now

logger = logging.getLogger(__name__)

SOURCE_BY_CATEGORY = {
    "geopolitics": "news",
    "disaster": "gdacs",
    "flight": "opensky",
    "markets": "yahoo",
}
PRICE_SOURCE = "yahoo"


def _default_is_past() -> Callable[[dict], bool]:
    from collectors.gdacs_collector import is_past
    return is_past


def flight_icao24(event: dict) -> str:
    icao = event.get("icao24")
    if not icao:
        icao = str(event.get("id", "")).removeprefix("flight-")
    return str(icao).lower()


def record_event(storage: Storage, event: dict) -> bool:
    """Stores an emitted event where it belongs (see module docstring).
    Returns True if something was written. Never raises."""
    try:
        category = event.get("category")
        if not event.get("id") or category not in SOURCE_BY_CATEGORY:
            return False
        if category == "flight":
            observed = norm_ts(event.get("observed_at")) or norm_ts(event.get("timestamp")) or iso()
            return storage.add_flight_positions([{
                "icao24": flight_icao24(event), "ts": observed, "lat": event.get("lat"), "lon": event.get("lon"),
                "altitude_m": event.get("altitude_m"), "heading_deg": event.get("heading_deg"),
                "velocity_ms": event.get("velocity_ms"), "callsign": event.get("title"), "payload": event,
            }]) == 1
        source = SOURCE_BY_CATEGORY[category]
        item_ts = (event.get("published") or event.get("timestamp")) if category == "geopolitics" \
            else event.get("timestamp")
        written = storage.upsert_items(source, [(event["id"], item_ts, event)]) == 1
        if category == "markets" and event.get("symbol") and event.get("price") is not None:
            storage.add_prices([(event["symbol"], event.get("quote_time") or event.get("timestamp"),
                                 event["price"], PRICE_SOURCE)])
        return written
    except Exception as ex:  # fail soft — storage must never break a collector
        logger.warning("Could not store event %s: %s", event.get("id"), ex)
        return False


def forget_events(storage: Storage, event_ids) -> int:
    """Deletes removed events (e.g. past disasters, "remove" message) from
    storage. Flight ids are ignored: their positions are track history."""
    try:
        by_source: dict = {}
        for eid in event_ids or []:
            eid = str(eid)
            if eid.startswith("disaster-"):
                by_source.setdefault("gdacs", []).append(eid)
            elif eid.startswith("news-"):
                by_source.setdefault("news", []).append(eid)
            elif eid.startswith("markets-"):
                by_source.setdefault("yahoo", []).append(eid)
        return sum(storage.delete_items(src, ids) for src, ids in by_source.items())
    except Exception as ex:
        logger.warning("Could not delete events from storage: %s", ex)
        return 0


def _gdacs_expired(payload: dict, item_ts: Optional[str], now: datetime, is_past) -> bool:
    try:
        if is_past(payload):
            return True
    except Exception:
        return True
    seen = parse_iso(item_ts)
    return seen is None or seen < now - timedelta(days=EVENT_RETENTION_DAYS)


def apply_retention(storage: Storage, now: Optional[datetime] = None,
                    is_past: Optional[Callable[[dict], bool]] = None) -> dict:
    """Retention rules (backend start + daily). Returns rows deleted per table/source."""
    try:
        now = now or utc_now()
        is_past = is_past or _default_is_past()
        event_cutoff = now - timedelta(days=EVENT_RETENTION_DAYS)
        counts = {
            "news": storage.prune_items("news", event_cutoff),
            "yahoo": storage.prune_items("yahoo", event_cutoff),
            "localnews": storage.prune_items("localnews", now - timedelta(hours=LOCALNEWS_RETENTION_HOURS)),
            "flight_positions": storage.prune_flight_positions(now - timedelta(hours=FLIGHT_TRACK_RETENTION_HOURS)),
            "prices": storage.prune_prices(now - timedelta(days=PRICE_RETENTION_DAYS)),
            "ai_outputs": storage.prune_ai_outputs(AI_OUTPUT_RETENTION_DAYS, now=now),
        }
        drop = [row["item_id"] for row in storage.load_items("gdacs")
                if _gdacs_expired(row["payload"], row["item_ts"], now, is_past)]
        counts["gdacs"] = storage.delete_items("gdacs", drop)
        storage.set_meta("last_retention_ts", iso(now))
        storage.checkpoint()
        return counts
    except Exception as ex:
        logger.warning("Storage retention failed: %s", ex)
        return {}


def warm_start_events(storage: Storage, now: Optional[datetime] = None,
                      is_past: Optional[Callable[[dict], bool]] = None) -> list:
    """Stored events to seed into the EventStore before the collectors start,
    oldest first, within the EventStore's own limits: news published within
    NEWS_MAX_AGE_HOURS, current GDACS alerts, the latest position of each
    aircraft seen within FLIGHT_CACHE_MAX_AGE_HOURS, the latest quote per
    symbol; each category capped at its CATEGORY_LIMITS (newest kept)."""
    try:
        now = now or utc_now()
        is_past = is_past or _default_is_past()

        def newest(events, category):
            limit = CATEGORY_LIMITS.get(category, MAX_EVENTS_PER_CATEGORY)
            events = sorted(events, key=lambda e: e.get("timestamp") or "")
            return events[-limit:] if limit else events

        news = [r["payload"] for r in storage.load_items("news", since=now - timedelta(hours=NEWS_MAX_AGE_HOURS))]
        gdacs = [r["payload"] for r in storage.load_items("gdacs")
                 if not _gdacs_expired(r["payload"], r["item_ts"], now, is_past)]
        flights = [r["payload"] for r in storage.load_flight_positions(
            since=now - timedelta(hours=FLIGHT_CACHE_MAX_AGE_HOURS), latest_only=True)]
        markets = [r["payload"] for r in storage.load_items("yahoo")]

        out = (newest(news, "geopolitics") + newest(gdacs, "disaster")
               + newest(flights, "flight") + newest(markets, "markets"))
        out = [e for e in out if isinstance(e, dict) and e.get("id") and e.get("category")
               and e.get("timestamp")]
        out.sort(key=lambda e: e.get("timestamp") or "")
        return out
    except Exception as ex:
        logger.warning("Could not load cached events: %s", ex)
        return []


def cached_status(storage: Storage, source: str, seeded_count: int) -> Optional[dict]:
    """Initial source_status for a source that had cached events at start:
    "from cache (updated HH:MM) — refreshing…" (HH:MM = its last successful
    poll in local time, else the newest cached item). None if nothing was cached."""
    if seeded_count <= 0:
        return None
    try:
        state = storage.get_state(source) or {}
        stamp = state.get("last_success_ts")
        if not stamp:
            stamp = (storage.load_flight_positions(latest_only=True) or [{}])[-1].get("ts") \
                if source == "opensky" else storage.newest_item_ts(source)
        dt = parse_iso(stamp)
        when = dt.astimezone().strftime("%H:%M") if dt else "?"
        return {"source": source, "ok": True, "message": f"from cache (updated {when}) — refreshing…",
                "timestamp": iso(dt) if dt else None, "cached": True}
    except Exception as ex:
        logger.warning("Could not build the cached status for %s: %s", source, ex)
        return None


def flight_track(storage: Storage, icao24: str, hours: float = FLIGHT_TRACK_RETENTION_HOURS,
                 now: Optional[datetime] = None) -> list:
    """Stored positions of one aircraft within `hours`, oldest first."""
    now = now or utc_now()
    return [{"t": p["ts"], "lat": p["lat"], "lon": p["lon"], "altitude_m": p["altitude_m"],
             "heading_deg": p["heading_deg"], "velocity_ms": p["velocity_ms"], "callsign": p["callsign"]}
            for p in storage.load_flight_positions(icao24=icao24, since=now - timedelta(hours=hours))
            if p["lat"] is not None and p["lon"] is not None]


def find_recent_ai_output(storage: Storage, feature: str, cache_key: str, max_age_seconds: float,
                          now: Optional[datetime] = None) -> Optional[dict]:
    """Newest stored AI output of `feature` whose input.cache_key matches,
    within max_age_seconds — or None."""
    if not cache_key or max_age_seconds <= 0:
        return None
    since = (now or utc_now()) - timedelta(seconds=max_age_seconds)
    for row in storage.list_ai_outputs(feature, limit=500, since=since):
        if isinstance(row.get("input"), dict) and row["input"].get("cache_key") == cache_key:
            return row
    return None
