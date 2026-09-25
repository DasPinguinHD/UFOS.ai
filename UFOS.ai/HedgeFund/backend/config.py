"""Central configuration for the HedgeFund backend process."""
import os
from dotenv import load_dotenv

load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-chat-v3-0324")
# Newsroom "Analysis & Reasoning" (2026-09-25): model behind the "Premium" switch only;
# "Standard" uses OPENROUTER_MODEL. Must be an OpenRouter model id.
OPENROUTER_ANALYSIS_PREMIUM_MODEL = os.getenv("OPENROUTER_ANALYSIS_PREMIUM_MODEL", "anthropic/claude-opus-5.5")

WS_HOST = os.getenv("WS_HOST", "0.0.0.0")
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

LOG_DIR = os.path.join(os.path.dirname(__file__), "data", "logs")
