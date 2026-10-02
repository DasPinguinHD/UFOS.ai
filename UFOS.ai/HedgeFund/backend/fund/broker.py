"""Paper broker of the HedgeFund loop — realistic long/short simulation
(fund/SPEC.md §6).

PROOF OF CONCEPT, PAPER TRADING ONLY: there is no live trading code path;
``PaperBroker`` refuses any mode other than "paper" and never talks to a
real broker. Simulated fills at delayed quotes — not investment advice.

Account model (cash account for longs, Reg T margin for shorts):
  cash   = sum of fund_cash_ledger (includes short-sale proceeds)
  L      = sum of long market values, S = sum of |short market values|
  E      = cash + L - S                       (equity)
  BP     = cash - S * (1 + initial_margin)    (buying power for new longs;
           a new short of value V needs V * initial_margin + fees <= BP)
  maintenance: cash - S >= maintenance_margin * S, else forced covers.
Fills: market orders only, at the quote with adverse slippage fees_bps/2 in
the price plus a fee of fees_bps/2 of notional. Outside the regular session
orders are queued ("pending") and filled by ``process_pending`` at the first
cycle in the session after re-validation. Whole shares only (min 1).

Crypto (``params.is_crypto``, e.g. BTC-USD; SPEC §6): tradable 24/7 (no
session, no opening grace), fractional quantities rounded DOWN to 8
decimals with a $10 minimum notional, ``crypto_fees_bps``. Longs are spot;
shorts are simulated perpetual futures: booked like a short sale (notional
credited to cash, liability marked to market — the same P&L), but with
ISOLATED perp margin instead of Reg T: posted margin =
crypto_initial_margin x entry notional at the entry QUOTE (fill price without
slippage); when posted margin + unrealized P&L vs that quote <
crypto_maintenance_margin x current notional the position is liquidated
(forced cover, origin "margin-call", extra ledger row "liquidation_fee" of
crypto_liquidation_fee_pct of the notional, event "margin_call"). Funding
every 8 h (00/08/16 UTC, catch-up from fund.last_funding_at, only windows
after the position was opened, one deduplicated ledger row per window,
booked before any fill that changes the short): |short notional| x rate/100, positive rate
= longs pay shorts, so the simulated shorts RECEIVE it (ledger "funding");
spot longs pay no funding. No borrow fee, no dividends, no Rule 201 for
crypto. Per-class sums: S = S_eq + S_cr,
  BP = cash - S_eq*(1+im_eq) - S_cr*(1+im_cr),
  account maintenance R = mm_eq*S_eq + mm_cr*S_cr (cash - S >= R, else
  forced covers, largest short first); the short-exposure cap stays gross
  over all shorts.
Opening grace: during the first PENDING_FILL_DELAY_MINUTES (5) of the session
nothing is filled — new orders are queued and pending orders wait — because
the delayed yfinance quote (fast_info.last_price) can still be the previous
close right after 09:30 NY. Quotes carry no trade time from yfinance
(Quote.ts is the fetch time), so this time rule is the actual safeguard; a
Quote whose ts is before today's open is treated as stale as well.

State reads fail closed: when the cash ledger / positions can't be read
(store disabled, SQLite error) ``account``, ``snapshot_equity``,
``check_margin`` and ``reset`` raise ``StateUnavailable`` instead of
computing with cash 0 (which would collapse equity and trip the drawdown
stop); ``submit``/``target`` return an unrecorded rejection and
``process_pending``/``daily_accruals`` leave their work for the next cycle.

All state lives in SQLite via the injected ``store`` module (fund.store):
positions, orders, cash ledger, equity curve, meta keys. All methods are
synchronous and serialised by a re-entrant lock (the engine calls them via
asyncio.to_thread; a manual API order may race with a cycle).
"""
from __future__ import annotations

import logging
import math
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import market
from . import params as fund_params
from .types import ORDER_ORIGINS, ORDER_SIDES, AccountView, OrderRequest, OrderResult, PositionView, Quote

logger = logging.getLogger(__name__)

REDUCING_SIDES = ("sell", "cover")
PROTECTIVE_ORIGINS = ("kill-switch", "drawdown-stop", "margin-call")
USER_ORIGINS = ("manual", "manual-accept")
# pending orders a target() of the given origin may cancel and replace; all
# other pending orders of the symbol (manual, protective) are left untouched
# and the target is skipped
REPLACEABLE_BY = {"autopilot": ("autopilot",), "manual-accept": ("autopilot", "manual-accept")}
PENDING_FILL_DELAY_MINUTES = 5     # opening grace (stale delayed quote right after 09:30 NY)
DIVIDEND_LOOKBACK_DAYS = 10        # trailing window of ex-dates scanned every cycle (deduplicated)
STATE_UNAVAILABLE = "account state unavailable (database not readable) — try again"


class StateUnavailable(RuntimeError):
    """The paper account (cash ledger / positions) could not be read. Raised
    instead of computing with a stand-in value (cash 0, no positions): the
    engine must skip trading, drawdown/margin checks and equity snapshots
    for this cycle when it sees it."""


def _utc(now) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, str):
        now = datetime.fromisoformat(now.replace("Z", "+00:00"))
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return _utc(dt).isoformat(timespec="seconds")


def _finite(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def fill_values(side: str, qty: float, price: float, fees_bps: float) -> Tuple[float, float, float]:
    """(fill_price, fee, notional) of a market fill: adverse slippage of
    fees_bps/2 in the price (buy/cover higher, sell/short lower) plus a fee
    of fees_bps/2 of the notional — together fees_bps."""
    half = max(0.0, float(fees_bps)) / 2.0 / 10000.0
    fp = price * (1.0 + half) if side in ("buy", "cover") else price * (1.0 - half)
    notional = qty * fp
    return fp, notional * half, notional


def round_qty(symbol: str, x) -> float:
    """Order quantity rounded DOWN: whole shares (int) for equities,
    8 decimals for crypto. Never negative."""
    v = max(0.0, float(x))
    if fund_params.is_crypto(symbol):
        scale = 10 ** fund_params.CRYPTO_QTY_DECIMALS
        return math.floor(v * scale + 1e-6) / scale
    return int(math.floor(v + 1e-9))


def ceil_qty(symbol: str, x) -> float:
    """Quantity rounded UP (whole shares / 8 decimals)."""
    v = max(0.0, float(x))
    if fund_params.is_crypto(symbol):
        scale = 10 ** fund_params.CRYPTO_QTY_DECIMALS
        return math.ceil(v * scale - 1e-6) / scale
    return int(math.ceil(v - 1e-9))


def fmt_qty(x) -> str:
    """'9' for whole numbers, up to 8 decimals otherwise ('0.01234567')."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    if not math.isfinite(v):
        return str(v)
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:.8f}".rstrip("0").rstrip(".")


def _same_qty(a, b) -> bool:
    return abs(float(a) - float(b)) < 1e-9


MIN_SIZE_REASON = f"size below minimum (${fund_params.CRYPTO_MIN_NOTIONAL:g} notional)"


class PaperBroker:
    """Simulated broker. Dependencies are injected so tests can fake them:

    store        fund.store module (or a compatible object)
    quote_fn     symbol -> Quote|None (e.g. PriceFeed.quote)
    clock        () -> aware datetime (UTC); used when ``now`` is omitted
    dividends_fn (symbol, since_date) -> [(ex_date, amount_per_share)] with
                 ex_date > since_date; None = fetch failed (retried)
    session_fn   now -> market.session_state(now)-like dict ("open" key)
    params_fn    () -> full params dict (default: store.effective_params())
    bars_fn      symbol -> [Bar] daily bars (Rule 201 look-back), optional

    Every public method takes ``params`` (defaults to params_fn()) and
    ``prices`` ({symbol: float|Quote}) to avoid re-fetching quotes the engine
    already has; missing symbols are fetched with quote_fn (memoised per call).
    """

    MODE = "paper"

    def __init__(self, store, quote_fn: Callable[[str], Optional[Quote]], clock: Optional[Callable[[], datetime]] = None,
                 *, dividends_fn: Optional[Callable] = None, session_fn: Optional[Callable] = None,
                 params_fn: Optional[Callable[[], dict]] = None, bars_fn: Optional[Callable] = None,
                 mode: str = "paper"):
        if mode != "paper":
            raise ValueError("PaperBroker supports only mode='paper' (no live trading exists)")
        self.store = store
        self.quote_fn = quote_fn
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.dividends_fn = dividends_fn
        self.session_fn = session_fn or market.session_state
        self.params_fn = params_fn or (lambda: store.effective_params()[1])
        self.bars_fn = bars_fn
        self.mode = "paper"
        self.margin_warnings: List[str] = []
        self._lock = threading.RLock()

    # --- small helpers -------------------------------------------------------

    def _now(self, now) -> datetime:
        return _utc(now if now is not None else self.clock())

    def _params(self, params) -> dict:
        return fund_params.merge(params if params is not None else self.params_fn(), None)

    def _is_open(self, now: datetime) -> bool:
        try:
            return bool(self.session_fn(now).get("open"))
        except Exception as ex:
            logger.warning("session state failed (%s) — treating the market as closed", ex)
            return False

    def _quote(self, symbol: str, pm: Dict[str, Any]) -> Optional[Quote]:
        if symbol in pm:
            v = pm[symbol]
            if v is None or isinstance(v, Quote):
                return v
            p = _finite(v)
            return Quote(symbol=symbol, price=p) if p and p > 0 else None
        q = None
        try:
            q = self.quote_fn(symbol) if self.quote_fn else None
        except Exception as ex:
            logger.warning("quote for %s failed: %s", symbol, ex)
        if q is not None and not (_finite(q.price) and q.price > 0):
            q = None
        pm[symbol] = q
        return q

    def _mark(self, pos: dict, pm: Dict[str, Any]) -> float:
        q = self._quote(pos["symbol"], pm)
        if q is not None:
            return float(q.price)
        return float(pos.get("last_price") or pos.get("avg_price") or 0.0)

    def _read_book(self) -> Tuple[float, List[dict], Optional[int]]:
        """(cash, open position rows, ledger row count or None) read in one
        go; raises StateUnavailable when unreadable (never cash 0 as a
        stand-in)."""
        snap_fn = getattr(self.store, "account_snapshot", None)
        if snap_fn is not None:
            snap = snap_fn()
            if not snap:
                raise StateUnavailable(STATE_UNAVAILABLE)
            return float(snap["cash"]), list(snap["positions"]), snap.get("ledger_rows")
        cash = self.store.cash_balance()
        if cash is None:
            raise StateUnavailable(STATE_UNAVAILABLE)
        return float(cash), list(self.store.get_positions() or []), None

    def _state(self, pm: Dict[str, Any]) -> dict:
        """cash, marks, L, S, E from the store (StateUnavailable when the
        store can't be read)."""
        cash, rows, _n = self._read_book()
        marks = {}
        long_mv = short_eq = short_cr = 0.0
        for p in rows:
            px = self._mark(p, pm)
            marks[p["symbol"]] = px
            if p["qty"] > 0:
                long_mv += p["qty"] * px
            elif p["qty"] < 0:
                if fund_params.is_crypto(p["symbol"]):
                    short_cr += -p["qty"] * px
                else:
                    short_eq += -p["qty"] * px
        short_mv = short_eq + short_cr
        return {"cash": cash, "positions": {p["symbol"]: p for p in rows}, "marks": marks,
                "long_mv": long_mv, "short_mv": short_mv, "short_mv_equity": short_eq, "short_mv_crypto": short_cr,
                "equity": cash + long_mv - short_mv}

    @staticmethod
    def _buying_power(st: dict, p: dict) -> float:
        """cash - S_eq*(1+im_eq) - S_cr*(1+im_cr) (may be negative)."""
        im_eq = float(p["initial_margin_pct"]) / 100.0
        im_cr = float(p["crypto_initial_margin_pct"]) / 100.0
        return st["cash"] - st.get("short_mv_equity", st["short_mv"]) * (1.0 + im_eq) \
            - st.get("short_mv_crypto", 0.0) * (1.0 + im_cr)

    @staticmethod
    def _maintenance(st: dict, p: dict) -> float:
        """Account maintenance requirement R = mm_eq*S_eq + mm_cr*S_cr."""
        mm_eq = float(p["maintenance_margin_pct"]) / 100.0
        mm_cr = float(p["crypto_maintenance_margin_pct"]) / 100.0
        return mm_eq * st.get("short_mv_equity", st["short_mv"]) + mm_cr * st.get("short_mv_crypto", 0.0)

    @staticmethod
    def perp_excess(pos: dict, px: float, p: dict) -> float:
        """Isolated margin excess of one crypto perp short, measured against
        the ENTRY QUOTE (the short's avg fill price with the adverse slippage
        removed: avg / (1 - crypto_fees_bps/2)): posted margin
        (crypto_initial_margin x entry notional at that quote) + unrealized
        P&L vs that quote - crypto_maintenance_margin x current notional.
        < 0 -> liquidation. A fresh short starts at (im - mm) x notional > 0
        (params require mm < im), never below maintenance because of fees."""
        q = -float(pos["qty"])
        half = fund_params.fees_bps_for(pos.get("symbol") or "BTC-USD", p) / 2.0 / 10000.0
        avg = float(pos.get("avg_price") or px)
        ref = avg / (1.0 - half) if half < 1.0 else avg
        im = float(p["crypto_initial_margin_pct"]) / 100.0
        mm = float(p["crypto_maintenance_margin_pct"]) / 100.0
        return im * ref * q + (ref - px) * q - mm * px * q

    def _crypto_excess(self, st: dict, p: dict) -> Optional[float]:
        vals = [self.perp_excess(pos, st["marks"][sym], p) for sym, pos in st["positions"].items()
                if pos["qty"] < 0 and fund_params.is_crypto(sym)]
        return sum(vals) if vals else None

    @staticmethod
    def _liq_fee(symbol: str, side: str, origin: Optional[str], notional: float, p: dict) -> float:
        """Liquidation fee of a forced crypto perp cover (origin margin-call)."""
        if origin != "margin-call" or side != "cover" or not fund_params.is_crypto(symbol):
            return 0.0
        return abs(notional) * float(p.get("crypto_liquidation_fee_pct") or 0.0) / 100.0

    def _can_fill(self, now: datetime, symbol: Optional[str] = None) -> bool:
        """True when market orders may be filled now: crypto always (24/7);
        equities when the regular session is open and the opening grace
        (PENDING_FILL_DELAY_MINUTES after the open) has passed."""
        if symbol is not None and fund_params.is_crypto(symbol):
            return True
        if not self._is_open(now):
            return False
        bounds = market.session_bounds(market.ny_date(now))
        if bounds is None:          # session_fn says open on a non-trading day (tests/fakes)
            return True
        return now >= bounds[0] + timedelta(minutes=PENDING_FILL_DELAY_MINUTES)

    @staticmethod
    def _stale(quote: Optional[Quote], now: datetime) -> bool:
        """A quote whose own timestamp lies before today's session open is
        from the previous session (feeds without a trade time pass). Crypto
        has no session: its quote is stale only when it is a fallback price
        (``Quote.stale``, e.g. the cached daily close) older than
        market.STALE_QUOTE_MINUTES — orders then wait instead of filling."""
        if quote is not None and fund_params.is_crypto(quote.symbol):
            return quote.stale and market.quote_is_stale(quote, now)
        if quote is None or not quote.ts:
            return False
        bounds = market.session_bounds(market.ny_date(now))
        if bounds is None:
            return False
        try:
            return _utc(quote.ts) < bounds[0]
        except (TypeError, ValueError):
            return False

    def _held(self, symbol: str) -> float:
        p = self.store.get_position(symbol)
        return float(p["qty"]) if p else 0.0

    @staticmethod
    def _result(order_id, symbol, side, qty, status, origin, ts, fill_price=None, fees=0.0, notional=None,
                reject_reason=None) -> OrderResult:
        return OrderResult(order_id=order_id, symbol=symbol, side=side, qty=qty, status=status, fill_price=fill_price,
                           fees=fees or 0.0, notional=notional, reject_reason=reject_reason, origin=origin, ts=ts)

    def _record_reject(self, req: OrderRequest, qty: float, reason: str, now: datetime) -> OrderResult:
        ts = _iso(now)
        oid = self.store.insert_order(ts, req.symbol, req.side, qty, "rejected", req.origin, run_id=req.run_id,
                                      recommendation_id=req.recommendation_id, rationale=req.rationale,
                                      reject_reason=reason)
        return self._result(oid, req.symbol, req.side, qty, "rejected", req.origin, ts, reject_reason=reason)

    def _rule201(self, symbol: str, quote: Quote, now: datetime) -> bool:
        """SEC Rule 201 approximation: the short-sale restriction applies when
        the price fell >= 10 % below the previous close today or yesterday."""
        if quote.prev_close and quote.price <= 0.9 * quote.prev_close:
            return True
        if not self.bars_fn:
            return False
        try:
            bars = list(self.bars_fn(symbol) or [])
        except Exception:
            return False
        today = market.ny_date(now).isoformat()
        yesterday = market.previous_trading_day(market.ny_date(now)).isoformat()
        for i in range(1, len(bars)):
            if bars[i].date in (today, yesterday) and bars[i - 1].close and \
                    bars[i].low <= 0.9 * bars[i - 1].close:
                return True
        return False

    def _static_reason(self, symbol: str, side: str, held: float, p: dict, quote: Optional[Quote],
                       now: datetime) -> Optional[str]:
        """Checks that don't depend on money: side vs position, short rules."""
        if side == "buy" and held < 0:
            return "cover the short position first"
        if side == "sell" and held <= 0:
            return "no long position to sell"
        if side == "cover" and held >= 0:
            return "no short position to cover"
        if side == "short":
            if held > 0:
                return "sell the long position first"
            if not p.get("allow_shorts", True):
                return "short selling is disabled"
            if symbol in (p.get("not_shortable") or []):
                return "not shortable (no borrow available)"
            if quote is not None and not fund_params.is_crypto(symbol) and self._rule201(symbol, quote, now):
                return "SEC Rule 201 short-sale restriction (price fell 10 % or more)"
        return None

    def _fit(self, side: str, n: float, price: float, p: dict, st: dict, allow_reduce: bool,
             symbol: str = "") -> Tuple[float, Optional[str]]:
        """Money checks for buy/short: returns (qty, reject_reason). Margin
        and fees of the symbol's class (crypto perps / Reg T equities)."""
        im = fund_params.margin_pcts_for(symbol, p)[0] / 100.0
        half = fund_params.fees_bps_for(symbol, p) / 2.0 / 10000.0
        bp = self._buying_power(st, p)
        if side == "buy":
            fp = price * (1.0 + half)
            max_n = round_qty(symbol, max(0.0, bp) / (fp * (1.0 + half)))
            reason = f"insufficient buying power (available ${max(0.0, bp):,.2f})"
        elif side == "short":
            fp = price * (1.0 - half)
            by_bp = round_qty(symbol, max(0.0, bp) / (fp * (im + half))) if fp * (im + half) > 0 else 0
            cap = float(p["short_exposure_max_pct"]) / 100.0
            room = cap * st["equity"] - st["short_mv"]
            per_share = price + cap * (price - fp + fp * half)
            by_exp = round_qty(symbol, room / per_share) if room > 0 and per_share > 0 else 0
            max_n = min(by_bp, by_exp)
            reason = (f"short exposure limit {p['short_exposure_max_pct']:g} % of equity reached"
                      if by_exp <= by_bp else f"insufficient buying power for the short margin "
                                               f"(available ${max(0.0, bp):,.2f})")
        else:
            return n, None
        if n <= max_n + 1e-12:
            return n, None
        crypto_ok = not fund_params.is_crypto(symbol) or max_n * price >= fund_params.CRYPTO_MIN_NOTIONAL
        if allow_reduce and max_n > 0 and crypto_ok:
            return max_n, None
        return 0, reason

    def _apply_fill(self, order_id: Optional[int], symbol: str, side: str, n: float, quote_price: float,
                    p: dict, now: datetime, origin: Optional[str] = None) -> Tuple[float, float, float]:
        """Books a fill: cash ledger (fill + fee [+ liquidation fee of a
        forced crypto perp cover]) and the position (weighted avg price on
        adds, realized P&L incl. fees on reduces). Returns (fill price,
        total fees, notional)."""
        crypto = fund_params.is_crypto(symbol)
        fp, fee, notional = fill_values(side, n, quote_price, fund_params.fees_bps_for(symbol, p))
        liq = self._liq_fee(symbol, side, origin, notional, p)
        ts = _iso(now)
        sign = -1.0 if side in ("buy", "cover") else 1.0
        tag = f"{side} {fmt_qty(n)} {symbol} @ {fp:.4f}" + (f" (order {order_id})" if order_id else "")
        self.store.add_cash_entry(ts, "fill", sign * notional, symbol=symbol, note=tag)
        if fee:
            self.store.add_cash_entry(ts, "fee", -fee, symbol=symbol, note=tag)
        if liq:
            self.store.add_cash_entry(ts, "liquidation_fee", -liq, symbol=symbol,
                                      note=f"perp liquidation {p.get('crypto_liquidation_fee_pct'):g} % of notional: "
                                           + tag)
        pos = self.store.get_position(symbol) or {}
        if crypto and float(pos.get("qty") or 0.0) < 0:
            # funding windows that fell due while the short was open are booked (deduplicated)
            # BEFORE it is reduced/closed/enlarged — they would be lost at the next accrual
            try:
                self._book_funding_symbol(pos, now, p, quote_price, ts)
            except Exception:
                logger.exception("funding before the %s fill failed — booked at the next accrual", symbol)
        q = float(pos.get("qty") or 0.0)
        avg = float(pos.get("avg_price") or 0.0)
        realized = float(pos.get("realized_pnl") or 0.0) - fee - liq
        opened_at = pos.get("opened_at")
        rate = pos.get("borrow_rate_pct")
        if side == "buy":
            new_q = q + n
            avg = (q * avg + n * fp) / new_q
            if q == 0:
                opened_at = ts
        elif side == "sell":
            realized += (fp - avg) * n
            new_q = q - n
        elif side == "short":
            new_q = q - n
            avg = (abs(q) * avg + n * fp) / abs(new_q)
            if q == 0:
                opened_at = ts
            rate = None if crypto else fund_params.borrow_rate_for(symbol, p)
        else:  # cover
            realized += (avg - fp) * n
            new_q = q + n
        if crypto:
            new_q = round(new_q, fund_params.CRYPTO_QTY_DECIMALS)
        if abs(new_q) < 1e-9:
            new_q, opened_at, rate = 0.0, None, None
        if new_q > 0:
            rate = None
        self.store.upsert_position(symbol, new_q, avg, updated_at=ts, opened_at=opened_at, realized_pnl=realized,
                                   borrow_rate_pct=rate, last_price=quote_price)
        return fp, fee + liq, notional

    # --- lifecycle -----------------------------------------------------------

    def ensure_initialized(self, now=None, params=None) -> bool:
        """Books the starting capital (params["starting_capital"], ledger kind
        'deposit') on first use of an empty store. True when it did."""
        with self._lock:
            db = getattr(self.store, "db", None)
            if db is not None and not db.is_enabled():
                return False
            if self.store.get_starting_capital() is not None:
                return False
            try:
                cash, rows, n_ledger = self._read_book()
            except StateUnavailable:
                return False            # unreadable is not "empty": never re-open the account on an error
            if cash or rows or n_ledger:
                return False
            p = self._params(params)
            self._reset(float(p["starting_capital"]), self._now(now), kind="deposit")
            return True

    def reset(self, starting_capital: float, now=None) -> AccountView:
        """Fresh paper account: cancels pending orders, drops all positions
        (no trades), books a 'reset' ledger entry so that cash =
        starting_capital, sets starting capital / reset_at / peak equity /
        accrual dates and writes an equity point. Ledger, orders and equity
        history are kept. The caller writes the audit row."""
        cap = _finite(starting_capital)
        lim = fund_params.LIMITS["starting_capital"]
        if cap is None or not lim["min"] <= cap <= lim["max"]:
            raise ValueError(f"starting_capital must be between {lim['min']:g} and {lim['max']:g}")
        with self._lock:
            return self._reset(cap, self._now(now), kind="reset")

    def _reset(self, cap: float, now: datetime, kind: str) -> AccountView:
        ts = _iso(now)
        current_cash, _rows, _n = self._read_book()      # StateUnavailable before anything is changed
        for o in self.store.pending_orders():
            self.store.update_order(o["id"], status="cancelled", reject_reason="cancelled: reset")
        self.store.clear_positions()
        delta = cap - current_cash
        self.store.add_cash_entry(ts, kind, delta, note=f"paper account {'reset' if kind == 'reset' else 'opened'} "
                                                        f"with ${cap:,.2f}")
        today = market.ny_date(now).isoformat()
        self.store.set_starting_capital(cap)
        self.store.set_reset_at(ts)
        self.store.set_peak_equity(cap)
        self.store.set_last_accrual_date(today)
        self.store.set_last_dividend_date(today)
        setter = getattr(self.store, "set_last_funding_at", None)
        if setter is not None:
            setter(market.last_funding_window(now))
        self.store.add_equity_point(ts, cap, cap, 0.0, 0.0, None, 0.0)
        return self.account(now=now, prices={})

    # --- views ---------------------------------------------------------------

    def positions(self, prices: Optional[dict] = None) -> List[PositionView]:
        """Open positions marked to the latest price (quote -> stored
        last_price -> avg_price)."""
        pm = dict(prices or {})
        with self._lock:
            out = []
            for p in self.store.get_positions():
                out.append(PositionView(symbol=p["symbol"], qty=p["qty"], avg_price=float(p["avg_price"] or 0.0),
                                        last_price=self._mark(p, pm), opened_at=p["opened_at"],
                                        realized_pnl=float(p["realized_pnl"] or 0.0),
                                        borrow_rate_pct=p["borrow_rate_pct"]
                                        if p["qty"] < 0 and not fund_params.is_crypto(p["symbol"]) else None))
            return out

    def account(self, prices: Optional[dict] = None, now=None, params=None) -> AccountView:
        """AccountView: equity, cash, L, S, buying power (>= 0), peak/drawdown
        (peak = max(stored peak, equity); not persisted here), maintenance,
        short exposure, day P&L vs the last snapshot before today's NY
        midnight (or the starting capital after a reset today), total P&L.
        Raises StateUnavailable when the cash ledger / positions can't be
        read — never an AccountView computed from cash 0."""
        pm = dict(prices or {})
        with self._lock:
            now = self._now(now)
            p = self._params(params)
            self.ensure_initialized(now, p)
            st = self._state(pm)
            e, s, cash = st["equity"], st["short_mv"], st["cash"]
            start = self.store.get_starting_capital()
            start = float(start) if start is not None else float(p["starting_capital"])
            peak = max(float(self.store.get_peak_equity() or e), e)
            dd = (peak - e) / peak * 100.0 if peak > 0 else 0.0
            maint = self._maintenance(st, p)
            reset_at = self.store.get_reset_at()
            funding_total = None
            sum_fn = getattr(self.store, "ledger_sum", None)
            if sum_fn is not None:
                try:
                    funding_total = sum_fn("funding", reset_at)
                except Exception:
                    funding_total = None
            midnight = datetime.combine(market.ny_date(now), datetime.min.time(), tzinfo=market.NY)
            ref = self.store.equity_point_before(midnight)
            day_ref = None
            if reset_at and (ref is None or ref["ts"] < reset_at) and reset_at >= _iso(midnight):
                day_ref = start
            elif ref is not None:
                day_ref = float(ref["equity"])
            return AccountView(
                equity=e, cash=cash, long_mv=st["long_mv"], short_mv=s,
                buying_power=max(0.0, self._buying_power(st, p)), peak_equity=peak, drawdown_pct=max(0.0, dd),
                maintenance_required=maint, margin_excess=(cash - s) - maint,
                short_exposure_pct=(s / e * 100.0) if e > 0 else (0.0 if s == 0 else 100.0),
                starting_capital=start, reset_at=reset_at,
                day_pnl=None if day_ref is None else e - day_ref,
                total_pnl=e - start, total_pnl_pct=(e / start - 1.0) * 100.0 if start else None,
                funding_total=funding_total, crypto_margin_excess=self._crypto_excess(st, p))

    def pending(self) -> List[dict]:
        """Queued orders (store rows), oldest first."""
        return self.store.pending_orders()

    def projected_qty(self, symbol: str) -> float:
        """Held quantity plus the net of pending orders for the symbol."""
        q = self._held(symbol)
        for o in self.store.pending_orders(symbol):
            q += o["qty"] if o["side"] in ("buy", "cover") else -o["qty"]
        return q

    # --- orders --------------------------------------------------------------

    def submit(self, req: OrderRequest, now=None, params=None, prices: Optional[dict] = None,
               allow_reduce: bool = False) -> OrderResult:
        """Validates, records and fills (session open) or queues (closed) one
        market order. qty (whole shares, floored) or notional (USD, converted
        with the quote). allow_reduce=True shrinks a buy/short that exceeds
        buying power / short limits and clips a sell/cover to the position
        instead of rejecting. Rejections are recorded too (status
        'rejected'); malformed requests (bad symbol/side/origin) and an
        unreadable account state (STATE_UNAVAILABLE) return a rejected
        result with order_id None (not recorded). While the market is
        closed or within the opening grace (first 5 min) the order is
        queued as 'pending'."""
        pm = prices if isinstance(prices, dict) and getattr(prices, "_memo", False) else _Memo(prices or {})
        with self._lock:
            now = self._now(now)
            ts = _iso(now)
            p = self._params(params)
            self.ensure_initialized(now, p)
            sym = fund_params.normalise_symbol(req.symbol)
            if sym is None or req.side not in ORDER_SIDES or req.origin not in ORDER_ORIGINS:
                return self._result(None, str(req.symbol)[:12], str(req.side), 0, "rejected", str(req.origin), ts,
                                    reject_reason="invalid symbol, side or origin")
            req.symbol = sym
            crypto = fund_params.is_crypto(sym)
            quote = self._quote(sym, pm)
            held = self._held(sym)
            can_fill = self._can_fill(now, sym) and not self._stale(quote, now)
            # size (whole shares for equities, 8 decimals for crypto — rounded down)
            n = 0
            if req.qty is not None:
                qv = _finite(req.qty)
                if qv is None or qv < 0:
                    return self._record_reject(req, 0, "invalid quantity", now)
                n = round_qty(sym, qv)
            elif req.notional is not None:
                nv = _finite(req.notional)
                if nv is None or nv <= 0:
                    return self._record_reject(req, 0, "invalid notional", now)
                if quote is None:
                    return self._record_reject(req, 0, "no quote available", now)
                n = round_qty(sym, nv / quote.price)
            else:
                return self._record_reject(req, 0, "qty or notional required", now)
            if n <= 0:
                return self._record_reject(req, 0, MIN_SIZE_REASON if crypto else "size below one share", now)
            reason = self._static_reason(sym, req.side, held, p, quote, now)
            if reason:
                return self._record_reject(req, n, reason, now)
            if req.side in REDUCING_SIDES and n > abs(held) + 1e-12:
                if not allow_reduce:
                    return self._record_reject(req, n, f"exceeds the position ({fmt_qty(abs(held))} "
                                                       f"{'units' if crypto else 'shares'})", now)
                n = abs(held) if crypto else int(abs(held))
            if crypto:
                # $10 minimum notional; closing a whole (dust) position is always allowed
                full_close = req.side in REDUCING_SIDES and _same_qty(n, abs(held))
                px = quote.price if quote is not None else None
                if not full_close and px is not None and n * px < fund_params.CRYPTO_MIN_NOTIONAL - 1e-9:
                    return self._record_reject(req, n, MIN_SIZE_REASON, now)
            if quote is None and req.side not in REDUCING_SIDES:
                return self._record_reject(req, n, "no quote available", now)
            if not can_fill or quote is None:
                # closed market / opening grace (or no quote for a closing order): queue for the session
                est = n * quote.price if quote else None
                oid = self.store.insert_order(ts, sym, req.side, n, "pending", req.origin, run_id=req.run_id,
                                              recommendation_id=req.recommendation_id, rationale=req.rationale,
                                              notional=est)
                return self._result(oid, sym, req.side, n, "pending", req.origin, ts, notional=est)
            if req.side in ("buy", "short"):
                try:
                    st = self._state(pm)
                except StateUnavailable:
                    return self._result(None, sym, req.side, n, "rejected", req.origin, ts,
                                        reject_reason=STATE_UNAVAILABLE)
                n, reason = self._fit(req.side, n, quote.price, p, st, allow_reduce, sym)
                if reason:
                    return self._record_reject(req, n, reason, now)
            fp, fee, notional = fill_values(req.side, n, quote.price, fund_params.fees_bps_for(sym, p))
            fee += self._liq_fee(sym, req.side, req.origin, notional, p)
            oid = self.store.insert_order(ts, sym, req.side, n, "filled", req.origin, run_id=req.run_id,
                                          recommendation_id=req.recommendation_id, rationale=req.rationale,
                                          notional=notional, fill_price=fp, fees=fee, filled_at=ts)
            self._apply_fill(oid, sym, req.side, n, quote.price, p, now, origin=req.origin)
            return self._result(oid, sym, req.side, n, "filled", req.origin, ts, fill_price=fp, fees=fee,
                                notional=notional)

    def target(self, symbol: str, target_weight: float, origin: str, now=None, params=None,
               prices: Optional[dict] = None, run_id: Optional[int] = None, recommendation_id: Optional[int] = None,
               rationale: Optional[str] = None) -> List[OrderResult]:
        """Orders that move the symbol to a signed target weight of equity
        (whole shares, truncated toward zero). Buys/shorts are reduced to what
        buying power / short limits allow.

        Transitions: long->short = sell all + short rest, short->long = cover
        + buy. The second leg is only sent once the first leg has FILLED:
        while the market is closed (or in the opening grace) only the first
        leg is queued; the next cycle after it filled at the open places the
        second leg. Nothing is rejected or recorded for the waiting leg.

        Pending orders of the symbol:
        - protective (kill-switch/drawdown-stop/margin-call) or user orders
          this origin may not replace (see REPLACEABLE_BY: autopilot never
          touches manual/manual-accept orders, manual-accept never touches
          manual orders) -> nothing is placed; a skip result (order_id None,
          reject_reason "protective order pending" / "manual order pending");
        - identical to what would be placed now (or the first leg of a
          transition) or already leading to the target -> kept, returns []
          (idempotent while closed: no cancel + re-queue churn);
        - otherwise cancelled ("replaced by a newer target") and re-planned.

        Returns [] when nothing is to do. Results with order_id None were not
        recorded (no quote, no equity, state unavailable, pending order
        left untouched)."""
        pm = _Memo(prices or {})
        with self._lock:
            now = self._now(now)
            ts = _iso(now)
            p = self._params(params)
            self.ensure_initialized(now, p)
            sym = fund_params.normalise_symbol(symbol) or str(symbol)
            quote = self._quote(sym, pm)
            w = _finite(target_weight)
            if quote is None or w is None:
                return [self._result(None, sym, "buy" if (w or 0) >= 0 else "short", 0, "rejected", origin, ts,
                                     reject_reason="no quote available" if quote is None else "invalid target")]
            try:
                st = self._state(pm)
            except StateUnavailable:
                return [self._result(None, sym, "buy" if w >= 0 else "short", 0, "rejected", origin, ts,
                                     reject_reason=STATE_UNAVAILABLE)]
            e = st["equity"]
            if e <= 0:
                return [self._result(None, sym, "buy", 0, "rejected", origin, ts, reject_reason="no equity")]
            crypto = fund_params.is_crypto(sym)
            raw = w * e / quote.price
            desired = round_qty(sym, abs(raw)) * (1 if raw >= 0 else -1)
            pos = st["positions"].get(sym)
            held = float(pos["qty"]) if pos else 0.0
            pend = self.store.pending_orders(sym)
            if any(o.get("origin") in PROTECTIVE_ORIGINS for o in pend):
                return [self._result(None, sym, "buy", 0, "rejected", origin, ts,
                                     reject_reason="protective order pending")]
            replaceable = REPLACEABLE_BY.get(origin, ("autopilot",))
            if any(o.get("origin") not in replaceable for o in pend):
                return [self._result(None, sym, "buy" if desired >= held else "sell", 0, "rejected", origin, ts,
                                     reject_reason="manual order pending")]
            if _same_qty(desired, held):
                plan = []
            elif held >= 0 and desired >= 0:
                plan = [("buy", desired - held)] if desired > held else [("sell", held - desired)]
            elif held <= 0 and desired <= 0:
                plan = [("short", held - desired)] if desired < held else [("cover", desired - held)]
            elif held > 0:
                plan = [("sell", held), ("short", -desired)]
            else:
                plan = [("cover", -held), ("buy", desired)]
            if crypto:
                plan = [(side, round(abs(n), fund_params.CRYPTO_QTY_DECIMALS)) for side, n in plan]
                # legs below the $10 minimum are dropped unless they close the whole position
                plan = [(side, n) for side, n in plan
                        if n > 0 and (n * quote.price >= fund_params.CRYPTO_MIN_NOTIONAL
                                      or (side in REDUCING_SIDES and _same_qty(n, abs(held))))]
            else:
                plan = [(side, int(abs(n))) for side, n in plan]
            if len(plan) == 2 and not self._can_fill(now, sym):
                plan = plan[:1]             # the second leg follows once the first has filled at the open

            def same(a, b):
                return len(a) == len(b) and all(x[0] == y[0] and _same_qty(x[1], y[1]) for x, y in zip(a, b))
            if pend:
                have = [(o["side"], float(o["qty"])) for o in pend]
                if plan and (same(have, plan) or (len(plan) == 2 and same(have, plan[:1]))):
                    return []               # identical order(s) already queued: keep them
                if _same_qty(self.projected_qty(sym), desired):
                    return []
                for o in pend:
                    self.store.update_order(o["id"], status="cancelled", reject_reason="replaced by a newer target")
            results = []
            for side, n in plan:
                r = self.submit(OrderRequest(symbol=sym, side=side, qty=n, origin=origin, run_id=run_id,
                                             recommendation_id=recommendation_id, rationale=rationale),
                                now=now, params=p, prices=pm, allow_reduce=True)
                results.append(r)
                if r.status != "filled":
                    break                   # rejected, or queued: the next leg waits for the fill
            return results

    def liquidate_all(self, origin: str, now=None, params=None, prices: Optional[dict] = None) -> List[OrderResult]:
        """Cancels every pending order and closes every position (sell longs,
        cover shorts); queued as pending while the market is closed (kill
        switch, drawdown stop)."""
        pm = _Memo(prices or {})
        with self._lock:
            now = self._now(now)
            p = self._params(params)
            for o in self.store.pending_orders():
                self.store.update_order(o["id"], status="cancelled", reject_reason=f"cancelled: {origin}")
            results = []
            for pos in self.store.get_positions():
                side = "sell" if pos["qty"] > 0 else "cover"
                results.append(self.submit(OrderRequest(symbol=pos["symbol"], side=side, qty=abs(pos["qty"]),
                                                        origin=origin, rationale=f"liquidate all ({origin})"),
                                           now=now, params=p, prices=pm, allow_reduce=True))
            return results

    def cancel_pending(self, origin: str, reason: str) -> List[dict]:
        """Cancels the queued (not yet filled) orders of one origin — e.g.
        "autopilot" when the autopilot is switched off; manual and protective
        pending orders are kept. Serialised with fills (broker lock).
        Returns the cancelled order rows (as they were before)."""
        with self._lock:
            out = []
            for o in self.store.pending_orders():
                if o.get("origin") != origin:
                    continue
                if self.store.update_order(o["id"], status="cancelled", reject_reason=reason):
                    out.append(o)
            return out

    def expire_pending(self, origin: str, older_than: timedelta, now=None, reason: str = "stale",
                       crypto_only: bool = True) -> List[dict]:
        """Cancels queued orders of ``origin`` (crypto only by default) placed
        more than ``older_than`` before now. Returns the cancelled rows."""
        with self._lock:
            now = self._now(now)
            out = []
            for o in self.store.pending_orders():
                if o.get("origin") != origin or (crypto_only and not fund_params.is_crypto(o["symbol"])):
                    continue
                try:
                    placed = _utc(o["ts"])
                except (TypeError, ValueError):
                    continue
                if now - placed > older_than and self.store.update_order(o["id"], status="cancelled",
                                                                         reject_reason=reason):
                    out.append(o)
            return out

    def process_pending(self, now=None, params=None, prices: Optional[dict] = None,
                        origins: Optional[Tuple[str, ...]] = None) -> List[OrderResult]:
        """Fills queued orders whose symbol is tradable now (equities: regular
        session after the opening grace; crypto: always), optionally only
        the given ``origins`` (the risk watch fills protective orders only):
        sells/covers first, then buys/shorts, oldest first, each
        re-validated against the current account — a sell/cover is clipped
        to the position, a buy/short is reduced to what buying power / short
        limits allow or rejected. Orders without a quote (or with a quote
        from before today's open) stay pending. Nothing is filled during the
        opening grace (first PENDING_FILL_DELAY_MINUTES = 5 min after 09:30
        NY): the delayed yfinance last price can still be yesterday's close
        right after the open. When the account state is unreadable the
        remaining orders stay pending for the next cycle."""
        pm = _Memo(prices or {})
        with self._lock:
            now = self._now(now)
            p = self._params(params)
            ts = _iso(now)
            results = []
            orders = sorted(self.store.pending_orders(),
                            key=lambda o: (0 if o["side"] in REDUCING_SIDES else 1, o["id"]))
            for o in orders:
                sym, side, origin = o["symbol"], o["side"], o.get("origin") or "manual"
                if origins is not None and origin not in origins:
                    continue
                if not self._can_fill(now, sym):
                    continue
                quote = self._quote(sym, pm)
                if quote is None or self._stale(quote, now):
                    continue
                held = self._held(sym)
                crypto = fund_params.is_crypto(sym)
                n = round(float(o["qty"]), fund_params.CRYPTO_QTY_DECIMALS) if crypto else int(o["qty"])
                reason = self._static_reason(sym, side, held, p, quote, now)
                if reason is None and side in REDUCING_SIDES:
                    n = min(n, abs(held) if crypto else int(abs(held)))
                    if n <= 1e-12:
                        reason = "position already closed"
                if reason is None and side in ("buy", "short"):
                    try:
                        st = self._state(pm)
                    except StateUnavailable:
                        logger.warning("pending orders: account state unreadable — left pending")
                        break
                    n, reason = self._fit(side, n, quote.price, p, st, True, sym)
                if reason:
                    self.store.update_order(o["id"], status="rejected", reject_reason=reason)
                    results.append(self._result(o["id"], sym, side, o["qty"], "rejected", origin, ts,
                                                reject_reason=reason))
                    continue
                fp, fee, notional = fill_values(side, n, quote.price, fund_params.fees_bps_for(sym, p))
                fee += self._liq_fee(sym, side, origin, notional, p)
                self.store.update_order(o["id"], status="filled", qty=n, fill_price=fp, fees=fee, filled_at=ts,
                                        notional=notional)
                self._apply_fill(o["id"], sym, side, n, quote.price, p, now, origin=origin)
                results.append(self._result(o["id"], sym, side, n, "filled", origin, ts, fill_price=fp, fees=fee,
                                            notional=notional))
            return results

    # --- accruals / margin ---------------------------------------------------

    def daily_accruals(self, now=None, params=None, prices: Optional[dict] = None) -> dict:
        """Borrow fees once per NY calendar day (catch-up for missed days
        since fund.last_accrual_date): |short MV| x rate/360 x days (ledger
        'borrow_fee', marked at the current price — the history of missed
        days is unknown). Then the dividend scan (see _book_dividends), run
        every call and deduplicated. When the account state is unreadable
        nothing is booked and no date advances (retried next cycle).
        Crypto: no borrow fee and no dividends; perp funding instead
        (``_book_funding``; the engine runs this BEFORE process_pending).
        Returns {"borrow_fees", "dividends", "days",
        "funding", "funding_windows"} (signed cash effects)."""
        pm = _Memo(prices or {})
        out = {"borrow_fees": 0.0, "dividends": 0.0, "days": 0, "funding": 0.0, "funding_windows": 0}
        with self._lock:
            now = self._now(now)
            p = self._params(params)
            ts = _iso(now)
            today = market.ny_date(now)
            try:
                _cash, rows, _n = self._read_book()
            except StateUnavailable:
                logger.warning("daily accruals: account state unreadable — retried next cycle")
                return out
            last = self.store.get_last_accrual_date()
            if last is None:
                self.store.set_last_accrual_date(today.isoformat())
            else:
                days = (today - date.fromisoformat(last[:10])).days
                if days > 0:
                    out["days"] = days
                    for pos in rows:
                        if pos["qty"] >= 0 or fund_params.is_crypto(pos["symbol"]):
                            continue            # crypto shorts are perps: funding, no borrow fee
                        rate = fund_params.borrow_rate_for(pos["symbol"], p)
                        fee = -pos["qty"] * self._mark(pos, pm) * rate / 100.0 / 360.0 * days
                        if fee > 0:
                            self.store.add_cash_entry(ts, "borrow_fee", -fee, symbol=pos["symbol"],
                                                      note=f"{days} day(s) at {rate:g} %/yr")
                            out["borrow_fees"] -= fee
                        self.store.set_borrow_rate(pos["symbol"], rate)
                    self.store.set_last_accrual_date(today.isoformat())
            if self.store.get_last_dividend_date() is None:
                self.store.set_last_dividend_date(today.isoformat())
            if self._book_dividends(now, today, ts, rows, out):
                self.store.set_last_dividend_date(today.isoformat())
            try:
                self._book_funding(now, ts, rows, p, pm, out)
            except Exception:
                logger.exception("perp funding accrual failed — retried next cycle")
            return out

    def _funding_state(self, now: datetime):
        """(last_funding_at as datetime | None, latest window <= now) or None
        when the store has no funding meta (fakes)."""
        get_fn = getattr(self.store, "get_last_funding_at", None)
        if get_fn is None or getattr(self.store, "booked_funding_keys", None) is None:
            return None
        last = get_fn()
        try:
            last_dt = _utc(last) if last else None
        except (TypeError, ValueError):
            last_dt = None
        return last_dt, market.last_funding_window(now)

    def _book_funding_symbol(self, pos: dict, now: datetime, p: dict, mark: float, ts: str,
                             booked: Optional[set] = None, fstate=None) -> Optional[Tuple[float, int]]:
        """Books the due funding windows of ONE crypto short: every window w
        with max(fund.last_funding_at, opened_at) < w <= now that is not in
        the ledger yet (dedupe key: note "fw YYYY-MM-DDTHH" per symbol and
        window), one ledger row per window, marked at ``mark``. Returns
        (amount, windows) or None when something could not be read/booked
        (the caller must then not advance fund.last_funding_at)."""
        sym = pos["symbol"]
        if float(pos.get("qty") or 0.0) >= 0 or not fund_params.is_crypto(sym):
            return 0.0, 0
        fstate = fstate or self._funding_state(now)
        if fstate is None or fstate[0] is None:
            return 0.0, 0                   # funding not initialised yet (first accrual sets it)
        last_dt, latest = fstate
        begin = last_dt
        if pos.get("opened_at"):
            try:
                begin = max(begin, _utc(pos["opened_at"]))
            except (TypeError, ValueError):
                pass
        windows = market.funding_windows_between(begin, latest)
        rate = float(p.get("crypto_funding_rate_pct_8h") or 0.0)
        if not windows or rate == 0:
            return 0.0, 0
        if booked is None:
            booked = self.store.booked_funding_keys(_iso(begin))
            if booked is None:
                return None
        total, n = 0.0, 0
        for w in windows:
            key = w.strftime("%Y-%m-%dT%H")
            if (sym, key) in booked:
                continue
            amount = -float(pos["qty"]) * float(mark) * rate / 100.0
            rid = self.store.add_cash_entry(ts, "funding", amount, symbol=sym,
                                            note=f"fw {key}: perp funding {rate:+g} %/8h on "
                                                 f"{fmt_qty(-float(pos['qty']))} ({'received' if amount >= 0 else 'paid'})")
            if rid is None:
                return None
            booked.add((sym, key))
            total += amount
            n += 1
        return total, n

    def _book_funding(self, now: datetime, ts: str, rows: List[dict], p: dict, pm: Dict[str, Any], out: dict) -> None:
        """Perp funding of crypto shorts every 8 h (00:00/08:00/16:00 UTC),
        with catch-up: every window w with max(fund.last_funding_at,
        position opened_at) < w <= now is booked once (one ledger row per
        symbol and window, deduplicated by the note key "fw YYYY-MM-DDTHH"),
        at the current mark (the history of missed windows is unknown).
        Amount per window = |short notional| x crypto_funding_rate_pct_8h /
        100; positive rate = longs pay shorts, so the short RECEIVES it
        (ledger 'funding', +); negative rate = the short pays. Spot longs pay
        no funding. fund.last_funding_at advances only when every symbol was
        booked (a failure mid-way is retried without double booking). A fill
        that changes a crypto short books its due windows first
        (``_apply_fill``). The first call only initialises the meta key."""
        set_fn = getattr(self.store, "set_last_funding_at", None)
        fstate = self._funding_state(now)
        if fstate is None or set_fn is None:
            return
        last_dt, latest = fstate
        if last_dt is None:
            set_fn(latest)
            return
        if latest <= last_dt:
            return
        booked = self.store.booked_funding_keys(_iso(last_dt))
        if booked is None:
            logger.warning("perp funding: ledger unreadable — retried next cycle")
            return
        complete = True
        for pos in rows:
            if pos["qty"] >= 0 or not fund_params.is_crypto(pos["symbol"]):
                continue
            try:
                res = self._book_funding_symbol(pos, now, p, self._mark(pos, pm), ts, booked, fstate)
            except Exception:
                logger.exception("perp funding for %s failed — retried next cycle", pos["symbol"])
                res = None
            if res is None:
                complete = False
                continue
            out["funding"] += res[0]
            out["funding_windows"] += res[1]
        if complete:
            set_fn(latest)

    def _book_dividends(self, now: datetime, today: date, ts: str, rows: List[dict], out: dict) -> bool:
        """Dividend accrual, robust against feed gaps and partial failures:

        - every call scans the ex-dates in (today - DIVIDEND_LOOKBACK_DAYS,
          today] for every symbol held now or traded (position row updated)
          within the window, so a dividend the feed lists late (the ex-date
          is often missing from the cached series on the ex-date itself) or
          a symbol whose fetch failed is picked up by a later cycle;
        - dedupe by (symbol, ex-date) against the booked ledger rows (note
          "ex YYYY-MM-DD: ..."), so nothing is ever booked twice, whatever
          failed in between; per symbol errors are caught;
        - the feed returns None when the fetch failed (retried next cycle)
          and [] when there are no dividends;
        - quantity: the shares held when the ex-date began (NY midnight) =
          current qty minus the net of the fills since then (buy/cover +,
          sell/short -). A position opened on/after the ex-date gets
          nothing; one closed after it still gets (or, short, pays) it.
          Ex-dates on or before the day of the last paper-account reset are
          ignored (positions are cleared by a reset without orders).
        Longs receive qty x amount ('dividend'), shorts pay it
        ('dividend_short', negative). Returns True when every symbol was
        scanned successfully (fund.last_dividend_date = today, informative)."""
        if not self.dividends_fn:
            return True
        window_start = today - timedelta(days=DIVIDEND_LOOKBACK_DAYS)
        booked = self.store.booked_dividend_keys(window_start.isoformat())
        if booked is None:
            logger.warning("dividend accrual: ledger unreadable — retried next cycle")
            return False
        floor_day = window_start
        reset_at = self.store.get_reset_at()
        if reset_at:
            try:
                floor_day = max(floor_day, market.ny_date(_utc(reset_at)))
            except (TypeError, ValueError):
                pass
        held = {r["symbol"]: float(r["qty"]) for r in rows if not fund_params.is_crypto(r["symbol"])}
        window_iso = _iso(datetime.combine(window_start, datetime.min.time(), tzinfo=market.NY))
        for r in self.store.get_positions(include_flat=True) or []:
            if r["symbol"] not in held and (r.get("updated_at") or "") >= window_iso \
                    and not fund_params.is_crypto(r["symbol"]):          # crypto pays no dividends
                held[r["symbol"]] = float(r["qty"] or 0.0)
        complete = True
        for sym in sorted(held):
            try:
                items = self.dividends_fn(sym, window_start)
                if items is None:
                    complete = False            # fetch failed: unknown, retried next cycle
                    continue
                for ex_date, amount in items:
                    ex = ex_date if isinstance(ex_date, date) else date.fromisoformat(str(ex_date)[:10])
                    amt = _finite(amount)
                    if amt is None or amt <= 0 or not floor_day < ex <= today:
                        continue
                    if (sym, ex.isoformat()) in booked:
                        continue
                    ex_start = datetime.combine(ex, datetime.min.time(), tzinfo=market.NY)
                    since_ex = self.store.net_filled_qty_since(sym, ex_start)
                    if since_ex is None:
                        complete = False
                        continue
                    qty = held[sym] - since_ex
                    if abs(qty) < 1e-9:
                        continue
                    cash = qty * amt
                    kind = "dividend" if qty > 0 else "dividend_short"
                    rid = self.store.add_cash_entry(ts, kind, cash, symbol=sym,
                                                    note=f"ex {ex.isoformat()}: {amt:g}/share x {qty:g}")
                    if rid is None:
                        complete = False
                        continue
                    booked.add((sym, ex.isoformat()))
                    out["dividends"] += cash
            except Exception as ex:
                complete = False
                logger.warning("dividend accrual for %s failed (%s) — retried next cycle", sym, ex)
        return complete

    def _stale_crypto_marks(self, st: dict, pm: Dict[str, Any], now: datetime) -> List[str]:
        """Held crypto symbols whose mark is not a live quote (no quote, or a
        fallback quote older than market.STALE_QUOTE_MINUTES)."""
        out = []
        for sym in st["positions"]:
            if fund_params.is_crypto(sym) and market.quote_is_stale(self._quote(sym, pm), now):
                out.append(sym)
        return sorted(out)

    def check_margin(self, now=None, params=None, prices: Optional[dict] = None) -> List[OrderResult]:
        """Margin checks after marking to market (SPEC §6):

        1. Crypto perp shorts, isolated margin per position: when posted
           margin (crypto_initial_margin x entry notional) + unrealized P&L <
           crypto_maintenance_margin x current notional, the whole short is
           liquidated (forced cover, origin 'margin-call', plus the
           liquidation fee); crypto is tradable 24/7, so it fills now.
        2. Account level: when cash - S < R = mm_eq*S_eq + mm_cr*S_cr,
           forced buy-to-cover orders (largest short first; a crypto short
           is covered completely and pays the liquidation fee) for the
           value needed to restore maintenance (fee-aware), minus covers
           already pending; equity covers are pending while the market is
           closed.
        Crypto positions without a live mark (no quote or a stale fallback
        quote, ``_stale_crypto_marks``) are never liquidated or force-covered
        on it; they are listed in ``self.margin_warnings`` (the engine marks
        the run partial). Writes 'margin_call' events. [] when fine. Raises
        StateUnavailable when the account can't be read (no forced covers
        computed from a stand-in state)."""
        pm = _Memo(prices or {})
        with self._lock:
            now = self._now(now)
            p = self._params(params)
            st = self._state(pm)
            results: List[OrderResult] = []
            stale = set(self._stale_crypto_marks(st, pm, now))
            self.margin_warnings = [f"{sym}: no live quote (stale mark) — margin/liquidation check skipped"
                                    for sym in sorted(stale)]
            pending = self.store.pending_orders()
            pending_cover: Dict[str, float] = {}
            for o in pending:
                if o["side"] == "cover":
                    pending_cover[o["symbol"]] = pending_cover.get(o["symbol"], 0.0) + float(o["qty"])

            # 1) crypto perps: isolated margin per position
            liquidated = []
            for sym, pos in sorted(st["positions"].items()):
                if pos["qty"] >= 0 or not fund_params.is_crypto(sym) or sym in stale:
                    continue
                px = st["marks"][sym]
                excess = self.perp_excess(pos, px, p)
                if excess >= 0 or px <= 0:
                    continue
                qty = round(abs(float(pos["qty"])) - pending_cover.get(sym, 0.0), fund_params.CRYPTO_QTY_DECIMALS)
                if qty <= 0:
                    continue
                r = self.submit(OrderRequest(symbol=sym, side="cover", qty=qty, origin="margin-call",
                                             rationale="perp liquidation: isolated margin below maintenance"),
                                now=now, params=p, prices=pm, allow_reduce=True)
                results.append(r)
                liquidated.append((sym, px, excess, r))
                if r.status in ("filled", "pending"):
                    pending_cover[sym] = pending_cover.get(sym, 0.0) + float(r.qty)
            if liquidated:
                body = "; ".join(f"{sym}: margin excess ${ex:,.2f} at {px:,.2f} -> cover {fmt_qty(r.qty)} "
                                 f"({r.status}{', ' + r.reject_reason if r.reject_reason else ''})"
                                 for sym, px, ex, r in liquidated)
                self.store.add_event("margin_call", "Liquidation: crypto perp short force-covered",
                                     body=f"Isolated perp margin below maintenance "
                                          f"({p['crypto_maintenance_margin_pct']:g} %). {body}. Liquidation fee "
                                          f"{p['crypto_liquidation_fee_pct']:g} % of the notional. "
                                          f"Paper trading (simulated).", ts=_iso(now))
                st = self._state(pm)

            # 2) account level: Reg T equity shorts + crypto perp maintenance
            s, cash = st["short_mv"], st["cash"]
            req = self._maintenance(st, p)
            if s <= 0 or cash - s >= req:
                return results

            def eff(sym: str) -> float:
                mm = fund_params.margin_pcts_for(sym, p)[1] / 100.0
                f = fund_params.fees_bps_for(sym, p) / 10000.0
                if fund_params.is_crypto(sym):
                    f += float(p.get("crypto_liquidation_fee_pct") or 0.0) / 100.0
                return max(mm - f, 1e-6)

            deficit = req - (cash - s)
            for o in pending:
                if o["side"] == "cover" and o["symbol"] in st["marks"]:
                    deficit -= float(o["qty"]) * st["marks"][o["symbol"]] * eff(o["symbol"])
            for sym, _px, _ex, r in liquidated:
                if r.status == "pending":
                    deficit -= float(r.qty) * st["marks"].get(sym, 0.0) * eff(sym)
            if deficit <= 0:
                return results
            shorts = sorted((pos for pos in st["positions"].values() if pos["qty"] < 0),
                            key=lambda x: x["qty"] * st["marks"][x["symbol"]])   # most negative MV first
            covers = []
            for pos in shorts:
                if deficit <= 0:
                    break
                sym = pos["symbol"]
                if sym in stale:
                    continue
                px = st["marks"][sym]
                crypto = fund_params.is_crypto(sym)
                available = abs(float(pos["qty"])) - pending_cover.get(sym, 0.0)
                available = round(available, fund_params.CRYPTO_QTY_DECIMALS) if crypto else int(available)
                if available <= 0 or px <= 0:
                    continue
                e = eff(sym)
                n = available if crypto else min(available, ceil_qty(sym, deficit / e / px))
                r = self.submit(OrderRequest(symbol=sym, side="cover", qty=n, origin="margin-call",
                                             rationale="maintenance margin call (forced cover)"),
                                now=now, params=p, prices=pm, allow_reduce=True)
                results.append(r)
                covers.append(r)
                if r.status in ("filled", "pending"):
                    deficit -= float(r.qty) * px * e
            if covers:
                body = ", ".join(f"{r.side} {fmt_qty(r.qty)} {r.symbol} ({r.status})" for r in covers)
                self.store.add_event("margin_call", "Margin call: forced cover of short positions",
                                     body=f"Equity behind shorts ${cash - s:,.2f} < maintenance ${req:,.2f}. "
                                          f"Orders: {body}. Paper trading (simulated).", ts=_iso(now))
            return results

    def snapshot_equity(self, now=None, benchmark_price: Optional[float] = None,
                        prices: Optional[dict] = None, params=None) -> dict:
        """Marks positions (stores last_price), raises the stored peak equity
        when exceeded and writes an equity_curve point (benchmark = SPY
        price). Returns the point dict. Raises StateUnavailable when the
        account can't be read (no bogus point, no peak change)."""
        pm = _Memo(prices or {})
        with self._lock:
            now = self._now(now)
            p = self._params(params)
            st = self._state(pm)
            e = st["equity"]
            peak = self.store.get_peak_equity()
            if peak is None or e > peak:
                self.store.set_peak_equity(e)
                peak = e
            dd = max(0.0, (peak - e) / peak * 100.0) if peak > 0 else 0.0
            self.store.set_last_prices(st["marks"], updated_at=now)
            ts = _iso(now)
            self.store.add_equity_point(ts, e, st["cash"], st["long_mv"], st["short_mv"], benchmark_price, dd)
            return {"ts": ts, "equity": e, "cash": st["cash"], "long_mv": st["long_mv"], "short_mv": st["short_mv"],
                    "benchmark": benchmark_price, "drawdown_pct": dd, "peak_equity": peak}


class _Memo(dict):
    """Per-call quote memo ({symbol: Quote|float|None}); a copy of the
    caller's prices so it is never mutated."""
    _memo = True
