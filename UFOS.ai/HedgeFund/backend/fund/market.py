"""Market data and the NYSE calendar for the HedgeFund loop (fund/SPEC.md §5).

Proof of concept, paper trading only — quotes are delayed (~15 min) Yahoo
Finance data via yfinance, not investment advice. Nothing outside this module
imports yfinance, so the feed can be swapped (e.g. Alpaca) later.

- ``PriceFeed``: the interface the engine/agents use.
- ``YFinanceFeed``: yfinance (imported lazily inside the methods), daily
  bars cached in the ``prices`` table; blocking — the engine wraps calls in
  ``asyncio.to_thread``. Never raises: failures return None/[] and are
  logged once per symbol and kind per hour. ``dividends`` distinguishes a
  failed fetch (None -> the broker retries next cycle) from "no dividends"
  ([]).
- ``FakeFeed``: in-memory feed for tests.
- Calendar: ``session_state(now)``, ``trading_day``, ``is_trading_day``,
  ``trading_days_between``, ``session_bounds``, ``next_trading_day``,
  ``previous_trading_day`` — regular session 09:30–16:00 America/New_York,
  NYSE holidays / early closes hardcoded for 2026–2027 (update yearly).
  ``session_state`` is the US session only.
- Per symbol (SPEC §5): ``is_tradable(symbol, now)`` — crypto (``BTC-USD``,
  see ``params.is_crypto``) trades 24/7, equities/ETFs only in the regular
  session; ``utc_days_between`` (crypto min-hold days), perp funding windows
  ``funding_windows_between`` / ``last_funding_window`` (00:00, 08:00, 16:00
  UTC).
"""
from __future__ import annotations

import logging
import math
import time as _time
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

try:
    from typing import Protocol
except ImportError:  # pragma: no cover
    Protocol = object

from zoneinfo import ZoneInfo

from . import params as fund_params
from .types import Bar, Quote

logger = logging.getLogger(__name__)

NY = ZoneInfo("America/New_York")
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)

# NYSE full-day closures (source: NYSE holiday calendar as published for 2026/2027).
NYSE_HOLIDAYS = frozenset(date.fromisoformat(d) for d in (
    # 2026
    "2026-01-01",  # New Year's Day
    "2026-01-19",  # Martin Luther King Jr. Day
    "2026-02-16",  # Washington's Birthday
    "2026-04-03",  # Good Friday
    "2026-05-25",  # Memorial Day
    "2026-06-19",  # Juneteenth
    "2026-07-03",  # Independence Day (observed; Jul 4 is a Saturday)
    "2026-09-07",  # Labor Day
    "2026-11-26",  # Thanksgiving
    "2026-12-25",  # Christmas
    # 2027
    "2027-01-01",  # New Year's Day
    "2027-01-18",  # Martin Luther King Jr. Day
    "2027-02-15",  # Washington's Birthday
    "2027-03-26",  # Good Friday
    "2027-05-31",  # Memorial Day
    "2027-06-18",  # Juneteenth (observed; Jun 19 is a Saturday)
    "2027-07-05",  # Independence Day (observed; Jul 4 is a Sunday)
    "2027-09-06",  # Labor Day
    "2027-11-25",  # Thanksgiving
    "2027-12-24",  # Christmas (observed; Dec 25 is a Saturday)
    # Jan 1 2028 is a Saturday: NYSE does not close on Fri Dec 31 2027.
))

# 13:00 ET early closes. Only dates we are sure of: the day after Thanksgiving
# and Christmas Eve when it is a regular weekday session. No early close on
# Jul 2 2026 (Jul 3 is the observed holiday) nor in July 2027 (Jul 3 is a
# Saturday) — NYSE did not shorten those sessions in comparable years (2015,
# 2020, 2021); re-check against the official calendar when it is published.
NYSE_EARLY_CLOSES = frozenset(date.fromisoformat(d) for d in (
    "2026-11-27",  # day after Thanksgiving
    "2026-12-24",  # Christmas Eve (Thursday)
    "2027-11-26",  # day after Thanksgiving
))
CALENDAR_YEARS = (2026, 2027)
_warned_years = set()


def _to_utc(now: Optional[datetime]) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, str):
        now = datetime.fromisoformat(now.replace("Z", "+00:00"))
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _as_date(value) -> date:
    """date | datetime (-> NY date) | 'YYYY-MM-DD' / ISO datetime string."""
    if isinstance(value, datetime):
        return _to_utc(value).astimezone(NY).date()
    if isinstance(value, date):
        return value
    text = str(value)
    if len(text) > 10:
        return _to_utc(datetime.fromisoformat(text.replace("Z", "+00:00"))).astimezone(NY).date()
    return date.fromisoformat(text)


def ny_now(now: Optional[datetime] = None) -> datetime:
    return _to_utc(now).astimezone(NY)


def ny_date(now: Optional[datetime] = None) -> date:
    """The America/New_York calendar date of ``now`` (UTC default)."""
    return ny_now(now).date()


def is_trading_day(d) -> bool:
    """Mon–Fri and not an NYSE holiday. Outside CALENDAR_YEARS only weekends
    are excluded (warning logged once per year)."""
    d = _as_date(d)
    if d.weekday() >= 5:
        return False
    if d.year not in CALENDAR_YEARS and d.year not in _warned_years:
        _warned_years.add(d.year)
        logger.warning("NYSE holiday calendar has no data for %d — treating every weekday as a trading day "
                       "(update fund/market.py)", d.year)
    return d not in NYSE_HOLIDAYS


def session_bounds(d) -> Optional[Tuple[datetime, datetime]]:
    """(open, close) as UTC datetimes of the regular session on NY date d,
    or None when d is not a trading day."""
    d = _as_date(d)
    if not is_trading_day(d):
        return None
    close = EARLY_CLOSE if d in NYSE_EARLY_CLOSES else REGULAR_CLOSE
    o = datetime.combine(d, REGULAR_OPEN, tzinfo=NY).astimezone(timezone.utc)
    c = datetime.combine(d, close, tzinfo=NY).astimezone(timezone.utc)
    return o, c


def next_trading_day(d) -> date:
    """First trading day strictly after d."""
    d = _as_date(d) + timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def previous_trading_day(d) -> date:
    """Last trading day strictly before d."""
    d = _as_date(d) - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def session_state(now: Optional[datetime] = None) -> dict:
    """{"open": bool, "session": "regular"|"closed", "next_open": iso,
    "next_close": iso|None, "trading_day": "YYYY-MM-DD"}.

    While open: next_close = today's close, next_open = the next session's
    open, trading_day = today. While closed: next_close = None, next_open =
    the next session's open, trading_day = that session's date (the day that
    orders queued now will execute on)."""
    now = _to_utc(now)
    today = now.astimezone(NY).date()
    bounds = session_bounds(today)
    if bounds and bounds[0] <= now < bounds[1]:
        nxt = session_bounds(next_trading_day(today))
        return {"open": True, "session": "regular", "next_open": _iso(nxt[0]), "next_close": _iso(bounds[1]),
                "trading_day": today.isoformat()}
    if bounds and now < bounds[0]:
        day = today
    else:
        day = next_trading_day(today)
    o, _c = session_bounds(day)
    return {"open": False, "session": "closed", "next_open": _iso(o), "next_close": None,
            "trading_day": day.isoformat()}


def is_open(now: Optional[datetime] = None) -> bool:
    return session_state(now)["open"]


def trading_day(now: Optional[datetime] = None) -> str:
    """'YYYY-MM-DD' of the current session, or of the next one while closed."""
    return session_state(now)["trading_day"]


def trading_days_between(a, b) -> int:
    """Number of trading days d with a < d <= b (NY dates; datetimes are
    converted). Opened Monday, asked Tuesday -> 1; same day -> 0; b before
    a -> 0. Used for min_hold_days."""
    a, b = _as_date(a), _as_date(b)
    if b <= a:
        return 0
    n, d = 0, a + timedelta(days=1)
    while d <= b:
        if is_trading_day(d):
            n += 1
        d += timedelta(days=1)
    return n


def is_crypto(symbol) -> bool:
    return fund_params.is_crypto(symbol)


def is_tradable(symbol: str, now: Optional[datetime] = None) -> bool:
    """True when market orders in ``symbol`` can execute now: crypto always
    (24/7), equities/ETFs during the NYSE regular session. (The broker adds
    the 5-minute opening grace for equities on top.)"""
    if fund_params.is_crypto(symbol):
        return True
    return bool(session_state(now)["open"])


def utc_days_between(a, b) -> int:
    """UTC calendar days from a to b (datetimes/ISO strings; dates as is);
    0 when b is not after a. Crypto counterpart of trading_days_between."""
    def _d(v):
        if isinstance(v, datetime):
            return _to_utc(v).date()
        if isinstance(v, date):
            return v
        text = str(v)
        if len(text) > 10:
            return _to_utc(datetime.fromisoformat(text.replace("Z", "+00:00"))).date()
        return date.fromisoformat(text)
    return max(0, (_d(b) - _d(a)).days)


FUNDING_HOURS_UTC = (0, 8, 16)


def last_funding_window(now: Optional[datetime] = None) -> datetime:
    """The latest perp funding timestamp (00:00/08:00/16:00 UTC) <= now."""
    now = _to_utc(now)
    h = max(x for x in FUNDING_HOURS_UTC if x <= now.hour)
    return now.replace(hour=h, minute=0, second=0, microsecond=0)


def funding_windows_between(a, b) -> List[datetime]:
    """Funding timestamps w with a < w <= b (UTC), oldest first."""
    a, b = _to_utc(a), _to_utc(b)
    out: List[datetime] = []
    if b <= a:
        return out
    w = last_funding_window(b)
    while w > a:
        out.append(w)
        w -= timedelta(hours=8)
    return out[::-1]


STALE_QUOTE_MINUTES = 30       # a stale crypto quote older than this never triggers liquidations


def bar_timestamp(bar) -> str:
    """UTC ISO timestamp of a daily bar's date (00:00 UTC) — the conservative
    age of a price taken from the bar cache."""
    return datetime.combine(date.fromisoformat(str(bar.date)[:10]), time(0, 0),
                            tzinfo=timezone.utc).isoformat(timespec="seconds")


def quote_is_stale(quote, now: Optional[datetime] = None, minutes: float = STALE_QUOTE_MINUTES) -> bool:
    """True for a missing quote or a fallback quote (``stale``) whose timestamp
    is older than ``minutes`` (or unknown)."""
    if quote is None:
        return True
    if not getattr(quote, "stale", False):
        return False
    try:
        return _to_utc(quote.ts) < _to_utc(now) - timedelta(minutes=minutes)
    except (TypeError, ValueError):
        return True


def last_close_before(now: Optional[datetime] = None) -> datetime:
    """UTC datetime of the most recent regular-session close at or before now."""
    now = _to_utc(now)
    d = now.astimezone(NY).date()
    for _ in range(15):
        b = session_bounds(d)
        if b and b[1] <= now:
            return b[1]
        d -= timedelta(days=1)
    return now


# --- feeds -------------------------------------------------------------------

class PriceFeed(Protocol):
    """What the engine and agents need from a market data source."""

    def daily_bars(self, symbol: str, days: int = 400) -> List[Bar]:
        """Adjusted daily bars, oldest first (at most ``days``)."""
        ...

    def quote(self, symbol: str) -> Optional[Quote]:
        """Latest (delayed) quote or None."""
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


def parse_news_item(item) -> Optional[dict]:
    """Normalises one yfinance news entry — old shape (title, publisher, link,
    providerPublishTime epoch) and new shape (>= 0.2.5x: {"id", "content":
    {title, pubDate, provider{displayName}, canonicalUrl{url},
    clickThroughUrl{url}}}). Returns None when there is no title."""
    if not isinstance(item, dict):
        return None
    c = item.get("content") if isinstance(item.get("content"), dict) else item
    title = _clean_text(c.get("title"), 300)
    if not title:
        return None
    provider = c.get("provider")
    publisher = provider.get("displayName") if isinstance(provider, dict) else c.get("publisher")
    url = None
    for key in ("canonicalUrl", "clickThroughUrl"):
        v = c.get(key)
        if isinstance(v, dict):
            url = _safe_url(v.get("url"))
            if url:
                break
    url = url or _safe_url(c.get("link")) or _safe_url(item.get("link"))
    published = None
    raw = c.get("pubDate") or c.get("displayTime") or c.get("providerPublishTime") or item.get("providerPublishTime")
    try:
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            published = datetime.fromtimestamp(float(raw), tz=timezone.utc).isoformat(timespec="seconds")
        elif isinstance(raw, str) and raw:
            published = _to_utc(datetime.fromisoformat(raw.replace("Z", "+00:00"))).isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError):
        published = None
    return {"title": title, "publisher": _clean_text(publisher, 100), "published_at": published, "url": url}


class YFinanceFeed:
    """yfinance-backed PriceFeed with the daily-bar cache in the ``prices``
    table (``cache_store`` = fund.store module or None for no cache).

    Refresh policy for bars: at most every ``session_refresh_minutes`` while
    the regular session is open; outside it once after the last close (a
    fetch older than the last close is refreshed). Quotes: fast_info
    last_price (delayed), fallback last cached daily close."""

    SOURCE = "yfinance"
    WARN_INTERVAL_SECONDS = 3600
    # Dividend series are re-downloaded at most every 2 h. Yahoo sometimes
    # lists a dividend only on/after its ex-date; the broker scans a trailing
    # window of ex-dates every cycle (deduplicated), so a late listing is
    # still booked — this TTL only bounds the delay.
    DIVIDEND_CACHE_SECONDS = 2 * 3600

    def __init__(self, cache_store=None, clock: Optional[Callable[[], datetime]] = None,
                 session_refresh_minutes: int = 30):
        self.store = cache_store
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.session_refresh = timedelta(minutes=session_refresh_minutes)
        self._warned: Dict[Tuple[str, str], float] = {}
        self._mem_bars: Dict[str, Tuple[datetime, int, List[Bar]]] = {}   # without a store
        self._div_cache: Dict[str, Tuple[float, list]] = {}

    def _warn(self, symbol: str, what: str, ex) -> None:
        key = (symbol, what)
        t = _time.monotonic()
        if t - self._warned.get(key, -1e12) >= self.WARN_INTERVAL_SECONDS:
            self._warned[key] = t
            logger.warning("yfinance %s for %s failed: %s", what, symbol, str(ex)[:200])

    @staticmethod
    def _ticker(symbol: str):
        import yfinance as yf  # lazy: only this module talks to Yahoo
        return yf.Ticker(symbol)

    def _needs_refresh(self, fetched_at: Optional[datetime], fetched_days: int, days: int,
                       symbol: Optional[str] = None) -> bool:
        if fetched_at is None or fetched_days < days:
            return True
        now = _to_utc(self.clock())
        if (symbol is not None and fund_params.is_crypto(symbol)) or session_state(now)["open"]:
            # crypto trades 24/7: its daily bar keeps moving, refresh on the session cadence
            return now - fetched_at >= self.session_refresh
        return fetched_at < last_close_before(now)

    def _download(self, symbol: str, days: int) -> Optional[List[Bar]]:
        try:
            start = (ny_date(self.clock()) - timedelta(days=int(days * 1.5) + 10)).isoformat()
            df = self._ticker(symbol).history(start=start, interval="1d", auto_adjust=True, actions=False)
            bars: List[Bar] = []
            if df is None or len(df) == 0:
                raise ValueError("no data")
            for idx, row in df.iterrows():
                close = _finite_pos(row.get("Close"))
                if close is None:
                    continue
                o = _finite_pos(row.get("Open")) or close
                h = _finite_pos(row.get("High")) or max(o, close)
                lo = _finite_pos(row.get("Low")) or min(o, close)
                vol = row.get("Volume")
                try:
                    vol = float(vol) if vol is not None and math.isfinite(float(vol)) else 0.0
                except (TypeError, ValueError):
                    vol = 0.0
                d = idx.date().isoformat() if hasattr(idx, "date") else str(idx)[:10]
                bars.append(Bar(date=d, open=o, high=h, low=lo, close=close, volume=vol))
            return bars[-days:] if bars else None
        except Exception as ex:
            self._warn(symbol, "daily bars", ex)
            return None

    def daily_bars(self, symbol: str, days: int = 400) -> List[Bar]:
        days = max(1, int(days))
        now = _to_utc(self.clock())
        if self.store is not None and self.store.db.is_enabled():
            meta = self.store.bars_fetched(symbol) or {}
            try:
                fetched_at = _to_utc(datetime.fromisoformat(meta["at"])) if meta.get("at") else None
            except ValueError:
                fetched_at = None
            if self._needs_refresh(fetched_at, int(meta.get("days") or 0), days, symbol):
                bars = self._download(symbol, days)
                if bars:
                    self.store.save_bars(symbol, bars, source=self.SOURCE)
                    self.store.set_bars_fetched(symbol, now, days)
            return self.store.load_bars(symbol, limit=days, source=self.SOURCE)
        cached = self._mem_bars.get(symbol)
        if cached is None or self._needs_refresh(cached[0], cached[1], days, symbol):
            bars = self._download(symbol, days)
            if bars:
                self._mem_bars[symbol] = (now, days, bars)
                cached = self._mem_bars[symbol]
        return list(cached[2][-days:]) if cached else []

    def _cached_bars(self, symbol: str, n: int) -> List[Bar]:
        if self.store is not None and self.store.db.is_enabled():
            return self.store.load_bars(symbol, limit=n, source=self.SOURCE)
        cached = self._mem_bars.get(symbol)
        return list(cached[2][-n:]) if cached else []

    def quote(self, symbol: str) -> Optional[Quote]:
        now_iso = _iso(_to_utc(self.clock()))
        price = prev = None
        try:
            fi = self._ticker(symbol).fast_info
            for key in ("last_price", "lastPrice"):
                try:
                    price = _finite_pos(fi[key])
                except Exception:
                    price = _finite_pos(getattr(fi, key, None))
                if price:
                    break
            for key in ("previous_close", "previousClose", "regular_market_previous_close"):
                try:
                    prev = _finite_pos(fi[key])
                except Exception:
                    prev = _finite_pos(getattr(fi, key, None))
                if prev:
                    break
        except Exception as ex:
            self._warn(symbol, "quote", ex)
        if price is None:
            bars = self._cached_bars(symbol, 2)
            if not bars:
                return None
            price = bars[-1].close
            prev = prev or (bars[-2].close if len(bars) > 1 else None)
            # Fallback: the cached daily close is NOT a live price — keep the bar's own
            # timestamp and mark it stale (the engine never liquidates on it, SPEC §5/§10).
            return Quote(symbol=symbol, price=price, prev_close=prev, ts=bar_timestamp(bars[-1]),
                         delayed=True, stale=True)
        # Yahoo's crypto quotes are near real-time (not exchange-grade); equities ~15 min delayed.
        return Quote(symbol=symbol, price=price, prev_close=prev, ts=now_iso,
                     delayed=not fund_params.is_crypto(symbol))

    def dividends(self, symbol: str, since: date) -> Optional[List[Tuple[date, float]]]:
        """(ex_date, amount) with ex_date > since; [] when there are none,
        None when the download failed (not cached, so the next call retries)."""
        since = _as_date(since)
        t = _time.monotonic()
        cached = self._div_cache.get(symbol)
        if cached is None or t - cached[0] > self.DIVIDEND_CACHE_SECONDS:
            try:
                series = self._ticker(symbol).dividends
                items = []
                if series is not None:
                    for idx, amount in series.items():
                        amt = _finite_pos(amount)
                        if amt is None:
                            continue
                        d = idx.date() if hasattr(idx, "date") else date.fromisoformat(str(idx)[:10])
                        items.append((d, amt))
                cached = (t, sorted(items))
                self._div_cache[symbol] = cached
            except Exception as ex:
                self._warn(symbol, "dividends", ex)
                return None
        return [(d, a) for d, a in cached[1] if d > since]

    def news(self, symbol: str, limit: int = 8) -> List[dict]:
        try:
            raw = self._ticker(symbol).news or []
            if isinstance(raw, dict):  # some versions wrap the list
                raw = raw.get("news") or raw.get("items") or []
            items = [x for x in (parse_news_item(i) for i in list(raw)) if x]
        except Exception as ex:
            self._warn(symbol, "news", ex)
            return []
        items.sort(key=lambda x: x["published_at"] or "", reverse=True)
        return items[:max(0, int(limit))]


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
