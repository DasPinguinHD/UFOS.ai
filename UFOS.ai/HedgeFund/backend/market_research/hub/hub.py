"""MarketDataHub — the ONLY owner of Alpaca stream connections (SPEC §7.6).

One connection per endpoint per account (stocks IEX, crypto, news), shared by
every subscriber (``/research/ws`` connections = views/tabs, alerts from MR7,
later the Strategy Platform live runtime): the hub keeps the union of all
topics, subscribes each symbol once and fans the messages out.

- Demand (lazy start/stop): streams run only while ``views + alerts > 0``
  (a view = a ``/research/ws`` connection whose last ``visible`` is true) and
  close ``HUB_LINGER_SECONDS`` after the demand (or the endpoint's last
  symbol) is gone.
- Stocks: at most ``STOCK_STREAM_SYMBOL_LIMIT`` symbols (priority: views'
  topics in subscribe order, then alerts); every further symbol gets
  ``symbol_status {live: false, reason: "Stream limit: 30 symbols — X is not live."}``
  (Q10, no snapshot fallback).
- Live candles (``candles.py``): one builder per topic, ≤
  ``LIVE_CANDLE_MAX_PER_SECOND`` messages per topic, trades coalesced to ≤
  ``TRADE_MAX_PER_SECOND`` per symbol. Seeds and SIP refreshes are REST calls
  through the REST side's ``DataService`` (``MinuteLoader``) on the Research
  pool inside ``http_gate.research_priority()`` (SPEC §5.1, §7.5).
- IEX tail → SIP: every ``SIP_TAIL_REFRESH_SECONDS`` the newly released SIP
  minutes of ≤ ``SIP_TAIL_MAX_SYMBOLS`` symbols with an open intraday chart
  are fetched; completed buckets go out as ``sip_replace`` (chunked ≤ 500
  bars per message). A failure keeps the tail candles marked as tail and puts
  the reason into ``symbol_status``.
- MR4: ``watchlist:<id>`` topics expand to ``trades:`` of the list's symbols
  (``watchlist_symbols`` resolver, list order = priority position of the
  topic); ``quote_state(symbol)`` + ``last_trades`` serve the watchlist quotes
  (stream values only, never a REST snapshot).
- ``run()`` is the supervised task ``market_data_hub``: any unexpected error
  ends it (all connections closed) and the Research supervisor restarts it —
  never the paper engine.

Part of UFOS.ai, a proof-of-concept portfolio project; research only.
"""
from __future__ import annotations

import asyncio
import logging
import time as _time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Callable, Deque, Dict, List, Optional

from strategy_platform.core.errors import UserFacingError
from strategy_platform.core.instruments import instrument

from .. import limits
from ..bars import aggregate as agg
from ..executor import run_blocking
from . import protocol
from .aio import within
from .alpaca_stream import HubConfig, StreamConnection, default_keys
from .candles import INTRADAY_MINUTES, LiveCandle, RateLimiter, iso
from .demand import Subscriber, rank_symbols

logger = logging.getLogger(__name__)

DEMAND_NOTE = "Live streams run only while a Research view is visible or an alert is active."
NOT_RUNNING_MESSAGE = "Live market data is not running (see the Research status)."
NOT_SUBSCRIBED_MESSAGE = "Not live — live quotes run only while this watchlist is shown in a Research view."
ENDPOINT_CLASS = {"stocks": "equity", "crypto": "crypto"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def chunk_bars(base: dict, bars: List[dict], size: int = limits.WS_MAX_BARS_PER_MESSAGE) -> List[dict]:
    """``base`` + ``bars`` split into messages of ≤ ``size`` bars with ``part``/``parts`` (1-based, SPEC §4.3)."""
    size = max(1, int(size))
    chunks = [bars[i:i + size] for i in range(0, len(bars), size)] or [[]]
    return [dict(base, bars=c, part=k, parts=len(chunks)) for k, c in enumerate(chunks, 1)]


async def default_probe() -> bool:
    """network.probe("alpaca") on the Research pool under research priority (SPEC §7.6, review L8)."""
    from strategy_platform.data import http_gate, network
    with http_gate.research_priority():
        return await run_blocking(network.probe, "alpaca")


def default_next_probe_in() -> float:
    from strategy_platform.data import network
    return network.next_probe_in("alpaca")


def _default_transport():
    from .transport import WebsocketsTransport
    return WebsocketsTransport()


class MarketDataHub:
    def __init__(self, *, transport=None, keys: Optional[Callable[[], tuple]] = None, probe=None,
                 next_probe_in: Optional[Callable[[], float]] = None, data: Optional[Callable[[], object]] = None,
                 config: Optional[HubConfig] = None, monotonic: Callable[[], float] = _time.monotonic,
                 now: Callable[[], datetime] = utc_now, status_fn: Optional[Callable[[], dict]] = None):
        self.config = config or HubConfig()
        self._transport = transport
        self._keys = keys or default_keys
        self._probe = probe or default_probe
        self._next_probe_in = next_probe_in or default_next_probe_in
        self._data_fn = data
        self._mono, self._now = monotonic, now
        self.status_fn = status_fn
        # MR6: (item, symbols) -> public item | None (repeat); set by ResearchService (news.NewsService)
        self.news_hook: Optional[Callable[[dict, List[str]], Optional[dict]]] = None
        # MR7: (endpoint, trade) for EVERY stream trade of a streaming symbol (alerts.AlertEngine.on_trade)
        self.trade_hook: Optional[Callable[[str, dict], None]] = None
        self.subscribers: List[Subscriber] = []
        self.running = False
        self.stop_reason: Optional[str] = None
        self.conns: Dict[str, StreamConnection] = self._make_conns()
        self.candles: Dict[str, LiveCandle] = {}
        self._finals: Dict[str, Deque[dict]] = {}
        self._candle_rl = RateLimiter(limits.LIVE_CANDLE_MAX_PER_SECOND, monotonic)
        self._trade_rl = RateLimiter(limits.TRADE_MAX_PER_SECOND, monotonic)
        self._trades: Dict[str, dict] = {}
        self._limited: set = set()
        self._stocks: List[str] = []
        self._counts = None
        self._status_sent: Dict[tuple, tuple] = {}
        self._sip_through: Dict[str, datetime] = {}
        self._sip_errors: Dict[str, str] = {}
        self._seed_due: Dict[str, float] = {}
        self._seed_wake: Optional[asyncio.Event] = None
        self._conn_live: Dict[str, bool] = {}
        self._feeds_last = None
        self.ws_clients = 0                     # open /research/ws connections (ws.py)
        # MR4: watchlist:<id> topics -> symbols (set by ResearchService: watchlists.Workspace.symbols_for)
        self.watchlist_symbols: Optional[Callable[[str], List[str]]] = None
        self.last_trades: Dict[str, dict] = {}   # latest stream trade per live symbol (watchlist quotes, MR4)

    def _make_conns(self) -> Dict[str, StreamConnection]:
        return {e: StreamConnection(e, self, transport=self._transport or _LazyTransport(), keys=self._keys,
                                    probe=self._probe, next_probe_in=self._next_probe_in, config=self.config,
                                    monotonic=self._mono, now=self._now)
                for e in protocol.ENDPOINT_IDS}

    # --- subscribers ------------------------------------------------------------------------
    def register(self, sub: Subscriber) -> None:
        if sub not in self.subscribers:
            self.subscribers.append(sub)
        self.refresh()

    def unregister(self, sub: Subscriber) -> None:
        if sub in self.subscribers:
            self.subscribers.remove(sub)
        for k in [k for k in self._status_sent if k[0] == sub.seq]:
            del self._status_sent[k]
        self.refresh()

    def refresh(self) -> None:
        """Call after a subscriber's topics or ``active`` changed."""
        self._recompute()

    def _subs_with(self, raw: str):
        return [s for s in self.subscribers if s.wants(raw)]

    def _expand_watchlists(self) -> None:
        """watchlist:<id> topics -> the list's symbols (MR4); a failing resolver expands to nothing."""
        resolver = self.watchlist_symbols
        for s in self.subscribers:
            exp = {}
            for raw, t in s.topics.items():
                if t.kind != "watchlist":
                    continue
                syms: List[str] = []
                if resolver is not None:
                    try:
                        syms = [str(x) for x in (resolver(t.list_id) or [])]
                    except Exception:   # noqa: BLE001 — never into the hub
                        logger.exception("Research hub: expanding %s failed", raw)
                exp[raw] = syms
            s.expanded = exp

    def _send(self, subs, msg: dict, key: Optional[str] = None) -> None:
        for s in subs:
            try:
                s.deliver(msg, key)
            except Exception:   # noqa: BLE001 — one broken subscriber never stops the others
                logger.exception("Research hub: delivering %s to a subscriber failed", msg.get("type"))

    def _recompute(self) -> None:
        self._expand_watchlists()
        subs = self.subscribers
        active = [s for s in subs if s.active]
        counts = (sum(1 for s in active if s.kind == "view"), sum(1 for s in active if s.kind == "alert"))
        demand_changed = counts != self._counts
        self._counts = counts
        wanted = sum(counts) > 0
        stocks = rank_symbols(subs, "equity")
        limit = limits.STOCK_STREAM_SYMBOL_LIMIT
        self._stocks, self._limited = stocks, set(stocks[limit:])
        crypto = rank_symbols(subs, "crypto")
        news = rank_symbols(subs, "equity", ("news",)) + rank_symbols(subs, "crypto", ("news",))
        self.conns["stocks"].update(stocks[:limit], wanted, demand_changed)
        self.conns["crypto"].update(crypto, wanted, demand_changed)
        self.conns["news"].update(news, wanted, demand_changed)
        # one live candle per candle topic of any subscriber
        topics = {raw: t for s in subs for raw, t in s.topics.items() if t.kind == "candle"}
        for raw in [r for r in self.candles if r not in topics]:
            del self.candles[raw]
            self._finals.pop(raw, None)
            self._seed_due.pop(raw, None)
            self._candle_rl.forget(raw)
        for raw, t in topics.items():
            if raw not in self.candles:
                lc = LiveCandle(t.symbol, t.tf, t.session, t.asset_class)
                self.candles[raw] = lc
                self._finals[raw] = deque()
        self._sync_candles()
        # SIP tail refresh: ≤ 8 equity symbols with an open intraday chart (priority order)
        intraday = [sym for sym in stocks if any(t.symbol == sym and t.tf in INTRADAY_MINUTES
                                                 for t in topics.values())][:limits.SIP_TAIL_MAX_SYMBOLS]
        for sym in [s for s in self._sip_through if s not in intraday]:
            del self._sip_through[sym]
            self._sip_errors.pop(sym, None)
        for sym in intraday:
            if sym not in self._sip_through:
                from ..bars.intraday import sip_through
                self._sip_through[sym] = sip_through(self._now()) - timedelta(minutes=2)
        for sym in [s for s in self._trades if not self._subs_with(f"trades:{s}")]:
            del self._trades[sym]
        for sym in [s for s in self.last_trades if not self._symbol_streaming(s)]:
            del self.last_trades[sym]
        self._refresh_symbol_status()

    # --- per-symbol live state --------------------------------------------------------------
    def _conn_for(self, symbol: str) -> StreamConnection:
        return self.conns["crypto" if instrument(symbol).asset_class == "crypto" else "stocks"]

    def _symbol_streaming(self, symbol: str) -> bool:
        c = self._conn_for(symbol)
        return self.running and c.is_live() and symbol in c.desired and symbol not in c.rejected

    def symbol_live(self, symbol: str):
        """(live, reason) of a symbol's trades/candles."""
        if symbol in self._limited:
            return False, protocol.SYMBOL_LIMIT_REASON.format(limit=limits.STOCK_STREAM_SYMBOL_LIMIT, symbol=symbol)
        if not self.running:
            return False, NOT_RUNNING_MESSAGE
        c = self._conn_for(symbol)
        if not c.is_live():
            return False, c.symbol_reason()
        if symbol in c.rejected:
            return False, c.rejected[symbol]
        return True, self._sip_errors.get(symbol)

    def quote_state(self, symbol: str) -> dict:
        """MR4 watchlist quotes: {live, reason, limited, last: {price, ts, feed}|None} — the stream only."""
        limited = symbol in self._limited
        live, reason = self.symbol_live(symbol)
        if live and symbol not in self._conn_for(symbol).desired:
            live, reason = False, NOT_SUBSCRIBED_MESSAGE
        last = self.last_trades.get(symbol) if live else None
        return {"live": live, "reason": reason, "limited": limited, "last": dict(last) if last else None}

    def _refresh_symbol_status(self) -> None:
        for s in self.subscribers:
            syms = s.symbols(("trades", "candle"))
            for sym in syms:
                st = self.symbol_live(sym)
                if self._status_sent.get((s.seq, sym)) != st:
                    self._status_sent[(s.seq, sym)] = st
                    self._send([s], {"type": "symbol_status", "symbol": sym, "live": st[0], "reason": st[1]},
                               f"status:{sym}")
            for k in [k for k in self._status_sent if k[0] == s.seq and k[1] not in syms]:
                del self._status_sent[k]

    # --- connection callbacks ---------------------------------------------------------------
    def _on_conn_change(self, conn: StreamConnection) -> None:
        live = conn.is_live()
        was = self._conn_live.get(conn.endpoint, False)
        self._conn_live[conn.endpoint] = live
        ac = ENDPOINT_CLASS.get(conn.endpoint)
        self._sync_candles()
        if ac is not None and live != was:
            if not live:
                for sym in [s for s in self._trades if instrument(s).asset_class == ac]:
                    del self._trades[sym]
                for sym in [s for s in self.last_trades if instrument(s).asset_class == ac]:
                    del self.last_trades[sym]
        self._refresh_symbol_status()

    def _sync_candles(self) -> None:
        """Starts (seed from REST) / stops the live candles whose symbol started / stopped streaming
        (topic added, connection (re)established or lost, stream-limit slot gained or lost)."""
        for raw, lc in self.candles.items():
            on = self._symbol_streaming(lc.symbol)
            if on and lc.streaming_since is None:
                lc.streaming_since = self._now()
                self._begin(raw, lc)
            elif not on and lc.streaming_since is not None:
                lc.streaming_since = None
                lc.reset()                           # gap: nothing is sent until re-seeded
                self._seed_due.pop(raw, None)
                self._finals[raw].clear()

    def _begin(self, raw: str, lc: LiveCandle) -> None:
        if lc.begin_at(self._now()):
            self._want_seed(raw)

    def _want_seed(self, raw: str, at: float = 0.0) -> None:
        self._seed_due[raw] = at
        if self._seed_wake is not None:
            self._seed_wake.set()

    def on_trade(self, endpoint: str, tr: dict) -> None:
        sym = tr["symbol"]
        if self._symbol_streaming(sym):
            ts = iso(tr["ts"])
            prev = self.last_trades.get(sym)
            if prev is None or prev["ts"] <= ts:         # a late (older) trade never replaces a newer one
                self.last_trades[sym] = {"price": tr["price"], "ts": ts,
                                         "feed": "iex" if endpoint == "stocks" else "crypto"}
            if self.trade_hook is not None:              # MR7 alerts: every trade, not the coalesced ones
                try:
                    self.trade_hook(endpoint, tr)
                except Exception:   # noqa: BLE001 — an alert failure never stops the stream
                    logger.exception("Research hub: trade hook failed")
        if self._subs_with(f"trades:{sym}"):
            self._trades[sym] = {"type": "trade", "symbol": sym, "price": tr["price"], "size": tr["size"],
                                 "ts": iso(tr["ts"]), "feed": "iex" if endpoint == "stocks" else "crypto"}
        for raw, lc in self.candles.items():
            if lc.symbol != sym:
                continue
            if lc.streaming_since is None:
                continue
            closed = lc.apply_trade(tr["ts"], tr["price"], tr["size"], lc.streaming_since, self._now())
            if closed is not None:                   # the closed previous bar, or a late-trade correction
                self._finals[raw].append(closed)
            if lc.needs_seed and raw not in self._seed_due:
                self._want_seed(raw)

    def on_bar(self, endpoint: str, bar: dict) -> None:
        now = self._now()
        for raw, lc in self.candles.items():
            if lc.symbol == bar["symbol"] and lc.streaming_since is not None:
                fixed = lc.apply_bar(bar, now)
                if fixed is not None:                # minute bar of the bucket closed last: corrected bar
                    self._finals[raw].append(fixed)

    def on_news(self, item: dict, wire_symbols: List[str]) -> None:
        mapping = self.conns["news"].subscribed
        syms = {mapping[w] for w in wire_symbols if w in mapping}
        subs = [s for s in self.subscribers if any(f"news:{sym}" in s.topics for sym in syms)]
        if self.news_hook is not None:       # MR6: de-dup, store queue, public shape (thumbnail proxy paths)
            try:
                item = self.news_hook(item, sorted(syms))
            except Exception:   # noqa: BLE001 — a hook failure never stops the stream
                logger.exception("Research hub: news hook failed — item sent without images")
                item = dict(item, images=[])
            if item is None:
                return
        else:
            item = dict(item, images=[])     # no MR6 service: never pass third-party image URLs on
        self._send(subs, {"type": "news", "item": item})

    # --- status (GET /research/feeds/status, §1.2) ------------------------------------------
    def feeds_status(self) -> List[dict]:
        rows = []
        for e in protocol.ENDPOINT_IDS:
            c = self.conns[e]
            live = self.running and c.is_live()
            if self.running:
                state, message = c.state, c.message()
            else:
                state, message = "off", self.stop_reason or NOT_RUNNING_MESSAGE
            rows.append({"id": e, "label": c.label, "feed_label": c.feed_label, "state": state,
                         "message": limits.clip(message),
                         "attempt": c.attempt if (self.running and c.state == "waiting") else 0,
                         "connected_since": iso(c.connected_since) if live and c.connected_since else None,
                         "last_message_at": iso(c.last_message_at) if c.last_message_at else None,
                         "symbols": len([s for s in c.desired if s not in c.rejected]) if live else 0,
                         "symbol_limit": c.symbol_limit})
        return rows

    def demand(self) -> dict:
        views, alerts = self._counts or (0, 0)
        return {"views": views, "alerts": alerts, "streams_wanted": views + alerts > 0, "note": DEMAND_NOTE}

    def feeds_message(self) -> Optional[dict]:
        if self.status_fn is None:
            return None
        return {"type": "feeds", "data": self.status_fn()}

    # --- supervised task --------------------------------------------------------------------
    async def run(self) -> None:
        self.conns = self._make_conns()
        self._conn_live, self._counts, self._feeds_last = {}, None, None
        self._seed_wake = asyncio.Event()
        self.running, self.stop_reason = True, None
        for lc in self.candles.values():
            lc.reset()
            lc.streaming_since = None
        self._seed_due.clear()
        self._recompute()
        tasks = [asyncio.ensure_future(c.run()) for c in self.conns.values()]
        tasks += [asyncio.ensure_future(f()) for f in (self._tick_loop, self._feeds_loop, self._sip_loop,
                                                        self._seed_loop)]
        try:
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if not t.cancelled() and t.exception() is not None:
                    raise t.exception()
            raise RuntimeError("a market data hub component ended unexpectedly")
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.running = False
            self.stop_reason = "Live market data stopped — the Research task is restarting or stopped."
            for lc in self.candles.values():
                lc.reset()
                lc.streaming_since = None
            for q in self._finals.values():
                q.clear()
            self._trades.clear()
            self.last_trades.clear()
            self._seed_due.clear()
            try:
                self._refresh_symbol_status()
                self._push_feeds()
            except Exception:   # noqa: BLE001 — never mask the original error
                logger.exception("Research hub: final status update failed")

    # --- loops ------------------------------------------------------------------------------
    async def _tick_loop(self) -> None:
        while True:
            self._flush()
            await asyncio.sleep(self.config.tick)

    def _flush(self) -> None:
        now = self._now()
        for raw, lc in self.candles.items():
            if lc.due(now):
                closed = lc.close()
                if closed is not None:
                    self._finals[raw].append(closed)
            if not self._candle_rl.ready(raw):
                continue
            finals = self._finals[raw]
            if finals:
                bar = finals.popleft()
            elif lc.dirty and lc.ready:
                bar = lc.bar(final=False)
                lc.dirty = False
            else:
                continue
            if bar is None:
                continue
            self._candle_rl.mark(raw)
            self._send(self._subs_with(raw), {"type": "candle", "symbol": lc.symbol, "tf": lc.tf,
                                              "session": lc.session, "bar": bar}, f"{raw}@{bar['t']}")
        for sym in list(self._trades):
            if self._trade_rl.ready(sym):
                msg = self._trades.pop(sym)
                self._trade_rl.mark(sym)
                self._send(self._subs_with(f"trades:{sym}"), msg, f"trade:{sym}")

    def _push_feeds(self) -> None:
        msg = self.feeds_message()
        if msg is None:
            return
        comparable = {k: v for k, v in msg["data"].items() if k != "generated_at"}
        if comparable != self._feeds_last:
            self._feeds_last = comparable
            self._send(self._subs_with("feeds"), msg, "feeds")

    async def _feeds_loop(self) -> None:
        while True:
            self._push_feeds()
            await asyncio.sleep(self.config.feeds_interval)

    async def _blocking(self, fn, *args, **kwargs):
        from strategy_platform.data import http_gate
        with http_gate.research_priority():
            return await run_blocking(fn, *args, **kwargs)

    def _data(self):
        if self._data_fn is None:
            raise RuntimeError("market data hub has no data service")
        return self._data_fn()

    # seeds: REST 1m (+ days for 1W) of the part of the current bucket before the stream covered it
    def _fetch_seed(self, lc: LiveCandle, start: datetime, until: datetime):
        data = self._data()
        days = []
        if lc.asset_class == "crypto":
            today0 = agg.utc_midnight(until.date())
        else:
            today0 = agg.ny_midnight(until.astimezone(agg.NY).date())
        if lc.tf == "1W" and lc.bucket.t < today0:
            if lc.asset_class == "crypto":
                rows = data.sources.crypto_hourly(lc.symbol, lc.bucket.t, today0)
            else:
                rows = data.sources.daily(lc.symbol, lc.bucket.t, today0)
            days = [m for m in (agg.minute(r, "sip") for r in rows or []) if lc.bucket.t <= m["t"] < today0]
            start = max(start, today0)
        if until <= start:
            return [], days
        if lc.asset_class == "crypto":
            return data.loader.crypto_minutes(lc.symbol, start, until), days
        return data.loader.equity_minutes(lc.symbol, start, until), days

    async def _seed_loop(self) -> None:
        while True:
            mono = self._mono()
            due = [raw for raw, at in self._seed_due.items() if at <= mono]
            if not due:
                nxt = min(self._seed_due.values(), default=None)
                timeout = 1.0 if nxt is None else max(0.0, min(1.0, nxt - mono))
                try:
                    await within(self._seed_wake.wait(), timeout)
                except asyncio.TimeoutError:
                    pass
                self._seed_wake.clear()
                continue
            for raw in due:
                lc = self.candles.get(raw)
                if lc is None or not lc.needs_seed or not self._symbol_streaming(lc.symbol) or lc.bucket is None:
                    self._seed_due.pop(raw, None)
                    continue
                start, until = lc.seed_window(self._now())
                bucket_t = lc.bucket.t
                try:
                    minutes, days = await self._blocking(self._fetch_seed, lc, start, until)
                except (UserFacingError, ValueError) as ex:
                    reason = getattr(ex, "user_message", None) or "the 1m history could not be loaded"
                    logger.warning("Research hub: live candle seed for %s failed: %s", raw, reason)
                    if self.candles.get(raw) is lc:
                        self._want_seed(raw, self._mono() + self.config.seed_retry)
                        self._send(self._subs_with(raw), {
                            "type": "error", "code": "candle_unavailable", "topic": raw,
                            "message": limits.clip(f"Live candle for {lc.symbol} {lc.tf} unavailable: {reason}")})
                    continue
                if self.candles.get(raw) is lc and lc.bucket is not None and lc.bucket.t == bucket_t \
                        and lc.needs_seed:
                    lc.apply_seed(minutes, days)
                self._seed_due.pop(raw, None)

    async def _sip_loop(self) -> None:
        from ..bars.intraday import sip_through
        while True:
            await asyncio.sleep(self.config.sip_refresh)
            if not (self._counts and sum(self._counts) > 0):
                continue
            new = sip_through(self._now())
            for sym in list(self._sip_through):
                last = self._sip_through.get(sym)
                if last is None or new <= last:
                    continue
                await self._sip_refresh(sym, last, new)

    async def _sip_refresh(self, sym: str, last: datetime, new: datetime) -> None:
        tops = [(raw, lc) for raw, lc in self.candles.items()
                if lc.symbol == sym and lc.tf in INTRADAY_MINUTES and lc.asset_class == "equity"]
        if not tops:
            return
        start = min(agg.bucket_start(last, lc.tf, lc.session, "equity") or last for _r, lc in tops)
        try:
            minutes = await self._blocking(self._data().loader.equity_minutes, sym, start, new, with_tail=False)
        except (UserFacingError, ValueError) as ex:
            reason = getattr(ex, "user_message", None) or "unexpected SIP data"
            self._sip_errors[sym] = limits.clip(f"SIP update failed: {reason} The IEX live tail stays marked "
                                                f"as tail.")
            logger.warning("Research hub: SIP tail refresh for %s failed: %s", sym, reason)
            self._refresh_symbol_status()
            return
        if self._sip_through.get(sym) != last:
            return                                   # topic set changed meanwhile
        self._sip_errors.pop(sym, None)
        minutes = [m for m in minutes if m["t"] < new and m.get("feed", "sip") == "sip"]
        for raw, lc in tops:
            if self.candles.get(raw) is not lc:
                continue
            bars = agg.aggregate(minutes, lc.tf, lc.session, "equity", sip_through=new)
            sel = []
            for b in bars:
                bstart = datetime.fromisoformat(b["t"])
                if b["final"] and agg.bucket_end(bstart, lc.tf, lc.session, "equity") > last:
                    sel.append(b)
            if sel:
                base = {"type": "sip_replace", "symbol": sym, "tf": lc.tf, "session": lc.session}
                subs = self._subs_with(raw)
                for msg in chunk_bars(base, sel):
                    self._send(subs, msg)
        self._sip_through[sym] = new
        self._refresh_symbol_status()


class _LazyTransport:
    """Resolves the websockets transport at the first connect (keeps the import lazy)."""

    def __init__(self):
        self._t = None

    async def connect(self, url):
        if self._t is None:
            self._t = _default_transport()
        return await self._t.connect(url)
