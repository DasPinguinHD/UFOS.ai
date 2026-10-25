"""Storage of the Disclosures section in newsroom.db (INSIDER_RET_API.md §3, CONGRESS_API.md §2).

Tables (newsroom/db.py migration v4): ``disclosure_trades`` (insider + congress rows, core fields only),
``disclosure_returns`` (8 rows per trade: 2 starts x 4 horizons), ``issuer_sic`` (SEC SIC per issuer CIK, refreshed
after 90 days) and ``disclosure_jobs`` (resume state / progress / gaps of the daily job and the backfill).
Retention: 2 years by filing date (``db.prune``).

Unlike the older newsroom helpers this module never swallows errors: the store being off raises
``StoreUnavailable`` and a failing statement raises, so the caller can show it (no silent fallback).

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import db

SIC_MAX_AGE_DAYS = 90
TRADE_COLUMNS = (
    "id", "source", "accession", "form", "accepted_at", "acceptance_state", "filing_date", "issuer_cik", "ticker", "issuer_name",
    "owner_cik", "owner_name", "role_class", "roles_raw", "co_reporters", "code", "shares", "value_usd", "avg_price",
    "trade_date_first", "trade_date_last", "plan_10b5_1", "plan_source", "cluster", "measurable",
    "not_measurable_reason", "official_url", "origin", "first_seen", "updated_at",
    # congress (CONGRESS_API.md §2), null for insider rows
    "chamber", "party", "state", "office", "owner_type", "amount_low", "amount_high", "amount_label", "days_to_file",
    "is_late", "asset_type", "filing_type", "source_ingested_at",
)
RETURN_COLUMNS = (
    "trade_id", "start", "horizon", "state", "sessions_done", "entry_ts", "entry_basis", "entry_price", "etf",
    "etf_entry_price", "exit_date", "exit_price", "etf_exit_price", "r_stock", "r_etf", "excess", "score", "reason",
    "computed_at",
)
REQUIRED_TRADE = ("id", "source", "filing_date", "code", "measurable", "origin", "first_seen", "updated_at")


class StoreUnavailable(RuntimeError):
    """newsroom.db is not open (the Disclosures need it; nothing is kept in memory instead)."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _conn():
    if not db.is_enabled():
        raise StoreUnavailable("newsroom.db is not available — the Disclosures need the local store")
    with db._connect() as conn:              # one connection per call, committed on success
        yield conn


# --- trades -----------------------------------------------------------------------------------------------------

def upsert_trades(rows: Sequence[dict], keep_existing: bool = False) -> int:
    """Insert or update by id; ``first_seen`` (and ``cluster`` once set) of a known row is kept.
    ``keep_existing``: a known row is left untouched (the backfill never overwrites a live row and its
    acceptance time)."""
    rows = list(rows or [])
    for r in rows:
        missing = [k for k in REQUIRED_TRADE if r.get(k) in (None, "")]
        if missing:
            raise ValueError(f"disclosure trade {r.get('id')!r}: missing {', '.join(missing)}")
    if not rows:
        return 0
    cols = ", ".join(TRADE_COLUMNS)
    params = ", ".join(":" + c for c in TRADE_COLUMNS)
    update = ", ".join(f"{c} = excluded.{c}" for c in TRADE_COLUMNS
                       if c not in ("id", "first_seen", "cluster"))
    sql = (f"INSERT INTO disclosure_trades({cols}) VALUES ({params}) "
           + ("ON CONFLICT(id) DO NOTHING" if keep_existing else f"ON CONFLICT(id) DO UPDATE SET {update}"))
    with _conn() as conn:
        conn.executemany(sql, [{c: r.get(c) for c in TRADE_COLUMNS} for r in rows])
    return len(rows)


def get_trade(trade_id: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM disclosure_trades WHERE id = ?", (trade_id,)).fetchone()
    return dict(row) if row else None


def trades(source: str, *, since_date: Optional[str] = None, ids: Optional[Iterable[str]] = None) -> List[dict]:
    sql, args = "SELECT * FROM disclosure_trades WHERE source = ?", [source]
    if since_date:
        sql += " AND filing_date >= ?"
        args.append(since_date)
    if ids is not None:
        ids = list(ids)
        if not ids:
            return []
        sql += f" AND id IN ({', '.join('?' for _ in ids)})"
        args += ids
    with _conn() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY filing_date DESC, id", args).fetchall()]


def query_trades(source: str, where: Sequence[Tuple[str, object]] = (), *, limit: int = 100, offset: int = 0,
                 order: str = "COALESCE(accepted_at, filing_date) DESC, id") -> Tuple[List[dict], int, int]:
    """(rows, total_matching, total_all). ``where``: [(sql fragment with one '?', value)] built by the API layer
    from validated filters (never raw user text in the SQL)."""
    clauses, args = ["source = ?"], [source]
    for frag, value in where:
        clauses.append(frag)
        if isinstance(value, (list, tuple)):
            args.extend(value)
        elif value is not None:
            args.append(value)
    cond = " AND ".join(clauses)
    with _conn() as conn:
        total_all = conn.execute("SELECT COUNT(*) FROM disclosure_trades WHERE source = ?", (source,)).fetchone()[0]
        total = conn.execute(f"SELECT COUNT(*) FROM disclosure_trades WHERE {cond}", args).fetchone()[0]
        rows = conn.execute(f"SELECT * FROM disclosure_trades WHERE {cond} ORDER BY {order} LIMIT ? OFFSET ?",
                            args + [int(limit), int(offset)]).fetchall()
    return [dict(r) for r in rows], total, total_all


def set_clusters(source: str, cluster: Iterable[str]) -> int:
    """cluster = 1 exactly for the given ids of ``source`` (0 for the others). Returns the number of cluster rows."""
    ids = sorted(set(cluster))
    with _conn() as conn:
        conn.execute("UPDATE disclosure_trades SET cluster = 0 WHERE source = ? AND cluster != 0", (source,))
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            conn.execute(f"UPDATE disclosure_trades SET cluster = 1 WHERE source = ? AND id IN "
                         f"({', '.join('?' for _ in chunk)})", [source] + chunk)
    return len(ids)


# --- returns ----------------------------------------------------------------------------------------------------

def replace_returns(trade_id: str, rows: Sequence[dict], computed_at: Optional[str] = None) -> int:
    computed_at = computed_at or utc_now()
    for r in rows:
        if r.get("trade_id") != trade_id:
            raise ValueError(f"return row for {r.get('trade_id')!r} passed with trade {trade_id!r}")
    cols = ", ".join(RETURN_COLUMNS)
    params = ", ".join(":" + c for c in RETURN_COLUMNS)
    with _conn() as conn:
        conn.execute("DELETE FROM disclosure_returns WHERE trade_id = ?", (trade_id,))
        conn.executemany(f"INSERT INTO disclosure_returns({cols}) VALUES ({params})",
                         [{c: (computed_at if c == "computed_at" else r.get(c)) for c in RETURN_COLUMNS}
                          for r in rows])
    return len(rows)


def returns_for(trade_ids: Iterable[str]) -> Dict[str, List[dict]]:
    ids = list(trade_ids)
    out: Dict[str, List[dict]] = {i: [] for i in ids}
    with _conn() as conn:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for r in conn.execute(f"SELECT * FROM disclosure_returns WHERE trade_id IN "
                                  f"({', '.join('?' for _ in chunk)}) ORDER BY start, horizon", chunk):
                out[r["trade_id"]].append(dict(r))
    return out


def returns_rows(source: str, start: str, horizon: int, since_date: Optional[str] = None) -> List[dict]:
    """Return rows of one start/horizon joined with the trade fields the Track record groups by."""
    sql = ("SELECT r.*, t.code, t.role_class, t.plan_10b5_1, t.cluster, t.form, t.chamber, t.party, t.is_late, "
           "t.amount_low FROM disclosure_returns r JOIN disclosure_trades t ON t.id = r.trade_id "
           "WHERE t.source = ? AND r.start = ? AND r.horizon = ?")
    args = [source, start, int(horizon)]
    if since_date:
        sql += " AND t.filing_date >= ?"
        args.append(since_date)
    with _conn() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def trades_to_compute(source: str, since_date: str, *, new_backfill: bool = False, limit: Optional[int] = None,
                      order: str = "t.filing_date, t.id") -> List[dict]:
    """Trades whose returns are missing or still running (complete / not measurable rows are final). A backfilled
    trade still waiting for its acceptance time ('pending') is never computed. ``new_backfill`` False (daily job):
    backfilled trades without any returns are left to the backfill's own price step; True (that step): ONLY those."""
    no_rows = "NOT EXISTS (SELECT 1 FROM disclosure_returns r WHERE r.trade_id = t.id)"
    running = "EXISTS (SELECT 1 FROM disclosure_returns r WHERE r.trade_id = t.id AND r.state = 'running')"
    if new_backfill:
        cond = f"t.origin = 'backfill' AND {no_rows}"
    else:
        cond = f"(({no_rows} AND t.origin != 'backfill') OR {running})"
    sql = (f"SELECT t.* FROM disclosure_trades t WHERE t.source = ? AND t.filing_date >= ? "
           f"AND COALESCE(t.acceptance_state, '') != 'pending' AND {cond} ORDER BY {order}")
    if limit:
        sql += f" LIMIT {int(limit)}"
    with _conn() as conn:
        return [dict(r) for r in conn.execute(sql, (source, since_date)).fetchall()]


def set_acceptance(found: Dict[str, str], missing: Iterable[str] = ()) -> int:
    """Backfill: acceptance times by accession ('found'); accessions not listed by the issuer -> 'missing'."""
    n = 0
    with _conn() as conn:
        for acc, when in found.items():
            n += conn.execute("UPDATE disclosure_trades SET accepted_at = ?, acceptance_state = 'found' "
                              "WHERE source = 'insider' AND accession = ? AND acceptance_state = 'pending'",
                              (when, acc)).rowcount
        for acc in missing:
            conn.execute("UPDATE disclosure_trades SET acceptance_state = 'missing' "
                         "WHERE source = 'insider' AND accession = ? AND acceptance_state = 'pending'", (acc,))
    return n


def pending_acceptance() -> Dict[str, List[str]]:
    """{issuer CIK: [accessions]} of backfilled trades waiting for their acceptance time."""
    out: Dict[str, List[str]] = {}
    with _conn() as conn:
        for r in conn.execute("SELECT DISTINCT issuer_cik, accession FROM disclosure_trades WHERE source = 'insider' "
                              "AND acceptance_state = 'pending' ORDER BY issuer_cik, accession"):
            out.setdefault(r[0] or "", []).append(r[1])
    return out


# --- issuer SIC -------------------------------------------------------------------------------------------------

def sic_get(cik) -> Optional[dict]:
    key = str(cik or "").lstrip("0")
    if not key:
        return None
    with _conn() as conn:
        row = conn.execute("SELECT * FROM issuer_sic WHERE cik = ?", (key,)).fetchone()
    if not row:
        return None
    out = dict(row)
    out["tickers"] = json.loads(out.pop("tickers_json") or "[]")
    return out


def sic_put(cik, sic, sic_description: Optional[str], tickers: Sequence[str], fetched_at: Optional[str] = None):
    key = str(cik or "").lstrip("0")
    if not key:
        raise ValueError("issuer CIK required")
    with _conn() as conn:
        conn.execute("INSERT INTO issuer_sic(cik, sic, sic_description, tickers_json, fetched_at) VALUES (?, ?, ?, ?, ?) "
                     "ON CONFLICT(cik) DO UPDATE SET sic = excluded.sic, sic_description = excluded.sic_description, "
                     "tickers_json = excluded.tickers_json, fetched_at = excluded.fetched_at",
                     (key, None if sic in (None, "") else int(sic), sic_description,
                      json.dumps(list(tickers or [])), fetched_at or utc_now()))


def sic_stale(cik, now: Optional[datetime] = None) -> bool:
    row = sic_get(cik)
    if row is None:
        return True
    try:
        fetched = datetime.fromisoformat(str(row["fetched_at"]).replace("Z", "+00:00"))
    except ValueError:
        return True
    now = now or datetime.now(timezone.utc)
    return (now - fetched).days >= SIC_MAX_AGE_DAYS


# --- jobs -------------------------------------------------------------------------------------------------------

def job_get(key: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM disclosure_jobs WHERE key = ?", (key,)).fetchone()
    if not row:
        return None
    out = dict(row)
    out["progress"] = json.loads(out.pop("progress_json") or "{}")
    return out


def job_put(key: str, state: str, progress: Optional[dict] = None, last_error: Optional[str] = None) -> None:
    with _conn() as conn:
        conn.execute("INSERT INTO disclosure_jobs(key, state, progress_json, last_error, updated_at) "
                     "VALUES (?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET state = excluded.state, "
                     "progress_json = excluded.progress_json, last_error = excluded.last_error, "
                     "updated_at = excluded.updated_at",
                     (key, state, json.dumps(progress or {}, default=str), last_error, utc_now()))


def jobs(prefix: str) -> List[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM disclosure_jobs WHERE key LIKE ? ORDER BY key",
                            (prefix.replace("%", "") + "%",)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["progress"] = json.loads(d.pop("progress_json") or "{}")
        out.append(d)
    return out


def sic_counts(source: str) -> Dict[Optional[int], int]:
    """{sic: number of measurable trades} over the issuers with a loaded SIC (status: unmapped SIC codes)."""
    with _conn() as conn:
        rows = conn.execute("SELECT s.sic, COUNT(*) FROM disclosure_trades t JOIN issuer_sic s ON s.cik = t.issuer_cik "
                            "WHERE t.source = ? AND t.measurable = 1 GROUP BY s.sic", (source,)).fetchall()
    return {r[0]: r[1] for r in rows}


def counts(source: str) -> dict:
    with _conn() as conn:
        trades_n = conn.execute("SELECT COUNT(*) FROM disclosure_trades WHERE source = ?", (source,)).fetchone()[0]
        measurable = conn.execute("SELECT COUNT(*) FROM disclosure_trades WHERE source = ? AND measurable = 1",
                                  (source,)).fetchone()[0]
        complete_20 = conn.execute(
            "SELECT COUNT(*) FROM disclosure_returns r JOIN disclosure_trades t ON t.id = r.trade_id "
            "WHERE t.source = ? AND r.start = 'filing' AND r.horizon = 20 AND r.state = 'complete'",
            (source,)).fetchone()[0]
    return {"trades": trades_n, "measurable": measurable, "complete_20d": complete_20}
