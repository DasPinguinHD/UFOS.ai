"""Central configuration: poll intervals, retention and collector settings.

Stage 10, S10-E (user decision 2026-10-24): no .env any more. Secrets (OpenRouter key, OpenSky client id/secret) are
read at use time from the Windows Credential Manager (credstore.py, ⚙ Keys & data); the tunables below come from the
optional data/settings.json (settings_file.py — a missing file/key = the default here; an invalid one refuses the
start). The servers always listen on 127.0.0.1 (no host switches since Docker was removed); only the
CORS_ALLOWED_ORIGINS and DB / cost-log path overrides remain as process environment (tests).
"""
import os

import settings_file

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(BACKEND_DIR, "data", settings_file.FILE_NAME)
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OPENROUTER_MODEL = "deepseek/deepseek-chat-v3-0324"
# data/settings.json keys (lower-case former .env names): (kind, default, …) — settings_file.py
SCHEMA = {
    "news_poll_seconds": ("int", 600, 1, 86400),
    "news_max_age_hours": ("float", 48.0, 0.0, 8760.0),
    "gdacs_poll_seconds": ("int", 600, 1, 86400),
    "gdacs_max_hours_since_end": ("float", 48.0, 0.0, 8760.0),
    "opensky_poll_seconds": ("int", 240, 1, 86400),
    "opensky_min_notable_altitude_meters": ("float", 1500.0, 0.0, 30000.0),
    "yahoo_poll_seconds": ("int", 90, 1, 86400),
    "openrouter_base_url": ("openrouter_url", DEFAULT_OPENROUTER_BASE_URL),
    "openrouter_model": ("str", DEFAULT_OPENROUTER_MODEL, 200),
    "localnews_max_records": ("int", 8, 1, 200),
    "localnews_cache_seconds": ("int", 1800, 0, 604800),
    "localnews_error_cache_seconds": ("int", 45, 0, 86400),
    "event_retention_days": ("float", 7.0, 0.0, 36500.0),
    "price_retention_days": ("float", 730.0, 0.0, 36500.0),
    "flight_track_retention_hours": ("float", 24.0, 0.0, 87600.0),
    "localnews_retention_hours": ("float", 24.0, 0.0, 87600.0),
    "ai_output_retention_days": ("float", 90.0, 0.0, 36500.0),
    "flight_cache_max_age_hours": ("float", 2.0, 0.0, 8760.0),
    "assess_reuse_seconds": ("int", 1800, 0, 604800),
}
SETTINGS = settings_file.load_or_exit(SETTINGS_PATH, SCHEMA)


def ai_settings() -> tuple:
    """(openrouter_base_url, openrouter_model), re-read from data/settings.json on every AI call (an edit applies
    without a restart, like the keys); SettingsError when the file became invalid."""
    s = settings_file.load(SETTINGS_PATH, SCHEMA)
    return s["openrouter_base_url"], s["openrouter_model"]

# SEC EDGAR (insider trades) and FRED (macro indicators) settings moved with
# those features to HedgeFund/backend/config.py on 2026-09-24.

# --- World news RSS feeds (geopolitics/conflict layer; replaced GDELT 2026-09-24) ---
# Feeds are listed in news_feeds.py. 10 minutes is gentle for any publisher.
NEWS_POLL_SECONDS = SETTINGS["news_poll_seconds"]
# Feeds keep older items around; stories older than this aren't placed.
NEWS_MAX_AGE_HOURS = SETTINGS["news_max_age_hours"]

# --- GDACS (disasters) ---
GDACS_POLL_SECONDS = SETTINGS["gdacs_poll_seconds"]
# GDACS's feed keeps alerts listed well after they've ended (e.g. a forest
# fire that ended 4 days ago). Past events aren't useful for a live monitor:
# an alert is dropped once its <gdacs:todate> is more than this many hours
# in the past, or when GDACS itself marks it <gdacs:iscurrent>false.
# Point-in-time events (earthquakes: fromdate == todate) therefore stay
# visible for this long after they happened.
GDACS_MAX_HOURS_SINCE_END = SETTINGS["gdacs_max_hours_since_end"]

# --- OpenSky (notable/government/military flights) ---
OPENSKY_POLL_SECONDS = SETTINGS["opensky_poll_seconds"]  # anonymous quota is ~400 credits/day
# Optional: OpenSky migrated to OAuth2 client-credentials auth. The client id / secret are read at use time from the
# Credential Manager (credstore "opensky-client-id" / "opensky-client-secret"); without them the collector uses the
# anonymous (more rate-limited) states/all endpoint.
# Pre-filter: a matching callsign (military_registry.py) below this altitude
# is a local training/approach/departure flight, not a notable one — dropped
# rather than shown. Together with a shorter, less generic callsign prefix
# list, this is what fixed the flights layer showing far too many markers.
OPENSKY_MIN_NOTABLE_ALTITUDE_METERS = SETTINGS["opensky_min_notable_altitude_meters"]

# --- Yahoo (market quotes, reused pattern from LiveStreamAgent/backend/market_data.py) ---
YAHOO_POLL_SECONDS = SETTINGS["yahoo_poll_seconds"]

# --- AI Assessment button (OpenRouter, same pattern as UFOS.ai/Services/OpenRouterClient.cs
# and LiveStreamAgent/backend/llm_analyst.py) ---
# The key: credstore "openrouter-api-key" (shared by all modules), read per call; base URL + model: ai_settings().
OPENROUTER_BASE_URL = SETTINGS["openrouter_base_url"]
OPENROUTER_MODEL = SETTINGS["openrouter_model"]

# --- Local news lookup (Google News search RSS, used by the /localnews
# endpoint the frontend calls when a capital-city star is clicked) ---
LOCALNEWS_MAX_RECORDS = SETTINGS["localnews_max_records"]
# A city's local news doesn't need re-fetching more than every 30 minutes.
LOCALNEWS_CACHE_SECONDS = SETTINGS["localnews_cache_seconds"]
# How long a failed Google News lookup is remembered, so clicking the same
# star again right away uses the fallback instead of asking again.
LOCALNEWS_ERROR_CACHE_SECONDS = SETTINGS["localnews_error_cache_seconds"]

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
EVENT_RETENTION_DAYS = SETTINGS["event_retention_days"]
PRICE_RETENTION_DAYS = SETTINGS["price_retention_days"]
FLIGHT_TRACK_RETENTION_HOURS = SETTINGS["flight_track_retention_hours"]
LOCALNEWS_RETENTION_HOURS = SETTINGS["localnews_retention_hours"]
AI_OUTPUT_RETENTION_DAYS = SETTINGS["ai_output_retention_days"]
# Warm start: a stored flight whose latest position is older than this is not
# put back on the globe as a marker (its track stays queryable for 24 h).
FLIGHT_CACHE_MAX_AGE_HOURS = SETTINGS["flight_cache_max_age_hours"]
# A repeated Assess/Sentiment click on the same marker within this window
# reuses the stored AI output (answer carries "cached": true) instead of a
# new paid OpenRouter call. 0 disables the reuse.
ASSESS_REUSE_SECONDS = SETTINGS["assess_reuse_seconds"]
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
# Always 127.0.0.1 (api_auth.bind_host) — security-by-default rule, CLAUDE.md; no host switches since stage 10.
# Page origins allowed to call the API: the globe page is loaded with
# NavigateToString, whose opaque origin is sent as "null".
DEFAULT_ALLOWED_ORIGINS = ("null",)
WS_PORT = 8767          # fixed: the app connects to 8767 / 8768 (no .env override since stage 10)
HEALTH_PORT = 8768

# --- Storage ---
LOG_DIR = os.path.join(os.path.dirname(__file__), "data", "logs")
SCENARIOS_DIR = os.path.join(os.path.dirname(__file__), "data", "scenarios")
