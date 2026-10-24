"""Market data and the NYSE calendar for the HedgeFund loop (fund/SPEC.md §5).

Proof of concept, paper trading only — not investment advice.

Market data comes from Alpaca through the Strategy Platform data layer
(``strategy_platform/data``; user decision 2026-10-02: the old engine is
switched to Alpaca now, yfinance is removed from the HedgeFund module).

- ``PriceFeed``: the interface the engine/agents use.
- ``AlpacaFeed``: PriceFeed on the data layer — daily bars from the bar
  cache in hedgefund.db (IEX, adjusted, only completed bars, freshness
  required: the bar of the last completed session / day must be there),
  quotes from the latest Alpaca trade, dividends from Alpaca corporate
  actions, news from the Alpaca news API. Blocking — the engine wraps calls
  in ``asyncio.to_thread``. A failure RAISES a ``UserFacingError`` with the
  reason (the engine records it per symbol and the run becomes ``partial``);
  there is no stand-in price: the former "last cached close stamped as a
  quote" fallback (never accepted by the user) is removed. ``dividends``
  keeps its contract: ``None`` = the fetch failed (unknown, the broker
  retries next cycle; reason logged), ``[]`` = no dividends.
- ``FakeFeed``: in-memory feed for tests.
- Calendar: re-exported from ``strategy_platform/core/calendars.py`` (single
  source of truth): ``session_state(now)``, ``trading_day``,
  ``is_trading_day``, ``trading_days_between``, ``session_bounds``,
  ``next_trading_day``, ``previous_trading_day``, ``last_close_before``,
  ``is_tradable``, ``utc_days_between``, perp funding windows
  ``funding_windows_between`` / ``last_funding_window`` (00:00, 08:00, 16:00
  UTC). Years outside the calendar raise ``CalendarUnavailable`` (no guess).
"""
from __future__ import annotations

import logging
import math
import time as _time
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

try:
    from typing import Protocol
except ImportError:  # pragma: no cover
    Protocol = object

from strategy_platform.core.calendars import (  # noqa: F401 — re-exported (single source of truth)
    CALENDAR_YEARS, EARLY_CLOSE, FUNDING_HOURS_UTC, NY, NYSE_EARLY_CLOSES, NYSE_HOLIDAYS, REGULAR_CLOSE,
    REGULAR_OPEN, CalendarUnavailable, _as_date, _iso, _to_utc, funding_windows_between, is_covered,
    is_crypto, is_early_close, is_open, is_tradable, is_trading_day, last_close_before, last_funding_window,
    next_trading_day, ny_date, ny_now, previous_trading_day, session_bounds, session_state, trading_day,
    trading_days, trading_days_between, utc_days_between)
from strategy_platform.core.errors import DataSourceUnavailable, UserFacingError

from strategy_platform.core.types import Bar, Quote

logger = logging.getLogger(__name__)


STALE_QUOTE_MINUTES = 30       # a stale crypto quote older than this never triggers liquidations


def quote_is_stale(quote, now: Optional[datetime] = None, minutes: float = STALE_QUOTE_MINUTES) -> bool:
    """True for a missing quote or a quote flagged ``stale`` whose timestamp
    is older than ``minutes`` (or unknown). AlpacaFeed never returns a stale
    quote (it raises instead); the flag stays for fakes and the broker/engine
    guards."""
    if quote is None:
        return True
    if not getattr(quote, "stale", False):
        return False
    try:
        return _to_utc(quote.ts) < _to_utc(now) - timedelta(minutes=minutes)
    except (TypeError, ValueError):
        return True


# --- feeds -------------------------------------------------------------------

class PriceFeed(Protocol):
    """What the engine and agents need from a market data source."""

    def daily_bars(self, symbol: str, days: int = 400) -> List[Bar]:
        """Adjusted daily bars, oldest first (at most ``days``)."""
        ...

    def quote(self, symbol: str) -> Optional[Quote]:
        """Latest quote; AlpacaFeed raises a UserFacingError instead of returning None."""
        ...

    def dividends(self, symbol: str, since: date) -> Optional[List[Tuple[date, float]]]:
        """(ex_date, amount_per_share) with ex_date > since, oldest first;
        [] = no dividends, None = the fetch failed (unknown, retry later)."""
        ...

    def news(self, symbol: str, limit: int = 8) -> List[dict]:
        """[{title, publisher, published_at (UTC ISO|None), url}] newest first."""
        ...


def _finite_pos(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def _safe_url(url) -> Optional[str]:
    if isinstance(url, str) and url.strip().lower().startswith(("https://", "http://")):
        return url.strip()[:1000]
    return None


def _clean_text(text, limit: int) -> Optional[str]:
    if not isinstance(text, str):
        return None
    t = " ".join(text.split())
    return t[:limit] if t else None


class AlpacaFeed:
    """PriceFeed on the Strategy Platform data layer (Alpaca, SPEC §3/§5).

    - ``daily_bars(symbol, days)``: completed daily bars (IEX, adjusted
      "all") from ``data.cache`` with freshness required — equities through
      the last session close (``last_close_before(now)``), crypto through now
      (the newest completed UTC-day bar). Equity history starts 2016
      (``HISTORY_START``): a longer look-back returns the bars since then.
      Bar.date: New York date (equities), UTC date (crypto). Today's
      unfinished bar is not included (the quote carries the live price).
    - ``quote(symbol)``: the latest Alpaca trade (IEX for equities, real
      time -> ``delayed=False``) with its own timestamp; ``prev_close`` =
      close of the last daily bar before the trade's session (equities) /
      of the newest completed daily bar (crypto). Refused (raises) when the
      trade is too old to be a current price: crypto older than
      ``STALE_QUOTE_MINUTES``; equities during the session older than
      ``EQUITY_MAX_TRADE_AGE_MINUTES``, outside it older than the last
      session's open.
    - ``dividends``: Alpaca cash dividends (ex_date > since), cached 2 h;
      failures -> None (not cached, logged), crypto -> [].
    - ``news``: Alpaca news (newest first) in the shape
      {title, publisher, published_at, url}.
    Errors raise ``UserFacingError`` subclasses with the reason."""

    SOURCE = "alpaca"
    HISTORY_START = date(2016, 1, 1)
    DIVIDEND_CACHE_SECONDS = 2 * 3600
    EQUITY_MAX_TRADE_AGE_MINUTES = 30
    WARN_INTERVAL_SECONDS = 3600

    def __init__(self, cache=None, fetch: Optional[Callable] = None,
                 clock: Optional[Callable[[], datetime]] = None):
        self._cache = cache
        self._fetch_fn = fetch
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._div_cache: Dict[str, Tuple[float, date, list]] = {}
        self._warned: Dict[Tuple[str, str], float] = {}
        self.findings: Dict[str, List[dict]] = {}       # symbol -> validation findings of the last bar read

    # --- dependencies -------------------------------------------------------------------

    def _bars_cache(self):
        if self._cache is not None:
            return self._cache
        from strategy_platform.data import cache
        return cache.default_cache()

    def _fetch(self, data_type: str, op: str, *args):
        if self._fetch_fn is not None:
            return self._fetch_fn(data_type, op, *args)
        from strategy_platform.data import registry
        return registry.fetch(data_type, op, *args)

    def _warn(self, symbol: str, what: str, reason: str) -> None:
        key = (symbol, what)
        t = _time.monotonic()
        if t - self._warned.get(key, -1e12) >= self.WARN_INTERVAL_SECONDS:
            self._warned[key] = t
            logger.warning("Alpaca %s for %s unavailable: %s", what, symbol, str(reason)[:300])

    # --- bars ----------------------------------------------------------------------------

    def _rows(self, symbol: str, start: datetime, fresh: datetime) -> List[dict]:
        now = _to_utc(self.clock())
        if not is_crypto(symbol):
            floor = datetime.combine(self.HISTORY_START, datetime.min.time(), tzinfo=timezone.utc)
            start = max(start, floor)
        return self._bars_cache().get_bars(symbol, "1Day", start, now, require_fresh_through=fresh)

    @classmethod
    def _malformed(cls, symbol: str, crypto: bool, day: str, what: str) -> DataSourceUnavailable:
        return DataSourceUnavailable(cls.SOURCE, "crypto_bars" if crypto else "equity_bars",
                                     f"Alpaca returned a malformed bar for {symbol} on {day} ({what}) — "
                                     f"{symbol} is not analysed until the data is valid")

    @classmethod
    def _to_bar(cls, row: dict, crypto: bool, symbol: str = "") -> Bar:
        """A strict Bar: open/high/low/close must be finite and > 0, else the
        fetch fails for the symbol (no repaired values — no unrequested
        fallbacks). A missing volume stays None (nothing downstream uses it)."""
        ts = _to_utc(row["ts"])
        d = ts.date() if crypto else ts.astimezone(NY).date()
        sym = symbol or str(row.get("symbol") or "?")
        prices = {k: _finite_pos(row.get(k)) for k in ("open", "high", "low", "close")}
        bad = [k for k, v in prices.items() if v is None]
        if bad:
            raise cls._malformed(sym, crypto, d.isoformat(), "missing or non-positive " + "/".join(bad))
        vol = row.get("volume")
        if vol is not None:
            try:
                vol = float(vol)
            except (TypeError, ValueError):
                raise cls._malformed(sym, crypto, d.isoformat(), "volume is not a number")
            if not math.isfinite(vol) or vol < 0:
                raise cls._malformed(sym, crypto, d.isoformat(), "invalid volume")
        return Bar(date=d.isoformat(), open=prices["open"], high=prices["high"], low=prices["low"],
                   close=prices["close"], volume=vol)

    def _bars(self, symbol: str, start: datetime, fresh: datetime) -> List[Bar]:
        crypto = is_crypto(symbol)
        rows = self._rows(symbol, start, fresh)
        bars = [self._to_bar(r, crypto, symbol) for r in rows]
        # validation findings (strategy_platform/data/validation.py): errors make the fetch
        # fail for this symbol; warnings are kept for inspection and logged (rate-limited)
        from strategy_platform.data.validation import validate_bars
        findings = validate_bars(rows, symbol=symbol, timeframe="1Day")
        self.findings[symbol] = findings
        errors = [f for f in findings if f.get("severity") == "error"]
        if errors:
            first = errors[0]
            day = str(first.get("ts") or "?")[:10]
            raise self._malformed(symbol, crypto, day, first.get("message") or first.get("code"))
        for f in findings:
            if f.get("severity") == "warning":
                self._warn(symbol, f"bar check {f.get('code')}", f.get("message") or "")
        return bars

    def _fresh_through(self, symbol: str, now: datetime) -> datetime:
        return now if is_crypto(symbol) else last_close_before(now)

    def daily_bars(self, symbol: str, days: int = 400) -> List[Bar]:
        days = max(1, int(days))
        now = _to_utc(self.clock())
        fresh = self._fresh_through(symbol, now)
        span = days + 2 if is_crypto(symbol) else int(days * 1.46) + 10     # calendar days for `days` sessions
        return self._bars(symbol, fresh - timedelta(days=span), fresh)[-days:]

    # --- quote ---------------------------------------------------------------------------

    def quote(self, symbol: str) -> Quote:
        now = _to_utc(self.clock())
        crypto = is_crypto(symbol)
        data_type = "crypto_quotes" if crypto else "equity_quotes"
        trade = self._fetch(data_type, "latest_trade", symbol)
        price = _finite_pos((trade or {}).get("price"))
        if price is None:
            raise DataSourceUnavailable(self.SOURCE, data_type, f"the latest {symbol} trade has no valid price")
        ts = _to_utc(trade["ts"])
        age_min = (now - ts).total_seconds() / 60.0
        if crypto:
            if age_min > STALE_QUOTE_MINUTES:
                raise DataSourceUnavailable(self.SOURCE, data_type,
                                            f"the latest {symbol} trade is {age_min:.0f} min old — no current price")
            bars = self._bars(symbol, now - timedelta(days=5), now)
            prev = bars[-1].close if bars else None
        else:
            if session_state(now)["open"]:
                if age_min > self.EQUITY_MAX_TRADE_AGE_MINUTES:
                    raise DataSourceUnavailable(
                        self.SOURCE, data_type,
                        f"no IEX trade in {symbol} for {age_min:.0f} min during the session — no current price")
            else:
                last_open = session_bounds(_as_date(last_close_before(now)))[0]
                if ts < last_open:
                    raise DataSourceUnavailable(self.SOURCE, data_type,
                                                f"no IEX trade in {symbol} since the last session's open")
            trade_day = ts.astimezone(NY).date()
            prev_session = previous_trading_day(trade_day)
            prev_close_at = session_bounds(prev_session)[1]
            bars = self._bars(symbol, prev_close_at - timedelta(days=15), prev_close_at)
            before = [b for b in bars if b.date < trade_day.isoformat()]
            # the bar of the previous session ITSELF (also after an early close) — never an older one
            if not before or before[-1].date != prev_session.isoformat():
                raise DataSourceUnavailable(self.SOURCE, "equity_bars",
                                            f"no daily bar for {symbol} of the previous session "
                                            f"{prev_session.isoformat()} yet — previous close unknown")
            prev = before[-1].close
        return Quote(symbol=symbol, price=price, prev_close=prev, ts=_iso(ts), delayed=False, stale=False)

    # --- dividends / news ----------------------------------------------------------------

    def dividends(self, symbol: str, since: date) -> Optional[List[Tuple[date, float]]]:
        """(ex_date, amount) with ex_date > since; [] when there are none,
        None when the fetch failed (not cached, so the next call retries)."""
        since = _as_date(since)
        if is_crypto(symbol):
            return []
        t = _time.monotonic()
        cached = self._div_cache.get(symbol)
        if cached is None or t - cached[0] > self.DIVIDEND_CACHE_SECONDS or since < cached[1]:
            today = ny_date(self.clock())
            try:
                rows = self._fetch("corporate_actions", "corporate_actions", [symbol], since + timedelta(days=1),
                                   today + timedelta(days=1), ("cash_dividend",))
                items = []
                for r in rows or []:
                    amt = _finite_pos(r.get("rate"))
                    if r.get("kind") != "cash_dividend" or str(r.get("symbol")).upper() != symbol.upper() \
                            or amt is None:
                        continue
                    items.append((date.fromisoformat(str(r["ex_date"])[:10]), amt))
            except UserFacingError as ex:
                self._warn(symbol, "dividends", ex.user_message)
                return None
            except Exception as ex:  # noqa: BLE001 — unknown, retried next cycle
                self._warn(symbol, "dividends", f"{type(ex).__name__}: {ex}")
                return None
            cached = (t, since, sorted(items))
            self._div_cache[symbol] = cached
        return [(d, a) for d, a in cached[2] if d > since]

    def news(self, symbol: str, limit: int = 8) -> List[dict]:
        limit = max(0, int(limit))
        if limit == 0:
            return []
        items = self._fetch("news", "news", [symbol], None, None, limit) or []
        out = []
        for it in items:
            title = _clean_text(it.get("headline"), 300)
            if not title:
                continue
            out.append({"title": title, "publisher": _clean_text(it.get("publisher"), 100),
                        "published_at": it.get("created_at"), "url": _safe_url(it.get("url"))})
        out.sort(key=lambda x: x["published_at"] or "", reverse=True)
        return out[:limit]


class FakeFeed:
    """In-memory PriceFeed for tests: set ``bars[sym]``, ``quotes[sym]``
    (Quote or float), ``divs[sym]`` [(date, amount)], ``headlines[sym]``;
    symbols in ``div_errors`` simulate a failed dividend download (None)."""

    def __init__(self, bars=None, quotes=None, divs=None, headlines=None):
        self.bars: Dict[str, List[Bar]] = dict(bars or {})
        self.quotes: Dict[str, object] = dict(quotes or {})
        self.divs: Dict[str, list] = dict(divs or {})
        self.headlines: Dict[str, List[dict]] = dict(headlines or {})
        self.div_errors: set = set()
        self.calls: List[tuple] = []

    def daily_bars(self, symbol: str, days: int = 400) -> List[Bar]:
        self.calls.append(("daily_bars", symbol))
        return list(self.bars.get(symbol, []))[-days:]

    def quote(self, symbol: str) -> Optional[Quote]:
        self.calls.append(("quote", symbol))
        q = self.quotes.get(symbol)
        if q is None:
            return None
        if isinstance(q, Quote):
            return q
        return Quote(symbol=symbol, price=float(q), prev_close=None, delayed=True)

    def dividends(self, symbol: str, since: date) -> Optional[List[Tuple[date, float]]]:
        self.calls.append(("dividends", symbol))
        if symbol in self.div_errors:
            return None
        since = _as_date(since)
        return [(_as_date(d), float(a)) for d, a in self.divs.get(symbol, []) if _as_date(d) > since]

    def news(self, symbol: str, limit: int = 8) -> List[dict]:
        return list(self.headlines.get(symbol, []))[:limit]
