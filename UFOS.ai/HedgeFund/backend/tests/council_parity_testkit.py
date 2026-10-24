"""Stage 10 S10-C R1 — the Council parity harness (SP §22.10): the OLD path (``fund.engine.FundEngine`` full cycles on a
temp newsroom.db copy of the old account) against the NEW path (``council.service`` + the platform runtime's
housekeeping on a temp hedgefund.db, after the REAL §17.9 migration and the REAL R2 switch-over of the same old
account), both driven by ONE
injected clock on identical ticks, a deterministic fake market (daily bars, intraday quotes, headlines, no
dividends) and ONE scripted fake LLM keyed by (feature, canonical request hash).

The fake LLM answers the OLD path's requests (deterministically from the request hash) and records them; the NEW path
may only send requests the OLD path sent at the same tick — an unknown request is recorded as a miss (the test fails
on any miss) and answered with ``LlmUnavailable`` so the run still finishes. The complete call logs of both paths
(feature, model, request hash, max_tokens, temperature, order) are compared per tick.

``compare_tick`` returns the mismatches of one tick (empty = parity): agent signals (score, confidence, source,
details), the judge verdicts (action, confidence, source), target weights, the order intents (symbol, side, qty) and
the AI cost of the run; ``compare_books`` the account at the end (cash, positions, equity ≤ 1e-6 USD).

The deliberate differences of SP §22.10 are listed in ``DELIBERATE_DIFFERENCES`` (the user approves the list before
R2); the lockstep ticks stay inside the window where none of them applies (in-session, after the opening grace).
Part of UFOS.ai, a proof-of-concept portfolio project — paper trading only. Offline, no network.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import importlib
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from fund import store as fund_store
from fund.engine import FundEngine
from newsroom import db as newsroom_db
from strategy_platform.core import calendars
from strategy_platform.core import params as P
from strategy_platform.core.llm import LlmUnavailable
from strategy_platform.core.types import Bar, OrderRequest, Quote
from strategy_platform.council import service as svc_mod
from strategy_platform.council import spend
from strategy_platform.council import store as cs
from strategy_platform.council import switchover
from strategy_platform.execution import accounts, control, migration, oms
from strategy_platform.execution.paper_broker import PaperBroker
from strategy_platform.storage import db

UTC = timezone.utc
EQUITIES = ["SPY", "QQQ", "TLT", "GLD", "IWM", "XLE"]       # XLE: not shortable in the fixture
CRYPTO = ["BTC-USD", "ETH-USD"]
UNIVERSE = EQUITIES + CRYPTO
BASE = {"SPY": 500.0, "QQQ": 420.0, "TLT": 90.0, "GLD": 210.0, "IWM": 200.0, "XLE": 95.0, "BTC-USD": 60000.0,
        "ETH-USD": 2500.0}
OLD_PARAMS = {"watchlist": UNIVERSE, "not_shortable": ["XLE"], "min_hold_days": 0, "max_trades_per_day": 20,
              "change_threshold": 0.05, "interval_minutes": 30}
FLOAT_TOL = 1e-12
BOOK_TOL = 1e-6

# SP §22.10 "Deliberate differences" (the user approves this list before R2) — (difference, old, council, handling)
DELIBERATE_DIFFERENCES = (
    ("equity orders outside the window / in the opening grace (D7)", "queued for the open",
     "none — 'market closed — no order placed'", "decisions compared, off-window equity placement excluded"),
    ("first scheduled cycle of a session", "open + 5 s", "open + 5 min grace + 5 s",
     "schedule-equivalence test uses the new rule (old trace shifted by the grace)"),
    ("replace of a queued equity order off-window", "replaced its queued order", "no queued equity orders (D7)",
     "separate scenario test only"),
    ("two-leg equity reversal while closed", "first leg queued", "decision only, no order (D7)",
     "separate scenario test only"),
    ("stale crypto order expiry (D11)", "2 x risk-watch interval, risk watch only", "fixed 60 min at any time",
     "separate test"),
    ("crypto risk watch (D2)", "own cycle every closed_interval_minutes", "runtime housekeeping (§22.6)",
     "account level only"),
    ("pre-trade checks", "none", "platform checks", "fixtures inside the limits; separate rejection test"),
    ("OMS state / audit / event names", "old names", "platform names", "mapped table"),
    ("dropped features (§22.9)", "manual accept, CSV exports, /fund/backtest, toasts", "— / alert kinds",
     "not compared"),
)
# the old engine's skip reasons → the Council's execution reasons (one fixed mapping table, SP §22.10)
SKIP_REASON_MAP = {
    # the old engine's wording -> the Council's (same meaning; gate names differ by design, SP §22.10 row "names")
    "engine halted": {("not_executed", "the account is halted")},
    "autopilot switched off": {("not_executed", "auto trading is off"), ("not_executed", "Council orders are off")},
}
# old recommendation status -> the Council's execution outcome
STATUS_MAP = {"executed": "submitted", "skipped": "skipped"}


def _h(*parts) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode("utf-8")).hexdigest()[:12], 16)


def _u(*parts) -> float:
    """A deterministic uniform number in [0, 1)."""
    return (_h(*parts) % 10 ** 9) / 10 ** 9


class World:
    """The fake market both paths read: daily bars up to the previous NY day, intraday quotes as a function of the
    injected clock (timestamped with it), fixed headlines, no dividends. Pure functions of (symbol, time)."""

    def __init__(self, clock, n_days: int = 420):
        self.clock = clock
        self.n_days = n_days
        self.fail_quotes: set = set()
        self.shock: Dict[str, float] = {}          # symbol -> price factor (drawdown scenarios)
        self._closes: Dict[str, Dict[date, float]] = {}

    @staticmethod
    def ny_date(t: datetime) -> date:
        return calendars.ny_date(t)

    def _series(self, sym: str, until: date) -> Dict[date, float]:
        s = self._closes.setdefault(sym, {})
        d0 = date(2025, 1, 1)
        p = BASE[sym] * 0.8
        d = d0
        if s:
            last = max(s)
            if last >= until:
                return s
            d, p = last + timedelta(days=1), s[last]
        while d <= until:
            if sym in CRYPTO or d.weekday() < 5:
                drift = 0.0006 if _h(sym, "trend") % 2 else -0.0002
                p = p * (1 + drift + (_u(sym, d.isoformat()) - 0.5) * (0.06 if sym in CRYPTO else 0.03))
                s[d] = p
            d += timedelta(days=1)
        return s

    def daily_bars(self, symbol: str, days: int = 400) -> List[Bar]:
        today = self.ny_date(self.clock())
        s = self._series(symbol, today - timedelta(days=1))
        out = []
        for d in sorted(k for k in s if k < today)[-days:]:
            c = s[d]
            out.append(Bar(date=d.isoformat(), open=c * 0.998, high=c * 1.01, low=c * 0.99, close=c, volume=1e6))
        return out

    def price(self, symbol: str, t: datetime) -> float:
        today = self.ny_date(t)
        s = self._series(symbol, today - timedelta(days=1))
        prev = s[max(k for k in s if k < today)]
        minutes = t.hour * 60 + t.minute
        phase = _u(symbol, "phase") * 6.283
        amp = 0.03 if symbol in CRYPTO else 0.015
        return round(prev * self.shock.get(symbol, 1.0) *
                     (1 + amp * math.sin(minutes / 47.0 + phase + today.toordinal() * 0.7)), 6)

    def quote(self, symbol: str) -> Optional[Quote]:
        if symbol in self.fail_quotes or symbol not in BASE:
            return None
        t = self.clock()
        today = self.ny_date(t)
        s = self._series(symbol, today - timedelta(days=1))
        prev = s[max(k for k in s if k < today)]
        return Quote(symbol=symbol, price=self.price(symbol, t), prev_close=prev,
                     ts=t.astimezone(UTC).isoformat(timespec="seconds"))

    def dividends(self, symbol: str, since) -> list:
        return []

    def news(self, symbol: str, limit: int = 8) -> List[dict]:
        if symbol in CRYPTO:
            return []
        t = self.clock().astimezone(UTC)
        day = t.date()
        out = []
        for back in (0, 1):
            d = day - timedelta(days=back)
            ts = datetime(d.year, d.month, d.day, 12, 0, tzinfo=UTC)
            if ts <= t:
                out.append({"title": f"{symbol}: market update {d.isoformat()}", "publisher": "Fixture Wire",
                            "published_at": ts.isoformat(timespec="seconds")})
        return out[:limit]

    # the FundEngine / CouncilService feed protocol
    def feed(self):
        return self


REAL_SYMBOLS = ["SPY", "QQQ", "TLT", "GLD", "BTC-USD"]
REAL_BARS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "council_parity_real_bars.json")


class RealWorld(World):
    """The frozen real-data fixture (SP §22.10 (c)): the daily bars of ``REAL_SYMBOLS`` come from
    ``council_parity_real_bars.json`` (last 260 days up to 2026-10-23, exported once from a copy of the stage-10
    backup); intraday quotes wiggle around the previous real close as in ``World``."""

    def __init__(self, clock):
        super().__init__(clock)
        with open(REAL_BARS, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.real = {sym: {date.fromisoformat(b[0]): b for b in v["bars"]} for sym, v in doc["symbols"].items()}

    def _series(self, sym: str, until: date) -> Dict[date, float]:
        if sym not in self.real:
            return super()._series(sym, until)
        return {d: b[4] for d, b in self.real[sym].items() if d <= until}

    def daily_bars(self, symbol: str, days: int = 400) -> List[Bar]:
        if symbol not in self.real:
            return super().daily_bars(symbol, days)
        today = self.ny_date(self.clock())
        rows = [b for d, b in sorted(self.real[symbol].items()) if d < today][-days:]
        return [Bar(date=b[0], open=b[1], high=b[2], low=b[3], close=b[4], volume=b[5]) for b in rows]


class ScriptedLLM:
    """One fake LLM for both paths, keyed by (feature, canonical request hash). See the module docstring."""

    SYMS = re.compile(r"these symbols[^:]*: ([^\n]*?)\.\n")

    def __init__(self):
        self.script: Dict[Tuple[str, str], tuple] = {}
        self.logs: Dict[str, List[tuple]] = {"old": [], "new": []}
        self.requests: Dict[str, List[list]] = {"old": [], "new": []}     # the raw messages (diagnostics)
        self.misses: List[tuple] = []
        self.fail: Dict[str, LlmUnavailable] = {}      # feature -> the failure both paths get
        self.fail_models: Dict[str, LlmUnavailable] = {}   # model -> the failure both paths get
        # policy(feature, symbols, request_hash) -> answer list, or None for the default scripted answer
        self.policy = None
        # on_call(path, feature): runs at each AI call BEFORE the answer (mid-cycle events: kill, switch off)
        self.on_call = None
        self.path: Optional[str] = None

    def _answer(self, feature: str, messages, model: str, h: str):
        user = next((m["content"] for m in messages if m.get("role") == "user"), "")
        m = self.SYMS.search(user)
        syms = [s.strip() for s in (m.group(1).split(",") if m else []) if s.strip()]
        forced = self.policy(feature, syms, h) if self.policy is not None else None
        if forced is not None:
            data = forced
        elif feature == "fund-judge":
            acts = ("LONG", "SHORT", "FLAT", "LONG")
            data = [{"symbol": s, "action": acts[_h(h, s) % 4], "confidence": round(0.5 + _u(h, s, "c") * 0.45, 3),
                     "rationale": f"Scripted verdict for {s}."} for s in syms]
        else:
            data = [{"symbol": s, "score": round(_u(h, s) * 1.6 - 0.8, 3),
                     "confidence": round(0.4 + _u(h, s, "c") * 0.5, 3), "summary": f"Scripted reading for {s}."}
                    for s in syms]
        usage = {"cost_usd": round(0.0005 + (_h(h) % 1000) / 1e6, 8), "prompt_tokens": 100 + _h(h, "p") % 900,
                 "completion_tokens": 20 + _h(h, "o") % 200}
        return data, json.dumps(data), model, usage

    def for_path(self, path: str):
        async def llm(messages, model, max_tokens, feature, feature_label=None, temperature=0.2):
            h = spend.request_hash(messages, model)
            self.logs[path].append((feature, model, h, int(max_tokens), float(temperature)))
            if self.on_call is not None:
                self.on_call(path, feature)
            self.requests[path].append(messages)
            if feature in self.fail:
                raise self.fail[feature]
            if model in self.fail_models:
                raise self.fail_models[model]
            key = (feature, h)
            if path == "old":
                if key not in self.script:
                    self.script[key] = self._answer(feature, messages, model, h)
                return self.script[key]
            if key not in self.script:
                self.misses.append((feature, model, h))
                raise LlmUnavailable("error", "unscripted request (parity harness)")
            return self.script[key]
        return llm


def _copy_db(src: str, dst: str) -> None:
    a, b = sqlite3.connect(src), sqlite3.connect(dst)
    try:
        a.backup(b)
    finally:
        a.close()
        b.close()


class Harness:
    """Builds both paths from ONE old account (the case is an ``sp_trading_testkit.TradingCase`` with
    ``open_main = False``) and drives them tick by tick."""

    def __init__(self, case, seed_orders=(("SPY", "buy", 40), ("TLT", "short", 60), ("BTC-USD", "buy", 0.2)),
                 old_params: Optional[dict] = None, calendar_fn=None, insider_fn=None, macro_fn=None,
                 macro_status_fn=None, world_cls=None):
        self.case = case
        self.world = (world_cls or World)(case.clock)
        self.newsroom = dict(calendar_fn=calendar_fn or (lambda: []), insider_fn=insider_fn or (lambda: []),
                             macro_fn=macro_fn or (lambda: {}), macro_status_fn=macro_status_fn or (lambda: {"ok": True}))
        # the OLD store books its AI spend on the WALL-CLOCK UTC day when called without ``now`` (the old engine's
        # Budget does); the harness pins that one read to the injected clock, so both paths count the same day
        orig_day = fund_store._utc_day
        fund_store._utc_day = lambda now=None: orig_day(now if now is not None else case.clock())
        case.addCleanup(setattr, fund_store, "_utc_day", orig_day)
        self.llm = ScriptedLLM()
        accounts.configure(quote_fn=self.world.quote, dividends_fn=self.world.dividends,
                           bars_fn=self.world.daily_bars, clock=case.clock)
        params = P.merge(None, dict(OLD_PARAMS, **(old_params or {})))
        fund_store.save_params(params, "test")
        self.legacy = PaperBroker(fund_store, self.world.quote, clock=case.clock, dividends_fn=self.world.dividends,
                                  bars_fn=self.world.daily_bars)
        # the old account is seeded in the previous session (filled, never queued: queued orders block §17.9)
        start = case.clock()
        prev = calendars.previous_trading_day(calendars.ny_date(start))
        seed_at = calendars.session_bounds(prev)[0] + timedelta(minutes=90)
        case.set_now(seed_at)
        self.legacy.ensure_initialized(seed_at)
        for sym, side, qty in seed_orders:
            self.legacy.submit(OrderRequest(symbol=sym, side=side, qty=qty, origin="manual"), seed_at)
        self.legacy.snapshot_equity(seed_at, None, {})
        case.set_now(start)
        fund_store.set_autopilot(False)
        # the old path continues on an untouched copy; the REAL migration runs on the original
        nr = os.path.join(case.tmp, "newsroom.db")
        self.old_db = os.path.join(case.tmp, "newsroom_old.db")
        _copy_db(nr, self.old_db)
        eng0 = FundEngine(self.world, self.legacy, fund_store, clock=case.clock, startup_delay=0.0)
        eng0.platform_migrated_fn = migration.platform_migrated
        control.set_legacy_engine(eng0)
        self.migration = migration.run(None, "", "user", eng0)
        # NEW path: the REAL R2 switch-over (SP §22.8) on the migrated temp store — Council params copied from the
        # old ones, heartbeat setting, spend seed (temp cost log), legacy import; then the user's "orders on"
        cost_log = os.path.join(case.tmp, "ai-cost-log.jsonl")
        prev_log = os.environ.get("UFOS_AI_COST_LOG")
        os.environ["UFOS_AI_COST_LOG"] = cost_log
        case.addCleanup(lambda: os.environ.pop("UFOS_AI_COST_LOG", None) if prev_log is None
                        else os.environ.__setitem__("UFOS_AI_COST_LOG", prev_log))
        svc_mod.set_default(svc_mod.CouncilService(self.world, clock=case.clock, llm=self.llm.for_path("new"),
                                                   sandbox_fn=lambda: False))      # blocker council_unavailable
        case.addCleanup(svc_mod.set_default, None)
        self.switchover = asyncio.run(switchover.run("parity harness", "user", eng0))
        control.set_legacy_engine(None)
        newsroom_db.close()
        assert newsroom_db.init(self.old_db, legacy="")
        cs.set_orders_enabled(True, "user", "parity")
        control._write_autotrade(True, "user")
        # OLD path: the engine on the copy, autopilot on
        fund_store.set_autopilot(True)
        self.restart()
        self.ticks: List[dict] = []

    def restart(self) -> None:
        """New FundEngine + CouncilService instances on the same stores (a backend restart: in-memory change
        detection is rebuilt from the databases on both paths)."""
        self.svc = svc_mod.CouncilService(self.world, clock=self.case.clock, llm=self.llm.for_path("new"),
                                          sandbox_fn=lambda: False, **self.newsroom)
        self.engine = FundEngine(self.world, self.legacy, fund_store, clock=self.case.clock, startup_delay=0.0,
                                 llm=self.llm.for_path("old"), **self.newsroom)

    @property
    def council(self):
        return accounts.get(accounts.COUNCIL)

    def tick(self, t: datetime, touch: bool = True) -> dict:
        """One lockstep tick at ``t``: old full cycle, then the platform housekeeping + one Council cycle.
        ``touch=False``: no client heartbeat this tick (dead-man scenarios)."""
        self.case.set_now(t)
        if touch:
            self.engine.touch_client(t)
            control.touch(t)
        self.llm.path = None
        n_old, n_new = len(self.llm.logs["old"]), len(self.llm.logs["new"])
        self.llm.path = "old"
        old = asyncio.run(self.engine.run_cycle("scheduled"))
        self.llm.path = None
        self.case.rt.cycle(t)
        self.llm.path = "new"
        new = asyncio.run(self.svc.run("scheduled"))
        self.llm.path = None
        rec = {"t": t, "old": old, "new": new, "calls_old": self.llm.logs["old"][n_old:],
               "calls_new": self.llm.logs["new"][n_new:]}
        self.ticks.append(rec)
        return rec

    # --- comparison -------------------------------------------------------------------------------------------
    @staticmethod
    def _close(a, b, tol=FLOAT_TOL) -> bool:
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
            return abs(float(a) - float(b)) <= tol
        return a == b

    # Orders are compared by what EXECUTED: (symbol, side, filled qty). The old engine stores a leg with the qty the
    # broker clipped it to (buying power / short cap) and a refused leg as "rejected" qty 0; the OMS stores the
    # requested qty with filled_qty (the clipped rest cancelled) and a refused leg as "rejected" — same fills.
    def old_orders(self, run_id) -> List[tuple]:
        return sorted((o["symbol"], o["side"], round(float(o["qty"]) if o["status"] == "filled" else 0.0, 9))
                      for o in fund_store.list_orders(limit=1000, run_id=run_id)
                      if o.get("origin") == "autopilot" and o["status"] in ("filled", "pending"))

    def new_orders(self, t: datetime) -> List[tuple]:
        ts = t.astimezone(UTC).isoformat(timespec="seconds")
        return sorted((o["symbol"], o["side"], round(float(o.get("filled_qty") or 0.0), 9))
                      for o in oms.list_orders(accounts.COUNCIL, limit=1000)
                      if o["origin"] == "council" and o["created_at"][:19] == ts[:19] and o["state"] != "rejected")

    def compare_tick(self, rec: dict, off_window: bool = False) -> List[str]:
        out: List[str] = []
        t = rec["t"].isoformat(timespec="seconds")
        oid, nid = rec["old"].get("run_id"), rec["new"].get("run_id")
        if oid is None or nid is None:
            return [f"{t}: run missing (old {rec['old'].get('status')}, new {rec['new'].get('status')})"]
        old_sig = {(s["symbol"], s["agent"]): s for s in fund_store.agent_scores(oid)}
        d = cs.run_detail(nid)
        new_sig = {(s["symbol"], s["agent"]): s for s in d["signals"]}
        for k in sorted(set(old_sig) | set(new_sig)):
            a, b = old_sig.get(k), new_sig.get(k)
            if a is None or b is None:
                out.append(f"{t}: signal {k} only in {'new' if a is None else 'old'}")
                continue
            for f in ("score", "confidence", "source"):
                if not self._close(a[f], b[f]):
                    out.append(f"{t}: signal {k}.{f}: old {a[f]!r} new {b[f]!r}")
            da = {x: y for x, y in (a.get("details") or {}).items() if x not in ("cached_at",)}
            dn = {x: y for x, y in (b.get("details") or {}).items() if x not in ("cached_at",)}
            if json.dumps(da, sort_keys=True, default=str) != json.dumps(dn, sort_keys=True, default=str):
                out.append(f"{t}: signal {k}.details differ: old {da!r} new {dn!r}"[:600])
        old_rec = {r["symbol"]: r for r in fund_store.list_recommendations(500, run_id=oid)}
        new_dec = {x["symbol"]: x for x in d["decisions"] if x["judge_source"] != "unavailable"}
        for s in sorted(set(old_rec) | set(new_dec)):
            a, b = old_rec.get(s), new_dec.get(s)
            if a is None or b is None:
                out.append(f"{t}: decision {s} only in {'new' if a is None else 'old'}")
                continue
            for fo, fn in (("action", "action"), ("confidence", "confidence"), ("target_weight", "target_weight"),
                           ("source", "judge_source")):
                if not self._close(a[fo], b[fn]):
                    out.append(f"{t}: decision {s}.{fo}: old {a[fo]!r} new {b[fn]!r}")
        ex = {x["decision_id"]: x for x in d["executions"]}
        for s in sorted(set(old_rec) & set(new_dec)):
            a, e = old_rec[s], ex.get(new_dec[s]["id"])
            want = self.map_skip(a)
            got = None if e is None else (e["outcome"], e["reason"])
            if want == ("open", None) and got is not None and got[0] == "not_executed":
                continue                 # old: autopilot off → nothing to execute; Council: the gate reason
            if off_window and s in EQUITIES:
                continue                 # D7: off-window equity placement is a deliberate difference
            if not (isinstance(want, set) and got in want or want == got):
                out.append(f"{t}: execution {s}: old {(a['status'], a['skip_reason'])} → {want} new {got}")
        oo, no = self.old_orders(oid), self.new_orders(rec["t"])
        if off_window:                   # D7: only the crypto orders are compared outside the window
            oo, no = [o for o in oo if o[0] in CRYPTO], [o for o in no if o[0] in CRYPTO]
        if oo != no:
            out.append(f"{t}: orders old {oo} new {no}")
        if rec["calls_old"] != rec["calls_new"]:
            out.append(f"{t}: AI call logs differ: old {rec['calls_old']} new {rec['calls_new']}"[:800])
        oc, nc = float(rec["old"].get("cost_usd") or 0.0), float(rec["new"].get("cost_usd") or 0.0)
        if abs(oc - nc) > FLOAT_TOL:
            out.append(f"{t}: AI cost old {oc} new {nc}")
        return out

    @staticmethod
    def map_skip(rec: dict):
        """The old recommendation's (status, skip_reason) in the Council's terms (outcome, reason); None = the old
        engine recorded nothing to execute (autopilot off: status stays "open") ↔ the Council records
        ``not_executed`` with the gate reason (compared by the caller via ``gate_reason``)."""
        st, why = rec["status"], rec["skip_reason"]
        if st == "executed":
            return {("submitted", None)}
        if st == "skipped":
            # the old engine has ONE status "skipped" for every reason; the Council splits it into "skipped" (sizing
            # said no), "rejected" (the broker refused the leg) and "not_executed" (an account-level stop, e.g.
            # valuation paused) — the reason text itself must be identical
            return SKIP_REASON_MAP.get(why, {("skipped", why), ("rejected", why), ("not_executed", why)})
        return ("open", None)

    def compare_books(self) -> List[str]:
        out: List[str] = []
        acc = self.council
        pm = {s: self.world.quote(s) for s in UNIVERSE}
        a, b = self.legacy.account(pm), acc.broker.account(pm)
        for f in ("cash", "equity"):
            if abs(float(getattr(a, f)) - float(getattr(b, f))) > BOOK_TOL:
                out.append(f"book {f}: old {getattr(a, f)!r} new {getattr(b, f)!r}")
        lp = {p["symbol"]: float(p["qty"]) for p in fund_store.get_positions()}
        pp = {p["symbol"]: float(p["qty"]) for p in acc.store.get_positions()}
        if set(lp) != set(pp) or any(abs(lp[s] - pp[s]) > BOOK_TOL for s in lp):
            out.append(f"book positions: old {lp} new {pp}")
        return out


def lockstep_ticks(first_day: date, sessions: int, every_minutes: int = 30) -> List[datetime]:
    """In-session ticks after the opening grace (SP §22.10 lockstep window): open + 5 min grace + 5 s, then every
    ``every_minutes`` while before the close, over ``sessions`` NY sessions from ``first_day``."""
    out: List[datetime] = []
    d = first_day
    n = 0
    while n < sessions:
        b = calendars.session_bounds(d)
        if b:
            t = b[0] + svc_mod.FIRST_CYCLE_OFFSET
            while t < b[1]:
                out.append(t)
                t += timedelta(minutes=every_minutes)
            n += 1
        d = calendars.next_trading_day(d)
    return out


# --- "no wall-clock read" check (SP §22.10) -----------------------------------------------------------------------

WALL_CLOCK_MODULES = (
    "fund.engine", "fund.store", "fund.judge", "fund.sizing", "fund.agents.technical", "fund.agents.risk",
    "fund.agents.sentiment", "strategy_platform.council.service", "strategy_platform.council.judge",
    "strategy_platform.council.sizing", "strategy_platform.council.spend", "strategy_platform.council.store",
    "strategy_platform.council.agents.technical", "strategy_platform.council.agents.risk",
    "strategy_platform.council.agents.sentiment", "strategy_platform.council.indicators",
    "strategy_platform.core.llm", "strategy_platform.core.calendars", "strategy_platform.strategy.features.macro_agent",
    "strategy_platform.execution.paper_broker", "strategy_platform.execution.oms",
    "strategy_platform.execution.accounts", "strategy_platform.execution.control", "strategy_platform.live.runtime",
    "strategy_platform.portfolio.ledger", "strategy_platform.portfolio.risk", "strategy_platform.portfolio.checks",
    "strategy_platform.data.market_feed", "newsroom.ai",      # newsroom.ai only where importable (needs openai)
)
# call sites that may read the wall clock: metadata stamps only, never a decision input
WALL_CLOCK_ALLOWED = {
    "strategy_platform.core.llm:provenance": "the AI provenance stamp generated_at (EU AI Act marking, metadata)",
    "newsroom.ai:provenance": "the same stamp when newsroom.ai is importable (production / Windows venv)",
}


class WallClockSpy:
    """Context manager: replaces ``datetime`` in ``WALL_CLOCK_MODULES`` by a subclass that records each
    ``now()`` / ``utcnow()`` / ``today()`` call site (``module:function``); ``isinstance`` keeps working for plain
    datetimes. ``sites`` minus ``WALL_CLOCK_ALLOWED`` must stay empty during the lockstep ticks."""

    def __init__(self):
        self.sites: Dict[str, int] = {}
        self._saved: List[tuple] = []

    def _spy(self):
        real, sites = datetime, self.sites

        class _Meta(type):
            def __instancecheck__(cls, obj):
                return isinstance(obj, real)

            def __subclasscheck__(cls, sub):
                return issubclass(sub, real)

        def record():
            f = sys._getframe(2)
            key = f"{f.f_globals.get('__name__')}:{f.f_code.co_name}"
            sites[key] = sites.get(key, 0) + 1

        class SpyDateTime(real, metaclass=_Meta):
            @classmethod
            def now(cls, tz=None):
                record()
                return real.now(tz)

            @classmethod
            def utcnow(cls):
                record()
                return real.utcnow()

            @classmethod
            def today(cls):
                record()
                return real.today()
        return SpyDateTime

    def __enter__(self):
        spy = self._spy()
        for name in WALL_CLOCK_MODULES:
            try:
                mod = importlib.import_module(name)
            except ImportError:
                continue                 # newsroom.ai without openai (offline VM): core.llm's own fallback runs
            if getattr(mod, "datetime", None) is datetime:
                self._saved.append((mod, mod.datetime))
                mod.datetime = spy
        return self

    def __exit__(self, *exc):
        for mod, orig in self._saved:
            mod.datetime = orig
        self._saved.clear()
        return False

    def probe(self) -> bool:
        """True when the spy is live: a deliberate ``now()`` through a patched module is recorded (then removed)."""
        before = dict(self.sites)
        importlib.import_module("strategy_platform.core.llm").datetime.now(UTC)
        live = self.sites != before
        self.sites.clear()
        self.sites.update(before)
        return live

    def violations(self) -> Dict[str, int]:
        return {k: v for k, v in self.sites.items() if k not in WALL_CLOCK_ALLOWED}
