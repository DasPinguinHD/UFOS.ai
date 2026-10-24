"""Provider contract of the data layer (strategy_platform/SPEC.md §3).

A provider is THE configured source of one or more data types (no fallback
chains, D8). Providers return plain dicts (stdlib only) so callers, the cache
and tests never depend on a vendor SDK:

- bar:   {symbol, timeframe, ts, open, high, low, close, volume, vwap,
          trade_count, source, known_at, fetched_at}  (ts = bar START, UTC ISO;
          known_at = point-in-time availability, see data/pit.py)
- quote: {symbol, bid, ask, bid_size, ask_size, ts, source}
- trade: {symbol, price, size, ts, source}
- news:  {id, headline, summary, publisher, url, created_at, updated_at, symbols, source}
- corporate action: {symbol, kind, ex_date, rate, old_rate, new_rate,
          process_date, payable_date, source}  (kind in CORPORATE_ACTION_KINDS;
          rate = cash per share for dividends)

Failures raise ``core.errors.UserFacingError`` subclasses (mostly
``DataSourceUnavailable``) — never a stand-in value.

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional

from ...core.errors import DataSourceUnavailable, UserFacingError

# Data types and the sources that may serve them (SPEC §3). Stage 1: exactly one
# possible source per type, so the configured source is never an "alternative".
DATA_TYPES = ("equity_bars", "crypto_bars", "equity_quotes", "crypto_quotes", "news", "corporate_actions",
              "macro", "insider", "equity_bars_pre2016", "equity_volume")
TIMEFRAMES = {"1Day": timedelta(days=1), "1Hour": timedelta(hours=1), "1Min": timedelta(minutes=1)}
CORPORATE_ACTION_KINDS = ("cash_dividend", "forward_split", "reverse_split")


class SourceAuthRejected(DataSourceUnavailable):
    """The source refused the credentials (HTTP 401/403). Never retried."""

    def __init__(self, source: str, data_type: str, user_message: str, *, detail: str = ""):
        super().__init__(source, data_type, "the keys were rejected")
        self.user_message = user_message
        self.detail = detail
        self.args = (user_message,)


class HistoryNotAvailable(UserFacingError):
    """A request reaches before the history the configured source offers."""

    def __init__(self, source: str, earliest: str):
        super().__init__(f"History before {earliest[:4]} is not available from the configured source ({source}).")
        self.source = source
        self.earliest = earliest


_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})(\.\d+)?(.*)$")


def parse_ts(value) -> datetime:
    """RFC 3339 / ISO-8601 (``Z`` allowed, up to nanoseconds as Alpaca sends
    them) or a datetime -> aware UTC datetime. Raises ValueError otherwise."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00").replace("z", "+00:00")
        m = _TS_RE.match(text)
        if m and m.group(2):                       # Python 3.10 accepts only 3 or 6 fraction digits
            text = m.group(1) + "." + (m.group(2)[1:] + "000000")[:6] + m.group(3)
        dt = datetime.fromisoformat(text)
    else:
        raise ValueError(f"not a timestamp: {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt) -> str:
    """UTC ISO-8601 with seconds (the platform's time format)."""
    return parse_ts(dt).isoformat(timespec="seconds")


def iso_precise(dt) -> str:
    """UTC ISO-8601 keeping microseconds (quotes/trades)."""
    return parse_ts(dt).isoformat()


def bars_data_type(asset_class: str) -> str:
    return "crypto_bars" if asset_class == "crypto" else "equity_bars"


def quotes_data_type(asset_class: str) -> str:
    return "crypto_quotes" if asset_class == "crypto" else "equity_quotes"


class Provider:
    """Interface of a data provider. Every method may raise UserFacingError."""

    name = "base"
    data_types: tuple = ()

    def get_bars(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> List[dict]:
        """Bars with start <= ts <= end, oldest first (may include an
        unfinished latest bar — the cache drops bars with known_at > now)."""
        raise NotImplementedError

    def latest_quote(self, symbol: str) -> dict:
        raise NotImplementedError

    def latest_trade(self, symbol: str) -> dict:
        raise NotImplementedError

    def news(self, symbols: Iterable[str], start: Optional[datetime] = None, end: Optional[datetime] = None,
             limit: int = 10) -> List[dict]:
        """Newest first."""
        raise NotImplementedError

    def corporate_actions(self, symbols: Iterable[str], start, end,
                          kinds: Iterable[str] = CORPORATE_ACTION_KINDS) -> List[dict]:
        raise NotImplementedError
