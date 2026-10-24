"""Fixtures for the stage-9 tests (SPEC §19): the stage-7 TradingCase (temp hedgefund.db + newsroom.db, fake quotes,
controllable clock) plus helpers to write sub-ledger snapshots (realised returns), FX rates and fake prices.
Offline. Part of UFOS.ai (paper only)."""
import json
import math
from datetime import date, datetime, timedelta, timezone

try:
    from sp_trading_testkit import EXPECTED, FRI, TradingCase, make_strategy
except ImportError:  # pragma: no cover
    from tests.sp_trading_testkit import EXPECTED, FRI, TradingCase, make_strategy

from strategy_platform.core import calendars
from strategy_platform.live import incubation
from strategy_platform.monitoring import alerts, prices
from strategy_platform.storage import db

__all__ = ["EXPECTED", "FRI", "MonCase", "make_strategy", "write_returns", "put_rates", "utc"]


def utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


class MonCase(TradingCase):
    def setUp(self):
        super().setUp()
        self._exp = incubation.expected_for
        incubation.expected_for = lambda sid, v: dict(EXPECTED)
        self.addCleanup(setattr, incubation, "expected_for", self._exp)
        alerts.reset_state()
        self.addCleanup(alerts.reset_state)
        self.addCleanup(prices.set_source, None)

    def start_run(self, length=60, **kw):
        sid = make_strategy(**kw)
        run = incubation.start(sid, length, "monitoring test", now=FRI)["run"]
        return sid, incubation.run_row(run["id"])


def write_returns(run: dict, returns, eq0: float = 40000.0):
    """Daily sub-ledger snapshots of the run's source: the session before start_date = eq0, then one per session."""
    start = date.fromisoformat(run["start_date"])
    days = []
    d = start
    while len(days) < len(returns):
        if calendars.is_trading_day(d):
            days.append(d)
        d += timedelta(days=1)
    eq = eq0
    snaps = [(calendars.previous_trading_day(days[0]), eq)]
    for d, r in zip(days, returns):
        eq *= (1.0 + r)
        snaps.append((d, eq))
    with db._connect() as conn:
        for d, e in snaps:
            conn.execute("INSERT OR REPLACE INTO sl_snapshots VALUES (?, ?, ?, ?, ?, ?, 0, 0)",
                         (run["account_id"], run["source_id"], d.isoformat(),
                          datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc).replace(hour=21).isoformat(),
                          e, e))
    return days


def put_rates(rates: dict):
    """{iso day: USD per EUR} into ext_observations (DEXUSEU, fred)."""
    db.upsert_ext_observations([{"series_id": "DEXUSEU", "date": d, "value": v, "realtime_start": d, "source": "fred",
                                 "fetched_at": "2026-10-18T00:00:00+00:00"} for d, v in rates.items()])


def after_close(d: date) -> datetime:
    """A moment after the decision lag of session ``d`` (21:00 UTC is ≥ close + 20 min in Oct (EDT) and in winter)."""
    return datetime(d.year, d.month, d.day, 21, 30, tzinfo=timezone.utc)


_ = (json, math)
