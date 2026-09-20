"""Central configuration: environment secrets and asset/ticker mappings."""
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-chat-v3-0324")

YOUTUBE_URL = os.getenv("YOUTUBE_URL", "")

WS_HOST = os.getenv("WS_HOST", "0.0.0.0")
WS_PORT = int(os.getenv("WS_PORT", "8765"))

MARKET_DATA_POLL_SECONDS = int(os.getenv("MARKET_DATA_POLL_SECONDS", "15"))

# Verdict / LLM analyst configuration
VERDICT_INTERVAL_SECONDS = int(os.getenv("VERDICT_INTERVAL_SECONDS", "90"))
VERDICT_MAX_REASON_CHARS = int(os.getenv("VERDICT_MAX_REASON_CHARS", "720"))
VERDICT_CONFIDENCE_SCALE = int(os.getenv("VERDICT_CONFIDENCE_SCALE", "100"))
# Initial delay before requesting the first verdict (seconds)
VERDICT_INITIAL_DELAY_SECONDS = int(os.getenv("VERDICT_INITIAL_DELAY_SECONDS", "60"))

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
