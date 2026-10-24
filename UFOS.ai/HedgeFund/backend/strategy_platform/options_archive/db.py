"""``data/options_archive.db`` (SP §21.5) — written ONLY by the archive process; the backend opens it read-only.

WAL, ``PRAGMA user_version`` + the insert-only table ``os_schema`` (one row per applied migration). Every table is
insert-only (no UPDATE / DELETE triggers); times are integer epoch milliseconds UTC; a NULL value is a recorded gap
(the run's ``os_gaps`` rows say why) — never a default. ``recursive_triggers`` is ON, so even the implicit delete of an
``INSERT OR REPLACE`` aborts. Open interest is keyed by (symbol, valid_for, observed_ms): two observations never
collapse; readers take the row with the latest ``available_ms`` ≤ t, ties → the latest ``observed_ms``.
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Optional

SCHEMA_VERSION = 1
INSERT_ONLY = ("os_schema", "os_configs", "os_runs", "os_contracts", "os_contract_sightings", "os_open_interest",
               "os_chain_snapshots", "os_bars", "os_fred", "os_gaps")

V1 = (
    """CREATE TABLE IF NOT EXISTS os_schema (version INTEGER PRIMARY KEY, applied_ms INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS os_configs (hash TEXT PRIMARY KEY, json TEXT NOT NULL, first_ms INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS os_runs (
           id INTEGER PRIMARY KEY AUTOINCREMENT,
           kind TEXT NOT NULL CHECK (kind IN ('oi', 'eod', 'intraday', 'fred', 'manual')),
           slot_ms INTEGER NOT NULL, slot_label TEXT, started_ms INTEGER NOT NULL, finished_ms INTEGER NOT NULL,
           status TEXT NOT NULL CHECK (status IN ('ok', 'partial', 'failed', 'late')),
           requests INTEGER NOT NULL DEFAULT 0, pages INTEGER NOT NULL DEFAULT 0, retries INTEGER NOT NULL DEFAULT 0,
           error TEXT, source TEXT NOT NULL, feed TEXT, spot_source TEXT, model_version TEXT NOT NULL,
           assumptions_hash TEXT NOT NULL, app_version TEXT NOT NULL,
           skew_ms INTEGER)""",
    """CREATE INDEX IF NOT EXISTS ix_os_runs_slot ON os_runs(slot_ms, kind)""",
    """CREATE TABLE IF NOT EXISTS os_contracts (
           symbol TEXT NOT NULL, underlying TEXT NOT NULL, type TEXT, strike REAL, expiration TEXT, style TEXT,
           size INTEGER, first_seen_ms INTEGER NOT NULL, PRIMARY KEY (symbol, first_seen_ms))""",
    """CREATE TABLE IF NOT EXISTS os_contract_sightings (
           run_id INTEGER NOT NULL, symbol TEXT NOT NULL, seen_ms INTEGER NOT NULL, PRIMARY KEY (run_id, symbol))""",
    """CREATE TABLE IF NOT EXISTS os_open_interest (
           run_id INTEGER NOT NULL, symbol TEXT NOT NULL, open_interest INTEGER, valid_for TEXT NOT NULL,
           available_ms INTEGER NOT NULL, observed_ms INTEGER NOT NULL, early_oi INTEGER NOT NULL DEFAULT 0,
           PRIMARY KEY (symbol, valid_for, observed_ms))""",
    """CREATE TABLE IF NOT EXISTS os_chain_snapshots (
           run_id INTEGER NOT NULL, symbol TEXT NOT NULL, spot REAL, spot_t_ms INTEGER, bid REAL, ask REAL,
           bid_size INTEGER, ask_size INTEGER, quote_t_ms INTEGER, last REAL, last_t_ms INTEGER, daily_volume INTEGER,
           iv REAL, delta REAL, gamma REAL, theta REAL, vega REAL, rho REAL,
           available_ms INTEGER NOT NULL, observed_ms INTEGER NOT NULL, PRIMARY KEY (run_id, symbol))""",
    """CREATE TABLE IF NOT EXISTS os_bars (
           symbol TEXT NOT NULL, timeframe TEXT NOT NULL, t_ms INTEGER NOT NULL, o REAL, h REAL, l REAL, c REAL,
           v INTEGER, n INTEGER, vw REAL, available_ms INTEGER NOT NULL, observed_ms INTEGER NOT NULL,
           backfill INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (symbol, timeframe, t_ms))""",
    """CREATE TABLE IF NOT EXISTS os_fred (
           series_id TEXT NOT NULL, date TEXT NOT NULL, value REAL, available_ms INTEGER NOT NULL,
           observed_ms INTEGER NOT NULL, PRIMARY KEY (series_id, date, available_ms))""",
    """CREATE TABLE IF NOT EXISTS os_gaps (
           id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, slot_ms INTEGER NOT NULL, kind TEXT NOT NULL,
           underlying TEXT, symbol TEXT, what TEXT NOT NULL, reason TEXT, recorded_ms INTEGER NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS ix_os_gaps_slot ON os_gaps(slot_ms)""",
) + tuple(
    stmt for t in INSERT_ONLY for stmt in (
        f"CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t} "
        f"BEGIN SELECT RAISE(ABORT, '{t} is insert-only'); END",
        f"CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t} "
        f"BEGIN SELECT RAISE(ABORT, '{t} is insert-only'); END"))

MIGRATIONS = ((1, V1),)


def now_ms() -> int:
    return int(time.time() * 1000)


def migrate(conn: sqlite3.Connection, clock_ms=now_ms) -> int:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > SCHEMA_VERSION:
        raise sqlite3.DatabaseError(f"options_archive.db schema v{current} is newer than this code (v{SCHEMA_VERSION})")
    for version, statements in MIGRATIONS:
        if version <= current:
            continue
        try:
            conn.execute("BEGIN")
            for sql in statements:
                conn.execute(sql)
            conn.execute("INSERT INTO os_schema (version, applied_ms) VALUES (?, ?)", (version, clock_ms()))
            conn.execute(f"PRAGMA user_version = {int(version)}")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        current = version
    return current


def open_rw(path: str, clock_ms=now_ms) -> sqlite3.Connection:
    """The archive's writable connection (creates + migrates)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA recursive_triggers=ON")     # INSERT OR REPLACE deletes fire the no-delete triggers too
    migrate(conn, clock_ms)
    return conn


def open_ro(path: str) -> Optional[sqlite3.Connection]:
    """A READ-ONLY connection for the backend (None when the file does not exist — "not set up")."""
    if not os.path.exists(path):
        return None
    from urllib.parse import quote
    p = os.path.abspath(path).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p                                  # Windows: file:/C:/… (drive letter after the slash)
    uri = "file:" + quote(p, safe="/:") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn
