"""The Council of AIs as a platform strategy (SP §22.1, §22.5) — one council cycle + the council task.

A cycle (its OWN lock; agents + LLM outside ``runtime.cycle``): Council params (``cl_params``) → the agent view
(Council params + paper-council's EFFECTIVE broker params + ``drawdown_stop_pct`` = ``pf_settings.drawdown_kill_pct``)
→ market data (daily bars, quotes, news) → agents (technical, macro, risk, sentiment) → judge (change detection,
batch LLM judge, model modes) → sizing (FUND §9, on paper-council's equity — ``_unallocated`` must be empty) → the
ORDER STEP under the account's trade lock (``oms.lock_for``): re-checks (auto trading ON, Council orders ON, account
not halted, not sandbox, switch-over done) right before each submit, D7 (equities outside the window / in the
opening grace → "market closed — no order placed"), ``PaperBroker.plan_target`` (the old ``broker.target`` rules:
pending protective / manual order → none; a working Council order identical / leading to the target → kept; else
replaced) and the legs through the OMS (origin ``council``, source ``council-of-ais``). Everything is recorded
insert-only in ``cl_*`` (signals with AI provenance, decisions, submit-time executions, per-run positions, AI calls).
No rule-based stand-in (FUND D9): a symbol without a judgement gets no new target — its position is HELD.

Housekeeping (accruals, queued fills, margin, drawdown kill, snapshots) is the platform runtime's job (§22.6).
"""
from __future__ import annotations

import asyncio
import logging
import math
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from strategy_platform.core import calendars
from strategy_platform.core import llm as llm_mod
from strategy_platform.core import notices
from strategy_platform.core import params as fund_params
from strategy_platform.core.types import AgentSignal, JudgeDecision, MarketContext, RiskAssessment
from strategy_platform.execution import accounts, oms
from strategy_platform.execution.paper_broker import PENDING_FILL_DELAY_MINUTES, StateUnavailable, ValuationPaused
from strategy_platform.strategy.features import macro_agent

from . import indicators as ind
from . import judge as judge_mod
from . import params as cparams
from . import sizing, spend, store
from .agents import risk as risk_agent
from .agents import sentiment as sentiment_agent
from .agents import technical as technical_agent

logger = logging.getLogger(__name__)

BENCHMARK = "SPY"
BARS_DAYS = 400
FETCH_CONCURRENCY = 4
AGENT_ORDER = ("technical", "macro", "sentiment", "risk")
STARTUP_DELAY_SECONDS = 20.0
FIRST_CYCLE_OFFSET = timedelta(minutes=PENDING_FILL_DELAY_MINUTES, seconds=5)   # open + grace + 5 s (D7)
CRYPTO_STALE_MINUTES = 60                          # D11: unfilled Council crypto orders expire (OMS accepted → expired)
MARKET_CLOSED = "market closed — no order placed"
LABEL = "not validated — AI strategy"
NOTICE = notices.PAPER          # S10-H context line ("Paper trading · simulated money.")


class CouncilError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _finite(x) -> Optional[float]:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _utc(dt) -> datetime:
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _iso(dt) -> Optional[str]:
    return None if dt is None else _utc(dt).isoformat(timespec="seconds")


def _signal_row(sym: str, sig: AgentSignal) -> dict:
    ai = sig.agent == "sentiment" and sig.source in ("llm", "cached")
    return {"symbol": sym, "agent": sig.agent, "score": _finite(sig.score), "confidence": _finite(sig.confidence),
            "summary": sig.summary, "details": sig.details, "source": sig.source or "rules", "ai_generated": ai,
            "provenance": getattr(sig, "provenance", None) if ai else None}


def session_label(now: datetime) -> str:
    d = calendars.ny_date(now)
    try:
        bounds = calendars.session_bounds(d)
    except calendars.CalendarUnavailable:
        return "unknown"
    if bounds is None:
        return "holiday" if d.weekday() < 5 else "closed"
    if bounds[0] <= now < bounds[1]:
        return "early_close" if d in calendars.NYSE_EARLY_CLOSES else "open"
    return "closed"


def equity_window_open(now: datetime) -> bool:
    """D7: equities may be ordered only inside the session AFTER the opening grace."""
    d = calendars.ny_date(now)
    bounds = calendars.session_bounds(d)
    return bool(bounds) and bounds[0] + timedelta(minutes=PENDING_FILL_DELAY_MINUTES) <= now < bounds[1]


def next_due(after: datetime, interval_minutes: int, last_start: Optional[datetime] = None) -> Optional[datetime]:
    """The next scheduled cycle start (SP §22.5, D7): only inside the session AFTER the opening grace — the first
    cycle of a session at open + grace + 5 s, then last start + interval (an overdue cycle runs once at once: missed
    cycles are never caught up); nothing outside the session. None when the calendar has no coming session."""
    after = _utc(after)
    step = timedelta(minutes=int(interval_minutes))
    d = calendars.ny_date(after)
    for _ in range(15):
        bounds = calendars.session_bounds(d)
        if bounds:
            first = bounds[0] + FIRST_CYCLE_OFFSET
            if after < bounds[1]:
                if after <= first:
                    return first
                if last_start is not None and first <= _utc(last_start) < bounds[1]:
                    nxt = _utc(last_start) + step
                    if nxt < bounds[1]:
                        return nxt
                else:
                    return after                 # in the session, no cycle yet this session: due now
        d = calendars.next_trading_day(d)
        nb = calendars.session_bounds(d)
        if nb:
            after = nb[0]
    return None


class CouncilService:
    """One instance per backend (``default()``); everything injectable for the tests / the parity harness."""

    def __init__(self, feed, *, macro_fn: Optional[Callable[[], dict]] = None,
                 calendar_fn: Optional[Callable[[], list]] = None, insider_fn: Optional[Callable[[], list]] = None,
                 macro_status_fn: Optional[Callable[[], Optional[dict]]] = None,
                 clock: Optional[Callable[[], datetime]] = None, llm: Optional[Callable] = None,
                 account_id: str = accounts.COUNCIL, sandbox_fn: Optional[Callable[[], bool]] = None):
        self.feed = feed
        self._macro_fn = macro_fn or (lambda: {})
        self._macro_status_fn = macro_status_fn
        self._calendar_fn = calendar_fn or (lambda: [])
        self._insider_fn = insider_fn or (lambda: [])
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.llm = llm
        self.account_id = account_id
        self._sandbox_fn = sandbox_fn
        self._run_lock = threading.Lock()
        self.cycle_in_progress = False
        self.last_start: Optional[datetime] = None
        self.next_due_at: Optional[datetime] = None
        self.last_result: Optional[dict] = None
        self._task: Optional[asyncio.Task] = None
        self._stopping = False
        self._wake: Optional[asyncio.Event] = None
        self._manual_pending = False
        # change-detection memory (FUND §8.3), rebuilt from cl_decisions / cl_run_results after a restart
        self._sentiment_cache: Dict[str, dict] = {}
        self._last_positions: Optional[Dict[str, float]] = None
        self._last_rule_actions: Dict[str, str] = {}
        self._judge_retry: set = set()
        self._memory_loaded = False

    # --- gates -----------------------------------------------------------------------------------------------
    def now(self) -> datetime:
        return _utc(self.clock())

    def sandbox(self) -> bool:
        if self._sandbox_fn is not None:
            return bool(self._sandbox_fn())
        try:
            import sandbox_profile
            return bool(sandbox_profile.is_sandbox())
        except Exception:      # noqa: BLE001 — fail closed: treated as sandbox (no orders)
            return True

    def active(self) -> bool:
        try:
            return store.switchover() == "done"
        except Exception:      # noqa: BLE001
            return False

    def account(self):
        return accounts.get(self.account_id)

    def order_gate(self, acc) -> Optional[str]:
        """Why no Council order may be submitted now (None = allowed). Re-checked right before each submit."""
        from strategy_platform.execution import control
        if not self.active():
            return "the Council is not switched over"
        if self.sandbox():
            return "sandbox mode — the Council only analyses"
        try:
            if not control.autotrade()["enabled"]:
                return "auto trading is off"
        except Exception:      # noqa: BLE001
            return "the auto-trading state could not be read"
        if not store.orders_enabled():
            return "Council orders are off"
        if acc.halt()["active"]:
            return "the account is halted"
        return None

    def params(self) -> Tuple[int, Dict[str, Any]]:
        got = store.latest_params()
        if got is None:
            raise CouncilError(503, "not_active", "The Council has no parameters yet (switch-over not done).")
        return got

    def agent_params(self, acc, council: Dict[str, Any]) -> Dict[str, Any]:
        from strategy_platform.portfolio import settings as pf_settings
        ov = store.latest_broker_override()
        eff = cparams.effective_broker(acc.params, ov[1] if ov else None)
        dd = float(pf_settings.values(acc.id)["drawdown_kill_pct"])
        return cparams.agent_view(council, eff, dd)

    # --- memory -------------------------------------------------------------------------------------------------
    def _load_memory(self) -> None:
        self._memory_loaded = True
        try:
            for r in store.runs(20):
                if r["status"] not in ("ok", "partial"):
                    continue
                s = r.get("summary") if isinstance(r.get("summary"), dict) else {}
                ju = s.get("judge_unavailable") if isinstance(s.get("judge_unavailable"), dict) else {}
                self._judge_retry.update(ju)
                ra = s.get("rule_actions") if isinstance(s.get("rule_actions"), dict) else {}
                for k, v in ra.items():
                    self._last_rule_actions.setdefault(k, v)
                return
        except Exception:      # noqa: BLE001
            logger.exception("Council memory not restored (runs unreadable)")

    def _previous_states(self, symbols: List[str], params: dict, changed: set) -> Dict[str, dict]:
        if not self._memory_loaded:
            self._load_memory()
        latest = store.latest_recommendations()
        out = {}
        for s in symbols:
            judged = store.last_judged(s)
            if judged is None and s not in latest:
                continue
            scores = store.agent_scores(judged["run_id"], s) if judged else None
            pchanged = judge_mod.decision_params_changed(store.decision_params_of_run(judged["run_id"]), params) \
                if judged else False
            out[s] = judge_mod.previous_state(judged, latest.get(s), scores, s in changed, pchanged,
                                              last_rule_action=self._last_rule_actions.get(s),
                                              judge_retry=s in self._judge_retry)
        return out

    # --- market data ------------------------------------------------------------------------------------------------
    async def _fetch(self, symbols: List[str], news_symbols: List[str], quote_only=()):
        sem = asyncio.Semaphore(FETCH_CONCURRENCY)
        bars: Dict[str, list] = {}
        quotes: Dict[str, Any] = {}
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
                        logger.warning("news for %s failed: %s", sym, ex)
                        news[sym] = [{"error": getattr(ex, "user_message", None) or "news fetch failed"}]

        await asyncio.gather(*(one(s) for s in symbols))
        return bars, quotes, news, errors

    def _newsroom(self):
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

    # --- one run ------------------------------------------------------------------------------------------------
    async def run(self, trigger: str = "manual", run_id: Optional[int] = None) -> dict:
        """One council cycle (scheduled or "Run now"). Never raises (except cancellation). ``run_id``: the row
        ``request_run`` created up front ("Run now" answers with its id, SP §22.5); a cycle that cannot start then
        finishes that row as "error" so it never stays "running"."""
        if not self._run_lock.acquire(blocking=False):
            if run_id is not None:
                _finish_unstarted(run_id, self.now(), "another Council cycle was running")
            raise CouncilError(409, "running", "A Council cycle is already running.")
        pre_created = run_id
        try:
            self.cycle_in_progress = True
            start = self.now()
            self.last_start = start
            version, council = await asyncio.to_thread(self.params)
            acc = await asyncio.to_thread(self.account)
            params = await asyncio.to_thread(self.agent_params, acc, council)
            gate = await asyncio.to_thread(self.order_gate, acc)
            if run_id is None:
                run_id = await asyncio.to_thread(store.create_run, trigger, start, version, session_label(start),
                                                 gate is None, self.sandbox())
            calls = spend.CallLog(run_id, self.now, _premium_model())
            try:
                out = await self._cycle(run_id, start, params, acc, calls)
            finally:
                await asyncio.to_thread(calls.flush)
            out["summary"]["decision_params"] = {k: params.get(k) for k in judge_mod.DECISION_PARAM_KEYS}
            out["summary"]["rule_actions"] = dict(self._last_rule_actions)
            out["summary"]["label"] = LABEL
            await asyncio.to_thread(store.finish_run, run_id, self.now(), out["status"], out.get("judge_mode"),
                                    out.get("cost_usd", 0.0), out["summary"])
            out["run_id"] = run_id
            self.last_result = out
            return out
        except CouncilError as ex:
            if pre_created is not None:
                _finish_unstarted(pre_created, self.now(), ex.message)
            raise
        except asyncio.CancelledError:
            if run_id is not None:
                store.finish_run(run_id, self.now(), "error", None, 0.0, {"error": "cancelled (backend shutting down)"})
            raise
        except Exception as ex:
            logger.exception("Council cycle %s crashed", run_id)
            msg = f"{type(ex).__name__}: {ex}"[:500]
            if run_id is not None:
                try:
                    await asyncio.to_thread(store.finish_run, run_id, self.now(), "error", None, 0.0, {"error": msg})
                    _alert("council_run_failed", f"Council cycle #{run_id} failed", msg, f"council-run-{run_id}")
                except Exception:
                    logger.exception("could not record the failed Council run")
            self.last_result = {"run_id": run_id, "status": "error", "error": msg}
            return self.last_result
        finally:
            self.cycle_in_progress = False
            self._run_lock.release()

    async def _cycle(self, run_id: int, now: datetime, params: dict, acc, calls: "spend.CallLog") -> dict:
        errors: Dict[str, str] = {}
        watch = list(params.get("watchlist") or [])
        start_pos = {p["symbol"]: float(p["qty"]) for p in (acc.store.get_positions() or [])}
        symbols = watch + [s for s in start_pos if s not in watch]
        fetch_syms = symbols + ([BENCHMARK] if BENCHMARK not in symbols else [])
        weights = params.get("agent_weights") or {}
        want_news = float(weights.get("sentiment", 0) or 0) > 0 and float(params.get("ai_daily_budget_usd") or 0) > 0
        bars, quotes, news, fetch_err = await self._fetch(fetch_syms, symbols if want_news else [],
                                                          quote_only=[s for s in fetch_syms if s not in symbols])
        prices = {s: q for s, q in quotes.items() if q is not None}
        pm: Dict[str, Any] = dict(prices)
        for s in list(start_pos) + symbols:
            pm.setdefault(s, None)
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
        halted = bool(acc.halt()["active"])
        acct, why_acct = await asyncio.to_thread(self._read_account, acc, pm, now, params)
        unalloc = await asyncio.to_thread(_unallocated_problem, acc)
        if unalloc:
            errors["unallocated"] = unalloc
            acct = None
        valuation_paused = None
        try:
            positions = {p.symbol: p for p in await asyncio.to_thread(acc.broker.positions, pm) or []}
        except ValuationPaused as ex:
            valuation_paused = str(ex)
            positions = {p.symbol: p for p in await asyncio.to_thread(acc.broker.positions, pm, True) or []}
            acct = None
        except Exception as ex:
            logger.exception("positions unreadable")
            errors["positions"] = f"positions unreadable: {ex}"[:200]
            positions, acct = {}, None
        if acct is None and why_acct and "unallocated" not in errors and valuation_paused is None:
            errors["account"] = f"account state unreadable ({why_acct}) — no Council orders this run"
        summary: Dict[str, Any] = {"flips": [], "orders": 0, "symbols": 0, "errors": errors, "halted": halted,
                                   "positions_changed": sorted(changed), "account_unreadable": acct is None,
                                   "valuation_paused": valuation_paused}
        analysable = [s for s in symbols if s not in no_data]
        budget = llm_mod.Budget(params.get("ai_daily_budget_usd", 0.0), lambda: spend.spent_today(self.now()),
                                calls.book)
        llm_kw = {"llm": calls.wrap(self.llm or llm_mod.chat_json)}
        decisions: Dict[str, Any] = {}
        judge_unavailable: Dict[str, str] = {}
        if analysable:
            macro_ctx, calendar, insider, cal_err, ins_err = self._newsroom()
            ctx = MarketContext(now=_iso(now), params=params, session=calendars.session_state(now),
                                bars={s: bars.get(s) or [] for s in symbols}, quotes=prices, macro=macro_ctx,
                                insider=insider, calendar=calendar, news=news, account=acct, positions=positions,
                                halted=halted, calendar_error=cal_err, insider_error=ins_err)
            if cal_err:
                errors["calendar"] = f"{cal_err} — risk cards unavailable, no Council orders this run"
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
                                                                **llm_kw)
                except Exception as ex:
                    logger.exception("sentiment agent failed")
                    sent = {}
                    errors["sentiment"] = str(ex)[:200]
                for s in signals:
                    signals[s]["sentiment"] = sent.get(s) or sentiment_agent.unavailable(
                        s, "Sentiment unavailable — agent error.")
                rows = [_signal_row(s, sig) for s in signals for sig in list(signals[s].values()) + [risk_sigs[s]]]
                await asyncio.to_thread(store.save_signals, run_id, rows)
                previous = await asyncio.to_thread(self._previous_states, list(signals), params, changed)
                try:
                    decisions = await judge_mod.judge(ctx, signals, risks, previous, params, budget, **llm_kw)
                except Exception as ex:
                    logger.exception("judge failed — no new recommendations this run")
                    errors["judge"] = f"judge failed: {ex}"[:200]
                    decisions = {s: judge_mod.unavailable(s, "AI judge failed — no new recommendation this run.",
                                                          "judge_error") for s in signals}
                judged = {s: d for s, d in decisions.items() if isinstance(d, JudgeDecision)}
                for s, d in decisions.items():
                    if s not in judged:
                        judge_unavailable[s] = d.message
                        self._judge_retry.add(s)
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
                    if any(m == llm_mod.BUDGET_REACHED_MESSAGE for m in judge_unavailable.values()):
                        _alert("council_budget_reached", "Council AI budget reached", llm_mod.BUDGET_REACHED_MESSAGE,
                               f"council-budget-{now.date().isoformat()}")
                applied = await asyncio.to_thread(self._apply_decisions, run_id, now, params, judged,
                                                  judge_unavailable, risks, atr, previous, positions,
                                                  acct.equity if acct is not None else None, halted, pm, acc,
                                                  valuation_paused or errors.get("unallocated")
                                                  or "account state unreadable")
                errors.update(applied["errors"])
                summary.update(flips=applied["flips"], orders=applied["orders"], symbols=len(signals),
                               orders_enabled=applied["orders_enabled"])
        try:
            await asyncio.to_thread(store.save_positions, run_id, _position_rows(positions, acct))
        except Exception:
            logger.exception("Council positions not recorded")
        self._last_positions = {p["symbol"]: float(p["qty"]) for p in (acc.store.get_positions() or [])}
        js = judge_mod.summarize(decisions) if decisions else {"judge_mode": "none", "models": [], "sources": []}
        summary["judge_sources"] = js.get("sources")
        for s in analysable:
            if s not in decisions and s in errors and s not in judge_unavailable:
                judge_unavailable[s] = f"Analysis failed: {errors[s]} — no new recommendation this run."[:400]
        summary["judge_unavailable"] = dict(judge_unavailable)
        summary["data_unavailable"] = dict(data_unavailable)
        if symbols and not analysable:
            status = "partial"
            summary["note"] = "No usable market data for any symbol — nothing analysed."
        else:
            status = "partial" if errors else "ok"
        summary["models"] = js.get("models")
        return {"status": status, "judge_mode": js.get("judge_mode"), "cost_usd": round(budget.run_spent, 8),
                "summary": summary}

    @staticmethod
    def _read_account(acc, pm, now, params):
        try:
            a = acc.broker.account(pm, now, acc.params)
        except ValuationPaused as ex:
            return None, str(ex)[:300]
        except Exception as ex:
            logger.error("paper-council state unreadable: %s: %s", type(ex).__name__, ex)
            return None, f"{type(ex).__name__}: {ex}"[:200]
        if a is None or _finite(getattr(a, "equity", None)) is None:
            return None, "equity unavailable"
        return a, None

    # --- decisions → orders ------------------------------------------------------------------------------------
    def _trades_today(self, acc, now: datetime) -> Dict[str, int]:
        """Council orders that reached the OMS as submitted / working / filled, per class day (SP §22.1)."""
        out = {"equity": 0, "crypto": 0}
        starts = {"equity": oms._class_day_start(now, False), "crypto": oms._class_day_start(now, True)}
        for o in oms.list_orders(acc.id, limit=500):
            if o.get("origin") != "council" or o["state"] in ("rejected",):
                continue
            if o["state"] == "cancelled" and not o.get("broker_order_id"):
                continue                       # cancelled before it was sent: not a trade
            cls = "crypto" if fund_params.is_crypto(o["symbol"]) else "equity"
            if _utc(o["created_at"]) >= starts[cls]:
                out[cls] += 1
        return out

    def _apply_decisions(self, run_id, now, params, decisions: Dict[str, JudgeDecision], unavailable: Dict[str, str],
                         risks, atr, previous, positions, equity, halted, pm, acc, unreadable_reason) -> dict:
        trades = self._trades_today(acc, now)
        max_trades = int(params.get("max_trades_per_day", 6))
        flips: List[str] = []
        placed_total = 0
        errors: Dict[str, str] = {}
        gate0 = self.order_gate(acc)
        for sym, why in unavailable.items():
            store.add_decision({"run_id": run_id, "symbol": sym, "judge_source": "unavailable",
                                "unavailable_reason": why[:500]})
        for sym, d in decisions.items():
            try:
                ra = risks.get(sym)
                pos = positions.get(sym)
                mv = pos.market_value if pos is not None else 0.0
                cur_w = mv / equity if (mv is not None and equity and equity > 0) else 0.0
                tw = sizing.target_weight(d, ra, atr.get(sym), params, cur_w)
                ai = d.source == "llm"
                did = store.add_decision({"run_id": run_id, "symbol": sym, "rule_score": d.rule_score,
                                          "agreement": getattr(d, "agreement", None), "rule_action": d.rule_action,
                                          "judge_source": d.source, "action": d.action, "confidence": d.confidence,
                                          "rationale": d.rationale, "model": d.model,
                                          "cost_share_usd": llm_mod.usage_cost(d.usage) if d.usage else None,
                                          "target_weight": tw, "ai_generated": ai,
                                          "provenance": d.provenance if ai else None})
                prev_action = (previous.get(sym) or {}).get("last_action")
                if prev_action and prev_action != d.action:
                    flips.append(f"{sym} {prev_action}→{d.action}")
                    _alert("council_flip", f"Council: {sym} {prev_action} → {d.action} ({d.confidence * 100:.0f}%)",
                           f"AI-generated rationale: {(d.rationale or '')[:400]} {NOTICE}",
                           f"council-flip-{run_id}-{sym}", ref=str(run_id))
                if gate0 is not None:
                    store.add_execution(did, "not_executed", gate0, now)
                    continue
                if equity is None:
                    store.add_execution(did, "not_executed", unreadable_reason[:200], now)
                    continue
                if ra is not None and getattr(ra, "unavailable", None):
                    store.add_execution(did, "skipped", f"risk card {ra.unavailable}"[:200], now)
                    continue
                if atr.get(sym) is None:
                    store.add_execution(did, "skipped", "not enough daily bars to volatility-scale the size", now)
                    continue
                cls = fund_params.asset_type(sym)
                target, reason = sizing.plan_trade(d, ra, pos, atr.get(sym), params, now, trades[cls], equity=equity)
                if target is None:
                    store.add_execution(did, "skipped", reason, now)
                    continue
                if cls != "crypto" and not equity_window_open(now):
                    store.add_execution(did, "not_executed", MARKET_CLOSED, now)          # D7 option B
                    continue
                outcome = self._order_step(acc, sym, target, now, pm, run_id, did, d)
                placed = outcome["placed"]
                trades[cls] += placed
                placed_total += placed
                if max(trades.values()) > max_trades:
                    logger.warning("Council trade count %s exceeds the daily limit %d", trades, max_trades)
            except Exception as ex:
                logger.exception("Council decision handling for %s failed", sym)
                errors[sym] = f"decision handling failed: {ex}"[:200]
        return {"flips": flips, "orders": placed_total, "orders_enabled": gate0 is None, "errors": errors}

    def _order_step(self, acc, sym, target, now, pm, run_id, did, d) -> dict:
        """Under the account's trade lock: re-check, plan (the old ``broker.target`` rules), cancel / place via OMS."""
        with oms.lock_for(acc.id):
            gate = self.order_gate(acc)
            if gate is not None:
                store.add_execution(did, "not_executed", gate, now)
                return {"placed": 0}
            plan = acc.broker.plan_target(sym, target, "council", now, acc.params, pm)
            if plan["reject"] is not None:
                store.add_execution(did, "skipped", plan["reject"].reject_reason, now)
                return {"placed": 0}
            if plan["keep"]:
                store.add_execution(did, "skipped", "a Council order for this target is already working", now)
                return {"placed": 0}
            for p in plan["cancel"]:
                cid = p.get("client_order_id")
                if cid:
                    oms.cancel(acc, cid, f"replaced by a newer target — run {run_id}", actor="engine")
            if not plan["legs"]:
                store.add_execution(did, "skipped", "no order needed", now)
                return {"placed": 0}
            placed, first_cid, last = 0, None, None
            for side, n in plan["legs"]:
                o, _ = oms.place(acc, {"symbol": sym, "side": side, "qty": n, "origin": "council",
                                       "source_id": accounts.COUNCIL_SOURCE, "allow_reduce": True,
                                       "reason": f"Council: {d.action} {d.confidence * 100:.0f}% (run {run_id})"},
                                 actor="engine", prices={k: v for k, v in pm.items() if v is not None})
                last = o
                if o["state"] == "rejected":
                    break
                placed += 1
                first_cid = first_cid or o["client_order_id"]
                if o["state"] != "filled":
                    break                       # the next leg waits for the fill
            if placed:
                store.add_execution(did, "submitted", None, now, first_cid)
                _alert("council_order_filled", f"Council order: {sym} {plan['legs'][0][0]} {plan['legs'][0][1]:g}",
                       f"Target weight {target * 100:+.2f}% of equity (decision #{did}). {NOTICE}",
                       f"council-order-{first_cid}", account_id=acc.id, ref=first_cid)
            else:
                store.add_execution(did, "rejected", (last or {}).get("reject_reason"), now,
                                    (last or {}).get("client_order_id"))
            return {"placed": placed}

    # --- schedule / task ---------------------------------------------------------------------------------------
    def request_run(self) -> dict:
        """"Run now": 202 started (orders per class) / 409 running / 503 not active (SP §22.5, D8)."""
        if not self.active():
            raise CouncilError(503, "not_active", "The Council is not switched over yet.")
        if self.cycle_in_progress or self._manual_pending:
            raise CouncilError(409, "running", "A Council cycle is already running.")
        acc = self.account()
        gate = self.order_gate(acc)
        now = self.now()
        version, _council = self.params()
        # the run row exists BEFORE the answer, so "Run now" returns its id (SP §22.5); the task picks it up at once
        run_id = store.create_run("manual", now, version, session_label(now), gate is None, self.sandbox())
        self._manual_pending = run_id
        if self._wake is not None:
            self._wake.set()
        return {"run_id": run_id, "started": True,
                "orders": {"equity": gate is None and equity_window_open(now), "crypto": gate is None}}

    async def run_forever(self) -> None:
        self._wake = asyncio.Event()
        try:
            n = await asyncio.to_thread(store.sweep_unfinished, self.now())
            if n:
                logger.warning("%d unfinished Council run(s) from before the start marked as interrupted", n)
        except Exception:      # noqa: BLE001
            logger.exception("Council: unfinished runs not swept")
        await asyncio.sleep(STARTUP_DELAY_SECONDS)
        startup = True
        while not self._stopping:
            try:
                now = self.now()
                if self._manual_pending:
                    rid, self._manual_pending = self._manual_pending, False
                    await self.run("manual", rid if isinstance(rid, int) and not isinstance(rid, bool) else None)
                    continue
                if not self.active() or self.sandbox():
                    self.next_due_at = None
                else:
                    _v, council = self.params()
                    due = next_due(now, council["interval_minutes"], self.last_start)
                    self.next_due_at = due
                    if due is not None and now >= due:
                        await self.run("startup" if startup else "scheduled")
                        startup = False
                        continue
                await asyncio.to_thread(self._expire_stale_crypto, now)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Council task tick failed")
            startup = False
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    def _expire_stale_crypto(self, now: datetime) -> int:
        """D11: Council crypto orders unfilled after 60 min expire (OMS accepted → expired), at any time."""
        if not accounts.exists(self.account_id):
            return 0
        acc = self.account()
        n = 0
        with oms.lock_for(acc.id):
            for o in oms.list_orders(acc.id, states=oms.WORKING, limit=500):
                if o.get("origin") != "council" or not fund_params.is_crypto(o["symbol"]):
                    continue
                if now - _utc(o["created_at"]) >= timedelta(minutes=CRYPTO_STALE_MINUTES):
                    try:
                        acc.adapter.cancel(o["client_order_id"], "stale — re-evaluated at the next run")
                        oms.transition(o["client_order_id"], "expired", note="stale — re-evaluated at the next run",
                                       fields={"reject_reason": "stale — re-evaluated at the next run"})
                        n += 1
                    except Exception:      # noqa: BLE001
                        logger.exception("expiring %s failed", o["client_order_id"])
        return n

    def start(self) -> asyncio.Task:
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.get_running_loop().create_task(self.run_forever(), name="council-cycle")
        return self._task

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass


def _unallocated_problem(acc) -> Optional[str]:
    """The Council sizes on its own sub-ledger only: paper-council's ``_unallocated`` must hold 0 cash / positions
    (SP §22.1). An unreadable ledger is reported (no orders this run)."""
    from strategy_platform.portfolio import ledger
    try:
        st = ledger.source_state(acc.id, accounts.UNALLOCATED)
    except Exception as ex:          # noqa: BLE001
        return f"the sub-ledgers of paper-council could not be read ({type(ex).__name__})"
    held = [s for s, v in (st.get("positions") or {}).items() if abs(float(v.get("qty") or 0.0)) > 1e-9]
    if abs(float(st.get("cash") or 0.0)) > 0.005 or held:
        return "paper-council has unallocated capital — the Council sizes only on its own sub-ledger"
    return None


def _finish_unstarted(run_id: int, now, why: str) -> None:
    """A pre-created "Run now" row whose cycle could not start → finished as "error" (never left "running")."""
    try:
        store.finish_run(run_id, now, "error", None, 0.0, {"error": f"not started: {why}"[:300]})
    except Exception:          # noqa: BLE001
        logger.exception("could not finish the unstarted Council run %s", run_id)


def _premium_model():
    """The premium model id (to label judge_premium calls); None when the settings source is unreadable (the
    judge then reports the AI as unavailable itself)."""
    try:
        return llm_mod.models().get("premium")
    except llm_mod.LlmUnavailable:
        return None


def _position_rows(positions, acct) -> List[dict]:
    eq = getattr(acct, "equity", None) if acct is not None else None
    out = []
    for s, p in positions.items():
        mv = p.market_value
        out.append({"symbol": s, "qty": p.qty, "avg_price": p.avg_price, "mark": p.last_price,
                    "weight": (mv / eq) if (mv is not None and eq) else None, "subledger_equity": eq})
    return out


def _alert(kind, title, body, key, **kw) -> None:
    try:
        from strategy_platform.monitoring import alerts
        alerts.emit(kind, title, body, key, **kw)
    except Exception:          # noqa: BLE001 — an alert never stops a cycle
        logger.exception("Council alert %s failed", kind)


_default: Optional[CouncilService] = None


def default() -> Optional[CouncilService]:
    return _default


def set_default(svc: Optional[CouncilService]) -> None:
    global _default
    _default = svc
