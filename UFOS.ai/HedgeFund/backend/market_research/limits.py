"""Market Research constants (SPEC §4.4) — the one place for limits and timings.

Part of UFOS.ai, a proof-of-concept portfolio project; research only, paper trading only.
"""

# --- Alpaca budget (Q3, SPEC §7.5) -------------------------------------------------------
SHARED_ALPACA_PER_MINUTE = 150          # = strategy_platform.data.http_gate.DEFAULT_RATES_PER_MINUTE["alpaca"] (SPEC §21.1)
RESEARCH_MAX_PER_MINUTE = 60
RESEARCH_RESERVE_TOKENS = 3
RESEARCH_ACQUIRE_TIMEOUT_SECONDS = 30         # review H1: short — a waiting Research call holds a Research thread
RESEARCH_EXECUTOR_WORKERS = 4                 # own pool, never asyncio's default executor (SPEC §5.1)

# --- streams / hub (SPEC §7.6) -----------------------------------------------------------
STOCK_STREAM_SYMBOL_LIMIT = 30
HUB_LINGER_SECONDS = 60
RECONNECT_BACKOFF_SECONDS = (2, 5, 10, 30, 60)
STREAM_SILENCE_SECONDS = 90
SIP_DELAY_MINUTES = 15
SIP_TAIL_REFRESH_SECONDS = 60
SIP_TAIL_MAX_SYMBOLS = 8

# --- Research WebSocket (SPEC §4.3) ------------------------------------------------------
WS_MAX_CLIENTS = 4
WS_MAX_MESSAGE_BYTES = 16384
WS_MAX_SUBSCRIPTIONS = 64
WS_HEARTBEAT_SECONDS = 20
WS_MAX_SERVER_MESSAGE_BYTES = 1048576         # server -> client frame cap (SPEC §4.3)
WS_MAX_BARS_PER_MESSAGE = 500                 # sip_replace / bar lists are chunked to this
LIVE_CANDLE_MAX_PER_SECOND = 4

# --- hub details (MR2b, SPEC §4.3 / §7.6 / §13 MR2b notes) -------------------------------
HUB_STABLE_SECONDS = 60                 # a stream connection live this long resets the reconnect backoff
RECHECK_MISSING_KEYS_SECONDS = 60       # "unavailable" re-check: keys missing / Credential Manager unreadable
RECHECK_REFUSED_SECONDS = 300           # "unavailable" re-check: 406 / keys rejected / plan refused
STREAM_CONNECT_TIMEOUT_SECONDS = 15     # TCP + TLS + WebSocket handshake to Alpaca
STREAM_HANDSHAKE_TIMEOUT_SECONDS = 10   # "connected" / "authenticated" answers
STREAM_PING_TIMEOUT_SECONDS = 10        # after STREAM_SILENCE_SECONDS without a frame: ping, no pong -> reconnect
STREAM_MAX_FRAME_BYTES = 4194304        # Alpaca -> hub frame cap
TRADE_MAX_PER_SECOND = 4                # "trade" messages per symbol (coalesced, newest wins)
FEEDS_MAX_PER_SECOND = 1                # "feeds" messages
WS_HELLO_TIMEOUT_SECONDS = 10
WS_SEND_QUEUE_MAX = 500
WS_BAD_MESSAGE_LIMIT = 5                # bad_message errors in a row -> close 1008
WS_MAX_TOPIC_CHARS = 64
SEED_RETRY_SECONDS = 60                 # live candle seed (REST 1m of the current bucket) retry after a failure
LATE_TRADE_GRACE_SECONDS = 15           # a trade / stream minute bar of a just-closed bucket arriving up to this long
                                        # after the bucket end is merged and the bar re-sent (correction); later ones are dropped

# --- request sizes / object limits -------------------------------------------------------
MAX_BARS_INTRADAY = 5000
MAX_BARS_DAILY = 2000
MAX_WATCHLISTS = 20
MAX_WATCHLIST_ITEMS = 200
MAX_NAME_CHARS = 40
MAX_TABS = 12
MAX_DRAWINGS_PER_SYMBOL = 500
MAX_DRAWING_POINTS = 10
MAX_BRUSH_POINTS = 500
MAX_NOTE_CHARS = 280
MAX_DRAWING_JSON_BYTES = 16384
MAX_IMPORT_BYTES = 262144
MAX_ALERTS = 200
ALERT_COOLDOWN_SECONDS = 900
ALERT_COOLDOWN_RANGE = (60, 86400)
MAX_LOGO_BYTES = 262144
MAX_TEXT_CHARS = 300                    # reasons / messages in responses
STATUS_POLL_SECONDS = 5                 # WPF poll interval of GET /research/feeds/status

# --- storage (SPEC §6) -------------------------------------------------------------------
DB_FILENAME = "market_research.db"
RETENTION_DAYS = {"bars_1m": 30, "news": 7, "logos": 30, "alert_log": 90, "audit": 365}
RETENTION_ALIASES = {"bars_1m_days": "bars_1m",   # MR2: coverage rows of bars_1m follow its retention
                     "news_symbols": "news", "news_images": "news"}   # MR6: follow the news retention
ASSET_REFRESH_HOURS = 24
MAINTENANCE_INTERVAL_SECONDS = 86400
STORE_REOPEN_SECONDS = 60

# --- MR2 data layer (SPEC §7, §13 MR2a notes) ------------------------------------------------
ASSET_REFRESH_RETRY_SECONDS = 1800      # asset_refresh task: next attempt after a failed refresh
ASSET_QUERY_MAX_CHARS = 32
ASSET_LIMIT_DEFAULT = 20
ASSET_LIMIT_MAX = 50
MAX_ASSET_NAME_CHARS = 120
INTRADAY_LOOKBACK_DAYS = 30             # = RETENTION_DAYS["bars_1m"]: oldest NY/UTC day served for 1m/5m/15m
MAX_TRANSIENT_SESSIONS = 130            # 1H/4H older than the 1m retention: SIP 30Min fetched per request, not stored
TRANSIENT_30M_DAYS = 300                # calendar days per SIP 30Min call (≤ 32 bars/session → ≤ ~6 900 bars)
ASSET_EMPTY_RETRY_SECONDS = 60          # empty asset table + failed refresh: next refresh attempt not before
FETCH_1M_DAYS = {"equity": 10, "crypto": 6}   # days per provider call (≤ ~10 000 bars = one Alpaca page)
CRYPTO_HOURLY_MAX_DAYS = 1800           # crypto 1H..1W window per request (hedgefund.db cache: ≤ 5 years of 1Hour)
STATS_DAILY_LOOKBACK_DAYS = 400         # daily history read for performance / 52W / avg volume

# --- MR4 symbol workspace (SPEC §13 "MR4 backend notes") ---------------------------------------
MAX_RECENT_SYMBOLS = 20                 # Ctrl+K palette: recent symbols kept (newest first)
MAX_WORKSPACE_BODY_BYTES = 32768        # serialized JSON body of a watchlist / session / recent request
SESSION_SCHEMA = 1                      # GET/PUT /research/session payload version
TAB_TYPES = ("candles", "line", "area")
SIDEBAR_SECTIONS = ("overview", "symbol", "alerts", "portfolio", "macro")   # portfolio/macro = MR8
QUOTE_LOOKUP_BUDGET_SECONDS = 15        # watchlist quotes: prev-close lookups per request, then complete=false
QUOTE_RETRY_SECONDS = 5                 # retry_after_seconds of an incomplete quotes answer
PREV_CLOSE_CACHE_SECONDS = 3600         # in-memory prev close per (symbol, session day)
PREV_CLOSE_CACHE_MAX = 2000
WORKSPACE_RELOAD_SECONDS = 30           # hub expansion of watchlist:<id> topics: reload spacing after a store failure

# --- MR5 drawings + indicators (SPEC §13 "MR5 backend notes") ------------------------------------
SPLIT_CHECK_INTERVAL_SECONDS = 86400    # drawing split check (corporate actions): daily, plus at start / after an import
SPLIT_CHECK_RETRY_SECONDS = 3600        # after a failed check (keys missing, Alpaca unreachable)
MAX_INDICATORS = 8                      # indicator specs per request / in the chart settings
MAX_SUB_PANES = 3                       # sub-pane indicators (rsi, macd); the chart's own volume pane is not counted
MAX_COMPARE = 2                         # compare overlays per request
IND_QUERY_MAX_CHARS = 400               # length of the ?ind= list
EMA_WARMUP_FACTOR = 4                   # extra bars loaded before the window: ema/macd 4·n
WILDER_WARMUP_FACTOR = 10               # rsi 10·n (Wilder smoothing decays slowly)

# --- MR6 news + logos (SPEC §13 "MR6 backend notes") ------------------------------------------------
NEWS_LIMIT_DEFAULT = 20                 # GET /research/symbols/{s}/news?limit= (1…NEWS_LIMIT_MAX)
NEWS_LIMIT_MAX = 50                     # = Alpaca's per-request maximum
NEWS_REFRESH_SECONDS = 60               # first page of a symbol: Alpaca asked again at most this often (else the store)
MAX_NEWS_HEADLINE_CHARS = 300
MAX_NEWS_SUMMARY_CHARS = 500
MAX_NEWS_PUBLISHER_CHARS = 100
MAX_NEWS_SYMBOLS = 50                   # symbol tags kept per item
NEWS_SEEN_MAX = 5000                    # stream de-dup memory (id -> updated_at)
NEWS_STREAM_QUEUE_MAX = 1000            # stream items waiting to be stored
NEWS_SOURCE = "Alpaca · Benzinga"       # feed label (SPEC §1.2 news feed)
NEWS_SOURCE_LABEL = "Benzinga via Alpaca"
MAX_NEWS_IMAGE_BYTES = 524288           # thumbnail proxy: larger answers are rejected
NEWS_IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif")   # no SVG, no WebP (review L6), nothing else
MAX_IMAGE_SIDE = 4096                   # pixels per side (header check; the WPF decodes with its own cap)
NEWS_IMAGE_TIMEOUT_SECONDS = 10         # connect / each read
NEWS_IMAGE_TOTAL_SECONDS = 20           # whole download
NEWS_IMAGE_MAX_REDIRECTS = 3            # each redirect target must pass the same https/host rules
NEWS_IMAGE_RETRY_SECONDS = 900          # a failed (network) download is tried again after this; rejected = never
NEWS_IMAGES_PER_MINUTE = 60             # own http_gate source "news_images" (not the Alpaca budget)
NEWS_IMAGE_GATE_WAIT_SECONDS = 5        # longest wait for a "news_images" gate token (review M2)
IMAGE_EXECUTOR_WORKERS = 2              # the thumbnail pool (executor.run_image) — never the Research pool (review M2)
IMAGE_QUEUE_MAX = 8                     # thumbnail jobs running + waiting; more → 503 "busy"
LOGO_NONE_DAYS = 7                      # negative cache: "Alpaca has no logo for X"
LOGO_PLAN_RECHECK_SECONDS = 86400       # "logos not on this plan": Alpaca asked again after this
MAX_LOGO_SIDE = 1024

# --- MR7 alerts (SPEC §13 "MR7 backend notes") ---------------------------------------------------------
ALERT_KINDS = ("price_cross", "line_cross", "day_move", "news")
ALERT_DAY_MOVE_PCT = (0.1, 50.0)        # day_move threshold range (percent vs previous close)
ALERT_MAX_LEVEL = 1e9                   # price levels: finite, 0 < level <= this
ALERT_RELOAD_SECONDS = 5                # alert task: rules + linked alert-line levels re-read at least this often
ALERT_PREV_CLOSE_RETRY_SECONDS = 60     # day_move: a failed previous-close lookup is retried after this
ALERT_NEWS_IDS_KEPT = 50                # news alerts: ids already handled (de-dup of updates / repeats)
ALERT_FIRE_QUEUE_MAX = 1000             # triggers waiting to be logged + sent
ALERT_LOG_LIMIT_DEFAULT = 100
ALERT_LOG_LIMIT_MAX = 500
MAX_ALERT_BODY_BYTES = 4096

# --- MR8 in-module integrations (SPEC §13 "MR8 backend notes") ---------------------------------------------
MAX_MARKERS = 2000                      # marker items per answer (then truncated: true)
MAX_MARKER_LABEL_CHARS = 160
MARKER_DEFAULT_DAYS = 365               # markers: default window ending at ?to= (or now)
MARKER_RANGE_MAX_DAYS = 7400            # ~20 years per request

# --- supervisor (SPEC §5) ----------------------------------------------------------------
RESTART_DELAYS_SECONDS = (5, 15, 30, 60, 120)
MAX_RESTARTS = 5
RESTART_WINDOW_SECONDS = 900
HEALTHY_AFTER_SECONDS = 900
SHUTDOWN_TIMEOUT_SECONDS = 2.0

TRADING_UNAFFECTED = "Trading is not affected."


def clip(text, limit: int = MAX_TEXT_CHARS) -> str:
    """One-line plain text of at most ``limit`` characters (control characters removed)."""
    s = " ".join(str(text if text is not None else "").split())
    s = "".join(ch for ch in s if ch.isprintable())
    return s if len(s) <= limit else s[: max(0, limit - 1)].rstrip() + "…"
