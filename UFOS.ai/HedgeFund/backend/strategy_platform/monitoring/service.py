"""The monitor task ``sp-monitor`` (SPEC §19.3) and the live dashboard payload (§19.11 overview).

``run_forever``: every 30 s the audit scanner + broker link watcher; every 60 s the decay checks of newly completed
sessions and the report scheduler. A failing step is logged and shown in ``overview.monitor.last_error``; the task
never touches trading (it only reads, stores alerts / checks / reports and — via the decay rule — pauses).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from ..storage import db
from . import alerts, decay, reports

logger = logging.getLogger(__name__)

TICK_SECONDS = 30.0
SLOW_EVERY = 2                 # decay + reports every 2nd tick (60 s)
STALE_SECONDS = 300            # a tick running (or no tick finished) for 5 min → the monitor is shown stale
from ..core import notices as _notices  # noqa: E402 — S10-H context notice (c), = Shared/AiContent.cs
NOTICE = _notices.PAPER_EXECUTION

state = {"running": False, "last_tick_at": None, "last_error": None, "ticks": 0, "tick_started_at": None}


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat(timespec="seconds") if dt else None


def status(now: Optional[datetime] = None) -> dict:
    """{running, last_tick_at, last_error, tick_in_progress, tick_started_at, stale} (review fix 2026-10-18)."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    started = state["tick_started_at"]
    last = datetime.fromisoformat(state["last_tick_at"]) if state["last_tick_at"] else None
    stale = False
    if state["running"]:
        if started is not None and (now - started).total_seconds() > STALE_SECONDS:
            stale = True                                   # a hung tick
        elif last is not None and started is None and (now - last).total_seconds() > STALE_SECONDS:
            stale = True                                   # no tick for too long
    return {"running": state["running"], "last_tick_at": state["last_tick_at"], "last_error": state["last_error"],
            "tick_in_progress": started is not None, "tick_started_at": _iso(started), "stale": stale}


def tick(now: Optional[datetime] = None, slow: bool = True) -> dict:
    from ..execution import accounts
    now = (now or accounts.now()).astimezone(timezone.utc)
    errors = {}
    out = {"alerts": 0, "decay": None, "reports": 0}
    for name, fn in (("audit", lambda: alerts.scan_audit(now)), ("links", lambda: alerts.watch_links(now))):
        try:
            out["alerts"] += fn() or 0
        except Exception as ex:      # noqa: BLE001 — one step failing never stops the others
            logger.exception("monitor step %s failed", name)
            errors[name] = (getattr(ex, "user_message", None) or type(ex).__name__)[:200]
    if slow:
        try:
            out["decay"] = decay.run_checks(now)
            if out["decay"]["errors"]:
                errors["decay"] = "; ".join(f"{k}: {v}" for k, v in out["decay"]["errors"].items())[:300]
        except Exception as ex:      # noqa: BLE001
            logger.exception("monitor decay checks failed")
            errors["decay"] = (getattr(ex, "user_message", None) or type(ex).__name__)[:200]
        try:
            out["reports"] = len(reports.due(now))
        except Exception as ex:      # noqa: BLE001
            logger.exception("monitor report scheduler failed")
            errors["reports"] = (getattr(ex, "user_message", None) or type(ex).__name__)[:200]
    state["last_tick_at"] = now.isoformat(timespec="seconds")
    state["last_error"] = "; ".join(f"{k}: {v}" for k, v in errors.items())[:500] or None
    out["errors"] = errors
    return out


def _timed_tick(slow: bool) -> None:
    state["tick_started_at"] = datetime.now(timezone.utc)
    try:
        tick(None, slow)
    finally:
        state["tick_started_at"] = None


async def run_forever(stop_event: Optional[asyncio.Event] = None) -> None:
    """One tick at a time: a hung tick is never stacked with a second one; ``status()`` shows it stale."""
    state["running"] = True
    task: Optional[asyncio.Future] = None
    try:
        while stop_event is None or not stop_event.is_set():
            if task is None or task.done():
                if task is not None and task.exception() is not None:
                    logger.error("monitor tick failed: %s", task.exception())
                state["ticks"] += 1
                task = asyncio.ensure_future(asyncio.to_thread(_timed_tick, state["ticks"] % SLOW_EVERY == 1))
            elif status()["stale"]:
                logger.warning("monitor tick hung since %s", state["tick_started_at"])
            await asyncio.sleep(TICK_SECONDS)
    finally:
        state["running"] = False


def overview(now: Optional[datetime] = None) -> dict:
    from ..execution import accounts
    from ..live import incubation
    from ..portfolio import views
    now = (now or accounts.now()).astimezone(timezone.utc)
    accs = []
    for acc in accounts.all_accounts():
        try:
            s = views.account_summary(acc, now)
        except Exception as ex:      # noqa: BLE001 — one account failing is shown, the others continue
            accs.append({"account_id": acc.id, "label": acc.label, "broker": getattr(acc, "broker_name",
                                                                                     "internal_paper"),
                         "paper": True, "equity": None, "day_pnl": None, "drawdown_pct": None,
                         "valuation_ok": False, "valuation_message": (getattr(ex, "user_message", None) or
                                                                      type(ex).__name__)[:200],
                         "halt": acc.halt(), "link": None, "reconciliation": None})
            continue
        a = s.get("account") or {}
        link = None
        if getattr(acc, "is_broker", False):
            v = acc.link.view()
            link = {k: v.get(k) for k in ("state", "attempt", "message", "last_ok_at", "next_retry_at")}
        accs.append({"account_id": acc.id, "label": acc.label, "broker": s["broker"], "paper": True,
                     "equity": a.get("equity"), "day_pnl": a.get("day_pnl"), "drawdown_pct": a.get("drawdown_pct"),
                     "valuation_ok": s["valuation"]["ok"], "valuation_message": s["valuation"]["message"],
                     "halt": s["halt"], "link": link, "reconciliation": s.get("reconciliation")})
    strategies = []
    for run in incubation.active_runs():
        try:
            strategies.append(decay.monitor_view(run, now))
        except Exception as ex:      # noqa: BLE001 — shown with its reason, never dropped (review fix 2026-10-18)
            logger.exception("monitor view of %s failed", run.get("id"))
            strategies.append({"strategy_id": run["strategy_id"], "run_id": run["id"], "account_id": run["account_id"],
                               "name": (db.strategy_get(run["strategy_id"]) or {}).get("name"),
                               "error": (getattr(ex, "user_message", None) or f"{type(ex).__name__}: {ex}")[:300]})
    return {"now": now.isoformat(timespec="seconds"), "paper_only": True, "notice": NOTICE,
            "monitor": status(),
            "alerts": alerts.counts(), "accounts": accs, "strategies": strategies}

