"""Central configuration for the HedgeFund backend process."""
import logging
import os
from urllib.parse import urlsplit

from dotenv import load_dotenv

load_dotenv()

DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


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
        logging.getLogger(__name__).warning(
            "OPENROUTER_BASE_URL is not an https URL — ignored, using %s", DEFAULT_OPENROUTER_BASE_URL)
        return DEFAULT_OPENROUTER_BASE_URL
    return text


OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = safe_openrouter_base_url(os.getenv("OPENROUTER_BASE_URL", DEFAULT_OPENROUTER_BASE_URL))
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-chat-v3-0324")
# Newsroom "Analysis & Reasoning" (2026-09-25): model behind the "Premium" switch only;
# "Standard" uses OPENROUTER_MODEL. Must be an OpenRouter model id.
OPENROUTER_ANALYSIS_PREMIUM_MODEL = os.getenv("OPENROUTER_ANALYSIS_PREMIUM_MODEL", "anthropic/claude-opus-5.5")

# Loopback by default; another host only takes effect with ALLOW_REMOTE_ACCESS=true
# (api_auth.bind_host). Security-by-default rule, CLAUDE.md.
WS_HOST = os.getenv("WS_HOST", "127.0.0.1")
HTTP_HOST = os.getenv("HTTP_HOST", "127.0.0.1")
# No browser page calls this backend — only the WPF app (no Origin header).
DEFAULT_ALLOWED_ORIGINS = ()
WS_PORT = int(os.getenv("WS_PORT", "8865"))
HEALTH_PORT = int(os.getenv("HEALTH_PORT", "8867"))

MARKET_DATA_POLL_SECONDS = int(os.getenv("MARKET_DATA_POLL_SECONDS", "30"))

# ===== Financial Newsroom (moved from Global Monitoring, 2026-09-24) =====

# --- SEC EDGAR (insider trades) ---
# SEC's Fair Access policy requires every automated requester to identify itself with a
# descriptive User-Agent (name + contact email). This is NOT optional — EDGAR will start
# throttling/blocking requests with a generic or missing User-Agent. Set this in .env.
SEC_EDGAR_USER_AGENT = os.getenv("SEC_EDGAR_USER_AGENT", "")
# Official rate limit is 10 requests/second; we stay well under that by default.
SEC_EDGAR_MIN_REQUEST_INTERVAL_SECONDS = float(os.getenv("SEC_EDGAR_MIN_REQUEST_INTERVAL_SECONDS", "0.5"))
SEC_EDGAR_POLL_SECONDS = int(os.getenv("SEC_EDGAR_POLL_SECONDS", "120"))

# --- FRED (macro indicators) ---
# Free registration required: https://fred.stlouisfed.org/docs/api/api_key.html
FRED_API_KEY = os.getenv("FRED_API_KEY", "")
FRED_POLL_SECONDS = int(os.getenv("FRED_POLL_SECONDS", "21600"))  # macro series update slowly; 6h is plenty

# --- Economic Calendar (FRED release dates + FOMC, 2026-09-25) --- uses FRED_API_KEY
CALENDAR_POLL_SECONDS = int(os.getenv("CALENDAR_POLL_SECONDS", "21600"))  # release schedules change rarely; 6h

# --- Local SQLite store (2026-09-25, newsroom/db.py) ---
# HedgeFund/backend/data/newsroom.db (env UFOS_HEDGEFUND_DB overrides the path).
# Insider Trades shows only filings of the last INSIDER_DISPLAY_DAYS (by filing time,
# at most INSIDER_DISPLAY_MAX); the database keeps them INSIDER_RETENTION_DAYS
# (default 5, same as the display window). AI outputs are kept AI_RESULTS_RETENTION_DAYS (90).
INSIDER_DISPLAY_DAYS = int(os.getenv("INSIDER_DISPLAY_DAYS", "5"))
INSIDER_DISPLAY_MAX = int(os.getenv("INSIDER_DISPLAY_MAX", "300"))
INSIDER_RETENTION_DAYS = int(os.getenv("INSIDER_RETENTION_DAYS", "5"))
AI_RESULTS_RETENTION_DAYS = int(os.getenv("AI_RESULTS_RETENTION_DAYS", "90"))
DB_PRUNE_INTERVAL_SECONDS = 86400

LOG_DIR = os.path.join(os.path.dirname(__file__), "data", "logs")
