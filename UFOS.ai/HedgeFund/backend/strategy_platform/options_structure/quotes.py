"""Inputs of one archived chain slot and their quality (SP §21.12, task §1) — read from ``options_archive.db``, which
OS2 opens READ-ONLY (``open_archive_ro`` → ``mode=ro``); nothing here writes to it.

For an archive run (kind ``intraday`` / ``eod``; ``manual`` runs never count for a slot) and an underlying:

* the contracts of the run's snapshot rows whose OCC root is the underlying (type / strike / expiration parsed from
  the OCC symbol; the contract multiplier from ``os_contracts.size`` as of the slot — missing → the contract cannot
  enter GEX, never a default);
* per quote the quality flags (each excludes the mid): ``missing_quote``, ``after_slot`` (quote stamped after the
  slot), ``stale`` (older than the archive's ``quote_max_age_s`` at the slot — PROVISIONAL), ``crossed`` (ask < bid),
  ``locked`` (ask = bid), ``zero_bid``, ``wide_spread`` ((ask − bid) / mid above ``spread_max_pct`` — PROVISIONAL),
  ``outside_band`` (strike outside the archive's ± band around the snapshot spot), ``expired``;
* open interest point-in-time (SP §21.3, user decision 2026-11-06): per contract the NEWEST ``valid_for`` ≤ the
  previous trading session of the slot date with ``available_ms`` ≤ the slot (and ≥ its T+1 08:00 floor) — in practice
  D−2, because Alpaca publishes the OI of day 1 on day 3; of several rows of that date the latest ``available_ms``,
  ties → the latest ``observed_ms``; the OI date / age is stated in every output. The OI is FRESH only when its
  ``valid_for`` is D−1 … D−``oi_max_age_sessions`` (= 2, the archive's fresh window; user decision 2026-11-06 b);
  older OI is treated like missing OI ("no fresh OI", with the reason). A contract without (fresh) OI contributes 0
  (no OI reported) — Alpaca never reported an explicit 0; the per-contract OI age distribution is shown;
* FRED values (rate, VIX series): the newest observation with ``available_ms`` ≤ the slot;
* the archive's own gap rows of the run (shown in the quality view).

Coverage per expiry = share of its in-band contracts with a usable mid (later: plus an own IV and, for GEX, OI);
below the archive's ``coverage_min_pct`` (PROVISIONAL) the expiry is ``unavailable`` — gaps are never filled.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Tuple

from strategy_platform.core import calendars
from strategy_platform.options_archive.config import hm

NY = calendars.NY
OCC = re.compile(r"([A-Z]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})")
CHAIN_KINDS = ("intraday", "eod")
FLAGS = ("missing_quote", "after_slot", "stale", "crossed", "locked", "zero_bid", "wide_spread", "outside_band",
         "expired")


def open_archive_ro(path: str):
    """The archive DB, read-only (``mode=ro``); None when it does not exist."""
    from strategy_platform.options_archive import db as odb
    return odb.open_ro(path)


def parse_occ(sym: str) -> Optional[Tuple[str, date, str, float]]:
    """OCC symbol → (root, expiration, 'call' | 'put', strike); None when malformed."""
    m = OCC.fullmatch(str(sym or ""))
    if not m:
        return None
    try:
        exp = date(2000 + int(m.group(2)), int(m.group(3)), int(m.group(4)))
    except ValueError:
        return None
    return m.group(1), exp, "call" if m.group(5) == "C" else "put", int(m.group(6)) / 1000.0


def ms_to_ny(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(NY)


def expiry_ms(exp: date) -> int:
    """Expiry instant: 16:00 New York on the expiration date (PM settlement) — an assumption stated in the outputs."""
    return int(datetime.combine(exp, calendars.REGULAR_CLOSE, tzinfo=NY).timestamp() * 1000)


YEAR_MS = 365.0 * 86400 * 1000


@dataclass
class Quote:
    occ: str
    kind: str
    strike: float
    expiration: date
    multiplier: Optional[int]
    bid: Optional[float]
    ask: Optional[float]
    quote_t_ms: Optional[int]
    available_ms: int
    vendor_iv: Optional[float]
    vendor_gamma: Optional[float]
    flags: List[str] = field(default_factory=list)
    oi: Optional[int] = None
    oi_available_ms: Optional[int] = None
    oi_reason: Optional[str] = None
    oi_run_id: Optional[int] = None
    oi_valid_for: Optional[str] = None
    oi_age_sessions: Optional[int] = None       # sessions between the newest OI found and the slot date (also stale)

    @property
    def mid(self) -> Optional[float]:
        if self.flags or self.bid is None or self.ask is None:
            return None
        return 0.5 * (self.bid + self.ask)

    def t_years(self, slot_ms: int) -> float:
        return (expiry_ms(self.expiration) - slot_ms) / YEAR_MS


@dataclass
class Chain:
    run_id: int
    run_kind: str
    run_status: str
    symbol: str
    slot_ms: int
    spot: Optional[float]
    spot_t_ms: Optional[int]
    quotes: List[Quote]
    archive_gaps: List[dict]
    available_ms: int                 # max available_ms of the chain rows used (0 when none)
    oi_expected_valid_for: Optional[str]
    reasons: List[str] = field(default_factory=list)

    @property
    def slot_date(self) -> date:
        return ms_to_ny(self.slot_ms).date()


def chain_runs(conn, after_id: int = 0, limit: int = 1000) -> List[dict]:
    """Archived chain runs (intraday / eod; not failed) with id > after_id, oldest first."""
    rows = conn.execute("SELECT id, kind, slot_ms, slot_label, status, finished_ms FROM os_runs WHERE id > ? AND "
                        "kind IN ('intraday', 'eod') AND status != 'failed' ORDER BY id LIMIT ?",
                        (int(after_id), int(limit))).fetchall()
    return [dict(r) for r in rows]


def run_row(conn, run_id: int) -> Optional[dict]:
    r = conn.execute("SELECT id, kind, slot_ms, slot_label, status, finished_ms FROM os_runs WHERE id = ?",
                     (int(run_id),)).fetchone()
    return dict(r) if r else None


def _multipliers(conn, underlying: str, as_of_ms: int) -> Dict[str, Optional[int]]:
    out: Dict[str, Optional[int]] = {}
    for r in conn.execute("SELECT symbol, size FROM os_contracts WHERE underlying = ? AND first_seen_ms <= ? "
                          "ORDER BY first_seen_ms", (underlying, int(as_of_ms))):
        out[r["symbol"]] = r["size"]
    return out


def open_interest(conn, underlying: str, max_valid_for: str, as_of_ms: int, acfg=None
                  ) -> Dict[str, Tuple[Optional[int], int, int, str]]:
    """occ → (open interest, available_ms, run_id, valid_for): per contract the newest ``valid_for`` ≤ max_valid_for
    with available_ms ≤ as_of (and ≥ that date's floor); within a date the latest available wins, ties → latest
    observed (SP §21.3 / §21.5)."""
    out: Dict[str, Tuple[Optional[int], int, int, str]] = {}
    floors: Dict[str, Optional[int]] = {}
    for r in conn.execute(
            "SELECT o.symbol, o.open_interest, o.available_ms, o.run_id, o.valid_for FROM os_open_interest o WHERE "
            "o.valid_for <= ? AND o.available_ms <= ? AND o.symbol LIKE ? ORDER BY o.valid_for, o.available_ms, "
            "o.observed_ms", (max_valid_for, int(as_of_ms), underlying + "%")):
        vf = r["valid_for"]
        if acfg is not None:
            if vf not in floors:
                try:
                    floors[vf] = oi_floor_ms(date.fromisoformat(vf), acfg)
                except Exception:        # noqa: BLE001 — unparsable / beyond the calendar: never used
                    floors[vf] = None
            if floors[vf] is None or r["available_ms"] < floors[vf]:
                continue             # never before T+1 at the floor (the archive stamps it so; checked again here)
        p = parse_occ(r["symbol"])
        if p and p[0] == underlying:
            out[r["symbol"]] = (r["open_interest"], r["available_ms"], r["run_id"], vf)
    return out


def fred_latest(conn, series_id: str, as_of_ms: int) -> Optional[dict]:
    """Newest observation (by date) with a value and available_ms ≤ as_of (a revision = the later available row)."""
    r = conn.execute("SELECT f.date, f.value, f.available_ms, f.observed_ms, (SELECT u.id FROM os_runs u WHERE "
                     "u.kind = 'fred' AND u.started_ms <= f.observed_ms AND f.observed_ms <= u.finished_ms "
                     "ORDER BY u.id LIMIT 1) AS run_id FROM os_fred f WHERE f.series_id = ? AND f.available_ms <= ? "
                     "AND f.value IS NOT NULL ORDER BY f.date DESC, f.available_ms DESC LIMIT 1",
                     (series_id, int(as_of_ms))).fetchone()
    return dict(r) if r else None


def load_chain(conn, run: dict, symbol: str, acfg, cfg) -> Chain:
    """The chain of one archive run for one underlying with every quality flag set (no value replaced)."""
    slot_ms = int(run["slot_ms"])
    slot_d = ms_to_ny(slot_ms).date()
    mult = _multipliers(conn, symbol, slot_ms)
    rows = conn.execute("SELECT symbol, spot, spot_t_ms, bid, ask, quote_t_ms, iv, gamma, available_ms FROM "
                        "os_chain_snapshots WHERE run_id = ? AND symbol LIKE ? ORDER BY symbol",
                        (int(run["id"]), symbol + "%")).fetchall()
    gaps = [dict(g) for g in conn.execute("SELECT kind, underlying, what, reason FROM os_gaps WHERE run_id = ? AND "
                                          "(underlying = ? OR underlying IS NULL) ORDER BY id",
                                          (int(run["id"]), symbol))]
    quotes: List[Quote] = []
    spot = spot_t = None
    avail = 0
    max_age = int(acfg.quote_max_age_s) * 1000
    band = float(acfg.strike_band_pct) / 100.0
    for r in rows:
        p = parse_occ(r["symbol"])
        if p is None or p[0] != symbol:
            continue
        _root, exp, kind, strike = p
        if spot is None and r["spot"] is not None:
            spot, spot_t = float(r["spot"]), r["spot_t_ms"]
        avail = max(avail, int(r["available_ms"]))
        q = Quote(r["symbol"], kind, strike, exp, mult.get(r["symbol"]), r["bid"], r["ask"], r["quote_t_ms"],
                  int(r["available_ms"]), r["iv"], r["gamma"])
        f = q.flags
        if exp < slot_d or q.t_years(slot_ms) <= 0:
            f.append("expired")
        if q.bid is None or q.ask is None or q.quote_t_ms is None:
            f.append("missing_quote")
        else:
            if q.quote_t_ms > slot_ms:
                f.append("after_slot")
            elif slot_ms - q.quote_t_ms > max_age:
                f.append("stale")
            if q.ask < q.bid:
                f.append("crossed")
            elif q.ask == q.bid:
                f.append("locked")
            if q.bid <= 0:
                f.append("zero_bid")
            m = 0.5 * (q.bid + q.ask)
            if m > 0 and q.ask >= q.bid and (q.ask - q.bid) / m * 100.0 > cfg.spread_max_pct:
                f.append("wide_spread")
        if r["spot"] is not None and abs(strike / float(r["spot"]) - 1.0) > band + 1e-12:
            f.append("outside_band")
        quotes.append(q)
    reasons = []
    if not quotes:
        reasons.append(f"no archived {symbol} chain rows in run {run['id']}")
    if spot is None and quotes:
        reasons.append("no spot price in the snapshot")
    expected = None
    try:
        expected = calendars.previous_trading_day(slot_d).isoformat()
    except Exception as ex:         # noqa: BLE001 — calendar end: said, never guessed
        reasons.append(f"calendar: {str(ex)[:120]}")
    if expected is not None and quotes:
        oi = open_interest(conn, symbol, expected, slot_ms, acfg)
        max_age = int(cfg.oi_max_age_sessions)
        ages: Dict[str, Optional[int]] = {}
        for q in quotes:
            got = oi.get(q.occ)
            if got is not None and got[3] not in ages:
                ages[got[3]] = session_age(got[3], slot_d)
            age = ages.get(got[3]) if got is not None else None
            if got is not None:
                q.oi_age_sessions = age
            if got is None:
                q.oi_reason = f"no open interest of {expected} or earlier available by the slot"
            elif age is None or age > max_age:
                q.oi_reason = (f"no fresh OI: the newest OI available is of {got[3]} ({age} sessions before the slot "
                               f"date > {max_age})")
            elif got[0] is None:
                q.oi_reason = "open interest field missing"
            else:
                q.oi, q.oi_available_ms, q.oi_run_id, q.oi_valid_for = int(got[0]), int(got[1]), int(got[2]), got[3]
            if q.oi is not None and q.multiplier is None:
                q.oi_reason = "no contract record (multiplier) by the slot"
    return Chain(int(run["id"]), run["kind"], run["status"], symbol, slot_ms, spot, spot_t, quotes, gaps, avail,
                 expected, reasons)


def session_age(valid_for: str, slot_d: date) -> Optional[int]:
    """Trading sessions from ``valid_for`` to the slot date (D−1 → 1, D−2 → 2); unparsable / beyond the calendar →
    None (then the OI is not fresh — said, never guessed)."""
    try:
        return len(calendars.trading_days(date.fromisoformat(valid_for), slot_d)) - 1
    except Exception:                # noqa: BLE001
        return None


def oi_summary(chain: Chain, max_age_sessions: Optional[int] = None) -> dict:
    """The OI dates used (valid_for → contracts), the newest date and its age in sessions before the slot date, the
    per-contract OI age distribution (fresh AND rejected stale OI), the contracts without fresh OI, and the share of
    in-filter contracts with (fresh) OI."""
    dates: Dict[str, int] = {}
    age_dist: Dict[str, int] = {}
    no_fresh = 0
    inf = [q for q in chain.quotes if not ({"expired", "outside_band"} & set(q.flags))]
    for q in inf:
        if q.oi is not None and q.oi_reason is None:
            dates[q.oi_valid_for] = dates.get(q.oi_valid_for, 0) + 1
        if q.oi_reason and q.oi_reason.startswith("no fresh OI"):
            no_fresh += 1
        k = "none" if q.oi_age_sessions is None else str(q.oi_age_sessions)
        age_dist[k] = age_dist.get(k, 0) + 1
    newest = max(dates) if dates else None
    age = session_age(newest, chain.slot_date) if newest else None
    n_oi = sum(dates.values())
    win = f"D−1 … D−{max_age_sessions}" if max_age_sessions else "D−1 … D−2"
    return {"rule": f"newest valid_for ≤ the previous session with available_at ≤ the slot (in practice D−2), used only "
                    f"when valid_for is {win} (older = no fresh OI)",
            "max_valid_for": chain.oi_expected_valid_for, "valid_for_counts": dict(sorted(dates.items())),
            "newest_valid_for": newest, "age_sessions": age, "contracts": len(inf), "with_oi": n_oi,
            "age_distribution": dict(sorted(age_dist.items())),
            "no_fresh_oi": no_fresh,
            "coverage_pct": round(100.0 * n_oi / len(inf), 2) if inf else None,
            "text": (f"OI reported for {100.0 * n_oi / len(inf):.1f} % of the contracts (Alpaca never reported an "
                     "explicit 0; a contract without OI contributes 0 (no OI reported))") if inf else None}


def flag_counts(chain: Chain) -> Dict[str, int]:
    out = {k: 0 for k in FLAGS}
    for q in chain.quotes:
        for f in q.flags:
            out[f] += 1
    return out


def oi_floor_ms(valid_for: date, acfg) -> int:
    """The earliest instant OI of ``valid_for`` may be used: the next trading day at the floor (SP §21.3)."""
    return int(datetime.combine(calendars.next_trading_day(valid_for), hm(acfg.oi_floor), tzinfo=NY).timestamp()
               * 1000)
