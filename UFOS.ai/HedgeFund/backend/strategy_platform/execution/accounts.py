"""Platform paper accounts (SPEC §17.1): the registry of ``pf_accounts`` rows, each
with its ``PlatformPaperStore``, the REAL ``execution.paper_broker.PaperBroker`` and the
``InternalPaperAdapter``. Market-data callbacks (quote, dividends, daily bars for Rule
201, clock, session) are injected once by main.py (``configure``) — the same Alpaca
feed the old engine uses — and by tests (fakes).

Account ids: ``paper-main`` (opened by the user), ``paper-council`` (only by the
migration of the old paper account). Halts per account live in ``pa_meta`` key
``halt`` (``{active, kind: kill|drawdown, reason, at}``).

Part of UFOS.ai, a proof-of-concept portfolio project; PAPER TRADING ONLY.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from ..storage import db
from .errors import TradingError

logger = logging.getLogger(__name__)

MAIN = "paper-main"
COUNCIL = "paper-council"
INTERNAL_IDS = (MAIN, COUNCIL)
ACTIVE_BROKER_IDS = ("alpaca-paper", "binance-demo")   # stage 8/8b broker accounts (SPEC §18.1, §18.14)
# Retired broker accounts (SPEC §18.13 "Kraken demo retired", user decision 2026-10-19): Kraken decommissioned its
# Futures demo (support.kraken.com/articles/360024809011, 14 July; the host redirects to pro.kraken.com/app). Their
# rows stay as history; they are never connectable, routable or traded and no broker call is ever made for them.
# Stage 10 (S10-K): the Kraken adapter and every kraken-demo branch are gone; only this id stays known, so an old
# kraken-demo row / route in hedgefund.db is shown and answered 409 "retired" (never silently re-routed).
RETIRED = {"kraken-demo": "retired — Kraken decommissioned its Futures demo environment (2026); see Brokers notes"}
RETIRED_SOURCES = ("https://support.kraken.com/articles/360024809011-api-testing-environment-derivatives",
                   "https://autoview.com/guides/kraken-futures-demo/")
BROKER_IDS = ACTIVE_BROKER_IDS + tuple(RETIRED)    # every id a br_accounts row can hold (retired = history, blocked)
ACCOUNT_IDS = INTERNAL_IDS + BROKER_IDS


def retired_reason(account_id) -> Optional[str]:
    return RETIRED.get(account_id)


def sandbox_block_reason(account_id) -> Optional[str]:
    """Sandbox profile (SPEC "Sandbox profile"): every broker account is hard-blocked — no broker request at all;
    only the internal paper broker trades. None in normal mode and for internal accounts."""
    if account_id not in BROKER_IDS:
        return None
    import sandbox_profile
    return sandbox_profile.BROKER_BLOCKED if sandbox_profile.is_sandbox() else None


def no_call_reason(account_id) -> Optional[str]:
    """Why no broker call may be made for ``account_id`` (retired, or blocked in sandbox), else None."""
    return retired_reason(account_id) or sandbox_block_reason(account_id)

LABELS = {MAIN: "Platform paper account", COUNCIL: "Council of AIs (migrated paper account)"}
UNALLOCATED = "_unallocated"
COUNCIL_SOURCE = "council-of-ais"
CAPITAL_MIN, CAPITAL_MAX, CAPITAL_DEFAULT = 1_000.0, 100_000_000.0, 100_000.0
NOT_FOUND = "No such platform paper account."
NOT_OPEN = ("The platform paper account is not open yet — open it in Portfolio & Trading (or migrate the old paper "
            "account).")

_lock = threading.RLock()
_cfg: Dict[str, Optional[Callable]] = {"quote_fn": None, "dividends_fn": None, "bars_fn": None, "clock": None,
                                       "session_fn": None}
_cache: Dict[str, "Account"] = {}


def configure(*, quote_fn: Optional[Callable] = None, dividends_fn: Optional[Callable] = None,
              bars_fn: Optional[Callable] = None, clock: Optional[Callable] = None,
              session_fn: Optional[Callable] = None) -> None:
    """Market-data callbacks for every account's broker (main.py: the AlpacaFeed; tests: fakes)."""
    with _lock:
        _cfg.update(quote_fn=quote_fn, dividends_fn=dividends_fn, bars_fn=bars_fn, clock=clock, session_fn=session_fn)
        _cache.clear()


def reset_cache() -> None:
    with _lock:
        _cache.clear()


def now() -> datetime:
    c = _cfg.get("clock")
    return c() if c else datetime.now(timezone.utc)


def quote(symbol: str):
    fn = _cfg.get("quote_fn")
    return fn(symbol) if fn else None


def quote_for(acc, symbol: str):
    """The quote an ACCOUNT trades and values with: its own (binance-demo: the demo's BASE/USDT price, SPEC §18.14 —
    never the USD quote) when it has one, else the platform's feed."""
    fn = getattr(acc, "own_quote", None)
    return fn(symbol) if callable(fn) else quote(symbol)


class Account:
    def __init__(self, row: dict):
        from strategy_platform.execution.paper_broker import PaperBroker
        from .brokers.paper import InternalPaperAdapter
        from .paper_store import PlatformPaperStore
        self.id = row["id"]
        self.row = row
        self.params = json.loads(row["params_json"])
        self.store = PlatformPaperStore(self.id, self.params)
        kw = {}
        if _cfg.get("session_fn") is not None:
            kw["session_fn"] = _cfg["session_fn"]
        self.broker = PaperBroker(self.store, _cfg.get("quote_fn") or (lambda s: None), clock=_cfg.get("clock"),
                                  dividends_fn=_cfg.get("dividends_fn"), bars_fn=_cfg.get("bars_fn"),
                                  params_fn=lambda: self.params, **kw)
        self.adapter = InternalPaperAdapter(self.broker, self.store)

    @property
    def label(self) -> str:
        return self.row["label"]

    @property
    def origin(self) -> str:
        return self.row["origin"]

    def halt(self) -> dict:
        v = self.store.meta_get("halt")
        try:
            h = json.loads(v) if v else None
        except ValueError:
            h = None
        if not isinstance(h, dict) or not h.get("active"):
            return {"active": False, "kind": None, "reason": None, "at": None}
        return {"active": True, "kind": h.get("kind"), "reason": h.get("reason"), "at": h.get("at")}

    def set_halt(self, active: bool, kind: Optional[str] = None, reason: Optional[str] = None, at=None) -> None:
        self.store.meta_set("halt", json.dumps({"active": bool(active), "kind": kind if active else None,
                                                "reason": reason if active else None,
                                                "at": (at or now().astimezone(timezone.utc).isoformat(
                                                    timespec="seconds")) if active else None}))

    def default_source(self) -> str:
        return COUNCIL_SOURCE if self.id == COUNCIL else UNALLOCATED

    currency = "USD"                     # the account's money (binance-demo: USDT, SPEC §18.14)


def _broker_row(r) -> dict:
    d = dict(r)
    d.update(origin="connected", migration_id=None)
    return d


def _row(account_id: str) -> Optional[dict]:
    with db._connect() as conn:
        if account_id in BROKER_IDS:
            r = conn.execute("SELECT * FROM br_accounts WHERE id = ?", (account_id,)).fetchone()
            return _broker_row(r) if r else None
        r = conn.execute("SELECT * FROM pf_accounts WHERE id = ?", (account_id,)).fetchone()
    return dict(r) if r else None


def connected(account_id: str, conn=None) -> bool:
    """A broker account is connected when its newest br_connections row says so (SPEC §18.1)."""
    def q(c):
        r = c.execute("SELECT status FROM br_connections WHERE account_id = ? ORDER BY id DESC LIMIT 1",
                      (account_id,)).fetchone()
        return r is not None and r["status"] == "connected"
    if conn is not None:
        return q(conn)
    with db._connect() as c:
        return q(c)


def rows(include_disconnected: bool = False) -> List[dict]:
    """Internal accounts + broker accounts (connected only, unless ``include_disconnected``). A RETIRED broker
    account — and in sandbox mode every broker account (SPEC "Sandbox profile") — is never listed as active
    (runtime, poller, kill switch, allocator skip it) — only with ``include_disconnected`` (history)."""
    with db._connect() as conn:
        out = [dict(r) for r in conn.execute("SELECT * FROM pf_accounts ORDER BY created_at, id").fetchall()]
        for r in conn.execute("SELECT * FROM br_accounts ORDER BY created_at, id").fetchall():
            if include_disconnected or (no_call_reason(r["id"]) is None and connected(r["id"], conn)):
                out.append(_broker_row(r))
    return out


def exists(account_id: str) -> bool:
    return _row(account_id) is not None


def is_broker(account_id) -> bool:
    return account_id in BROKER_IDS


def get(account_id: str) -> Account:
    """The account (cached); TradingError 404 when unknown."""
    with _lock:
        acc = _cache.get(account_id)
        if acc is not None:
            return acc
        if not isinstance(account_id, str) or account_id not in ACCOUNT_IDS:
            raise TradingError(404, "not_found", NOT_FOUND)
        row = _row(account_id)
        if row is None:
            if account_id in BROKER_IDS:
                raise TradingError(409, "not_connected", f"The broker account {account_id} is not connected — connect "
                                                         "it in Settings → Brokers.")
            raise TradingError(404 if account_id == COUNCIL else 409, "not_found" if account_id == COUNCIL
                               else "account_not_open", NOT_FOUND if account_id == COUNCIL else NOT_OPEN)
        if account_id in BROKER_IDS:
            from .brokers.mirror import BrokerAccount
            acc = BrokerAccount(row)
        else:
            acc = Account(row)
        _cache[account_id] = acc
        return acc


def all_accounts() -> List[Account]:
    return [get(r["id"]) for r in rows()]


def broker_params(starting_capital: float) -> dict:
    """Broker params defaults (``core.params``, like the backtest, SPEC §8.9) with the starting capital."""
    from strategy_platform.core import params as fund_params
    p = fund_params.defaults()
    p["starting_capital"] = float(starting_capital)
    return p


def insert_account(conn, account_id: str, origin: str, params: dict, actor: str, migration_id: Optional[str] = None,
                   label: Optional[str] = None) -> None:
    conn.execute("INSERT INTO pf_accounts (id, broker, label, origin, params_json, created_at, created_by, migration_id)"
                 " VALUES (?, 'internal_paper', ?, ?, ?, ?, ?, ?)",
                 (account_id, label or LABELS[account_id], origin, json.dumps(params, sort_keys=True),
                  now().astimezone(timezone.utc).isoformat(timespec="seconds"), actor, migration_id))


def open_main(starting_capital=None, label: Optional[str] = None, actor: str = "user") -> Account:
    """Opens ``paper-main`` (stage 7: one opened account) with a deposit of the starting capital."""
    from ..portfolio import settings as pf_settings
    cap = CAPITAL_DEFAULT if starting_capital is None else starting_capital
    if isinstance(cap, bool) or not isinstance(cap, (int, float)) or not CAPITAL_MIN <= float(cap) <= CAPITAL_MAX:
        raise ValueError(f"starting_capital must be between {CAPITAL_MIN:,.0f} and {CAPITAL_MAX:,.0f}")
    with _lock:
        if exists(MAIN):
            raise TradingError(409, "account_exists", "The platform paper account is already open.")
        params = broker_params(float(cap))
        with db._connect() as conn:
            insert_account(conn, MAIN, "opened", params, actor, label=(label or None))
            pf_settings.write(MAIN, pf_settings.defaults(), "system", "account opened (defaults)", conn=conn)
            conn.execute("INSERT INTO audit (ts, actor, action, detail_json) VALUES (?, ?, 'account_opened', ?)",
                         (db.utc_now_iso(), actor, json.dumps({"account_id": MAIN, "starting_capital": float(cap)})))
        acc = get(MAIN)
        with acc.broker._lock:
            with acc.store.atomic():
                acc.broker.ensure_initialized(now(), acc.params)
        from ..portfolio import ledger
        ledger.attribute(acc)           # the deposit -> _unallocated
        return acc
