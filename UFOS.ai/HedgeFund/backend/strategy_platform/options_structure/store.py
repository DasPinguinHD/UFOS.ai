"""``data/options_structure.db`` — OS2's own store (SP §21.12; decision: a separate DB like the archive's, not
hedgefund.db — OS2 outputs are derived, re-computable data with their own schema version, and keeping them apart
keeps hedgefund.db's backups and migrations untouched). WAL, ``PRAGMA user_version`` + ``os2_schema``; every table is
insert-only (BEFORE UPDATE / DELETE triggers abort; ``recursive_triggers`` ON so INSERT OR REPLACE cannot delete);
times are integer epoch ms UTC. The only writer is the backend's OS2 job; the archive DB is never written.

Tables:
* ``os2_assumptions`` (hash, json, first_ms) — the canonical assumption set behind each ``assumptions_hash``;
* ``os2_results`` — one row per (archive run, symbol, part, model_version, assumptions_hash); part ∈ ``regime`` /
  ``gex`` / ``surface`` / ``quality``; status ``ok`` / ``partial`` / ``unavailable``; ``valid_for_ms`` (the slot),
  ``available_ms`` (max of the inputs' available_at), ``computed_ms``, ``provisional``, ``reasons`` (JSON list),
  ``payload`` (JSON), ``source_runs`` (JSON list of archive run ids).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Iterable, List, Optional

SCHEMA_VERSION = 1
PARTS = ("regime", "gex", "surface", "quality")
TABLES = ("os2_schema", "os2_assumptions", "os2_results")

V1 = (
    "CREATE TABLE IF NOT EXISTS os2_schema (version INTEGER PRIMARY KEY, applied_ms INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS os2_assumptions (hash TEXT PRIMARY KEY, json TEXT NOT NULL, first_ms INTEGER NOT NULL)",
    """CREATE TABLE IF NOT EXISTS os2_results (
           run_id INTEGER NOT NULL, symbol TEXT NOT NULL,
           part TEXT NOT NULL CHECK (part IN ('regime', 'gex', 'surface', 'quality')),
           model_version TEXT NOT NULL, assumptions_hash TEXT NOT NULL,
           status TEXT NOT NULL CHECK (status IN ('ok', 'partial', 'unavailable')),
           valid_for_ms INTEGER NOT NULL, available_ms INTEGER NOT NULL, computed_ms INTEGER NOT NULL,
           provisional INTEGER NOT NULL, reasons TEXT NOT NULL, payload TEXT NOT NULL, source_runs TEXT NOT NULL,
           PRIMARY KEY (run_id, symbol, part, model_version, assumptions_hash))""",
    "CREATE INDEX IF NOT EXISTS ix_os2_results_sym ON os2_results(symbol, part, valid_for_ms)",
) + tuple(
    stmt for t in TABLES for stmt in (
        f"CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t} "
        f"BEGIN SELECT RAISE(ABORT, '{t} is insert-only'); END",
        f"CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t} "
        f"BEGIN SELECT RAISE(ABORT, '{t} is insert-only'); END"))

MIGRATIONS = ((1, V1),)


def now_ms() -> int:
    return int(time.time() * 1000)


def path(data_dir: Optional[str] = None) -> str:
    from .config import DATA_DIR
    return os.path.join(data_dir or DATA_DIR, "options_structure.db")


def migrate(conn: sqlite3.Connection, clock_ms=now_ms) -> int:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > SCHEMA_VERSION:
        raise sqlite3.DatabaseError(f"options_structure.db schema v{current} is newer than this code (v{SCHEMA_VERSION})")
    for version, statements in MIGRATIONS:
        if version <= current:
            continue
        try:
            conn.execute("BEGIN")
            for sql in statements:
                conn.execute(sql)
            conn.execute("INSERT INTO os2_schema (version, applied_ms) VALUES (?, ?)", (version, clock_ms()))
            conn.execute(f"PRAGMA user_version = {int(version)}")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        current = version
    return current


def open_rw(p: str, clock_ms=now_ms) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    conn = sqlite3.connect(p, isolation_level=None, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA recursive_triggers=ON")
    migrate(conn, clock_ms)
    return conn


def open_ro(p: str) -> Optional[sqlite3.Connection]:
    """Read-only (``mode=ro``) — the API's view; None when the file does not exist yet."""
    if not os.path.exists(p):
        return None
    from urllib.parse import quote
    a = os.path.abspath(p).replace("\\", "/")
    if not a.startswith("/"):
        a = "/" + a
    conn = sqlite3.connect("file:" + quote(a, safe="/:") + "?mode=ro", uri=True, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def done(conn, run_id: int, symbol: str, model_version: str, ahash: str) -> bool:
    return conn.execute("SELECT 1 FROM os2_results WHERE run_id = ? AND symbol = ? AND part = 'regime' AND "
                        "model_version = ? AND assumptions_hash = ?", (run_id, symbol, model_version, ahash)
                        ).fetchone() is not None


def compact(x):
    """Floats to 7 significant digits (storage size); structure unchanged."""
    if isinstance(x, float):
        return float(f"{x:.7g}")
    if isinstance(x, dict):
        return {k: compact(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [compact(v) for v in x]
    return x


def write(conn, rows: Iterable[dict], ahash: str, ajson: str, clock_ms=now_ms) -> int:
    """All parts of one (run, symbol) in ONE transaction (a crash leaves none → the job redoes it)."""
    rows = list(rows)
    conn.execute("BEGIN")
    try:
        conn.execute("INSERT OR IGNORE INTO os2_assumptions (hash, json, first_ms) VALUES (?, ?, ?)",
                     (ahash, ajson, clock_ms()))
        for r in rows:
            conn.execute(
                "INSERT INTO os2_results (run_id, symbol, part, model_version, assumptions_hash, status, valid_for_ms,"
                " available_ms, computed_ms, provisional, reasons, payload, source_runs) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (r["run_id"], r["symbol"], r["part"], r["model_version"], ahash, r["status"], r["valid_for_ms"],
                 r["available_ms"], r["computed_ms"], 1 if r["provisional"] else 0,
                 json.dumps(r["reasons"], ensure_ascii=False), json.dumps(compact(r["payload"]), ensure_ascii=False,
                                                                          allow_nan=False, default=str),
                 json.dumps(r["source_runs"])))
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return len(rows)


def row_view(r) -> dict:
    return {"run_id": r["run_id"], "symbol": r["symbol"], "part": r["part"], "model_version": r["model_version"],
            "assumptions_hash": r["assumptions_hash"], "status": r["status"], "valid_for_ms": r["valid_for_ms"],
            "available_ms": r["available_ms"], "computed_ms": r["computed_ms"], "provisional": bool(r["provisional"]),
            "reasons": json.loads(r["reasons"]), "payload": json.loads(r["payload"]),
            "source_runs": json.loads(r["source_runs"])}


def latest(conn, symbol: str, part: str, as_of_ms: int, model_version: str, ahash: Optional[str] = None):
    """Newest row (by valid_for) with available_ms ≤ as_of — the only rows a reader may use (SP §21.3)."""
    sql = ("SELECT * FROM os2_results WHERE symbol = ? AND part = ? AND model_version = ? AND available_ms <= ?")
    args: list = [symbol, part, model_version, int(as_of_ms)]
    if ahash:
        sql += " AND assumptions_hash = ?"
        args.append(ahash)
    r = conn.execute(sql + " ORDER BY valid_for_ms DESC, computed_ms DESC LIMIT 1", args).fetchone()
    return row_view(r) if r else None


def at_slot(conn, symbol: str, part: str, slot_ms: int, model_version: str, ahash: Optional[str] = None):
    sql = "SELECT * FROM os2_results WHERE symbol = ? AND part = ? AND model_version = ? AND valid_for_ms = ?"
    args: list = [symbol, part, model_version, int(slot_ms)]
    if ahash:
        sql += " AND assumptions_hash = ?"
        args.append(ahash)
    r = conn.execute(sql + " ORDER BY computed_ms DESC LIMIT 1", args).fetchone()
    return row_view(r) if r else None


def history(conn, symbol: str, part: str, from_ms: int, to_ms: int, model_version: str,
            ahash: Optional[str] = None) -> List[dict]:
    sql = ("SELECT * FROM os2_results WHERE symbol = ? AND part = ? AND model_version = ? AND valid_for_ms >= ? AND "
           "valid_for_ms <= ? AND available_ms <= ?")
    args: list = [symbol, part, model_version, int(from_ms), int(to_ms), int(to_ms)]
    if ahash:
        sql += " AND assumptions_hash = ?"
        args.append(ahash)
    return [row_view(r) for r in conn.execute(sql + " ORDER BY valid_for_ms", args)]
