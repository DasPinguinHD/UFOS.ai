"""Backtest engine v2 (SPEC §8.3): a strategy spec simulated day by day on
the REAL ``execution.paper_broker.PaperBroker`` (fees, slippage, Reg T margin, borrow
fees, Rule 201, dividends, crypto perps with funding and liquidation) over
an in-memory store (``backtest/memstore.py``) and a simulated clock.

``run(spec, *, start=None, end=None, capital=100000, benchmark=None,
history_from=None, progress=None, cancel=None, data=None, cost_overrides=None,
clamp_warmup=False) -> BacktestResult``

Data (``data`` = loader object, default ``DataLoader()``):
- equities: ``seam.history(sym, "all")`` -> signals, ``seam.history(sym,
  "split")`` -> fills / valuation, ``seam.corporate_actions`` -> dividends
  (``cash_amount_split_adj``, the split series' share basis),
  ``seam.volume`` -> participation limit (consolidated dollar volume:
  Alpaca SIP from the seam, Tiingo before it — SPEC §8.8, 2026-10-06);
- crypto: ``data.cache.get_bars(sym, "1Day")`` -> one series for signals,
  fills and volume (no corporate actions).

Calendar (as §7.7): equities only or mixed -> NYSE sessions; crypto only ->
UTC days. Each simulated day has two phases with fixed timestamps:
- NYSE: open phase = session open + 6 min (after PaperBroker's 5-minute
  opening grace), close phase = session close - 1 min (still in session, so
  the broker fills market orders at once);
- UTC days: open phase 00:00:00 UTC, close phase 23:59:00 UTC.
Quotes (``Quote.ts`` = the phase time, so the broker treats them as current):
equities = open / close of that NY date from the ``split`` series;
crypto on UTC days = open / close of the bar of that UTC date; crypto in a
mixed (NYSE) run = the newest price known at the phase time — a bar's open
at its start, its close at its ``known_at`` — at most 24 h old (else no
quote). ``prev_close`` (Rule 201) = the previous session's close; the
Rule 201 bar look-back only sees bars before the current day.

Per day: open phase -> ``daily_accruals`` (borrow, dividends on the ex-date
for positions held when the ex-date began, perp funding), orders of a
``next_open`` decision, ``check_margin``; close phase -> ``daily_accruals``
(funding windows since the morning), orders of a ``next_close`` decision,
``check_margin``, ``snapshot_equity`` (mark to market), then the decision for
the NEXT day from data known at this close: the signal row of the day
(``evaluate`` on the full panel — causal, stage-2 property; row t only uses
rows <= t), on rebalance days ``portfolio.target_weights``; risk block.
Orders are sent as ``PaperBroker.target(symbol, weight, "autopilot")``,
reducing orders first, then increases/flips, each by symbol.

Participation limit (equities only): an order's notional (|target qty -
held| x execution quote) <= 1 % of the 20-session average dollar volume known
at the decision (``dollar_volume_split`` of ``seam.volume`` for the 20
sessions ending on the decision day, all 20 required). Crypto has NO volume
limit (user decision 2026-10-03: Alpaca crypto volume covers only Alpaca's
own venue and would be far too strict) — run-level warning
``crypto_no_participation_limit``; fills assume any size is tradable. The excess is dropped (not
carried) and counted (warning ``participation_cut``). Missing volume -> the
symbol's order is refused that day (warning ``missing_volume``) — no default
volume. Margin-call covers of the broker are not limited (broker-internal).

Risk block (``spec.risk``), evaluated at the close of day t, acting at the
next execution: ``stop_loss_pct`` / ``take_profit_pct`` per position (P&L vs
the average entry price, sign-aware) -> exit, and the symbol stays flat until
the next rebalance after the trigger; ``max_drawdown_stop_pct`` (equity vs
its peak) -> liquidate everything and stay flat for the rest of the run;
``vol_target_annual`` -> on rebalance days the target weights are scaled by
min(1, target / ex-ante volatility) (60 daily returns of the ``all`` series,
covariance, x252 NYSE / x365 UTC; de-levering only — the paper account's
longs are a cash account); a symbol without 60 complete returns is dropped
from that rebalance (warning ``vol_target_no_history``).

Costs: ``costs.model = custom`` sets the broker's ``fees_bps`` and
``crypto_fees_bps`` to fee_bps + slippage_bps for this run (the broker
always books half as adverse slippage in the price and half as a fee; when
fee != slippage the split differs, total cost per fill is the same —
warning ``custom_costs_split``). ``cost_overrides`` (stress hook, 3d, user
decision 2026-10-04; None in normal runs) = {fees_bps?, crypto_fees_bps?}:
per-class per-fill cost in bps (0..2000) written into the broker params of
THIS run after the spec's costs (warning ``cost_override``; echoed in
``costs.overrides``). Broker params = ``core.params.defaults()``
with ``starting_capital = capital`` and ``short_exposure_max_pct = 100 x
gross_exposure`` (the fund-level short cap of the live paper account is a
fund preference, not a market rule; the strategy's exposure comes from its
spec). Benchmark: request > ``spec["benchmark"]`` > SPY, buy-and-hold
(``target(benchmark, 1.0)``) on its own PaperBroker with the same params,
bought at the same execution point as the strategy's first orders,
dividends booked as cash. A crypto-only strategy with an equity benchmark:
the benchmark runs on NYSE sessions and each UTC day shows its latest close
value.

Range (fix 2026-10-09, SPEC §8.5): ``history_from`` = the data LOAD start of a
run without an explicit start: "2016" (default, None) = the seam 2016-01-04
(Alpaca history), "2000" = 2000-01-03 (Tiingo before the seam, key needed);
start None -> loading from there, trading from the first day with data +
warm-up. An explicit ``start`` is the first TRADING day: loading begins
``warm-up`` sessions (NYSE) / days (UTC) before it, ``history_from`` is then
only recorded; a warm-up reaching before 2000-01-03 -> ``InvalidRequest``
("… use 'From 2000' or a later start") — except with ``clamp_warmup=True``
(stress episodes, SPEC §8.10: loading starts at 2000-01-03 and the episode
reports ``short_warmup``). end None -> the last day for which ALL required
sources have data (``range.end_limited_by`` names the source); an explicit
end beyond that -> ``InvalidRequest``. The NYSE calendar covers 2000-2030
(core.calendars); outside it the calendar's error is raised (no guessed
sessions).

Holdout lock (stage 4, SPEC §9.1, V1 notes §9.8): every run is locked out of
the global holdout window of its universe (``validation.families``: last
complete session − 24 months with equities / mixed, − 12 months crypto-only)
unless ``holdout_access=True`` (ONLY the holdout job of the validation layer):
end None -> the data is loaded and simulated only up to the training end (the
session before the window; ``range.end_limited_by`` is then None when the lock,
not the data, ends the run); an explicit start/end on/after the window start
-> ``families.HoldoutWindowViolation`` (an ``InvalidRequest``). ``range.holdout``
= {universe_kind, start, end, training_end, access}. The universe kind comes
from the strategy's symbols (the benchmark does not count).

Analysis hook (3d, SPEC §8.10): the returned result carries a plain
attribute ``analysis`` (not a dataclass field, not in ``to_dict()``) = {dates
(panel, ISO), symbols, signal (ndarray dates x symbols), close_all (the
``all`` close panel), i0, i1 (simulated rows), calendar, dollar_volume
{symbol: {date: split-basis dollar volume}} (equities), crypto [symbols]} —
read by ``metrics.signal_quality`` / ``metrics.capacity``.

Per-symbol P&L (SPEC §11.1, X1, 2026-10-14; ENGINE_VERSION 3c.2 — recording only, the simulation is
unchanged): after every VALUED close snapshot the engine notes each held position's value qty x mark
(the same close quote the broker valued it with). ``result.symbol_pnl`` = {fields, rows} with
fields ``SYMBOL_PNL_FIELDS`` = date, symbol, pnl, mtm, dividends, fees, slippage, borrow, funding, mv:
per (valued day, symbol) where the symbol was held at that close or booked cash that day (sparse, rows
sorted by date then symbol). Cash ledger rows are binned to the first valued close at or after their
timestamp (a day whose valuation was paused passes its rows on). mtm = mv_t - mv_(previous valued
close) + the fill cash of the bin (price P&L on the ``split`` series; it CONTAINS the adverse slippage
of the fill prices); dividends = dividend + dividend_short; fees = -(fee + liquidation_fee); borrow =
borrow_fee; funding as booked (all signed cash, + = gain); slippage = the reconstructed adverse price
part of the bin's fills as a POSITIVE cost (metrics.costs rule: qty x |fill - fill / (1 +- h)|), shown
separately but already inside mtm; pnl = mtm + dividends + fees + borrow + funding; mv = signed position
value at the close. Ledger rows without a symbol other than deposit / reset (none exist today) go to
the symbol ``"_cash"``. Sum of pnl over all rows = last valued equity - starting capital.
``record_symbol_pnl`` (review L7, 2026-10-16): True (default) for single backtests and the validation
base / holdout runs; False for the grid and stress worker runs (``symbol_pnl`` None — their results are
reduced to returns / metrics, recording would only cost memory). No effect on the simulation.

Deterministic (no randomness, sorted iteration). ``progress(phase, percent,
message)``; ``cancel()`` is checked per symbol load and per simulated day
(``BacktestCancelled``).

Part of UFOS.ai, a proof-of-concept portfolio project; a simulation with
paper-trading rules, not investment advice.
"""
from __future__ import annotations

import bisect
import dataclasses
import hashlib
import json
import math
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..core.errors import InvalidRequest, UserFacingError

ENGINE_VERSION = "sp-backtest/3c.2"   # 3c.2 (2026-10-14, SPEC §11.1): + symbol_pnl, recording only
DEFAULT_CAPITAL = 100_000.0
CAPITAL_MIN = 1_000.0
CAPITAL_MAX = 100_000_000.0
DEFAULT_BENCHMARK = "SPY"
PARTICIPATION_RATE = 0.01
ADV_WINDOW = 20
VOL_TARGET_LOOKBACK = 60
WARMUP_MARGIN = 5
HISTORY_FROM_CHOICES = ("2016", "2000")   # request field history_from (fix 2026-10-09, SPEC §8.5)
DEFAULT_HISTORY_FROM = "2016"
LATE_START_DAYS = 365          # many_late_starts: a symbol counts as "late" beyond this (user, 2026-10-06)
LATE_START_SHARE = 0.25        # … and the warning fires above this share of the universe
OPEN_DELAY = timedelta(minutes=6)          # PaperBroker.PENDING_FILL_DELAY_MINUTES (5) + 1
CLOSE_LEAD = timedelta(minutes=1)
UTC_CLOSE = timedelta(hours=23, minutes=59)
CRYPTO_QUOTE_MAX_AGE = timedelta(hours=24)
ORIGIN = "autopilot"
MAX_LISTED_DATES = 50
PERIODS_PER_YEAR = {"nyse": 252, "utc": 365}
PHASES = ("open", "close")
COST_OVERRIDE_KEYS = ("fees_bps", "crypto_fees_bps")
SYMBOL_PNL_FIELDS = ("date", "symbol", "pnl", "mtm", "dividends", "fees", "slippage", "borrow", "funding", "mv")
CAPITAL_FLOW_KINDS = ("deposit", "reset")          # capital, not P&L (SPEC §11.1)
CASH_SYMBOL = "_cash"
COST_OVERRIDE_MAX_BPS = 2000.0          # = the custom path's maximum (fee 1000 + slippage 1000)


class BacktestCancelled(UserFacingError):
    is_cancellation = True      # data.registry leaves the source status alone

    def __init__(self):
        super().__init__("The backtest was cancelled.")


class BacktestDataError(UserFacingError):
    """The loaded data cannot be simulated (malformed rows, a dividend
    without an amount, no data for the benchmark …)."""


@dataclasses.dataclass
class BacktestResult:
    engine_version: str
    spec_hash: str
    request: dict                    # {start, end, capital, benchmark, history_from} as asked (None = default)
    range: dict                      # {start, end, calendar, first_data, end_limited_by, warmup_sessions}
    dates: List[str]                 # simulated days (ISO)
    equity: List[Optional[float]]    # close snapshot per day (None = valuation paused)
    cash: List[Optional[float]]
    long_mv: List[Optional[float]]
    short_mv: List[Optional[float]]
    drawdown_pct: List[Optional[float]]
    benchmark: List[Optional[float]]  # benchmark account equity per day
    benchmark_info: dict              # {symbol, calendar, orders, ledger}
    positions: List[dict]             # [{date, positions: {symbol: qty}}] only when changed
    orders: List[dict]                # every order row (memstore = fund.store shape), oldest first
    fills: List[dict]                 # filled orders, compact
    ledger: List[dict]                # cash ledger rows, oldest first
    events: List[dict]                # broker events (margin calls)
    warnings: List[dict]              # structured: {code, message, symbol?, …}
    data: dict                        # per symbol: sources, ranges, seam, row hashes
    broker_params: dict
    costs: dict
    stats: dict
    # SPEC §11.1 (X1, 2026-10-14): per symbol and valued day, sparse, columnar — see _symbol_pnl()
    symbol_pnl: Optional[dict] = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


# --- data loader ------------------------------------------------------------------------------

class DataLoader:
    """Injectable data access. Default: ``data.seam`` (equities: history
    "all"/"split", corporate actions, volume) and ``data.cache.get_bars``
    (crypto). ``seam`` may be a ``Seam`` instance or the module.
    ``run`` loads symbols in parallel (SPEC §10.1, ``_load_symbols``); a
    loader may set ``max_workers`` (int) — default ``data.network.pool_size()``."""

    def __init__(self, seam=None, get_bars: Optional[Callable] = None):
        self._seam = seam
        self._get_bars = get_bars

    def _s(self):
        if self._seam is None:
            from ..data import seam
            return seam
        return self._seam

    def history(self, symbol: str, adjustment: str, start: date, end: Optional[date] = None, *, progress=None):
        return self._s().history(symbol, adjustment, start, end, progress=progress)

    def corporate_actions(self, symbol: str, start: date, end: Optional[date] = None, *, progress=None):
        return self._s().corporate_actions(symbol, start, end, progress=progress)

    def volume(self, symbol: str, start: date, end: Optional[date] = None, *, progress=None):
        # default range (end None): a missing NEWEST Tiingo session limits the end instead of waiting
        return self._s().volume(symbol, start, end, progress=progress)

    def crypto_bars(self, symbol: str, start: date, end: Optional[date] = None):
        get = self._get_bars
        if get is None:
            from ..data import cache
            get = cache.get_bars
        s = datetime.combine(start, time(0), tzinfo=timezone.utc)
        e = None if end is None else datetime.combine(end + timedelta(days=1), time(0), tzinfo=timezone.utc) \
            - timedelta(seconds=1)
        rows = list(get(symbol, "1Day", s, e) or [])
        sources = sorted({r.get("source") for r in rows if r.get("source")})
        info = {"symbol": symbol, "start": start.isoformat(), "end": None if end is None else end.isoformat(),
                "sources": sources, "rows": len(rows),
                "first": _utc_date(rows[0]["ts"]).isoformat() if rows else None,
                "last": _utc_date(rows[-1]["ts"]).isoformat() if rows else None, "warnings": []}
        return rows, info

    def ext_values(self, spec: dict, panel):
        """External feature values for ``evaluate`` (None = evaluate loads them)."""
        return None


class _LoadStopped(UserFacingError):
    """A symbol load stopped because another symbol's load failed (parallel loading)."""
    is_cancellation = True      # data.registry leaves the source status alone

    def __init__(self):
        super().__init__("Data loading stopped after another symbol failed.")


def _load_symbols(loader, needed: List[str], classes: Dict[str, str], bclass: str, load_start: date,
                  load_end: Optional[date], *, emit: Callable, check_cancel: Callable[[], None],
                  unknown_symbol: type) -> List[Tuple[dict, bool]]:
    """Loads every symbol of ``needed`` (SPEC §10.1): a thread pool of
    ``loader.max_workers`` (if set) or ``data.network.pool_size()`` workers;
    the HTTP requests in flight per source are bounded by the network
    limiters in front of the HTTP gate. Returns [(parts, no_volume)] in
    ``needed`` order — the same values as loading one symbol after the other.
    The first failure stops symbols not yet started (and gate waits of running
    ones); the error raised is the one of the EARLIEST symbol in ``needed``
    order that failed on its own — the pool starts symbols in order, so that is
    the error a sequential load would raise."""
    import threading
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    n = len(needed)
    if n == 0:
        return []
    workers = getattr(loader, "max_workers", None)
    if not isinstance(workers, int) or isinstance(workers, bool) or workers < 1:
        from ..data import network
        workers = network.pool_size()
    workers = max(1, min(int(workers), n))
    stop = threading.Event()
    count_lock = threading.Lock()
    done = [0]

    def pct() -> float:
        with count_lock:
            return 100.0 * done[0] / n

    def load_one(k: int, s: str) -> Tuple[dict, bool]:
        if stop.is_set():
            raise _LoadStopped()
        check_cancel()
        emit("data", pct(), f"loading {s} ({k + 1}/{n})")

        def sp(event):
            if stop.is_set():
                raise _LoadStopped()
            check_cancel()
            if isinstance(event, dict) and event.get("message"):
                emit("data", pct(), f"{s}: {event['message']}")

        cls = classes.get(s, bclass)
        strat = s in classes
        e: dict = {}
        no_volume = False
        if cls == "crypto":
            e["bars"] = loader.crypto_bars(s, load_start, load_end)
        else:
            if strat:
                e["all"] = loader.history(s, "all", load_start, load_end, progress=sp)
            e["split"] = loader.history(s, "split", load_start, load_end, progress=sp)
            e["actions"] = loader.corporate_actions(s, load_start, load_end, progress=sp)
            if strat:
                try:
                    e["volume"] = loader.volume(s, load_start, load_end, progress=sp)
                except unknown_symbol:
                    e["volume"] = None
                    no_volume = True
        with count_lock:
            done[0] += 1
        return e, no_volume

    results: List[Optional[Tuple[dict, bool]]] = [None] * n
    errors: Dict[int, BaseException] = {}
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sp-data")
    try:
        futures = {pool.submit(load_one, k, s): k for k, s in enumerate(needed)}
        pending = set(futures)
        while pending and not stop.is_set():
            finished, pending = wait(pending, return_when=FIRST_COMPLETED)
            for f in finished:
                ex = f.exception()
                if ex is not None:
                    stop.set()
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    for f, k in futures.items():
        if f.cancelled():
            continue
        ex = f.exception()
        if ex is None:
            results[k] = f.result()
        elif not isinstance(ex, _LoadStopped):
            errors[k] = ex
    if errors:
        raise errors[min(errors)]
    return results          # type: ignore[return-value]


# --- small helpers ------------------------------------------------------------------------------

def _ts(value) -> datetime:
    from ..data.providers.base import parse_ts
    return parse_ts(value)


def _ny_date(value) -> date:
    from ..core import calendars
    return _ts(value).astimezone(calendars.NY).date()


def _utc_date(value) -> date:
    return _ts(value).astimezone(timezone.utc).date()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _as_date(value, name: str) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise InvalidRequest(f"{name} must be a date (YYYY-MM-DD).") from None


def _crypto_known(row: dict) -> datetime:
    """When a crypto daily bar was knowable: its ``known_at``, else the documented rule
    ``data.pit.known_at`` (bar start + 1 day) — review 2026-10-04: no ad-hoc default."""
    if row.get("known_at"):
        return _ts(row["known_at"])
    from ..data import pit
    return pit.known_at("crypto", "1Day", row["ts"])


def _rows_hash(rows: List[dict], keys: Tuple[str, ...]) -> str:
    payload = json.dumps([[r.get(k) for k in keys] for r in rows], default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


BAR_KEYS = ("ts", "open", "high", "low", "close", "volume", "source")
VOLUME_KEYS = ("date", "volume", "close_split", "source")
ACTION_KEYS = ("kind", "ex_date", "cash_amount", "split_ratio", "cash_amount_split_adj", "source")


def phase_times(day: date, calendar: str) -> Tuple[datetime, datetime]:
    """(open-phase time, close-phase time) of a simulated day (UTC datetimes)."""
    if calendar == "nyse":
        from ..core import calendars
        bounds = calendars.session_bounds(day)
        if bounds is None:
            raise BacktestDataError(f"{day.isoformat()} is not an NYSE session.")
        return bounds[0] + OPEN_DELAY, bounds[1] - CLOSE_LEAD
    t0 = datetime.combine(day, time(0), tzinfo=timezone.utc)
    return t0, t0 + UTC_CLOSE


class _Warnings:
    """Structured warnings; repeated per-day events are aggregated per (code, symbol)."""

    def __init__(self):
        self._items: List[dict] = []
        self._keys = set()
        self._agg: Dict[Tuple[str, Optional[str]], dict] = {}

    def add(self, code: str, message: str, **fields) -> None:
        key = (code, fields.get("symbol"), message)
        if key in self._keys:
            return
        self._keys.add(key)
        item = {"code": code, "message": message}
        item.update({k: v for k, v in fields.items() if v is not None})
        self._items.append(item)

    def count(self, code: str, symbol: Optional[str], day: Optional[date], message: str, **amounts) -> None:
        key = (code, symbol)
        item = self._agg.get(key)
        if item is None:
            item = {"code": code, "symbol": symbol, "count": 0, "dates": [], "message": message}
            self._agg[key] = item
        item["count"] += 1
        if day is not None and len(item["dates"]) < MAX_LISTED_DATES:
            item["dates"].append(day.isoformat())
        for k, v in amounts.items():
            item[k] = item.get(k, 0.0) + v

    def total(self, code: str) -> int:
        return sum(v["count"] for (c, _s), v in self._agg.items() if c == code)

    def items(self) -> List[dict]:
        agg = [dict(v, message=v["message"].replace("{count}", str(v["count"]))) for _k, v in
               sorted(self._agg.items(), key=lambda kv: (kv[0][0], kv[0][1] or ""))]
        return list(self._items) + agg


# --- market data for execution -------------------------------------------------------------------

def _num(row: dict, key: str, symbol: str) -> float:
    v = row.get(key)
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise BacktestDataError(f"{symbol}: bar {row.get('ts')} has no valid {key} ({v!r}).") from None
    if not math.isfinite(f) or f <= 0:
        raise BacktestDataError(f"{symbol}: bar {row.get('ts')} has no valid {key} ({v!r}).")
    return f


class _Instrument:
    """Execution prices, dividends and dollar volume of one symbol."""

    def __init__(self, symbol: str, asset_class: str, bars: List[dict]):
        from strategy_platform.core.types import Bar
        self.symbol = symbol
        self.crypto = asset_class == "crypto"
        self.days: List[date] = []
        self.by_day: Dict[date, Tuple[float, float, float]] = {}       # (open, close, low)
        self.fund_bars: List[Any] = []
        self.events: List[Tuple[datetime, float]] = []                  # crypto: (time, price)
        self.event_times: List[datetime] = []
        self.dividends: List[Tuple[date, float, datetime]] = []
        self.volume_by_day: Optional[Dict[date, float]] = None          # equities (seam.volume)
        rows = sorted(bars, key=lambda b: _ts(b["ts"]))
        for b in rows:
            d = _utc_date(b["ts"]) if self.crypto else _ny_date(b["ts"])
            if d in self.by_day:
                raise BacktestDataError(f"{symbol}: two daily bars on {d.isoformat()}.")
            o, c = _num(b, "open", symbol), _num(b, "close", symbol)
            low, high = _num(b, "low", symbol), _num(b, "high", symbol)      # missing -> BacktestDataError
            self.by_day[d] = (o, c, low)
            self.days.append(d)
            self.fund_bars.append(Bar(date=d.isoformat(), open=o, high=high, low=low,
                                      close=c, volume=float(b.get("volume") or 0.0)))
            if self.crypto:
                start = _ts(b["ts"])
                known = _crypto_known(b)
                self.events.append((start, o))
                self.events.append((known, c))
        self.events.sort(key=lambda e: e[0])
        self.event_times = [e[0] for e in self.events]

    # quotes
    def price(self, day: date, phase: str, now: datetime, calendar: str) -> Optional[float]:
        if self.crypto and calendar == "nyse":
            k = bisect.bisect_right(self.event_times, now) - 1
            if k < 0 or now - self.events[k][0] > CRYPTO_QUOTE_MAX_AGE:
                return None
            return self.events[k][1]
        v = self.by_day.get(day)
        if v is None:
            return None
        return v[0] if phase == "open" else v[1]

    def prev_close(self, day: date) -> Optional[float]:
        if self.crypto:
            return None
        from ..core import calendars
        v = self.by_day.get(calendars.previous_trading_day(day))
        return v[1] if v else None

    def bars_before(self, day: date, n: int = 4) -> list:
        i = bisect.bisect_left(self.days, day)
        return self.fund_bars[max(0, i - n):i]

    # volume
    def adv_equity(self, sessions: List[date], i: int) -> Optional[float]:
        if self.volume_by_day is None or i + 1 < ADV_WINDOW:
            return None
        vals = [self.volume_by_day.get(d) for d in sessions[i + 1 - ADV_WINDOW:i + 1]]
        if any(v is None for v in vals):
            return None
        return math.fsum(vals) / ADV_WINDOW

    def dividends_since(self, since: date, now: datetime) -> List[Tuple[date, float]]:
        return [(ex, amt) for ex, amt, known in self.dividends if ex > since and known <= now]


def _dividend_rows(symbol: str, actions: List[dict]) -> List[Tuple[date, float, datetime]]:
    from ..data import pit
    out = []
    for a in actions:
        if a.get("kind") != "cash_dividend":
            continue
        amt = a.get("cash_amount_split_adj")
        try:
            amt = float(amt)
        except (TypeError, ValueError):
            amt = None
        if amt is None or not math.isfinite(amt) or amt <= 0:
            raise BacktestDataError(f"{symbol}: the dividend with ex-date {a.get('ex_date')} has no usable cash "
                                    "amount on the split basis — the backtest cannot book it.")
        ex = date.fromisoformat(str(a["ex_date"])[:10])
        known = _ts(a["known_at"]) if a.get("known_at") else pit.action_known_at(ex)
        out.append((ex, amt, known))
    out.sort()
    return out


# --- one simulated paper account -----------------------------------------------------------------

class _Plan:
    """A decision taken at a close, executed at the next execution phase."""

    def __init__(self, targets: Optional[Dict[str, float]], force_flat: set, adv: Dict[str, Optional[float]],
                 day: date, reason: str):
        self.targets = targets
        self.force_flat = set(force_flat)
        self.adv = adv
        self.day = day
        self.reason = reason


class _Sim:
    def __init__(self, *, store, params: dict, instruments: Dict[str, _Instrument], calendar: str,
                 warnings: _Warnings, limit_participation: bool, stats: dict):
        from strategy_platform.execution.paper_broker import PaperBroker
        self.store = store
        self.params = params
        self.instruments = instruments
        self.calendar = calendar
        self.warnings = warnings
        self.limit = limit_participation
        self.stats = stats
        self.cut_order_ids: set = set()       # orders sent after a participation cut (review 2026-10-04)
        self.now: Optional[datetime] = None
        self.day: Optional[date] = None
        self.phase = "open"
        self.broker = PaperBroker(store, self._quote, clock=lambda: self.now, dividends_fn=self._dividends,
                                  params_fn=lambda: self.params, bars_fn=self._bars)

    # broker callbacks
    def _quote(self, symbol: str):
        from strategy_platform.core.types import Quote
        inst = self.instruments.get(symbol)
        if inst is None:
            return None
        p = inst.price(self.day, self.phase, self.now, self.calendar)
        if p is None:
            return None
        return Quote(symbol=symbol, price=p, prev_close=inst.prev_close(self.day), ts=_iso(self.now),
                     delayed=False, stale=False)

    def _dividends(self, symbol: str, since: date):
        inst = self.instruments.get(symbol)
        return [] if inst is None else inst.dividends_since(since, self.now)

    def _bars(self, symbol: str):
        inst = self.instruments.get(symbol)
        return [] if inst is None else inst.bars_before(self.day)

    # phases
    def set_phase(self, day: date, phase: str, now: datetime) -> None:
        self.day, self.phase, self.now = day, phase, now

    def open_account(self) -> None:
        self.broker.ensure_initialized(now=self.now)

    def accruals(self) -> None:
        out = self.broker.daily_accruals(now=self.now)
        if out.get("paused"):
            self.warnings.count("accrual_paused", None, self.day, "Borrow fees waited {count} time(s) for a quote "
                                "of a held short (booked at the next quote).")

    def margin(self) -> None:
        from strategy_platform.execution.paper_broker import StateUnavailable
        try:
            self.broker.check_margin(now=self.now)
        except StateUnavailable:
            pass                    # valuation paused: counted by the snapshot / execution

    def held(self) -> Dict[str, float]:
        return {p["symbol"]: float(p["qty"]) for p in self.store.get_positions()}

    def position_values(self, held: Dict[str, float]) -> Dict[str, float]:
        """{symbol: qty x close quote} of the held positions (SPEC §11.1 recording; read-only — the same
        quote function the broker valued the snapshot with)."""
        out = {}
        for s in sorted(held):
            if held[s] != 0:
                q = self._quote(s)
                out[s] = held[s] * float(q.price)
        return out

    def snapshot(self) -> Optional[dict]:
        from strategy_platform.execution.paper_broker import ValuationPaused
        try:
            return self.broker.snapshot_equity(now=self.now)
        except ValuationPaused as ex:
            for s in ex.symbols:
                self.warnings.count("valuation_paused", s, self.day, "No price for held {symbol} on {count} "
                                    "day(s): the account could not be valued (equity left empty, no trading)."
                                    .replace("{symbol}", s))
            return None

    def execute(self, plan: Optional[_Plan]) -> None:
        if plan is None:
            return
        from strategy_platform.execution.paper_broker import StateUnavailable, round_qty
        try:
            equity = self.broker.account(now=self.now).equity
        except StateUnavailable:
            self.warnings.count("execution_skipped", None, self.day, "Orders of {count} execution(s) were skipped "
                                "because a held position had no price.")
            return
        held = self.held()
        weights = {}
        if plan.targets is not None:
            for s in set(plan.targets) | set(held):
                weights[s] = float(plan.targets.get(s, 0.0))
        for s in plan.force_flat:
            weights[s] = 0.0
        work = []
        for s in sorted(weights):
            w, h = weights[s], held.get(s, 0.0)
            q = self._quote(s)
            if q is None:
                if w != 0.0 or h != 0.0:
                    self.warnings.count("no_quote", s, self.day, "No execution price for " + s +
                                        " on {count} day(s): its order was not placed.")
                continue
            raw = w * equity / q.price
            desired = round_qty(s, abs(raw)) * (1.0 if raw >= 0 else -1.0)
            if abs(desired - h) < 1e-9:
                continue
            reducing = desired == 0 or (desired * h > 0 and abs(desired) < abs(h))
            work.append((0 if reducing else 1, s, w))
        for _rank, s, w in sorted(work):
            try:
                equity = self.broker.account(now=self.now).equity
            except StateUnavailable:
                return
            q = self._quote(s)
            h = self.held().get(s, 0.0)
            raw = w * equity / q.price
            desired = round_qty(s, abs(raw)) * (1.0 if raw >= 0 else -1.0)
            delta = desired - h
            if abs(delta) < 1e-9:
                continue
            send = w
            cut = False
            if self.limit and not self.instruments[s].crypto:     # crypto: no limit (user, 2026-10-03)
                adv = plan.adv.get(s)
                if adv is None:
                    self.warnings.count("missing_volume", s, self.day, "No complete 20-day volume for " + s +
                                        ": its order was refused on {count} day(s) (no default volume).")
                    self.stats["missing_volume_refusals"] += 1
                    continue
                cap = PARTICIPATION_RATE * adv
                notional = abs(delta) * q.price
                if notional > cap * (1.0 + 1e-12):
                    allowed = round_qty(s, cap / q.price)
                    final = h + math.copysign(allowed, delta)
                    dropped = (abs(delta) - allowed) * q.price
                    self.warnings.count("participation_cut", s, self.day, "Orders in " + s + " were cut to 1 % of "
                                        "the 20-day average dollar volume on {count} day(s) (excess dropped).",
                                        dropped_notional=dropped)
                    self.stats["participation_cuts"] += 1
                    cut = True
                    self.stats["dropped_notional"] += dropped
                    if abs(final - h) < 1e-9:
                        continue
                    send = final * q.price / equity
            results = self.broker.target(s, send, ORIGIN, now=self.now, rationale=plan.reason)
            if cut:
                self.cut_order_ids.update(r.order_id for r in results if r.order_id is not None)
            for r in results:
                if r.order_id is None and r.reject_reason:
                    self.warnings.count("order_skipped", s, self.day, "Order for " + s + " not placed on {count} "
                                        "day(s): " + r.reject_reason + ".")


def _all_rows(store, kind: str) -> List[dict]:
    """Every order / ledger / event row oldest first (memstore or fund.store)."""
    if kind == "orders":
        fn = getattr(store, "all_orders", None)
        return fn() if fn else sorted(store.list_orders(limit=100000), key=lambda o: o["id"])
    if kind == "ledger":
        fn = getattr(store, "all_cash_entries", None)
        return fn() if fn else sorted(store.cash_entries(limit=100000), key=lambda r: r["id"])
    fn = getattr(store, "all_events", None)
    return fn() if fn else list(store.events_after(0, limit=500))


def _crypto_last_session(rows: List[dict]) -> date:
    """Last NYSE session whose close still sees a crypto bar known < 24 h before it."""
    from ..core import calendars
    k = max(_crypto_known(r) for r in rows)
    limit = k + CRYPTO_QUOTE_MAX_AGE
    d = limit.astimezone(calendars.NY).date()
    for _ in range(20):
        b = calendars.session_bounds(d)
        if b is not None and b[1] < limit:
            return d
        d -= timedelta(days=1)
    raise BacktestDataError("No NYSE session found near the last crypto bar.")


def _symbol_pnl(marks: List[Tuple[datetime, str, Dict[str, float]]], ledger: List[dict], fills: List[dict],
                params: dict) -> dict:
    """Per (valued day, symbol) P&L rows (module docstring, SPEC §11.1) from the valued close marks, the
    cash ledger and the fills. Recording only: reads finished rows, changes nothing."""
    from strategy_platform.core import params as fund_params
    rows_ts = [(_ts(r["ts"]), r) for r in ledger]
    fill_ts = [(_ts(f["ts"]), f) for f in fills if f.get("ts")]
    out_rows: List[list] = []
    prev: Dict[str, float] = {}
    li = fi = 0
    for t, day, mv in marks:
        acc: Dict[str, Dict[str, float]] = {}

        def bucket(sym: str) -> Dict[str, float]:
            return acc.setdefault(sym, {"fill": 0.0, "dividends": 0.0, "fees": 0.0, "slippage": 0.0,
                                        "borrow": 0.0, "funding": 0.0})
        while li < len(rows_ts) and rows_ts[li][0] <= t:
            r = rows_ts[li][1]
            li += 1
            kind, amount, sym = r.get("kind"), float(r.get("amount") or 0.0), r.get("symbol")
            if not sym:
                if kind in CAPITAL_FLOW_KINDS:
                    continue
                sym = CASH_SYMBOL
            b = bucket(sym)
            if kind == "fill":
                b["fill"] += amount
            elif kind in ("dividend", "dividend_short"):
                b["dividends"] += amount
            elif kind in ("fee", "liquidation_fee"):
                b["fees"] += amount
            elif kind == "borrow_fee":
                b["borrow"] += amount
            elif kind == "funding":
                b["funding"] += amount
            else:
                b["fill"] += amount          # any other symbol cash flow is price P&L of that symbol
        while fi < len(fill_ts) and fill_ts[fi][0] <= t:
            f = fill_ts[fi][1]
            fi += 1
            fp, qty = float(f["fill_price"]), abs(float(f["qty"]))
            h = fund_params.fees_bps_for(f["symbol"], params) / 2.0 / 10000.0
            slip = qty * (fp - fp / (1.0 + h)) if f.get("side") in ("buy", "cover") else qty * (fp / (1.0 - h) - fp)
            bucket(f["symbol"])["slippage"] += slip
        for sym in sorted(set(acc) | set(mv) | set(prev)):
            b = acc.get(sym)
            now_v, was_v = mv.get(sym, 0.0), prev.get(sym, 0.0)
            if b is None and now_v == 0.0 and was_v == 0.0:
                continue
            b = b or {"fill": 0.0, "dividends": 0.0, "fees": 0.0, "slippage": 0.0, "borrow": 0.0, "funding": 0.0}
            mtm = now_v - was_v + b["fill"]
            pnl = mtm + b["dividends"] + b["fees"] + b["borrow"] + b["funding"]
            out_rows.append([day, sym, pnl, mtm, b["dividends"], b["fees"], b["slippage"], b["borrow"],
                             b["funding"], now_v])
        prev = mv
    return {"fields": list(SYMBOL_PNL_FIELDS), "rows": out_rows}


def history_from_date(history_from) -> date:
    """Data load start of a run without an explicit start (SPEC §8.5, fix 2026-10-09): None / "2016" ->
    the seam 2016-01-04 (Alpaca history, no Tiingo key), "2000" -> 2000-01-03 (Tiingo before the seam)."""
    from ..data import seam as seam_mod
    value = DEFAULT_HISTORY_FROM if history_from is None else history_from
    if value == "2016":
        return seam_mod.SEAM_DATE
    if value == "2000":
        return seam_mod.EARLIEST_START
    raise InvalidRequest(f"history_from must be one of {', '.join(HISTORY_FROM_CHOICES)}.")


def _load_start(calendar: str, start_d: Optional[date], warmup: int, history_from=None,
                clamp_warmup: bool = False) -> date:
    """First day the run loads. No explicit start -> ``history_from_date(history_from)`` (the first
    NYSE session on/after it; trading starts after the warm-up). An explicit start -> start minus
    ``warmup`` sessions (NYSE) / days (UTC); reaching before 2000-01-03 (the earliest data) ->
    ``InvalidRequest`` with a hint, or with ``clamp_warmup`` (stress episodes) 2000-01-03."""
    from ..core import calendars
    from ..data import seam as seam_mod
    earliest = seam_mod.EARLIEST_START
    first_cov = max(date(min(calendars.CALENDAR_YEARS), 1, 1), history_from_date(history_from))
    too_early = (f"The backtest needs {warmup} sessions of history before its start ({start_d}), but the "
                 f"earliest data is {earliest.isoformat()} — use 'From 2000' or a later start.")
    if calendar == "nyse":
        try:
            if start_d is None:
                return calendars.next_trading_day(first_cov - timedelta(days=1))
            d = start_d if calendars.is_trading_day(start_d) else calendars.next_trading_day(start_d)
            for _ in range(warmup):
                if d <= earliest:
                    if clamp_warmup:
                        return earliest
                    raise InvalidRequest(too_early)
                d = calendars.previous_trading_day(d)
            return d
        except calendars.CalendarUnavailable as ex:
            raise InvalidRequest(f"The backtest needs {warmup} sessions of history before its start "
                                 f"({start_d}): {ex.user_message}") from None
    if start_d is None:
        return first_cov
    load_start = start_d - timedelta(days=warmup)
    if load_start < earliest:
        if clamp_warmup:
            return earliest
        raise InvalidRequest(too_early.replace(" sessions ", " days "))
    return load_start


def needs_pre_seam_equity(spec: dict, start=None, benchmark: Optional[str] = None, history_from=None) -> bool:
    """True when a run of ``spec`` loads equity data before the seam (load start = explicit start incl.
    warm-up, else ``history_from``; < 2016-01-04) — then the Tiingo key is required (user decision
    2026-10-06, history_from 2026-10-09; checked by the API at submit time). An invalid
    spec/start/history_from -> False: the engine reports it on the run card."""
    from ..core.instruments import instrument
    from ..data import seam as seam_mod
    from ..strategy import schema, universe, validator
    try:
        start_d = _as_date(start, "start")
        if start_d is None and history_from_date(history_from) >= seam_mod.SEAM_DATE:
            return False                      # default: loading from the seam
        res = validator.validate(spec)
        if not res.ok:
            return False
        full = schema.with_defaults(spec)
        symbols = universe.resolve(full["universe"])
        classes = {s: instrument(s).asset_class for s in symbols}
        calendar = "utc" if all(c == "crypto" for c in classes.values()) else "nyse"
        vol_target = (full["risk"] or {}).get("vol_target_annual")
        warmup = max(int(res.facts["max_lookback"]), ADV_WINDOW,
                     (VOL_TARGET_LOOKBACK + 1) if vol_target else 0) + WARMUP_MARGIN
        if _load_start(calendar, start_d, warmup, history_from) >= seam_mod.SEAM_DATE:
            return False
        bench = instrument(str(benchmark or spec.get("benchmark") or DEFAULT_BENCHMARK)).symbol
    except (InvalidRequest, ValueError, KeyError, TypeError):
        return False
    return any(c == "equity" for c in classes.values()) or instrument(bench).asset_class == "equity"


def run(spec: dict, *, start=None, end=None, capital=DEFAULT_CAPITAL, benchmark: Optional[str] = None,
        history_from: Optional[str] = None,
        progress: Optional[Callable[[str, float, str], None]] = None, cancel: Optional[Callable[[], bool]] = None,
        data=None, cost_overrides: Optional[Dict[str, float]] = None, clamp_warmup: bool = False,
        holdout_access: bool = False, record_symbol_pnl: bool = True,
        _store_factory: Optional[Callable[[], Any]] = None) -> BacktestResult:
    """Simulate ``spec`` (validated here) — see the module docstring.
    ``_store_factory`` (tests only): the strategy account's store (default a
    new ``MemStore``) — the parity test passes ``fund.store`` on a temp DB."""
    import numpy as np
    from strategy_platform.core import params as fund_params
    from ..core import calendars
    from ..core.instruments import instrument
    from ..data import seam as seam_mod
    from ..data.providers import tiingo as tiingo_mod
    from ..strategy import evaluate as ev
    from ..strategy import portfolio, schema, universe, validator
    from ..strategy.panel import Panel
    from .memstore import MemStore

    def emit(phase: str, pct: float, message: str) -> None:
        if progress is not None:
            progress(phase, round(max(0.0, min(100.0, float(pct))), 2), message)

    def check_cancel() -> None:
        if cancel is not None and cancel():
            raise BacktestCancelled()

    # --- request ---------------------------------------------------------------------------
    res = validator.validate(spec)
    if not res.ok:
        first = res.errors[0]
        raise InvalidRequest(f"The strategy is not valid ({first['path']}: {first['message']}).")
    full = schema.with_defaults(spec)
    spec_hash = schema.content_hash(spec)
    if isinstance(capital, bool):
        raise InvalidRequest("capital must be a number.")
    try:
        cap = float(capital)
    except (TypeError, ValueError):
        raise InvalidRequest("capital must be a number.") from None
    if not math.isfinite(cap) or not CAPITAL_MIN <= cap <= CAPITAL_MAX:
        raise InvalidRequest(f"capital must be between {CAPITAL_MIN:,.0f} and {CAPITAL_MAX:,.0f}.")
    bench_raw = benchmark if benchmark is not None else (spec.get("benchmark") or DEFAULT_BENCHMARK)
    try:
        bsym = instrument(str(bench_raw)).symbol
    except (TypeError, ValueError):
        raise InvalidRequest(f"benchmark {bench_raw!r} is not a valid symbol.") from None
    bclass = instrument(bsym).asset_class
    symbols = universe.resolve(full["universe"])
    classes = {s: instrument(s).asset_class for s in symbols}
    calendar = "utc" if all(c == "crypto" for c in classes.values()) else "nyse"
    timing = full["execution"]["timing"]
    risk = full["risk"] or {}
    pf = full["portfolio"]
    vol_target = risk.get("vol_target_annual")
    warmup = max(int(res.facts["max_lookback"]), ADV_WINDOW,
                 (VOL_TARGET_LOOKBACK + 1) if vol_target else 0) + WARMUP_MARGIN
    start_d, end_d = _as_date(start, "start"), _as_date(end, "end")
    if start_d is not None and start_d < seam_mod.EARLIEST_START:
        raise InvalidRequest(f"start must be on or after {seam_mod.EARLIEST_START.isoformat()}.")
    if start_d is not None and end_d is not None and start_d >= end_d:
        raise InvalidRequest("start must lie before end.")
    history_from_date(history_from)           # validated even with an explicit start (then only recorded)
    # holdout lock (stage 4, SPEC §9.1): normal runs never load or simulate the locked window
    from ..validation import families
    lock = families.lock_for_kind(families.universe_kind_of_classes(classes.values()))
    if not holdout_access:
        for what, d in (("end", end_d), ("start", start_d)):
            if d is not None and d >= lock["start"]:
                raise families.HoldoutWindowViolation(lock["start"], what)
    load_end = end_d if (end_d is not None or holdout_access) else lock["training_end"]
    load_start = _load_start(calendar, start_d, warmup, history_from, clamp_warmup)
    costs = full["costs"]
    params = fund_params.defaults()
    params["starting_capital"] = cap
    params["short_exposure_max_pct"] = 100.0 * float(pf["gross_exposure"])
    warnings = _Warnings()
    for w in res.warnings:
        warnings.add(w["code"], w["message"], path=w.get("path"))
    if load_start < seam_mod.SEAM_DATE and "equity" in classes.values() and \
            any(w["code"] == "absolute_price_level" for w in res.warnings):
        warnings.add("absolute_level_two_sources", "The strategy compares absolute price levels and the data "
                     f"starts before {seam_mod.SEAM_DATE.isoformat()}: equity levels before that date are Tiingo "
                     "prices scaled by the seam factor (and the adjusted levels are rebased by later dividends) — "
                     "level thresholds can trigger on different days than they would have (SPEC §8.2).")
    if costs.get("model") == "custom":
        if costs.get("fee_bps") is None or costs.get("slippage_bps") is None:
            raise InvalidRequest("costs.model 'custom' needs fee_bps and slippage_bps.")
        total = float(costs["fee_bps"]) + float(costs["slippage_bps"])
        params["fees_bps"] = params["crypto_fees_bps"] = total
        if float(costs["fee_bps"]) != float(costs["slippage_bps"]):
            warnings.add("custom_costs_split", f"Custom costs {costs['fee_bps']:g} bps fee + "
                         f"{costs['slippage_bps']:g} bps slippage are applied as {total:g} bps per fill, booked "
                         "half as slippage in the price and half as a fee (the paper broker's cost model).")
    if cost_overrides is not None:
        # stress hook (3d, user decision 2026-10-04): per-class per-fill cost for THIS run only
        if not isinstance(cost_overrides, dict) or not cost_overrides:
            raise InvalidRequest("cost_overrides must map fees_bps / crypto_fees_bps to numbers.")
        for k, v in sorted(cost_overrides.items()):
            if k not in COST_OVERRIDE_KEYS:
                raise InvalidRequest(f"cost_overrides: unknown key {k!r}.")
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)) \
                    or not 0.0 <= float(v) <= COST_OVERRIDE_MAX_BPS:
                raise InvalidRequest(f"cost_overrides.{k} must be 0..{COST_OVERRIDE_MAX_BPS:g} bps.")
            params[k] = float(v)
        warnings.add("cost_override", "Per-fill costs overridden for this run (stress test): " +
                     ", ".join(f"{k} {params[k]:g} bps" for k in COST_OVERRIDE_KEYS) +
                     " — booked half as slippage in the price and half as a fee.")

    # --- data ---------------------------------------------------------------------------------
    if any(c == "crypto" for c in classes.values()):
        warnings.add("crypto_no_participation_limit", "No volume limit for crypto (user decision 2026-10-03) — "
                     "fills assume the order size is always tradable.")
    loader = data if data is not None else DataLoader()
    needed = list(symbols) + ([bsym] if bsym not in classes else [])
    loaded: Dict[str, dict] = {}
    # parallel symbol loading (SPEC §10.1): results assembled in ``needed`` order -> identical to sequential
    for s, (e, no_volume) in zip(needed, _load_symbols(loader, needed, classes, bclass, load_start, load_end,
                                                        emit=emit, check_cancel=check_cancel,
                                                        unknown_symbol=tiingo_mod.UnknownSymbol)):
        if no_volume:
            warnings.add("no_volume_source", f"Tiingo does not know {s}: no consolidated volume, so its "
                         "orders are refused (participation limit).", symbol=s)
        for part in e.values():
            if part is not None:
                for w in (part[1] or {}).get("warnings") or []:
                    warnings.add(w.get("code", "data_warning"), w.get("message", ""), symbol=w.get("symbol", s))
        loaded[s] = e
    emit("data", 100.0, "data loaded")
    # data-coverage guard (user decision 2026-10-06, after IEX history silently started 2020-07): when
    # more than LATE_START_SHARE of the universe starts more than LATE_START_DAYS after the requested
    # load start, say so loudly — a young ETF is normal, most of the universe being "young" is a data problem
    late = []
    for s in symbols:
        e = loaded[s]
        rows = e["bars"][0] if classes[s] == "crypto" else e["all"][0]
        first = (_utc_date(rows[0]["ts"]) if classes[s] == "crypto" else _ny_date(rows[0]["ts"])) if rows else None
        if first is None or (first - load_start).days > LATE_START_DAYS:
            late.append((s, first.isoformat() if first else None))
    if symbols and len(late) / len(symbols) > LATE_START_SHARE:
        warnings.add("many_late_starts",
                     f"{len(late)} of {len(symbols)} symbols have no data until more than {LATE_START_DAYS} days "
                     f"after the requested start {load_start.isoformat()} (" +
                     ", ".join(f"{s} {d or 'none'}" for s, d in late[:10]) + (", …" if len(late) > 10 else "") +
                     ") — check the data source's history before trusting the early years of this run.",
                     symbols=[s for s, _ in late])

    present = []
    for s in symbols:
        e = loaded[s]
        rows = e["bars"][0] if classes[s] == "crypto" else e["all"][0]
        if not rows:
            warnings.add("symbol_excluded", f"No daily bars for {s} in the range — excluded.", symbol=s)
            continue
        if classes[s] == "equity" and not e["split"][0]:
            raise BacktestDataError(f"{s}: the split-adjusted price series is missing while the adjusted one exists.")
        present.append(s)
    if not present:
        raise InvalidRequest("None of the strategy's symbols has daily bars in the range.")
    be = loaded[bsym]
    if not (be["bars"][0] if bclass == "crypto" else be["split"][0]):
        raise InvalidRequest(f"No daily bars for the benchmark {bsym} in the range.")

    # --- range end: the last day every required source covers -----------------------------------
    lasts: List[Tuple[date, str, str]] = []
    for s in present + ([bsym] if bsym not in present else []):
        e = loaded[s]
        if instrument(s).asset_class == "crypto":
            rows, info = e["bars"]
            last = _utc_date(rows[-1]["ts"]) if calendar == "utc" else _crypto_last_session(rows)
            lasts.append((last, s, "crypto bars (" + ", ".join(info.get("sources") or []) + ")"))
            continue
        for key in ("all", "split"):
            if key in e and e[key][0]:
                rows, info = e[key]
                lasts.append((_ny_date(rows[-1]["ts"]), s, f"{key} price series (" +
                              ", ".join(info.get("sources") or []) + ")"))
        if e.get("volume") and e["volume"][0]:
            lasts.append((date.fromisoformat(str(e["volume"][0][-1]["date"])[:10]), s,
                          f"equity volume ({e['volume'][1].get('source', 'tiingo')})"))
    end_auto, lim_sym, lim_src = min(lasts)
    limited_by = {"date": end_auto.isoformat(), "symbol": lim_sym, "source": lim_src,
                  "provider": "tiingo" if ("volume" in lim_src or "tiingo)" in lim_src) else "alpaca"}
    if end_d is None:
        end_final = end_auto
        if not holdout_access and end_auto >= lock["training_end"]:
            # the lock, not the data, ends this run (range.holdout says so)
            end_final, limited_by = lock["training_end"], None
    else:
        end_eff = end_d if calendar == "utc" or calendars.is_trading_day(end_d) \
            else calendars.previous_trading_day(end_d)
        if end_eff > end_auto:
            raise InvalidRequest(f"The end date {end_d.isoformat()} lies after the last day with data from every "
                                 f"required source ({end_auto.isoformat()}: {lim_src} of {lim_sym}).")
        end_final = end_eff
    end_close = calendars.session_bounds(end_final)[1] if calendar == "nyse" else None

    def keep(rows: List[dict], cls: str) -> List[dict]:
        if cls == "crypto":
            if calendar == "utc":
                return [r for r in rows if _utc_date(r["ts"]) <= end_final]
            return [r for r in rows if _crypto_known(r) <= end_close]
        return [r for r in rows if _ny_date(r["ts"]) <= end_final]

    # --- panel + signal -----------------------------------------------------------------------
    sig_bars = {s: keep(loaded[s]["bars"][0] if classes[s] == "crypto" else loaded[s]["all"][0], classes[s])
                for s in present}
    try:
        panel = Panel.from_bars(sig_bars, symbols=present)
    except ValueError as ex:
        raise BacktestDataError(f"The price data cannot be aligned: {ex}") from None
    dates = panel.dates
    if start_d is None:
        has = np.isfinite(panel.fields["close"]).any(axis=1)
        if not has.any():
            raise InvalidRequest("No closing prices in the range.")
        i0 = int(np.argmax(has)) + warmup
    else:
        i0 = bisect.bisect_left(dates, start_d)
    i1 = bisect.bisect_right(dates, end_final) - 1
    if i0 >= len(dates) or i1 - i0 < 1:
        raise InvalidRequest(f"The range holds fewer than two days with data (the strategy needs {warmup} days of "
                             "history before its first day).")
    try:
        ev.check_cost(full, panel)
        ext = loader.ext_values(full, panel) if hasattr(loader, "ext_values") else None
        signal = ev.evaluate(full, panel, ext_values=ext)
    except ev.EvaluationError as ex:
        raise BacktestDataError(f"The signal could not be evaluated on the loaded data: {ex}") from None
    mask = portfolio.rebalance_mask(full, dates)

    insts: Dict[str, _Instrument] = {}
    for s in present + ([bsym] if bsym not in present else []):
        cls = instrument(s).asset_class
        e = loaded[s]
        inst = _Instrument(s, cls, keep(e["bars"][0] if cls == "crypto" else e["split"][0], cls))
        if cls == "equity":
            inst.dividends = _dividend_rows(s, e["actions"][0])
            if e.get("volume"):
                inst.volume_by_day = {}
                for r in e["volume"][0]:
                    v = r.get("dollar_volume_split")
                    if v is not None and math.isfinite(float(v)) and float(v) >= 0:
                        inst.volume_by_day[date.fromisoformat(str(r["date"])[:10])] = float(v)
        insts[s] = inst

    # --- strategy simulation ----------------------------------------------------------------------
    stats = {"participation_cuts": 0, "dropped_notional": 0.0, "missing_volume_refusals": 0, "stop_exits": 0,
             "take_profit_exits": 0, "rebalances": 0, "days": 0}
    store = _store_factory() if _store_factory is not None else MemStore(params)
    sim = _Sim(store=store, params=params, instruments=insts, calendar=calendar, warnings=warnings,
               limit_participation=True, stats=stats)
    days = dates[i0:i1 + 1]
    total = len(days)
    step = max(1, total // 100)
    series: Dict[str, List[Optional[float]]] = {k: [] for k in ("equity", "cash", "long_mv", "short_mv",
                                                                "drawdown_pct")}
    positions_out: List[dict] = []
    last_held: Optional[Dict[str, float]] = None
    close_times: List[datetime] = []
    marks_out: List[Tuple[datetime, str, Dict[str, float]]] = []     # SPEC §11.1: valued closes (recording only)
    plan: Optional[_Plan] = None
    # the decision block is shared with the live/paper runtime (SPEC §17.3, portfolio/decision.py)
    from ..portfolio.decision import DecisionState, decide
    dstate = DecisionState()
    last_rebalance = None
    for r in range(i0, -1, -1):
        if mask[r]:
            last_rebalance = r
            break

    def adv_for(s: str, i: int, day: date) -> Optional[float]:
        inst = insts.get(s)
        if inst is None or inst.crypto:          # crypto: no participation limit (user, 2026-10-03)
            return None
        return inst.adv_equity(dates, i)

    for k, day in enumerate(days):
        check_cancel()
        if k % step == 0:
            emit("simulate", 85.0 * k / total, f"simulating {day.isoformat()} ({k + 1}/{total})")
        i = i0 + k
        t_open, t_close = phase_times(day, calendar)
        sim.set_phase(day, "open", t_open)
        if k == 0:
            sim.open_account()
        sim.accruals()
        if timing == "next_open":
            sim.execute(plan)
        sim.margin()
        sim.set_phase(day, "close", t_close)
        sim.accruals()
        if timing == "next_close":
            sim.execute(plan)
        sim.margin()
        snap = sim.snapshot()
        for key in series:
            series[key].append(None if snap is None else float(snap[key]))
        held = sim.held()
        if snap is not None and record_symbol_pnl:
            marks_out.append((t_close, day.isoformat(), sim.position_values(held)))
        if held != last_held:
            positions_out.append({"date": day.isoformat(), "positions": {s: held[s] for s in sorted(held)}})
            last_held = held
        close_times.append(t_close)
        stats["days"] += 1
        # decision for the next execution, from data known at this close (shared code, SPEC §17.3)
        targets, force, reason = decide(
            full, panel, signal, mask, i, first=(k == 0), last_rebalance=last_rebalance, state=dstate,
            positions=store.get_positions() if (risk.get("stop_loss_pct") or risk.get("take_profit_pct")) else [],
            close_price=lambda s_, _d=day, _t=t_close: (insts[s_].price(_d, "close", _t, calendar)
                                                         if s_ in insts else None),
            drawdown_pct=None if snap is None else float(snap["drawdown_pct"]), held=set(held), calendar=calendar,
            day=day, warnings=warnings, stats=stats)
        syms = set(force) | ((set(targets) | set(held)) if targets is not None else set())
        plan = _Plan(targets, force, {s: adv_for(s, i, day) for s in sorted(syms)}, day, reason) \
            if (targets is not None or force) else None

    # --- benchmark (buy and hold, same costs and dividends) ----------------------------------------
    bench_cal = "nyse" if (calendar == "utc" and bclass == "equity") else calendar
    bdays = days if bench_cal == calendar else calendars.trading_days(days[0], days[-1])
    bwarn = _Warnings()
    bstats = dict(stats, participation_cuts=0, dropped_notional=0.0, missing_volume_refusals=0)
    bstore = MemStore(params)
    bsim = _Sim(store=bstore, params=params, instruments={bsym: insts[bsym]}, calendar=bench_cal, warnings=bwarn,
                limit_participation=False, stats=bstats)
    bpoints: List[Tuple[datetime, Optional[float]]] = []
    bplan: Optional[_Plan] = None
    btotal = max(1, len(bdays))
    for k, day in enumerate(bdays):
        check_cancel()
        if k % step == 0:
            emit("simulate", 85.0 + 15.0 * k / btotal, f"benchmark {bsym} {day.isoformat()}")
        t_open, t_close = phase_times(day, bench_cal)
        bsim.set_phase(day, "open", t_open)
        if k == 0:
            bsim.open_account()
        bsim.accruals()
        if timing == "next_open":
            bsim.execute(bplan)
            bplan = None if bsim.held() else bplan          # retried until the position exists
        bsim.margin()
        bsim.set_phase(day, "close", t_close)
        bsim.accruals()
        if timing == "next_close":
            bsim.execute(bplan)
            bplan = None if bsim.held() else bplan
        snap = bsim.snapshot()
        bpoints.append((t_close, None if snap is None else float(snap["equity"])))
        if k == 0:
            bplan = _Plan({bsym: 1.0}, set(), {}, day, "benchmark buy and hold")
    bench_series: List[Optional[float]] = []
    btimes = [t for t, _v in bpoints]
    for t in close_times:
        j = bisect.bisect_right(btimes, t) - 1
        bench_series.append(cap if j < 0 else bpoints[j][1])
    for item in bwarn.items():
        warnings.add("benchmark_" + item["code"], f"Benchmark {bsym}: " + item["message"], symbol=item.get("symbol"))
    emit("simulate", 100.0, "simulation finished")

    # --- result ---------------------------------------------------------------------------------
    orders = _all_rows(store, "orders")
    fills = [{"id": o["id"], "ts": o["filled_at"], "symbol": o["symbol"], "side": o["side"], "qty": o["qty"],
              "fill_price": o["fill_price"], "fees": o["fees"], "notional": o["notional"], "origin": o["origin"],
              "participation_cut": o["id"] in sim.cut_order_ids}
             for o in orders if o["status"] == "filled"]
    ledger_rows = _all_rows(store, "ledger")
    symbol_pnl = _symbol_pnl(marks_out, ledger_rows, fills, params) if record_symbol_pnl else None
    data_out: Dict[str, dict] = {}
    for s in needed:
        e = loaded[s]
        cls = instrument(s).asset_class
        role = "strategy+benchmark" if (s in classes and s == bsym) else ("strategy" if s in classes else "benchmark")
        entry: Dict[str, Any] = {"asset_class": cls, "role": role,
                                 "excluded": s in classes and s not in present}
        for key in ("all", "split", "bars"):
            if key in e:
                rows, info = e[key]
                entry[key] = dict(info, row_hash=_rows_hash(rows, BAR_KEYS))
        if "volume" in e:
            entry["volume"] = None if e["volume"] is None else dict(e["volume"][1],
                                                                   row_hash=_rows_hash(e["volume"][0], VOLUME_KEYS))
        if "actions" in e:
            rows, info = e["actions"]
            entry["corporate_actions"] = dict(info, rows=len(rows),
                                              dividends=sum(1 for a in rows if a.get("kind") == "cash_dividend"),
                                              row_hash=_rows_hash(rows, ACTION_KEYS))
        data_out[s] = entry
    result = BacktestResult(
        engine_version=ENGINE_VERSION, spec_hash=spec_hash,
        request={"start": None if start_d is None else start_d.isoformat(),
                 "end": None if end_d is None else end_d.isoformat(), "capital": cap,
                 "benchmark": benchmark, "history_from": history_from},
        range={"start": days[0].isoformat(), "end": days[-1].isoformat(), "calendar": calendar,
               "load_start": load_start.isoformat(), "first_data": dates[0].isoformat(),
               "end_limited_by": limited_by, "end_explicit": end_d is not None, "warmup_sessions": warmup,
               "timing": timing,
               "holdout": {"universe_kind": lock["universe_kind"], "start": lock["start"].isoformat(),
                           "end": lock["end"].isoformat(), "training_end": lock["training_end"].isoformat(),
                           "access": bool(holdout_access)}},
        dates=[d.isoformat() for d in days],
        equity=series["equity"], cash=series["cash"], long_mv=series["long_mv"], short_mv=series["short_mv"],
        drawdown_pct=series["drawdown_pct"], benchmark=bench_series,
        benchmark_info={"symbol": bsym, "calendar": bench_cal, "orders": _all_rows(bstore, "orders"),
                        "ledger": _all_rows(bstore, "ledger")},
        positions=positions_out, orders=orders, fills=fills, ledger=ledger_rows,
        events=_all_rows(store, "events"), warnings=warnings.items(), data=data_out,
        broker_params=params,
        costs={"model": costs.get("model"), "fees_bps": params["fees_bps"],
               "crypto_fees_bps": params["crypto_fees_bps"], "requested": dict(costs),
               "overrides": None if cost_overrides is None else dict(cost_overrides)},
        stats=stats, symbol_pnl=symbol_pnl)
    # 3d hook (SPEC §8.10): inputs of metrics.signal_quality / capacity that are not part of the
    # result's fields. A plain attribute — NOT a dataclass field, so ``to_dict()`` (and the run card)
    # stay unchanged; nothing in the simulation reads it.
    result.analysis = {
        "dates": [d.isoformat() for d in dates], "symbols": list(panel.symbols), "signal": signal,
        "close_all": panel.fields["close"], "i0": i0, "i1": i1, "calendar": calendar,
        "dollar_volume": {s: dict((d.isoformat(), v) for d, v in sorted(inst.volume_by_day.items()))
                          for s, inst in insts.items() if inst.volume_by_day is not None and s in present},
        "crypto": sorted(s for s in present if classes[s] == "crypto"),
    }
    return result
