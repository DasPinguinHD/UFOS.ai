"""Central configuration for the HedgeFund backend process.

Stage 10, S10-E (user decision 2026-10-24): no .env any more. Keys (OpenRouter — shared by all modules —, FRED,
SEC EDGAR user agent, Alpaca, Tiingo, Binance demo, EODHD) come ONLY from the Windows Credential Manager via
strategy_platform.secrets, read at use time. Non-secret settings (models, intervals, base URL) come from the optional
data/settings.json (settings_file.py: a missing file/key = the default below, an invalid one refuses the start; the
AI fields are re-read per call by ai_settings()). The backend always listens on 127.0.0.1 (no host switch since
Docker was removed); only the CORS_ALLOWED_ORIGINS and DB / cost-log path overrides remain as process environment.
"""
import logging
import os
from urllib.parse import urlsplit

import settings_file

DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OPENROUTER_MODEL = "deepseek/deepseek-chat-v3-0324"
DEFAULT_ANALYSIS_PREMIUM_MODEL = "anthropic/claude-opus-5.5"
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(BACKEND_DIR, "data", settings_file.FILE_NAME)
# data/settings.json keys (lower-case former .env names): (kind, default, …) — settings_file.py
SCHEMA = {
    "openrouter_base_url": ("openrouter_url", DEFAULT_OPENROUTER_BASE_URL),
    "openrouter_model": ("str", DEFAULT_OPENROUTER_MODEL, 200),
    "openrouter_analysis_premium_model": ("str", DEFAULT_ANALYSIS_PREMIUM_MODEL, 200),
    "market_data_poll_seconds": ("int", 30, 1, 86400),
    "sec_edgar_min_request_interval_seconds": ("float", 0.5, 0.1, 60.0),
    "sec_edgar_poll_seconds": ("int", 120, 10, 86400),
    "fred_poll_seconds": ("int", 21600, 60, 604800),
    "calendar_poll_seconds": ("int", 21600, 60, 604800),
    "insider_display_days": ("int", 5, 1, 365),
    "insider_display_max": ("int", 300, 1, 100000),
    "insider_retention_days": ("int", 5, 1, 3650),
    "ai_results_retention_days": ("int", 90, 1, 36500),
}
SETTINGS = settings_file.load_or_exit(SETTINGS_PATH, SCHEMA)


def ai_settings() -> dict:
    """{"base_url", "model", "premium_model"} re-read from data/settings.json on every AI call (an edit applies
    without a restart); settings_file.SettingsError when the file became invalid."""
    s = settings_file.load(SETTINGS_PATH, SCHEMA)
    return {"base_url": s["openrouter_base_url"], "model": s["openrouter_model"],
            "premium_model": s["openrouter_analysis_premium_model"]}


def safe_openrouter_base_url(value) -> str:
    """The OpenRouter base URL if it is an https URL with a host; otherwise a
    warning is logged and the default https URL is used (the API key is sent
    with every request — never over plain http). Security by default."""
    text = str(value or "").strip()
    if not text:
        return DEFAULT_OPENROUTER_BASE_URL
    try:
        parts = urlsplit(text)
        ok = parts.scheme.lower() == "https" and bool(parts.hostname)
    except ValueError:
        ok = False
    if not ok:
        # FALLBACK (accepted by the user 2026-10-02): a non-https OPENROUTER_BASE_URL is
        # replaced by the default https URL (security default) instead of stopping AI calls.
        logging.getLogger(__name__).warning(
            "OPENROUTER_BASE_URL is not an https URL — ignored, using %s", DEFAULT_OPENROUTER_BASE_URL)
        return DEFAULT_OPENROUTER_BASE_URL
    return text


OPENROUTER_BASE_URL = SETTINGS["openrouter_base_url"]          # validated strictly by settings_file
OPENROUTER_MODEL = SETTINGS["openrouter_model"]
# Newsroom "Analysis & Reasoning" (2026-09-25): model behind the "Premium" switch only;
# "Standard" uses OPENROUTER_MODEL. Must be an OpenRouter model id.
OPENROUTER_ANALYSIS_PREMIUM_MODEL = SETTINGS["openrouter_analysis_premium_model"]

# Always loopback (stage 10 security audit M6; HTTP_HOST / ALLOW_REMOTE_ACCESS removed with Docker): api_auth.bind_host.
# The WebSocket placeholder (8865) was removed (L9).
# No browser page calls this backend — only the WPF app (no Origin header).
DEFAULT_ALLOWED_ORIGINS = ()
HEALTH_PORT = 8867          # fixed: the app connects to 8867 (no .env override since stage 10)

MARKET_DATA_POLL_SECONDS = SETTINGS["market_data_poll_seconds"]

# ===== Financial Newsroom (moved from Global Monitoring, 2026-09-24) =====

# --- SEC EDGAR (insider trades) ---
# SEC's Fair Access policy requires every automated requester to identify itself with a
# descriptive User-Agent (name + contact email). This is NOT optional — EDGAR will start
# throttling/blocking requests with a generic or missing User-Agent. Stored in the Windows
# Credential Manager (⚙ Keys & data, "sec-edgar-user-agent").
# Official rate limit is 10 requests/second; we stay well under that by default.
SEC_EDGAR_MIN_REQUEST_INTERVAL_SECONDS = SETTINGS["sec_edgar_min_request_interval_seconds"]
SEC_EDGAR_POLL_SECONDS = SETTINGS["sec_edgar_poll_seconds"]

# --- FRED (macro indicators) ---
# Free registration required: https://fred.stlouisfed.org/docs/api/api_key.html — the key is
# stored in the Windows Credential Manager (Hedge Fund → Settings → Keys, "fred-api-key").
FRED_POLL_SECONDS = SETTINGS["fred_poll_seconds"]  # macro series update slowly; 6h is plenty

# --- Economic Calendar (FRED release dates + FOMC, 2026-09-25) --- uses the FRED key
CALENDAR_POLL_SECONDS = SETTINGS["calendar_poll_seconds"]  # release schedules change rarely; 6h

# --- Local SQLite store (2026-09-25, newsroom/db.py) ---
# HedgeFund/backend/data/newsroom.db (env UFOS_HEDGEFUND_DB overrides the path).
# Insider Trades shows only filings of the last INSIDER_DISPLAY_DAYS (by filing time,
# at most INSIDER_DISPLAY_MAX); the database keeps them INSIDER_RETENTION_DAYS
# (default 5, same as the display window). AI outputs are kept AI_RESULTS_RETENTION_DAYS (90).
INSIDER_DISPLAY_DAYS = SETTINGS["insider_display_days"]
INSIDER_DISPLAY_MAX = SETTINGS["insider_display_max"]
INSIDER_RETENTION_DAYS = SETTINGS["insider_retention_days"]
AI_RESULTS_RETENTION_DAYS = SETTINGS["ai_results_retention_days"]
DB_PRUNE_INTERVAL_SECONDS = 86400

LOG_DIR = os.path.join(os.path.dirname(__file__), "data", "logs")

