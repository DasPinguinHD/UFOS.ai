"""The Council's ONE AI spend counter (SP §22.2): hedgefund.db meta ``council.ai_spend`` ``{"day": UTC day, "usd"}``.

Each booked call writes the counter AND its ``cl_ai_calls`` row in ONE transaction: the recording LLM wrapper
stashes every call (``CallLog.stash``), the budget's ``add_fn`` books the oldest unbooked call with its cost
(``CallLog.book``), and ``CallLog.flush`` writes the calls that cost nothing (e.g. no key) at the end of the run.
Fails closed: an unreadable counter = budget exhausted (``core.llm.Budget`` semantics).
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Callable, List, Optional

from strategy_platform.storage import db

from . import store

logger = logging.getLogger(__name__)
FEATURES = {"fund-sentiment": "sentiment", "fund-judge": "judge"}


def _day(now: datetime) -> str:
    return now.astimezone(timezone.utc).date().isoformat()


def _read(conn, now: datetime) -> Optional[float]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (store.META_SPEND,)).fetchone()
    if row is None:
        return 0.0
    try:
        v = json.loads(row["value"])
    except (TypeError, ValueError):
        return None
    if not isinstance(v, dict):
        return None
    if v.get("day") != _day(now):
        return 0.0
    try:
        usd = float(v.get("usd") or 0.0)
    except (TypeError, ValueError):
        return None
    return usd if usd == usd and usd >= 0 else None


def spent_today(now: datetime) -> Optional[float]:
    """USD booked on the UTC day of ``now``; None when unreadable (the budget then fails closed)."""
    try:
        with db._connect() as conn:
            return _read(conn, now)
    except Exception:          # noqa: BLE001
        logger.exception("Council AI spend unreadable")
        return None


def _write(conn, now: datetime, usd: float) -> float:
    cur = _read(conn, now)
    if cur is None:
        raise db.StorageUnavailable("the Council AI spend counter is unreadable")
    total = cur + max(0.0, float(usd or 0.0))
    conn.execute("INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                 "value = excluded.value, updated_at = excluded.updated_at",
                 (store.META_SPEND, json.dumps({"day": _day(now), "usd": total}), db.utc_now_iso()))
    return total


def seed(conn, now: datetime, usd: float) -> float:
    """R2: the counter starts at ``usd`` for today (inside the switch-over transaction)."""
    conn.execute("INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                 "value = excluded.value, updated_at = excluded.updated_at",
                 (store.META_SPEND, json.dumps({"day": _day(now), "usd": max(0.0, float(usd or 0.0))}),
                  db.utc_now_iso()))
    return float(usd or 0.0)


def request_hash(messages, model: str) -> str:
    return hashlib.sha256(json.dumps([messages, model], sort_keys=True, ensure_ascii=False,
                                     default=str).encode("utf-8")).hexdigest()


class CallLog:
    """Per-run log of the Council's LLM calls (recording wrapper + budget booking, see the module docstring)."""

    def __init__(self, run_id: Optional[int], clock: Callable[[], datetime], premium_model: Optional[str] = None):
        self.run_id, self.clock, self.premium_model = run_id, clock, premium_model
        self._pending: List[dict] = []
        self._lock = threading.Lock()
        self.calls: List[dict] = []          # every call of the run (tests / parity call log)

    def wrap(self, inner):
        """An ``llm`` callable for the agents / judge that records every call."""
        async def llm(messages, model, max_tokens, feature, feature_label=None, temperature=0.2):
            from strategy_platform.core.llm import LlmUnavailable
            entry = {"feature": FEATURES.get(feature, feature), "model": model,
                     "request_hash": request_hash(messages, model), "usage": None, "status": "ok",
                     "provenance": None}
            if entry["feature"] == "judge" and self.premium_model and model == self.premium_model:
                entry["feature"] = "judge_premium"
            try:
                parsed, raw, used_model, usage = await inner(messages, model, max_tokens, feature, feature_label,
                                                             temperature=temperature)
            except LlmUnavailable as ex:
                entry.update(status={"no_key": "no_key", "bad_json": "invalid", "budget": "budget"}.get(ex.reason,
                                                                                                         "error"),
                             usage=getattr(ex, "usage", None), model=getattr(ex, "model", None) or model)
                self._stash(entry)
                raise
            entry.update(model=used_model or model, usage=dict(usage or {}))
            from strategy_platform.core import llm as llm_mod
            entry["provenance"] = llm_mod.provenance(feature, used_model or model)
            self._stash(entry)
            return parsed, raw, used_model, usage
        return llm

    def _stash(self, entry: dict) -> None:
        with self._lock:
            self._pending.append(entry)
            self.calls.append(entry)

    def book(self, usd: float) -> Optional[float]:
        """The budget's ``add_fn``: counter + the oldest unbooked call row in ONE transaction."""
        now = self.clock()
        with self._lock:
            entry = self._pending.pop(0) if self._pending else None
        with db._connect() as conn:
            total = _write(conn, now, usd)
            if entry is not None:
                store.add_ai_call(self.run_id, entry["feature"], entry["model"], entry["request_hash"], entry["usage"],
                                  usd, entry["status"], entry["provenance"], now, conn=conn)
        return total

    def flush(self) -> None:
        """Writes the calls that were never booked (they cost nothing)."""
        with self._lock:
            rest, self._pending = self._pending, []
        if not rest:
            return
        now = self.clock()
        with db._connect() as conn:
            for e in rest:
                store.add_ai_call(self.run_id, e["feature"], e["model"], e["request_hash"], e["usage"], 0.0,
                                  e["status"], e["provenance"], now, conn=conn)
