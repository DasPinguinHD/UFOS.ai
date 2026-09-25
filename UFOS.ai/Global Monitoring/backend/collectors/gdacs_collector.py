"""GDACS (Global Disaster Alert and Coordination System) — GeoRSS/CAP feed.

Free, no API key, no rate limit published (it's a low-volume alert feed —
only events past a significance threshold appear at all). Covers all
disaster types (earthquakes, storms/cyclones, floods, volcanoes, droughts),
each with real lat/lon in the feed itself — no geocoding table needed here,
unlike GDELT.
"""
import asyncio
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import aiohttp

from config import GDACS_POLL_SECONDS, GDACS_MAX_HOURS_SINCE_END
from events import make_event

logger = logging.getLogger(__name__)

FEED_URL = "https://www.gdacs.org/xml/rss.xml"

EVENT_TYPES = {"EQ": "Earthquake", "TC": "Tropical cyclone", "FL": "Flood", "VO": "Volcano",
               "DR": "Drought", "WF": "Wildfire", "TS": "Tsunami"}

NS = {
    "geo": "http://www.w3.org/2003/01/geo/wgs84_pos#",
    "gdacs": "http://www.gdacs.org",
}


def _parse_rfc822(text):
    try:
        dt = parsedate_to_datetime(text) if text else None
    except (TypeError, ValueError):
        return None
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def is_past(event: dict) -> bool:
    """True if a disaster event ended more than GDACS_MAX_HOURS_SINCE_END
    ago. Also used by main.py's periodic prune, so an alert that was current
    when first seen disappears once it ages out while the app keeps running."""
    ends_at = event.get("ends_at")
    if not ends_at:
        return False
    try:
        end = datetime.fromisoformat(ends_at)
    except ValueError:
        return False
    return end < datetime.now(timezone.utc) - timedelta(hours=GDACS_MAX_HOURS_SINCE_END)


async def _poll_once(session: aiohttp.ClientSession, emit, report_status) -> None:
    try:
        async with session.get(FEED_URL, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            resp.raise_for_status()
            body = await resp.text()
    except Exception as ex:
        logger.warning("GDACS poll failed: %s", ex)
        await report_status(False, str(ex))
        return

    try:
        root = ET.fromstring(body)
    except ET.ParseError as ex:
        logger.warning("GDACS feed did not parse as XML: %s", ex)
        await report_status(False, f"parse error: {ex}")
        return

    items = root.findall(".//item")
    placed = skipped_past = 0
    for item in items:
        try:
            # Drop alerts that are over (see GDACS_MAX_HOURS_SINCE_END).
            iscurrent_el = item.find("gdacs:iscurrent", NS)
            if iscurrent_el is not None and (iscurrent_el.text or "").strip().lower() == "false":
                skipped_past += 1
                continue
            todate_el = item.find("gdacs:todate", NS)
            ends_at_dt = _parse_rfc822(todate_el.text if todate_el is not None else None)
            ends_at = ends_at_dt.isoformat() if ends_at_dt else None
            if is_past({"ends_at": ends_at}):
                skipped_past += 1
                continue

            # GDACS nests lat/long one level deeper than you'd guess from the
            # namespace name alone — under <geo:Point>, not directly on
            # <item> — so a plain item.find("geo:lat") (direct children only)
            # always came back None and every alert was silently skipped.
            # This is why "Weather & Disasters" showed nothing at all. ".//"
            # searches all descendants instead of just direct children.
            lat_el = item.find(".//geo:lat", NS)
            lon_el = item.find(".//geo:long", NS)
            if lat_el is None or lon_el is None or not lat_el.text or not lon_el.text:
                continue
            lat, lon = float(lat_el.text), float(lon_el.text)

            guid_el = item.find("guid")
            title_el = item.find("title")
            link_el = item.find("link")
            desc_el = item.find("description")
            severity_el = item.find("gdacs:severity", NS)
            eventtype_el = item.find("gdacs:eventtype", NS)
            country_el = item.find("gdacs:country", NS)
            alertlevel_el = item.find("gdacs:alertlevel", NS)
            population_el = item.find("gdacs:population", NS)

            event_id = "disaster-" + (guid_el.text if guid_el is not None and guid_el.text else (title_el.text or ""))
            severity = severity_el.get("value") if severity_el is not None else None
            country = country_el.text if country_el is not None else None
            eventtype = eventtype_el.text if eventtype_el is not None else None
            alertlevel = alertlevel_el.text if alertlevel_el is not None else None

            # GDACS's own <description> is already a full sentence with the
            # real substance (date, what happened, magnitude/scale, who's
            # affected) — e.g. "On 9/23/2026 2:41:02 PM, an earthquake
            # occurred in Tonga potentially affecting 1 thousand in MMI IV.
            # The earthquake had Magnitude 5.7M, Depth:10km." That's far more
            # for the Assess button to work with than the old
            # "EQ — Tonga — Magnitude 5.7M" tag line, which threw away the
            # description entirely unless every other field was missing.
            # <gdacs:population> carries its own descriptive text (e.g.
            # "1 thousand in MMI IV") in its element body, not just the
            # numeric `value` attribute — appended when present and not
            # already restated by the description sentence.
            description = (desc_el.text or "").strip() if desc_el is not None else ""
            population_text = (population_el.text or "").strip() if population_el is not None else ""

            summary_parts = []
            if description:
                summary_parts.append(description)
            else:
                # Fallback for the rare item with no <description> at all.
                summary_bits = [b for b in [eventtype, country, severity] if b]
                summary_parts.append(" — ".join(summary_bits) if summary_bits else "GDACS alert")
            if alertlevel:
                summary_parts.append(f"GDACS alert level: {alertlevel}.")
            # <gdacs:population> is appended only if it adds something. For
            # floods it holds a death count that can CONTRADICT the
            # description (2026-09-24: "caused 1 deaths" + "Affected: 0
            # deaths" for the same Italy flood) — feeding the Assess model a
            # contradiction. Death counts come from the description only.
            if (population_text and population_text not in description
                    and "death" not in population_text.lower()):
                summary_parts.append(f"Affected: {population_text}.")
            summary = " ".join(summary_parts)

            event = make_event(
                event_id=event_id,
                category="disaster",
                title=title_el.text if title_el is not None else "GDACS alert",
                summary=summary,
                source="GDACS",
                lat=lat, lon=lon,
                url=link_el.text if link_el is not None else None,
                severity=severity or alertlevel,
            )
            event["ends_at"] = ends_at
            # Structured facts for the Assess context (assess_context.py):
            # unambiguous ISO dates instead of GDACS' mixed dd/mm vs m/d
            # description formats, full event type, country, alert level.
            fromdate_el = item.find("gdacs:fromdate", NS)
            starts_at_dt = _parse_rfc822(fromdate_el.text if fromdate_el is not None else None)
            event["facts"] = {
                "type": EVENT_TYPES.get(eventtype or "", eventtype),
                "country": country,
                "alert_level": alertlevel,
                # GDACS fills severity with "Magnitude 0" (value="0") for event
                # types it doesn't measure that way (floods, droughts) — noise.
                "severity": (severity_el.text.strip()
                             if severity_el is not None and severity_el.text
                             and (severity_el.get("value") or "0") not in ("0", "0.0") else None),
                "started": starts_at_dt.date().isoformat() if starts_at_dt else None,
                "last_update_or_end": ends_at_dt.date().isoformat() if ends_at_dt else None,
            }
            emit(event)
            placed += 1
        except Exception:
            logger.exception("Failed to parse a GDACS item, skipping it")

    await report_status(True, f"{placed} current alerts placed ({skipped_past} past alerts skipped)")


async def run(emit, report_status) -> None:
    async with aiohttp.ClientSession() as session:
        while True:
            await _poll_once(session, emit, report_status)
            await asyncio.sleep(GDACS_POLL_SECONDS)
