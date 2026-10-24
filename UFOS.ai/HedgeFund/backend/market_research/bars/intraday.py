"""1m loading for Research — intraday model c (SPEC §7.1, Q2) and the 30-day 1m store (Q12, C1).

- ``sip_through(now)`` = now − 15 min, floored to the minute. Equity minutes with start < sip_through come
  from SIP 1m history (``sources.sip_1m`` → ``AlpacaSipProvider``, request end clamped to
  ``sip_request_end``); they are final and stored in ``market_research.db`` ``bars_1m`` (feed ``sip``) for
  the NY days inside the retention window (``INTRADAY_LOOKBACK_DAYS``). Coverage per (symbol, day, feed)
  is kept in ``bars_1m_days`` (``through`` = how far that day was fetched), because minutes without
  trades are absent and cannot tell "fetched" from "missing".
- The live tail (start ≥ sip_through) = IEX 1m REST bars at load (``sources.iex_1m``), feed
  ``iex_tail``, never stored and never final; the hub (MR2b) carries it on from IEX trades and replaces it
  by SIP (``sip_replace``).
- Equity days older than the retention window (only 1H/4H ask for them) are fetched per request as SIP
  **30-minute** bars (clock aligned = on the 09:30 / 04:00 anchors, so 1H/4H buckets built from them equal
  the 1m buckets; review M4), not stored; their dicts carry ``"span": 30``. At most
  ``limits.TRANSIENT_30M_DAYS`` calendar days per provider call (≤ ~10 000 bars = one Alpaca page).
- Crypto: Alpaca crypto 1m (one feed, no tail), UTC days, stored with feed ``crypto``; only complete
  minutes (start + 1 min ≤ now); the coverage mark stays ``CRYPTO_SETTLE`` behind now so a minute
  published late is fetched again.
Fetches are batched to ≤ ``FETCH_1M_DAYS`` calendar days per provider call (≈ one Alpaca page; the span
of a batch, not only its number of days, is bounded — review L6).

Blocking (SQLite + REST): call from the Research pool under ``http_gate.research_priority()``.

Part of UFOS.ai, a proof-of-concept portfolio project; research only.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Callable, List, Optional, Tuple

from strategy_platform.core import calendars
from strategy_platform.core.clock import iso, to_utc

from .. import limits
from .. import store as default_store
from . import aggregate as agg

SIP_DELAY = timedelta(minutes=limits.SIP_DELAY_MINUTES)
CRYPTO_SETTLE = timedelta(minutes=2)
ONE_MINUTE = timedelta(minutes=1)


def floor_minute(t: datetime) -> datetime:
    t = to_utc(t)
    return t.replace(second=0, microsecond=0)


def sip_through(now) -> datetime:
    """Start of the first minute that is NOT yet available from SIP (now − 15 min, floored)."""
    return floor_minute(to_utc(now) - SIP_DELAY)


def retention_day(now, asset_class: str) -> date:
    """Oldest day (NY date for equities, UTC date for crypto) whose 1m bars Research keeps/serves."""
    now = to_utc(now)
    d = now.date() if asset_class == "crypto" else now.astimezone(agg.NY).date()
    return d - timedelta(days=limits.INTRADAY_LOOKBACK_DAYS - 1)


def _days(start: date, end: date) -> List[date]:
    out, d = [], start
    while d <= end:
        out.append(d)
        d += timedelta(days=1)
    return out


class MinuteLoader:
    def __init__(self, sources, *, store=default_store, now: Optional[Callable[[], datetime]] = None):
        self.sources = sources
        self.store = store
        self._now = now or (lambda: datetime.now(timezone.utc))

    # --- equities ---------------------------------------------------------------------------------
    def _fetch_sip_days(self, symbol: str, plan: List[Tuple[date, datetime, datetime]], *, store_it: bool,
                        fetched_at: str) -> List[dict]:
        """plan: [(day, fetch_from, fetch_to)] in day order; batched calls; returns the fetched rows."""
        rows_all: List[dict] = []
        per_call = limits.FETCH_1M_DAYS["equity"]
        i = 0
        while i < len(plan):
            group = [plan[i]]
            while i + len(group) < len(plan) and len(group) < per_call and \
                    (plan[i + len(group)][0] - group[0][0]).days < per_call:
                group.append(plan[i + len(group)])
            a, b = group[0][1], group[-1][2]
            rows = self.sources.sip_1m(symbol, a, b) if b > a else []
            if store_it:
                if rows:
                    self.store.bars_1m_upsert(symbol, "sip", rows, fetched_at)
                for day, _f, to in group:
                    self.store.bars_1m_day_set(symbol, "sip", day.isoformat(), iso(to), fetched_at)
            rows_all.extend(rows)
            i += len(group)
        return rows_all

    def equity_minutes(self, symbol: str, start, end, *, now=None, with_tail: bool = True) -> List[dict]:
        """Minute dicts of ``symbol`` with start ≤ t < end (ETH windows of trading days; filter per session
        happens in aggregate): SIP (stored / transient) + the IEX tail."""
        now = to_utc(now or self._now())
        start, end = to_utc(start), min(to_utc(end), now)
        if end <= start:
            return []
        through = sip_through(now)
        sip_end = min(end, through)
        keep_from = retention_day(now, "equity")
        fetched_at = iso(now)
        stored_plan, transient_plan = [], []
        days = [d for d in _days(start.astimezone(agg.NY).date(), (end - ONE_MINUTE).astimezone(agg.NY).date())
                if calendars.is_trading_day(d)]
        coverage = self.store.bars_1m_days_get(symbol, "sip", [d.isoformat() for d in days if d >= keep_from])
        for d in days:
            w = agg.session_window(d, "eth")
            if w is None:
                continue
            target = min(w[1], through)
            if target <= w[0]:
                continue
            if d >= keep_from:
                done = coverage.get(d.isoformat())
                frm = max(w[0], to_utc(done)) if done else w[0]
                if frm < target:
                    stored_plan.append((d, frm, target))
            elif w[0] < sip_end and w[1] > start:
                transient_plan.append((d, max(w[0], start), min(target, sip_end)))
        minutes: List[dict] = []
        if transient_plan:
            span = timedelta(days=limits.TRANSIENT_30M_DAYS)
            i = 0
            while i < len(transient_plan):
                j = i
                while j + 1 < len(transient_plan) and transient_plan[j + 1][0] - transient_plan[i][0] < span:
                    j += 1
                rows = self.sources.sip_30m(symbol, transient_plan[i][1], transient_plan[j][2])
                for r in rows:
                    if start <= to_utc(r["ts"]) < sip_end:
                        m = agg.minute(r, "sip")
                        m["span"] = 30
                        minutes.append(m)
                i = j + 1
        if stored_plan:
            self._fetch_sip_days(symbol, stored_plan, store_it=True, fetched_at=fetched_at)
        stored_from = max(start, agg.ny_midnight(keep_from))
        if stored_from < sip_end:
            minutes.extend(agg.minute(r, "sip") for r in
                           self.store.bars_1m_query(symbol, "sip", iso(stored_from), iso(sip_end)))
        if with_tail and end > through:
            tail_from = max(start, through)
            minutes.extend(agg.minute(r, "iex_tail") for r in self.sources.iex_1m(symbol, tail_from, end))
        minutes.sort(key=lambda m: m["t"])
        return minutes

    # --- crypto -----------------------------------------------------------------------------------
    def crypto_minutes(self, symbol: str, start, end, *, now=None) -> List[dict]:
        """Complete crypto minutes with start ≤ t < end inside the retention window (older → ValueError)."""
        now = to_utc(now or self._now())
        keep_from = retention_day(now, "crypto")
        start = to_utc(start)
        if start < agg.utc_midnight(keep_from):
            raise ValueError("crypto 1m bars older than the retention window are not kept")
        complete_to = floor_minute(now)              # the minute starting here is still open
        end = min(to_utc(end), complete_to)
        if end <= start:
            return []
        settle = complete_to - CRYPTO_SETTLE
        fetched_at = iso(now)
        days = _days(start.date(), (end - ONE_MINUTE).date())
        coverage = self.store.bars_1m_days_get(symbol, "crypto", [d.isoformat() for d in days])
        plan = []
        for d in days:
            d0, d1 = agg.utc_midnight(d), agg.utc_midnight(d + timedelta(days=1))
            target = min(d1, complete_to)
            done = coverage.get(d.isoformat())
            frm = max(d0, to_utc(done)) if done else d0
            if frm < target:
                plan.append((d, frm, target))
        per_call = limits.FETCH_1M_DAYS["crypto"]
        i = 0
        while i < len(plan):
            group = [plan[i]]
            while i + len(group) < len(plan) and (plan[i + len(group)][0] - plan[i][0]).days < per_call:
                group.append(plan[i + len(group)])        # bounded SPAN (≤ per_call calendar days), review L6
            rows = self.sources.crypto_1m(symbol, group[0][1], group[-1][2])
            if rows:
                self.store.bars_1m_upsert(symbol, "crypto", rows, fetched_at)
            for d, _f, to in group:
                self.store.bars_1m_day_set(symbol, "crypto", d.isoformat(), iso(min(to, settle)), fetched_at)
            i += len(group)
        return [agg.minute(r, "crypto") for r in self.store.bars_1m_query(symbol, "crypto", iso(start), iso(end))]
