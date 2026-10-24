"""``macro.regime_score`` — the rule-based macro agent's score, point in time
(SPEC §7.8).

Mirrors ``features/macro_agent.py`` (formerly fund/agents/macro.py) exactly: for every date the agent's own
``evaluate()`` runs on a macro context (same shape as
``build_macro_context``) built ONLY from FRED observations as first published
(ALFRED initial releases, ``features/fred.py``), and the regime comes from
``newsroom.macro_derived.regime`` on those first releases. One value per
asset class of the agent (``ASSET_CLASSES``); a symbol maps to its class with
``macro_agent.asset_class`` (``key_for``).

Inputs (FRED ids, units as in the newsroom): DGS10, T10Y3M, VIXCLS,
BAMLH0A0HYM2 (daily), SAHMREALTIME (the Sahm rule), FEDFUNDS, PCEPILFE (YoY,
``pc1``), PAYEMS and INDPRO (YoY, ``pc1``; regime growth axis). The real
policy rate = FEDFUNDS − core PCE YoY on the newest common month (as
``newsroom.macro_derived.compute``).

Point in time: an observation's known date is its first ``realtime_start``.
The value for date ``d`` uses observations with ``realtime_start < d`` (known
by the end of the previous day — FRED dates carry no time of day and some
releases, e.g. H.15 yields, come after the US close). The agent's own
staleness rules use ``now = d``. A date before any input is known has no
value (absent, never 0).

Storage: first releases go to hedgefund.db ``ext_observations`` (source
``fred``). A series is fetched again unless its stored coverage (meta
``ext_cover:fred:<series>`` = {from, fetched_at}) starts at or before the
needed start AND was fetched after ``end`` (then every observation first
published by ``end`` is already stored — history, not a fallback).

Not investment advice; historical tendencies only.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Dict, List, Optional

from . import ExtFeature
from . import fred as fred_mod

logger = logging.getLogger(__name__)

NAME = "macro.regime_score"
SOURCE = "fred"
# (stored series id, FRED id, units, frequency, role)
SERIES = (
    ("DGS10", "DGS10", None, "daily", "reading"),
    ("T10Y3M", "T10Y3M", None, "daily", "reading"),
    ("VIXCLS", "VIXCLS", None, "daily", "reading"),
    ("BAMLH0A0HYM2", "BAMLH0A0HYM2", None, "daily", "reading"),
    ("SAHMREALTIME", "SAHMREALTIME", None, "monthly", "sahm"),
    ("FEDFUNDS", "FEDFUNDS", None, "monthly", "reading"),
    ("PCEPILFE:pc1", "PCEPILFE", "pc1", "monthly", "reading"),
    ("PAYEMS:pc1", "PAYEMS", "pc1", "monthly", "aux"),
    ("INDPRO:pc1", "INDPRO", "pc1", "monthly", "aux"),
)
# History needed before ``start``: regime = 48 monthly points (newsroom
# MONTHLY_FETCH_POINTS) incl. 3-month smoothing + 6-month momentum + 12-month trail.
HISTORY_DAYS = 5 * 366
REGIME_POINTS = 48
COVER_META = "ext_cover:fred:"


def _agent():
    from strategy_platform.strategy.features import macro_agent as macro
    return macro


def asset_classes() -> tuple:
    macro = _agent()
    return tuple(dict.fromkeys(list(macro.ASSET_CLASS.values()) + ["crypto"]))


def key_for(symbol: str) -> str:
    return _agent().asset_class(symbol)


def _representatives() -> Dict[str, str]:
    """asset class -> a symbol of that class (evaluate() takes a symbol)."""
    macro = _agent()
    reps: Dict[str, str] = {}
    for sym, cls in macro.ASSET_CLASS.items():
        reps.setdefault(cls, sym)
    reps["crypto"] = "BTC-USD"
    return reps


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


# --- observations (store + fetch) ----------------------------------------------------------

def _storage():
    from ...storage import db
    return db


def series_observations(store_id: str, fred_id: str, units: Optional[str], frequency: str,
                        need_from: date, end: date, *, client=None, now: Optional[datetime] = None) -> List[dict]:
    """First releases of one series with date >= need_from (all known dates;
    the caller filters by realtime_start). Fetches when the stored coverage
    does not prove completeness through ``end``."""
    db = _storage()
    now = now or datetime.now(timezone.utc)
    meta_key = COVER_META + store_id
    raw = db.meta_get(meta_key)
    cover = None
    if raw:
        try:
            cover = json.loads(raw)
        except ValueError:
            cover = None
    complete = (isinstance(cover, dict) and isinstance(cover.get("from"), str)
                and isinstance(cover.get("fetched_at"), str)
                and cover["from"] <= need_from.isoformat()
                and _as_date(cover["fetched_at"]) > end)
    if not complete:
        client = client or fred_mod.default_client()
        rows = client.first_releases(fred_id, units, need_from, now.date(), frequency)
        fetched_at = now.isoformat(timespec="seconds")
        db.upsert_ext_observations([{**r, "series_id": store_id, "source": SOURCE, "fetched_at": fetched_at}
                                    for r in rows])
        start_cover = need_from.isoformat()
        if isinstance(cover, dict) and isinstance(cover.get("from"), str):
            start_cover = min(start_cover, cover["from"])     # first releases never change: coverage adds up
        db.meta_set(meta_key, json.dumps({"from": start_cover, "fetched_at": fetched_at}))
    return db.query_ext_observations(store_id, SOURCE, need_from, None)


# --- point-in-time context ------------------------------------------------------------------

class _Known:
    """Observations of one series known so far: {obs date: value}."""

    def __init__(self):
        self.by_date: Dict[str, float] = {}

    def latest(self):
        if not self.by_date:
            return None
        d = max(self.by_date)
        return d, self.by_date[d]

    def points(self, last: Optional[int] = None) -> List[dict]:
        pts = [{"date": d, "value": self.by_date[d]} for d in sorted(self.by_date)]
        return pts[-last:] if last else pts


def _regime(known: Dict[str, _Known]):
    from newsroom import macro_derived
    full = {"PCEPILFE": known["PCEPILFE:pc1"].points(REGIME_POINTS)}
    aux = {"PAYEMS": known["PAYEMS:pc1"].points(REGIME_POINTS), "INDPRO": known["INDPRO:pc1"].points(REGIME_POINTS)}
    reg = macro_derived.regime(full, aux)
    if not reg:
        return None
    return {"quadrant": reg["quadrant"], "label": reg.get("label"), "date": str(reg.get("date") or "")[:10] or None,
            "borderline": bool(reg.get("borderline")), "months_in_regime": int(reg.get("months_in_regime") or 0),
            "x": reg.get("x"), "y": reg.get("y")}


def _readings(known: Dict[str, _Known]) -> dict:
    readings = {}
    for store_id, _f, _u, frequency, role in SERIES:
        if role not in ("reading", "sahm"):
            continue
        last = known[store_id].latest()
        if last is None:
            continue
        key = "SAHM_RULE" if role == "sahm" else store_id.split(":")[0]
        readings[key] = {"value": last[1], "date": last[0], "monthly": frequency != "daily", "unit": ""}
    ff, pce = known["FEDFUNDS"].by_date, known["PCEPILFE:pc1"].by_date
    common = sorted(set(ff) & set(pce))
    if common:
        d = common[-1]
        readings["REAL_POLICY_RATE"] = {"value": round(ff[d] - pce[d], 4), "date": d, "monthly": True, "unit": "pp"}
    return readings


def compute(observations: Dict[str, List[dict]], start: date, end: date) -> Dict[str, Dict[str, float]]:
    """observations: store id -> [{date, value, realtime_start}] -> {asset class: {date: score}}."""
    macro = _agent()
    reps = _representatives()
    events = sorted((o["realtime_start"], sid, o["date"], float(o["value"]))
                    for sid, rows in observations.items() for o in rows)
    known = {s[0]: _Known() for s in SERIES}
    out: Dict[str, Dict[str, float]] = {cls: {} for cls in reps}
    i = 0
    regime = None
    regime_dirty = True
    d = start
    while d <= end:
        iso = d.isoformat()
        while i < len(events) and events[i][0] < iso:      # known by the end of the previous day
            _rt, sid, obs_date, value = events[i]
            known[sid].by_date[obs_date] = value
            if sid in ("PCEPILFE:pc1", "PAYEMS:pc1", "INDPRO:pc1"):
                regime_dirty = True
            i += 1
        if regime_dirty:
            regime = _regime(known)
            regime_dirty = False
        readings = _readings(known)
        if readings or regime:
            dates = [r["date"] for r in readings.values()] + ([regime["date"]] if regime and regime.get("date") else [])
            ctx = SimpleNamespace(now=f"{iso}T00:00:00+00:00",
                                  macro={"available": True, "as_of": max(dates) if dates else None,
                                         "regime": regime, "readings": readings})
            for cls, sym in reps.items():
                sig = macro.evaluate(ctx, sym)
                out[cls][iso] = float(sig.score)
        d += timedelta(days=1)
    return out


def load(start, end, *, client=None, now: Optional[datetime] = None) -> Dict[str, Dict[str, float]]:
    """{asset class: {date_iso: score}} for every calendar day start..end.
    Raises DataSourceUnavailable / SecretMissing / StorageUnavailable."""
    start, end = _as_date(start), _as_date(end)
    if end < start:
        raise ValueError("end before start")
    need_from = start - timedelta(days=HISTORY_DAYS)
    obs = {}
    for store_id, fred_id, units, frequency, _role in SERIES:
        obs[store_id] = series_observations(store_id, fred_id, units, frequency, need_from, end,
                                            client=client, now=now)
    return compute(obs, start, end)


FEATURE = ExtFeature(
    name=NAME,
    description=("Rule-based macro agent score (−1..+1) per asset class, computed only from FRED data as first "
                 "published (ALFRED): growth × inflation regime tendency plus stress adjustments (10Y yield, "
                 "Sahm rule, 10Y−3M curve, VIX, HY spread, real policy rate). Value for day d uses data published "
                 "before d."),
    load=load, point_in_time=True, scope="asset_class", source=SOURCE, key_for=key_for,
)
