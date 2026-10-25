"""Return engine of the Disclosures section (INSIDER_RET_API.md §2, accepted 2026-10-24; reused by CONGRESS_API.md §3).

For one disclosed trade it measures, from two start points, the price move over 1 / 5 / 20 / 60 trading sessions
against the issuer's sector ETF:

- start "filing" (tradable): insiders — the first 1-minute IEX bar that starts strictly after the EDGAR acceptance
  minute during the regular session ("minute"); no trade before the close -> next session's open
  ("next_open_no_trade"); accepted outside the session -> next session's open ("next_open"). Congress (only a filing
  DATE is known, decision 2026-10-24) -> the open of the first session after the filing date
  ("next_open_after_filing_date"). No acceptance time for an insider trade -> not measurable ("acceptance time
  unknown"); never a date-only substitute.
- start "trade_date" (not tradable, the insider's / member's own timing): the close of the trade date ("close"), or
  of the previous trading day when it was not one ("prev_close_non_trading_day").
- exit for horizon h: the close of the h-th regular session after the entry session E. Until that close (+ a short
  settle time for the final bar) the horizon is "running" with ``sessions_done``.
- the ETF is measured over exactly the same window (same entry minute or open/close, same exit close).
  r = exit / entry - 1; excess = r_stock - r_etf; score = excess for purchases (P), -excess for sales (S).
  No ETF (unmapped SIC) -> r_stock only, excess/score None with the reason.

Prices come through a ``PriceAccess`` object (daily adjusted bars + minute bars); ``AlpacaPrices`` is the real one
(shared bar cache + HTTP gate, adjustment "all" = splits and dividends). The engine itself does no I/O and makes no
AI call. A price-source failure raises ``PriceUnavailable`` — the caller keeps the previous result and retries later.

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from statistics import mean, median
from typing import Dict, Iterable, List, Optional, Protocol, Tuple

from strategy_platform.core import calendars

HORIZONS = (1, 5, 20, 60)
STARTS = ("filing", "trade_date")
SETTLE = timedelta(minutes=20)          # a session's daily bar is final this long after the close
MINUTE_LOOKAHEAD_ETF = timedelta(minutes=30)
SMALL_SAMPLE = 30
TRACK_RECORD_CAVEAT = ("Descriptive statistics, not a validated signal — small samples are noise. "
                       "Sector benchmark: approximation (SIC → GICS-based sector ETFs).")


class PriceUnavailable(RuntimeError):
    """The price source failed for a symbol (temporary: retried at the next run)."""


class PriceAccess(Protocol):
    def daily(self, symbol: str, first: date, last: date) -> Dict[date, Tuple[float, float]]:
        """{NY date: (open, close)} of the regular sessions first..last (adjusted)."""

    def minutes(self, symbol: str, start: datetime, end: datetime) -> List[Tuple[datetime, float]]:
        """[(bar start UTC, open)] of the 1-minute bars with start <= bar start < end, oldest first (adjusted)."""


@dataclass(frozen=True)
class Entry:
    session: date            # the session the entry price belongs to (E)
    ts: datetime             # when the price was available (bar start or session open/close), UTC
    kind: str                # "open" | "close" | "minute"
    basis: str               # entry_basis (see module doc)
    price: Optional[float] = None   # the stock's minute-bar open (kind "minute"); daily kinds read it from the bars


@dataclass(frozen=True)
class NotMeasurable:
    reason: str


def _utc(value) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _date(value) -> Optional[date]:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _open_entry(session: date, basis: str) -> Entry:
    return Entry(session, calendars.session_bounds(session)[0], "open", basis)


def filing_entry(trade: dict, prices: PriceAccess) -> object:
    """The tradable entry after the filing: Entry or NotMeasurable (see module doc)."""
    if trade.get("source") == "congress":
        filed = _date(trade.get("filing_date"))
        if filed is None:
            return NotMeasurable("filing date unknown")
        return _open_entry(calendars.next_trading_day(filed), "next_open_after_filing_date")
    accepted = _utc(trade.get("accepted_at"))
    if accepted is None:
        return NotMeasurable("acceptance time unknown")
    d = calendars.ny_date(accepted)
    bounds = calendars.session_bounds(d)
    if bounds is None:
        return _open_entry(calendars.next_trading_day(d), "next_open")
    open_, close = bounds
    if accepted < open_:
        return _open_entry(d, "next_open")
    if accepted >= close:
        return _open_entry(calendars.next_trading_day(d), "next_open")
    start = accepted.replace(second=0, microsecond=0) + timedelta(minutes=1)   # strictly after the acceptance minute
    if start < close:
        for ts, price in prices.minutes(trade["ticker"], start, close):
            if start <= ts < close and price and price > 0:
                return Entry(d, ts, "minute", "minute", price)
    return _open_entry(calendars.next_trading_day(d), "next_open_no_trade")


def trade_date_entry(trade: dict) -> object:
    d = _date(trade.get("trade_date_first"))
    if d is None:
        return NotMeasurable("trade date unknown")
    if calendars.is_trading_day(d):
        session, basis = d, "close"
    else:
        session, basis = calendars.previous_trading_day(d), "prev_close_non_trading_day"
    return Entry(session, calendars.session_bounds(session)[1], "close", basis)


def _exit_sessions(entry_session: date, h_max: int) -> List[date]:
    out, d = [], entry_session
    for _ in range(h_max):
        d = calendars.next_trading_day(d)
        out.append(d)
    return out


def _entry_price(entry: Entry, symbol: str, daily: Dict[date, Tuple[float, float]], prices: PriceAccess,
                 minute_cache: dict, own: bool = False) -> Optional[float]:
    if entry.kind == "minute" and own:
        return entry.price
    if entry.kind == "minute":
        key = (symbol, entry.ts)
        if key not in minute_cache:
            bars = prices.minutes(symbol, entry.ts, entry.ts + MINUTE_LOOKAHEAD_ETF)
            minute_cache[key] = next((p for ts, p in bars if ts >= entry.ts), None)
        return minute_cache[key]
    bar = daily.get(entry.session)
    if bar is None:
        return None
    return bar[0] if entry.kind == "open" else bar[1]


def _row(trade_id, start, h, **kw) -> dict:
    row = {"trade_id": trade_id, "start": start, "horizon": h, "state": None, "sessions_done": 0,
           "entry_ts": None, "entry_basis": None, "entry_price": None, "etf": None, "etf_entry_price": None,
           "exit_date": None, "exit_price": None, "etf_exit_price": None, "r_stock": None, "r_etf": None,
           "excess": None, "score": None, "reason": None}
    row.update(kw)
    return row


def compute(trade: dict, etf: Optional[str], etf_reason: Optional[str], prices: PriceAccess,
            now: Optional[datetime] = None) -> List[dict]:
    """All return rows (2 starts x 4 horizons) of one trade. ``etf`` None -> stock returns only, with ``etf_reason``.
    Raises PriceUnavailable when the price source fails (nothing partial is returned)."""
    now = _utc(now) or datetime.now(timezone.utc)
    tid = trade["id"]
    if not trade.get("measurable"):
        reason = trade.get("not_measurable_reason") or "not measurable"
        return [_row(tid, s, h, state="not_measurable", reason=reason) for s in STARTS for h in HORIZONS]
    symbol = trade["ticker"]
    sign = -1.0 if trade.get("code") == "S" else 1.0
    entries = {"filing": filing_entry(trade, prices), "trade_date": trade_date_entry(trade)}
    sessions = {s: _exit_sessions(e.session, max(HORIZONS)) for s, e in entries.items() if isinstance(e, Entry)}
    if sessions:
        first = min(e.session for e in entries.values() if isinstance(e, Entry))
        last_needed = max(v[-1] for v in sessions.values())
        last = min(last_needed, calendars.ny_date(now))
        daily = prices.daily(symbol, first, last) if first <= last else {}
        etf_daily = prices.daily(etf, first, last) if (etf and first <= last) else {}
    else:
        daily = etf_daily = {}
    minute_cache: dict = {}
    rows = []
    for start in STARTS:
        entry = entries[start]
        if isinstance(entry, NotMeasurable):
            rows += [_row(tid, start, h, state="not_measurable", reason=entry.reason) for h in HORIZONS]
            continue
        base = {"entry_ts": entry.ts.isoformat(timespec="seconds"), "entry_basis": entry.basis, "etf": etf}
        if entry.ts > now:
            rows += [_row(tid, start, h, state="running", reason="entry not reached yet", **base) for h in HORIZONS]
            continue
        p0 = _entry_price(entry, symbol, daily, prices, minute_cache, own=True)
        if p0 is None or p0 <= 0:
            reason = f"no price bar for {symbol} on {entry.session.isoformat()}"
            rows += [_row(tid, start, h, state="not_measurable", reason=reason, **base) for h in HORIZONS]
            continue
        e0 = _entry_price(entry, etf, etf_daily, prices, minute_cache) if etf else None
        exits = sessions[start]
        done = sum(1 for d in exits if calendars.session_bounds(d)[1] + SETTLE <= now)
        for h in HORIZONS:
            exit_day = exits[h - 1]
            common = dict(base, entry_price=p0, etf_entry_price=e0, exit_date=exit_day.isoformat(),
                          sessions_done=min(done, h))
            if calendars.session_bounds(exit_day)[1] + SETTLE > now:
                rows.append(_row(tid, start, h, state="running", **common))
                continue
            bar = daily.get(exit_day)
            if bar is None:
                rows.append(_row(tid, start, h, state="not_measurable",
                                 reason=f"no price bar for {symbol} on {exit_day.isoformat()}", **common))
                continue
            p1 = bar[1]
            r = p1 / p0 - 1.0
            out = dict(common, exit_price=p1, r_stock=r, state="complete")
            if not etf:
                out["reason"] = etf_reason
            else:
                ebar = etf_daily.get(exit_day)
                if e0 is None or e0 <= 0 or ebar is None:
                    out["reason"] = f"no price bar for {etf} on {(exit_day if ebar is None else entry.session).isoformat()}"
                else:
                    r_etf = ebar[1] / e0 - 1.0
                    out.update(etf_exit_price=ebar[1], r_etf=r_etf, excess=r - r_etf, score=sign * (r - r_etf))
            rows.append(_row(tid, start, h, **out))
    return rows


def chip(rows: Iterable[dict]) -> Optional[dict]:
    """The ONE return chip of a card (§7.1): start "filing", the longest COMPLETE horizon; none complete -> the
    1-day horizon (running or not measurable)."""
    filing = {r["horizon"]: r for r in rows if r["start"] == "filing"}
    if not filing:
        return None
    complete = [h for h in HORIZONS if filing.get(h, {}).get("state") == "complete"]
    r = filing[max(complete)] if complete else filing.get(1)
    if r is None:
        return None
    return {"start": "filing", "horizon": r["horizon"], "state": r["state"], "sessions_done": r["sessions_done"],
            "excess": r["excess"], "r_stock": r["r_stock"], "etf": r["etf"], "reason": r["reason"]}


def group_stats(rows: Iterable[dict], groups: Dict[str, str], horizon: int, start: str = "filing") -> List[dict]:
    """Track-record statistics. ``groups`` maps trade_id -> group key; rows are return rows. Per group: n (complete
    rows with a score), n_running, n_not_measurable, mean/median score, hit rate (score > 0), small_sample."""
    acc: Dict[str, dict] = {}
    for r in rows:
        if r["start"] != start or r["horizon"] != horizon or r["trade_id"] not in groups:
            continue
        g = acc.setdefault(groups[r["trade_id"]], {"scores": [], "running": 0, "not_measurable": 0})
        if r["state"] == "running":
            g["running"] += 1
        elif r["state"] == "complete" and r["score"] is not None:
            g["scores"].append(r["score"])
        else:
            g["not_measurable"] += 1
    out = []
    for key in sorted(acc):
        s = acc[key]["scores"]
        out.append({"group": key, "n": len(s), "n_running": acc[key]["running"],
                    "n_not_measurable": acc[key]["not_measurable"],
                    "mean_score": mean(s) if s else None, "median_score": median(s) if s else None,
                    "hit_rate": (sum(1 for x in s if x > 0) / len(s)) if s else None,
                    "small_sample": len(s) < SMALL_SAMPLE})
    return out


class AlpacaPrices:
    """Real prices: daily bars through the shared bar cache (hedgefund.db, adjustment "all"), minute bars straight
    through the configured provider and its HTTP gate (not cached — only the derived entry price is stored).
    Results are memoised for the lifetime of the object (one job run)."""

    def __init__(self, now: Optional[datetime] = None):
        self._now = _utc(now)
        self._daily: Dict[tuple, Dict[date, Tuple[float, float]]] = {}

    def daily(self, symbol: str, first: date, last: date) -> Dict[date, Tuple[float, float]]:
        key = (symbol, first, last)
        if key in self._daily:
            return self._daily[key]
        from strategy_platform.core.errors import UserFacingError
        from strategy_platform.data import cache
        from strategy_platform.data.providers.base import parse_ts
        now = self._now or datetime.now(timezone.utc)
        start = datetime.combine(first, datetime.min.time(), tzinfo=timezone.utc)
        end = datetime.combine(last + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        last_close = calendars.last_close_before(now)
        # the newest needed session has closed -> its bar must be included (never a stale series as if current)
        fresh = last_close if calendars.ny_date(last_close) <= last else None
        try:
            bars = cache.get_bars(symbol, "1Day", start, min(end, now), require_fresh_through=fresh,
                                  adjustment="all")
        except UserFacingError as ex:
            raise PriceUnavailable(f"{symbol}: {ex.user_message}") from ex
        out = {}
        for b in bars or []:
            d = calendars.ny_date(parse_ts(b["ts"]))
            if first <= d <= last and b.get("open") is not None and b.get("close") is not None:
                out[d] = (float(b["open"]), float(b["close"]))
        self._daily[key] = out
        return out

    def minutes(self, symbol: str, start: datetime, end: datetime) -> List[Tuple[datetime, float]]:
        from strategy_platform.core.errors import UserFacingError
        from strategy_platform.data import registry
        from strategy_platform.data.providers.base import parse_ts
        try:
            bars = registry.fetch("equity_bars", "get_bars", symbol, "1Min", start, end)
        except UserFacingError as ex:
            raise PriceUnavailable(f"{symbol}: {ex.user_message}") from ex
        out = []
        for b in bars or []:
            if b.get("open") is not None:
                out.append((parse_ts(b["ts"]), float(b["open"])))
        out.sort()
        return out
