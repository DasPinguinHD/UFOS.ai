"""Central configuration: settings and asset/ticker mappings.

Stage 10, S10-E (user decision 2026-10-24): no .env any more. Secrets (Deepgram key, the shared OpenRouter key) are
read at use time from the Windows Credential Manager (credstore.py, ⚙ Keys & data); the settings below come from the
optional data/settings.json (settings_file.py — a missing file/key = the default here; an invalid one refuses the
start). youtube_url is written by the LiveStream Agent window (ENV_API §4a: atomically, then the backend restarts).
The servers always listen on 127.0.0.1 (no host switches since Docker was removed); only the CORS_ALLOWED_ORIGINS
and DB / cost-log path overrides remain as process environment (tests).
"""
import os
from dataclasses import dataclass, field

import settings_file

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(BACKEND_DIR, "data", settings_file.FILE_NAME)
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OPENROUTER_MODEL = "deepseek/deepseek-chat-v3-0324"
# data/settings.json keys (lower-case former .env names): (kind, default, …) — settings_file.py
SCHEMA = {
    "youtube_url": ("youtube_url", ""),
    "openrouter_base_url": ("openrouter_url", DEFAULT_OPENROUTER_BASE_URL),
    "openrouter_model": ("str", DEFAULT_OPENROUTER_MODEL, 200),
    "market_data_poll_seconds": ("int", 15, 1, 86400),
    "verdict_interval_seconds": ("int", 90, 1, 86400),
    "verdict_max_reason_chars": ("int", 720, 50, 20000),
    "verdict_confidence_scale": ("int", 100, 1, 1000),
    "verdict_initial_delay_seconds": ("int", 60, 0, 86400),
}
SETTINGS = settings_file.load_or_exit(SETTINGS_PATH, SCHEMA)

# The keys: credstore "openrouter-api-key" (shared by all modules) and "deepgram-api-key", read at use time.
OPENROUTER_BASE_URL = SETTINGS["openrouter_base_url"]
OPENROUTER_MODEL = SETTINGS["openrouter_model"]

YOUTUBE_URL = SETTINGS["youtube_url"]

# Always 127.0.0.1 (api_auth.bind_host) — security-by-default rule, CLAUDE.md; no host switches since stage 10.
# No browser page calls this backend — only the WPF app (no Origin header).
DEFAULT_ALLOWED_ORIGINS = ()
WS_PORT = 8765          # fixed: the app connects to 8765 / 8766 (no .env override since stage 10)

MARKET_DATA_POLL_SECONDS = SETTINGS["market_data_poll_seconds"]

# Verdict / LLM analyst configuration
VERDICT_INTERVAL_SECONDS = SETTINGS["verdict_interval_seconds"]
VERDICT_MAX_REASON_CHARS = SETTINGS["verdict_max_reason_chars"]
VERDICT_CONFIDENCE_SCALE = SETTINGS["verdict_confidence_scale"]
# Initial delay before requesting the first verdict (seconds)
VERDICT_INITIAL_DELAY_SECONDS = SETTINGS["verdict_initial_delay_seconds"]

# Deepgram expects raw PCM at this rate/format from the ffmpeg transcode step.
AUDIO_SAMPLE_RATE = 16000
AUDIO_CHANNELS = 1

LOG_DIR = os.path.join(os.path.dirname(__file__), "data", "logs")
SCENARIOS_DIR = os.path.join(os.path.dirname(__file__), "data", "scenarios")

# nested market-data groups used by the live UI and analyst prompts
TICKERS: dict[str, dict[str, list[str]]] = {
    "small_grid": {
        "treasury_yields_core": ["BIL", "^TNX", "^TYX"],
        "liquidity_anchors": ["UUP", "GLD"],
        "equity_indices": ["QQQ", "IWM"],
        "banking_stress": ["KRE"],
    },
    "expanded_grid_extras": {
        "bond_proxies": ["TLT", "AGG"],
        "reits": ["VNQ"],
        "utilities": ["XLU"],
        "homebuilders": ["XHB", "ITB"],
        "tech_giants_extended": ["MAGS"],
        "unprofitable_growth": ["ARKK"],
        "financials_broad": ["XLF"],
        "credit_risk": ["HYG"],
    },
}


def iter_ticker_groups() -> list[tuple[str, str, list[str]]]:
    groups: list[tuple[str, str, list[str]]] = []
    for section_name, sections in TICKERS.items():
        for group_name, symbols in sections.items():
            groups.append((section_name, group_name, symbols))
    return groups


def all_ticker_symbols() -> list[str]:
    symbols: list[str] = []
    seen: set[str] = set()
    for _, _, group_symbols in iter_ticker_groups():
        for symbol in group_symbols:
            if symbol not in seen:
                seen.add(symbol)
                symbols.append(symbol)
    return symbols



@dataclass
class TickerQuote:
    symbol: str
    price: float | None = None
    change_percent: float | None = None


@dataclass
class MarketSnapshot:
    quotes: dict[str, TickerQuote] = field(default_factory=dict)
    timestamp: str | None = None
