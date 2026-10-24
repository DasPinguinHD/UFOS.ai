"""The strategy runtime (SPEC §17.3) — one runtime for backtest, paper and live: the
SAME data loader, evaluator, portfolio rules and decision block as ``backtest/engine.run``
(``portfolio/decision.decide``) and the SAME cost model (the real PaperBroker behind the
internal paper adapter), driven by the real clock instead of a simulated one.

One ``cycle(now)`` (sync; a worker thread; one at a time):
1. dead-man switch (``control.check_heartbeat``);
2. per account: quotes of the held symbols → accruals → queued fills → OMS sync →
   margin check → OMS sync (imports broker margin orders) → sub-ledger attribution →
   post-trade checks (drawdown kill; daily-loss events) — the old engine's order;
3. incubation bookkeeping: runs whose strategy left incubation / got a new version are
   aborted; completed sessions are recorded (traded / missed); a run whose last session
   is complete is completed (result);
4. allocation (when due: initial, strategy set changed, monthly, capital release);
5. decisions: per active run, once per new completed daily bar (≥ 20 min after the
   close / 00:20 UTC);
6. executions: decisions inside their execution window (next_open: open + 6 min … close;
   next_close: close − 10 min … close − 1 min; UTC days analogous) → desired quantities
   per source → netting (internal crosses + one order per symbol, participation cap) →
   OMS (pre-trade checks) — only while auto trading is ON and the account is not halted
   (else recorded ``not_executed``); a decision past its window is ``missed``; sources of
   ended runs are closed in the next tradable window;
7. attribution + equity snapshots (account and sub-ledgers).
``run_forever`` ticks every 60 s; a cycle runs when due (5 min while a session is open,
crypto is held/incubating or orders work; else 30 min) or when requested.

Part of UFOS.ai, a proof-of-concept portfolio project; PAPER TRADING ONLY.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import math
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..execution import accounts, control, oms
from ..execution.errors import TradingError
from ..storage import db

logger = logging.getLogger(__name__)

DECISION_LAG_MINUTES = 20
OPEN_DELAY = timedelta(minutes=6)
CLOSE_WINDOW = (timedelta(minutes=10), timedelta(minutes=1))
INTERVAL_ACTIVE_MIN = 5
INTERVAL_IDLE_MIN = 30
TICK_SECONDS = 60.0
PARTICIPATION_RATE = 0.01
ADV_WINDOW = 20
WARMUP_MARGIN = 5
CAL_DAYS_PER_SESSION = 7 / 5 * 1.04


class Runtime:
    def __init__(self, loader=None):
        self.loader = loader
        self._lock = threading.Lock()
        self.running = False
        self.cycle_in_progress = False
        self.last_cycle_at: Optional[str] = None
        self.next_cycle_at: Optional[datetime] = None
        self.last_error: Optional[str] = None
        self.cycles = collections.deque(maxlen=50)
        self.last_quotes: Dict[str, Dict[str, Any]] = {}
        self.decision_errors: Dict[str, dict] = {}
        self._wake = False
        self._event: Optional[asyncio.Event] = None
        self._stop = False

    # --- status / scheduling ------------------------------------------------------------------------

    def status(self) -> dict:
        return {"running": self.running, "cycle_in_progress": self.cycle_in_progress,
                "last_cycle_at": self.last_cycle_at,
                "next_cycle_at": self.next_cycle_at.isoformat(timespec="seconds") if self.next_cycle_at else None,
                "last_error": self.last_error, "cycles": list(self.cycles)[::-1]}

    def request_run(self) -> dict:
        self._wake = True
        if self._event is not None:
            try:
                self._event.set()
            except Exception:    # noqa: BLE001
                pass
        return {"queued": True}

    def _loader(self):
        if self.loader is None:
            from ..backtest.engine import DataLoader
            self.loader = DataLoader()
        return self.loader

    def _interval(self, now: datetime) -> int:
        from strategy_platform.data import market_feed as market
        try:
            if market.session_state(now).get("open"):
                return INTERVAL_ACTIVE_MIN
        except Exception:        # noqa: BLE001
            pass
        from strategy_platform.core import params as fund_params
        try:
            for acc in accounts.all_accounts():
                if any(fund_params.is_crypto(p["symbol"]) for p in acc.store.get_positions()):
                    return INTERVAL_ACTIVE_MIN
                if oms.list_orders(acc.id, states=oms.WORKING, limit=1):
                    return INTERVAL_ACTIVE_MIN
            from . import incubation
            if any(r["calendar"] == "utc" for r in incubation.active_runs()):
                return INTERVAL_ACTIVE_MIN
        except Exception:        # noqa: BLE001
            pass
        return INTERVAL_IDLE_MIN

    async def run_forever(self) -> None:
        self.running = True
        self._event = asyncio.Event()
        try:
            while not self._stop:
                try:
                    await asyncio.wait_for(self._event.wait(), TICK_SECONDS)
                except asyncio.TimeoutError:
                    pass
                self._event.clear()
                now = accounts.now()
                woken, self._wake = self._wake, False
                try:
                    await asyncio.to_thread(control.check_heartbeat, now)
                except Exception:        # noqa: BLE001
                    logger.exception("platform heartbeat check failed")
                if woken or self.next_cycle_at is None or now >= self.next_cycle_at:
                    await asyncio.to_thread(self.cycle, None, "manual" if woken else "schedule")
        finally:
            self.running = False

    def stop(self) -> None:
        self._stop = True
        self.request_run()

    # --- the cycle -------------------------------------------------------------------------------------

    def cycle(self, now: Optional[datetime] = None, trigger: str = "schedule") -> dict:
        if not self._lock.acquire(blocking=False):
            return {"status": "skipped", "reason": "a cycle is already running"}
        self.cycle_in_progress = True
        started = (now or accounts.now()).astimezone(timezone.utc)
        summary: Dict[str, Any] = {"decisions": 0, "orders": 0, "errors": {}, "trigger": trigger}
        status = "ok"
        try:
            now = started
            try:
                control.check_heartbeat(now)
            except Exception as ex:     # noqa: BLE001
                summary["errors"]["heartbeat"] = str(ex)[:200]
            try:
                accs = accounts.all_accounts()
            except Exception as ex:     # noqa: BLE001 — hedgefund.db unavailable: nothing runs, said so
                summary["errors"]["accounts"] = getattr(ex, "user_message", str(ex))[:300]
                accs = []
            for acc in accs:
                self._step(summary, f"{acc.id}:housekeeping", self._housekeeping, acc, now)
            self._step(summary, "incubation", self._incubation_bookkeeping, now)
            for acc in accs:
                self._step(summary, f"{acc.id}:allocation", self._allocate, acc, now)
            self._step(summary, "decisions", self._decide_all, now, summary)
            for acc in accs:
                self._step(summary, f"{acc.id}:execution", self._execute, acc, now, summary)
            for acc in accs:
                self._step(summary, f"{acc.id}:snapshot", self._finish_account, acc, now)
            status = "partial" if summary["errors"] else "ok"
            return {"status": status, "summary": summary}
        except Exception as ex:          # noqa: BLE001 — the cycle never raises into the loop
            logger.exception("runtime cycle failed")
            status = "error"
            summary["errors"]["cycle"] = f"{type(ex).__name__}: {ex}"[:300]
            return {"status": status, "summary": summary}
        finally:
            fin = accounts.now().astimezone(timezone.utc)
            self.last_cycle_at = started.isoformat(timespec="seconds")
            self.last_error = "; ".join(f"{k}: {v}" for k, v in summary["errors"].items())[:1000] or None
            self.cycles.append({"started_at": started.isoformat(timespec="seconds"),
                                "finished_at": fin.isoformat(timespec="seconds"), "status": status,
                                "summary": {"decisions": summary["decisions"], "orders": summary["orders"],
                                            "errors": dict(summary["errors"])}})
            try:
                self.next_cycle_at = started + timedelta(minutes=self._interval(started))
            except Exception:    # noqa: BLE001
                self.next_cycle_at = started + timedelta(minutes=INTERVAL_IDLE_MIN)
            self.cycle_in_progress = False
            self._lock.release()

    @staticmethod
    def _step(summary: dict, key: str, fn, *args) -> None:
        try:
            fn(*args)
        except TradingError as ex:
            summary["errors"][key] = ex.user_message[:300]
        except Exception as ex:          # noqa: BLE001 — one failing step never stops the others
            logger.exception("runtime step %s failed", key)
            summary["errors"][key] = (getattr(ex, "user_message", None) or f"{type(ex).__name__}: {ex}")[:300]

    # --- quotes -----------------------------------------------------------------------------------------

    def remember_quotes(self, account_id: str, pm: Dict[str, Any]) -> None:
        """Quotes fetched outside the cycle (an OMS order, retest 2026-10-23) join the account's last quotes, so a
        position filled between two cycles is valued at once — only real quotes (price > 0), never a stand-in."""
        cache = self.last_quotes.setdefault(account_id, {})
        for s, q in (pm or {}).items():
            if q is not None and (getattr(q, "price", 0) or 0) > 0:
                cache[s] = q

    def _quotes(self, acc, symbols) -> Dict[str, Any]:
        pm = {}
        for s in sorted(set(symbols)):
            try:
                q = accounts.quote_for(acc, s)
            except Exception as ex:      # noqa: BLE001 — no quote: valuation paused for it (never a stored price)
                logger.info("runtime: no quote for %s (%s)", s, getattr(ex, "user_message", ex))
                q = None
            pm[s] = q if q is not None and (getattr(q, "price", 0) or 0) > 0 else None
        cache = self.last_quotes.setdefault(acc.id, {})
        for s, q in pm.items():
            if q is None:
                cache.pop(s, None)
            else:
                cache[s] = q
        return pm

    # --- 2. housekeeping + post-trade -------------------------------------------------------------------

    def _housekeeping(self, acc, now: datetime) -> None:
        from strategy_platform.data import market_feed as market
        from strategy_platform.execution.paper_broker import StateUnavailable
        from ..portfolio import ledger, risk
        from ..portfolio import settings as pf_settings
        syms = [p["symbol"] for p in acc.store.get_positions()] + [o["symbol"] for o in acc.store.pending_orders()]
        can_hold = getattr(acc, "can_hold", None)      # the SPY benchmark only where the venue can hold it
        pm = self._quotes(acc, syms + (["SPY"] if can_hold is None or can_hold("SPY") else []))
        acc.adapter.daily_accruals(now, pm)
        acc.adapter.process_pending(now, pm)
        oms.sync(acc, now)
        if getattr(acc, "is_broker", False):
            # stage 8 (SPEC §18.5): reconcile every cycle — a mismatch halts this broker account
            from ..execution.brokers import reconcile
            res = reconcile.run(acc, "cycle", now)
            if res["status"] == "unreachable":
                raise TradingError(503, "broker_unreachable", f"{acc.id}: {res.get('error')}")
        try:
            view = acc.broker.account(pm)
        except StateUnavailable as ex:
            ledger.attribute(acc)
            raise TradingError(503, "valuation_paused", f"{ex} — margin check, drawdown check and snapshot wait")
        acc.adapter.check_margin(now, pm)
        oms.sync(acc, now)
        ledger.attribute(acc)
        view = acc.broker.account(pm)
        st = pf_settings.values(acc.id)
        halt = acc.halt()
        from strategy_platform.core import params as fund_params
        stale = [p["symbol"] for p in acc.store.get_positions()
                 if fund_params.is_crypto(p["symbol"]) and market.quote_is_stale(pm.get(p["symbol"]), now)]
        if not halt["active"] and view.drawdown_pct >= float(st["drawdown_kill_pct"]):
            if stale:
                raise TradingError(503, "stale_marks", f"drawdown {view.drawdown_pct:.2f} % ≥ kill "
                                                       f"{st['drawdown_kill_pct']:g} % but no live quote for "
                                                       f"{', '.join(stale)} — the drawdown kill waits")
            reason = (f"Drawdown kill: equity {view.drawdown_pct:.2f} % below its peak (limit "
                      f"{st['drawdown_kill_pct']:g} %)")
            control.set_autotrade(False, "engine")
            control.kill_account(acc, reason, "drawdown", "engine", pm)
            db.add_audit("engine", "post_trade_halt", {"account_id": acc.id, "kind": "drawdown",
                                                       "drawdown_pct": view.drawdown_pct, "equity": view.equity,
                                                       "peak_equity": view.peak_equity})
        dl = risk.day_loss(acc, now, view.equity, st["daily_loss_pct"])
        for cls, v in dl.items():
            if v["blocked"] and not v.get("latched"):
                risk.record_block(acc, cls, v, now, "engine")     # latched until the next NY / UTC day (M1)

    # --- 3. incubation bookkeeping -------------------------------------------------------------------------

    def _incubation_bookkeeping(self, now: datetime) -> None:
        from . import incubation
        for r in incubation.run_rows():
            if incubation.end_event(r["id"]) is not None:
                continue
            row = db.strategy_get(r["strategy_id"])
            if row is None or row["state"] != "incubation" or int(row["current_version"]) != int(r["version"]):
                why = ("a new version was saved — incubation ended" if row is not None and row["state"] == "draft"
                       else f"the strategy is {row['state'] if row else 'gone'} — incubation ended")
                incubation.add_event(r["id"], "aborted", {"reason": why, "actor": "engine"})
                db.add_audit("engine", "incubation_aborted", {"run_id": r["id"], "reason": why})
                continue
            last = incubation.last_completed_session(r["calendar"], now, DECISION_LAG_MINUTES)
            start = date.fromisoformat(r["start_date"])
            end = date.fromisoformat(r["end_date"])
            if last is None or last < start:
                continue
            days = incubation.sessions_between(r["calendar"], start, min(last, end))
            done = incubation._session_events(r["id"])
            from ..portfolio import ledger
            snaps = {s["day"]: s for s in ledger.snapshots(r["account_id"], r["source_id"], r["start_date"])}
            for d in days:
                if d.isoformat() in done:
                    continue
                traded = self._traded_on(r, d)
                snap = snaps.get(d.isoformat())
                incubation.record_session(r, d, traded, snap["equity"] if snap else None)
            if last >= end:
                incubation.complete(incubation.run_row(r["id"]), now)

    @staticmethod
    def _traded_on(run: dict, day: date) -> bool:
        with db._connect() as conn:
            rows = conn.execute("SELECT created_at, decision_ids_json, result_json FROM rt_executions WHERE"
                                " account_id = ?", (run["account_id"],)).fetchall()
            dec = {r["id"] for r in conn.execute("SELECT id FROM rt_decisions WHERE incubation_id = ?",
                                                 (run["id"],)).fetchall()}
        from . import incubation
        for r in rows:
            res = json.loads(r["result_json"])
            if res.get("status") != "executed":
                continue
            if not dec & set(json.loads(r["decision_ids_json"])):
                continue
            if incubation.session_day(run["calendar"], datetime.fromisoformat(r["created_at"])) == day:
                return True
        return False

    # --- 4. allocation ---------------------------------------------------------------------------------------

    def _allocate(self, acc, now: datetime) -> None:
        from ..portfolio import allocation
        why = allocation.due(acc, now)
        if why:
            allocation.rebalance(acc, why, "engine", now=now)

    # --- 5. decisions -----------------------------------------------------------------------------------------

    def _decide_all(self, now: datetime, summary: dict) -> None:
        from . import incubation
        for r in incubation.active_runs():
            key = f"decision:{r['strategy_id']}"
            try:
                if self.decide(r, now):
                    summary["decisions"] += 1
                self.decision_errors.pop(r["id"], None)
            except Exception as ex:      # noqa: BLE001 — that strategy waits; the others continue
                msg = (getattr(ex, "user_message", None) or f"{type(ex).__name__}: {ex}")[:300]
                if not isinstance(ex, TradingError):
                    logger.warning("runtime decision for %s failed: %s", r["strategy_id"], msg)
                self.decision_errors[r["id"]] = {"at": now.isoformat(timespec="seconds"), "error": msg}
                summary["errors"][key] = msg

    @staticmethod
    def latest_decision(run_id: str) -> Optional[dict]:
        with db._connect() as conn:
            r = conn.execute("SELECT * FROM rt_decisions WHERE incubation_id = ? AND status = 'ok' ORDER BY bar_date"
                             " DESC, created_at DESC LIMIT 1", (run_id,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["result"] = json.loads(d.pop("result_json"))
        return d

    def decide(self, run: dict, now: datetime) -> bool:
        """One decision for the newest completed bar (True when one was stored)."""
        import numpy as np
        from ..backtest import engine as bt
        from ..core import calendars
        from ..core.instruments import instrument
        from ..data.providers import tiingo as tiingo_mod
        from ..lifecycle import registry
        from ..portfolio import ledger
        from ..portfolio.decision import DecisionState, decide
        from ..strategy import evaluate as ev
        from ..strategy import portfolio, schema, universe, validator
        from ..strategy.panel import Panel
        from . import incubation
        cal = run["calendar"]
        bar = incubation.last_completed_session(cal, now, DECISION_LAG_MINUTES)
        start = date.fromisoformat(run["start_date"])
        first_bar = start - timedelta(days=1) if cal == "utc" else calendars.previous_trading_day(start)
        if bar is None or bar < first_bar:
            return False
        last = self.latest_decision(run["id"])
        if last is not None and last["bar_date"] >= bar.isoformat():
            return False
        spec = registry.get_version(run["strategy_id"], int(run["version"]))["spec"]
        res = validator.validate(spec)
        if not res.ok:
            raise TradingError(409, "invalid", "The strategy version is not valid any more.")
        full = schema.with_defaults(spec)
        symbols = universe.resolve(full["universe"])
        classes = {s: instrument(s).asset_class for s in symbols}
        vol_target = (full["risk"] or {}).get("vol_target_annual")
        warmup = max(int(res.facts["max_lookback"]), ADV_WINDOW,
                     (bt.VOL_TARGET_LOOKBACK + 1) if vol_target else 0) + WARMUP_MARGIN
        days_back = warmup + 2 if cal == "utc" else int(math.ceil((warmup + 2) * CAL_DAYS_PER_SESSION)) + 10
        load_start = bar - timedelta(days=days_back)
        loader = self._loader()
        loaded = bt._load_symbols(loader, list(symbols), classes, "equity", load_start, bar, emit=lambda *a: None,
                                  check_cancel=lambda: None, unknown_symbol=tiingo_mod.UnknownSymbol)
        end_close = calendars.session_bounds(bar)[1] if cal == "nyse" else None
        sig_bars, split_close, adv = {}, {}, {}
        for s, (e, _nov) in zip(symbols, loaded):
            if classes[s] == "crypto":
                rows = e["bars"][0]
                rows = [r for r in rows if (bt._utc_date(r["ts"]) <= bar if cal == "utc"
                                            else bt._crypto_known(r) <= end_close)]
                if rows:
                    split_close[s] = float(rows[-1]["close"])
            else:
                rows = [r for r in e["all"][0] if bt._ny_date(r["ts"]) <= bar]
                srows = [r for r in e["split"][0] if bt._ny_date(r["ts"]) <= bar]
                if srows and bt._ny_date(srows[-1]["ts"]) == bar:
                    split_close[s] = float(srows[-1]["close"])
                vol = e.get("volume")
                vals = []
                if vol:
                    vrows = [r for r in vol[0] if date.fromisoformat(str(r["date"])[:10]) <= bar]
                    vals = [float(r["dollar_volume_split"]) for r in vrows[-ADV_WINDOW:]
                            if r.get("dollar_volume_split") is not None]
                adv[s] = (math.fsum(vals) / len(vals)) if len(vals) == ADV_WINDOW else None
            if rows:
                sig_bars[s] = rows
        present = [s for s in symbols if s in sig_bars]
        if not present:
            raise TradingError(503, "no_data", f"No daily bars for the strategy's symbols up to {bar.isoformat()}.")
        panel = Panel.from_bars(sig_bars, symbols=present)
        if not panel.dates or panel.dates[-1] != bar:
            raise TradingError(503, "waiting_data", f"The {bar.isoformat()} daily bar is not available yet — the "
                                                    "decision waits (no stale signal).")
        ev.check_cost(full, panel)
        ext = loader.ext_values(full, panel) if hasattr(loader, "ext_values") else None
        signal = ev.evaluate(full, panel, ext_values=ext)
        mask = portfolio.rebalance_mask(full, panel.dates)
        i = len(panel.dates) - 1
        last_rebalance = None
        for k in range(i, -1, -1):
            if mask[k]:
                last_rebalance = k
                break
        state = DecisionState.from_dict(last["result"].get("state") if last else None)
        hold = ledger.holdings(run["account_id"]).get(run["source_id"], {})
        st = ledger.source_state(run["account_id"], run["source_id"])
        positions = [{"symbol": s, "qty": v["qty"], "avg_price": v["avg_price"]} for s, v in st["positions"].items()]
        rets = [r for _d, r in ledger.daily_returns(run["account_id"], run["source_id"], run["start_date"])]
        nav, peak = 1.0, 1.0
        for r_ in rets:
            nav *= (1.0 + r_)
            peak = max(peak, nav)
        dd = (peak - nav) / peak * 100.0 if peak > 0 else 0.0
        warnings = bt._Warnings()
        stats: dict = {}
        targets, force, reason = decide(
            full, panel, signal, mask, i, first=last is None, last_rebalance=last_rebalance, state=state,
            positions=positions, close_price=lambda s_: split_close.get(s_), drawdown_pct=dd, held=set(hold),
            calendar=cal, day=bar, warnings=warnings, stats=stats)
        win = windows(cal, bar, full["execution"]["timing"])
        result = {"targets": targets, "force": sorted(force), "reason": reason, "state": state.to_dict(),
                  "ref_prices": split_close, "adv": adv, "timing": full["execution"]["timing"],
                  "window": {"start": win[0].isoformat(timespec="seconds"), "end": win[1].isoformat(timespec="seconds")},
                  "warnings": warnings.items(), "drawdown_pct": dd,
                  "signal": {s: (None if not np.isfinite(signal[i][j]) else float(signal[i][j]))
                             for j, s in enumerate(panel.symbols)}}
        did = f"rd-{uuid.uuid4().hex[:12]}"
        with db._connect() as conn:
            conn.execute("INSERT INTO rt_decisions (id, account_id, source_id, strategy_id, version, incubation_id,"
                         " bar_date, created_at, status, result_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ok', ?)",
                         (did, run["account_id"], run["source_id"], run["strategy_id"], int(run["version"]), run["id"],
                          bar.isoformat(), now.isoformat(timespec="seconds"), json.dumps(result, default=str)))
        return True

    # --- 6. executions --------------------------------------------------------------------------------------

    @staticmethod
    def _executed_ids(account_id: str) -> Dict[str, str]:
        with db._connect() as conn:
            rows = conn.execute("SELECT decision_ids_json, result_json FROM rt_executions WHERE account_id = ?",
                                (account_id,)).fetchall()
        out = {}
        for r in rows:
            st = json.loads(r["result_json"]).get("status")
            for d in json.loads(r["decision_ids_json"]):
                out[d] = st
        return out

    def _record_execution(self, account_id: str, decision_ids: List[str], result: dict, now: datetime) -> str:
        eid = result.get("id") or f"ex-{uuid.uuid4().hex[:10]}"
        with db._connect() as conn:
            conn.execute("INSERT INTO rt_executions (id, account_id, created_at, decision_ids_json, result_json) VALUES"
                         " (?, ?, ?, ?, ?)", (eid, account_id, now.isoformat(timespec="seconds"),
                                              json.dumps(decision_ids), json.dumps(result, default=str)))
        return eid

    def _execute(self, acc, now: datetime, summary: dict) -> None:
        from strategy_platform.core import params as fund_params
        from strategy_platform.execution.paper_broker import round_qty
        from ..portfolio import ledger, netting
        from . import incubation
        from ..monitoring import decay as mon_decay
        done = self._executed_ids(acc.id)
        runs = [r for r in incubation.active_runs() if r["account_id"] == acc.id]
        # stage 9 (SPEC §19.2/§19.14): a strategy paused by the decay monitor sends no new or increasing orders
        # (positions kept) until the user decides — only its own risk-REDUCING protective exits (the decision's
        # ``force`` symbols: stop loss, take profit, drawdown stop) still run (default, user away 2026-10-18 —
        # confirm); an unreadable pause table fails this step closed (no orders at all)
        paused = mon_decay.paused_run_ids()
        protective_only: Dict[str, List[str]] = {}
        active_sources = {r["source_id"] for r in runs}
        due: List[Tuple[dict, dict]] = []
        for r in runs:
            d = self.latest_decision(r["id"])
            if d is None or d["id"] in done:
                continue
            w0 = datetime.fromisoformat(d["result"]["window"]["start"])
            w1 = datetime.fromisoformat(d["result"]["window"]["end"])
            if now < w0:
                continue
            if r["id"] in paused:
                held_now = ledger.holdings(acc.id).get(r["source_id"], {})
                exits = sorted(s for s in (d["result"].get("force") or []) if abs(float(held_now.get(s) or 0.0)) > 1e-12)
                if not exits:
                    self._record_execution(acc.id, [d["id"]], {"status": "not_executed",
                                                               "reason": mon_decay.PAUSE_REASON}, now)
                    continue
                protective_only[r["id"]] = exits
            if now >= w1:
                self._record_execution(acc.id, [d["id"]], {"status": "missed", "reason": "the execution window "
                                       f"({w0.isoformat(timespec='minutes')} – {w1.isoformat(timespec='minutes')}) "
                                       "passed without an execution"}, now)
                continue
            due.append((r, d))
        hold = ledger.holdings(acc.id)
        closing = {s: q for s, q in hold.items() if s.startswith("s-") and s not in active_sources and q}
        if not due and not closing:
            return
        dids = [d["id"] for _r, d in due]
        prot_src = {r["source_id"]: r for r, _d in due if r["id"] in protective_only}
        auto = control.autotrade()
        halt = acc.halt()
        if not auto["enabled"] or halt["active"]:
            why = "auto trading off" if not auto["enabled"] else f"account halted ({halt['reason'] or halt['kind']})"
            if dids:
                self._record_execution(acc.id, dids, {"status": "not_executed", "reason": why}, now)
            return
        # desired quantities per source — computed symbol by symbol, reductions first, from the sources'
        # CURRENT equity before each symbol (exactly like the backtest's execution, backtest/engine._Sim.execute,
        # which re-reads the equity before every target(): one source = the old engine's target() behaviour)
        plans: List[Tuple[str, dict]] = []
        for r, d in due:
            res = d["result"]
            if r["id"] in protective_only:          # paused: its protective exits only (reduce to 0), nothing else
                plans.append((r["source_id"], {"want": {s: 0.0 for s in protective_only[r["id"]]},
                                               "ref": res.get("ref_prices") or {}, "adv": res.get("adv") or {}}))
                continue
            targets, force = res.get("targets"), set(res.get("force") or [])
            want: Dict[str, float] = {}
            held = hold.get(r["source_id"], {})
            if targets is not None:
                for s in set(targets) | set(held):
                    want[s] = float(targets.get(s, 0.0))
            for s in force:
                want[s] = 0.0
            plans.append((r["source_id"], {"want": want, "ref": res.get("ref_prices") or {}, "adv": res.get("adv") or {}}))
        for sid, q in closing.items():
            plans.append((sid, {"want": {s: 0.0 for s in q}, "ref": {}, "adv": {}}))
        syms = set()
        for _sid, p in plans:
            syms |= set(p["want"])
        pm = self._quotes(acc, list(syms) + [p["symbol"] for p in acc.store.get_positions()])
        marks = {s: (float(q.price) if q is not None else None) for s, q in pm.items()}
        notes: List[str] = []
        for rid, ex in protective_only.items():
            notes.append(f"{rid}: paused by the decay monitor — protective exits only ({', '.join(ex)})")
        refs: Dict[str, float] = {}
        advs: Dict[str, Optional[float]] = {}
        for _sid, p in plans:
            for s, v in p["ref"].items():
                refs.setdefault(s, float(v))
            for s, v in p["adv"].items():
                advs.setdefault(s, v)

        def source_deltas(sym: str) -> Dict[str, float]:
            hold_now = ledger.holdings(acc.id)
            out: Dict[str, float] = {}
            px = marks.get(sym)
            for sid, p in plans:
                if sym not in p["want"]:
                    continue
                h = float(hold_now.get(sid, {}).get(sym, 0.0))
                if p["want"][sym] == 0.0:
                    desired = 0.0
                else:
                    st = ledger.source_state(acc.id, sid, marks)
                    eq = st.get("equity")
                    if eq is None:
                        notes.append(f"{sid}: not valued (a held symbol has no quote) — its {sym} order waits")
                        continue
                    raw = p["want"][sym] * eq / px
                    desired = round_qty(sym, abs(raw)) * (1.0 if raw >= 0 else -1.0)
                if abs(desired - h) > 1e-9:
                    out[sid] = desired - h
            return out

        work = []
        for sym in sorted(syms):
            if not acc.broker._can_fill(now, sym):
                # only reachable for sources of ended runs (decision windows lie inside the session): an equity
                # close waits for the session instead of producing a rejected order every cycle
                continue
            if marks.get(sym) is None:
                if any(p["want"].get(sym) or hold.get(sid, {}).get(sym) for sid, p in plans if sym in p["want"]):
                    notes.append(f"{sym}: no execution price — not traded")
                continue
            dl = source_deltas(sym)
            if not dl:
                continue
            h_acc = float((acc.store.get_position(sym) or {}).get("qty") or 0.0)
            target = h_acc + math.fsum(dl.values())
            reducing = abs(target) <= 1e-9 or (target * h_acc > 0 and abs(target) < abs(h_acc))
            work.append((0 if reducing else 1, sym))
        eid = f"ex-{uuid.uuid4().hex[:10]}"
        ts = now.isoformat(timespec="seconds")
        placed: List[str] = []
        crosses_out: List[dict] = []
        with oms.lock_for(acc.id):
            for _rank, sym in sorted(work):
                if not control.autotrade()["enabled"] or acc.halt()["active"]:
                    notes.append("stopped: auto trading off / account halted during the execution")
                    break
                ledger.attribute(acc)
                dl = source_deltas(sym)
                if not dl:
                    continue
                cap = None
                if not fund_params.is_crypto(sym):
                    a = advs.get(sym)
                    if a is None and any(sid in active_sources for sid in dl):
                        cap = 0.0
                        notes.append(f"{sym}: no complete 20-day volume — its order is refused (no default volume)")
                    elif a is not None:
                        cap = PARTICIPATION_RATE * a / marks[sym]
                acct_qty = {sym: float((acc.store.get_position(sym) or {}).get("qty") or 0.0)}
                plan = netting.net({sym: dl}, acct_qty, {sym: cap} if cap is not None else None)
                rows = []
                for c in plan["crosses"]:
                    crosses_out.append(c)
                    for sid, q in c["legs"]:
                        rows.append({"source_id": sid, "kind": "cross", "symbol": sym, "qty": q, "price": marks[sym],
                                     "amount": -q * marks[sym], "ref": eid, "ts": ts,
                                     "note": "internal cross (netting)"})
                if rows:
                    with db._connect() as conn:
                        ledger.insert_entries(conn, acc.id, rows, ts)
                for o in plan["orders"]:
                    if o.get("capped"):
                        notes.append(f"{sym}: cut to 1 % of the 20-day average dollar volume")
                    for k, leg in enumerate(o["legs"], start=1):
                        order, _created = oms.place(acc, {
                            "symbol": sym, "side": leg["side"], "qty": leg["qty"], "origin": "runtime",
                            "reason": "strategy rebalance", "ref_price": refs.get(sym),
                            "allocations": leg["allocations"], "execution_id": eid, "allow_reduce": True,
                            "client_order_id": f"rt-{eid}-{sym}-{k}"}, actor="engine", prices=pm)
                        placed.append(order["client_order_id"])
                        summary["orders"] += 1
                        for src_id, aq in leg["allocations"]:
                            if src_id in prot_src:      # stage 9 (§19.14): every protective exit while paused is recorded
                                mon_decay.record_protective_exit(prot_src[src_id], order, aq,
                                                                 protective_only[prot_src[src_id]["id"]])
                        ledger.attribute(acc)
                        if order["state"] != "filled":
                            if len(o["legs"]) > k:
                                notes.append(f"{sym}: the second leg waits until the first is filled")
                            break
        ledger.attribute(acc)
        if not dids and not placed and not crosses_out:
            return                      # only ended sources to close and nothing tradable now: nothing to record
        sources_info = {sid: {"targets": p["want"]} for sid, p in plans}
        self._record_execution(acc.id, dids, {"id": eid, "status": "executed", "orders": placed,
                                              "crosses": crosses_out, "notes": notes, "sources": sources_info}, now)
        try:
            db.add_audit("engine", "runtime_execution", {"account_id": acc.id, "execution_id": eid, "decisions": dids,
                                                         "orders": placed, "crosses": len(crosses_out)})
        except Exception:        # noqa: BLE001
            logger.exception("audit row runtime_execution failed")

    # --- 7. attribution + snapshots ---------------------------------------------------------------------------

    def _finish_account(self, acc, now: datetime) -> None:
        from strategy_platform.execution.paper_broker import StateUnavailable
        from ..portfolio import ledger
        from . import incubation
        ledger.attribute(acc)
        pm = dict(self.last_quotes.get(acc.id) or {})
        for p in acc.store.get_positions():
            if p["symbol"] not in pm:
                pm.update(self._quotes(acc, [p["symbol"]]))
        try:
            spy = pm.get("SPY")
            acc.adapter.snapshot_equity(now, pm, spy.price if spy is not None else None)
        except StateUnavailable:
            return
        marks = {s: (float(q.price) if q is not None else None) for s, q in pm.items()}
        cals = {r["source_id"]: r["calendar"] for r in incubation.run_rows() if r["account_id"] == acc.id}
        ts = now.isoformat(timespec="seconds")
        for sid, st in ledger.all_states(acc.id, marks).items():
            cal = cals.get(sid, "nyse")
            ledger.snapshot(acc.id, sid, incubation.session_day(cal, now).isoformat(), ts, st)


def windows(calendar: str, bar: date, timing: str) -> Tuple[datetime, datetime]:
    """Execution window of the decision taken on ``bar`` (SPEC §17.3)."""
    from ..core import calendars
    if calendar == "utc":
        d1 = datetime(bar.year, bar.month, bar.day, tzinfo=timezone.utc) + timedelta(days=1)
        if timing == "next_close":
            return d1 + timedelta(hours=23, minutes=49), d1 + timedelta(hours=23, minutes=59)
        return d1, d1 + timedelta(days=1)
    nxt = calendars.next_trading_day(bar)
    o, c = calendars.session_bounds(nxt)
    if timing == "next_close":
        return c - CLOSE_WINDOW[0], c - CLOSE_WINDOW[1]
    return o + OPEN_DELAY, c


_runtime: Optional[Runtime] = None


def default() -> Runtime:
    global _runtime
    if _runtime is None:
        _runtime = Runtime()
    return _runtime


def set_default(rt: Optional[Runtime]) -> None:
    global _runtime
    _runtime = rt
