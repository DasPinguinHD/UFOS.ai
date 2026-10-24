"""Post-trade limits (SPEC §17.6): the daily loss limit and the drawdown kill.

- **Daily loss** (default 2 %): reference = the account's last equity snapshot before
  00:00 New York (equity orders) / 00:00 UTC (crypto orders) — or, for an account opened
  after that midnight, its starting capital. When (equity − reference) / reference ≤
  −limit, new risk-increasing orders of that class are refused until the next NY day /
  UTC day (reducing and protective orders still pass). No reference yet → no block (said
  so in the payload).
- **Drawdown kill** (default 10 % from the stored peak): the account's kill (halt, cancel
  all, close all, auto trading off) — skipped (with a message) while a held crypto symbol
  has no live mark (never liquidate on a stale price, like the old engine).

Part of UFOS.ai, a proof-of-concept portfolio project; paper trading only.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional


def _midnights(now: datetime):
    from ..core import calendars
    ny = now.astimezone(calendars.NY)
    ny_mid = datetime(ny.year, ny.month, ny.day, tzinfo=calendars.NY)
    utc_mid = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    return ny_mid, utc_mid


def _next_midnight(cls: str, now: datetime) -> datetime:
    ny_mid, utc_mid = _midnights(now.astimezone(timezone.utc))
    return (utc_mid if cls == "crypto" else ny_mid) + timedelta(days=1)


def latch_key(cls: str, now: datetime) -> str:
    """meta key of the class day's daily-loss block (review fix M1, 2026-10-18): once the limit was hit, the block
    holds until the next NY day (equity) / UTC day (crypto) even when the price recovers the same day. The date is
    the day the block lifts (= the runtime's key since stage 7: ``day_loss:<cls>:<until[:10]>``)."""
    until = _next_midnight(cls, now).astimezone(timezone.utc).isoformat(timespec="seconds")
    return f"day_loss:{cls}:{until[:10]}"


def latched(account, cls: str, now: datetime) -> Optional[str]:
    """When the class day's block was latched (ISO), else None."""
    return account.store.meta_get(latch_key(cls, now))


def record_block(account, cls: str, v: dict, now: datetime, actor: str = "engine") -> bool:
    """Latches a daily-loss block of ``cls`` (``v`` = that class's day_loss() entry, blocked) once per class day:
    meta key + account event + audit row. True when it was newly latched."""
    key = latch_key(cls, now)
    if account.store.meta_get(key) is not None:
        return False
    from ..storage import db
    account.store.meta_set(key, now.astimezone(timezone.utc).isoformat(timespec="seconds"))
    pnl = v.get("pnl_pct")
    head = f"Day P&L {pnl:.2f} % ≤ −{v['limit_pct']:g} %: " if pnl is not None else ""
    account.store.add_event("halt", f"Daily loss limit reached ({cls})", None,
                            head + f"no new {cls} orders until {v.get('until')}. Paper trading · simulated money.")
    db.add_audit(actor, "post_trade_halt", {"account_id": account.id, "kind": "daily_loss", "class": cls, **v})
    return True


def day_loss(account, now: datetime, equity: Optional[float], limit_pct: float) -> dict:
    """{"equity": {...}, "crypto": {...}} — each {pnl_pct, reference_equity, limit_pct, blocked, until, latched}
    (``latched``: the limit was hit earlier this class day — still blocked although the P&L recovered)."""
    now = now.astimezone(timezone.utc)
    ny_mid, utc_mid = _midnights(now)
    out = {}
    reset_at = account.store.get_reset_at()
    start = account.store.get_starting_capital()
    for cls, mid, nxt in (("equity", ny_mid, ny_mid + timedelta(days=1)),
                          ("crypto", utc_mid, utc_mid + timedelta(days=1))):
        ref_pt = account.store.equity_point_before(mid)
        ref = float(ref_pt["equity"]) if ref_pt else None
        if ref is None and reset_at and start and reset_at >= mid.astimezone(timezone.utc).isoformat(timespec="seconds"):
            ref = float(start)
        pnl = None if (ref is None or equity is None or ref <= 0) else (float(equity) / ref - 1.0) * 100.0
        blocked = pnl is not None and pnl <= -float(limit_pct)
        was = account.store.meta_get(latch_key(cls, now)) is not None
        blocked = blocked or was
        out[cls] = {"pnl_pct": pnl, "reference_equity": ref, "limit_pct": float(limit_pct), "blocked": blocked,
                    "until": nxt.astimezone(timezone.utc).isoformat(timespec="seconds") if blocked else None,
                    "latched": was}
    return out
