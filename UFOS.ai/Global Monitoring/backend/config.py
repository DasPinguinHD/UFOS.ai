"""Central configuration: environment secrets, poll intervals, and collector settings."""
import os

from dotenv import load_dotenv

load_dotenv()

# SEC EDGAR (insider trades) and FRED (macro indicators) settings moved with
# those features to HedgeFund/backend/config.py on 2026-09-24.

# --- World news RSS feeds (geopolitics/conflict layer; replaced GDELT 2026-09-24) ---
# Feeds are listed in news_feeds.py. 10 minutes is gentle for any publisher.
NEWS_POLL_SECONDS = int(os.getenv("NEWS_POLL_SECONDS", "600"))
# Feeds keep older items around; stories older than this aren't placed.
NEWS_MAX_AGE_HOURS = float(os.getenv("NEWS_MAX_AGE_HOURS", "48"))

# --- GDACS (disasters) ---
GDACS_POLL_SECONDS = int(os.getenv("GDACS_POLL_SECONDS", "600"))
# GDACS's feed keeps alerts listed well after they've ended (e.g. a forest
# fire that ended 4 days ago). Past events aren't useful for a live monitor:
# an alert is dropped once its <gdacs:todate> is more than this many hours
# in the past, or when GDACS itself marks it <gdacs:iscurrent>false.
# Point-in-time events (earthquakes: fromdate == todate) therefore stay
# visible for this long after they happened.
GDACS_MAX_HOURS_SINCE_END = float(os.getenv("GDACS_MAX_HOURS_SINCE_END", "48"))

# --- OpenSky (notable/government/military flights) ---
OPENSKY_POLL_SECONDS = int(os.getenv("OPENSKY_POLL_SECONDS", "240"))  # anonymous quota is ~400 credits/day
# Optional: OpenSky migrated to OAuth2 client-credentials auth. Without these, the
# collector falls back to the anonymous (more rate-limited) states/all endpoint.
OPENSKY_CLIENT_ID = os.getenv("OPENSKY_CLIENT_ID", "")
OPENSKY_CLIENT_SECRET = os.getenv("OPENSKY_CLIENT_SECRET", "")
# Pre-filter: a matching callsign (military_registry.py) below this altitude
# is a local training/approach/departure flight, not a notable one — dropped
# rather than shown. Together with a shorter, less generic callsign prefix
# list, this is what fixed the flights layer showing far too many markers.
OPENSKY_MIN_NOTABLE_ALTITUDE_METERS = float(os.getenv("OPENSKY_MIN_NOTABLE_ALTITUDE_METERS", "1500"))

# --- Yahoo (market quotes, reused pattern from LiveStreamAgent/backend/market_data.py) ---
YAHOO_POLL_SECONDS = int(os.getenv("YAHOO_POLL_SECONDS", "90"))

# --- AI Assessment button (OpenRouter, same pattern as UFOS.ai/Services/OpenRouterClient.cs
# and LiveStreamAgent/backend/llm_analyst.py) ---
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-chat-v3-0324")

# --- Local news lookup (Google News search RSS, used by the /localnews
# endpoint the frontend calls when a capital-city star is clicked) ---
LOCALNEWS_MAX_RECORDS = int(os.getenv("LOCALNEWS_MAX_RECORDS", "8"))
# A city's local news doesn't need re-fetching more than every 30 minutes.
LOCALNEWS_CACHE_SECONDS = int(os.getenv("LOCALNEWS_CACHE_SECONDS", "1800"))
# How long a failed Google News lookup is remembered, so clicking the same
# star again right away uses the fallback instead of asking again.
LOCALNEWS_ERROR_CACHE_SECONDS = int(os.getenv("LOCALNEWS_ERROR_CACHE_SECONDS", "45"))

# --- Local storage (SQLite, storage.py/persistence.py, 2026-09-25) ---
# File: backend/data/gms.db (UFOS_GM_DB = full path overrides it).
# Retention, applied at every backend start and once a day while running:
# news (geopolitics) events and disaster alerts EVENT_RETENTION_DAYS (news by
# published time, disasters by last-seen time; ended alerts are deleted as
# soon as gdacs_collector.is_past() drops them), the latest market quote
# event per symbol likewise; flight positions (flight_positions, basis for
# tracks) FLIGHT_TRACK_RETENTION_HOURS; price points (prices)
# PRICE_RETENTION_DAYS; persisted /localnews lookups
# LOCALNEWS_RETENTION_HOURS; AI outputs AI_OUTPUT_RETENTION_DAYS.
EVENT_RETENTION_DAYS = float(os.getenv("EVENT_RETENTION_DAYS", "7"))
PRICE_RETENTION_DAYS = float(os.getenv("PRICE_RETENTION_DAYS", "730"))
FLIGHT_TRACK_RETENTION_HOURS = float(os.getenv("FLIGHT_TRACK_RETENTION_HOURS", "24"))
LOCALNEWS_RETENTION_HOURS = float(os.getenv("LOCALNEWS_RETENTION_HOURS", "24"))
AI_OUTPUT_RETENTION_DAYS = float(os.getenv("AI_OUTPUT_RETENTION_DAYS", "90"))
# Warm start: a stored flight whose latest position is older than this is not
# put back on the globe as a marker (its track stays queryable for 24 h).
FLIGHT_CACHE_MAX_AGE_HOURS = float(os.getenv("FLIGHT_CACHE_MAX_AGE_HOURS", "2"))
# A repeated Assess/Sentiment click on the same marker within this window
# reuses the stored AI output (answer carries "cached": true) instead of a
# new paid OpenRouter call. 0 disables the reuse.
ASSESS_REUSE_SECONDS = int(os.getenv("ASSESS_REUSE_SECONDS", "1800"))
# Stale-while-revalidate (refresh.py): a request (GET /events, a new
# WebSocket client) wakes a collector early when its last poll is at least
# this old. OpenSky's minimum is its poll interval (anonymous quota!).
REFRESH_MIN_SECONDS = {
    "news": 600,
    "gdacs": 600,
    "opensky": OPENSKY_POLL_SECONDS,
    "yahoo": 90,
}

# --- Networking ---
# Loopback by default; another host only takes effect with ALLOW_REMOTE_ACCESS=true
# (api_auth.bind_host). Security-by-default rule, CLAUDE.md.
WS_HOST = os.getenv("WS_HOST", "127.0.0.1")
HTTP_HOST = os.getenv("HTTP_HOST", "127.0.0.1")
# Page origins allowed to call the API: the globe page is loaded with
# NavigateToString, whose opaque origin is sent as "null".
DEFAULT_ALLOWED_ORIGINS = ("null",)
WS_PORT = int(os.getenv("WS_PORT", "8767"))
HEALTH_PORT = int(os.getenv("HEALTH_PORT", "8768"))

# --- Storage ---
LOG_DIR = os.path.join(os.path.dirname(__file__), "data", "logs")
SCENARIOS_DIR = os.path.join(os.path.dirname(__file__), "data", "scenarios")
