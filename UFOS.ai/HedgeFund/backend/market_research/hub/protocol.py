"""Alpaca real-time market-data stream protocol (JSON) — SPEC §7.6.

Official protocol (Alpaca docs "Real-time Stock / Crypto / News data"):

- the server greets with ``[{"T": "success", "msg": "connected"}]``;
- the client authenticates with ``{"action": "auth", "key": …, "secret": …}``
  → ``[{"T": "success", "msg": "authenticated"}]`` or an error;
- ``{"action": "subscribe" | "unsubscribe", "trades": [...], "bars": [...]}``
  (news: ``"news": [...]``) → ``[{"T": "subscription", "trades": [...], …}]``
  listing EVERYTHING subscribed afterwards;
- data frames are JSON arrays of messages: ``T`` = ``t`` trade, ``b`` minute
  bar, ``n`` news (``q``/``d``/``u``/… are ignored here);
- errors ``{"T": "error", "code": n, "msg": …}``: 400 invalid syntax, 401 not
  authenticated, 402 auth failed, 403 already authenticated, 404 auth timeout,
  405 symbol limit exceeded, 406 connection limit exceeded, 407 slow client,
  409 insufficient subscription, 410 invalid subscribe action for this feed,
  500 internal error.

Keys never appear in logs or exceptions (``auth_message`` is the only place
that sees them). Symbols: platform symbols outside (``BTC-USD``), Alpaca
symbols on the wire (``BTC/USD``; news tags crypto as ``BTCUSD``).

Part of UFOS.ai, a proof-of-concept portfolio project; research only.
"""
from __future__ import annotations

import json
import math
from typing import List, Optional

from strategy_platform.core.instruments import from_alpaca_symbol, instrument
from strategy_platform.data.providers.alpaca import AUTH_MESSAGE, _clean, _safe_url, news_symbol
from strategy_platform.data.providers.base import parse_ts

from .. import limits

ENDPOINT_IDS = ("stocks", "crypto", "news")
ENDPOINTS = {
    "stocks": "wss://stream.data.alpaca.markets/v2/iex",
    "crypto": "wss://stream.data.alpaca.markets/v1beta3/crypto/us",
    "news": "wss://stream.data.alpaca.markets/v1beta1/news",
}

CONNECTION_LIMIT_MESSAGE = ("Live data unavailable: Alpaca allows one stream connection per account and another "
                            "program is using it.")
PLAN_MESSAGE = "Live data unavailable: your Alpaca market-data plan does not include this stream."
SYMBOL_LIMIT_REASON = "Stream limit: {limit} symbols — {symbol} is not live."
ALPACA_SYMBOL_LIMIT_REASON = "Stream limit: Alpaca refused more symbols — {symbol} is not live."
UNCONFIRMED_REASON = "Alpaca did not confirm the subscription — {symbol} is not live."

# error codes
INVALID_SYNTAX, NOT_AUTHENTICATED, AUTH_FAILED, ALREADY_AUTHENTICATED, AUTH_TIMEOUT = 400, 401, 402, 403, 404
SYMBOL_LIMIT, CONNECTION_LIMIT, SLOW_CLIENT, INSUFFICIENT_SUBSCRIPTION, INVALID_FEED_ACTION = 405, 406, 407, 409, 410


class StreamLost(Exception):
    """The connection failed or dropped (network, timeout, server closed, 404/407/500): reconnect with backoff."""


class StreamRefused(Exception):
    """Alpaca refused the connection for a reason a reconnect does not fix: feed ``unavailable``."""

    def __init__(self, message: str, kind: str = "refused"):
        super().__init__(message)
        self.user_message = message
        self.kind = kind            # "refused" (406 / keys rejected / plan: re-check 300 s) | "keys" (missing: 60 s)


def classify(code, msg="") -> str:
    """'refused' | 'symbol_limit' | 'ignore' | 'lost' for an Alpaca error code."""
    if code in (NOT_AUTHENTICATED, AUTH_FAILED, CONNECTION_LIMIT, INSUFFICIENT_SUBSCRIPTION, INVALID_FEED_ACTION):
        return "refused"
    if code == SYMBOL_LIMIT:
        return "symbol_limit"
    if code in (INVALID_SYNTAX, ALREADY_AUTHENTICATED):
        return "ignore"           # our own protocol slip: logged, the connection stays up
    return "lost"


def refused_error(code) -> StreamRefused:
    if code == CONNECTION_LIMIT:
        return StreamRefused(CONNECTION_LIMIT_MESSAGE)
    if code in (INSUFFICIENT_SUBSCRIPTION, INVALID_FEED_ACTION):
        return StreamRefused(PLAN_MESSAGE)
    return StreamRefused(AUTH_MESSAGE)


def auth_message(key_id: str, secret_key: str) -> str:
    return json.dumps({"action": "auth", "key": key_id, "secret": secret_key})


def subscribe_message(endpoint: str, action: str, wire_symbols: List[str]) -> str:
    if action not in ("subscribe", "unsubscribe"):
        raise ValueError("action")
    syms = sorted(wire_symbols)
    if endpoint == "news":
        return json.dumps({"action": action, "news": syms})
    return json.dumps({"action": action, "trades": syms, "bars": syms})


def wire_symbol(endpoint: str, symbol: str) -> str:
    """Platform symbol -> the symbol on this endpoint's wire."""
    if endpoint == "news":
        return news_symbol(symbol)
    return instrument(symbol).alpaca_symbol


def parse_frame(text) -> List[dict]:
    """A frame -> list of message dicts. ValueError for anything that is not JSON objects."""
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8")
    if not isinstance(text, str) or len(text) > limits.STREAM_MAX_FRAME_BYTES:
        raise ValueError("frame type/size")
    data = json.loads(text)
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or not all(isinstance(m, dict) for m in data):
        raise ValueError("frame is not a list of objects")
    return data


def _num(v) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def parse_trade(m: dict, endpoint: str) -> Optional[dict]:
    """{"symbol", "price", "size", "ts" (aware UTC datetime)} or None when malformed."""
    try:
        sym = from_alpaca_symbol(str(m.get("S") or ""))
        instrument(sym)
        price, size = _num(m.get("p")), _num(m.get("s"))
        ts = parse_ts(m.get("t"))
    except (ValueError, TypeError):
        return None
    if price is None or price <= 0 or size is None or size < 0:
        return None
    return {"symbol": sym, "price": price, "size": size, "ts": ts}


def parse_bar(m: dict) -> Optional[dict]:
    """A stream minute bar -> {"symbol", "t", "o", "h", "l", "c", "v", "vwap", "n"} or None."""
    try:
        sym = from_alpaca_symbol(str(m.get("S") or ""))
        instrument(sym)
        t = parse_ts(m.get("t"))
    except (ValueError, TypeError):
        return None
    o, h, lo, c = (_num(m.get(k)) for k in ("o", "h", "l", "c"))
    if None in (o, h, lo, c):
        return None
    n = m.get("n")
    return {"symbol": sym, "t": t.replace(second=0, microsecond=0), "o": o, "h": h, "l": lo, "c": c,
            "v": _num(m.get("v")) or 0.0, "vwap": _num(m.get("vw")),
            "n": int(n) if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else 0}


def parse_news(m: dict) -> Optional[dict]:
    """A stream news message -> the internal MR6 item (``news_text.sanitize_item``: plain text, capped,
    http(s) links, https image URLs; untrusted). None when unusable (no id / headline / created_at)."""
    from ..news_text import sanitize_item
    return sanitize_item(m)
