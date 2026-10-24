"""The New York slot plan of one NYSE session (SP §21.2) — times in America/New_York (zoneinfo, DST-correct), the
NYSE calendar from ``strategy_platform/core/calendars.py`` (holidays, early closes; outside its years →
``CalendarUnavailable``, nothing guessed).

Per trading day: ``oi`` (08:30), ``fred`` (08:45), intraday chain snapshots every ``cadence`` minutes from 09:45 up to
15:45 (always before the close), the fixed slots 09:55 / 15:25, and ``eod`` at close + 15 min. EARLY-CLOSE days
(13:00 close): intraday slots only while the market is open (≤ 12:45), 09:55 kept, **no 15:25 slot** (it is after the
close — H1 and H2 are undefined on such sessions, PENDING the user's confirmation, SP §21.2), EOD at 13:15.
A chain slot's snapshot starts at slot − lead and must FINISH by the slot time.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from strategy_platform.core import calendars

from .config import Config, hm

NY = calendars.NY
CHAIN_KINDS = ("intraday", "eod")


@dataclass(frozen=True)
class Slot:
    kind: str                 # oi | fred | intraday | eod
    ny: datetime              # aware, America/New_York
    label: str                # "09:55", "oi", "eod", "fred"
    fixed: bool = False

    @property
    def utc(self) -> datetime:
        return self.ny.astimezone(timezone.utc)

    @property
    def ms(self) -> int:
        return int(self.utc.timestamp() * 1000)

    def start_utc(self, cfg: Config) -> datetime:
        """When the run starts: chain slots slot − lead (they must finish by the slot), the others at the slot."""
        return self.utc - timedelta(seconds=cfg.slot_lead_seconds) if self.kind in CHAIN_KINDS else self.utc

    def view(self) -> dict:
        return {"kind": self.kind, "label": self.label, "slot_ny": self.ny.isoformat(),
                "slot_utc": self.utc.isoformat().replace("+00:00", "Z")}


def _at(d: date, hhmm: str) -> datetime:
    return datetime.combine(d, hm(hhmm), tzinfo=NY)


def day_slots(d: date, cfg: Config) -> List[Slot]:
    """All slots of NY date ``d`` (empty on a non-trading day), sorted by time."""
    if not calendars.is_trading_day(d):
        return []
    close = (calendars.EARLY_CLOSE if calendars.is_early_close(d) else calendars.REGULAR_CLOSE)
    close_ny = datetime.combine(d, close, tzinfo=NY)
    out = [Slot("oi", _at(d, cfg.oi_slot), "oi"), Slot("fred", _at(d, cfg.fred_slot), "fred")]
    t, last = _at(d, cfg.intraday_first), _at(d, cfg.intraday_last)
    if not cfg.cadence_minutes or cfg.cadence_minutes < 1:      # config.validate refuses it; never loop forever
        raise ValueError("cadence_minutes must be ≥ 1")
    times = set()
    for _ in range(24 * 60 + 1):                                 # bounded: at most one slot per minute of the day
        if t > last:
            break
        if t < close_ny:
            times.add(t)
        t += timedelta(minutes=cfg.cadence_minutes)
    fixed = set()
    for f in cfg.fixed_slots:
        ft = _at(d, f)
        if ft < close_ny:          # an early close drops the 15:25 slot (documented in SP §21.2)
            fixed.add(ft)
    for t in sorted(times | fixed):
        out.append(Slot("intraday", t, t.strftime("%H:%M"), fixed=t in fixed))
    out.append(Slot("eod", close_ny + timedelta(minutes=cfg.eod_after_close_minutes), "eod"))
    return sorted(out, key=lambda s: s.ny)


def find(d: date, label: str, cfg: Config) -> Optional[Slot]:
    return next((s for s in day_slots(d, cfg) if s.label == label), None)


def next_slot(now_utc: datetime, cfg: Config, days_ahead: int = 10) -> Optional[Slot]:
    """The first slot whose START time (slot − lead for chain slots) is ≥ now."""
    d = now_utc.astimezone(NY).date()
    for k in range(days_ahead + 1):
        for s in day_slots(d + timedelta(days=k), cfg):
            if s.start_utc(cfg) >= now_utc:
                return s
    return None
