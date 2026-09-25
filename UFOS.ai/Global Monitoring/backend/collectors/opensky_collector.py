"""OpenSky Network — notable (government/military) flights.

Free, anonymous access to /states/all is possible but tightly rate-limited
(~400 weight-1 credits/day, i.e. roughly one call every ~4 minutes). If
OPENSKY_CLIENT_ID/SECRET are set (OpenSky migrated to OAuth2 client-credentials
in 2025), the collector authenticates for a higher quota; otherwise it falls
back to the anonymous endpoint and simply polls less often.

OpenSky does not flag military/government aircraft itself — filtering is done
here via military_registry.is_notable_military(), a self-maintained callsign
prefix list that needs periodic upkeep (see that module's docstring).
"""
import asyncio
import logging
import time

import aiohttp

from config import OPENSKY_POLL_SECONDS, OPENSKY_CLIENT_ID, OPENSKY_CLIENT_SECRET, OPENSKY_MIN_NOTABLE_ALTITUDE_METERS
from events import make_event
from .military_registry import is_notable_military, describe_callsign

logger = logging.getLogger(__name__)

STATES_URL = "https://opensky-network.org/api/states/all"
TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"

# states/all row layout (subset we use): index -> field
IDX_ICAO24, IDX_CALLSIGN, IDX_ORIGIN_COUNTRY = 0, 1, 2
IDX_LON, IDX_LAT, IDX_BARO_ALT, IDX_ON_GROUND, IDX_VELOCITY, IDX_HEADING = 5, 6, 7, 8, 9, 10
IDX_VERTICAL_RATE, IDX_SQUAWK = 11, 14


_COMPASS_POINTS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


def _compass(heading_degrees: float) -> str:
    """16-point compass label for a heading in degrees, so a summary reads
    "heading 252° (WSW)" instead of a bare number assess.py's model has to
    convert in its head."""
    idx = int((heading_degrees % 360) / 22.5 + 0.5) % 16
    return _COMPASS_POINTS[idx]


class _TokenCache:
    def __init__(self) -> None:
        self.token = None
        self.expires_at = 0.0

    async def get(self, session: aiohttp.ClientSession):
        if not (OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET):
            return None
        if self.token and time.time() < self.expires_at - 30:
            return self.token
        data = {
            "grant_type": "client_credentials",
            "client_id": OPENSKY_CLIENT_ID,
            "client_secret": OPENSKY_CLIENT_SECRET,
        }
        async with session.post(TOKEN_URL, data=data, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            resp.raise_for_status()
            payload = await resp.json(content_type=None)
        self.token = payload["access_token"]
        self.expires_at = time.time() + float(payload.get("expires_in", 1800))
        return self.token


_token_cache = _TokenCache()


async def _poll_once(session: aiohttp.ClientSession, emit, report_status) -> None:
    headers = {}
    try:
        token = await _token_cache.get(session)
        if token:
            headers["Authorization"] = f"Bearer {token}"
    except Exception as ex:
        logger.warning("OpenSky OAuth2 token fetch failed, continuing anonymously: %s", ex)

    try:
        async with session.get(STATES_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
    except Exception as ex:
        logger.warning("OpenSky poll failed: %s", ex)
        await report_status(False, str(ex))
        return

    states = data.get("states") or []
    placed = 0
    for row in states:
        try:
            callsign = (row[IDX_CALLSIGN] or "").strip()
            if not is_notable_military(callsign):
                continue
            lat, lon = row[IDX_LAT], row[IDX_LON]
            if lat is None or lon is None:
                continue
            icao24 = row[IDX_ICAO24]
            on_ground = row[IDX_ON_GROUND]
            if on_ground:
                continue
            altitude = row[IDX_BARO_ALT]
            # Second half of the flight pre-filter (see military_registry.py):
            # a matching callsign still shows up at low altitude for a local
            # training circuit or a normal approach/departure near its home
            # base — neither is "notable" in the sense this layer is for.
            # Requiring cruise-ish altitude cuts that out; unknown altitude
            # (None) is dropped too, since that's usually a stale/partial
            # states/all row rather than a confirmed high-altitude flight.
            if not isinstance(altitude, (int, float)) or altitude < OPENSKY_MIN_NOTABLE_ALTITUDE_METERS:
                continue
            heading = row[IDX_HEADING]
            origin_country = row[IDX_ORIGIN_COUNTRY]
            velocity = row[IDX_VELOCITY]
            vertical_rate = row[IDX_VERTICAL_RATE] if len(row) > IDX_VERTICAL_RATE else None
            squawk = row[IDX_SQUAWK] if len(row) > IDX_SQUAWK else None

            alt_str = f"{altitude:.0f}m" if isinstance(altitude, (int, float)) else "unknown altitude"
            hdg_str = f"{heading:.0f}° ({_compass(heading)})" if isinstance(heading, (int, float)) else "unknown heading"
            speed_str = f"{velocity * 3.6:.0f} km/h ground speed" if isinstance(velocity, (int, float)) else None
            if isinstance(vertical_rate, (int, float)):
                if vertical_rate > 1:
                    vrate_str = f"climbing at {vertical_rate:.0f} m/s"
                elif vertical_rate < -1:
                    vrate_str = f"descending at {abs(vertical_rate):.0f} m/s"
                else:
                    vrate_str = "level flight"
            else:
                vrate_str = None
            squawk_str = f"squawk {squawk}" if squawk else None

            # This is what makes the Assess/Sentiment button able to say
            # something substantive about a flight marker instead of "input
            # too sparse to derive substantive insight" (2026-09-24): a
            # plain-English description of what the matched callsign prefix
            # generally is (from military_registry.py), plus the fuller set
            # of telemetry OpenSky actually gives us — not just altitude and
            # heading, but ground speed, whether it's climbing/descending/
            # level, and its squawk code when broadcast.
            operation = describe_callsign(callsign)
            detail_bits = [b for b in [alt_str, vrate_str, hdg_str, speed_str, squawk_str] if b]
            summary_parts = []
            if operation:
                summary_parts.append(f"{origin_country or 'Unknown origin'} — {operation}.")
            else:
                summary_parts.append(f"{origin_country or 'Unknown origin'} registered flight.")
            summary_parts.append("Currently: " + ", ".join(detail_bits) + ".")

            event = make_event(
                event_id=f"flight-{icao24}",
                category="flight",
                title=callsign or icao24,
                summary=" ".join(summary_parts),
                source="OpenSky",
                lat=lat, lon=lon,
            )
            emit(event)
            placed += 1
        except Exception:
            logger.exception("Failed to parse an OpenSky state row, skipping it")

    await report_status(True, f"{placed} notable flight(s) of {len(states)} tracked")


async def run(emit, report_status) -> None:
    async with aiohttp.ClientSession() as session:
        while True:
            await _poll_once(session, emit, report_status)
            await asyncio.sleep(OPENSKY_POLL_SECONDS)
