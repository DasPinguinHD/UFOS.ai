"""Strategy families, "seen until", the locked holdout window and trials
(SPEC §9.1, §9.2; V1 notes §9.8). Storage: hedgefund.db v6 (``storage/db.py``:
``strategies.family_id``, ``strategy_families``, ``holdout_events``,
``backtest_runs.kind`` / ``trial_number``).

- **Family** = a root strategy and every strategy created from it by
  duplicate or by importing a file exported here (``db.family_parent_of``:
  envelope ``parent`` of another strategy, or a VERIFIED
  ``claimed.exported_from`` — strategy + version exist with that content
  hash). ``family_id`` = the root's id; versions stay in their strategy's family.
- **Holdout window** (global, rolling): ``start`` = the first session on/after
  (last complete session − ``EQUITY_HOLDOUT_MONTHS`` (24) months) for
  universes with equities (also mixed), − ``CRYPTO_HOLDOUT_MONTHS`` (12) for
  crypto-only; ``end`` = the last complete session. Sessions: NYSE for
  ``equity``/``mixed`` (last complete session = NY date of
  ``calendars.last_close_before(now)``), UTC days for ``crypto`` (last
  complete UTC day = today UTC − 1). Month arithmetic clamps the day to the
  month's end (2026-02-28 − 24 months = 2024-02-28; 2026-03-31 − 1 month =
  2026-02-28).
- **Training end** = the session before ``start`` — the default end of
  normal backtests and validation runs; an explicit start/end on or after
  ``start`` -> ``HoldoutWindowViolation`` (an ``InvalidRequest`` -> HTTP 400).
- **seen_until(family)** = max simulated end over all done run cards of the
  family (``runcard.finish`` raises it; a holdout event sets it to the then
  last session). **Contaminated** = seen_until ≥ window start.
- **Eligibility**: unseen window = (max(seen_until, training end), last
  session]. Fresh family (not contaminated, never failed) -> eligible, the full
  window. Contaminated or holdout-failed -> eligible once last session ≥
  anchor + ``REOPEN_UNSEEN_MONTHS`` (12) months (anchor = max(seen_until,
  training end)); the test then uses ONLY the unseen window. ``eligible_from``
  = the first session (UTC day for crypto) on/after anchor + 12 months: the
  family becomes eligible once that session is complete.
- **Logic twins** (review 2026-10-09): a NEW strategy whose logic hash
  (``db.logic_hash_of``: the stored spec without name, description, category,
  tags, provenance and parameter descriptions) equals a version of an existing
  strategy joins that strategy's family (copy-paste, an import with the
  provenance stripped). A strategy EDITED into a twin keeps its family, but
  its eligibility and chips use the merged state of every family owning a
  version with the current version's logic hash (``family_state``: max
  seen_until, newest holdout_failed_at). A spec differing in any value is
  another logic — the ``universe_events`` counter covers near-duplicates.
- **Trials**: every done run card (any kind) of any family member;
  ``trial_number`` on a card = the family's trial count when it finished.

Dates are ``datetime.date`` objects, except in ``holdout_eligibility`` (an
API payload: ISO strings). ``now`` arguments are aware UTC datetimes (None =
the module clock, ``set_clock`` for tests).

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

from ..core import clock as clock_mod
from ..core.errors import InvalidRequest
from ..storage import db

EQUITY_HOLDOUT_MONTHS = 24          # universes with equities (also mixed) — SPEC §9.1
CRYPTO_HOLDOUT_MONTHS = 12          # crypto-only universes
REOPEN_UNSEEN_MONTHS = 12           # contaminated / failed families: unseen months needed
UNIVERSE_KINDS = ("equity", "crypto", "mixed")
RESULTS = ("pass", "fail")
LOCKED_EPISODE_REASON = "not available: inside the locked holdout window"

_clock: clock_mod.Clock = clock_mod.SYSTEM_CLOCK


class HoldoutWindowViolation(InvalidRequest):
    """An explicit start/end of a normal backtest or validation run lies inside the locked
    holdout window (HTTP 400 via ``InvalidRequest``)."""

    def __init__(self, holdout_start: date, what: str = "end"):
        super().__init__(f"This {what} date lies inside the locked holdout window (from "
                         f"{holdout_start.isoformat()}). The holdout is opened once per strategy family via "
                         "'Open holdout'.")
        self.holdout_start = holdout_start
        self.what = what


def set_clock(clock: Optional[clock_mod.Clock]) -> None:
    """Replaces the module clock (tests); None = the system clock."""
    global _clock
    _clock = clock_mod.resolve(clock)


def _now(now: Optional[datetime]) -> datetime:
    return clock_mod.to_utc(now) if now is not None else _clock.now_utc()


def _d(value, name: str = "date") -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            pass
    raise ValueError(f"{name}: not a valid date ({value!r})")


def add_months(d: date, months: int) -> date:
    """``d`` shifted by ``months`` calendar months; the day is clamped to the month's last day."""
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return date(year, month, min(d.day, (nxt - timedelta(days=1)).day))


# --- universe kind and sessions --------------------------------------------------------------------

def universe_kind_of_classes(classes: Iterable[str]) -> str:
    cls = set(classes)
    if not cls:
        raise ValueError("universe: no symbols")
    if cls == {"crypto"}:
        return "crypto"
    if "crypto" in cls:
        return "mixed"
    return "equity"


def _universe(spec: dict):
    from ..core.instruments import instrument
    from ..strategy import schema, universe
    full = schema.with_defaults(spec)
    symbols = universe.resolve(full["universe"])
    return full["universe"], symbols, {s: instrument(s).asset_class for s in symbols}


def universe_kind(spec: dict) -> str:
    """'equity' | 'crypto' | 'mixed' from the asset classes of the spec's universe symbols."""
    return universe_kind_of_classes(_universe(spec)[2].values())


def universe_key(spec: dict) -> str:
    """Identity of a universe for the holdout universe counter: ``preset:<name>:<classes>`` or
    ``symbols:<sha256 of the sorted symbols, 16 hex>``."""
    uni, symbols, classes = _universe(spec)
    if "preset" in uni:
        return f"preset:{uni['preset']}:{'+'.join(sorted(set(classes.values())))}"
    return "symbols:" + hashlib.sha256(",".join(sorted(symbols)).encode("utf-8")).hexdigest()[:16]


def _check_kind(kind: str) -> str:
    if kind not in UNIVERSE_KINDS:
        raise ValueError(f"universe_kind: one of {', '.join(UNIVERSE_KINDS)}")
    return kind


def last_session(kind: str, now: Optional[datetime] = None) -> date:
    """The last COMPLETE session: NYSE (equity/mixed) or UTC day (crypto)."""
    from ..core import calendars
    _check_kind(kind)
    t = _now(now)
    if kind == "crypto":
        return t.astimezone(timezone.utc).date() - timedelta(days=1)
    return calendars.last_close_before(t).astimezone(calendars.NY).date()


def next_session(kind: str, d: date, *, inclusive: bool = False) -> date:
    """The first session after ``d`` (or on it with ``inclusive``)."""
    from ..core import calendars
    _check_kind(kind)
    if kind == "crypto":
        return d if inclusive else d + timedelta(days=1)
    if inclusive and calendars.is_trading_day(d):
        return d
    return calendars.next_trading_day(d)


def previous_session(kind: str, d: date) -> date:
    """The last session before ``d``."""
    from ..core import calendars
    _check_kind(kind)
    return d - timedelta(days=1) if kind == "crypto" else calendars.previous_trading_day(d)


def holdout_window(universe_kind: str, last_session: Optional[date] = None) -> Dict[str, date]:
    """{start, end} (dates) of the locked holdout window; ``last_session`` None = from the clock."""
    kind = _check_kind(universe_kind)
    end = _d(last_session, "last_session") if last_session is not None else globals()["last_session"](kind)
    months = CRYPTO_HOLDOUT_MONTHS if kind == "crypto" else EQUITY_HOLDOUT_MONTHS
    return {"start": next_session(kind, add_months(end, -months), inclusive=True), "end": end}


def training_end(spec: dict, last_session: Optional[date] = None) -> date:
    """holdout start − 1 session for the spec's universe: the default end of normal runs."""
    kind = universe_kind(spec)
    return previous_session(kind, holdout_window(kind, last_session)["start"])


def lock_for_kind(kind: str, now: Optional[datetime] = None) -> dict:
    """{universe_kind, start, end, training_end} (dates) — what the engine applies to a run."""
    w = holdout_window(kind, last_session(kind, now))
    return {"universe_kind": kind, "start": w["start"], "end": w["end"],
            "training_end": previous_session(kind, w["start"])}


def check_request_dates(spec: dict, start=None, end=None, *, now: Optional[datetime] = None) -> Optional[dict]:
    """Normal backtests / validation runs: an explicit ``start`` or ``end`` on/after the holdout start
    -> ``HoldoutWindowViolation``. -> the lock dict, or None when neither date is given."""
    if start is None and end is None:
        return None
    lock = lock_for_kind(universe_kind(spec), now)
    for what, value in (("end", end), ("start", start)):
        if value is not None and _d(value, what) >= lock["start"]:
            raise HoldoutWindowViolation(lock["start"], what)
    return lock


# --- families -------------------------------------------------------------------------------------

def family_of(strategy_id: str) -> str:
    """The family id of a strategy (lifecycle ``NotFound`` when unknown)."""
    from ..lifecycle import registry
    row = db.strategy_get(strategy_id) if isinstance(strategy_id, str) else None
    if row is None:
        raise registry.NotFound(strategy_id)
    if not row.get("family_id"):
        raise db.StorageUnavailable("a strategy has no family (schema v6 data missing)", detail=strategy_id)
    return row["family_id"]


def seen_until(family_id: str) -> Optional[date]:
    row = db.family_get(family_id)
    return date.fromisoformat(row["seen_until"]) if row and row["seen_until"] else None


def holdout_failed_at(family_id: str) -> Optional[str]:
    """UTC ISO time of the family's last failed holdout while the lock stands (a pass clears it)."""
    row = db.family_get(family_id)
    return row["holdout_failed_at"] if row else None


def record_seen(family_id: str, end_date) -> None:
    """Raises the family's seen_until to ``end_date`` (never lowers it)."""
    db.family_record_seen(family_id, _d(end_date, "end_date"))


def family_state(strategy_id: str) -> dict:
    """{family_id, related (family ids sharing the current version's logic hash, incl. its own),
    seen_until (date | None, max over related), holdout_failed_at (newest standing failure over
    related, or None)} — what eligibility, chips and the lifecycle lock use."""
    row = db.strategy_get(strategy_id) if isinstance(strategy_id, str) else None
    if row is None:
        from ..lifecycle import registry
        raise registry.NotFound(strategy_id)
    fid = family_of(strategy_id)
    lh = db.logic_hash_get(strategy_id, row["current_version"])
    if lh is None:
        raise db.StorageUnavailable("a strategy version has no logic hash (schema v7 data missing)",
                                    detail=strategy_id)
    related = [fid] + [f for f in db.families_with_logic_hash(lh) if f != fid]
    seen, failed = None, None
    for f in related:
        r = db.family_get(f) or {}
        if r.get("seen_until"):
            d = date.fromisoformat(r["seen_until"])
            seen = d if seen is None or d > seen else seen
        if r.get("holdout_failed_at") and (failed is None or r["holdout_failed_at"] > failed):
            failed = r["holdout_failed_at"]
    return {"family_id": fid, "related": related, "seen_until": seen, "holdout_failed_at": failed}


def is_contaminated(family_id: str, window: dict) -> bool:
    s = seen_until(family_id)
    return s is not None and s >= _d(window["start"], "window.start")


def _eligibility(kind: str, last: date, seen: Optional[date], failed_at: Optional[str]) -> dict:
    """Pure core of ``holdout_eligibility`` (dates)."""
    window = holdout_window(kind, last)
    te = previous_session(kind, window["start"])
    contaminated = seen is not None and seen >= window["start"]
    failed = bool(failed_at)
    if not contaminated and not failed:
        return {"eligible": True, "reason": None, "mode": "full", "window": window, "unseen_from": window["start"],
                "eligible_from": None, "contaminated": False, "failed": False}
    anchor = max(seen, te) if seen is not None else te
    unseen_from = next_session(kind, anchor)
    threshold = add_months(anchor, REOPEN_UNSEEN_MONTHS)
    eligible_from = next_session(kind, threshold, inclusive=True)
    eligible = last >= threshold
    reason = None
    if not eligible:
        if failed:
            reason = (f"This strategy family failed the holdout on {failed_at[:10]} — it is locked until "
                      f"{eligible_from.isoformat()}, when {REOPEN_UNSEEN_MONTHS} new unseen months exist; then it may "
                      "be re-tested once on exactly those months.")
        else:
            reason = (f"This strategy family has already seen data up to {seen.isoformat()}, inside the holdout "
                      f"window (from {window['start'].isoformat()}) — it becomes eligible on "
                      f"{eligible_from.isoformat()}, when {REOPEN_UNSEEN_MONTHS} unseen months exist; the test then "
                      "uses only those months.")
    return {"eligible": eligible, "reason": reason, "mode": "unseen",
            "window": {"start": unseen_from, "end": last} if eligible else window,
            "unseen_from": unseen_from, "eligible_from": None if eligible else eligible_from,
            "contaminated": contaminated, "failed": failed}


def _current_spec(strategy_id: str):
    from ..lifecycle import registry
    row = registry._require(strategy_id)
    v = db.strategy_version_get(strategy_id, row["current_version"])
    if v is None:
        raise registry.NotFound(f"{strategy_id} v{row['current_version']}")
    return row, registry._loads(v["spec_json"], "spec")


def _overlaps(ev: dict, window: dict) -> bool:
    return ev["window_start"] <= window["end"].isoformat() and ev["window_end"] >= window["start"].isoformat()


def holdout_eligibility(strategy_id: str, now: Optional[datetime] = None) -> dict:
    """GET /v2/strategies/{id}/holdout payload (ISO date strings): {eligible, reason (None when
    eligible), window {start, end} (the window the holdout run must use when eligible: full window
    for a fresh family, the unseen window otherwise; when not eligible the current full window),
    unseen_from, eligible_from (None when eligible), family_events (holdout events of this family),
    universe_events (OTHER families whose holdout events on the same universe overlap the current
    full window), plus mode ('full' | 'unseen'), contaminated, holdout_failed_at, seen_until,
    universe_kind, holdout_start}."""
    row, spec = _current_spec(strategy_id)
    kind = universe_kind(spec)
    st = family_state(strategy_id)
    fid, seen = st["family_id"], st["seen_until"]
    fam = {"holdout_failed_at": st["holdout_failed_at"]}
    last = last_session(kind, now)
    core = _eligibility(kind, last, seen, fam.get("holdout_failed_at"))
    full = holdout_window(kind, last)
    key = universe_key(spec)
    fam_events = [e for f in st["related"] for e in db.holdout_events_list(f)]
    others = {e["family_id"] for e in db.holdout_events_list(universe_key=key)
              if e["family_id"] not in st["related"] and _overlaps(e, full)}
    iso = lambda d: d.isoformat() if d is not None else None        # noqa: E731
    return {"eligible": core["eligible"], "reason": core["reason"],
            "window": {"start": iso(core["window"]["start"]), "end": iso(core["window"]["end"])},
            "unseen_from": iso(core["unseen_from"]), "eligible_from": iso(core["eligible_from"]),
            "family_events": len(fam_events), "universe_events": len(others), "mode": core["mode"],
            "contaminated": core["contaminated"], "holdout_failed_at": fam.get("holdout_failed_at"),
            "seen_until": iso(seen), "universe_kind": kind, "holdout_start": iso(full["start"])}


def record_holdout_event(strategy_id: str, version: int, window: dict, result: str, run_id: str) -> int:
    """Writes the immutable holdout event of an opened holdout and, in the same transaction, sets the
    family's seen_until = max(seen_until, window end = the last session) and holdout_failed_at (ts on
    'fail', cleared on 'pass'). ``window`` = {start, end} (dates or ISO strings); ``result`` 'pass' |
    'fail'. -> event id. The holdout JOB uses ``runcard.finish_holdout`` instead (card + event in ONE
    transaction, review 2026-10-09); this stays for tests / tools."""
    event = holdout_event_row(strategy_id, version, window, result, run_id)
    return db.holdout_event_insert(event, audit=("engine", "holdout_opened",
                                                 {"family_id": event["family_id"], "strategy_id": strategy_id,
                                                  "version": event["version"],
                                                  "window": [event["window_start"].isoformat(),
                                                             event["window_end"].isoformat()],
                                                  "result": result, "run_id": run_id}))


def holdout_event_row(strategy_id: str, version: int, window: dict, result: str, run_id: str) -> dict:
    """The holdout_events row (without id) for ``record_holdout_event`` / ``runcard.finish_holdout``."""
    if result not in RESULTS:
        raise ValueError("result: pass or fail")
    from ..lifecycle import registry
    registry._require(strategy_id)
    v = db.strategy_version_get(strategy_id, int(version)) \
        if not isinstance(version, bool) and isinstance(version, int) else None
    if v is None:
        raise registry.NotFound(f"{strategy_id} v{version}")
    spec = registry._loads(v["spec_json"], "spec")
    fid = family_of(strategy_id)
    ws, we = _d(window["start"], "window.start"), _d(window["end"], "window.end")
    ts = _clock.now_iso()
    return {"ts": ts, "family_id": fid, "strategy_id": strategy_id, "version": int(version),
            "window_start": ws, "window_end": we, "universe_kind": universe_kind(spec),
            "universe_key": universe_key(spec), "result": result, "run_id": run_id}


def trial_count(family_id: str) -> int:
    """Done run cards (backtest, validation, holdout) of all family members."""
    return db.family_trial_count(family_id)


def family_trial_sharpes(family_id: str) -> List[float]:
    """Annualised Sharpe ratios of the family's done run cards (oldest first), read from
    ``metrics_json.performance.sharpe``; cards where it is null/non-finite are left out (so the
    list can be shorter than ``trial_count``). Unreadable metrics -> StorageUnavailable."""
    out: List[float] = []
    for r in db.family_done_runs(family_id):
        if r["metrics_json"] is None:
            continue
        try:
            m = json.loads(r["metrics_json"])
        except ValueError:
            raise db.StorageUnavailable("the stored metrics of a run card are unreadable", detail=r["id"]) from None
        sharpe = ((m or {}).get("performance") or {}).get("sharpe")
        if isinstance(sharpe, (int, float)) and not isinstance(sharpe, bool) and math.isfinite(float(sharpe)):
            out.append(float(sharpe))
    return out


def family_chips(row: dict, spec: Optional[dict] = None, *, now: Optional[datetime] = None) -> dict:
    """Chip data for a strategy row (SPEC §9.7): {family_id, contaminated, holdout_failed_until
    (eligible_from of a failed family, else None), trial_count} — contaminated / failed from the
    merged ``family_state`` (logic twins). ``spec`` = the current version's spec (loaded when None)."""
    if spec is None:
        spec = _current_spec(row["id"])[1]
    st = family_state(row["id"])
    fid, seen = st["family_id"], st["seen_until"]
    kind = universe_kind(spec)
    last = last_session(kind, now)
    core = _eligibility(kind, last, seen, st["holdout_failed_at"])
    until = None
    if core["failed"]:
        anchor = max(seen, previous_session(kind, holdout_window(kind, last)["start"])) if seen is not None \
            else previous_session(kind, holdout_window(kind, last)["start"])
        until = next_session(kind, add_months(anchor, REOPEN_UNSEEN_MONTHS), inclusive=True).isoformat()
    return {"family_id": fid, "contaminated": core["contaminated"], "holdout_failed_until": until,
            "trial_count": trial_count(fid)}
