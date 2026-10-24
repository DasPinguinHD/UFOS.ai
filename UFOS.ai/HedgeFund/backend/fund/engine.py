"""The HedgeFund loop: scheduling, cycle orchestration, halt / kill switch /
drawdown stop, events and the /fund/state payload (fund/SPEC.md §10, §11).

PROOF OF CONCEPT, PAPER TRADING ONLY — every order goes to the simulated
``PaperBroker``; nothing here is investment advice.

``FundEngine(feed, broker, store, macro_fn=, calendar_fn=, insider_fn=, clock=,
backtest_feed=)``

- ``start()`` (inside a running event loop) forces the autopilot off, runs
  the one-time crypto watchlist rollout (``fund.crypto_rollout``), books
  the starting capital on a fresh store and starts one asyncio task:
  first run ``startup_delay`` seconds after the start (``trigger="startup"``;
  a full cycle during the US session, a light risk watch outside it), then
  full cycles every ``interval_minutes`` ONLY during the US regular session
  (measured from the last cycle start). Outside the session there are no
  scheduled full cycles (``schedule_mode`` "manual_only"): only
  ``request_run()`` ("Run now") runs one — crypto orders fill at once,
  equity orders are queued for the open. While crypto positions are open
  and ``crypto_risk_watch`` is on, ``run_risk_watch`` runs every
  ``closed_interval_minutes`` outside the session: mark to market, funding,
  perp liquidation / margin check, drawdown stop, equity snapshot — no
  agents, no LLM, no new orders (trigger "risk-watch", judge_mode "none").
  Missed runs are not caught up. ``stop()`` cancels the task.
- ``run_cycle(trigger)``: one cycle at a time (lock). The params are read
  ONCE at the start (a PUT during the cycle applies from the next one).
  Blocking feed / broker / store calls run via ``asyncio.to_thread``. Errors
  of single symbols make the run ``partial``; a crash marks it ``error``
  and the loop continues; no market data at all -> ``skipped``.
- ``kill(reason)``, ``resume()``, ``set_autopilot(enabled)`` (refused while
  halted), ``update_params(partial)``, ``manual_order(...)``,
  ``execute_recommendation(id)`` (manual-accept: sizing without min-hold and
  trade limit), ``reset(capital)``, ``backtest(...)``, ``equity(since)``,
  ``state()``. User actions write ``fund_audit`` rows (actor "user"),
  engine actions actor "engine".
- Events: ``flip`` (a symbol's action changed), ``order`` (autopilot orders,
  filled or pending, and pending autopilot orders filled at the open),
  ``halt`` (kill switch / drawdown stop), ``resume``, ``autopilot``,
  ``error`` (cycle crash); ``margin_call`` is written by the broker.

Guardrails (review 2026-09-26):

- Dead-man switch: ``touch_client()`` is called by the API on every
  authenticated GET /fund/state and GET /fund/events. When the autopilot is
  on and no client was seen for ``autopilot_heartbeat_minutes`` the engine
  switches it off (audit actor "engine", event "autopilot"). This keeps
  "autopilot off after each start" / "visible kill switch" true while the
  backend may outlive the window (SPEC §14 detached lifetime); a future truly
  headless mode would replace it with an explicit opt-in.
- Kill-switch ordering: a ``threading.Lock`` (``_trade_lock``) serialises the
  halt + liquidation with every non-protective order placement (autopilot
  target, manual order, manual accept), which re-check the halt inside it —
  no non-protective order is placed after a kill switch.
- Unreadable account state (``broker.account`` raises or has no equity): the
  cycle skips the drawdown check, the margin actions and the autopilot
  orders (run ``partial`` with the reason); it never liquidates on it.
- Audit: user actions write their audit row FIRST and are refused with 503
  "audit log unavailable" when it cannot be written — except the kill switch
  and switching the autopilot off, which always work (error logged).
  Autopilot orders and their fills at the open are audited (actor "engine",
  action "autopilot_order").

The newsroom accessors (``macro_fn`` -> main.py's ``macro_latest`` dict,
``calendar_fn`` -> calendar events list, ``insider_fn`` -> insider events)
are called in the event-loop thread (they read in-memory caches).
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import indicators as ind
from . import judge as judge_mod
from strategy_platform.core import llm as llm_mod
from strategy_platform.data import market_feed as market
from strategy_platform.core import params as fund_params
from . import sizing
from strategy_platform.strategy.features import macro_agent
from .agents import risk as risk_agent
from .agents import sentiment as sentiment_agent
from .agents import technical as technical_agent
from strategy_platform.execution.paper_broker import PROTECTIVE_ORIGINS, ValuationPaused, fmt_qty

VALUATION_PAUSED_LOG_SECONDS = 600.0            # repeat the "valuation paused" warning at most every 10 min
_VP_LOCK = threading.Lock()
from strategy_platform.core.errors import UserFacingError

from strategy_platform.core.types import (ORDER_SIDES, AccountView, AgentSignal, JudgeDecision, MarketContext,
                                         OrderRequest, OrderResult, PositionView, Quote, RiskAssessment)

logger = logging.getLogger(__name__)

BENCHMARK = "SPY"
BARS_DAYS = 400
STARTUP_DELAY_SECONDS = 20.0
FETCH_CONCURRENCY = 4
OPEN_GRACE_SECONDS = 5
AGENT_ORDER = ("technical", "macro", "sentiment", "risk")
DISCLAIMER = "Paper trading (simulated) — not investment advice."
BACKTEST_DEFAULT_YEARS = 5
BACKTEST_WARMUP_DAYS = 400          # calendar days of indicator warm-up before the start
MANUAL_QTY_MAX = 10_000_000
MANUAL_NOTIONAL_MAX = 1_000_000_000.0
BACKTEST_MAX_SYMBOLS = 10
BACKTEST_MAX_YEARS = 20
HEARTBEAT_POLL_SECONDS = 60.0        # the loop checks the dead-man switch at least this often
AUDIT_UNAVAILABLE = "audit log unavailable — the action was not carried out"
STALE_ORDER_REASON = "stale — re-evaluated at the next run"
# Stage 7 (strategy_platform/SPEC.md §17.1): after the migration of the paper account to the Strategy Platform the
# old engine is advisory only (agents + judge + recommendations) and never trades this account again.
MIGRATED_MESSAGE = ("This paper account was migrated to the Strategy Platform on {date} — it is traded in "
                    "Portfolio & Trading now.")
# the risk watch fills protective orders and the user's own queued orders (crypto is tradable)
RISK_WATCH_FILL_ORIGINS = PROTECTIVE_ORIGINS + ("manual", "manual-accept")
ACCOUNT_FIELDS = tuple(f.name for f in dataclasses.fields(AccountView))


class EngineError(Exception):
    """A refused / invalid request. ``status`` is the HTTP status the API
    answers with (400 invalid input, 404 unknown id, 409 refused, 503 store
    unavailable); ``errors`` maps field -> message for 400."""

    def __init__(self, status: int, message: str, errors: Optional[Dict[str, str]] = None):
        super().__init__(message)
        self.status = int(status)
        self.message = message
        self.errors = errors


# --- small helpers -------------------------------------------------------------

def _history_error(start) -> Optional[str]:
    """fund/backtest.py's HISTORY_ERROR for a start before 2016, else None."""
    from .backtest import check_history_start      # lazy, like the backtest itself
    return check_history_start(start)


def _utc(dt) -> datetime:
    if dt is None:
        return datetime.now(timezone.utc)
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt) -> Optional[str]:
    return None if dt is None else _utc(dt).isoformat(timespec="seconds")


def _finite(x) -> Optional[float]:
    if isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError, OverflowError):
        return None
    return v if math.isfinite(v) else None


def json_safe(obj):
    """JSON-able copy of ``obj`` without NaN/Infinity (-> None): dataclasses
    and objects with to_dict() become dicts, datetimes ISO strings, tuples/sets
    lists. The .NET client rejects non-finite numbers."""
    if obj is None or isinstance(obj, (str, bool)):
        return obj
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [json_safe(v) for v in obj]
    if isinstance(obj, datetime):
        return _iso(obj)
    if isinstance(obj, date):
        return obj.isoformat()
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return json_safe(dataclasses.asdict(obj))
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        return json_safe(to_dict())
    try:
        f = float(obj)            # numpy / decimal scalars
        return f if math.isfinite(f) else None
    except (TypeError, ValueError, OverflowError):
        return str(obj)


def _quote_dict(q: Optional[Quote]) -> Optional[dict]:
    if q is None:
        return None
    return {"price": q.price, "prev_close": q.prev_close, "change_pct": q.change_pct, "delayed": bool(q.delayed),
            "ts": q.ts}


def _signal_dict(sig: AgentSignal) -> dict:
    d = sig.to_dict()
    return {k: d.get(k) for k in ("agent", "symbol", "score", "confidence", "summary", "details", "source",
                                  "provenance", "usage")}


def _source_label(source: Optional[str], model: Optional[str], provenance) -> str:
    if isinstance(provenance, dict) and provenance.get("ai_generated"):
        return f"AI-generated ({model or provenance.get('model') or 'model'})"
    return "Rule-based (no AI)"


def _cancel_text(cancelled) -> str:
    """Event sentence about the queued autopilot orders cancelled with the autopilot."""
    n = len(cancelled or [])
    if n == 0:
        return "No queued autopilot orders to cancel."
    names = ", ".join(f"{str(o.get('side', '')).upper()} {fmt_qty(o.get('qty', 0))} {o.get('symbol', '')}"
                      for o in list(cancelled)[:5])
    return (f"{n} queued autopilot order{'s' if n != 1 else ''} cancelled ({names}"
            f"{', …' if n > 5 else ''}); manual and protective orders are kept.")


def _order_title(r: OrderResult, prefix: str) -> str:
    text = f"{prefix}: {r.side.upper()} {fmt_qty(r.qty)} {r.symbol} {r.status}"
    if r.status == "filled" and r.fill_price is not None:
        text += f" @ {r.fill_price:,.2f}"
    return text


# --- the engine ------------------------------------------------------------------


def switchover_refusal() -> Optional[str]:
    """Why the old engine may not run (SP §22.8): the Council switch-over is pending / done, or its state is
    unreadable (fail closed); None when it may run (none / reverted, or no hedgefund.db open at all — then the
    Council cannot exist either and the old engine runs as before)."""
    try:
        from strategy_platform.council import store as cl_store
        from strategy_platform.storage import db as sp_db
        if sp_db.db_path() is None:
            return None
        state = cl_store.switchover()
    except Exception as ex:      # noqa: BLE001
        return f"the Council switch-over state is unreadable ({type(ex).__name__})"
    if state in ("pending", "done"):
        return "the old engine was replaced by the Council strategy"
    return None

class FundEngine:
    """See the module docstring."""

    def __init__(self, feed, broker, store, *, macro_fn: Optional[Callable[[], dict]] = None,
                 calendar_fn: Optional[Callable[[], list]] = None, insider_fn: Optional[Callable[[], list]] = None,
                 clock: Optional[Callable[[], datetime]] = None, llm: Optional[Callable] = None,
                 session_fn: Optional[Callable] = None, startup_delay: float = STARTUP_DELAY_SECONDS,
                 backtest_feed=None, macro_status_fn: Optional[Callable[[], Optional[dict]]] = None):
        self.feed = feed
        # Backtests need years of bars; the prices cache is pruned to 2 years, so
        # main.py passes a feed without the SQLite cache here (default: feed).
        self.backtest_feed = backtest_feed or feed
        self.broker = broker
        self.store = store
        self._macro_fn = macro_fn or (lambda: {})
        # FRED source status from the newsroom ({"ok", "message", ...}); not ok -> the macro
        # agent is unavailable (user decision 2026-10-02: no stored FRED data as a trading input
        # while FRED fails or its key is missing)
        self._macro_status_fn = macro_status_fn
        self._calendar_fn = calendar_fn or (lambda: [])
        self._insider_fn = insider_fn or (lambda: [])
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.llm = llm                      # None -> core.llm.chat_json (tests: llm.TRANSPORT)
        self.session_fn = session_fn or market.session_state
        self.startup_delay = float(startup_delay)

        self.next_run_at: Optional[datetime] = None          # next scheduled FULL cycle (US session only)
        self.next_risk_watch_at: Optional[datetime] = None   # next risk-watch check (outside the session)
        self.cycle_in_progress = False
        self._task: Optional[asyncio.Task] = None
        self._stopping = False
        self._wake_pending = False
        self._loop_ref = None
        self._lock_obj: Optional[asyncio.Lock] = None
        self._event_obj: Optional[asyncio.Event] = None
        self._current_run: Optional[dict] = None
        # sandbox profile (strategy_platform/SPEC.md "Sandbox profile"; set by main.py): autopilot + scheduled AI
        # cycles off — the reason is the 409 text of "autopilot on"; "Run now" still works (inside the sandbox)
        self.sandbox_off_reason: Optional[str] = None

        self._sentiment_cache: Dict[str, dict] = {}        # owned here, see agents/sentiment.py
        self._last_positions: Optional[Dict[str, float]] = None   # qty per symbol after the previous cycle
        self._last_quotes: Dict[str, Quote] = {}
        self._last_agents: Dict[str, List[dict]] = {}      # symbol -> agent dicts of the last analysed run
        self._agents_loaded = False
        self._run_versions: Dict[int, int] = {}
        self._params_cache: Dict[int, Optional[dict]] = {}
        self._bench_base: Optional[Tuple[Optional[str], Optional[float]]] = None
        self._last_rule_actions: Dict[str, str] = {}      # symbol -> rule action (prompt input) of the last cycle
        # Per-symbol outcome of the last FULL cycle for /fund/state (D9, 2026-10-02):
        # symbol -> {"judge_unavailable": msg|None, "data_unavailable": msg|None, "run_id", "at"}
        self._symbol_status: Dict[str, dict] = {}
        self._symbol_status_loaded = False
        self._judge_retry: set = set()                    # symbols whose last judgement attempt failed
        self._valuation_paused: Optional[str] = None     # "valuation paused: no current quote for X" (last cycle)
        # Serialises halt + liquidation with non-protective order placement
        # (broker calls run in worker threads, so a threading lock).
        self._trade_lock = threading.Lock()
        self._last_client_seen: Optional[datetime] = None  # dead-man switch (touch_client)
        self._backtest_running = False
        # Stage 7 (strategy_platform SPEC §17.1), set by main.py: the platform half of the kill switch
        # (``platform_kill_fn(reason, actor) -> dict``) and the platform's view of the migration
        # (``platform_migrated_fn() -> dict|None``; failures are logged and ignored — the newsroom.db flag rules).
        self.platform_kill_fn: Optional[Callable[[str, str], dict]] = None
        self.platform_migrated_fn: Optional[Callable[[], Optional[dict]]] = None
        self._migrated_seen: Optional[dict] = None

    # --- asyncio primitives (bound to the running loop; tests use several loops) --

    def _prims(self):
        loop = asyncio.get_running_loop()
        if self._loop_ref is not loop:
            self._loop_ref = loop
            self._lock_obj = asyncio.Lock()
            self._event_obj = asyncio.Event()
            if self._wake_pending:
                self._event_obj.set()
        return self._lock_obj, self._event_obj

    def _now(self) -> datetime:
        return _utc(self.clock())

    def _session(self, now: datetime) -> dict:
        try:
            return dict(self.session_fn(now))
        except Exception as ex:
            logger.warning("session state failed (%s) — treating the market as closed", ex)
            return {"open": False, "session": "closed", "next_open": None, "next_close": None,
                    "trading_day": market.ny_date(now).isoformat()}

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # --- audit helpers ---------------------------------------------------------------

    def _audit(self, actor: str, action: str, detail=None, now=None) -> Optional[int]:
        """Best-effort audit row; logs an error when it cannot be written."""
        try:
            rid = self.store.add_audit(actor, action, detail, now or self._now())
        except Exception:
            logger.exception("audit row %s/%s failed", actor, action)
            rid = None
        if rid is None:
            logger.error("AUDIT LOG UNAVAILABLE: could not record %s/%s", actor, action)
        return rid

    def _audit_required(self, actor: str, action: str, detail=None, now=None) -> int:
        """Audit row written BEFORE a state-changing user action; refuses the
        action (503) when the audit log is unavailable."""
        rid = self._audit(actor, action, detail, now)
        if rid is None:
            raise EngineError(503, AUDIT_UNAVAILABLE)
        return rid

    # --- stage 7: migrated (frozen) account ------------------------------------------------

    def migration_state(self) -> Optional[dict]:
        """{status: migrating|migrated, migration_id, at} once the paper account was (being) migrated to the
        Strategy Platform (newsroom.db flag, or the platform's record); None before. Remembered for the
        process lifetime once seen (fail closed: never trade a migrated account after a flaky read)."""
        if self._migrated_seen is not None and self._migrated_seen.get("status") == "migrated":
            return dict(self._migrated_seen)
        state = None
        getter = getattr(self.store, "get_migration_state", None)
        if getter is not None:
            try:
                state = getter()
            except Exception:
                logger.exception("migration flag unreadable")
        if state is None and self.platform_migrated_fn is not None:
            try:
                state = self.platform_migrated_fn()
            except Exception:
                logger.exception("platform migration state unreadable")
        if state is not None:
            self._migrated_seen = dict(state)
        elif self._migrated_seen is not None:
            return dict(self._migrated_seen)
        return state

    def _refuse_if_migrated(self) -> None:
        st = self.migration_state()
        if st is not None:
            raise EngineError(409, MIGRATED_MESSAGE.format(date=str(st.get("at") or "")[:10] or "?"))

    # --- dead-man switch (client heartbeat) ---------------------------------------------

    def touch_client(self, now=None) -> None:
        """Marks client activity (the API calls it on authenticated GET
        /fund/state and GET /fund/events)."""
        self._last_client_seen = _utc(now) if now is not None else self._now()

    @property
    def last_client_seen(self) -> Optional[datetime]:
        return self._last_client_seen

    @staticmethod
    def _heartbeat_minutes(params: Optional[dict]) -> float:
        lim = fund_params.LIMITS["autopilot_heartbeat_minutes"]
        v = _finite((params or {}).get("autopilot_heartbeat_minutes"))
        if v is None:
            v = float(fund_params.DEFAULTS["autopilot_heartbeat_minutes"])
        return max(float(lim["min"]), min(float(lim["max"]), v))

    def check_heartbeat(self, now=None, params: Optional[dict] = None) -> bool:
        """Dead-man switch (guardrail; sync). Switches the autopilot off when
        it is on and no client was seen for ``autopilot_heartbeat_minutes``.
        Returns True when it switched the autopilot off."""
        try:
            if not self.store.get_autopilot():
                return False
        except Exception:
            logger.exception("heartbeat: autopilot flag unreadable")
            return False
        now = _utc(now) if now is not None else self._now()
        if params is None:
            try:
                _v, params = self.store.effective_params()
            except Exception:
                params = {}
        minutes = self._heartbeat_minutes(params)
        seen = self._last_client_seen
        if seen is not None and (now - seen) < timedelta(minutes=minutes):
            return False
        cancelled = self._autopilot_off_and_cancel("cancelled: autopilot off (no app heartbeat)")
        since = _iso(seen) if seen is not None else None
        self._audit("engine", "autopilot_heartbeat_off",
                    {"last_client_seen": since, "timeout_minutes": minutes,
                     "cancelled_orders": [o.get("id") for o in cancelled]}, now)
        self.store.add_event(
            "autopilot", f"Autopilot switched off: no app heartbeat for {minutes:g} min", None,
            f"The HedgeFund window has not polled the engine since {since or 'the backend start'}. Guardrail: "
            f"the autopilot only trades while the app is watching (dead-man switch, "
            f"autopilot_heartbeat_minutes = {minutes:g}). {_cancel_text(cancelled)} Open positions are kept; "
            f"switch the autopilot on again explicitly. {DISCLAIMER}", now)
        logger.warning("autopilot switched off: no client heartbeat since %s (limit %g min)", since, minutes)
        return True

    def _autopilot_off_and_cancel(self, reason: str) -> List[dict]:
        """Autopilot flag off + cancel the queued (unfilled) autopilot orders,
        under ``_trade_lock`` so no cycle places a new one in between. Manual
        and protective pending orders stay. Returns the cancelled rows."""
        with self._trade_lock:
            self.store.set_autopilot(False)
            try:
                cancel = getattr(self.broker, "cancel_pending", None)
                if cancel is not None:
                    return list(cancel("autopilot", reason) or [])
                out = []
                for o in self.store.pending_orders() or []:
                    if o.get("origin") == "autopilot" and self.store.update_order(o["id"], status="cancelled",
                                                                                 reject_reason=reason):
                        out.append(o)
                return out
            except Exception:
                logger.exception("could not cancel the pending autopilot orders")
                return []

    # --- account state -------------------------------------------------------------------

    def _read_account(self, pm, now, params) -> Tuple[Optional[AccountView], Optional[str]]:
        """(AccountView, None) or (None, reason) when the paper account state
        is unreadable (broker raised, e.g. a StateUnavailable, or no equity)."""
        try:
            acct = self.broker.account(pm, now, params)
        except ValuationPaused as ex:          # no current quote for a held position (no stored-price mark)
            self._log_valuation_paused(ex)
            return None, str(ex)[:300]
        except Exception as ex:
            logger.error("paper account state unreadable: %s: %s", type(ex).__name__, ex)
            return None, f"{type(ex).__name__}: {ex}"[:200]
        if acct is None or _finite(getattr(acct, "equity", None)) is None:
            return None, "equity unavailable"
        with _VP_LOCK:
            self._vp_logged = None                # valued again: a new pause is logged at once
        return acct, None

    def _log_valuation_paused(self, ex) -> None:
        """Click-test 2026-10-22: the status poll reads the account every 5 s — the pause is logged once per
        change of the symbol set, then at most every ``VALUATION_PAUSED_LOG_SECONDS`` while it persists (the UI
        state is unchanged)."""
        import time as _t
        key = tuple(getattr(ex, "symbols", None) or [str(ex)])
        now = _t.monotonic()
        with _VP_LOCK:
            last = getattr(self, "_vp_logged", None)
            if last is not None and last[0] == key and now - last[1] < VALUATION_PAUSED_LOG_SECONDS:
                return
            self._vp_logged = (key, now)
        logger.warning("%s", ex)

    # --- lifecycle ---------------------------------------------------------------

    def start(self) -> asyncio.Task:
        """Starts the loop task (call inside the running event loop). Forces
        the autopilot off (SPEC §2: at every backend start)."""
        if self.running:
            return self._task
        # SP §22.8 (R2): never again after the Council switch-over (pending / done) — also when the state is unreadable
        why = switchover_refusal()
        if why or getattr(self, "_switchover_refused", False):
            raise RuntimeError(why or "the old engine was replaced by the Council strategy")
        try:
            was_on = self.store.get_autopilot()
            self.store.set_autopilot(False)
            if was_on:
                self._audit("system", "autopilot_forced_off", {"reason": "backend start"})
        except Exception:
            logger.exception("could not reset the autopilot flag")
        self.crypto_rollout()
        try:
            self.broker.ensure_initialized(self._now())
        except Exception:
            logger.exception("paper account initialisation failed")
        self._stopping = False
        self._prims()
        self.next_run_at = self._now() + timedelta(seconds=self.startup_delay)
        self._task = asyncio.get_running_loop().create_task(self._loop(), name="fund-engine")
        logger.info("HedgeFund engine started (paper trading only); first cycle in %.0f s", self.startup_delay)
        return self._task

    def crypto_rollout(self, now=None) -> Optional[int]:
        """One-time params migration (user decision 2026-09-26): when meta
        ``fund.crypto_rollout`` is not set and the latest stored watchlist
        contains none of BTC-USD / ETH-USD / SOL-USD, a new params version
        (actor "system") appends them (as many as the 25-symbol cap allows),
        writes an audit row and sets the flag. A fresh install (no stored
        version yet) stores the crypto-inclusive DEFAULTS explicitly as
        version 1 (actor "system", note "initial parameters (defaults incl.
        BTC-USD, ETH-USD, SOL-USD)"), so the watchlist the engine trades is
        always a stored, auditable version. Returns the new version or None.
        Never raises."""
        getter = getattr(self.store, "get_crypto_rollout", None)
        setter = getattr(self.store, "set_crypto_rollout", None)
        if getter is None or setter is None:
            return None
        try:
            if getter():
                return None
            now = _utc(now) if now is not None else self._now()
            latest = self.store.latest_params()
            if latest is None or not isinstance(latest.get("params"), dict):
                # fresh install: store the crypto-inclusive defaults as version 1
                version = self.store.save_params(fund_params.defaults(), "system",
                                                 "initial parameters (defaults incl. "
                                                 + ", ".join(fund_params.CRYPTO_ROLLOUT_SYMBOLS) + ")")
                if version is None:
                    logger.error("crypto rollout: params store unavailable — retried at the next start")
                    return None
                self._audit("system", "crypto_rollout", {"fresh_install": True, "new_version": version,
                                                         "added": list(fund_params.CRYPTO_ROLLOUT_SYMBOLS)}, now)
                setter(now)
                return version
            current = fund_params.merge(latest["params"], None)
            wl = list(current.get("watchlist") or [])
            wanted = list(fund_params.CRYPTO_ROLLOUT_SYMBOLS)
            if any(sym in wl for sym in wanted):
                setter(now)                 # the user already has (some of) them
                return None
            added = wanted[:max(0, fund_params.WATCHLIST_MAX - len(wl))]
            version = None
            if added:
                new = fund_params.merge(current, {"watchlist": wl + added})
                version = self.store.save_params(new, "system", "crypto rollout: added " + ", ".join(added))
                if version is None:
                    logger.error("crypto rollout: params store unavailable — retried at the next start")
                    return None
            self._audit("system", "crypto_rollout",
                        {"added": added, "not_added_watchlist_full": [x for x in wanted if x not in added],
                         "base_version": latest.get("version"), "new_version": version}, now)
            setter(now)
            if added:
                logger.info("crypto rollout: watchlist v%s + %s", version, ", ".join(added))
            return version
        except Exception:
            logger.exception("crypto rollout failed — retried at the next start")
            return None

    async def stop(self) -> None:
        self._stopping = True
        task = self._task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None

    def request_run(self, actor: Optional[str] = None) -> dict:
        """Wakes the loop now (a cycle in progress is followed by one more).
        A user request is audited first (503 when the audit log is down)."""
        if actor:
            self._audit_required(actor, "run_now", {"cycle_in_progress": self.cycle_in_progress})
        self._wake_pending = True
        if self._event_obj is not None:
            try:
                self._event_obj.set()
            except Exception:
                pass
        return {"queued": True}

    async def _loop(self) -> None:
        startup = True
        while not self._stopping:
            _lock, wake = self._prims()
            if not self._wake_pending:
                due = [t for t in (self.next_run_at, self.next_risk_watch_at) if t is not None]
                timeout = max(0.0, (min(due) - self._now()).total_seconds()) if due else HEARTBEAT_POLL_SECONDS
                # wake up at least every HEARTBEAT_POLL_SECONDS for the dead-man switch
                timeout = min(timeout, HEARTBEAT_POLL_SECONDS)
                try:
                    await asyncio.wait_for(wake.wait(), timeout)
                except asyncio.TimeoutError:
                    pass
            if self._stopping:
                break
            try:
                await asyncio.to_thread(self.check_heartbeat)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("heartbeat check failed")
            now = self._now()
            woken = self._wake_pending
            run_due = (self.next_run_at is None or now >= self.next_run_at) and not self.sandbox_off_reason
            watch_due = self.next_risk_watch_at is not None and now >= self.next_risk_watch_at
            if not woken and not run_due and not watch_due:
                continue                    # heartbeat poll only; nothing is due yet
            self._wake_pending = False
            wake.clear()
            try:
                if woken:
                    await self.run_cycle("manual")          # "Run now": a full cycle, also outside the session
                    startup = False
                elif run_due:
                    session = self._session(now)
                    if session.get("open"):
                        await self.run_cycle("startup" if startup else "schedule")
                    else:
                        # US session closed: no scheduled full cycles (SPEC §10) — the next one
                        # is at the open; crypto is covered by the risk watch and "Run now".
                        _v, params = await asyncio.to_thread(self.store.effective_params)
                        self.next_run_at = self._next_after(now, params, session)
                        if startup:
                            await self.run_risk_watch("startup", force=True)
                    startup = False
                else:
                    await self.run_risk_watch("risk-watch")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("fund cycle failed unexpectedly")
            now = self._now()
            if not self.sandbox_off_reason and (self.next_run_at is None or self.next_run_at <= now):
                # the cycle took longer than the interval (or had no params): no catch-up
                self.next_run_at = now + timedelta(minutes=5)
            if self.next_risk_watch_at is not None and self.next_risk_watch_at <= now:
                self.next_risk_watch_at = now + timedelta(minutes=5)

    # --- scheduling ----------------------------------------------------------------

    @staticmethod
    def _interval_minutes(params: dict, session: Optional[dict] = None, key: str = "interval_minutes") -> float:
        v = _finite(params.get(key)) or fund_params.DEFAULTS[key]
        return max(1.0, float(v))

    def _next_after(self, start: datetime, params: dict, session: dict) -> datetime:
        """Next scheduled FULL cycle: start + interval_minutes during the US
        session; outside it the next session open (+ a few seconds) — there
        are no scheduled full cycles while the US market is closed."""
        if session.get("open"):
            return start + timedelta(minutes=self._interval_minutes(params))
        try:
            nopen = _utc(session["next_open"]) if session.get("next_open") else None
        except (TypeError, ValueError):
            nopen = None
        if nopen is None or nopen <= start:
            return start + timedelta(minutes=self._interval_minutes(params, key="closed_interval_minutes"))
        return nopen + timedelta(seconds=OPEN_GRACE_SECONDS)

    def _next_risk_watch(self, start: datetime, params: dict) -> datetime:
        return start + timedelta(minutes=self._interval_minutes(params, key="closed_interval_minutes"))

    # --- the cycle ---------------------------------------------------------------------

    async def run_cycle(self, trigger: str = "manual") -> dict:
        """One full cycle (see module docstring). Never raises (except
        cancellation); returns {"run_id", "status", ...}."""
        lock, _ = self._prims()
        async with lock:
            self.cycle_in_progress = True
            start = self._now()
            run_id = None
            try:
                version, params = await asyncio.to_thread(self.store.effective_params)
                try:
                    await asyncio.to_thread(self.check_heartbeat, start, params)
                except Exception:
                    logger.exception("heartbeat check failed")
                session = self._session(start)
                self.next_run_at = self._next_after(start, params, session)
                self.next_risk_watch_at = self._next_risk_watch(start, params)
                run_id = await asyncio.to_thread(self.store.create_run, start, version, trigger, bool(session.get("open")))
                if run_id is not None:
                    self._run_versions[run_id] = version
                self._current_run = {"id": run_id, "started_at": _iso(start), "params_version": version}
                out = await self._cycle(run_id, start, params, session)
                await asyncio.to_thread(self.store.finish_run, run_id, out["status"], self._now(),
                                        out.get("judge_mode"), out.get("models"), out.get("cost_usd", 0.0),
                                        out.get("error"), out.get("summary"))
                out["run_id"] = run_id
                return out
            except asyncio.CancelledError:
                if run_id is not None:
                    self.store.finish_run(run_id, "error", self._now(), error="cancelled (backend shutting down)")
                raise
            except Exception as ex:
                logger.exception("fund cycle %s crashed", run_id)
                msg = f"{type(ex).__name__}: {ex}"[:500]
                try:
                    await asyncio.to_thread(self.store.finish_run, run_id, "error", self._now(), None, None, 0.0, msg,
                                            {"flips": []})
                    await asyncio.to_thread(self.store.add_event, "error", "Engine cycle failed", None,
                                            f"Run #{run_id}: {msg}. The loop continues with the next schedule.",
                                            self._now())
                except Exception:
                    logger.exception("could not record the failed run")
                return {"run_id": run_id, "status": "error", "error": msg}
            finally:
                self.cycle_in_progress = False
                self._current_run = None

    async def _fetch(self, symbols: List[str], news_symbols: List[str], quote_only=()):
        sem = asyncio.Semaphore(FETCH_CONCURRENCY)
        bars: Dict[str, list] = {}
        quotes: Dict[str, Optional[Quote]] = {}
        news: Dict[str, list] = {}
        errors: Dict[str, str] = {}

        async def one(sym: str):
            async with sem:
                try:
                    bars[sym] = [] if sym in quote_only else \
                        list(await asyncio.to_thread(self.feed.daily_bars, sym, BARS_DAYS) or [])
                except Exception as ex:
                    bars[sym] = []
                    errors[sym] = f"daily bars failed: {getattr(ex, 'user_message', None) or ex}"[:200]
                try:
                    q = await asyncio.to_thread(self.feed.quote, sym)
                    quotes[sym] = q if q is not None and (_finite(q.price) or 0) > 0 else None
                except Exception as ex:
                    quotes[sym] = None
                    msg = f"quote failed: {getattr(ex, 'user_message', None) or ex}"
                    errors[sym] = (f"{errors[sym]}; {msg}" if sym in errors else msg)[:300]
                if sym in news_symbols:
                    try:
                        news[sym] = list(await asyncio.to_thread(self.feed.news, sym, 8) or [])
                    except Exception as ex:
                        # not "no headlines": the sentiment agent reports "News could not be
                        # loaded: …" for this symbol and makes no LLM call (no fallback)
                        logger.warning("news for %s failed: %s", sym, ex)
                        news[sym] = [{"error": getattr(ex, "user_message", None) or "news fetch failed"}]

        await asyncio.gather(*(one(s) for s in symbols))
        return bars, quotes, news, errors

    def _remember_quotes(self, quotes: Dict[str, Optional[Quote]]) -> None:
        """The quotes /fund/state shows: this run's; a symbol whose quote failed
        is dropped (never shown/valued at an older quote — 2026-10-02)."""
        for s, q in quotes.items():
            if q is None:
                self._last_quotes.pop(s, None)
            else:
                self._last_quotes[s] = q

    def _position_qtys(self) -> Dict[str, float]:
        return {p["symbol"]: float(p["qty"]) for p in (self.store.get_positions() or [])}

    def _newsroom(self):
        """(macro ctx, calendar, insider, calendar_error, insider_error). A
        failing accessor is reported as an error, never replaced by an empty
        list (user decision 2026-10-02): the risk card / sentiment input says
        that data is unavailable."""
        def read(fn, what):
            try:
                v = fn()
                return (v if v is not None else []), None
            except Exception as ex:
                logger.warning("newsroom %s accessor failed: %s", what, ex)
                return [], f"{what} accessor failed ({type(ex).__name__})"
        calendar, cal_err = read(self._calendar_fn, "calendar")
        if isinstance(calendar, dict):
            calendar = calendar.get("events") or []
        insider, ins_err = read(self._insider_fn, "insider")
        return self._macro_context(), list(calendar), list(insider), cal_err, ins_err

    def _macro_context(self) -> dict:
        """ctx.macro for the agents. FRED down / key missing / accessor error ->
        ``{"available": False, "unavailable_reason": "Macro data unavailable: …"}``
        (the macro agent reports it; the newsroom's own display may keep its
        stored data — that is not a trading input)."""
        reason = None
        if self._macro_status_fn is not None:
            try:
                st = self._macro_status_fn() or {}
            except Exception as ex:
                st = {"ok": False, "message": f"FRED status unreadable ({type(ex).__name__})"}
            if st.get("ok") is not True:
                msg = str(st.get("message") or ("not loaded yet" if st.get("ok") is None else "FRED failed"))
                reason = f"Macro data unavailable: {msg[:200]}"
        if reason is None:
            try:
                return macro_agent.build_macro_context(self._macro_fn() or {})
            except Exception as ex:
                logger.warning("macro accessor failed: %s", ex)
                reason = f"Macro data unavailable: the newsroom macro data could not be read ({type(ex).__name__})"
        return {"available": False, "as_of": None, "regime": None, "readings": {}, "unavailable_reason": reason}

    def _budget(self, params: dict) -> llm_mod.Budget:
        return llm_mod.Budget(params.get("ai_daily_budget_usd", 0.0), self.store.ai_spend_today,
                              self.store.add_ai_spend)

    def _llm_kw(self) -> dict:
        return {"llm": self.llm} if self.llm is not None else {}

    def _housekeeping(self, now, params, pm, session, protective_only: bool = False) -> dict:
        """Broker steps of a cycle (sync, in a worker thread): accruals FIRST
        (funding/borrow of the positions as they were, before any queued
        fill changes them), then pending orders per symbol (crypto 24/7,
        equities in the session — the broker decides), then the margin
        check. ``protective_only`` (risk watch): only protective and user
        (manual / manual-accept) orders are filled, and queued AUTOPILOT
        crypto orders older than 2 x the risk-watch interval are cancelled
        ("stale — re-evaluated at the next run")."""
        out = {"fills": [], "accruals": None, "margin": [], "errors": {}, "expired": []}
        try:
            out["accruals"] = self.broker.daily_accruals(now, params, pm)
        except Exception as ex:
            logger.exception("daily accruals failed")
            out["errors"]["broker:accruals"] = str(ex)[:200]
        if protective_only:
            expire = getattr(self.broker, "expire_pending", None)
            if expire is not None:
                try:
                    age = timedelta(minutes=2 * self._interval_minutes(params, key="closed_interval_minutes"))
                    with self._trade_lock:
                        out["expired"] = list(expire("autopilot", age, now, STALE_ORDER_REASON) or [])
                    if out["expired"]:
                        self._audit("engine", "autopilot_orders_expired",
                                    {"order_ids": [o.get("id") for o in out["expired"]], "reason": STALE_ORDER_REASON,
                                     "older_than_minutes": age.total_seconds() / 60}, now)
                        self.store.add_event(
                            "autopilot", f"{len(out['expired'])} stale queued crypto autopilot order(s) cancelled",
                            None, f"{_cancel_text(out['expired']).replace('queued autopilot order', 'order')} "
                                  f"They were queued more than {age.total_seconds() / 60:g} min ago and are "
                                  f"re-evaluated at the next full run. {DISCLAIMER}", now)
                except Exception as ex:
                    logger.exception("expiring stale autopilot orders failed")
                    out["errors"]["broker:expire"] = str(ex)[:200]
        try:
            if protective_only:
                out["fills"] = self.broker.process_pending(now, params, pm, RISK_WATCH_FILL_ORIGINS)
            else:
                out["fills"] = self.broker.process_pending(now, params, pm)
        except Exception as ex:
            logger.exception("process_pending failed")
            out["errors"]["broker:pending"] = str(ex)[:200]
        acct, why = self._read_account(pm, now, params)
        if acct is None:
            # never force covers on an unreadable account state
            out["errors"]["broker:margin"] = f"margin check skipped — account state unreadable ({why})"
        else:
            try:
                out["margin"] = self.broker.check_margin(now, params, pm) or []
                warnings = list(getattr(self.broker, "margin_warnings", None) or [])
                if warnings:
                    logger.warning("margin check: %s", "; ".join(warnings))
                    out["errors"]["broker:stale_quotes"] = "; ".join(warnings)[:500]
                if out["margin"]:
                    self._audit("engine", "margin_call", {"orders": [r.to_dict() for r in out["margin"]]}, now)
            except Exception as ex:
                logger.exception("margin check failed")
                out["errors"]["broker:margin"] = str(ex)[:200]
        for r in out["fills"] or []:
            if r.origin == "autopilot" and r.status == "filled":
                crypto = fund_params.is_crypto(r.symbol)
                self.store.add_event("order", _order_title(r, "Queued autopilot order filled" if crypto
                                                           else "Autopilot order at the open"), r.symbol,
                                     f"Queued order #{r.order_id} executed "
                                     f"{'(crypto, 24/7)' if crypto else 'at the session open'}. {DISCLAIMER}", now)
                self._audit("engine", "autopilot_order",
                            {"stage": "filled_at_open", "order_id": r.order_id, "symbol": r.symbol, "side": r.side,
                             "qty": r.qty, "status": r.status, "fill_price": r.fill_price, "fees": r.fees}, now)
        return out

    def _halt_and_liquidate(self, reason: str, origin: str, now, params, pm, actor: str, action: str,
                            detail: Optional[dict] = None) -> List[OrderResult]:
        """Halt + autopilot off + liquidate all, atomically with respect to
        non-protective order placement (``_trade_lock``). Always works: the
        audit row is written afterwards and a failure only logs an error."""
        liquidation_error = None
        migrated = self.migration_state()
        with self._trade_lock:
            self.store.set_halted(True, reason, now)
            self.store.set_autopilot(False)
            try:
                # stage 7: a migrated account is never traded here — the Strategy Platform's kill closes its
                # positions in the platform account (paper-council); this half only halts
                results = [] if migrated is not None else list(self.broker.liquidate_all(origin, now, params, pm) or [])
            except Exception as ex:
                # the halt stands (no new exposure); the liquidation is retried by the user / next kill
                logger.exception("liquidation (%s) failed", origin)
                liquidation_error = f"{type(ex).__name__}: {ex}"[:200]
                results = []
        summary = ", ".join(f"{r.side} {fmt_qty(r.qty)} {r.symbol} ({r.status})" for r in results) \
            or "no open positions"
        title = "Drawdown stop: engine halted" if origin == "drawdown-stop" else "Kill switch: engine halted"
        body = (f"{reason}. Autopilot off; all positions are being closed ({summary}). "
                f"Orders are queued until the next open when the market is closed. {DISCLAIMER}")
        if liquidation_error:
            body = (f"{reason}. Autopilot off and trading halted, but closing the positions FAILED "
                    f"({liquidation_error}) — press the kill switch again or close them manually. {DISCLAIMER}")
        try:
            self.store.add_event("halt", title, None, body, now)
        except Exception:
            logger.exception("halt event failed")
        audit = dict(detail or {}, reason=reason, orders=[r.to_dict() for r in results])
        if liquidation_error:
            audit["liquidation_error"] = liquidation_error
        self._audit(actor, action, audit, now)
        return results

    def _params_of_run(self, run_id) -> Optional[dict]:
        if run_id is None:
            return None
        version = self._run_versions.get(run_id)
        if version is None:
            run = self.store.get_run(run_id)
            version = (run or {}).get("run", {}).get("params_version") if run else None
            if version is None:
                return None
            self._run_versions[run_id] = version
        if version not in self._params_cache:
            if version == 0:
                self._params_cache[version] = fund_params.defaults()
            else:
                row = self.store.get_params(version)
                self._params_cache[version] = (fund_params.merge(row["params"], None)
                                               if row and isinstance(row.get("params"), dict) else None)
        return self._params_cache[version]

    def _load_symbol_status(self) -> None:
        """After a restart: the per-symbol outcome of the newest full cycle from
        its run summary (``judge_unavailable`` / ``data_unavailable``), so the
        UI keeps showing why a recommendation was not updated and a failed
        judgement is retried (never carried)."""
        self._symbol_status_loaded = True
        try:
            runs = self.store.list_runs(20) or []
        except Exception:
            logger.exception("runs unreadable — per-symbol status not restored")
            return
        for r in runs:
            if r.get("trigger") == "risk-watch" or r.get("status") not in ("ok", "partial", "skipped"):
                continue
            summary = r.get("summary") if isinstance(r.get("summary"), dict) else {}
            ju = summary.get("judge_unavailable") if isinstance(summary.get("judge_unavailable"), dict) else {}
            du = summary.get("data_unavailable") if isinstance(summary.get("data_unavailable"), dict) else {}
            for s in set(ju) | set(du):
                self._symbol_status[s] = {"judge_unavailable": ju.get(s), "data_unavailable": du.get(s),
                                          "run_id": r.get("id"), "at": r.get("started_at")}
            self._judge_retry.update(ju)
            return

    def _previous_states(self, symbols: List[str], params: dict, changed: set) -> Dict[str, dict]:
        if not self._symbol_status_loaded:
            self._load_symbol_status()
        latest = self.store.latest_recommendations() or {}
        out = {}
        for s in symbols:
            judged = self.store.last_judged_recommendation(s)
            if judged is None and s not in latest:
                continue
            scores = self.store.agent_scores(judged["run_id"], s) if judged else None
            pchanged = judge_mod.decision_params_changed(self._params_of_run(judged["run_id"]), params) \
                if judged else False
            out[s] = judge_mod.previous_state(judged, latest.get(s), scores, s in changed, pchanged,
                                              last_rule_action=self._last_rule_actions.get(s),
                                              judge_retry=s in self._judge_retry)
        return out

    def _apply_decisions(self, run_id, now, params, session, decisions: Dict[str, JudgeDecision],
                         risks: Dict[str, RiskAssessment], atr: Dict[str, Optional[float]],
                         previous: Dict[str, dict], positions: Dict[str, PositionView], equity: Optional[float],
                         halted: bool, pm, unreadable_reason: str = "account state unreadable") -> dict:
        """Stores recommendations, writes flip events and — autopilot on, not
        halted, account readable (``equity`` not None) — places the orders
        (sync, worker thread). Each placement runs under ``_trade_lock`` and
        re-checks the halt and the autopilot flag there (kill switch / heartbeat
        during the cycle)."""
        autopilot = bool(self.store.get_autopilot()) and not halted
        unreadable = equity is None
        # max_trades_per_day is counted per class (SPEC §9): equities per NY trading day,
        # crypto (24/7) per UTC day — a weekend crypto trade never uses Monday's equity budget
        trades = {"equity": 0, "crypto": 0}
        if autopilot and not unreadable:
            trades["equity"] = sum(1 for o in self.store.orders_on_trading_day(session.get("trading_day"), "autopilot")
                                   or [] if not fund_params.is_crypto(o.get("symbol")))
            utc_day = _utc(now).date().isoformat()
            trades["crypto"] = sum(1 for o in self.store.orders_on_utc_day(utc_day, "autopilot") or []
                                   if fund_params.is_crypto(o.get("symbol")))
        max_trades = int(params.get("max_trades_per_day", 6))
        flips: List[str] = []
        placed_total = 0
        errors: Dict[str, str] = {}
        for sym, d in decisions.items():
            try:
                ra = risks.get(sym)
                pos = positions.get(sym)
                mv = pos.market_value if pos is not None else 0.0
                cur_w = mv / equity if (mv is not None and equity and equity > 0) else 0.0
                tw = sizing.target_weight(d, ra, atr.get(sym), params, cur_w)
                rec_id = self.store.save_recommendation(run_id, now, sym, d.action, d.confidence, tw, d.rule_score,
                                                        d.rationale, d.source, d.model, d.provenance, d.usage)
                prev_action = (previous.get(sym) or {}).get("last_action")
                if prev_action and prev_action != d.action:
                    flips.append(f"{sym} {prev_action}→{d.action}")
                    self.store.add_event(
                        "flip", f"{sym}: {prev_action} → {d.action} ({d.confidence * 100:.0f}%)", sym,
                        f"{_source_label(d.source, d.model, d.provenance)}: {(d.rationale or '')[:400]} {DISCLAIMER}",
                        now)
                if not autopilot:
                    continue
                if unreadable:
                    if rec_id:
                        self.store.update_recommendation_status(rec_id, "skipped", unreadable_reason[:200])
                    continue
                note = None
                with self._trade_lock:
                    if self.store.get_halted().get("active"):      # a kill switch arrived during the cycle
                        autopilot = False
                        skip = "engine halted"
                    elif not self.store.get_autopilot():          # switched off during the cycle
                        autopilot = False
                        skip = "autopilot switched off"
                    else:
                        skip = None
                    if skip is not None:
                        if rec_id:
                            self.store.update_recommendation_status(rec_id, "skipped", skip)
                        continue
                    cls = fund_params.asset_type(sym)
                    if ra is not None and getattr(ra, "unavailable", None):
                        if rec_id:
                            self.store.update_recommendation_status(rec_id, "skipped", f"risk card {ra.unavailable}"[:200])
                        continue
                    if atr.get(sym) is None:
                        # no unscaled size as a stand-in (user decision 2026-10-02)
                        if rec_id:
                            self.store.update_recommendation_status(
                                rec_id, "skipped", "not enough daily bars to volatility-scale the size")
                        continue
                    target, reason = sizing.plan_trade(d, ra, pos, atr.get(sym), params, now, trades[cls],
                                                       equity=equity)
                    if target is None:
                        if rec_id:
                            self.store.update_recommendation_status(rec_id, "skipped", reason)
                        continue
                    note = reason                                  # e.g. "only the close fits today"
                    results = self.broker.target(sym, target, "autopilot", now, params, pm, run_id=run_id,
                                                 recommendation_id=rec_id,
                                                 rationale=f"autopilot: {d.action} {d.confidence * 100:.0f}% "
                                                           f"(recommendation #{rec_id})")
                placed = [r for r in results if r.order_id is not None and r.status in ("filled", "pending")]
                trades[fund_params.asset_type(sym)] += len(placed)
                placed_total += len(placed)
                for r in placed:
                    self.store.add_event("order", _order_title(r, "Autopilot"), sym,
                                         f"Target weight {target * 100:+.2f}% of equity from recommendation "
                                         f"#{rec_id} ({d.action}, {d.confidence * 100:.0f}%). {DISCLAIMER}", now)
                    self._audit("engine", "autopilot_order",
                                {"stage": "placed", "order_id": r.order_id, "symbol": r.symbol, "side": r.side,
                                 "qty": r.qty, "status": r.status, "fill_price": r.fill_price,
                                 "recommendation_id": rec_id, "run_id": run_id, "target_weight": target}, now)
                if max(trades.values()) > max_trades:
                    logger.warning("autopilot trade count %s exceeds the daily limit %d", trades, max_trades)
                if rec_id:
                    if placed:
                        self.store.update_recommendation_status(rec_id, "executed", note, placed[0].order_id)
                    else:
                        why = next((r.reject_reason for r in results if r.reject_reason), None)
                        if not why and self.store.pending_orders(sym):
                            why = "order already pending (executes at the next open)"
                        self.store.update_recommendation_status(rec_id, "skipped", why or "no order needed")
            except Exception as ex:
                logger.exception("decision handling for %s failed", sym)
                errors[sym] = f"decision handling failed: {ex}"[:200]
        return {"flips": flips, "orders": placed_total, "autopilot": autopilot, "errors": errors}

    async def _drawdown_guard(self, now, params, pm, halted: bool, errors: Dict[str, str]):
        """Reads the account and applies the drawdown stop (halt + liquidate
        all). Returns (account | None, halted, drawdown_stop_fired)."""
        acct, acct_err = await asyncio.to_thread(self._read_account, pm, now, params)
        drawdown_stop = False
        dd_limit = float(params.get("drawdown_stop_pct") or 0)
        if acct is None and str(acct_err or "").startswith("valuation paused"):
            # a held position has no current quote (user decision 2026-10-02: never valued at
            # the stored price): valuation, drawdown check, margin actions, equity snapshot and
            # equity-dependent orders pause for this run
            errors["valuation"] = (f"{acct_err} — valuation, drawdown check, margin actions, equity snapshot "
                                   f"and autopilot orders paused this run")
        elif acct is None:
            # Unreadable state (DB failure): no drawdown stop, no margin actions,
            # no autopilot orders this cycle — never liquidate on it.
            errors["account"] = (f"account state unreadable ({acct_err}) — drawdown check, margin actions and "
                                 f"autopilot orders skipped this cycle")
        else:
            dd = _finite(getattr(acct, "drawdown_pct", None))
            stale = self._stale_crypto_marks(pm, now)
            if not halted and dd_limit > 0 and dd is not None and dd >= dd_limit and stale:
                # never liquidate everything on a price that is not live (SPEC §10)
                msg = (f"drawdown {dd:.2f}% ≥ stop {dd_limit:g}% but no live quote for "
                       f"{', '.join(stale)} — drawdown stop skipped this run")
                logger.warning(msg)
                errors["drawdown"] = msg
            elif not halted and dd_limit > 0 and dd is not None and dd >= dd_limit:
                reason = (f"Drawdown stop: equity {dd:.2f}% below its peak "
                          f"(limit {dd_limit:g}%)")
                await asyncio.to_thread(self._halt_and_liquidate, reason, "drawdown-stop", now, params, pm, "engine",
                                        "drawdown_stop", {"equity": acct.equity, "peak_equity": acct.peak_equity,
                                                          "drawdown_pct": dd})
                halted = True
                drawdown_stop = True
                acct2, _err = await asyncio.to_thread(self._read_account, pm, now, params)
                acct = acct2 or acct
        return acct, halted, drawdown_stop

    def _stale_crypto_marks(self, pm, now) -> List[str]:
        """Held crypto symbols without a live quote in ``pm`` (missing, or a
        fallback quote older than market.STALE_QUOTE_MINUTES)."""
        out = []
        for p in self.store.get_positions() or []:
            sym = p["symbol"]
            if fund_params.is_crypto(sym) and market.quote_is_stale((pm or {}).get(sym), now):
                out.append(sym)
        return sorted(out)

    def _crypto_positions_open(self) -> bool:
        return any(fund_params.is_crypto(p["symbol"]) for p in (self.store.get_positions() or []))

    async def run_risk_watch(self, trigger: str = "risk-watch", force: bool = False) -> dict:
        """Light run outside the US session (SPEC §10) — NO agents, NO LLM,
        NO new orders: quotes -> protective pending orders -> accruals
        (perp funding, borrow, dividends) -> perp liquidation / margin check
        -> drawdown stop (may liquidate: protective) -> equity snapshot.
        Recorded as a run (judge_mode "none"). Runs only when the US session
        is closed, ``crypto_risk_watch`` is on and crypto positions are open;
        ``force`` (startup outside the session) skips these conditions and
        also refreshes the watchlist quotes. Never raises (except
        cancellation); returns {"run_id", "status", ...} or {"status":
        "not_needed", "reason"} without a run row."""
        lock, _ = self._prims()
        async with lock:
            start = self._now()
            version, params = await asyncio.to_thread(self.store.effective_params)
            self.next_risk_watch_at = self._next_risk_watch(start, params)
            try:
                await asyncio.to_thread(self.check_heartbeat, start, params)
            except Exception:
                logger.exception("heartbeat check failed")
            session = self._session(start)
            if self.migration_state() is not None:
                return {"status": "not_needed", "reason": "paper account migrated to the Strategy Platform"}
            if not force:
                why = None
                if session.get("open"):
                    why = "US session open — full cycles run"
                elif not params.get("crypto_risk_watch", True):
                    why = "crypto_risk_watch is off"
                elif not await asyncio.to_thread(self._crypto_positions_open):
                    why = "no crypto positions open"
                if why:
                    return {"status": "not_needed", "reason": why}
            self.cycle_in_progress = True
            run_id = None
            try:
                run_id = await asyncio.to_thread(self.store.create_run, start, version, trigger,
                                                 bool(session.get("open")))
                if run_id is not None:
                    self._run_versions[run_id] = version
                self._current_run = {"id": run_id, "started_at": _iso(start), "params_version": version}
                out = await self._risk_watch(start, params, session, include_watchlist=force)
                await asyncio.to_thread(self.store.finish_run, run_id, out["status"], self._now(), "none", [], 0.0,
                                        out.get("error"), out.get("summary"))
                out["run_id"] = run_id
                return out
            except asyncio.CancelledError:
                if run_id is not None:
                    self.store.finish_run(run_id, "error", self._now(), error="cancelled (backend shutting down)")
                raise
            except Exception as ex:
                logger.exception("risk watch %s crashed", run_id)
                msg = f"{type(ex).__name__}: {ex}"[:500]
                try:
                    await asyncio.to_thread(self.store.finish_run, run_id, "error", self._now(), "none", None, 0.0,
                                            msg, {"flips": [], "risk_watch": True})
                except Exception:
                    logger.exception("could not record the failed risk watch")
                return {"run_id": run_id, "status": "error", "error": msg}
            finally:
                self.cycle_in_progress = False
                self._current_run = None

    async def _risk_watch(self, now, params: dict, session: dict, include_watchlist: bool = False) -> dict:
        errors: Dict[str, str] = {}
        start_pos = await asyncio.to_thread(self._position_qtys)
        symbols = list(start_pos)
        if include_watchlist:
            symbols += [s for s in params.get("watchlist") or [] if s not in symbols]
        fetch_syms = symbols + ([BENCHMARK] if BENCHMARK not in symbols else [])
        _bars, quotes, _news, fetch_err = await self._fetch(fetch_syms, [], quote_only=fetch_syms)
        prices = {s: q for s, q in quotes.items() if q is not None}
        self._remember_quotes(quotes)
        pm: Dict[str, Any] = dict(prices)
        for s in start_pos:
            # no quote: None — the broker refuses to value it (ValuationPaused), never the stored price
            pm.setdefault(s, None)
            if s not in prices:
                errors[s] = f"no current quote ({fetch_err.get(s) or 'not delivered'}) — valuation paused"[:300]
        halted = bool(self.store.get_halted().get("active"))
        house = await asyncio.to_thread(self._housekeeping, now, params, pm, session, True)
        errors.update(house["errors"])
        acct, halted, drawdown_stop = await self._drawdown_guard(now, params, pm, halted, errors)
        spy = prices.get(BENCHMARK)
        if acct is not None:
            try:
                await asyncio.to_thread(self.broker.snapshot_equity, now, spy.price if spy else None, pm, params)
            except Exception as ex:
                logger.exception("equity snapshot failed")
                errors["broker:snapshot"] = str(ex)[:200]
        summary = {"risk_watch": True, "flips": [], "orders": 0, "symbols": 0, "errors": errors, "halted": halted,
                   "drawdown_stop": drawdown_stop, "autopilot": False,
                   "pending_filled": sum(1 for r in house["fills"] if r.status == "filled"),
                   "margin_orders": len(house["margin"]), "accruals": house["accruals"],
                   "crypto_positions": sorted(s for s in start_pos if fund_params.is_crypto(s)),
                   "account_unreadable": acct is None,
                   "note": "Risk watch: no agents, no AI, no new orders (protective actions only)."}
        status = "partial" if errors else "ok"
        error = "; ".join(f"{k}: {v}" for k, v in errors.items())[:2000] if errors else None
        return {"status": status, "judge_mode": "none", "models": [], "cost_usd": 0.0, "error": error,
                "summary": json_safe(summary)}

    async def _cycle(self, run_id, now, params: dict, session: dict) -> dict:
        errors: Dict[str, str] = {}
        watch = list(params.get("watchlist") or [])
        start_pos = await asyncio.to_thread(self._position_qtys)
        symbols = watch + [s for s in start_pos if s not in watch]
        fetch_syms = symbols + ([BENCHMARK] if BENCHMARK not in symbols else [])
        weights = params.get("agent_weights") or {}
        want_news = float(weights.get("sentiment", 0) or 0) > 0 and float(params.get("ai_daily_budget_usd") or 0) > 0
        bars, quotes, news, fetch_err = await self._fetch(fetch_syms, symbols if want_news else [],
                                                          quote_only=[s for s in fetch_syms if s not in symbols])
        prices = {s: q for s, q in quotes.items() if q is not None}
        self._remember_quotes(quotes)
        pm: Dict[str, Any] = dict(prices)
        for s in list(start_pos) + symbols:
            # no quote: None — never re-fetched in the broker, never valued at the stored price
            # (ValuationPaused: valuation / drawdown / snapshot / equity-dependent orders pause)
            pm.setdefault(s, None)
        # A symbol without daily bars OR without a quote is not analysed this run (no agents,
        # no judgement, no autopilot order) — never on partial data; the reason is recorded.
        no_data = [s for s in symbols if not bars.get(s) or s not in prices]
        data_unavailable: Dict[str, str] = {}
        for s in no_data:
            missing = " and ".join(w for w, gone in (("daily bars", not bars.get(s)), ("quote", s not in prices))
                                   if gone)
            why = fetch_err.get(s) or f"no {missing} available"
            data_unavailable[s] = f"Market data unavailable ({missing} missing): {why} — not analysed this run."[:400]
            errors[s] = data_unavailable[s]

        changed = set()
        if self._last_positions is not None:
            for s in set(start_pos) | set(self._last_positions):
                if abs(start_pos.get(s, 0.0) - self._last_positions.get(s, 0.0)) > 1e-9:
                    changed.add(s)

        halted = bool(self.store.get_halted().get("active"))
        migrated = self.migration_state()
        if migrated is None:
            house = await asyncio.to_thread(self._housekeeping, now, params, pm, session)
            errors.update(house["errors"])
            acct, halted, drawdown_stop = await self._drawdown_guard(now, params, pm, halted, errors)
        else:
            # stage 7: the account lives in the Strategy Platform now — advisory cycle (agents, judge,
            # recommendations); no accruals, fills, margin actions, drawdown stop or snapshot here
            house = {"fills": [], "accruals": None, "margin": [], "errors": {}, "expired": []}
            acct, _why = await asyncio.to_thread(self._read_account, pm, now, params)
            drawdown_stop = False
        valuation_paused = None
        try:
            positions = {p.symbol: p for p in await asyncio.to_thread(self.broker.positions, pm) or []}
        except ValuationPaused as ex:
            # held quantities stay known for the agents; unvalued positions carry no market value
            valuation_paused = str(ex)
            positions = {p.symbol: p for p in await asyncio.to_thread(self.broker.positions, pm, True) or []}
            acct = None                 # no sizing / autopilot orders without a valuation
        except Exception as ex:
            logger.exception("positions unreadable")
            errors["positions"] = f"positions unreadable: {ex}"[:200]
            positions = {}
            acct = None                 # no sizing / autopilot orders without positions
        self._valuation_paused = valuation_paused

        summary: Dict[str, Any] = {"flips": [], "orders": 0, "symbols": 0, "errors": errors, "halted": halted,
                                   "drawdown_stop": drawdown_stop, "autopilot": False,
                                   "account_migrated": migrated,
                                   "pending_filled": sum(1 for r in house["fills"] if r.status == "filled"),
                                   "margin_orders": len(house["margin"]), "accruals": house["accruals"],
                                   "positions_changed": sorted(changed), "account_unreadable": acct is None,
                                   "valuation_paused": valuation_paused}
        analysable = [s for s in symbols if s not in no_data]
        budget = self._budget(params)
        decisions: Dict[str, Any] = {}                 # JudgeDecision | judge.JudgeUnavailable
        judge_unavailable: Dict[str, str] = {}
        if analysable:
            macro_ctx, calendar, insider, cal_err, ins_err = self._newsroom()
            ctx = MarketContext(now=_iso(now), params=params, session=session,
                                bars={s: bars.get(s) or [] for s in symbols}, quotes=prices, macro=macro_ctx,
                                insider=insider, calendar=calendar, news=news, account=acct, positions=positions,
                                halted=halted, calendar_error=cal_err, insider_error=ins_err)
            if cal_err:
                errors["calendar"] = f"{cal_err} — risk cards unavailable, no autopilot orders this run"
            signals: Dict[str, Dict[str, AgentSignal]] = {}
            risks: Dict[str, RiskAssessment] = {}
            risk_sigs: Dict[str, AgentSignal] = {}
            atr: Dict[str, Optional[float]] = {}
            for s in analysable:
                try:
                    tech = technical_agent.evaluate(ctx, s)
                    mac = macro_agent.evaluate(ctx, s)
                    ra, rsig = risk_agent.assess_signal(ctx, s, params)
                    b = bars.get(s) or []
                    atr[s] = ind.atr_pct(b[-60:], 14) if len(b) >= 15 else None
                    signals[s] = {"technical": tech, "macro": mac}
                    risks[s], risk_sigs[s] = ra, rsig
                except Exception as ex:
                    logger.exception("agents failed for %s", s)
                    errors[s] = f"agents failed: {ex}"[:200]
            if signals:
                try:
                    sent = await sentiment_agent.evaluate_batch(ctx, list(signals), self._sentiment_cache, budget,
                                                                **self._llm_kw())
                except Exception as ex:
                    logger.exception("sentiment agent failed")
                    sent = {}
                    errors["sentiment"] = str(ex)[:200]
                for s in signals:
                    signals[s]["sentiment"] = sent.get(s) or sentiment_agent.unavailable(
                        s, "Sentiment unavailable — agent error.")
                all_sigs = [sig for s in signals for sig in list(signals[s].values()) + [risk_sigs[s]]]
                if run_id is not None:
                    await asyncio.to_thread(self.store.save_agent_scores, run_id, all_sigs)
                self._last_agents = {s: [_signal_dict(signals[s][a]) for a in AGENT_ORDER[:3]]
                                     + [_signal_dict(risk_sigs[s])] for s in signals}
                self._agents_loaded = True
                previous = await asyncio.to_thread(self._previous_states, list(signals), params, changed)
                try:
                    decisions = await judge_mod.judge(ctx, signals, risks, previous, params, budget, **self._llm_kw())
                except Exception as ex:
                    # no rule-based stand-in (user decision D9, 2026-10-02): no recommendation this run
                    logger.exception("judge failed — no new recommendations this run")
                    errors["judge"] = f"judge failed: {ex}"[:200]
                    decisions = {s: judge_mod.unavailable(s, "AI judge failed — no new recommendation this run.",
                                                          "judge_error") for s in signals}
                judged = {s: d for s, d in decisions.items() if isinstance(d, JudgeDecision)}
                for s, d in decisions.items():
                    if s not in judged:
                        judge_unavailable[s] = d.message
                        self._judge_retry.add(s)       # judged again next cycle, never carried
                        continue
                    self._judge_retry.discard(s)
                    if d.rule_action in ("LONG", "SHORT", "FLAT"):
                        self._last_rule_actions[s] = d.rule_action
                if judge_unavailable:
                    by_msg: Dict[str, List[str]] = {}
                    for s, m in judge_unavailable.items():
                        by_msg.setdefault(m, []).append(s)
                    note = "; ".join(f"no AI judgement for {', '.join(sorted(v))}: {m}" for m, v in by_msg.items())
                    errors["judge"] = (f"{errors['judge']}; {note}" if "judge" in errors else note)[:800]
                # only real (AI / carried) decisions are stored and may trade; an unavailable symbol
                # gets no recommendation row and no autopilot order this run
                applied = await asyncio.to_thread(self._apply_decisions, run_id, now, params, session, judged,
                                                  risks, atr, previous, positions,
                                                  acct.equity if acct is not None else None, halted, pm,
                                                  valuation_paused or "account state unreadable")
                errors.update(applied["errors"])
                summary.update(flips=applied["flips"], orders=applied["orders"], autopilot=applied["autopilot"],
                               symbols=len(signals))

        spy = prices.get(BENCHMARK)
        if prices and acct is not None and migrated is None:   # no snapshot from an unreadable / migrated state
            try:
                await asyncio.to_thread(self.broker.snapshot_equity, now, spy.price if spy else None, pm, params)
            except Exception as ex:
                logger.exception("equity snapshot failed")
                errors["broker:snapshot"] = str(ex)[:200]
        self._last_positions = await asyncio.to_thread(self._position_qtys)

        js = judge_mod.summarize(decisions) if decisions else {"judge_mode": "none", "models": [], "sources": []}
        summary["judge_sources"] = js.get("sources")
        for s in analysable:
            if s not in decisions and s in errors and s not in judge_unavailable:      # agents failed
                judge_unavailable[s] = f"Analysis failed: {errors[s]} — no new recommendation this run."[:400]
        summary["judge_unavailable"] = dict(judge_unavailable)
        summary["data_unavailable"] = dict(data_unavailable)
        self._symbol_status = {s: {"judge_unavailable": judge_unavailable.get(s),
                                   "data_unavailable": data_unavailable.get(s), "run_id": run_id, "at": _iso(now)}
                               for s in symbols}
        self._symbol_status_loaded = True
        if symbols and not analysable:
            status = "skipped"
            error = ("No usable market data for any symbol from the Alpaca data layer — nothing analysed "
                     "(per-symbol reasons below). " + "; ".join(f"{k}: {v}" for k, v in errors.items()))[:2000]
        else:
            status = "partial" if errors else "ok"
            error = "; ".join(f"{k}: {v}" for k, v in errors.items())[:2000] if errors else None
        return {"status": status, "judge_mode": js.get("judge_mode"), "models": js.get("models"),
                "cost_usd": round(budget.run_spent, 8), "error": error, "summary": json_safe(summary)}

    # --- user actions ------------------------------------------------------------------

    async def kill(self, reason: Optional[str] = None, actor: str = "user", *, propagate: bool = True) -> dict:
        """Kill switch: halt, autopilot off, liquidate everything (origin
        kill-switch; pending while the market is closed). Stage 7: also runs the
        Strategy Platform's kill (all platform accounts) unless ``propagate`` is
        False (the platform's own kill calls this with False); each half reports
        its own failure, neither blocks the other."""
        reason = (str(reason).strip() if reason else "") or "Kill switch pressed"
        now = self._now()
        out: Dict[str, Any] = {}
        try:
            _v, params = await asyncio.to_thread(self.store.effective_params)
            results = await asyncio.to_thread(self._halt_and_liquidate, f"Kill switch: {reason[:200]}", "kill-switch",
                                              now, params, None, actor, "kill")
            out.update(halted=True, halt=self.store.get_halted(), orders=[r.to_dict() for r in results])
        except Exception as ex:
            if not propagate or self.platform_kill_fn is None:
                raise
            logger.exception("kill switch (old engine half) failed")
            out.update(halted=bool(self.store.get_halted().get("active")), halt=self.store.get_halted(), orders=[],
                       error=f"{type(ex).__name__}: {ex}"[:200])
        if propagate and self.platform_kill_fn is not None:
            try:
                out["platform"] = await asyncio.to_thread(self.platform_kill_fn, reason, actor)
            except Exception as ex:
                logger.exception("kill switch (Strategy Platform half) failed")
                out["platform"] = {"ok": False, "error": f"{type(ex).__name__}: {ex}"[:200], "accounts": []}
        return out

    def resume(self, actor: str = "user") -> dict:
        """Clears the halt; the autopilot stays off. The drawdown peak is
        rebased to the current equity — otherwise a drawdown stop would fire
        again at the next cycle (the loss that triggered it is still there)."""
        was = self.store.get_halted()
        now = self._now()
        acct = None
        if was.get("active"):
            pm: Dict[str, Any] = dict(self._last_quotes)
            for r in self.store.get_positions() or []:
                pm.setdefault(r["symbol"], None)
            acct, why = self._read_account(pm, now, None)
            if acct is None:
                raise EngineError(503, f"The paper account state is unreadable ({why}) — cannot resume safely "
                                       f"(the drawdown peak could not be rebased). Try again later.")
        self._audit_required(actor, "resume", {"was": was, "peak_equity_rebased_to": acct.equity if acct else None},
                             now)
        self.store.set_halted(False, None, now)
        rebased = None
        if acct is not None:
            try:
                rebased = acct.equity
                self.store.set_peak_equity(rebased)
            except Exception:
                logger.exception("could not rebase the peak equity on resume")
        if was.get("active"):
            self.store.add_event("resume", "Engine resumed (autopilot stays off)", None,
                                 f"Halt cleared ({was.get('reason') or 'no reason'}). Switch the autopilot on "
                                 f"explicitly to trade again. {DISCLAIMER}", now)
        return {"halted": False}

    def set_autopilot(self, enabled: bool, actor: str = "user") -> dict:
        if not isinstance(enabled, bool):
            raise EngineError(400, "enabled must be true or false", {"enabled": "must be true or false"})
        if enabled and self.sandbox_off_reason:
            raise EngineError(409, self.sandbox_off_reason)
        if enabled and self.store.get_halted().get("active"):
            raise EngineError(409, "The engine is halted — resume it before switching the autopilot on.")
        if enabled:
            self._refuse_if_migrated()
        now = self._now()
        before = self.store.get_autopilot()
        if enabled:
            # switching ON needs an audit row first; switching OFF is a safety action and always works
            self._audit_required(actor, "autopilot", {"enabled": True, "was": before}, now)
            if actor == "user":
                self.touch_client(now)      # the user is here: starts the heartbeat window
        cancelled: List[dict] = []
        if enabled:
            self.store.set_autopilot(True)
        else:
            cancelled = self._autopilot_off_and_cancel("cancelled: autopilot switched off")
            self._audit(actor, "autopilot", {"enabled": False, "was": before,
                                             "cancelled_orders": [o.get("id") for o in cancelled]}, now)
        if before != enabled or cancelled:
            self.store.add_event("autopilot", f"Autopilot {'on' if enabled else 'off'}", None,
                                 ("The engine now places simulated paper orders within its limits. "
                                  if enabled else "No automatic orders; recommendations stay open. "
                                  + _cancel_text(cancelled) + " ") + DISCLAIMER,
                                 now)
        return {"autopilot": enabled}

    def get_params(self) -> dict:
        version, params = self.store.effective_params()
        row = self.store.get_params(version) if version else None
        return {"version": version, "params": params, "defaults": fund_params.defaults(),
                "limits": fund_params.LIMITS, "created_at": (row or {}).get("created_at")}

    def update_params(self, partial, actor: str = "user") -> dict:
        """Validates and stores a new params version; it applies from the next
        cycle start (``effective_from``)."""
        version, current = self.store.effective_params()
        clean, errors = fund_params.validate(partial, current)
        if errors:
            raise EngineError(400, "invalid parameters", errors)
        merged = fund_params.merge(current, clean)
        if merged != current or version == 0:
            self._audit_required(actor, "params_update", {"base_version": version, "changes": clean})
            new_version = self.store.save_params(merged, actor, "PUT /fund/params")
            if new_version is None:
                raise EngineError(503, "The parameter store is unavailable.")
            version = new_version
        return {"version": version, "params": merged, "effective_from": _iso(self._next_params_use())}

    def _risk_watch_due_at(self, params: dict, session: dict, crypto_open: bool) -> Optional[datetime]:
        """next_risk_watch_at when a risk watch will actually run (US session
        closed, crypto_risk_watch on, crypto positions open), else None."""
        if session.get("open") or not params.get("crypto_risk_watch", True) or not crypto_open:
            return None
        return self.next_risk_watch_at

    def _next_params_use(self) -> Optional[datetime]:
        """When the next run (full cycle, or a risk watch that will run) reads the params."""
        now = self._now()
        _v, params = self.store.effective_params()
        watch = self._risk_watch_due_at(params, self._session(now), self._crypto_positions_open())
        due = [t for t in (self.next_run_at, watch) if t is not None]
        return min(due) if due else None

    async def reset(self, starting_capital=None, actor: str = "user") -> dict:
        """Fresh paper account (waits for a running cycle); autopilot off."""
        _v, params = await asyncio.to_thread(self.store.effective_params)
        self._refuse_if_migrated()
        cap = params.get("starting_capital") if starting_capital is None else starting_capital
        capf = _finite(cap)
        lim = fund_params.LIMITS["starting_capital"]
        if capf is None or not lim["min"] <= capf <= lim["max"]:
            raise EngineError(400, "invalid starting capital",
                              {"starting_capital": f"must be between {lim['min']:g} and {lim['max']:g}"})
        lock, _ = self._prims()
        async with lock:
            now = self._now()
            self._audit_required(actor, "reset", {"starting_capital": capf}, now)
            try:
                acct = await asyncio.to_thread(self.broker.reset, capf, now)
            except Exception as ex:
                if type(ex).__name__ == "StateUnavailable":
                    raise EngineError(503, "The paper account state is unreadable — reset not possible right now.")
                raise
            self.store.set_autopilot(False)
            self._last_positions = {}
            self._bench_base = None
        return {"account": acct.to_dict()}

    def _validate_order(self, body) -> Tuple[str, str, Optional[float], Optional[float]]:
        if not isinstance(body, dict):
            raise EngineError(400, "invalid order", {"_": "expected a JSON object"})
        errors: Dict[str, str] = {}
        sym = fund_params.normalise_symbol(body.get("symbol"))
        if sym is None:
            errors["symbol"] = "invalid ticker symbol"
        side = body.get("side")
        if side not in ORDER_SIDES:
            errors["side"] = "must be one of " + ", ".join(ORDER_SIDES)
        qty, notional = body.get("qty"), body.get("notional")

        def _number(v) -> bool:
            return isinstance(v, (int, float)) and not isinstance(v, bool)
        if (qty is None) == (notional is None):
            errors["qty"] = "give exactly one of qty or notional"
        elif qty is not None and not _number(qty):
            errors["qty"] = "must be a JSON number"
        elif notional is not None and not _number(notional):
            errors["notional"] = "must be a JSON number"
        elif qty is not None:
            q = _finite(qty)
            if q is not None and sym is not None and not fund_params.is_crypto(sym) \
                    and not float(q).is_integer():
                errors["qty"] = "fractional quantities are only allowed for crypto — equities trade in whole shares"
            elif sym is not None and fund_params.is_crypto(sym):
                # crypto: fractional quantities (rounded down to 8 decimals by the broker)
                if q is None or q <= 0 or q > MANUAL_QTY_MAX:
                    errors["qty"] = f"must be a positive quantity up to {MANUAL_QTY_MAX:,} (up to 8 decimals)"
            elif q is None or q < 1 or q > MANUAL_QTY_MAX:
                errors["qty"] = f"must be a number of shares between 1 and {MANUAL_QTY_MAX:,}"
            elif not float(q).is_integer():
                errors["qty"] = "fractional quantities are only allowed for crypto — equities trade in whole shares"
            qty = q
        else:
            n = _finite(notional)
            if n is None or n <= 0 or n > MANUAL_NOTIONAL_MAX:
                errors["notional"] = "must be a positive USD amount"
            notional = n
        unknown = [k for k in body if k not in ("symbol", "side", "qty", "notional")]
        if unknown:
            errors[str(unknown[0])[:40]] = "unknown field"
        if errors:
            raise EngineError(400, "invalid order", errors)
        return sym, side, qty, notional

    async def manual_order(self, body, actor: str = "user") -> dict:
        """POST /fund/orders: one manual simulated order (buy/short refused
        while halted; sell/cover allowed to reduce risk)."""
        sym, side, qty, notional = self._validate_order(body)
        self._refuse_if_migrated()
        if side in ("buy", "short") and self.store.get_halted().get("active"):
            raise EngineError(409, "The engine is halted — only closing orders (sell/cover) are accepted.")
        now = self._now()
        _v, params = await asyncio.to_thread(self.store.effective_params)
        request = {"symbol": sym, "side": side, "qty": qty, "notional": notional}
        self._audit_required(actor, "manual_order", {"stage": "requested", "request": request}, now)
        req = OrderRequest(symbol=sym, side=side, qty=qty, notional=notional, origin="manual",
                           rationale="manual order (user)")

        def submit():
            # a fresh quote for the order's symbol (also shown by /fund/state right away);
            # without one the broker decides (closing orders queue, opening orders are rejected)
            prices = None
            try:
                q = self.feed.quote(sym)
            except Exception as ex:
                logger.info("manual order: no current quote for %s (%s)", sym, getattr(ex, "user_message", ex))
                q = None
            if q is not None and (_finite(getattr(q, "price", None)) or 0) > 0:
                self._last_quotes[sym] = q
                prices = {sym: q}
            with self._trade_lock:       # ordered against a concurrent kill switch / the migration freeze
                self._refuse_if_migrated()
                if side in ("buy", "short") and self.store.get_halted().get("active"):
                    raise EngineError(409, "The engine is halted — only closing orders (sell/cover) are accepted.")
                return self.broker.submit(req, now, params, prices)

        r = await asyncio.to_thread(submit)
        self._audit(actor, "manual_order", {"stage": "done", "request": request, "result": r.to_dict()}, now)
        return r.to_dict()

    def _risk_from_scores(self, run_id, symbol) -> RiskAssessment:
        """The stored risk card of the recommendation's run. Missing card or
        size multiplier -> EngineError 409 (never sized without the risk
        limits, no default multiplier)."""
        for row in self.store.agent_scores(run_id, symbol) if run_id is not None else []:
            if row.get("agent") == "risk":
                d = row.get("details") or {}
                mult = _finite(d.get("size_multiplier"))
                if d.get("unavailable"):
                    raise EngineError(409, f"The risk card for {symbol} is {d['unavailable']} — this recommendation "
                                           f"cannot be executed; wait for the next run.")
                if mult is None:
                    raise EngineError(409, f"The stored risk assessment for {symbol} is incomplete (no size "
                                           f"multiplier) — wait for the next run and execute its recommendation.")
                return RiskAssessment(symbol=symbol, veto=bool(d.get("veto")), veto_short=bool(d.get("veto_short")),
                                      size_multiplier=mult,
                                      block_increase=bool(d.get("block_increase")),
                                      block_short_increase=bool(d.get("block_short_increase")),
                                      reasons=list(d.get("reasons") or []))
        raise EngineError(409, f"No stored risk assessment for {symbol} in this recommendation's run — wait for "
                               f"the next run and execute its recommendation.")

    def _execute_sync(self, rec: dict, actor: str) -> List[dict]:
        now = self._now()
        _v, params = self.store.effective_params()
        sym = rec["symbol"]
        try:
            quote = self.feed.quote(sym)
        except UserFacingError as ex:        # the data source failed: say why, no stand-in price
            raise EngineError(503, ex.user_message) from ex
        if quote is None:
            raise EngineError(409, f"No quote for {sym} available right now — try again later.")
        pm = {sym: quote}
        missing = []
        for p in self.store.get_positions():
            # every held position is valued at a CURRENT quote (no last-seen / stored price,
            # user decision 2026-10-02); without one the execution is refused
            if p["symbol"] in pm:
                continue
            try:
                q = self.feed.quote(p["symbol"])
            except UserFacingError:
                q = None
            if q is None:
                missing.append(p["symbol"])
            pm[p["symbol"]] = q
        if missing:
            raise EngineError(503, f"{ValuationPaused(missing)} — the recommendation cannot be executed right now "
                                   f"(the account cannot be valued); try again later.")
        try:
            bars = list(self.feed.daily_bars(sym, BARS_DAYS) or [])
        except Exception as ex:
            logger.info("manual accept: daily bars for %s failed: %s", sym, getattr(ex, "user_message", ex))
            bars = []
        atrp = ind.atr_pct(bars[-60:], 14) if len(bars) >= 15 else None
        if atrp is None:
            # no unscaled sizing as a stand-in (user decision 2026-10-02)
            raise EngineError(409, f"No daily bars for {sym} — the position size can't be volatility-scaled; "
                                   f"try again when market data is available.")
        acct, why = self._read_account(pm, now, params)
        if acct is None:
            raise EngineError(503, f"The paper account state is unreadable ({why}) — try again later.")
        pos = next((p for p in self.broker.positions(pm) if p.symbol == sym), None)
        decision = JudgeDecision(symbol=sym, action=rec["action"], confidence=float(rec["confidence"] or 0.0),
                                 rationale=rec.get("rationale") or "", source=rec.get("source") or "unknown")
        risk = self._risk_from_scores(rec.get("run_id"), sym)
        target, reason = sizing.plan_trade(decision, risk, pos, atrp, params, now, 0, equity=acct.equity,
                                           enforce_min_hold=False, enforce_trade_limit=False)
        if target is None:
            raise EngineError(409, f"Nothing to execute for {sym}: {reason}.")
        self._audit_required(actor, "execute_recommendation",
                             {"stage": "requested", "recommendation_id": rec["id"], "symbol": sym,
                              "target_weight": target}, now)
        with self._trade_lock:           # ordered against a concurrent kill switch
            if self.store.get_halted().get("active"):
                raise EngineError(409, "The engine is halted — resume it first.")
            latest = (self.store.latest_recommendations() or {}).get(sym)
            if latest is not None and latest.get("id") != rec["id"]:
                raise EngineError(409, f"Recommendation #{rec['id']} was superseded by a newer recommendation "
                                       f"(#{latest.get('id')}) for {sym}.")
            results = self.broker.target(sym, target, "manual-accept", now, params, pm, run_id=rec.get("run_id"),
                                         recommendation_id=rec["id"],
                                         rationale=f"manual accept of recommendation #{rec['id']}")
        placed = [r for r in results if r.order_id is not None and r.status in ("filled", "pending")]
        if placed:
            self.store.update_recommendation_status(rec["id"], "executed", None, placed[0].order_id)
        self._audit(actor, "execute_recommendation",
                    {"stage": "done", "recommendation_id": rec["id"], "symbol": sym, "target_weight": target,
                     "orders": [r.to_dict() for r in results]}, now)
        return [r.to_dict() for r in results]

    async def execute_recommendation(self, rec_id, actor: str = "user") -> List[dict]:
        """Manual accept of a recommendation: sizing without min-hold and trade
        limit (the confidence minimum and risk limits still apply)."""
        try:
            rid = int(rec_id)
        except (TypeError, ValueError):
            raise EngineError(400, "invalid recommendation id", {"id": "must be an integer"})
        rec = await asyncio.to_thread(self.store.get_recommendation, rid)
        if rec is None:
            raise EngineError(404, f"Recommendation #{rid} not found.")
        latest = (await asyncio.to_thread(self.store.latest_recommendations) or {}).get(rec.get("symbol"))
        if latest is not None and latest.get("id") != rid:
            raise EngineError(409, f"Recommendation #{rid} was superseded by a newer recommendation "
                                   f"(#{latest.get('id')}) for {rec.get('symbol')}.")
        if rec.get("status") not in ("open", "skipped"):
            raise EngineError(409, f"Recommendation #{rid} is {rec.get('status')} — only the current (open) "
                                   f"recommendation can be executed.")
        if self.store.get_halted().get("active"):
            raise EngineError(409, "The engine is halted — resume it first.")
        self._refuse_if_migrated()
        return await asyncio.to_thread(self._execute_sync, rec, actor)

    # --- backtest -----------------------------------------------------------------------

    def _validate_backtest(self, body, params) -> Tuple[List[str], str, str]:
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise EngineError(400, "invalid backtest request", {"_": "expected a JSON object"})
        errors: Dict[str, str] = {}
        symbols = body.get("symbols")
        if symbols in (None, []):
            # default: the watchlist, capped to the first BACKTEST_MAX_SYMBOLS (noted in the result)
            symbols = list(params.get("watchlist") or [])[:BACKTEST_MAX_SYMBOLS]
        else:
            symbols, err = fund_params.normalise_watchlist(symbols)
            if err:
                errors["symbols"] = err
            elif len(symbols) > BACKTEST_MAX_SYMBOLS:
                errors["symbols"] = f"at most {BACKTEST_MAX_SYMBOLS} symbols per backtest"
        today = market.ny_date(self._now())

        def _day(key):
            v = body.get(key)
            if v in (None, ""):
                return None
            try:
                return date.fromisoformat(str(v)[:10]) if len(str(v)) == 10 else None
            except ValueError:
                return None

        end = _day("end")
        if body.get("end") not in (None, "") and end is None:
            errors["end"] = "must be a date YYYY-MM-DD"
        start = _day("start")
        if body.get("start") not in (None, "") and start is None:
            errors["start"] = "must be a date YYYY-MM-DD"
        end = min(end or today, today)
        if start is None and "start" not in errors:
            try:
                start = end.replace(year=end.year - BACKTEST_DEFAULT_YEARS)
            except ValueError:
                start = end - timedelta(days=365 * BACKTEST_DEFAULT_YEARS)
        if start is not None and not errors:
            if start >= end:
                errors["start"] = "must be before the end date"
            elif (end - start).days < 30:
                errors["start"] = "the period must be at least 30 days"
            elif _history_error(start):
                # the configured source (Alpaca) has equity history only since 2016 — no
                # shorter test is run in its place
                errors["start"] = _history_error(start)
            elif (end - start).days > int(BACKTEST_MAX_YEARS * 365.25) + 1:
                errors["start"] = f"the period must be at most {BACKTEST_MAX_YEARS} years"
        unknown = [k for k in body if k not in ("symbols", "start", "end")]
        if unknown:
            errors[str(unknown[0])[:40]] = "unknown field"
        if errors:
            raise EngineError(400, "invalid backtest request", errors)
        return symbols, start.isoformat(), end.isoformat()

    async def backtest(self, body, actor: str = "user") -> dict:
        """Rule-only backtest (fund/backtest.py) over the feed's daily bars;
        the result is stored and returned with its ``id``."""
        if self._backtest_running:
            raise EngineError(409, "a backtest is already running — wait for it to finish.")
        self._backtest_running = True      # set before the first await: one backtest at a time
        try:
            return await self._backtest(body, actor)
        finally:
            self._backtest_running = False

    async def _backtest(self, body, actor: str) -> dict:
        from . import backtest as bt
        _v, params = await asyncio.to_thread(self.store.effective_params)
        symbols, start, end = self._validate_backtest(body, params)
        capped = (body is None or not isinstance(body, dict) or body.get("symbols") in (None, [])) \
            and len(params.get("watchlist") or []) > BACKTEST_MAX_SYMBOLS
        request = {"symbols": symbols, "start": start, "end": end}
        self._audit_required(actor, "backtest", dict(request, stage="requested"))
        today = market.ny_date(self._now())
        cal_days = (today - date.fromisoformat(start)).days + BACKTEST_WARMUP_DAYS
        days_eq = int(cal_days * 252 / 365) + 30
        days_cr = int(cal_days) + 30                    # crypto: daily bars 7 days a week
        sem = asyncio.Semaphore(FETCH_CONCURRENCY)
        bars: Dict[str, list] = {}

        async def one(sym):
            days = days_cr if fund_params.is_crypto(sym) else days_eq
            async with sem:
                try:
                    bars[sym] = list(await asyncio.to_thread(self.backtest_feed.daily_bars, sym, days) or [])
                except Exception as ex:
                    logger.warning("backtest bars for %s failed: %s", sym, ex)
                    bars[sym] = []

        await asyncio.gather(*(one(s) for s in symbols))
        macro_latest = {}
        try:
            macro_latest = self._macro_fn() or {}
        except Exception:
            pass
        history = bt.build_macro_history(macro_latest)
        result = await asyncio.to_thread(bt.run_backtest, {s: b for s, b in bars.items() if b}, history, params,
                                         start, end)
        if not isinstance(result, dict):
            raise EngineError(500, "backtest failed")
        if result.get("error"):
            missing = [s for s in symbols if not bars.get(s)]
            notes = list(result.get("notes") or [])
            if missing:
                notes.append("No price data for: " + ", ".join(missing)
                             + " (the Alpaca data layer returned no bars — see backend.log for the reason)")
            raise EngineError(409, f"Backtest not possible: {result['error']}. " + " ".join(notes)[:400])
        if capped:
            result = dict(result)
            result["notes"] = list(result.get("notes") or []) + [
                f"Only the first {BACKTEST_MAX_SYMBOLS} watchlist symbols were tested (backtest limit)."]
        bid = await asyncio.to_thread(self.store.save_backtest, request, json_safe(result), self._now())
        out = dict(result)
        out["id"] = bid
        return out

    # --- read models ----------------------------------------------------------------------

    def equity(self, since=None) -> dict:
        """Equity curve since ``since`` (default: the last reset) with the SPY
        benchmark scaled to the starting capital at the reset."""
        start_cap = self.store.get_starting_capital()
        reset_at = self.store.get_reset_at()
        since_eff = _iso(since) if since else reset_at
        points = self.store.equity_points(since=since_eff)
        base = None
        if reset_at:
            if self._bench_base and self._bench_base[0] == reset_at and self._bench_base[1]:
                base = self._bench_base[1]
            else:
                src = points if since_eff == reset_at else self.store.equity_points(since=reset_at, limit=200000)
                base = next((p["benchmark"] for p in src if p["ts"] >= reset_at and _finite(p["benchmark"])
                             and p["benchmark"] > 0), None)
                self._bench_base = (reset_at, base)
        out = []
        for p in points:
            bench = _finite(p.get("benchmark"))
            be = None
            if base and bench and start_cap and reset_at and p["ts"] >= reset_at:
                be = start_cap * bench / base
            out.append({"ts": p["ts"], "equity": p["equity"], "cash": p["cash"], "long_mv": p["long_mv"],
                        "short_mv": p["short_mv"], "benchmark": bench, "benchmark_equity": be,
                        "drawdown_pct": p["drawdown_pct"]})
        return json_safe({"points": out, "starting_capital": start_cap, "reset_at": reset_at, "benchmark": BENCHMARK})

    def _load_agents_from_store(self) -> None:
        self._agents_loaded = True
        for r in self.store.list_runs(20) or []:
            if r.get("status") not in ("ok", "partial"):
                continue
            rows = self.store.agent_scores(r["id"])
            if not rows:
                continue
            by: Dict[str, Dict[str, dict]] = {}
            for row in rows:
                by.setdefault(row["symbol"], {})[row["agent"]] = {
                    k: row.get(k) for k in ("agent", "symbol", "score", "confidence", "summary", "details", "source",
                                            "provenance")}
            self._last_agents = {s: [a[k] for k in AGENT_ORDER if k in a] for s, a in by.items()}
            return

    def state(self) -> dict:
        """The GET /fund/state payload (SPEC §11). Sync (DB only, no network):
        positions are valued at the last cycle's quotes; a position without
        one is listed without a value and the account shows
        ``valuation_paused`` (never valued at the stored price)."""
        now = self._now()
        version, params = self.store.effective_params()
        session = self._session(now)
        last = self.store.last_run()
        running_version = (self._current_run or {}).get("params_version") if self.cycle_in_progress else None
        if running_version is None and last:
            running_version = last.get("params_version")
        pending_params = bool(version) and (running_version is None or int(running_version) != int(version))

        rows = self.store.get_positions() or []
        pm: Dict[str, Any] = dict(self._last_quotes)      # copy (the cycle updates it)
        for r in rows:
            pm.setdefault(r["symbol"], None)
        acct, acct_err = self._read_account(pm, now, params)
        paused = acct_err if acct is None and str(acct_err or "").startswith("valuation paused") else None
        if acct is None:
            # unreadable state / valuation paused: same keys, all null, plus the reason (the UI shows it)
            account = {k: None for k in ACCOUNT_FIELDS}
            account.update(unreadable=True,
                           error=(f"Valuation paused: {paused[len('valuation paused: '):]} — account values, "
                                  f"drawdown check and equity-dependent orders wait for a current quote."
                                  if paused else f"Account state unreadable: {acct_err}"))
            equity = None
        else:
            account = acct.to_dict()
            account["unreadable"] = False
            equity = acct.equity
        account["valuation_paused"] = paused
        account["drawdown_stop_pct"] = params.get("drawdown_stop_pct")
        positions = []
        pos_by = {}
        try:
            pos_views = list(self.broker.positions(pm, True) or [])
        except Exception:
            logger.exception("positions unreadable for /fund/state")
            pos_views = []
        for p in pos_views:
            pos_by[p.symbol] = p
            positions.append({"symbol": p.symbol, "qty": p.qty, "asset_class": p.asset_class,
                              "instrument": p.instrument, "avg_price": p.avg_price, "last_price": p.last_price,
                              "market_value": p.market_value,
                              "weight": (p.market_value / equity)
                              if (p.market_value is not None and equity and equity > 0) else None,
                              "unrealized_pnl": p.unrealized_pnl, "realized_pnl": p.realized_pnl,
                              "opened_at": p.opened_at, "side": p.side, "borrow_rate_pct": p.borrow_rate_pct})

        if not self._last_agents and not self._agents_loaded:
            self._load_agents_from_store()
        recs = self.store.latest_recommendations() or {}
        if not self._symbol_status_loaded:
            self._load_symbol_status()
        watch = list(params.get("watchlist") or [])
        syms = watch + [s for s in pos_by if s not in watch]
        symbols = []
        for s in syms:
            st = self._symbol_status.get(s) or {}
            judge_na, data_na = st.get("judge_unavailable"), st.get("data_unavailable")
            rec = recs.get(s)
            if rec is not None:
                rec = dict(rec)
                pos = pos_by.get(s)
                mv = pos.market_value if pos else 0.0
                rec["current_weight"] = (mv / equity) if (mv is not None and equity and equity > 0) else \
                    (0.0 if not pos else None)
                # the last full cycle produced no new recommendation: this one keeps its own
                # time and says so — it is never shown as if it were fresh (D9, 2026-10-02)
                rec["not_updated"] = (f"not updated: {judge_na or data_na}" if (judge_na or data_na) else None)
                # a carried recommendation re-uses an earlier AI judgement (change detection, accepted
                # design 2026-09-26): ``judged_at`` is that judgement's ORIGINAL time (its provenance),
                # ``ts`` only when it was carried
                prov = rec.get("provenance") if isinstance(rec.get("provenance"), dict) else {}
                rec["judged_at"] = prov.get("generated_at") if rec.get("source") == "carried" else rec.get("ts")
            crypto = fund_params.is_crypto(s)
            symbols.append({"symbol": s, "asset_class": "crypto" if crypto else "equity",
                            "tradable_now": True if crypto else bool(session.get("open")),
                            "quote": _quote_dict(self._last_quotes.get(s)),
                            "agents": list(self._last_agents.get(s) or []), "recommendation": rec,
                            "judge_unavailable": judge_na, "data_unavailable": data_na,
                            "status_run_id": st.get("run_id")})

        last_run = None
        if last:
            last_run = {k: last.get(k) for k in ("id", "started_at", "finished_at", "status", "judge_mode",
                                                  "cost_usd", "error", "trigger", "params_version")}
        autopilot = bool(self.store.get_autopilot())
        hb_minutes = self._heartbeat_minutes(params)
        seen = self._last_client_seen
        in_session = bool(session.get("open"))
        watch_on = bool(params.get("crypto_risk_watch", True))
        crypto_open = any(fund_params.is_crypto(r["symbol"]) for r in rows)
        next_watch = self._risk_watch_due_at(params, session, crypto_open)
        payload = {
            "now": _iso(now),
            "market": dict(session, delayed_quotes=True),
            "engine": {"running": self.running, "cycle_in_progress": self.cycle_in_progress,
                       # "session": scheduled full cycles every interval_minutes (US regular session);
                       # "manual_only": US market closed — full cycles only via "Run now" (SPEC §10)
                       # sandbox profile: no scheduled cycles at all ("manual_only", no next run)
                       "schedule_mode": "session" if in_session and not self.sandbox_off_reason else "manual_only",
                       "next_run_at": _iso(self.next_run_at) if in_session and not self.sandbox_off_reason else None,
                       "next_session_run_at": None if self.sandbox_off_reason else _iso(self.next_run_at),
                       "next_risk_watch_at": _iso(next_watch),
                       "crypto_risk_watch": watch_on,
                       "risk_watch_interval_minutes": self._interval_minutes(params, key="closed_interval_minutes"),
                       "interval_minutes": self._interval_minutes(params), "last_run": last_run,
                       "autopilot_heartbeat_minutes": hb_minutes,
                       # when the dead-man switch turns the autopilot off without a further client poll
                       "autopilot_off_at": _iso(seen + timedelta(minutes=hb_minutes))
                       if (autopilot and seen is not None) else None},
            "last_client_seen": _iso(seen),
            "autopilot": autopilot,
            "halted": self.store.get_halted(),
            "params_version": version,
            "pending_params": pending_params,
            "ai_budget": self._budget(params).to_dict(),
            "account": account,
            "positions": positions,
            "pending_orders": self.store.pending_orders() or [],
            "symbols": symbols,
            "paper_trading": True,
            # stage 7 (strategy_platform SPEC §17.1): who trades this (old) paper account
            "legacy_account": self._legacy_account_state(),
        }
        return json_safe(payload)

    def _legacy_account_state(self) -> dict:
        st = self.migration_state()
        if st is None:
            return {"status": "active", "owner": "old engine", "migrated_at": None, "migration_id": None}
        return {"status": st.get("status"), "owner": "platform", "migrated_at": st.get("at"),
                "migration_id": st.get("migration_id")}
