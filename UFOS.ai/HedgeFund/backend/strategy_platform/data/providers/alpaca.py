"""Alpaca market data (alpaca-py >= 0.44, official SDK) — SPEC §3.

Free "Basic" plan, which this module is written for:
- stocks/ETFs: IEX feed only (``feed="iex"``, real time, IEX volume only),
  history since 2016 (``HISTORY_START``; earlier requests raise
  ``HistoryNotAvailable``), the latest 15 minutes of SIP data are restricted
  (not used: always IEX); bars ``adjustment="all"`` (splits + dividends) or, on request,
  ``adjustment="split"`` (splits only — SPEC §8.1; same feed, gate and cache rules);
- crypto: ``CryptoHistoricalDataClient`` (US feed); crypto history needs no
  key, the keys are sent anyway so every call takes the same code path;
- news: ``NewsClient`` (Benzinga headlines); corporate actions (cash
  dividends, splits): ``CorporateActionsClient``;
- asset list (Market Research MR2, ``market_research/SPEC.md`` §12): ``assets()``
  via the trading API ``TradingClient(paper=True).get_all_assets`` (active US
  equities + crypto), same keys, same gate (data type ``assets``);
- logos (Market Research MR6, ``market_research/SPEC.md`` §12): ``logo(symbol)`` =
  ``GET https://data.alpaca.markets/v1beta1/logos/{symbol}`` (no placeholder image), plain
  ``requests`` through the same gate (data type ``logos``); HTTP 404 -> ``None`` (no logo),
  HTTP 401/403 -> ``LogosNotAvailable`` (a paid add-on, usually not on the free plan);
  news dicts carry ``images`` (https URLs only, MR6);
- 200 requests/min per account -> the HTTP gate allows 180/min.

Keys: ``secrets.require_secret("alpaca-paper-key-id" / "alpaca-paper-secret-key")``
read at every use (a key changed in Settings applies at once); never logged.
A client is built per call (no long-lived session holding the keys).

Errors -> ``UserFacingError`` (``classify_error``): HTTP 401/403 ->
``SourceAuthRejected`` "Alpaca rejected the keys — check them in Hedge Fund →
Settings → Keys." (a 403 about the plan/subscription says so instead);
429 / 5xx / timeouts -> retried by the gate, then ``DataSourceUnavailable``;
network/TLS errors and other 4xx -> ``DataSourceUnavailable`` at once.
The SDK's own retry loop (429/504, 3 x 3 s) is switched off — the gate is the
only place that retries — and every HTTP request gets a timeout
(``REQUEST_TIMEOUT_SECONDS``; the SDK sets none). Both use SDK internals
(``client._retry``, ``client._session``), see ``harden_client``.

``raw_data=True``: the SDK returns the API's JSON (``{"SPY": [{"t", "o",
"h", "l", "c", "v", "n", "vw"}]}``), parsed here into the dicts of
providers/base.py — no pandas, no SDK models.

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

import threading
import logging
import math
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Callable, Iterable, List, Optional

from ...core.errors import DataSourceUnavailable, UserFacingError
from ...core.instruments import from_alpaca_symbol, instrument
from .. import http_gate, pit
from ..http_gate import Retry
from .base import (CORPORATE_ACTION_KINDS, TIMEFRAMES, HistoryNotAvailable, Provider, SourceAuthRejected,
                   bars_data_type, iso, iso_precise, parse_ts, quotes_data_type)

logger = logging.getLogger(__name__)

SOURCE = "alpaca"
# Historical daily bars for backtests (user decision 2026-10-06): the free plan's IEX history
# starts 2020-07 for most ETFs, SIP history since 2016 is free for queries whose END is at least
# 15 minutes old (Alpaca Market Data FAQ: "For historical queries, the `end` parameter must be at
# least 15 minutes old to query SIP data without a subscription"). Own source label, so SIP rows
# never mix with the IEX rows of the live paper engine in the cache.
SIP_SOURCE = "alpaca_sip"
HISTORY_DATA_TYPE = "equity_bars_history"
SIP_DELAY = timedelta(minutes=15)
# Market Research (MR2a review M4): 30-minute bars for intraday history older than its 1m store —
# get_bars only (never through the hedgefund.db cache / pit, which keep 1Min/1Hour/1Day).
RESEARCH_TIMEFRAME = "30Min"
RESEARCH_TIMEFRAME_LENGTH = timedelta(minutes=30)
SIP_PLAN_MESSAGE = ("Alpaca refused SIP historical data — without a market-data subscription SIP history is "
                    "only available for queries ending at least 15 minutes ago; check your Alpaca account "
                    "(no IEX data is used instead)")
KEY_ID_SECRET = "alpaca-paper-key-id"
SECRET_KEY_SECRET = "alpaca-paper-secret-key"
HISTORY_START = date(2016, 1, 1)
REQUEST_TIMEOUT_SECONDS = 20.0
AUTH_MESSAGE = "Alpaca rejected the keys — check them in Hedge Fund → Settings → Keys."
DATA_TYPES = ("equity_bars", "crypto_bars", "equity_quotes", "crypto_quotes", "news", "corporate_actions")
# corporate actions: at most this many symbols and days per request (SPEC §10.1, weak connections: small
# requests time out less; the chunks partition the request, the merged rows equal one big request's)
CORPORATE_ACTIONS_MAX_SYMBOLS = 10
CORPORATE_ACTIONS_MAX_DAYS = 3 * 365          # <= 3 years
_CA_KEYS = {"cash_dividends": "cash_dividend", "forward_splits": "forward_split", "reverse_splits": "reverse_split"}
_CLIENTS = {"stock": "StockHistoricalDataClient", "crypto": "CryptoHistoricalDataClient", "news": "NewsClient",
            "corporate_actions": "CorporateActionsClient"}


_SDK_LOCK = threading.Lock()
_SDK: Optional[SimpleNamespace] = None


def load_sdk(data_type: str = "equity_bars") -> SimpleNamespace:
    """The alpaca-py classes this module uses (lazy import).

    Imported once, under a lock: alpaca-py's packages import each other, and
    two worker threads importing them at the same time raised
    ``_DeadlockError`` on Windows/Python 3.14 (seen 2026-10-02/03)."""
    global _SDK
    with _SDK_LOCK:
        if _SDK is None:
            _SDK = _import_sdk(data_type)
        return _SDK


def _import_sdk(data_type: str) -> SimpleNamespace:
    try:
        from alpaca.data.enums import Adjustment, CorporateActionsType, DataFeed
        from alpaca.data.historical import CryptoHistoricalDataClient, NewsClient, StockHistoricalDataClient
        from alpaca.data.historical.corporate_actions import CorporateActionsClient
        from alpaca.data.requests import (CorporateActionsRequest, CryptoBarsRequest, CryptoLatestQuoteRequest,
                                          CryptoLatestTradeRequest, NewsRequest, StockBarsRequest,
                                          StockLatestQuoteRequest, StockLatestTradeRequest)
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    except ImportError as ex:
        raise DataSourceUnavailable(SOURCE, data_type, "the alpaca-py package is not installed in the HedgeFund "
                                    "backend environment (restart the app so it installs requirements.txt)") from ex
    return SimpleNamespace(
        Adjustment=Adjustment, CorporateActionsType=CorporateActionsType, DataFeed=DataFeed,
        CryptoHistoricalDataClient=CryptoHistoricalDataClient, NewsClient=NewsClient,
        StockHistoricalDataClient=StockHistoricalDataClient, CorporateActionsClient=CorporateActionsClient,
        CorporateActionsRequest=CorporateActionsRequest, CryptoBarsRequest=CryptoBarsRequest,
        CryptoLatestQuoteRequest=CryptoLatestQuoteRequest, CryptoLatestTradeRequest=CryptoLatestTradeRequest,
        NewsRequest=NewsRequest, StockBarsRequest=StockBarsRequest, StockLatestQuoteRequest=StockLatestQuoteRequest,
        StockLatestTradeRequest=StockLatestTradeRequest, TimeFrame=TimeFrame, TimeFrameUnit=TimeFrameUnit)


def harden_client(client, timeout: float = REQUEST_TIMEOUT_SECONDS, on_send: Optional[Callable[[], None]] = None):
    """No SDK-internal retries (the gate retries, bounded) and a timeout on
    every HTTP request. Relies on alpaca-py's RESTClient internals
    (``_retry`` and the requests ``_session``) — checked in the tests with a
    fake; re-check after an alpaca-py upgrade. ``on_send`` (the gate's
    ``note_send``, MR2a) is called before EVERY HTTP request — alpaca-py pages
    one call into several requests, and research-priority calls take a permit
    per request (market_research/SPEC.md §7.5)."""
    if hasattr(client, "_retry"):
        client._retry = 0
    session = getattr(client, "_session", None)
    original = getattr(session, "request", None)
    if session is not None and callable(original) and not getattr(original, "_ufos_timeout", False):
        def request(method, url, **kw):
            kw.setdefault("timeout", timeout)
            if on_send is not None:
                on_send()
            return original(method, url, **kw)
        request._ufos_timeout = True
        session.request = request
    return client


def http_status(ex) -> Optional[int]:
    try:
        v = getattr(ex, "status_code", None)
    except Exception:  # noqa: BLE001 — a property of a foreign exception
        v = None
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    v = getattr(getattr(ex, "response", None), "status_code", None)
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _api_message(ex) -> str:
    text = None
    try:
        text = ex.message          # APIError: the JSON body's "message"
    except Exception:  # noqa: BLE001
        text = None
    if not isinstance(text, str) or not text.strip():
        text = str(ex)
    return " ".join(text.split())[:200]


def _retry_after(ex) -> Optional[float]:
    try:
        v = getattr(getattr(ex, "response", None), "headers", {}).get("Retry-After")
        return float(v) if v is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


def classify_error(ex: BaseException, data_type: str):
    """Gate classifier: ``Retry`` for 429 / 5xx / timeouts, else the
    UserFacingError to raise."""
    status = http_status(ex)
    if status is not None:
        msg = _api_message(ex)
        if status in (401, 403):
            low = msg.lower()
            if any(w in low for w in ("subscription", "not permitted", "not entitled")):
                if data_type == HISTORY_DATA_TYPE:
                    return DataSourceUnavailable(SIP_SOURCE, data_type, f"{SIP_PLAN_MESSAGE} ({msg})")
                return DataSourceUnavailable(SOURCE, data_type,
                                             f"your Alpaca market-data plan does not include this request ({msg})")
            return SourceAuthRejected(SOURCE, data_type, AUTH_MESSAGE, detail=f"HTTP {status}")
        if status == 429:
            return Retry("Alpaca rate limit reached (HTTP 429)", _retry_after(ex))
        if 500 <= status <= 599:
            return Retry(f"Alpaca server error (HTTP {status})", _retry_after(ex))
        return DataSourceUnavailable(SOURCE, data_type, f"Alpaca refused the request (HTTP {status}: {msg})")
    names = [c.__name__ for c in type(ex).__mro__]
    if isinstance(ex, TimeoutError) or any("Timeout" in n for n in names):
        return Retry("the request to Alpaca timed out")
    if isinstance(ex, OSError) or any("ConnectionError" in n for n in names):
        return DataSourceUnavailable(SOURCE, data_type, "network/TLS error — data.alpaca.markets could not be reached")
    if isinstance(ex, (ValueError, KeyError, TypeError, AttributeError)):
        return DataSourceUnavailable(SOURCE, data_type, f"unexpected response from Alpaca ({type(ex).__name__})")
    return DataSourceUnavailable(SOURCE, data_type, f"unexpected error ({type(ex).__name__})")


def news_symbol(symbol: str) -> str:
    """Alpaca/Benzinga news tags crypto as "BTCUSD"; equities as is."""
    inst = instrument(symbol)
    return inst.alpaca_symbol.replace("/", "") if inst.asset_class == "crypto" else inst.alpaca_symbol


def _finite(v, field: str) -> float:
    f = float(v)
    if not math.isfinite(f):
        raise ValueError(f"{field} not finite")
    return f


def _opt_float(v) -> Optional[float]:
    if v is None:
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _opt_int(v) -> Optional[int]:
    f = _opt_float(v)
    return int(f) if f is not None and f >= 0 else None


def _safe_url(url) -> Optional[str]:
    if isinstance(url, str) and url.strip().lower().startswith(("https://", "http://")):
        return url.strip()[:1000]
    return None


def _news_images(values) -> List[dict]:
    """MR6: the news item's images as [{size, url}] — https URLs only (``_safe_url``), ≤ 5."""
    out = []
    for im in values if isinstance(values, list) else []:
        url = _safe_url(im.get("url")) if isinstance(im, dict) else None
        if url and url.lower().startswith("https://"):
            out.append({"size": str(im.get("size") or "")[:10], "url": url})
    return out[:5]


def _clean(text, limit: int) -> Optional[str]:
    if not isinstance(text, str):
        return None
    t = " ".join(text.split())
    return t[:limit] if t else None


def _day(value) -> date:
    if isinstance(value, datetime):
        return parse_ts(value).date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _now_fn(clock) -> Callable[[], datetime]:
    if clock is None:
        return lambda: datetime.now(timezone.utc)
    if hasattr(clock, "now_utc"):
        return clock.now_utc
    return clock


def sip_request_end(timeframe: str, now: datetime) -> datetime:
    """Latest request ``end`` allowed for SIP history without a subscription.

    Intraday: now − 15 min. Daily bars (fix 2026-10-06, real failure during market hours):
    a window reaching into TODAY's session returns today's in-progress daily bar, which contains
    the last 15 minutes -> Alpaca refuses ("subscription does not permit querying recent SIP
    data") even with end = now − 15 min. So the end is the close of the last COMPLETED NYSE
    session whose close is at least 15 minutes ago (core.calendars; during/before today's session
    and until close + 15 min -> the previous session's close; later -> today's close)."""
    now = parse_ts(now)
    if timeframe == "1Day":
        from ...core import calendars
        return calendars.last_close_before(now - SIP_DELAY)
    return now - SIP_DELAY


# Market Research MR6: logos. Alpaca documents the endpoint as a paid add-on of its Broker API (blog "Display
# Logos of Popular Crypto & Stock Symbols with Broker API": Essential/Core plans, additional cost) — on the free
# plan a 401/403 is expected and reported as LogosNotAvailable (verify against the live API).
LOGO_DATA_TYPE = "logos"
LOGO_URL = "https://data.alpaca.markets/v1beta1/logos/{symbol}"
LOGO_MAX_BYTES = 262144
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
LOGOS_PLAN_HINT = ("Alpaca offers the logo endpoint as a paid add-on (Broker API); it is usually not part of the "
                   "free plan")


class LogosNotAvailable(DataSourceUnavailable):
    """Alpaca refused the logo request (HTTP 401/403): the account's plan does not include logos."""

    def __init__(self, status: int, message: str):
        super().__init__(SOURCE, LOGO_DATA_TYPE, f"Alpaca refused the logo request (HTTP {status}: {message}) — "
                                                 f"{LOGOS_PLAN_HINT}")
        self.status = status


class _HttpStatus(Exception):
    """A non-200 answer of a plain ``requests`` call, shaped for ``http_status`` / ``_api_message``."""

    def __init__(self, status_code: int, message: str):
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code
        self.message = message or f"HTTP {status_code}"


def _logo_classify(ex: BaseException, data_type: str):
    status = http_status(ex)
    if status in (401, 403):
        return LogosNotAvailable(status, _api_message(ex))
    return classify_error(ex, data_type)


def load_requests():
    try:
        import requests
    except ImportError as ex:
        raise DataSourceUnavailable(SOURCE, LOGO_DATA_TYPE, "the requests package is not installed in the HedgeFund "
                                    "backend environment (restart the app so it installs requirements.txt)") from ex
    return requests


ASSETS_DATA_TYPE = "assets"
ASSET_CLASSES = ("us_equity", "crypto")
_TRADING_SDK: Optional[SimpleNamespace] = None


def load_trading_sdk() -> SimpleNamespace:
    """alpaca-py's trading-API classes for ``assets()`` (lazy, under the same import lock)."""
    global _TRADING_SDK
    with _SDK_LOCK:
        if _TRADING_SDK is None:
            try:
                from alpaca.trading.client import TradingClient
                from alpaca.trading.enums import AssetClass, AssetStatus
                from alpaca.trading.requests import GetAssetsRequest
            except ImportError as ex:
                raise DataSourceUnavailable(SOURCE, ASSETS_DATA_TYPE, "the alpaca-py package is not installed in "
                                            "the HedgeFund backend environment (restart the app so it installs "
                                            "requirements.txt)") from ex
            _TRADING_SDK = SimpleNamespace(TradingClient=TradingClient, AssetClass=AssetClass,
                                           AssetStatus=AssetStatus, GetAssetsRequest=GetAssetsRequest)
        return _TRADING_SDK


class AlpacaProvider(Provider):
    name = SOURCE
    data_types = DATA_TYPES
    feed = "iex"

    def __init__(self, *, gate=None, sdk=None, secrets_module=None, clock=None,
                 timeout: float = REQUEST_TIMEOUT_SECONDS, trading_sdk=None, http=None):
        self._gate_obj = gate
        self._sdk = sdk
        self._trading_sdk = trading_sdk
        self._http = http              # MR6 logo(): a requests-like session factory (tests); default requests.Session
        self._secrets = secrets_module
        self._now = _now_fn(clock)
        self.timeout = float(timeout)

    # --- plumbing ---------------------------------------------------------------------

    def _gate(self):
        return self._gate_obj if self._gate_obj is not None else http_gate.gate_for(SOURCE)

    def _load(self, data_type: str):
        return self._sdk if self._sdk is not None else load_sdk(data_type)

    def _keys(self):
        sec = self._secrets
        if sec is None:
            from ... import secrets as sec
        return sec.require_secret(KEY_ID_SECRET), sec.require_secret(SECRET_KEY_SECRET)

    def _client(self, kind: str, data_type: str):
        sdk = self._load(data_type)
        key_id, secret_key = self._keys()
        cls = getattr(sdk, _CLIENTS[kind])
        return harden_client(cls(api_key=key_id, secret_key=secret_key, raw_data=True), self.timeout,
                             on_send=self._send_hook())

    def _send_hook(self):
        return getattr(self._gate(), "note_send", None)

    def _call(self, data_type: str, fn):
        return self._gate().call(fn, data_type=data_type, classify=classify_error)

    @staticmethod
    def _parse(data_type: str, fn):
        try:
            return fn()
        except UserFacingError:
            raise
        except (ValueError, KeyError, TypeError, AttributeError) as ex:
            raise DataSourceUnavailable(SOURCE, data_type,
                                        f"unexpected data from Alpaca ({type(ex).__name__}: {str(ex)[:120]})") from ex

    @staticmethod
    def _timeframe(sdk, timeframe: str):
        if timeframe == RESEARCH_TIMEFRAME:          # 30-minute bars (Market Research, not via the cache)
            return sdk.TimeFrame(30, sdk.TimeFrameUnit.Minute)
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"unknown timeframe {timeframe!r}")
        unit = {"1Day": sdk.TimeFrameUnit.Day, "1Hour": sdk.TimeFrameUnit.Hour,
                "1Min": sdk.TimeFrameUnit.Minute}[timeframe]
        return sdk.TimeFrame(1, unit)

    @staticmethod
    def _series(raw, key: str) -> list:
        if raw is None:
            return []
        if not isinstance(raw, dict):
            raise TypeError(f"expected a JSON object, got {type(raw).__name__}")
        items = raw.get(key)
        if items is None:
            return []
        if not isinstance(items, list):
            raise TypeError(f"expected a list for {key}")
        return items

    @staticmethod
    def _latest(raw, key: str, data_type: str, what: str, symbol: str) -> dict:
        item = raw.get(key) if isinstance(raw, dict) else None
        if not isinstance(item, dict):
            raise DataSourceUnavailable(SOURCE, data_type, f"no {what} for {symbol} was returned")
        return item

    # --- bars --------------------------------------------------------------------------

    def get_bars(self, symbol: str, timeframe: str, start, end, adjustment: str = "all") -> List[dict]:
        inst = instrument(symbol)
        sip = self.feed == "sip"
        if sip and inst.asset_class != "equity":
            raise ValueError("SIP history is for equities/ETFs only")
        data_type = HISTORY_DATA_TYPE if sip else bars_data_type(inst.asset_class)
        if adjustment not in ("all", "split"):
            raise ValueError(f"unknown adjustment {adjustment!r}")
        if adjustment != "all" and inst.asset_class != "equity":
            raise ValueError(f"adjustment {adjustment!r} applies to equities only")
        start, end = parse_ts(start), parse_ts(end)
        if inst.asset_class == "equity" and start.date() < HISTORY_START:
            raise HistoryNotAvailable(self.name, HISTORY_START.isoformat())
        if sip:
            limit = sip_request_end(timeframe, parse_ts(self._now()))    # the free plan's SIP rule
            end = min(end, limit)
            logger.debug("alpaca_sip %s %s: effective request end %s (limit %s)", inst.symbol, timeframe,
                         iso(end), iso(limit))
            if end <= start:
                return []
        sdk = self._load(data_type)
        tf = self._timeframe(sdk, timeframe)
        if inst.asset_class == "crypto":
            request = sdk.CryptoBarsRequest(symbol_or_symbols=[inst.alpaca_symbol], timeframe=tf, start=start, end=end)
            client = self._client("crypto", data_type)
            raw = self._call(data_type, lambda: client.get_crypto_bars(request))
        else:
            request = sdk.StockBarsRequest(symbol_or_symbols=[inst.alpaca_symbol], timeframe=tf, start=start, end=end,
                                           adjustment=sdk.Adjustment.ALL if adjustment == "all" else
                                           sdk.Adjustment.SPLIT,
                                           feed=sdk.DataFeed.SIP if sip else sdk.DataFeed.IEX)
            client = self._client("stock", data_type)
            raw = self._call(data_type, lambda: client.get_stock_bars(request))
        fetched_at = iso(self._now())

        def rows():
            out = []
            for item in self._series(raw, inst.alpaca_symbol):
                ts = parse_ts(item["t"])
                out.append({
                    "symbol": inst.symbol, "timeframe": timeframe, "ts": iso(ts),
                    "open": _finite(item["o"], "open"), "high": _finite(item["h"], "high"),
                    "low": _finite(item["l"], "low"), "close": _finite(item["c"], "close"),
                    "volume": _opt_float(item.get("v")), "vwap": _opt_float(item.get("vw")),
                    "trade_count": _opt_int(item.get("n")), "source": self.name,
                    "known_at": (iso(ts + RESEARCH_TIMEFRAME_LENGTH) if timeframe == RESEARCH_TIMEFRAME else
                                 pit.known_at_iso(inst.asset_class, timeframe, ts)), "fetched_at": fetched_at})
            out.sort(key=lambda r: r["ts"])
            return out
        return self._parse(data_type, rows)

    # --- latest quote / trade ----------------------------------------------------------

    def latest_quote(self, symbol: str) -> dict:
        inst = instrument(symbol)
        data_type = quotes_data_type(inst.asset_class)
        sdk = self._load(data_type)
        if inst.asset_class == "crypto":
            request = sdk.CryptoLatestQuoteRequest(symbol_or_symbols=[inst.alpaca_symbol])
            client = self._client("crypto", data_type)
            raw = self._call(data_type, lambda: client.get_crypto_latest_quote(request))
        else:
            request = sdk.StockLatestQuoteRequest(symbol_or_symbols=[inst.alpaca_symbol], feed=sdk.DataFeed.IEX)
            client = self._client("stock", data_type)
            raw = self._call(data_type, lambda: client.get_stock_latest_quote(request))
        item = self._latest(raw, inst.alpaca_symbol, data_type, "quote", inst.symbol)
        return self._parse(data_type, lambda: {
            "symbol": inst.symbol, "bid": _opt_float(item.get("bp")), "ask": _opt_float(item.get("ap")),
            "bid_size": _opt_float(item.get("bs")), "ask_size": _opt_float(item.get("as")),
            "ts": iso_precise(item["t"]), "source": SOURCE})

    def latest_trade(self, symbol: str) -> dict:
        inst = instrument(symbol)
        data_type = quotes_data_type(inst.asset_class)
        sdk = self._load(data_type)
        if inst.asset_class == "crypto":
            request = sdk.CryptoLatestTradeRequest(symbol_or_symbols=[inst.alpaca_symbol])
            client = self._client("crypto", data_type)
            raw = self._call(data_type, lambda: client.get_crypto_latest_trade(request))
        else:
            request = sdk.StockLatestTradeRequest(symbol_or_symbols=[inst.alpaca_symbol], feed=sdk.DataFeed.IEX)
            client = self._client("stock", data_type)
            raw = self._call(data_type, lambda: client.get_stock_latest_trade(request))
        item = self._latest(raw, inst.alpaca_symbol, data_type, "trade", inst.symbol)
        return self._parse(data_type, lambda: {
            "symbol": inst.symbol, "price": _finite(item["p"], "price"), "size": _opt_float(item.get("s")),
            "ts": iso_precise(item["t"]), "source": SOURCE})

    # --- news ----------------------------------------------------------------------------

    def news(self, symbols: Iterable[str], start=None, end=None, limit: int = 10) -> List[dict]:
        data_type = "news"
        tags = [news_symbol(s) for s in symbols]
        if not tags:
            return []
        sdk = self._load(data_type)
        kw = {"symbols": ",".join(tags), "limit": max(1, min(50, int(limit))), "sort": "desc",
              "include_content": False}
        if start is not None:
            kw["start"] = parse_ts(start)
        if end is not None:
            kw["end"] = parse_ts(end)
        request = sdk.NewsRequest(**kw)
        client = self._client("news", data_type)
        raw = self._call(data_type, lambda: client.get_news(request))

        def items():
            out = []
            for it in self._series(raw, "news"):
                headline = _clean(it.get("headline"), 300)
                if not headline:
                    continue
                created = it.get("created_at")
                out.append({"id": it.get("id"), "headline": headline, "summary": _clean(it.get("summary"), 500),
                            "publisher": _clean(it.get("source"), 100), "url": _safe_url(it.get("url")),
                            "created_at": iso(created) if created else None,
                            "updated_at": iso(it["updated_at"]) if it.get("updated_at") else None,
                            "symbols": [str(s)[:20] for s in (it.get("symbols") or [])][:50],
                            "images": _news_images(it.get("images")), "source": SOURCE})
            out.sort(key=lambda n: n["created_at"] or "", reverse=True)
            return out
        return self._parse(data_type, items)

    # --- logos (Market Research MR6) -------------------------------------------------------------

    def logo(self, symbol: str) -> Optional[bytes]:
        """PNG bytes of the symbol's logo, or ``None`` when Alpaca has none (HTTP 404). No placeholder image is
        requested. Checked: HTTP 200, content type ``image/png``, PNG signature, ≤ ``LOGO_MAX_BYTES`` (read in
        chunks, stops above it), no redirects; anything else -> ``DataSourceUnavailable``; HTTP 401/403 ->
        ``LogosNotAvailable``. Crypto symbols use the news spelling (``BTCUSD``; verify)."""
        data_type = LOGO_DATA_TYPE
        from urllib.parse import quote
        url = LOGO_URL.format(symbol=quote(news_symbol(symbol), safe=""))
        key_id, secret_key = self._keys()
        factory = self._http if self._http is not None else load_requests().Session
        hook = self._send_hook()

        def fetch():
            session = factory()
            try:
                if hook is not None:
                    hook()
                resp = session.get(url, headers={"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key,
                                                 "Accept": "image/png"},
                                   timeout=self.timeout, stream=True, allow_redirects=False)
                try:
                    status = int(resp.status_code)
                    if status == 404:
                        return None
                    if status != 200:
                        try:
                            text = " ".join(str(resp.text or "").split())[:200]
                        except Exception:  # noqa: BLE001 — the body is only for the message
                            text = ""
                        raise _HttpStatus(status, text)
                    ctype = str(resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                    if ctype != "image/png":
                        raise DataSourceUnavailable(SOURCE, data_type, f"the logo is not a PNG image ({ctype[:40] or 'no content type'})")
                    body = bytearray()
                    for chunk in resp.iter_content(16384):
                        body.extend(chunk or b"")
                        if len(body) > LOGO_MAX_BYTES:
                            raise DataSourceUnavailable(SOURCE, data_type, f"the logo is larger than {LOGO_MAX_BYTES} bytes")
                    if not bytes(body).startswith(PNG_SIGNATURE):
                        raise DataSourceUnavailable(SOURCE, data_type, "the logo is not a valid PNG file")
                    return bytes(body)
                finally:
                    resp.close()
            finally:
                close = getattr(session, "close", None)
                if callable(close):
                    close()
        return self._gate().call(fetch, data_type=data_type, classify=_logo_classify)

    # --- asset list (Market Research MR2) -----------------------------------------------------

    def assets(self) -> List[dict]:
        """Active Alpaca assets: [{symbol (Alpaca spelling, e.g. "AAPL", "BRK.B", "BTC/USD"), name,
        asset_class: "us_equity"|"crypto", exchange, tradable, status, attributes}] — names are untrusted
        text (whitespace-normalised, capped). One gate call per asset class."""
        data_type = ASSETS_DATA_TYPE
        sdk = self._trading_sdk if self._trading_sdk is not None else load_trading_sdk()
        key_id, secret_key = self._keys()
        client = harden_client(sdk.TradingClient(api_key=key_id, secret_key=secret_key, paper=True, raw_data=True),
                               self.timeout, on_send=self._send_hook())
        raws = []
        for cls in ASSET_CLASSES:
            request = sdk.GetAssetsRequest(status=sdk.AssetStatus.ACTIVE, asset_class=sdk.AssetClass(cls))
            raws.append((cls, self._call(data_type, lambda request=request: client.get_all_assets(request))))

        def rows():
            out = []
            for cls, raw in raws:
                if raw is None:
                    continue
                if not isinstance(raw, list):
                    raise TypeError(f"expected a JSON list of assets, got {type(raw).__name__}")
                for it in raw:
                    if not isinstance(it, dict) or not isinstance(it.get("symbol"), str):
                        continue
                    attrs = it.get("attributes") if isinstance(it.get("attributes"), list) else []
                    out.append({"symbol": it["symbol"].strip().upper()[:20], "name": _clean(it.get("name"), 200),
                                "asset_class": str(it.get("class") or cls), "exchange": _clean(it.get("exchange"), 20),
                                "tradable": bool(it.get("tradable")), "status": _clean(it.get("status"), 20),
                                "attributes": [str(a)[:40] for a in attrs][:20], "source": SOURCE})
            return out
        return self._parse(data_type, rows)

    # --- corporate actions ---------------------------------------------------------------

    def corporate_actions(self, symbols: Iterable[str], start, end,
                          kinds: Iterable[str] = CORPORATE_ACTION_KINDS) -> List[dict]:
        data_type = "corporate_actions"
        equities = [i for i in (instrument(s) for s in symbols) if i.asset_class == "equity"]
        kinds = [k for k in kinds]
        unknown = [k for k in kinds if k not in CORPORATE_ACTION_KINDS]
        if unknown:
            raise ValueError(f"unsupported corporate action kinds: {unknown}")
        if not equities or not kinds:
            return []                                  # crypto has no corporate actions
        sdk = self._load(data_type)
        lo, hi = _day(start), _day(end)
        client = self._client("corporate_actions", data_type)
        raws = []
        for group, (a, b) in corporate_action_chunks([i.alpaca_symbol for i in equities], lo, hi):
            request = sdk.CorporateActionsRequest(symbols=group, types=[sdk.CorporateActionsType(k) for k in kinds],
                                                  start=a, end=b)
            raws.append(self._call(data_type, lambda request=request: client.get_corporate_actions(request)))

        def rows():
            out, seen = [], set()
            for raw in raws:
                if raw is not None and not isinstance(raw, dict):
                    raise TypeError(f"expected a JSON object, got {type(raw).__name__}")
                for key, kind in _CA_KEYS.items():
                    for it in (raw or {}).get(key) or []:
                        ident = it.get("id")
                        if ident is not None:
                            if (key, ident) in seen:
                                continue            # the same action reported by two chunks: once
                            seen.add((key, ident))
                        out.append({"symbol": from_alpaca_symbol(it["symbol"]), "kind": kind,
                                    "ex_date": _day(it["ex_date"]).isoformat(),
                                    "rate": _opt_float(it.get("rate")), "old_rate": _opt_float(it.get("old_rate")),
                                    "new_rate": _opt_float(it.get("new_rate")),
                                    "process_date": str(it["process_date"])[:10] if it.get("process_date") else None,
                                    "payable_date": str(it["payable_date"])[:10] if it.get("payable_date") else None,
                                    "special": bool(it.get("special")) if kind == "cash_dividend" else None,
                                    "source": SOURCE})
            out.sort(key=_action_sort_key)
            return out
        return self._parse(data_type, rows)


def corporate_action_chunks(symbols: List[str], start: date, end: date):
    """[(symbols <= CORPORATE_ACTIONS_MAX_SYMBOLS, (start, end) <= CORPORATE_ACTIONS_MAX_DAYS days)] in a
    fixed order (symbol groups as given, windows oldest first). Disjoint, together exactly
    ``symbols`` x [start, end]. ``start > end`` -> one unchanged request (the source answers it)."""
    groups = [symbols[i:i + CORPORATE_ACTIONS_MAX_SYMBOLS]
              for i in range(0, len(symbols), CORPORATE_ACTIONS_MAX_SYMBOLS)]
    windows = []
    a = start
    while a <= end:
        b = min(end, a + timedelta(days=CORPORATE_ACTIONS_MAX_DAYS - 1))
        windows.append((a, b))
        a = b + timedelta(days=1)
    if not windows:
        windows = [(start, end)]
    return [(g, w) for g in groups for w in windows]


def _action_sort_key(r: dict):
    """Total order (ex_date, symbol, kind, then every value) -> chunked and unchunked
    results sort identically."""
    return (r["ex_date"], r["symbol"], r["kind"],
            tuple((v is None, "" if v is None else str(v)) for v in
                  (r["rate"], r["old_rate"], r["new_rate"], r["process_date"], r["payable_date"], r["special"])))


class AlpacaSipProvider(AlpacaProvider):
    """Alpaca SIP daily history (data type ``equity_bars_history``, source label
    ``alpaca_sip``; user decision 2026-10-06). Same keys, same gate (one Alpaca
    account limit), ``feed="sip"``, request end clamped to now − 15 min. Only
    ``get_bars`` for equities; latest quotes/trades stay IEX (AlpacaProvider)."""
    name = SIP_SOURCE
    data_types = (HISTORY_DATA_TYPE,)
    feed = "sip"

    def __init__(self, **kw):
        super().__init__(**kw)
