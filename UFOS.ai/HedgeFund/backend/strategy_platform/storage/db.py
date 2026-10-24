"""hedgefund.db — the Strategy Platform's SQLite store (SPEC §5).

File: ``HedgeFund/backend/data/hedgefund.db`` (git-ignored ``*.db``); tests
pass their own path to ``init(path)``. The paper account of the old engine
stays in newsroom.db; stage 7 (SPEC §17, v15) adds the platform paper accounts
(``pa_*``), the OMS, sub-ledgers, runtime and incubation tables; stage 9 (§19, v17) the
monitoring (``mon_*``) and Shadow Account (``sh_*``) tables.

Rules (differ from newsroom/db.py on purpose — project rule "no unrequested
fallbacks", 2026-10-02):
- "Not found" is ``None`` (or ``[]`` for lists); a FAILURE (file locked,
  unwritable, not opened, SQLite error) raises ``StorageUnavailable``, a
  ``UserFacingError`` the API maps to 503. Nothing is swallowed into a
  default value. Invalid arguments raise ``ValueError``.
- WAL journal, a connection per call, ``PRAGMA user_version`` migrations
  (``MIGRATIONS``: append-only, idempotent statements).
- A corrupt file (not a database / failed quick_check) is renamed to
  ``<name>.corrupt-<UTC timestamp>`` (with -wal/-shm) and recreated; a locked
  or unopenable file is NOT renamed (another process may hold it) —
  ``init`` raises and later calls retry opening at most every
  ``REOPEN_INTERVAL_SECONDS``.
- Times are stored as UTC ISO-8601 strings with seconds
  (``2026-10-02T14:00:00+00:00``), normalised on write so string order is
  time order.

Stdlib only; part of UFOS.ai, a proof-of-concept portfolio project.
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import threading
import time as _time
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Dict, Iterable, List, Optional

from ..core.errors import UserFacingError

logger = logging.getLogger(__name__)

DB_FILENAME = "hedgefund.db"
BUSY_TIMEOUT_SECONDS = 5
REOPEN_INTERVAL_SECONDS = 30


class StorageUnavailable(UserFacingError):
    """hedgefund.db could not be opened, read or written."""

    def __init__(self, reason: str, *, detail: str = ""):
        super().__init__(f"The Strategy Platform database (hedgefund.db) is unavailable: {reason}. "
                         "The affected function is paused — see backend.log.", detail=detail)
        self.reason = reason


class CorruptDatabase(Exception):
    pass


def _begin(conn: sqlite3.Connection) -> None:
    """Opens an explicit transaction so the DDL of a migration version (which
    Python's sqlite3 would otherwise run in autocommit mode) commits or rolls
    back together with its data copy and the user_version bump."""
    if not conn.in_transaction:
        conn.execute("BEGIN")


# --- strategy families (SPEC §9.1; shared by the v6 migration and lifecycle/registry.py) -----------

def family_parent_of(strategy_id: str, envelope: dict, version_hash) -> Optional[str]:
    """The strategy a new strategy was created FROM, per its first version's system envelope, or None:
    - ``envelope.parent.strategy_id`` when it names ANOTHER strategy (duplicate; a parent naming the
      strategy itself is a normal new version);
    - an import whose file carried ``provenance.exported_from = {strategy_id, version, content_hash}``
      (kept verbatim in ``envelope.claimed``): the claim counts ONLY when that strategy version exists
      here with exactly that content hash (``version_hash(strategy_id, version) -> hash | None``) —
      an unverifiable claim never joins a family.
    """
    parent = envelope.get("parent") if isinstance(envelope, dict) else None
    if isinstance(parent, dict) and isinstance(parent.get("strategy_id"), str) \
            and parent["strategy_id"] != strategy_id:
        return parent["strategy_id"]
    if isinstance(envelope, dict) and envelope.get("origin") == "import":
        claimed = envelope.get("claimed")
        ef = claimed.get("exported_from") if isinstance(claimed, dict) else None
        if isinstance(ef, dict) and isinstance(ef.get("strategy_id"), str) and ef["strategy_id"] != strategy_id \
                and isinstance(ef.get("version"), int) and not isinstance(ef.get("version"), bool) \
                and isinstance(ef.get("content_hash"), str):
            if version_hash(ef["strategy_id"], ef["version"]) == ef["content_hash"]:
                return ef["strategy_id"]
    return None


def _v6_families(conn: sqlite3.Connection) -> None:
    """v6 data step: family_id for every strategy (root of its parent chain), one strategy_families row
    per family, seen_until = max range end of the family's done run cards, trial_number of the existing
    done cards = their order (finished_at, id) within the family. Unreadable JSON -> the version fails
    and rolls back (v5 stays intact)."""
    now = utc_now_iso()
    hashes = {(r[0], int(r[1])): r[2] for r in
              conn.execute("SELECT strategy_id, version, content_hash FROM strategy_versions")}
    ids = [r[0] for r in conn.execute("SELECT id FROM strategies ORDER BY created_at, id")]
    parent: dict = {}
    known = set(ids)
    for sid in ids:
        row = conn.execute("SELECT envelope_json FROM strategy_versions WHERE strategy_id = ? "
                           "ORDER BY version LIMIT 1", (sid,)).fetchone()
        env = json.loads(row[0]) if row is not None else {}
        p = family_parent_of(sid, env, lambda s, v: hashes.get((s, int(v))))
        if p is not None and p in known:
            parent[sid] = p
    family: dict = {}
    for sid in ids:
        chain, cur = [], sid
        while cur not in family and cur in parent and cur not in chain:
            chain.append(cur)
            cur = parent[cur]
        root = family.get(cur, cur)
        for c in chain + [sid]:
            family[c] = root
    for sid in ids:
        conn.execute("UPDATE strategies SET family_id = ? WHERE id = ?", (family[sid], sid))
    for fid in sorted(set(family.values())):
        conn.execute("INSERT OR IGNORE INTO strategy_families (family_id, seen_until, holdout_failed_at, "
                     "created_at, updated_at) VALUES (?, NULL, NULL, ?, ?)", (fid, now, now))
    seen: dict = {}
    trials: dict = {}
    for rid, sid, range_json in conn.execute(
            "SELECT id, strategy_id, range_json FROM backtest_runs WHERE status = 'done' "
            "ORDER BY finished_at, id").fetchall():
        fid = family.get(sid)
        if fid is None:
            raise sqlite3.DatabaseError(f"run card {rid}: strategy {sid} does not exist")
        end = date.fromisoformat(str(json.loads(range_json)["end"])[:10]).isoformat()
        if seen.get(fid) is None or end > seen[fid]:
            seen[fid] = end
        trials[fid] = trials.get(fid, 0) + 1
        conn.execute("UPDATE backtest_runs SET trial_number = ? WHERE id = ?", (trials[fid], rid))
    for fid, end in seen.items():
        conn.execute("UPDATE strategy_families SET seen_until = ?, updated_at = ? WHERE family_id = ?",
                     (end, now, fid))


# keys that do not change what a strategy trades (renames/re-descriptions of a copy) — SPEC §9.8
LOGIC_IGNORED_KEYS = ("name", "description", "category", "tags", "provenance")


def logic_hash_of(spec: dict) -> str:
    """sha256 of the canonical JSON of a (default-filled, stored) spec without LOGIC_IGNORED_KEYS and
    without the parameters' descriptions: two specs with the same logic hash trade identically."""
    import hashlib
    if not isinstance(spec, dict):
        raise ValueError("spec: a JSON object")
    logic = {k: v for k, v in spec.items() if k not in LOGIC_IGNORED_KEYS}
    params = logic.get("parameters")
    if isinstance(params, dict):
        logic["parameters"] = {n: ({k: v for k, v in p.items() if k != "description"} if isinstance(p, dict) else p)
                               for n, p in params.items()}
    text = json.dumps(logic, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _v7_logic(conn: sqlite3.Connection) -> None:
    for sid, v, spec_json in conn.execute("SELECT strategy_id, version, spec_json FROM strategy_versions").fetchall():
        conn.execute("INSERT OR IGNORE INTO strategy_logic (strategy_id, version, logic_hash) VALUES (?, ?, ?)",
                     (sid, int(v), logic_hash_of(json.loads(spec_json))))


# version -> statements (SQL strings or callables taking the connection).
# Append new versions; never edit an applied one.
MIGRATIONS = [
    (1, [
        """CREATE TABLE IF NOT EXISTS meta (
               key        TEXT PRIMARY KEY,
               value      TEXT NOT NULL,
               updated_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS audit (
               id          INTEGER PRIMARY KEY AUTOINCREMENT,
               ts          TEXT NOT NULL,          -- UTC ISO-8601
               actor       TEXT NOT NULL,          -- user | engine | system | data
               action      TEXT NOT NULL,
               detail_json TEXT)""",
        "CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit(ts)",
        """CREATE TABLE IF NOT EXISTS data_sources (
               data_type     TEXT PRIMARY KEY,     -- equity_bars, crypto_bars, ..., macro, insider
               source        TEXT NOT NULL,        -- the ONE configured source (alpaca, fred, sec_edgar)
               licence       TEXT,                 -- personal_use | commercial_ok | unknown
               licence_note  TEXT,
               status        TEXT,                 -- NULL (never checked) | ok | error
               last_ok_at    TEXT,
               last_error_at TEXT,
               message       TEXT,                 -- last user-facing status message
               updated_at    TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS bars (
               symbol      TEXT NOT NULL,          -- platform symbol (SPY, BTC-USD)
               timeframe   TEXT NOT NULL,          -- e.g. 1Day, 1Hour, 1Min
               ts          TEXT NOT NULL,          -- bar start, UTC ISO-8601
               open        REAL NOT NULL,
               high        REAL NOT NULL,
               low         REAL NOT NULL,
               close       REAL NOT NULL,
               volume      REAL,
               vwap        REAL,
               trade_count INTEGER,
               source      TEXT NOT NULL,
               known_at    TEXT NOT NULL,          -- point-in-time: when the bar was knowable
               fetched_at  TEXT NOT NULL,
               PRIMARY KEY (symbol, timeframe, ts, source)) WITHOUT ROWID""",
    ]),
    # v2 (stage 2, SPEC §7.10 / §7.8): strategy lifecycle + external feature observations.
    (2, [
        """CREATE TABLE IF NOT EXISTS strategies (
               id               TEXT PRIMARY KEY,      -- "s-" + 12 hex chars
               name             TEXT NOT NULL,
               origin           TEXT NOT NULL,         -- library | user | import | duplicate
               category         TEXT NOT NULL,
               state            TEXT NOT NULL,         -- idea draft backtested candidate validated incubation live scaled retired
               current_version  INTEGER NOT NULL,
               created_at       TEXT NOT NULL,
               updated_at       TEXT NOT NULL,
               state_changed_at TEXT NOT NULL,
               library_key      TEXT UNIQUE)""",
        "CREATE INDEX IF NOT EXISTS ix_strategies_state ON strategies(state)",
        # insert-only: there is no UPDATE/DELETE code path for versions
        """CREATE TABLE IF NOT EXISTS strategy_versions (
               strategy_id     TEXT NOT NULL,
               version         INTEGER NOT NULL,
               spec_json       TEXT NOT NULL,          -- canonical JSON of the stored spec
               content_hash    TEXT NOT NULL,
               envelope_json   TEXT NOT NULL,          -- system-owned provenance (SPEC §7.6)
               validation_json TEXT NOT NULL,
               created_at      TEXT NOT NULL,
               PRIMARY KEY (strategy_id, version))""",
        """CREATE TRIGGER IF NOT EXISTS strategy_versions_no_update BEFORE UPDATE ON strategy_versions
               BEGIN SELECT RAISE(ABORT, 'strategy_versions is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS strategy_versions_no_delete BEFORE DELETE ON strategy_versions
               BEGIN SELECT RAISE(ABORT, 'strategy_versions is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS strategy_transitions (
               id            INTEGER PRIMARY KEY AUTOINCREMENT,
               ts            TEXT NOT NULL,
               strategy_id   TEXT NOT NULL,
               version       INTEGER NOT NULL,
               from_state    TEXT,                   -- NULL for the creation row
               to_state      TEXT NOT NULL,
               actor         TEXT NOT NULL,          -- user | system
               reason        TEXT NOT NULL,
               evidence_json TEXT)""",
        "CREATE INDEX IF NOT EXISTS ix_transitions_strategy ON strategy_transitions(strategy_id, id)",
        """CREATE TABLE IF NOT EXISTS ext_observations (
               series_id      TEXT NOT NULL,         -- e.g. DGS10, PAYEMS:pc1, release:10
               date           TEXT NOT NULL,         -- observation (reference) date YYYY-MM-DD
               value          REAL NOT NULL,
               realtime_start TEXT NOT NULL,         -- first publication date (ALFRED) = known date
               source         TEXT NOT NULL,         -- fred
               fetched_at     TEXT NOT NULL,
               PRIMARY KEY (series_id, date, source)) WITHOUT ROWID""",
    ]),
    # v3 (stage 3, SPEC §8.1/§8.2): two price series per bar ("all" = splits + dividends,
    # "split" = splits only) -> bars.adjustment joins the primary key. SQLite cannot change a
    # primary key in place, so the table is rebuilt; existing rows become "all" (what the
    # stage-1 cache stored). Everything of this version runs in ONE transaction (migrate()
    # commits after the version), so a failure leaves v2 and its data untouched.
    (3, [
        _begin,
        """CREATE TABLE bars_v3 (
               symbol      TEXT NOT NULL,
               timeframe   TEXT NOT NULL,
               ts          TEXT NOT NULL,          -- bar start, UTC ISO-8601
               open        REAL NOT NULL,
               high        REAL NOT NULL,
               low         REAL NOT NULL,
               close       REAL NOT NULL,
               volume      REAL,
               vwap        REAL,
               trade_count INTEGER,
               source      TEXT NOT NULL,
               known_at    TEXT NOT NULL,
               fetched_at  TEXT NOT NULL,
               adjustment  TEXT NOT NULL DEFAULT 'all',   -- all (splits + dividends) | split (splits only)
               PRIMARY KEY (symbol, timeframe, ts, source, adjustment)) WITHOUT ROWID""",
        """INSERT INTO bars_v3 (symbol, timeframe, ts, open, high, low, close, volume, vwap, trade_count,
                                source, known_at, fetched_at, adjustment)
               SELECT symbol, timeframe, ts, open, high, low, close, volume, vwap, trade_count,
                      source, known_at, fetched_at, 'all' FROM bars""",
        "DROP TABLE bars",
        "ALTER TABLE bars_v3 RENAME TO bars",
        """CREATE TABLE IF NOT EXISTS corporate_actions (
               symbol      TEXT NOT NULL,
               kind        TEXT NOT NULL,          -- cash_dividend | forward_split | reverse_split
               ex_date     TEXT NOT NULL,          -- YYYY-MM-DD
               cash_amount REAL,                   -- dividends: cash per share as reported (raw)
               split_ratio REAL,                   -- splits: new shares per old share (2.0 = 2-for-1)
               pay_date    TEXT,                   -- YYYY-MM-DD when the source reports it
               source      TEXT NOT NULL,          -- alpaca (>= seam) | tiingo (< seam)
               known_at    TEXT NOT NULL,
               fetched_at  TEXT NOT NULL,
               PRIMARY KEY (symbol, kind, ex_date, source)) WITHOUT ROWID""",
        """CREATE TABLE IF NOT EXISTS seam_checks (
               symbol      TEXT NOT NULL,
               adjustment  TEXT NOT NULL,          -- all | split
               checked_at  TEXT NOT NULL,
               ok          INTEGER NOT NULL,       -- 1 pass, 0 fail
               factor      REAL,                   -- NULL when the check failed before a factor existed
               stats_json  TEXT NOT NULL,
               PRIMARY KEY (symbol, adjustment, checked_at)) WITHOUT ROWID""",
    ]),
    # v4 (stage 3, SPEC §8.8): consolidated daily volume (data type equity_volume, ONE source:
    # tiingo) for ALL years — its own table, never mixed into the price bars of another source.
    (4, [
        """CREATE TABLE IF NOT EXISTS volumes (
               symbol       TEXT NOT NULL,
               date         TEXT NOT NULL,         -- NY trading date YYYY-MM-DD
               volume       REAL NOT NULL,         -- shares, split basis of the fetch (see seam.volume)
               close_split  REAL NOT NULL,         -- split-adjusted close of the same response/basis
               source       TEXT NOT NULL,         -- tiingo
               known_at     TEXT NOT NULL,
               fetched_at   TEXT NOT NULL,
               PRIMARY KEY (symbol, date, source)) WITHOUT ROWID""",
    ]),
    # v5 (stage 3d, SPEC §8.4 / §8.10): backtest run cards. Insert-only once finished: the triggers
    # refuse DELETE, any UPDATE of a finished card, any UPDATE that is not one of the status
    # transitions queued->running|failed|cancelled, running->done|failed|cancelled, and any change of
    # the identity columns (what was asked for and on which spec).
    (5, [
        """CREATE TABLE IF NOT EXISTS backtest_runs (
               id              TEXT PRIMARY KEY,      -- "bt-" + 12 hex chars
               created_at      TEXT NOT NULL,
               updated_at      TEXT NOT NULL,
               started_at      TEXT,
               finished_at     TEXT,
               strategy_id     TEXT NOT NULL,
               version         INTEGER NOT NULL,
               content_hash    TEXT NOT NULL,
               status          TEXT NOT NULL CHECK (status IN ('queued','running','done','failed','cancelled')),
               spec_json       TEXT NOT NULL,         -- snapshot of the stored spec of that version
               request_json    TEXT NOT NULL,         -- {start, end, benchmark, capital, stress}
               seed            INTEGER,               -- Monte-Carlo seed (stress), NULL without stress
               trial_number    INTEGER,               -- NULL until stage 6
               engine_version  TEXT,
               range_json      TEXT,
               data_json       TEXT,                  -- per symbol: sources, ranges, seam, row hashes
               licences_json   TEXT,
               cost_model_json TEXT,
               metrics_json    TEXT,
               curve_json      TEXT,                  -- daily: equity, benchmark, gross/net exposure
               trades_json     TEXT,
               warnings_json   TEXT,
               stress_json     TEXT,
               error           TEXT)""",
        "CREATE INDEX IF NOT EXISTS ix_backtest_runs_strategy ON backtest_runs(strategy_id, version, status)",
        "CREATE INDEX IF NOT EXISTS ix_backtest_runs_created ON backtest_runs(created_at)",
        """CREATE TRIGGER IF NOT EXISTS backtest_runs_no_delete BEFORE DELETE ON backtest_runs
               BEGIN SELECT RAISE(ABORT, 'backtest_runs is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS backtest_runs_finished_immutable BEFORE UPDATE ON backtest_runs
               WHEN OLD.status NOT IN ('queued', 'running')
               BEGIN SELECT RAISE(ABORT, 'backtest_runs: a finished run card is immutable'); END""",
        """CREATE TRIGGER IF NOT EXISTS backtest_runs_status_transition BEFORE UPDATE ON backtest_runs
               WHEN OLD.status IN ('queued', 'running')
                AND NOT ((OLD.status = 'queued' AND NEW.status IN ('running', 'failed', 'cancelled'))
                      OR (OLD.status = 'running' AND NEW.status IN ('done', 'failed', 'cancelled')))
               BEGIN SELECT RAISE(ABORT, 'backtest_runs: only status transitions are allowed'); END""",
        """CREATE TRIGGER IF NOT EXISTS backtest_runs_identity_fixed BEFORE UPDATE ON backtest_runs
               WHEN NEW.id IS NOT OLD.id OR NEW.created_at IS NOT OLD.created_at
                 OR NEW.strategy_id IS NOT OLD.strategy_id OR NEW.version IS NOT OLD.version
                 OR NEW.content_hash IS NOT OLD.content_hash OR NEW.spec_json IS NOT OLD.spec_json
                 OR NEW.request_json IS NOT OLD.request_json OR NEW.seed IS NOT OLD.seed
                 OR NEW.trial_number IS NOT OLD.trial_number
               BEGIN SELECT RAISE(ABORT, 'backtest_runs: identity columns are fixed'); END""",
    ]),
    # v6 (stage 4, SPEC §9.1 / §9.2, V1 notes §9.8): strategy families, "seen until", holdout events,
    # run kinds, trial numbers, validation exceptions. ONE transaction (like v3).
    (6, [
        _begin,
        "ALTER TABLE strategies ADD COLUMN family_id TEXT",      # filled below; set by strategy_create later
        "CREATE INDEX IF NOT EXISTS ix_strategies_family ON strategies(family_id)",
        """CREATE TABLE IF NOT EXISTS strategy_families (
               family_id         TEXT PRIMARY KEY,     -- = id of the family's root strategy
               seen_until        TEXT,                 -- YYYY-MM-DD: max simulated end over all family run cards
               holdout_failed_at TEXT,                 -- UTC ISO of the last failed holdout (cleared by a pass)
               created_at        TEXT NOT NULL,
               updated_at        TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS holdout_events (
               id            INTEGER PRIMARY KEY AUTOINCREMENT,
               ts            TEXT NOT NULL,
               family_id     TEXT NOT NULL,
               strategy_id   TEXT NOT NULL,
               version       INTEGER NOT NULL,
               window_start  TEXT NOT NULL,          -- YYYY-MM-DD (first session of the tested window)
               window_end    TEXT NOT NULL,          -- YYYY-MM-DD (last session = the then last session)
               universe_kind TEXT NOT NULL,          -- equity | crypto | mixed
               universe_key  TEXT NOT NULL,          -- preset:<name>:<classes> | symbols:<sha256[:16]>
               result        TEXT NOT NULL CHECK (result IN ('pass', 'fail')),
               run_id        TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS ix_holdout_events_family ON holdout_events(family_id, id)",
        "CREATE INDEX IF NOT EXISTS ix_holdout_events_universe ON holdout_events(universe_key, window_end)",
        """CREATE TRIGGER IF NOT EXISTS holdout_events_no_update BEFORE UPDATE ON holdout_events
               BEGIN SELECT RAISE(ABORT, 'holdout_events is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS holdout_events_no_delete BEFORE DELETE ON holdout_events
               BEGIN SELECT RAISE(ABORT, 'holdout_events is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS validation_exceptions (
               id      INTEGER PRIMARY KEY AUTOINCREMENT,
               ts      TEXT NOT NULL,
               run_id  TEXT NOT NULL,                -- a done validation run card
               gate    TEXT NOT NULL CHECK (gate IN ('G1','G2','G3','G4','G5','G6','G7','G8')),
               reason  TEXT NOT NULL,
               actor   TEXT NOT NULL,
               UNIQUE (run_id, gate))""",
        """CREATE TRIGGER IF NOT EXISTS validation_exceptions_no_update BEFORE UPDATE ON validation_exceptions
               BEGIN SELECT RAISE(ABORT, 'validation_exceptions is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS validation_exceptions_no_delete BEFORE DELETE ON validation_exceptions
               BEGIN SELECT RAISE(ABORT, 'validation_exceptions is insert-only'); END""",
        """ALTER TABLE backtest_runs ADD COLUMN kind TEXT NOT NULL DEFAULT 'backtest'
               CHECK (kind IN ('backtest', 'validation', 'holdout'))""",
        "CREATE INDEX IF NOT EXISTS ix_backtest_runs_kind ON backtest_runs(strategy_id, version, kind, status)",
        # trial_number becomes settable exactly once (running -> done); the two triggers are recreated
        # after the backfill of the existing finished cards below
        "DROP TRIGGER IF EXISTS backtest_runs_identity_fixed",
        "DROP TRIGGER IF EXISTS backtest_runs_finished_immutable",
        lambda conn: _v6_families(conn),
        """CREATE TRIGGER backtest_runs_finished_immutable BEFORE UPDATE ON backtest_runs
               WHEN OLD.status NOT IN ('queued', 'running')
               BEGIN SELECT RAISE(ABORT, 'backtest_runs: a finished run card is immutable'); END""",
        """CREATE TRIGGER backtest_runs_identity_fixed BEFORE UPDATE ON backtest_runs
               WHEN NEW.id IS NOT OLD.id OR NEW.created_at IS NOT OLD.created_at
                 OR NEW.strategy_id IS NOT OLD.strategy_id OR NEW.version IS NOT OLD.version
                 OR NEW.content_hash IS NOT OLD.content_hash OR NEW.spec_json IS NOT OLD.spec_json
                 OR NEW.request_json IS NOT OLD.request_json OR NEW.seed IS NOT OLD.seed
                 OR NEW.kind IS NOT OLD.kind
                 OR (NEW.trial_number IS NOT OLD.trial_number
                     AND NOT (OLD.trial_number IS NULL AND OLD.status = 'running' AND NEW.status = 'done'))
               BEGIN SELECT RAISE(ABORT, 'backtest_runs: identity columns are fixed'); END""",
    ]),
    # v7 (stage 4 review, SPEC §9.8): the "logic hash" of every strategy version (canonical spec without
    # name/description/category/tags/provenance/parameter descriptions) in its own insert-only table
    # (strategy_versions keeps its columns), backfilled from the stored specs; one transaction.
    (7, [
        _begin,
        """CREATE TABLE IF NOT EXISTS strategy_logic (
               strategy_id TEXT NOT NULL,
               version     INTEGER NOT NULL,
               logic_hash  TEXT NOT NULL,
               PRIMARY KEY (strategy_id, version))""",
        "CREATE INDEX IF NOT EXISTS ix_strategy_logic_hash ON strategy_logic(logic_hash)",
        """CREATE TRIGGER IF NOT EXISTS strategy_logic_no_update BEFORE UPDATE ON strategy_logic
               BEGIN SELECT RAISE(ABORT, 'strategy_logic is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS strategy_logic_no_delete BEFORE DELETE ON strategy_logic
               BEGIN SELECT RAISE(ABORT, 'strategy_logic is insert-only'); END""",
        lambda conn: _v7_logic(conn),
    ]),
    # v8 (SPEC §10.3, R2 notes): resumable runs — run card columns resumed_from / resume_count / error_kind /
    # auto_resume, the insert-only table run_checkpoints, and a running card may now be "noted" (running ->
    # running: ONLY resume_count up and error_kind — the waiting state of the auto-resume). One transaction.
    (8, [
        _begin,
        "ALTER TABLE backtest_runs ADD COLUMN resumed_from TEXT",      # id of the failed run this one resumes
        "ALTER TABLE backtest_runs ADD COLUMN resume_count INTEGER NOT NULL DEFAULT 0",
        """ALTER TABLE backtest_runs ADD COLUMN error_kind TEXT
               CHECK (error_kind IS NULL OR error_kind IN ('connection', 'interrupted', 'basis', 'error'))""",
        "ALTER TABLE backtest_runs ADD COLUMN auto_resume INTEGER NOT NULL DEFAULT 0 CHECK (auto_resume IN (0, 1))",
        # one successor per failed run: a resumed chain can never fork (it counts as ONE trial)
        """CREATE UNIQUE INDEX IF NOT EXISTS ux_backtest_runs_resumed_from ON backtest_runs(resumed_from)
               WHERE resumed_from IS NOT NULL""",
        """CREATE TABLE IF NOT EXISTS run_checkpoints (
               id           INTEGER PRIMARY KEY AUTOINCREMENT,
               run_id       TEXT NOT NULL,
               step_key     TEXT NOT NULL,          -- basis | data:<call> | grid:<config hash> | stress:<key>
               payload_json TEXT NOT NULL,          -- canonical JSON
               sha256       TEXT NOT NULL,          -- sha256 over run_id, step_key, payload_json; verified on reuse
               created_at   TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS ix_run_checkpoints_run ON run_checkpoints(run_id, step_key, id)",
        """CREATE TRIGGER IF NOT EXISTS run_checkpoints_no_update BEFORE UPDATE ON run_checkpoints
               BEGIN SELECT RAISE(ABORT, 'run_checkpoints is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS run_checkpoints_no_delete BEFORE DELETE ON run_checkpoints
               BEGIN SELECT RAISE(ABORT, 'run_checkpoints is insert-only'); END""",
        "DROP TRIGGER IF EXISTS backtest_runs_status_transition",
        """CREATE TRIGGER backtest_runs_status_transition BEFORE UPDATE ON backtest_runs
               WHEN OLD.status IN ('queued', 'running')
                AND NOT ((OLD.status = 'queued' AND NEW.status IN ('running', 'failed', 'cancelled'))
                      OR (OLD.status = 'running' AND NEW.status IN ('running', 'done', 'failed', 'cancelled')))
               BEGIN SELECT RAISE(ABORT, 'backtest_runs: only status transitions are allowed'); END""",
        """CREATE TRIGGER IF NOT EXISTS backtest_runs_running_note BEFORE UPDATE ON backtest_runs
               WHEN OLD.status = 'running' AND NEW.status = 'running'
                AND (NEW.engine_version IS NOT OLD.engine_version OR NEW.range_json IS NOT OLD.range_json
                  OR NEW.data_json IS NOT OLD.data_json OR NEW.licences_json IS NOT OLD.licences_json
                  OR NEW.cost_model_json IS NOT OLD.cost_model_json OR NEW.metrics_json IS NOT OLD.metrics_json
                  OR NEW.curve_json IS NOT OLD.curve_json OR NEW.trades_json IS NOT OLD.trades_json
                  OR NEW.warnings_json IS NOT OLD.warnings_json OR NEW.stress_json IS NOT OLD.stress_json
                  OR NEW.error IS NOT OLD.error OR NEW.started_at IS NOT OLD.started_at
                  OR NEW.finished_at IS NOT OLD.finished_at)
               BEGIN SELECT RAISE(ABORT, 'backtest_runs: a running card may only note resume_count / error_kind'); END""",
        "DROP TRIGGER IF EXISTS backtest_runs_identity_fixed",
        """CREATE TRIGGER backtest_runs_identity_fixed BEFORE UPDATE ON backtest_runs
               WHEN NEW.id IS NOT OLD.id OR NEW.created_at IS NOT OLD.created_at
                 OR NEW.strategy_id IS NOT OLD.strategy_id OR NEW.version IS NOT OLD.version
                 OR NEW.content_hash IS NOT OLD.content_hash OR NEW.spec_json IS NOT OLD.spec_json
                 OR NEW.request_json IS NOT OLD.request_json OR NEW.seed IS NOT OLD.seed
                 OR NEW.kind IS NOT OLD.kind
                 OR NEW.resumed_from IS NOT OLD.resumed_from OR NEW.auto_resume IS NOT OLD.auto_resume
                 OR NEW.resume_count < OLD.resume_count
                 OR (NEW.trial_number IS NOT OLD.trial_number
                     AND NOT (OLD.trial_number IS NULL AND OLD.status = 'running' AND NEW.status = 'done'))
               BEGIN SELECT RAISE(ABORT, 'backtest_runs: identity columns are fixed'); END""",
    ]),
    # v9 (SPEC §11.1, X1 2026-10-14): attribution (layer 1 of "Explain") on the finished card — column
    # attribution_json, written ONLY with running -> done (part of the insert-only finished card; the
    # finished-immutable trigger covers it). Cards finished before stay NULL (user decision §11.4). The
    # running-note trigger is rebuilt so a running -> running note cannot write it either.
    (9, [
        _begin,
        "ALTER TABLE backtest_runs ADD COLUMN attribution_json TEXT",
        "DROP TRIGGER IF EXISTS backtest_runs_running_note",
        """CREATE TRIGGER backtest_runs_running_note BEFORE UPDATE ON backtest_runs
               WHEN OLD.status = 'running' AND NEW.status = 'running'
                AND (NEW.engine_version IS NOT OLD.engine_version OR NEW.range_json IS NOT OLD.range_json
                  OR NEW.data_json IS NOT OLD.data_json OR NEW.licences_json IS NOT OLD.licences_json
                  OR NEW.cost_model_json IS NOT OLD.cost_model_json OR NEW.metrics_json IS NOT OLD.metrics_json
                  OR NEW.curve_json IS NOT OLD.curve_json OR NEW.trades_json IS NOT OLD.trades_json
                  OR NEW.warnings_json IS NOT OLD.warnings_json OR NEW.stress_json IS NOT OLD.stress_json
                  OR NEW.attribution_json IS NOT OLD.attribution_json
                  OR NEW.error IS NOT OLD.error OR NEW.started_at IS NOT OLD.started_at
                  OR NEW.finished_at IS NOT OLD.finished_at)
               BEGIN SELECT RAISE(ABORT, 'backtest_runs: a running card may only note resume_count / error_kind'); END""",
    ]),
    # v10 (SPEC §11.2, X2 2026-10-15): AI explanations (layer 2 of "Explain") — insert-only table
    # run_explanations; regenerate = a new row. The explanation budget lives in meta (keys
    # EXPLAIN_BUDGET_KEY and EXPLAIN_SPEND_PREFIX + UTC day), no schema needed for it.
    (10, [
        _begin,
        """CREATE TABLE IF NOT EXISTS run_explanations (
               id              INTEGER PRIMARY KEY AUTOINCREMENT,
               run_id          TEXT NOT NULL,                  -- a done run card (backtest_runs.id)
               run_kind        TEXT NOT NULL CHECK (run_kind IN ('backtest', 'validation', 'holdout')),
               created_at      TEXT NOT NULL,
               model           TEXT NOT NULL,                  -- the model id OpenRouter answered with
               language        TEXT NOT NULL CHECK (language IN ('en', 'de')),
               facts_hash      TEXT NOT NULL,                  -- sha256 of the canonical fact table
               output_json     TEXT NOT NULL,                  -- {summary, sections, hypotheses, limits}
               check_json      TEXT NOT NULL,                  -- {ok, unverified, unknown_ids}
               usage_json      TEXT,
               cost_usd        REAL,
               provenance_json TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS ix_run_explanations_run ON run_explanations(run_id, id)",
        """CREATE TRIGGER IF NOT EXISTS run_explanations_no_update BEFORE UPDATE ON run_explanations
               BEGIN SELECT RAISE(ABORT, 'run_explanations is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS run_explanations_no_delete BEFORE DELETE ON run_explanations
               BEGIN SELECT RAISE(ABORT, 'run_explanations is insert-only'); END""",
    ]),
    # v11 (SPEC §13, stage 5a, 2026-10-17): Strategy Studio + memory. studio_sessions: insert-only except
    # strategy_id, which is set ONCE (NULL -> the strategy the first valid draft created); studio_messages,
    # studio_backtests (the draft-backtest queue as an event log) and memory_facts: insert-only. The ideation
    # budget lives in meta (IDEATION_* keys), no schema needed for it.
    (11, [
        _begin,
        """CREATE TABLE IF NOT EXISTS studio_sessions (
               id          TEXT PRIMARY KEY,                 -- ss-<12 hex>
               created_at  TEXT NOT NULL,
               title       TEXT,
               strategy_id TEXT)""",
        """CREATE TRIGGER IF NOT EXISTS studio_sessions_strategy_once BEFORE UPDATE ON studio_sessions
               WHEN NEW.id IS NOT OLD.id OR NEW.created_at IS NOT OLD.created_at OR NEW.title IS NOT OLD.title
                 OR OLD.strategy_id IS NOT NULL OR NEW.strategy_id IS NULL
               BEGIN SELECT RAISE(ABORT, 'studio_sessions is insert-only (strategy_id is set once)'); END""",
        """CREATE TRIGGER IF NOT EXISTS studio_sessions_no_delete BEFORE DELETE ON studio_sessions
               BEGIN SELECT RAISE(ABORT, 'studio_sessions is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS studio_messages (
               id                     INTEGER PRIMARY KEY AUTOINCREMENT,
               session_id             TEXT NOT NULL,
               role                   TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system_note')),
               text                   TEXT NOT NULL,
               spec_version           INTEGER,
               run_id                 TEXT,
               operator_requests_json TEXT,
               provenance_json        TEXT,
               usage_json             TEXT,
               cost_usd               REAL,
               created_at             TEXT NOT NULL,
               detail_json            TEXT)""",
        "CREATE INDEX IF NOT EXISTS ix_studio_messages_session ON studio_messages(session_id, id)",
        """CREATE TRIGGER IF NOT EXISTS studio_messages_no_update BEFORE UPDATE ON studio_messages
               BEGIN SELECT RAISE(ABORT, 'studio_messages is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS studio_messages_no_delete BEFORE DELETE ON studio_messages
               BEGIN SELECT RAISE(ABORT, 'studio_messages is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS studio_backtests (
               id          INTEGER PRIMARY KEY AUTOINCREMENT,
               session_id  TEXT NOT NULL,
               message_id  INTEGER NOT NULL,
               strategy_id TEXT NOT NULL,
               version     INTEGER NOT NULL,
               event       TEXT NOT NULL CHECK (event IN ('waiting', 'started', 'cancelled', 'start_failed',
                                                         'not_queued')),
               run_id      TEXT,
               note        TEXT,
               created_at  TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS ix_studio_backtests_message ON studio_backtests(message_id, id)",
        """CREATE TRIGGER IF NOT EXISTS studio_backtests_no_update BEFORE UPDATE ON studio_backtests
               BEGIN SELECT RAISE(ABORT, 'studio_backtests is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS studio_backtests_no_delete BEFORE DELETE ON studio_backtests
               BEGIN SELECT RAISE(ABORT, 'studio_backtests is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS memory_facts (
               id          INTEGER PRIMARY KEY AUTOINCREMENT,
               family_id   TEXT NOT NULL,
               strategy_id TEXT NOT NULL,
               version     INTEGER NOT NULL,
               run_id      TEXT NOT NULL,
               kind        TEXT NOT NULL,
               fact_json   TEXT NOT NULL,
               created_at  TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS ix_memory_facts_family ON memory_facts(family_id, id)",
        "CREATE INDEX IF NOT EXISTS ix_memory_facts_run ON memory_facts(run_id)",
        """CREATE TRIGGER IF NOT EXISTS memory_facts_no_update BEFORE UPDATE ON memory_facts
               BEGIN SELECT RAISE(ABORT, 'memory_facts is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS memory_facts_no_delete BEFORE DELETE ON memory_facts
               BEGIN SELECT RAISE(ABORT, 'memory_facts is insert-only'); END""",
    ]),
    # v12 (SPEC §13.5 review M1, 2026-10-17): a valid Studio draft of a candidate/validated/… strategy is
    # saved only after the user confirms (it demotes the strategy to draft) — the confirmation is recorded
    # here (one row per draft message, insert-only) because studio_messages is insert-only.
    (12, [
        _begin,
        """CREATE TABLE IF NOT EXISTS studio_draft_saves (
               id          INTEGER PRIMARY KEY AUTOINCREMENT,
               session_id  TEXT NOT NULL,
               message_id  INTEGER NOT NULL UNIQUE,
               strategy_id TEXT NOT NULL,
               version     INTEGER NOT NULL,
               created_at  TEXT NOT NULL)""",
        """CREATE TRIGGER IF NOT EXISTS studio_draft_saves_no_update BEFORE UPDATE ON studio_draft_saves
               BEGIN SELECT RAISE(ABORT, 'studio_draft_saves is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS studio_draft_saves_no_delete BEFORE DELETE ON studio_draft_saves
               BEGIN SELECT RAISE(ABORT, 'studio_draft_saves is insert-only'); END""",
    ]),
    # v13 (SPEC §14, stage 5b, 2026-10-17): papers (+ extractions, confirmations, replication events) and the
    # investment committee (runs, messages, events) — all insert-only; states are derived from the events.
    (13, [
        _begin,
        """CREATE TABLE IF NOT EXISTS papers (
               id TEXT PRIMARY KEY, source_kind TEXT NOT NULL CHECK (source_kind IN ('pdf', 'text', 'arxiv', 'ssrn')),
               url TEXT, filename TEXT, sha256 TEXT NOT NULL UNIQUE, title TEXT, authors TEXT, year INTEGER,
               text_chars INTEGER NOT NULL, pages INTEGER, created_at TEXT NOT NULL)""",
        """CREATE TRIGGER IF NOT EXISTS papers_no_update BEFORE UPDATE ON papers
               BEGIN SELECT RAISE(ABORT, 'papers is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS papers_no_delete BEFORE DELETE ON papers
               BEGIN SELECT RAISE(ABORT, 'papers is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS paper_extractions (
               id INTEGER PRIMARY KEY AUTOINCREMENT, paper_id TEXT NOT NULL, created_at TEXT NOT NULL,
               model TEXT NOT NULL, model_tier TEXT NOT NULL, relevant INTEGER NOT NULL CHECK (relevant IN (0, 1)),
               result_json TEXT NOT NULL, provenance_json TEXT NOT NULL, usage_json TEXT, cost_usd REAL)""",
        """CREATE TRIGGER IF NOT EXISTS paper_extractions_no_update BEFORE UPDATE ON paper_extractions
               BEGIN SELECT RAISE(ABORT, 'paper_extractions is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS paper_extractions_no_delete BEFORE DELETE ON paper_extractions
               BEGIN SELECT RAISE(ABORT, 'paper_extractions is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS paper_confirmations (
               id INTEGER PRIMARY KEY AUTOINCREMENT, paper_id TEXT NOT NULL UNIQUE, extraction_id INTEGER NOT NULL,
               created_at TEXT NOT NULL, confirmed_json TEXT NOT NULL, strategy_id TEXT, version INTEGER,
               draft_json TEXT)""",
        """CREATE TRIGGER IF NOT EXISTS paper_confirmations_no_update BEFORE UPDATE ON paper_confirmations
               BEGIN SELECT RAISE(ABORT, 'paper_confirmations is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS paper_confirmations_no_delete BEFORE DELETE ON paper_confirmations
               BEGIN SELECT RAISE(ABORT, 'paper_confirmations is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS paper_replications (
               id INTEGER PRIMARY KEY AUTOINCREMENT, paper_id TEXT NOT NULL, strategy_id TEXT NOT NULL,
               version INTEGER NOT NULL, event TEXT NOT NULL CHECK (event IN ('waiting', 'started', 'start_failed')),
               run_id TEXT, request_json TEXT, note TEXT, created_at TEXT NOT NULL)""",
        """CREATE TRIGGER IF NOT EXISTS paper_replications_no_update BEFORE UPDATE ON paper_replications
               BEGIN SELECT RAISE(ABORT, 'paper_replications is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS paper_replications_no_delete BEFORE DELETE ON paper_replications
               BEGIN SELECT RAISE(ABORT, 'paper_replications is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS committee_runs (
               id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, version INTEGER NOT NULL, validation_run_id TEXT,
               model_tier TEXT NOT NULL, model TEXT, trigger TEXT NOT NULL CHECK (trigger IN ('manual', 'auto')),
               holdout_included INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)""",
        """CREATE TRIGGER IF NOT EXISTS committee_runs_no_update BEFORE UPDATE ON committee_runs
               BEGIN SELECT RAISE(ABORT, 'committee_runs is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS committee_runs_no_delete BEFORE DELETE ON committee_runs
               BEGIN SELECT RAISE(ABORT, 'committee_runs is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS committee_messages (
               id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, role TEXT NOT NULL, seq INTEGER NOT NULL,
               output_json TEXT NOT NULL, check_json TEXT, provenance_json TEXT NOT NULL, usage_json TEXT,
               cost_usd REAL, created_at TEXT NOT NULL, UNIQUE (run_id, seq))""",
        """CREATE TRIGGER IF NOT EXISTS committee_messages_no_update BEFORE UPDATE ON committee_messages
               BEGIN SELECT RAISE(ABORT, 'committee_messages is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS committee_messages_no_delete BEFORE DELETE ON committee_messages
               BEGIN SELECT RAISE(ABORT, 'committee_messages is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS committee_events (
               id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
               event TEXT NOT NULL CHECK (event IN ('started', 'role_done', 'cancel_requested', 'cancelled', 'failed',
                                                    'done', 'skipped')),
               role TEXT, note TEXT, created_at TEXT NOT NULL)""",
        """CREATE TRIGGER IF NOT EXISTS committee_events_no_update BEFORE UPDATE ON committee_events
               BEGIN SELECT RAISE(ABORT, 'committee_events is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS committee_events_no_delete BEFORE DELETE ON committee_events
               BEGIN SELECT RAISE(ABORT, 'committee_events is insert-only'); END""",
        """CREATE INDEX IF NOT EXISTS ix_paper_extractions_paper ON paper_extractions(paper_id, id)""",
        """CREATE INDEX IF NOT EXISTS ix_paper_replications_paper ON paper_replications(paper_id, id)""",
        """CREATE INDEX IF NOT EXISTS ix_committee_runs_strategy ON committee_runs(strategy_id, created_at)""",
        """CREATE INDEX IF NOT EXISTS ix_committee_messages_run ON committee_messages(run_id, seq)""",
        """CREATE INDEX IF NOT EXISTS ix_committee_events_run ON committee_events(run_id, id)""",
    ]),
    # v14 (SPEC §16, stage 6, 2026-10-17): the discovery loop — runs, variants (one row per generation slot),
    # an append-only event log (state = fold over the events) and the loop's AI messages; all insert-only.
    (14, [
        _begin,
        """CREATE TABLE IF NOT EXISTS loop_runs (
               id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
               seed_kind TEXT NOT NULL CHECK (seed_kind IN ('strategy', 'idea', 'paper')),
               seed_json TEXT NOT NULL, config_json TEXT NOT NULL)""",
        """CREATE TRIGGER IF NOT EXISTS loop_runs_no_update BEFORE UPDATE ON loop_runs
               BEGIN SELECT RAISE(ABORT, 'loop_runs is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS loop_runs_no_delete BEFORE DELETE ON loop_runs
               BEGIN SELECT RAISE(ABORT, 'loop_runs is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS loop_variants (
               id TEXT PRIMARY KEY, loop_id TEXT NOT NULL, generation INTEGER NOT NULL, slot INTEGER NOT NULL,
               origin TEXT NOT NULL CHECK (origin IN ('seed', 'mutation', 'refined', 'child', 'elite')),
               parent_variant_id TEXT, created_at TEXT NOT NULL, UNIQUE (loop_id, generation, slot))""",
        """CREATE TRIGGER IF NOT EXISTS loop_variants_no_update BEFORE UPDATE ON loop_variants
               BEGIN SELECT RAISE(ABORT, 'loop_variants is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS loop_variants_no_delete BEFORE DELETE ON loop_variants
               BEGIN SELECT RAISE(ABORT, 'loop_variants is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS loop_events (
               id INTEGER PRIMARY KEY AUTOINCREMENT, loop_id TEXT NOT NULL, event TEXT NOT NULL,
               generation INTEGER, variant_id TEXT, data_json TEXT, created_at TEXT NOT NULL)""",
        """CREATE TRIGGER IF NOT EXISTS loop_events_no_update BEFORE UPDATE ON loop_events
               BEGIN SELECT RAISE(ABORT, 'loop_events is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS loop_events_no_delete BEFORE DELETE ON loop_events
               BEGIN SELECT RAISE(ABORT, 'loop_events is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS loop_ai_messages (
               id INTEGER PRIMARY KEY AUTOINCREMENT, loop_id TEXT NOT NULL, variant_id TEXT,
               role TEXT NOT NULL CHECK (role IN ('seed', 'diagnosis', 'refinement')),
               output_json TEXT NOT NULL, check_json TEXT, provenance_json TEXT NOT NULL, usage_json TEXT,
               cost_usd REAL, created_at TEXT NOT NULL)""",
        """CREATE TRIGGER IF NOT EXISTS loop_ai_messages_no_update BEFORE UPDATE ON loop_ai_messages
               BEGIN SELECT RAISE(ABORT, 'loop_ai_messages is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS loop_ai_messages_no_delete BEFORE DELETE ON loop_ai_messages
               BEGIN SELECT RAISE(ABORT, 'loop_ai_messages is insert-only'); END""",
        """CREATE INDEX IF NOT EXISTS ix_loop_variants_loop ON loop_variants(loop_id, generation, slot)""",
        """CREATE INDEX IF NOT EXISTS ix_loop_events_loop ON loop_events(loop_id, id)""",
        """CREATE INDEX IF NOT EXISTS ix_loop_ai_messages_loop ON loop_ai_messages(loop_id, id)""",
    ]),
    # v15 (SPEC §17, stage 7, 2026-10-17): platform paper accounts (the PaperBroker's book per account,
    # pa_*), OMS orders + events, virtual sub-ledgers per strategy source, runtime decisions/executions,
    # incubation runs (insert-only: the length can never change), settings/allocations/migrations history.
    (15, [
        _begin,
        """CREATE TABLE IF NOT EXISTS pf_accounts (
               id TEXT PRIMARY KEY, broker TEXT NOT NULL CHECK (broker IN ('internal_paper')),
               label TEXT NOT NULL, origin TEXT NOT NULL CHECK (origin IN ('opened', 'migrated')),
               params_json TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL, migration_id TEXT)""",
        """CREATE TRIGGER IF NOT EXISTS pf_accounts_no_update BEFORE UPDATE ON pf_accounts
               BEGIN SELECT RAISE(ABORT, 'pf_accounts is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS pf_accounts_no_delete BEFORE DELETE ON pf_accounts
               BEGIN SELECT RAISE(ABORT, 'pf_accounts is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS pf_settings (
               id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, created_at TEXT NOT NULL,
               settings_json TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT)""",
        """CREATE TRIGGER IF NOT EXISTS pf_settings_no_update BEFORE UPDATE ON pf_settings
               BEGIN SELECT RAISE(ABORT, 'pf_settings is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS pf_settings_no_delete BEFORE DELETE ON pf_settings
               BEGIN SELECT RAISE(ABORT, 'pf_settings is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS pf_allocations (
               id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, created_at TEXT NOT NULL,
               actor TEXT NOT NULL, reason TEXT, result_json TEXT NOT NULL)""",
        """CREATE TRIGGER IF NOT EXISTS pf_allocations_no_update BEFORE UPDATE ON pf_allocations
               BEGIN SELECT RAISE(ABORT, 'pf_allocations is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS pf_allocations_no_delete BEFORE DELETE ON pf_allocations
               BEGIN SELECT RAISE(ABORT, 'pf_allocations is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS pf_migrations (
               id TEXT PRIMARY KEY, created_at TEXT NOT NULL, account_id TEXT NOT NULL, legacy_path TEXT,
               summary_json TEXT NOT NULL)""",
        """CREATE TRIGGER IF NOT EXISTS pf_migrations_no_update BEFORE UPDATE ON pf_migrations
               BEGIN SELECT RAISE(ABORT, 'pf_migrations is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS pf_migrations_no_delete BEFORE DELETE ON pf_migrations
               BEGIN SELECT RAISE(ABORT, 'pf_migrations is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS pa_orders (
               id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, ts TEXT NOT NULL,
               symbol TEXT NOT NULL, side TEXT NOT NULL, qty REAL NOT NULL, order_type TEXT NOT NULL DEFAULT 'market',
               limit_price REAL, status TEXT NOT NULL, broker TEXT, broker_order_id TEXT,
               mode TEXT NOT NULL DEFAULT 'paper' CHECK (mode = 'paper'), rationale TEXT, provenance_json TEXT,
               run_id INTEGER, recommendation_id INTEGER, origin TEXT, fill_price REAL, fees REAL, filled_at TEXT,
               reject_reason TEXT, notional REAL, client_order_id TEXT, legacy_id INTEGER)""",
        """CREATE TRIGGER IF NOT EXISTS pa_orders_no_delete BEFORE DELETE ON pa_orders
               BEGIN SELECT RAISE(ABORT, 'rows of pa_orders are never deleted'); END""",
        """CREATE UNIQUE INDEX IF NOT EXISTS ux_pa_orders_client ON pa_orders(account_id, client_order_id)
               WHERE client_order_id IS NOT NULL""",
        """CREATE INDEX IF NOT EXISTS ix_pa_orders_account ON pa_orders(account_id, status, id)""",
        """CREATE TABLE IF NOT EXISTS pa_positions (
               account_id TEXT NOT NULL, symbol TEXT NOT NULL, qty REAL NOT NULL, avg_price REAL,
               updated_at TEXT NOT NULL, opened_at TEXT, realized_pnl REAL DEFAULT 0, borrow_rate_pct REAL,
               last_price REAL, PRIMARY KEY (account_id, symbol))""",
        """CREATE TABLE IF NOT EXISTS pa_cash_ledger (
               id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, ts TEXT NOT NULL, kind TEXT NOT NULL,
               symbol TEXT, amount REAL NOT NULL, note TEXT, legacy_id INTEGER)""",
        """CREATE TRIGGER IF NOT EXISTS pa_cash_ledger_no_update BEFORE UPDATE ON pa_cash_ledger
               BEGIN SELECT RAISE(ABORT, 'pa_cash_ledger is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS pa_cash_ledger_no_delete BEFORE DELETE ON pa_cash_ledger
               BEGIN SELECT RAISE(ABORT, 'pa_cash_ledger is insert-only'); END""",
        """CREATE INDEX IF NOT EXISTS ix_pa_cash_ledger_account ON pa_cash_ledger(account_id, id)""",
        """CREATE TABLE IF NOT EXISTS pa_equity (
               account_id TEXT NOT NULL, ts TEXT NOT NULL, equity REAL NOT NULL, cash REAL, long_mv REAL,
               short_mv REAL, benchmark REAL, drawdown_pct REAL, PRIMARY KEY (account_id, ts))""",
        """CREATE TABLE IF NOT EXISTS pa_events (
               id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, ts TEXT NOT NULL, kind TEXT NOT NULL,
               symbol TEXT, title TEXT NOT NULL, body TEXT)""",
        """CREATE TABLE IF NOT EXISTS pa_meta (
               account_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT, PRIMARY KEY (account_id, key))""",
        """CREATE TABLE IF NOT EXISTS oms_orders (
               client_order_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, created_at TEXT NOT NULL,
               updated_at TEXT NOT NULL, symbol TEXT NOT NULL,
               side TEXT NOT NULL CHECK (side IN ('buy', 'sell', 'short', 'cover')),
               qty REAL, notional_req REAL, origin TEXT NOT NULL, tif TEXT NOT NULL CHECK (tif IN ('day', 'gtc')),
               reason TEXT, ref_price REAL,
               state TEXT NOT NULL CHECK (state IN ('new', 'submitted', 'accepted', 'partially_filled', 'filled',
                                                     'cancelled', 'rejected', 'expired')),
               broker_order_id TEXT, filled_qty REAL NOT NULL DEFAULT 0, avg_fill_price REAL,
               fees REAL NOT NULL DEFAULT 0, notional REAL, reject_reason TEXT, attempts INTEGER NOT NULL DEFAULT 0,
               allow_reduce INTEGER NOT NULL DEFAULT 0, allocations_json TEXT, checks_json TEXT, execution_id TEXT)""",
        """CREATE TRIGGER IF NOT EXISTS oms_orders_no_delete BEFORE DELETE ON oms_orders
               BEGIN SELECT RAISE(ABORT, 'rows of oms_orders are never deleted'); END""",
        """CREATE INDEX IF NOT EXISTS ix_oms_orders_account ON oms_orders(account_id, state, created_at)""",
        """CREATE TABLE IF NOT EXISTS oms_order_events (
               id INTEGER PRIMARY KEY AUTOINCREMENT, client_order_id TEXT NOT NULL, ts TEXT NOT NULL,
               from_state TEXT, to_state TEXT NOT NULL, note TEXT, data_json TEXT)""",
        """CREATE TRIGGER IF NOT EXISTS oms_order_events_no_update BEFORE UPDATE ON oms_order_events
               BEGIN SELECT RAISE(ABORT, 'oms_order_events is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS oms_order_events_no_delete BEFORE DELETE ON oms_order_events
               BEGIN SELECT RAISE(ABORT, 'oms_order_events is insert-only'); END""",
        """CREATE INDEX IF NOT EXISTS ix_oms_order_events_order ON oms_order_events(client_order_id, id)""",
        """CREATE TABLE IF NOT EXISTS sl_entries (
               id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, source_id TEXT NOT NULL, ts TEXT NOT NULL,
               kind TEXT NOT NULL CHECK (kind IN ('capital', 'fill', 'fee', 'slippage', 'borrow_fee', 'dividend',
                                                  'dividend_short', 'funding', 'liquidation_fee', 'cross',
                                                  'migration', 'other')),
               symbol TEXT, qty REAL NOT NULL DEFAULT 0, price REAL, amount REAL NOT NULL DEFAULT 0, ref TEXT,
               note TEXT)""",
        """CREATE TRIGGER IF NOT EXISTS sl_entries_no_update BEFORE UPDATE ON sl_entries
               BEGIN SELECT RAISE(ABORT, 'sl_entries is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS sl_entries_no_delete BEFORE DELETE ON sl_entries
               BEGIN SELECT RAISE(ABORT, 'sl_entries is insert-only'); END""",
        """CREATE INDEX IF NOT EXISTS ix_sl_entries_source ON sl_entries(account_id, source_id, id)""",
        """CREATE TABLE IF NOT EXISTS sl_snapshots (
               account_id TEXT NOT NULL, source_id TEXT NOT NULL, day TEXT NOT NULL, ts TEXT NOT NULL,
               equity REAL NOT NULL, cash REAL, long_mv REAL, short_mv REAL,
               PRIMARY KEY (account_id, source_id, day))""",
        """CREATE TABLE IF NOT EXISTS rt_decisions (
               id TEXT PRIMARY KEY, account_id TEXT NOT NULL, source_id TEXT NOT NULL, strategy_id TEXT,
               version INTEGER, incubation_id TEXT, bar_date TEXT NOT NULL, created_at TEXT NOT NULL,
               status TEXT NOT NULL CHECK (status IN ('ok', 'failed')), result_json TEXT NOT NULL)""",
        """CREATE TRIGGER IF NOT EXISTS rt_decisions_no_update BEFORE UPDATE ON rt_decisions
               BEGIN SELECT RAISE(ABORT, 'rt_decisions is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS rt_decisions_no_delete BEFORE DELETE ON rt_decisions
               BEGIN SELECT RAISE(ABORT, 'rt_decisions is insert-only'); END""",
        """CREATE INDEX IF NOT EXISTS ix_rt_decisions_source ON rt_decisions(account_id, source_id, created_at)""",
        """CREATE TABLE IF NOT EXISTS rt_executions (
               id TEXT PRIMARY KEY, account_id TEXT NOT NULL, created_at TEXT NOT NULL, decision_ids_json TEXT NOT NULL,
               result_json TEXT NOT NULL)""",
        """CREATE TRIGGER IF NOT EXISTS rt_executions_no_update BEFORE UPDATE ON rt_executions
               BEGIN SELECT RAISE(ABORT, 'rt_executions is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS rt_executions_no_delete BEFORE DELETE ON rt_executions
               BEGIN SELECT RAISE(ABORT, 'rt_executions is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS incubation_runs (
               id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, version INTEGER NOT NULL, content_hash TEXT,
               account_id TEXT NOT NULL, source_id TEXT NOT NULL,
               length_sessions INTEGER NOT NULL CHECK (length_sessions BETWEEN 20 AND 250),
               calendar TEXT NOT NULL CHECK (calendar IN ('nyse', 'utc')), start_date TEXT NOT NULL,
               end_date TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL, reason TEXT,
               expected_json TEXT)""",
        """CREATE TRIGGER IF NOT EXISTS incubation_runs_no_update BEFORE UPDATE ON incubation_runs
               BEGIN SELECT RAISE(ABORT, 'incubation_runs is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS incubation_runs_no_delete BEFORE DELETE ON incubation_runs
               BEGIN SELECT RAISE(ABORT, 'incubation_runs is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS incubation_events (
               id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, ts TEXT NOT NULL,
               event TEXT NOT NULL CHECK (event IN ('started', 'session', 'completed', 'aborted')), day TEXT,
               data_json TEXT)""",
        """CREATE TRIGGER IF NOT EXISTS incubation_events_no_update BEFORE UPDATE ON incubation_events
               BEGIN SELECT RAISE(ABORT, 'incubation_events is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS incubation_events_no_delete BEFORE DELETE ON incubation_events
               BEGIN SELECT RAISE(ABORT, 'incubation_events is insert-only'); END""",
        """CREATE UNIQUE INDEX IF NOT EXISTS ux_incubation_end ON incubation_events(run_id)
               WHERE event IN ('completed', 'aborted')""",
        """CREATE UNIQUE INDEX IF NOT EXISTS ux_incubation_session ON incubation_events(run_id, day)
               WHERE event = 'session'""",
        """CREATE INDEX IF NOT EXISTS ix_incubation_runs_strategy ON incubation_runs(strategy_id, created_at)""",
    ]),
    # v16 (SPEC §18, stage 8, 2026-10-18): broker accounts (Alpaca paper, Kraken Futures demo) — accounts,
    # connection history, routes, reconciliations, resolutions, live-guard settings (all insert-only), imported
    # broker events (dedupe), fill marks per broker order; OMS order types / legs.
    (16, [
        _begin,
        """CREATE TABLE IF NOT EXISTS br_accounts (
               id TEXT PRIMARY KEY CHECK (id IN ('alpaca-paper', 'kraken-demo')),
               broker TEXT NOT NULL CHECK (broker IN ('alpaca_paper', 'kraken_demo')),
               label TEXT NOT NULL, params_json TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL)""",
        """CREATE TRIGGER IF NOT EXISTS br_accounts_no_update BEFORE UPDATE ON br_accounts
               BEGIN SELECT RAISE(ABORT, 'br_accounts is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS br_accounts_no_delete BEFORE DELETE ON br_accounts
               BEGIN SELECT RAISE(ABORT, 'br_accounts is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS br_connections (
               id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, ts TEXT NOT NULL,
               status TEXT NOT NULL CHECK (status IN ('connected', 'disconnected')), actor TEXT NOT NULL,
               reason TEXT, detail_json TEXT)""",
        """CREATE TRIGGER IF NOT EXISTS br_connections_no_update BEFORE UPDATE ON br_connections
               BEGIN SELECT RAISE(ABORT, 'br_connections is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS br_connections_no_delete BEFORE DELETE ON br_connections
               BEGIN SELECT RAISE(ABORT, 'br_connections is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS br_routes (
               id INTEGER PRIMARY KEY AUTOINCREMENT, strategy_id TEXT NOT NULL, account_id TEXT NOT NULL,
               ts TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT)""",
        """CREATE TRIGGER IF NOT EXISTS br_routes_no_update BEFORE UPDATE ON br_routes
               BEGIN SELECT RAISE(ABORT, 'br_routes is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS br_routes_no_delete BEFORE DELETE ON br_routes
               BEGIN SELECT RAISE(ABORT, 'br_routes is insert-only'); END""",
        """CREATE INDEX IF NOT EXISTS ix_br_routes_strategy ON br_routes(strategy_id, id)""",
        """CREATE TABLE IF NOT EXISTS br_reconciliations (
               id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, ts TEXT NOT NULL, trigger TEXT NOT NULL,
               status TEXT NOT NULL CHECK (status IN ('ok', 'mismatch', 'unreachable')), result_json TEXT NOT NULL)""",
        """CREATE TRIGGER IF NOT EXISTS br_reconciliations_no_update BEFORE UPDATE ON br_reconciliations
               BEGIN SELECT RAISE(ABORT, 'br_reconciliations is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS br_reconciliations_no_delete BEFORE DELETE ON br_reconciliations
               BEGIN SELECT RAISE(ABORT, 'br_reconciliations is insert-only'); END""",
        """CREATE INDEX IF NOT EXISTS ix_br_reconciliations_account ON br_reconciliations(account_id, id)""",
        """CREATE TABLE IF NOT EXISTS br_resolutions (
               id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, ts TEXT NOT NULL,
               action TEXT NOT NULL CHECK (action IN ('recheck', 'cancel_unknown_orders', 'accept_broker')),
               actor TEXT NOT NULL, reason TEXT, reconciliation_id INTEGER, detail_json TEXT)""",
        """CREATE TRIGGER IF NOT EXISTS br_resolutions_no_update BEFORE UPDATE ON br_resolutions
               BEGIN SELECT RAISE(ABORT, 'br_resolutions is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS br_resolutions_no_delete BEFORE DELETE ON br_resolutions
               BEGIN SELECT RAISE(ABORT, 'br_resolutions is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS br_live_guard (
               id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, ts TEXT NOT NULL,
               live_flag INTEGER NOT NULL CHECK (live_flag IN (0, 1)), capital_cap_usd REAL, actor TEXT NOT NULL,
               reason TEXT)""",
        """CREATE TRIGGER IF NOT EXISTS br_live_guard_no_update BEFORE UPDATE ON br_live_guard
               BEGIN SELECT RAISE(ABORT, 'br_live_guard is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS br_live_guard_no_delete BEFORE DELETE ON br_live_guard
               BEGIN SELECT RAISE(ABORT, 'br_live_guard is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS br_imports (
               account_id TEXT NOT NULL, kind TEXT NOT NULL, ext_id TEXT NOT NULL, ts TEXT NOT NULL, data_json TEXT,
               PRIMARY KEY (account_id, kind, ext_id))""",
        """CREATE TRIGGER IF NOT EXISTS br_imports_no_update BEFORE UPDATE ON br_imports
               BEGIN SELECT RAISE(ABORT, 'br_imports is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS br_imports_no_delete BEFORE DELETE ON br_imports
               BEGIN SELECT RAISE(ABORT, 'br_imports is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS br_fill_marks (
               account_id TEXT NOT NULL, broker_order_id TEXT NOT NULL, client_order_id TEXT NOT NULL,
               filled_qty REAL NOT NULL DEFAULT 0, avg_price REAL, fees REAL NOT NULL DEFAULT 0,
               bookings INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
               PRIMARY KEY (account_id, broker_order_id))""",
        "ALTER TABLE oms_orders ADD COLUMN order_type TEXT NOT NULL DEFAULT 'market'",
        "ALTER TABLE oms_orders ADD COLUMN limit_price REAL",
        "ALTER TABLE oms_orders ADD COLUMN stop_price REAL",
        "ALTER TABLE oms_orders ADD COLUMN legs_json TEXT",
        "ALTER TABLE oms_orders ADD COLUMN parent_client_order_id TEXT",
        "ALTER TABLE oms_orders ADD COLUMN broker_status TEXT",
    ]),
    # v17 (SPEC §19, stage 9, 2026-10-18): monitoring (decay checks/events, alerts + acks, reports, AI commentaries)
    # and the Shadow Account (accounts, mapping profiles, imports, normalised transactions, classifications,
    # valuations) — all insert-only.
    (17, [
        _begin,
        """CREATE TABLE IF NOT EXISTS mon_decay_checks (
               id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, strategy_id TEXT NOT NULL, day TEXT NOT NULL,
               ts TEXT NOT NULL, state TEXT NOT NULL CHECK (state IN ('insufficient', 'ok', 'watch', 'paused')),
               result_json TEXT NOT NULL, UNIQUE (run_id, day))""",
        """CREATE TRIGGER IF NOT EXISTS mon_decay_checks_no_update BEFORE UPDATE ON mon_decay_checks
               BEGIN SELECT RAISE(ABORT, 'mon_decay_checks is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS mon_decay_checks_no_delete BEFORE DELETE ON mon_decay_checks
               BEGIN SELECT RAISE(ABORT, 'mon_decay_checks is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS mon_decay_events (
               id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, strategy_id TEXT NOT NULL, run_id TEXT NOT NULL,
               action TEXT NOT NULL CHECK (action IN ('paused', 'keep_running', 'demoted')), actor TEXT NOT NULL,
               reason TEXT, rule TEXT, check_day TEXT, data_json TEXT)""",
        """CREATE TRIGGER IF NOT EXISTS mon_decay_events_no_update BEFORE UPDATE ON mon_decay_events
               BEGIN SELECT RAISE(ABORT, 'mon_decay_events is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS mon_decay_events_no_delete BEFORE DELETE ON mon_decay_events
               BEGIN SELECT RAISE(ABORT, 'mon_decay_events is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS mon_alerts (
               id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL,
               severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')), title TEXT NOT NULL,
               body TEXT NOT NULL, account_id TEXT, strategy_id TEXT, ref TEXT, toast INTEGER NOT NULL CHECK (toast IN (0, 1)),
               dedupe_key TEXT NOT NULL UNIQUE, data_json TEXT)""",
        """CREATE TRIGGER IF NOT EXISTS mon_alerts_no_update BEFORE UPDATE ON mon_alerts
               BEGIN SELECT RAISE(ABORT, 'mon_alerts is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS mon_alerts_no_delete BEFORE DELETE ON mon_alerts
               BEGIN SELECT RAISE(ABORT, 'mon_alerts is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS mon_alert_acks (
               alert_id INTEGER PRIMARY KEY, ts TEXT NOT NULL, actor TEXT NOT NULL)""",
        """CREATE TRIGGER IF NOT EXISTS mon_alert_acks_no_update BEFORE UPDATE ON mon_alert_acks
               BEGIN SELECT RAISE(ABORT, 'mon_alert_acks is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS mon_alert_acks_no_delete BEFORE DELETE ON mon_alert_acks
               BEGIN SELECT RAISE(ABORT, 'mon_alert_acks is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS mon_reports (
               id TEXT PRIMARY KEY, period TEXT NOT NULL CHECK (period IN ('daily', 'weekly')), period_id TEXT NOT NULL,
               start_ts TEXT NOT NULL, end_ts TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL,
               numbers_json TEXT NOT NULL, facts_json TEXT NOT NULL)""",
        """CREATE TRIGGER IF NOT EXISTS mon_reports_no_update BEFORE UPDATE ON mon_reports
               BEGIN SELECT RAISE(ABORT, 'mon_reports is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS mon_reports_no_delete BEFORE DELETE ON mon_reports
               BEGIN SELECT RAISE(ABORT, 'mon_reports is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS mon_commentaries (
               id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('report', 'shadow')), ref_id TEXT NOT NULL,
               created_at TEXT NOT NULL, model TEXT, language TEXT, facts_hash TEXT, output_json TEXT NOT NULL,
               check_json TEXT NOT NULL, usage_json TEXT, cost_usd REAL, provenance_json TEXT NOT NULL)""",
        """CREATE TRIGGER IF NOT EXISTS mon_commentaries_no_update BEFORE UPDATE ON mon_commentaries
               BEGIN SELECT RAISE(ABORT, 'mon_commentaries is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS mon_commentaries_no_delete BEFORE DELETE ON mon_commentaries
               BEGIN SELECT RAISE(ABORT, 'mon_commentaries is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS sh_accounts (
               id TEXT PRIMARY KEY, label TEXT NOT NULL, base_currency TEXT NOT NULL CHECK (base_currency IN ('EUR', 'USD')),
               broker_country TEXT NOT NULL CHECK (broker_country IN ('DE', 'other')), created_at TEXT NOT NULL,
               created_by TEXT NOT NULL)""",
        """CREATE TRIGGER IF NOT EXISTS sh_accounts_no_update BEFORE UPDATE ON sh_accounts
               BEGIN SELECT RAISE(ABORT, 'sh_accounts is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS sh_accounts_no_delete BEFORE DELETE ON sh_accounts
               BEGIN SELECT RAISE(ABORT, 'sh_accounts is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS sh_profiles (
               id TEXT PRIMARY KEY, name TEXT NOT NULL, version INTEGER NOT NULL, mapping_json TEXT NOT NULL,
               created_at TEXT NOT NULL, created_by TEXT NOT NULL, UNIQUE (name, version))""",
        """CREATE TRIGGER IF NOT EXISTS sh_profiles_no_update BEFORE UPDATE ON sh_profiles
               BEGIN SELECT RAISE(ABORT, 'sh_profiles is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS sh_profiles_no_delete BEFORE DELETE ON sh_profiles
               BEGIN SELECT RAISE(ABORT, 'sh_profiles is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS sh_imports (
               id TEXT PRIMARY KEY, account_id TEXT NOT NULL, created_at TEXT NOT NULL, filename TEXT, sha256 TEXT NOT NULL,
               template TEXT, profile_id TEXT, rows INTEGER NOT NULL, accepted INTEGER NOT NULL, rejected INTEGER NOT NULL,
               duplicates INTEGER NOT NULL, skipped INTEGER NOT NULL, report_json TEXT)""",
        """CREATE TRIGGER IF NOT EXISTS sh_imports_no_update BEFORE UPDATE ON sh_imports
               BEGIN SELECT RAISE(ABORT, 'sh_imports is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS sh_imports_no_delete BEFORE DELETE ON sh_imports
               BEGIN SELECT RAISE(ABORT, 'sh_imports is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS sh_transactions (
               id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, import_id TEXT NOT NULL, row_no INTEGER,
               hash TEXT NOT NULL, date TEXT NOT NULL,
               kind TEXT NOT NULL CHECK (kind IN ('buy', 'sell', 'dividend', 'interest', 'withholding', 'fee', 'deposit',
                                                  'withdrawal')),
               key TEXT, symbol TEXT, isin TEXT, name TEXT, asset_class TEXT, qty REAL, price REAL,
               currency TEXT NOT NULL, gross REAL NOT NULL, fee REAL NOT NULL, tax REAL NOT NULL, net REAL NOT NULL,
               raw_type TEXT, UNIQUE (account_id, hash))""",
        """CREATE TRIGGER IF NOT EXISTS sh_transactions_no_update BEFORE UPDATE ON sh_transactions
               BEGIN SELECT RAISE(ABORT, 'sh_transactions is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS sh_transactions_no_delete BEFORE DELETE ON sh_transactions
               BEGIN SELECT RAISE(ABORT, 'sh_transactions is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS sh_classifications (
               id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL,
               asset_class TEXT NOT NULL CHECK (asset_class IN ('share', 'etf', 'fund', 'bond', 'crypto', 'derivative',
                                                                'other')),
               ts TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT)""",
        """CREATE TRIGGER IF NOT EXISTS sh_classifications_no_update BEFORE UPDATE ON sh_classifications
               BEGIN SELECT RAISE(ABORT, 'sh_classifications is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS sh_classifications_no_delete BEFORE DELETE ON sh_classifications
               BEGIN SELECT RAISE(ABORT, 'sh_classifications is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS sh_valuations (
               id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, as_of TEXT NOT NULL,
               prices_json TEXT NOT NULL, ts TEXT NOT NULL, actor TEXT NOT NULL)""",
        """CREATE TRIGGER IF NOT EXISTS sh_valuations_no_update BEFORE UPDATE ON sh_valuations
               BEGIN SELECT RAISE(ABORT, 'sh_valuations is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS sh_valuations_no_delete BEFORE DELETE ON sh_valuations
               BEGIN SELECT RAISE(ABORT, 'sh_valuations is insert-only'); END""",
        """CREATE INDEX IF NOT EXISTS ix_mon_decay_events_run ON mon_decay_events(run_id, id)""",
        """CREATE INDEX IF NOT EXISTS ix_mon_alerts_ts ON mon_alerts(ts)""",
        """CREATE INDEX IF NOT EXISTS ix_mon_reports_period ON mon_reports(period, period_id)""",
        """CREATE INDEX IF NOT EXISTS ix_mon_commentaries_ref ON mon_commentaries(kind, ref_id)""",
        """CREATE INDEX IF NOT EXISTS ix_sh_transactions_account ON sh_transactions(account_id, date)""",
    ]),
    # v18 (SPEC §19.14, stage-9 review fixes 2026-10-18): a Shadow import batch can be removed by the user (rows
    # only: the delete triggers of sh_imports / sh_transactions are dropped; every removal is recorded insert-only
    # in sh_import_removals + audit); one import per identical file and account (UNIQUE sha256 — a concurrent
    # identical import fails atomically).
    (18, [
        _begin,
        "DROP TRIGGER IF EXISTS sh_imports_no_delete",
        "DROP TRIGGER IF EXISTS sh_transactions_no_delete",
        """CREATE TABLE IF NOT EXISTS sh_import_removals (
               id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, import_id TEXT NOT NULL,
               sha256 TEXT NOT NULL, filename TEXT, transactions INTEGER NOT NULL, ts TEXT NOT NULL, actor TEXT NOT NULL,
               reason TEXT)""",
        """CREATE TRIGGER IF NOT EXISTS sh_import_removals_no_update BEFORE UPDATE ON sh_import_removals
               BEGIN SELECT RAISE(ABORT, 'sh_import_removals is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS sh_import_removals_no_delete BEFORE DELETE ON sh_import_removals
               BEGIN SELECT RAISE(ABORT, 'sh_import_removals is insert-only'); END""",
        """CREATE UNIQUE INDEX IF NOT EXISTS ux_sh_imports_sha ON sh_imports(account_id, sha256)""",
    ]),
    # v19 (SPEC §19.16, stage 9c — the real Trade Republic export 2026-10-18): sh_transactions is rebuilt (SQLite
    # cannot alter a CHECK) with the kinds tax_refund / staking / pending_buy / corporate_action and the columns
    # ext_id (the broker's transaction id — the dedupe key across overlapping exports, unique per account),
    # original_amount / original_currency / fx_rate (as reported) and note. Every row is copied unchanged; the
    # update trigger and the index are recreated. NO counterparty / IBAN / payment-reference column exists — those
    # fields are discarded at import (user decision 2026-10-18).
    (19, [
        _begin,
        """CREATE TABLE sh_transactions_v19 (
               id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, import_id TEXT NOT NULL, row_no INTEGER,
               hash TEXT NOT NULL, date TEXT NOT NULL,
               kind TEXT NOT NULL CHECK (kind IN ('buy', 'sell', 'dividend', 'interest', 'withholding', 'fee', 'deposit',
                                                  'withdrawal', 'tax_refund', 'staking', 'pending_buy',
                                                  'corporate_action')),
               key TEXT, symbol TEXT, isin TEXT, name TEXT, asset_class TEXT, qty REAL, price REAL,
               currency TEXT NOT NULL, gross REAL NOT NULL, fee REAL NOT NULL, tax REAL NOT NULL, net REAL NOT NULL,
               raw_type TEXT, ext_id TEXT, original_amount REAL, original_currency TEXT, fx_rate REAL, note TEXT,
               UNIQUE (account_id, hash))""",
        """INSERT INTO sh_transactions_v19 (id, account_id, import_id, row_no, hash, date, kind, key, symbol, isin, name,
               asset_class, qty, price, currency, gross, fee, tax, net, raw_type)
               SELECT id, account_id, import_id, row_no, hash, date, kind, key, symbol, isin, name, asset_class, qty,
                      price, currency, gross, fee, tax, net, raw_type FROM sh_transactions""",
        "DROP TABLE sh_transactions",
        "ALTER TABLE sh_transactions_v19 RENAME TO sh_transactions",
        """CREATE TRIGGER IF NOT EXISTS sh_transactions_no_update BEFORE UPDATE ON sh_transactions
               BEGIN SELECT RAISE(ABORT, 'sh_transactions is insert-only'); END""",
        """CREATE INDEX IF NOT EXISTS ix_sh_transactions_account ON sh_transactions(account_id, date)""",
        """CREATE UNIQUE INDEX IF NOT EXISTS ux_sh_transactions_ext ON sh_transactions(account_id, ext_id)
               WHERE ext_id IS NOT NULL""",
    ]),
    # v20 (SPEC §19.16 review fixes 2026-10-18): the statement's own timestamp (``ts``, sortable, null when the export
    # has no time) and the position in the parsed file (``seq``) — the same-day FIFO order; ``row_no`` stays the
    # original file line for messages only. Older rows keep null (their order is unchanged).
    (20, [
        _begin,
        "ALTER TABLE sh_transactions ADD COLUMN ts TEXT",
        "ALTER TABLE sh_transactions ADD COLUMN seq INTEGER",
    ]),
    # v21 (SPEC §20.10, user decision 2026-10-18): the user's ISIN → US ticker mapping (global, insert-only; the
    # newest row per ISIN wins, a removal is a row with symbol NULL; audited)
    (21, [
        _begin,
        """CREATE TABLE IF NOT EXISTS sh_symbol_maps (
               id INTEGER PRIMARY KEY AUTOINCREMENT, isin TEXT NOT NULL, symbol TEXT, exchange TEXT, asset_name TEXT,
               confirmed_us_listing INTEGER NOT NULL, ts TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT)""",
        """CREATE TRIGGER IF NOT EXISTS sh_symbol_maps_no_update BEFORE UPDATE ON sh_symbol_maps
               BEGIN SELECT RAISE(ABORT, 'sh_symbol_maps is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS sh_symbol_maps_no_delete BEFORE DELETE ON sh_symbol_maps
               BEGIN SELECT RAISE(ABORT, 'sh_symbol_maps is insert-only'); END""",
        """CREATE INDEX IF NOT EXISTS ix_sh_symbol_maps_isin ON sh_symbol_maps(isin, id)""",
    ]),
    # v22 (SPEC §20.11, stage 9d): a mapping names its price source (alpaca_us | eodhd), the listing (ticker or
    # CODE.EXCHANGE) and the listing's currency; older rows are Alpaca US tickers in USD
    (22, [
        _begin,
        "ALTER TABLE sh_symbol_maps ADD COLUMN price_source TEXT",
        "ALTER TABLE sh_symbol_maps ADD COLUMN listing TEXT",
        "ALTER TABLE sh_symbol_maps ADD COLUMN currency TEXT",
    ]),
    # v23 (SPEC §18.14, stage 8b 2026-10-19): the Binance Spot demo broker account — br_accounts is rebuilt (SQLite
    # cannot alter a CHECK) with binance-demo / binance_demo added; every row is copied unchanged and the insert-only
    # triggers are recreated (DROP TABLE removes them; it fires no row trigger).
    (23, [
        _begin,
        """CREATE TABLE br_accounts_v23 (
               id TEXT PRIMARY KEY CHECK (id IN ('alpaca-paper', 'kraken-demo', 'binance-demo')),
               broker TEXT NOT NULL CHECK (broker IN ('alpaca_paper', 'kraken_demo', 'binance_demo')),
               label TEXT NOT NULL, params_json TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL)""",
        """INSERT INTO br_accounts_v23 (id, broker, label, params_json, created_at, created_by)
               SELECT id, broker, label, params_json, created_at, created_by FROM br_accounts""",
        "DROP TABLE br_accounts",
        "ALTER TABLE br_accounts_v23 RENAME TO br_accounts",
        """CREATE TRIGGER IF NOT EXISTS br_accounts_no_update BEFORE UPDATE ON br_accounts
               BEGIN SELECT RAISE(ABORT, 'br_accounts is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS br_accounts_no_delete BEFORE DELETE ON br_accounts
               BEGIN SELECT RAISE(ABORT, 'br_accounts is insert-only'); END""",
    ]),
    # v24 (fix1023 round 2, 2026-10-23): provenance of a Shadow symbol mapping — confirmed_via 'confirm_flag' (the
    # request carried confirm_us_listing / confirm_listing: true; every row before v24 reads as that) | 'dialog' (saved
    # from the app's summary dialog without a flag). Additive column; no data is rewritten.
    (24, [
        _begin,
        "ALTER TABLE sh_symbol_maps ADD COLUMN confirmed_via TEXT",
    ]),
    # v25 (SPEC §22.3, stage 10 S10-C, 2026-10-24): the Council of AIs as a platform strategy — insert-only tables for
    # its params (+ the paper-council broker-params override, D9), runs + results, agent signals, decisions, per-run
    # positions, submit-time executions and AI calls; `legacy` / `old_id` mark rows imported from the old engine (D5).
    # A consistent pre-v25 backup is written first (``backup_before``), part of the rollback (§22.8).
    (25, [
        _begin,
        """CREATE TABLE IF NOT EXISTS cl_params (
               version INTEGER PRIMARY KEY, params_json TEXT NOT NULL, created_at TEXT NOT NULL, actor TEXT NOT NULL,
               reason TEXT, source TEXT NOT NULL, legacy INTEGER NOT NULL DEFAULT 0)""",
        """CREATE TRIGGER IF NOT EXISTS cl_params_no_update BEFORE UPDATE ON cl_params
               BEGIN SELECT RAISE(ABORT, 'cl_params is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS cl_params_no_delete BEFORE DELETE ON cl_params
               BEGIN SELECT RAISE(ABORT, 'cl_params is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS cl_broker_params (
               version INTEGER PRIMARY KEY, params_json TEXT NOT NULL, created_at TEXT NOT NULL,
               actor TEXT NOT NULL, reason TEXT)""",
        """CREATE TRIGGER IF NOT EXISTS cl_broker_params_no_update BEFORE UPDATE ON cl_broker_params
               BEGIN SELECT RAISE(ABORT, 'cl_broker_params is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS cl_broker_params_no_delete BEFORE DELETE ON cl_broker_params
               BEGIN SELECT RAISE(ABORT, 'cl_broker_params is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS cl_runs (
               id INTEGER PRIMARY KEY AUTOINCREMENT, old_id INTEGER,
               trigger TEXT NOT NULL CHECK (trigger IN ('scheduled', 'manual', 'startup')), started_at TEXT NOT NULL,
               params_version INTEGER, session_state TEXT, orders_enabled INTEGER, sandbox INTEGER,
               legacy INTEGER NOT NULL DEFAULT 0)""",
        """CREATE TRIGGER IF NOT EXISTS cl_runs_no_update BEFORE UPDATE ON cl_runs
               BEGIN SELECT RAISE(ABORT, 'cl_runs is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS cl_runs_no_delete BEFORE DELETE ON cl_runs
               BEGIN SELECT RAISE(ABORT, 'cl_runs is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS cl_run_results (
               run_id INTEGER PRIMARY KEY, finished_at TEXT NOT NULL,
               status TEXT NOT NULL CHECK (status IN ('ok', 'partial', 'error')),
               judge_mode TEXT, cost_usd REAL NOT NULL DEFAULT 0, summary_json TEXT)""",
        """CREATE TRIGGER IF NOT EXISTS cl_run_results_no_update BEFORE UPDATE ON cl_run_results
               BEGIN SELECT RAISE(ABORT, 'cl_run_results is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS cl_run_results_no_delete BEFORE DELETE ON cl_run_results
               BEGIN SELECT RAISE(ABORT, 'cl_run_results is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS cl_signals (
               run_id INTEGER NOT NULL, symbol TEXT NOT NULL, agent TEXT NOT NULL, score REAL, confidence REAL,
               summary TEXT, details_json TEXT, source TEXT NOT NULL, ai_generated INTEGER NOT NULL DEFAULT 0,
               provenance_json TEXT, legacy INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (run_id, symbol, agent))""",
        """CREATE TRIGGER IF NOT EXISTS cl_signals_no_update BEFORE UPDATE ON cl_signals
               BEGIN SELECT RAISE(ABORT, 'cl_signals is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS cl_signals_no_delete BEFORE DELETE ON cl_signals
               BEGIN SELECT RAISE(ABORT, 'cl_signals is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS cl_decisions (
               id INTEGER PRIMARY KEY AUTOINCREMENT, old_id INTEGER, run_id INTEGER NOT NULL, symbol TEXT NOT NULL,
               rule_score REAL, agreement REAL, rule_action TEXT, judge_source TEXT NOT NULL, action TEXT,
               confidence REAL, rationale TEXT, model TEXT, cost_share_usd REAL, unavailable_reason TEXT,
               target_weight REAL, sizing_note TEXT, ai_generated INTEGER NOT NULL DEFAULT 0, provenance_json TEXT,
               legacy INTEGER NOT NULL DEFAULT 0)""",
        """CREATE TRIGGER IF NOT EXISTS cl_decisions_no_update BEFORE UPDATE ON cl_decisions
               BEGIN SELECT RAISE(ABORT, 'cl_decisions is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS cl_decisions_no_delete BEFORE DELETE ON cl_decisions
               BEGIN SELECT RAISE(ABORT, 'cl_decisions is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS cl_positions (
               run_id INTEGER NOT NULL, symbol TEXT NOT NULL, qty REAL, avg_price REAL, mark REAL, weight REAL,
               subledger_equity REAL, PRIMARY KEY (run_id, symbol))""",
        """CREATE TRIGGER IF NOT EXISTS cl_positions_no_update BEFORE UPDATE ON cl_positions
               BEGIN SELECT RAISE(ABORT, 'cl_positions is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS cl_positions_no_delete BEFORE DELETE ON cl_positions
               BEGIN SELECT RAISE(ABORT, 'cl_positions is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS cl_executions (
               id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id INTEGER, client_order_id TEXT,
               outcome TEXT NOT NULL CHECK (outcome IN ('submitted', 'rejected', 'not_executed', 'skipped')),
               reason TEXT, at TEXT NOT NULL)""",
        """CREATE TRIGGER IF NOT EXISTS cl_executions_no_update BEFORE UPDATE ON cl_executions
               BEGIN SELECT RAISE(ABORT, 'cl_executions is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS cl_executions_no_delete BEFORE DELETE ON cl_executions
               BEGIN SELECT RAISE(ABORT, 'cl_executions is insert-only'); END""",
        """CREATE TABLE IF NOT EXISTS cl_ai_calls (
               id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER,
               feature TEXT NOT NULL CHECK (feature IN ('sentiment', 'judge', 'judge_premium')), model TEXT,
               request_hash TEXT, prompt_tokens INTEGER, completion_tokens INTEGER, cost_usd REAL NOT NULL DEFAULT 0,
               status TEXT NOT NULL CHECK (status IN ('ok', 'error', 'budget', 'no_key', 'invalid')),
               provenance_json TEXT, at TEXT NOT NULL)""",
        """CREATE TRIGGER IF NOT EXISTS cl_ai_calls_no_update BEFORE UPDATE ON cl_ai_calls
               BEGIN SELECT RAISE(ABORT, 'cl_ai_calls is insert-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS cl_ai_calls_no_delete BEFORE DELETE ON cl_ai_calls
               BEGIN SELECT RAISE(ABORT, 'cl_ai_calls is insert-only'); END""",
        "CREATE INDEX IF NOT EXISTS ix_cl_decisions_run ON cl_decisions(run_id, symbol)",
        "CREATE INDEX IF NOT EXISTS ix_cl_decisions_symbol ON cl_decisions(symbol, id)",
        "CREATE INDEX IF NOT EXISTS ix_cl_executions_decision ON cl_executions(decision_id)",
        "CREATE INDEX IF NOT EXISTS ix_cl_ai_calls_run ON cl_ai_calls(run_id)",
        "CREATE INDEX IF NOT EXISTS ix_cl_runs_started ON cl_runs(started_at)",
    ]),
]
SCHEMA_VERSION = MIGRATIONS[-1][0]
TABLES = ("meta", "audit", "data_sources", "bars", "strategies", "strategy_versions", "strategy_transitions",
          "ext_observations", "corporate_actions", "seam_checks", "volumes", "backtest_runs",
          "strategy_families", "holdout_events", "validation_exceptions", "strategy_logic",
          "run_checkpoints", "run_explanations",
          "studio_sessions", "studio_messages", "studio_backtests", "memory_facts",
          "studio_draft_saves", "papers", "paper_extractions", "paper_confirmations", "paper_replications", "committee_runs", "committee_messages", "committee_events",
          "loop_runs", "loop_variants", "loop_events", "loop_ai_messages",
          "pf_accounts", "pf_settings", "pf_allocations", "pf_migrations", "pa_orders", "pa_positions",
          "pa_cash_ledger", "pa_equity", "pa_events", "pa_meta", "oms_orders", "oms_order_events",
          "sl_entries", "sl_snapshots", "rt_decisions", "rt_executions", "incubation_runs",
          "incubation_events", "br_accounts", "br_connections", "br_routes", "br_reconciliations",
          "br_resolutions", "br_live_guard", "br_imports", "br_fill_marks",
          "mon_decay_checks", "mon_decay_events", "mon_alerts", "mon_alert_acks", "mon_reports", "mon_commentaries", "sh_accounts", "sh_profiles", "sh_imports", "sh_transactions", "sh_classifications", "sh_valuations", "sh_import_removals", "sh_symbol_maps",
          "cl_params", "cl_broker_params", "cl_runs", "cl_run_results", "cl_signals", "cl_decisions", "cl_positions",
          "cl_executions", "cl_ai_calls")

_lock = threading.Lock()
_path: Optional[str] = None          # open database
_wanted: Optional[str] = None        # path init() was asked for (reopen target)
_last_attempt = 0.0
_last_error = ""


def default_path() -> str:
    """HedgeFund/backend/data/hedgefund.db — data/sandbox/hedgefund.db in sandbox mode (sandbox_profile.py)."""
    import sandbox_profile
    return sandbox_profile.db_file(DB_FILENAME)


def db_path() -> Optional[str]:
    """Path of the open database, or None while it is not open."""
    return _path


def is_open() -> bool:
    return _path is not None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _open(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Brings the schema to SCHEMA_VERSION; safe to call any number of times."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > SCHEMA_VERSION:
        raise sqlite3.DatabaseError(f"schema v{current} is newer than this code (v{SCHEMA_VERSION})")
    for version, statements in MIGRATIONS:
        if version <= current:
            continue
        try:
            for sql in statements:
                if callable(sql):
                    sql(conn)
                else:
                    conn.execute(sql)
            conn.execute(f"PRAGMA user_version = {int(version)}")
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        current = version
    return current


def _open_and_migrate(target: str) -> int:
    conn = None
    try:
        # Only open + pragmas + quick_check are classified as corruption: an
        # error from migrate() must never get a good file quarantined.
        conn = _open(target)
        conn.execute("PRAGMA journal_mode=WAL")
        check = conn.execute("PRAGMA quick_check").fetchone()
    except sqlite3.OperationalError:
        if conn is not None:
            conn.close()
        raise  # locked / busy / cannot open: not corruption
    except sqlite3.DatabaseError as ex:  # "file is not a database", malformed image
        if conn is not None:
            conn.close()
        raise CorruptDatabase(str(ex)) from ex
    try:
        if not check or check[0] != "ok":
            raise CorruptDatabase(f"quick_check: {check[0] if check else 'no result'}")
        backup_before(conn, target, 23)
        backup_before(conn, target, 25)
        return migrate(conn)
    finally:
        conn.close()


def backup_before(conn: sqlite3.Connection, target: str, version: int, now: Optional[datetime] = None) -> Optional[str]:
    """A consistent copy (sqlite backup API, WAL included) of an EXISTING database before the migration to ``version``
    (v23 rebuilds br_accounts, SPEC §18.14; v25 adds the Council tables, SPEC §22.3): ``data/backups/hedgefund.db.pre-v<version>-<UTC stamp>`` next to the
    database — written once (an existing pre-v<version> copy is kept, no second one) and only when the file is at a
    schema 1 … version-1. A failed backup raises (the migration does not run — no silent risk). Returns the path."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if not 1 <= current < version or target == ":memory:":
        return None
    folder = os.path.join(os.path.dirname(os.path.abspath(target)), "backups")
    prefix = f"{os.path.basename(target)}.pre-v{version}-"
    if os.path.isdir(folder) and any(n.startswith(prefix) for n in os.listdir(folder)):
        return None
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    dest = os.path.join(folder, prefix + stamp)
    os.makedirs(folder, exist_ok=True)
    out = sqlite3.connect(dest + ".tmp")
    try:
        conn.backup(out)
    except BaseException:
        out.close()
        if os.path.exists(dest + ".tmp"):
            os.remove(dest + ".tmp")      # our own incomplete copy only
        raise
    out.close()
    os.replace(dest + ".tmp", dest)
    logger.info("hedgefund.db: backup before the v%d migration written to %s (schema v%d)", version, dest, current)
    return dest


def _quarantine(target: str, now: Optional[datetime] = None) -> str:
    """Renames a corrupt database (and its -wal/-shm) out of the way."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    moved = f"{target}.corrupt-{stamp}"
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(target + suffix):
            os.replace(target + suffix, moved + suffix)
    return moved


def init(path: Optional[str] = None) -> int:
    """Opens (creates) the database, sets WAL, migrates; returns the schema
    version. A corrupt file is quarantined and recreated. Any other failure
    raises StorageUnavailable (the store stays closed; later calls retry)."""
    global _path, _wanted, _last_attempt, _last_error
    with _lock:
        target = os.path.abspath(path or default_path())
        _wanted = target
        _last_attempt = _time.monotonic()
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            try:
                version = _open_and_migrate(target)
            except CorruptDatabase as ex:
                moved = _quarantine(target)
                logger.error("hedgefund.db %s is corrupt (%s) — renamed to %s, starting with a fresh database",
                             target, ex, moved)
                version = _open_and_migrate(target)
            _path = target
            _last_error = ""
            logger.info("hedgefund.db ready: %s (schema v%d)", target, version)
            return version
        except Exception as ex:
            _path = None
            _last_error = str(ex) or type(ex).__name__
            logger.error("hedgefund.db unavailable (%s): %s", target, _last_error)
            raise StorageUnavailable("it could not be opened", detail=_last_error) from ex


def close() -> None:
    """Closes the store and forgets the path (tests; no reopen afterwards)."""
    global _path, _wanted
    with _lock:
        _path = None
        _wanted = None


def _require_path() -> str:
    """The open path; retries a failed init at most every REOPEN_INTERVAL_SECONDS."""
    if _path is not None:
        return _path
    if _wanted is None:
        raise StorageUnavailable("it has not been opened")
    if _time.monotonic() - _last_attempt < REOPEN_INTERVAL_SECONDS:
        raise StorageUnavailable("it could not be opened", detail=_last_error)
    init(_wanted)
    return _path


@contextmanager
def _connect():
    """A connection with commit/rollback; sqlite errors -> StorageUnavailable."""
    path = _require_path()
    try:
        conn = _open(path)
    except sqlite3.Error as ex:
        raise StorageUnavailable("it could not be opened", detail=str(ex)) from ex
    try:
        yield conn
        conn.commit()
    except sqlite3.Error as ex:
        conn.rollback()
        logger.warning("hedgefund.db operation failed: %s", ex)
        raise StorageUnavailable("a database operation failed", detail=str(ex)) from ex
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def storage_info() -> dict:
    """{path, size_bytes, schema_version, rows: {table: n}} — raises StorageUnavailable."""
    with _connect() as conn:
        rows = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    path = _path
    try:
        size = os.path.getsize(path)
    except (OSError, TypeError) as ex:
        raise StorageUnavailable("its file could not be read", detail=str(ex)) from ex
    return {"path": path, "size_bytes": size, "schema_version": version, "rows": rows}


# --- validation helpers ------------------------------------------------------------

def to_utc_iso(value, field: str = "time") -> str:
    """datetime (naive = UTC) | date (00:00 UTC) | ISO string -> 'YYYY-MM-DDTHH:MM:SS+00:00'.
    Raises ValueError for anything unparsable (never substitutes "now")."""
    try:
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, date):
            dt = datetime(value.year, value.month, value.day)
        elif isinstance(value, str) and value.strip():
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        else:
            raise TypeError(type(value).__name__)
    except (TypeError, ValueError) as ex:
        raise ValueError(f"{field}: not a valid time ({value!r})") from ex
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _text(value, field: str, max_len: int = 64) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > max_len:
        raise ValueError(f"{field}: expected a non-empty string of at most {max_len} characters")
    return value.strip()


def _number(value, field: str, required: bool):
    if value is None:
        if required:
            raise ValueError(f"{field}: required")
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field}: not a number")
    try:
        f = float(value)
    except (TypeError, ValueError) as ex:
        raise ValueError(f"{field}: not a number ({value!r})") from ex
    if not math.isfinite(f):
        raise ValueError(f"{field}: not finite")
    return f


def _dumps(value) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _loads(text):
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        # stored by an older/foreign writer: return the raw text, never a made-up value
        return text


# --- meta -----------------------------------------------------------------------------

def meta_get(key: str) -> Optional[str]:
    """The stored value or None when the key does not exist."""
    key = _text(key, "key", 128)
    with _connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def meta_set(key: str, value) -> None:
    """Stores str(value); ``None`` deletes the key."""
    key = _text(key, "key", 128)
    with _connect() as conn:
        if value is None:
            conn.execute("DELETE FROM meta WHERE key = ?", (key,))
        else:
            conn.execute("INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                         (key, str(value), utc_now_iso()))


def meta_keys(prefix: str) -> List[str]:
    """Keys starting with ``prefix`` (sorted); [] when none."""
    prefix = _text(prefix, "prefix", 128)
    like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    with _connect() as conn:
        rows = conn.execute("SELECT key FROM meta WHERE key LIKE ? ESCAPE '\\' ORDER BY key", (like,)).fetchall()
    return [r["key"] for r in rows]


# --- audit ----------------------------------------------------------------------------

AUDIT_ACTORS = ("user", "engine", "system", "data")


def add_audit(actor: str, action: str, detail=None) -> int:
    """Appends one audit row; returns its id. ``detail``: JSON-serialisable
    (never secrets). Raises StorageUnavailable — callers that must not act
    without an audit row refuse the action then."""
    if actor not in AUDIT_ACTORS:
        raise ValueError(f"actor: one of {', '.join(AUDIT_ACTORS)}")
    action = _text(action, "action", 64)
    with _connect() as conn:
        cur = conn.execute("INSERT INTO audit (ts, actor, action, detail_json) VALUES (?, ?, ?, ?)",
                           (utc_now_iso(), actor, action, _dumps(detail)))
        return int(cur.lastrowid)


def list_audit(limit: int = 50, action: Optional[str] = None) -> List[dict]:
    """Newest first: [{id, ts, actor, action, detail}]."""
    limit = max(1, min(int(limit), 1000))
    sql, args = "SELECT id, ts, actor, action, detail_json FROM audit", []
    if action is not None:
        sql += " WHERE action = ?"
        args.append(_text(action, "action", 64))
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with _connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [{"id": r["id"], "ts": r["ts"], "actor": r["actor"], "action": r["action"],
             "detail": _loads(r["detail_json"])} for r in rows]


# --- data sources -----------------------------------------------------------------------

LICENCE_TERMS = ("personal_use", "commercial_ok", "unknown")
_DS_COLUMNS = ("data_type", "source", "licence", "licence_note", "status", "last_ok_at", "last_error_at",
               "message", "updated_at")


def _ds_row(row) -> dict:
    return {c: row[c] for c in _DS_COLUMNS}


def data_source_get(data_type: str) -> Optional[dict]:
    """{data_type, source, licence, licence_note, status, last_ok_at,
    last_error_at, message, updated_at} or None when the type is not configured."""
    data_type = _text(data_type, "data_type")
    with _connect() as conn:
        row = conn.execute(f"SELECT {', '.join(_DS_COLUMNS)} FROM data_sources WHERE data_type = ?",
                           (data_type,)).fetchone()
    return _ds_row(row) if row else None


def data_sources_list() -> List[dict]:
    """All configured data types, ordered by data_type."""
    with _connect() as conn:
        rows = conn.execute(f"SELECT {', '.join(_DS_COLUMNS)} FROM data_sources ORDER BY data_type").fetchall()
    return [_ds_row(r) for r in rows]


def data_source_set(data_type: str, source: str, *, licence: Optional[str] = None,
                    licence_note: Optional[str] = None) -> dict:
    """Configures THE source of a data type (one per type, no chains). A
    changed source clears the status (it was about the old source); the same
    source keeps it. Returns the stored row."""
    data_type = _text(data_type, "data_type")
    source = _text(source, "source")
    if licence is not None and licence not in LICENCE_TERMS:
        raise ValueError(f"licence: one of {', '.join(LICENCE_TERMS)}")
    if licence_note is not None and not isinstance(licence_note, str):
        raise ValueError("licence_note: expected a string")
    now = utc_now_iso()
    with _connect() as conn:
        row = conn.execute("SELECT source FROM data_sources WHERE data_type = ?", (data_type,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO data_sources (data_type, source, licence, licence_note, updated_at) "
                         "VALUES (?, ?, ?, ?, ?)", (data_type, source, licence, licence_note, now))
        elif row["source"] != source:
            conn.execute("UPDATE data_sources SET source = ?, licence = ?, licence_note = ?, status = NULL, "
                         "last_ok_at = NULL, last_error_at = NULL, message = NULL, updated_at = ? "
                         "WHERE data_type = ?", (source, licence, licence_note, now, data_type))
        elif licence is not None or licence_note is not None:
            # same source: only the given licence fields change — None never clears a stored licence
            conn.execute("UPDATE data_sources SET licence = COALESCE(?, licence), "
                         "licence_note = COALESCE(?, licence_note), updated_at = ? "
                         "WHERE data_type = ?", (licence, licence_note, now, data_type))
    return data_source_get(data_type)


def data_source_status_update(data_type: str, ok: bool, message: Optional[str] = None,
                              at=None) -> Optional[dict]:
    """Records the result of the latest use/test of a data type's source:
    status 'ok' (sets last_ok_at) or 'error' (sets last_error_at), plus the
    user-facing ``message``. Returns the updated row, None when the data type
    is not configured."""
    data_type = _text(data_type, "data_type")
    if not isinstance(ok, bool):
        raise ValueError("ok: expected a bool")
    if message is not None and not isinstance(message, str):
        raise ValueError("message: expected a string")
    ts = to_utc_iso(at, "at") if at is not None else utc_now_iso()
    column = "last_ok_at" if ok else "last_error_at"
    with _connect() as conn:
        cur = conn.execute(f"UPDATE data_sources SET status = ?, {column} = ?, message = ?, updated_at = ? "
                           "WHERE data_type = ?",
                           ("ok" if ok else "error", ts, (message or "")[:1000] or None, utc_now_iso(), data_type))
        updated = cur.rowcount
    return data_source_get(data_type) if updated else None


# --- bars (cache of strategy_platform/data/cache.py) --------------------------------------

BAR_COLUMNS = ("symbol", "timeframe", "ts", "open", "high", "low", "close", "volume", "vwap", "trade_count",
               "source", "known_at", "fetched_at", "adjustment")
UPSERT_BARS_MAX_ROWS = 50000
# "all" = adjusted for splits AND dividends (signals), "split" = splits only (fills,
# valuation, volume limit) — SPEC §8.1. A row without the field is "all": that is the
# column's schema default and what every stage-1 caller stored (no other meaning possible).
ADJUSTMENTS = ("all", "split")


def _adjustment(value, field: str = "adjustment") -> str:
    if value not in ADJUSTMENTS:
        raise ValueError(f"{field}: one of {', '.join(ADJUSTMENTS)}")
    return value


def _bar_tuple(row, i: int) -> tuple:
    if not isinstance(row, dict):
        raise ValueError(f"bars[{i}]: expected a dict")
    unknown = set(row) - set(BAR_COLUMNS)
    if unknown:
        raise ValueError(f"bars[{i}]: unknown fields {sorted(unknown)}")
    trade_count = row.get("trade_count")
    if trade_count is not None:
        tc = _number(trade_count, f"bars[{i}].trade_count", False)
        if tc != int(tc) or tc < 0:
            raise ValueError(f"bars[{i}].trade_count: expected a non-negative integer")
        trade_count = int(tc)
    return (
        _text(row.get("symbol"), f"bars[{i}].symbol", 32).upper(),
        _text(row.get("timeframe"), f"bars[{i}].timeframe", 16),
        to_utc_iso(row.get("ts"), f"bars[{i}].ts"),
        _number(row.get("open"), f"bars[{i}].open", True),
        _number(row.get("high"), f"bars[{i}].high", True),
        _number(row.get("low"), f"bars[{i}].low", True),
        _number(row.get("close"), f"bars[{i}].close", True),
        _number(row.get("volume"), f"bars[{i}].volume", False),
        _number(row.get("vwap"), f"bars[{i}].vwap", False),
        trade_count,
        _text(row.get("source"), f"bars[{i}].source", 32),
        to_utc_iso(row.get("known_at"), f"bars[{i}].known_at"),
        to_utc_iso(row.get("fetched_at"), f"bars[{i}].fetched_at"),
        _adjustment(row.get("adjustment", "all"), f"bars[{i}].adjustment"),
    )


def upsert_bars(rows: Iterable[dict]) -> int:
    """Inserts or replaces bars (key symbol, timeframe, ts, source, adjustment); all rows
    in one transaction — one invalid row -> ValueError, nothing written.
    Each row: dict with BAR_COLUMNS (volume, vwap, trade_count may be None;
    times as datetime or ISO string, normalised to UTC). Prices are stored as
    given (plausibility is data/validation.py's job), but must be finite
    numbers. Returns the number of rows written."""
    tuples = [_bar_tuple(r, i) for i, r in enumerate(rows)]
    if len(tuples) > UPSERT_BARS_MAX_ROWS:
        raise ValueError(f"at most {UPSERT_BARS_MAX_ROWS} bars per call")
    if not tuples:
        return 0
    with _connect() as conn:
        conn.executemany(f"INSERT OR REPLACE INTO bars ({', '.join(BAR_COLUMNS)}) "
                         f"VALUES ({', '.join('?' * len(BAR_COLUMNS))})", tuples)
    return len(tuples)


def query_bars(symbol: str, timeframe: str, start, end, source: str, *, known_by=None,
               adjustment: str = "all") -> List[dict]:
    """Bars of ONE source and ONE adjustment series, oldest first, with
    start <= ts <= end (either bound may be None = open). ``known_by``
    (optional): only bars with known_at <= known_by (point-in-time). Each bar
    is a dict with BAR_COLUMNS. [] when nothing is stored — not an error."""
    symbol = _text(symbol, "symbol", 32).upper()
    timeframe = _text(timeframe, "timeframe", 16)
    source = _text(source, "source", 32)
    adjustment = _adjustment(adjustment)
    sql = (f"SELECT {', '.join(BAR_COLUMNS)} FROM bars WHERE symbol = ? AND timeframe = ? AND source = ? "
           "AND adjustment = ?")
    args: list = [symbol, timeframe, source, adjustment]
    if start is not None:
        sql += " AND ts >= ?"
        args.append(to_utc_iso(start, "start"))
    if end is not None:
        sql += " AND ts <= ?"
        args.append(to_utc_iso(end, "end"))
    if known_by is not None:
        sql += " AND known_at <= ?"
        args.append(to_utc_iso(known_by, "known_by"))
    sql += " ORDER BY ts"
    with _connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [{c: r[c] for c in BAR_COLUMNS} for r in rows]


def latest_bar_ts(symbol: str, timeframe: str, source: str, *, adjustment: str = "all") -> Optional[str]:
    """ts of the newest stored bar of this source/adjustment, None when none is stored."""
    symbol = _text(symbol, "symbol", 32).upper()
    timeframe = _text(timeframe, "timeframe", 16)
    source = _text(source, "source", 32)
    adjustment = _adjustment(adjustment)
    with _connect() as conn:
        row = conn.execute("SELECT MAX(ts) AS ts FROM bars WHERE symbol = ? AND timeframe = ? AND source = ? "
                           "AND adjustment = ?", (symbol, timeframe, source, adjustment)).fetchone()
    return row["ts"] if row and row["ts"] is not None else None


# --- external feature observations (SPEC §7.8) ------------------------------------------------

EXT_COLUMNS = ("series_id", "date", "value", "realtime_start", "source", "fetched_at")


def _iso_date(value, field: str) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10]).isoformat()
        except ValueError:
            pass
    raise ValueError(f"{field}: not a valid date ({value!r})")


def upsert_ext_observations(rows: Iterable[dict]) -> int:
    """Stores first-release observations (key series_id, date, source) in ONE
    transaction; an invalid row -> ValueError, nothing written."""
    tuples = []
    for i, r in enumerate(rows):
        if not isinstance(r, dict) or set(r) - set(EXT_COLUMNS):
            raise ValueError(f"ext_observations[{i}]: expected a dict with {EXT_COLUMNS}")
        tuples.append((_text(r.get("series_id"), f"ext_observations[{i}].series_id", 64),
                       _iso_date(r.get("date"), f"ext_observations[{i}].date"),
                       _number(r.get("value"), f"ext_observations[{i}].value", True),
                       _iso_date(r.get("realtime_start"), f"ext_observations[{i}].realtime_start"),
                       _text(r.get("source"), f"ext_observations[{i}].source", 32),
                       to_utc_iso(r.get("fetched_at"), f"ext_observations[{i}].fetched_at")))
    if not tuples:
        return 0
    with _connect() as conn:
        conn.executemany(f"INSERT OR REPLACE INTO ext_observations ({', '.join(EXT_COLUMNS)}) "
                         f"VALUES ({', '.join('?' * len(EXT_COLUMNS))})", tuples)
    return len(tuples)


def query_ext_observations(series_id: str, source: str, start=None, end=None) -> List[dict]:
    """Observations of one series/source with start <= date <= end, oldest first; [] when none."""
    sql = f"SELECT {', '.join(EXT_COLUMNS)} FROM ext_observations WHERE series_id = ? AND source = ?"
    args: list = [_text(series_id, "series_id", 64), _text(source, "source", 32)]
    if start is not None:
        sql += " AND date >= ?"
        args.append(_iso_date(start, "start"))
    if end is not None:
        sql += " AND date <= ?"
        args.append(_iso_date(end, "end"))
    sql += " ORDER BY date"
    with _connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [{c: r[c] for c in EXT_COLUMNS} for r in rows]


# --- corporate actions (SPEC §8.1) -------------------------------------------------------------

CA_COLUMNS = ("symbol", "kind", "ex_date", "cash_amount", "split_ratio", "pay_date", "source", "known_at",
              "fetched_at")
CA_KINDS = ("cash_dividend", "forward_split", "reverse_split")


def _ca_tuple(r, i: int) -> tuple:
    if not isinstance(r, dict):
        raise ValueError(f"corporate_actions[{i}]: expected a dict")
    unknown = set(r) - set(CA_COLUMNS)
    if unknown:
        raise ValueError(f"corporate_actions[{i}]: unknown fields {sorted(unknown)}")
    kind = r.get("kind")
    if kind not in CA_KINDS:
        raise ValueError(f"corporate_actions[{i}].kind: one of {', '.join(CA_KINDS)}")
    cash = _number(r.get("cash_amount"), f"corporate_actions[{i}].cash_amount", kind == "cash_dividend")
    ratio = _number(r.get("split_ratio"), f"corporate_actions[{i}].split_ratio", kind != "cash_dividend")
    if ratio is not None and ratio <= 0:
        raise ValueError(f"corporate_actions[{i}].split_ratio: must be > 0")
    pay = r.get("pay_date")
    return (_text(r.get("symbol"), f"corporate_actions[{i}].symbol", 32).upper(), kind,
            _iso_date(r.get("ex_date"), f"corporate_actions[{i}].ex_date"), cash, ratio,
            _iso_date(pay, f"corporate_actions[{i}].pay_date") if pay is not None else None,
            _text(r.get("source"), f"corporate_actions[{i}].source", 32),
            to_utc_iso(r.get("known_at"), f"corporate_actions[{i}].known_at"),
            to_utc_iso(r.get("fetched_at"), f"corporate_actions[{i}].fetched_at"))


def replace_corporate_actions(symbol: str, source: str, start, end, rows: Iterable[dict]) -> int:
    """Replaces the stored actions of ONE symbol and source with ex_date in
    [start, end] (dates; either None = open) by ``rows`` in ONE transaction
    (an action the source no longer reports disappears). Every row must
    belong to that symbol/source and range, else ValueError and nothing is
    written. Returns the number of rows written."""
    symbol = _text(symbol, "symbol", 32).upper()
    source = _text(source, "source", 32)
    lo = _iso_date(start, "start") if start is not None else None
    hi = _iso_date(end, "end") if end is not None else None
    tuples = [_ca_tuple(r, i) for i, r in enumerate(rows)]
    for i, t in enumerate(tuples):
        if t[0] != symbol or t[6] != source or (lo and t[2] < lo) or (hi and t[2] > hi):
            raise ValueError(f"corporate_actions[{i}]: outside {symbol}/{source} [{lo or '…'}, {hi or '…'}]")
    sql, args = "DELETE FROM corporate_actions WHERE symbol = ? AND source = ?", [symbol, source]
    if lo:
        sql += " AND ex_date >= ?"
        args.append(lo)
    if hi:
        sql += " AND ex_date <= ?"
        args.append(hi)
    with _connect() as conn:
        conn.execute(sql, args)
        if tuples:
            conn.executemany(f"INSERT OR REPLACE INTO corporate_actions ({', '.join(CA_COLUMNS)}) "
                             f"VALUES ({', '.join('?' * len(CA_COLUMNS))})", tuples)
    return len(tuples)


def query_corporate_actions(symbol: str, source: str, start=None, end=None) -> List[dict]:
    """Actions of ONE symbol and source with start <= ex_date <= end (dates,
    None = open), oldest first; [] when none."""
    sql = f"SELECT {', '.join(CA_COLUMNS)} FROM corporate_actions WHERE symbol = ? AND source = ?"
    args: list = [_text(symbol, "symbol", 32).upper(), _text(source, "source", 32)]
    if start is not None:
        sql += " AND ex_date >= ?"
        args.append(_iso_date(start, "start"))
    if end is not None:
        sql += " AND ex_date <= ?"
        args.append(_iso_date(end, "end"))
    sql += " ORDER BY ex_date, kind"
    with _connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [{c: r[c] for c in CA_COLUMNS} for r in rows]


# --- consolidated volume (SPEC §8.8) -----------------------------------------------------------

VOLUME_COLUMNS = ("symbol", "date", "volume", "close_split", "source", "known_at", "fetched_at")


def _volume_tuple(r, i: int) -> tuple:
    if not isinstance(r, dict) or set(r) - set(VOLUME_COLUMNS):
        raise ValueError(f"volumes[{i}]: expected a dict with {VOLUME_COLUMNS}")
    vol = _number(r.get("volume"), f"volumes[{i}].volume", True)
    close = _number(r.get("close_split"), f"volumes[{i}].close_split", True)
    if vol < 0 or close <= 0:
        raise ValueError(f"volumes[{i}]: volume must be >= 0 and close_split > 0")
    return (_text(r.get("symbol"), f"volumes[{i}].symbol", 32).upper(), _iso_date(r.get("date"), f"volumes[{i}].date"),
            vol, close, _text(r.get("source"), f"volumes[{i}].source", 32),
            to_utc_iso(r.get("known_at"), f"volumes[{i}].known_at"),
            to_utc_iso(r.get("fetched_at"), f"volumes[{i}].fetched_at"))


def store_volumes(symbol: str, source: str, rows: Iterable[dict], *, replace_all: bool = False) -> int:
    """Upserts volume rows of ONE symbol/source in ONE transaction;
    ``replace_all`` first deletes every stored row of that symbol/source (a
    new split basis). A row of another symbol/source or invalid -> ValueError,
    nothing written. Returns the number of rows written."""
    symbol = _text(symbol, "symbol", 32).upper()
    source = _text(source, "source", 32)
    tuples = [_volume_tuple(r, i) for i, r in enumerate(rows)]
    if any(t[0] != symbol or t[4] != source for t in tuples):
        raise ValueError(f"volumes: every row must belong to {symbol}/{source}")
    with _connect() as conn:
        if replace_all:
            conn.execute("DELETE FROM volumes WHERE symbol = ? AND source = ?", (symbol, source))
        if tuples:
            conn.executemany(f"INSERT OR REPLACE INTO volumes ({', '.join(VOLUME_COLUMNS)}) "
                             f"VALUES ({', '.join('?' * len(VOLUME_COLUMNS))})", tuples)
    return len(tuples)


def query_volumes(symbol: str, source: str, start=None, end=None, *, known_by=None) -> List[dict]:
    """Volume rows of ONE symbol/source, start <= date <= end (None = open),
    optionally known_at <= known_by; oldest first; [] when none."""
    sql = f"SELECT {', '.join(VOLUME_COLUMNS)} FROM volumes WHERE symbol = ? AND source = ?"
    args: list = [_text(symbol, "symbol", 32).upper(), _text(source, "source", 32)]
    if start is not None:
        sql += " AND date >= ?"
        args.append(_iso_date(start, "start"))
    if end is not None:
        sql += " AND date <= ?"
        args.append(_iso_date(end, "end"))
    if known_by is not None:
        sql += " AND known_at <= ?"
        args.append(to_utc_iso(known_by, "known_by"))
    sql += " ORDER BY date"
    with _connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [{c: r[c] for c in VOLUME_COLUMNS} for r in rows]


# --- seam checks (SPEC §8.2) -------------------------------------------------------------------

SEAM_COLUMNS = ("symbol", "adjustment", "checked_at", "ok", "factor", "stats")


def seam_check_add(symbol: str, adjustment: str, ok: bool, factor, stats: dict, checked_at=None) -> dict:
    """Stores one seam check result (insert; history is kept) and returns it."""
    if not isinstance(stats, dict):
        raise ValueError("stats: expected a dict")
    row = (_text(symbol, "symbol", 32).upper(), _adjustment(adjustment),
           to_utc_iso(checked_at, "checked_at") if checked_at is not None else utc_now_iso(),
           1 if ok else 0, _number(factor, "factor", bool(ok)), _dumps(stats))
    with _connect() as conn:
        conn.execute("INSERT OR REPLACE INTO seam_checks (symbol, adjustment, checked_at, ok, factor, stats_json) "
                     "VALUES (?, ?, ?, ?, ?, ?)", row)
    return {"symbol": row[0], "adjustment": row[1], "checked_at": row[2], "ok": bool(row[3]), "factor": row[4],
            "stats": stats}


def _seam_row(r) -> dict:
    return {"symbol": r["symbol"], "adjustment": r["adjustment"], "checked_at": r["checked_at"],
            "ok": bool(r["ok"]), "factor": r["factor"], "stats": _loads(r["stats_json"])}


def seam_check_latest(symbol: str, adjustment: str) -> Optional[dict]:
    """The newest check of symbol/adjustment or None."""
    with _connect() as conn:
        r = conn.execute("SELECT * FROM seam_checks WHERE symbol = ? AND adjustment = ? "
                         "ORDER BY checked_at DESC LIMIT 1",
                         (_text(symbol, "symbol", 32).upper(), _adjustment(adjustment))).fetchone()
    return _seam_row(r) if r else None


def seam_checks_list(symbol: Optional[str] = None, limit: int = 100) -> List[dict]:
    """Newest first (optionally one symbol)."""
    limit = max(1, min(int(limit), 1000))
    sql, args = "SELECT * FROM seam_checks", []
    if symbol is not None:
        sql += " WHERE symbol = ?"
        args.append(_text(symbol, "symbol", 32).upper())
    sql += " ORDER BY checked_at DESC LIMIT ?"
    args.append(limit)
    with _connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_seam_row(r) for r in rows]


# --- strategy lifecycle (SPEC §7.10; logic in lifecycle/registry.py) -----------------------------

STRATEGY_COLUMNS = ("id", "name", "origin", "category", "state", "current_version", "created_at", "updated_at",
                    "state_changed_at", "library_key", "family_id")      # family_id: v6 (SPEC §9.1)
VERSION_COLUMNS = ("strategy_id", "version", "spec_json", "content_hash", "envelope_json", "validation_json",
                   "created_at")
TRANSITION_COLUMNS = ("id", "ts", "strategy_id", "version", "from_state", "to_state", "actor", "reason",
                      "evidence_json")


def _row(row, columns) -> dict:
    return {c: row[c] for c in columns}


def _version_tuple(v: dict) -> tuple:
    return (v["strategy_id"], int(v["version"]), v["spec_json"], v["content_hash"], v["envelope_json"],
            v["validation_json"], v["created_at"])


def _transition_tuple(t: dict) -> tuple:
    return (t["ts"], t["strategy_id"], int(t["version"]), t.get("from_state"), t["to_state"], t["actor"],
            t["reason"], t.get("evidence_json"))


def _insert_transition(conn, t: dict) -> int:
    cur = conn.execute("INSERT INTO strategy_transitions (ts, strategy_id, version, from_state, to_state, actor, "
                       "reason, evidence_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", _transition_tuple(t))
    return int(cur.lastrowid)


def _insert_audit(conn, actor: str, action: str, detail) -> None:
    conn.execute("INSERT INTO audit (ts, actor, action, detail_json) VALUES (?, ?, ?, ?)",
                 (utc_now_iso(), actor, action, _dumps(detail)))


def _insert_logic(conn, version: dict) -> None:
    """The version's logic hash (v7) — computed here from its spec_json, same transaction."""
    try:
        spec = json.loads(version["spec_json"])
    except (TypeError, ValueError) as ex:
        raise ValueError(f"spec_json: not JSON ({ex})") from None
    conn.execute("INSERT INTO strategy_logic (strategy_id, version, logic_hash) VALUES (?, ?, ?)",
                 (version["strategy_id"], int(version["version"]), logic_hash_of(spec)))


def logic_hash_get(strategy_id: str, version: int) -> Optional[str]:
    with _connect() as conn:
        row = conn.execute("SELECT logic_hash FROM strategy_logic WHERE strategy_id = ? AND version = ?",
                           (_text(strategy_id, "strategy_id", 64), int(version))).fetchone()
    return row[0] if row else None


def families_with_logic_hash(logic_hash: str) -> List[str]:
    """Family ids of every strategy having a version with ``logic_hash``, oldest strategy first."""
    with _connect() as conn:
        rows = conn.execute("SELECT s.family_id, MIN(s.created_at) AS c, MIN(s.rowid) AS r FROM strategy_logic l "
                            "JOIN strategies s ON s.id = l.strategy_id WHERE l.logic_hash = ? AND s.family_id IS NOT NULL "
                            "GROUP BY s.family_id ORDER BY c, r", (_text(logic_hash, "logic_hash", 64),)).fetchall()
    return [r[0] for r in rows]


def strategy_create(strategy: dict, version: dict, transition: dict, audit: tuple) -> None:
    """Inserts a strategy, its first version, the creation transition and the
    audit row in ONE transaction (all or nothing). ``strategy['family_id']`` (v6) is required; the
    family row is created when it is new."""
    _text(strategy.get("family_id"), "family_id", 64)
    with _connect() as conn:
        conn.execute("INSERT OR IGNORE INTO strategy_families (family_id, seen_until, holdout_failed_at, "
                     "created_at, updated_at) VALUES (?, NULL, NULL, ?, ?)",
                     (strategy["family_id"], strategy["created_at"], strategy["created_at"]))
        conn.execute(f"INSERT INTO strategies ({', '.join(STRATEGY_COLUMNS)}) "
                     f"VALUES ({', '.join('?' * len(STRATEGY_COLUMNS))})",
                     tuple(strategy[c] for c in STRATEGY_COLUMNS))
        conn.execute(f"INSERT INTO strategy_versions ({', '.join(VERSION_COLUMNS)}) "
                     f"VALUES ({', '.join('?' * len(VERSION_COLUMNS))})", _version_tuple(version))
        _insert_logic(conn, version)
        _insert_transition(conn, transition)
        _insert_audit(conn, *audit)


class StaleVersion(Exception):
    """strategy_add_version: the strategy's current version is not the expected base."""

    def __init__(self, current: int):
        super().__init__(f"current version is {current}")
        self.current = current


def strategy_add_version(strategy_id: str, base_version: int, version: dict, *, name: str, category: str,
                         new_state: Optional[str], transition: Optional[dict], audit: tuple) -> None:
    """Appends a version if ``current_version == base_version`` (else
    StaleVersion), updates the strategy row (name, category, state when
    ``new_state``), writes the optional transition and the audit row — one
    transaction."""
    now = version["created_at"]
    with _connect() as conn:
        # read + insert in ONE write transaction: a concurrent save waits (busy timeout) and then sees
        # the new current_version -> StaleVersion (409), never an IntegrityError (503).
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT current_version FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
        if row is None:
            raise KeyError(strategy_id)
        if int(row["current_version"]) != int(base_version):
            raise StaleVersion(int(row["current_version"]))
        try:
            conn.execute(f"INSERT INTO strategy_versions ({', '.join(VERSION_COLUMNS)}) "
                         f"VALUES ({', '.join('?' * len(VERSION_COLUMNS))})", _version_tuple(version))
        except sqlite3.IntegrityError:
            # (strategy_id, version) already taken by another writer
            cur = conn.execute("SELECT current_version FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
            raise StaleVersion(int(cur["current_version"]) if cur else int(base_version) + 1) from None
        _insert_logic(conn, version)
        if new_state is not None:
            conn.execute("UPDATE strategies SET current_version = ?, name = ?, category = ?, updated_at = ?, "
                         "state = ?, state_changed_at = ? WHERE id = ?",
                         (int(version["version"]), name, category, now, new_state, now, strategy_id))
        else:
            conn.execute("UPDATE strategies SET current_version = ?, name = ?, category = ?, updated_at = ? "
                         "WHERE id = ?", (int(version["version"]), name, category, now, strategy_id))
        if transition is not None:
            _insert_transition(conn, transition)
        _insert_audit(conn, *audit)


def strategy_set_state(strategy_id: str, expected_state: str, transition: dict, audit: tuple, *,
                       expected_version: Optional[int] = None) -> bool:
    """Moves the strategy to ``transition['to_state']`` if it is still in
    ``expected_state`` (and, when given, still at ``expected_version``); writes
    the transition + audit row. False when the state/version changed meanwhile
    (nothing written)."""
    sql = "UPDATE strategies SET state = ?, state_changed_at = ?, updated_at = ? WHERE id = ? AND state = ?"
    args = [transition["to_state"], transition["ts"], transition["ts"], strategy_id, expected_state]
    if expected_version is not None:
        sql += " AND current_version = ?"
        args.append(int(expected_version))
    with _connect() as conn:
        cur = conn.execute(sql, args)
        if cur.rowcount != 1:
            return False
        _insert_transition(conn, transition)
        _insert_audit(conn, *audit)
    return True


def strategy_get(strategy_id: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(f"SELECT {', '.join(STRATEGY_COLUMNS)} FROM strategies WHERE id = ?",
                           (_text(strategy_id, "strategy_id", 64),)).fetchone()
    return _row(row, STRATEGY_COLUMNS) if row else None


def strategy_by_library_key(library_key: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(f"SELECT {', '.join(STRATEGY_COLUMNS)} FROM strategies WHERE library_key = ?",
                           (_text(library_key, "library_key", 128),)).fetchone()
    return _row(row, STRATEGY_COLUMNS) if row else None


def strategies_list(state: Optional[str] = None, origin: Optional[str] = None,
                    category: Optional[str] = None) -> List[dict]:
    """Strategies with their current version's content_hash, envelope_json and
    validation_json, ordered by name, id."""
    cols = ", ".join(f"s.{c}" for c in STRATEGY_COLUMNS)
    sql = (f"SELECT {cols}, v.content_hash, v.envelope_json, v.validation_json, v.spec_json FROM strategies s "
           "JOIN strategy_versions v ON v.strategy_id = s.id AND v.version = s.current_version")
    where, args = [], []
    for col, val in (("state", state), ("origin", origin), ("category", category)):
        if val is not None:
            where.append(f"s.{col} = ?")
            args.append(_text(val, col, 64))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY s.name COLLATE NOCASE, s.id"
    with _connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    extra = ("content_hash", "envelope_json", "validation_json", "spec_json")
    return [_row(r, STRATEGY_COLUMNS + extra) for r in rows]


def strategy_versions_list(strategy_id: str) -> List[dict]:
    """All versions of a strategy, oldest first (full rows)."""
    with _connect() as conn:
        rows = conn.execute(f"SELECT {', '.join(VERSION_COLUMNS)} FROM strategy_versions WHERE strategy_id = ? "
                            "ORDER BY version", (_text(strategy_id, "strategy_id", 64),)).fetchall()
    return [_row(r, VERSION_COLUMNS) for r in rows]


def strategy_version_get(strategy_id: str, version: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(f"SELECT {', '.join(VERSION_COLUMNS)} FROM strategy_versions "
                           "WHERE strategy_id = ? AND version = ?",
                           (_text(strategy_id, "strategy_id", 64), int(version))).fetchone()
    return _row(row, VERSION_COLUMNS) if row else None


def strategy_transitions_list(strategy_id: str) -> List[dict]:
    """Transitions of a strategy, oldest first."""
    with _connect() as conn:
        rows = conn.execute(f"SELECT {', '.join(TRANSITION_COLUMNS)} FROM strategy_transitions "
                            "WHERE strategy_id = ? ORDER BY id", (_text(strategy_id, "strategy_id", 64),)).fetchall()
    return [_row(r, TRANSITION_COLUMNS) for r in rows]


# --- backtest run cards (v5, SPEC §8.4 / §8.10) ---------------------------------------------------

RUN_STATUSES = ("queued", "running", "done", "failed", "cancelled")
RUN_KINDS = ("backtest", "validation", "holdout")          # v6 (SPEC §9)
RUN_TRANSITIONS = {"queued": ("running", "failed", "cancelled"), "running": ("done", "failed", "cancelled")}
RUN_COLUMNS = ("id", "created_at", "updated_at", "started_at", "finished_at", "strategy_id", "version",
               "content_hash", "status", "spec_json", "request_json", "seed", "trial_number", "engine_version",
               "range_json", "data_json", "licences_json", "cost_model_json", "metrics_json", "curve_json",
               "trades_json", "warnings_json", "stress_json", "error", "kind",
               "resumed_from", "resume_count", "error_kind", "auto_resume",          # v8 (SPEC §10.3)
               "attribution_json")                                                     # v9 (SPEC §11.1)
ERROR_KINDS = ("connection", "interrupted", "basis", "error")               # v8: why a card failed
RUN_RESULT_COLUMNS = ("engine_version", "range_json", "data_json", "licences_json", "cost_model_json",
                      "metrics_json", "curve_json", "trades_json", "warnings_json", "stress_json", "error",
                      "attribution_json")                                              # v9 (SPEC §11.1)
RUN_LIST_COLUMNS = ("id", "created_at", "updated_at", "started_at", "finished_at", "strategy_id", "version",
                    "content_hash", "status", "request_json", "range_json", "metrics_json", "error", "kind",
                    "trial_number", "resumed_from", "resume_count", "error_kind", "auto_resume")


def backtest_run_insert(row: dict, audit: tuple) -> None:
    """Inserts a new run card (status ``queued``) + its audit row in ONE transaction.
    ``row`` holds RUN_COLUMNS (missing optional columns = NULL); an existing id -> StorageUnavailable."""
    if row.get("status") != "queued":
        raise ValueError("status: a new run card starts as 'queued'")
    row = dict(row)
    row.setdefault("kind", "backtest")          # = the column default (v6)
    row["resume_count"] = row.get("resume_count") or 0          # v8 defaults
    row["auto_resume"] = 1 if row.get("auto_resume") else 0
    if isinstance(row["resume_count"], bool) or not isinstance(row["resume_count"], int) or row["resume_count"] < 0:
        raise ValueError("resume_count: an integer >= 0")
    if row.get("error_kind") is not None:
        raise ValueError("error_kind: set when the card fails")
    if row.get("resumed_from") is not None:
        _text(row["resumed_from"], "resumed_from", 64)
    if row["kind"] not in RUN_KINDS:
        raise ValueError(f"kind: one of {', '.join(RUN_KINDS)}")
    if row.get("trial_number") is not None:
        raise ValueError("trial_number: set when the card finishes (SPEC §9.2)")
    for k in row:
        if k not in RUN_COLUMNS:
            raise ValueError(f"unknown run card field {k!r}")
    for k in ("id", "created_at", "updated_at", "strategy_id", "content_hash", "spec_json", "request_json"):
        _text(row.get(k), k, 10_000_000)
    int(row["version"])
    with _connect() as conn:
        conn.execute(f"INSERT INTO backtest_runs ({', '.join(RUN_COLUMNS)}) "
                     f"VALUES ({', '.join('?' * len(RUN_COLUMNS))})", tuple(row.get(c) for c in RUN_COLUMNS))
        _insert_audit(conn, *audit)


def backtest_run_transition(run_id: str, from_status: str, to_status: str, fields: Optional[dict] = None,
                            audit: Optional[tuple] = None) -> bool:
    """Moves a run card ``from_status`` -> ``to_status`` (RUN_TRANSITIONS) and writes the result
    ``fields`` (RUN_RESULT_COLUMNS + started_at/finished_at) in the same UPDATE; False when the card
    is not (any more) in ``from_status`` (nothing written). The DB triggers enforce the same rules."""
    if to_status not in RUN_TRANSITIONS.get(from_status, ()):
        raise ValueError(f"status: {from_status!r} -> {to_status!r} is not allowed")
    fields = dict(fields or {})
    for k in fields:
        if k not in RUN_RESULT_COLUMNS + ("started_at", "finished_at", "error_kind"):
            raise ValueError(f"run card field {k!r} cannot be written by a status transition")
    if fields.get("error_kind") is not None and fields["error_kind"] not in ERROR_KINDS:
        raise ValueError(f"error_kind: one of {', '.join(ERROR_KINDS)}")
    now = utc_now_iso()
    sets = ["status = ?", "updated_at = ?"] + [f"{k} = ?" for k in fields]
    args = [to_status, now] + list(fields.values()) + [_text(run_id, "run_id", 64), from_status]
    with _connect() as conn:
        cur = conn.execute(f"UPDATE backtest_runs SET {', '.join(sets)} WHERE id = ? AND status = ?", args)
        if cur.rowcount != 1:
            return False
        if audit is not None:
            _insert_audit(conn, *audit)
    return True


def backtest_run_get(run_id: str) -> Optional[dict]:
    """The full stored row (JSON columns as text) or None."""
    with _connect() as conn:
        row = conn.execute(f"SELECT {', '.join(RUN_COLUMNS)} FROM backtest_runs WHERE id = ?",
                           (_text(run_id, "run_id", 64),)).fetchone()
    return _row(row, RUN_COLUMNS) if row else None


def backtest_runs_list(strategy_id: Optional[str] = None, *, version: Optional[int] = None,
                       status: Optional[str] = None, limit: int = 50, kind: Optional[str] = None) -> List[dict]:
    """Run cards newest first (RUN_LIST_COLUMNS — no curve/data/spec), ``[]`` when none; ``kind`` filter (v6)."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("limit: an integer 1..1000")
    where, args = [], []
    if strategy_id is not None:
        where.append("strategy_id = ?")
        args.append(_text(strategy_id, "strategy_id", 64))
    if version is not None:
        if isinstance(version, bool) or not isinstance(version, int):
            raise ValueError("version: an integer")
        where.append("version = ?")
        args.append(version)
    if status is not None:
        if status not in RUN_STATUSES:
            raise ValueError(f"status: one of {', '.join(RUN_STATUSES)}")
        where.append("status = ?")
        args.append(status)
    if kind is not None:
        if kind not in RUN_KINDS:
            raise ValueError(f"kind: one of {', '.join(RUN_KINDS)}")
        where.append("kind = ?")
        args.append(kind)
    sql = f"SELECT {', '.join(RUN_LIST_COLUMNS)} FROM backtest_runs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"     # rowid: insertion order within one second (v6)
    with _connect() as conn:
        rows = conn.execute(sql, args + [limit]).fetchall()
    return [_row(r, RUN_LIST_COLUMNS) for r in rows]


def backtest_run_finish(run_id: str, fields: dict, *, seen_until: Optional[str], audit: tuple,
                        holdout_event: Optional[dict] = None) -> Optional[int]:
    """running -> done (v6, SPEC §9.1/§9.2) in ONE write transaction: the result ``fields``
    (RUN_RESULT_COLUMNS + finished_at), ``trial_number`` = number of done cards of the strategy's
    family + 1, the family's ``seen_until`` raised to ``seen_until`` (YYYY-MM-DD; None = unchanged),
    the optional ``holdout_event`` (HOLDOUT_EVENT_COLUMNS without id; review 2026-10-09: the holdout
    result and its event are written together or not at all — the card must be kind ``holdout`` and
    the event's run_id/strategy/version its own; the event id goes into the audit detail) and the
    audit row. -> the trial number, or None when the card is not ``running`` (nothing written)."""
    ev = _check_holdout_event(holdout_event) if holdout_event is not None else None
    fields = dict(fields or {})
    for k in fields:
        if k not in RUN_RESULT_COLUMNS + ("started_at", "finished_at"):
            raise ValueError(f"run card field {k!r} cannot be written by a status transition")
    if seen_until is not None:
        seen_until = _iso_date(seen_until, "seen_until")
    now = utc_now_iso()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")            # the count and the update see the same cards
        row = conn.execute("SELECT r.status, r.kind, r.strategy_id, r.version, s.family_id FROM backtest_runs r "
                           "LEFT JOIN strategies s ON s.id = r.strategy_id WHERE r.id = ?",
                           (_text(run_id, "run_id", 64),)).fetchone()
        if row is None or row["status"] != "running":
            return None
        if ev is not None and (row["kind"] != "holdout" or ev["run_id"] != run_id
                               or ev["strategy_id"] != row["strategy_id"] or ev["version"] != int(row["version"])
                               or ev["family_id"] != row["family_id"]):
            raise ValueError("holdout_event: must belong to this holdout run card (run, strategy, version, family)")
        fid = row["family_id"]
        if not fid:
            raise StorageUnavailable("a strategy has no family (schema v6 data missing)", detail=run_id)
        n = conn.execute("SELECT COUNT(*) FROM backtest_runs r JOIN strategies s ON s.id = r.strategy_id "
                         "WHERE s.family_id = ? AND r.status = 'done' AND " + NOT_RESUMED, (fid,)).fetchone()[0]
        trial = int(n) + 1
        sets = ["status = 'done'", "updated_at = ?", "trial_number = ?"] + [f"{k} = ?" for k in fields]
        cur = conn.execute(f"UPDATE backtest_runs SET {', '.join(sets)} WHERE id = ? AND status = 'running'",
                           [now, trial] + list(fields.values()) + [run_id])
        if cur.rowcount != 1:
            return None
        if seen_until is not None:
            _family_raise_seen(conn, fid, seen_until, now)
        if ev is not None:
            event_id = _write_holdout_event(conn, ev)
            actor, action, detail = audit
            audit = (actor, action, dict(detail or {}, holdout_event_id=event_id, result=ev["result"]))
        _insert_audit(conn, *audit)
    return trial


# --- strategy families, holdout events, validation exceptions (v6, SPEC §9) -----------------------

# v8 (SPEC §10.3): trial counting ignores a card that has a later resumed_from successor. Only failed cards
# can be resumed, so a done card never has one and the chain's final (done) card is its single trial.
NOT_RESUMED = "NOT EXISTS (SELECT 1 FROM backtest_runs x WHERE x.resumed_from = r.id)"

FAMILY_COLUMNS = ("family_id", "seen_until", "holdout_failed_at", "created_at", "updated_at")
HOLDOUT_EVENT_COLUMNS = ("id", "ts", "family_id", "strategy_id", "version", "window_start", "window_end",
                         "universe_kind", "universe_key", "result", "run_id")
EXCEPTION_COLUMNS = ("id", "ts", "run_id", "gate", "reason", "actor")
VALIDATION_GATES = ("G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8")
MIN_EXCEPTION_REASON = 30
MAX_EXCEPTION_REASON = 2000


def _family_raise_seen(conn, family_id: str, seen_until: str, now: str) -> None:
    conn.execute("INSERT INTO strategy_families (family_id, seen_until, holdout_failed_at, created_at, updated_at) "
                 "VALUES (?, ?, NULL, ?, ?) ON CONFLICT(family_id) DO UPDATE SET "
                 "seen_until = CASE WHEN strategy_families.seen_until IS NULL "
                 "OR excluded.seen_until > strategy_families.seen_until THEN excluded.seen_until "
                 "ELSE strategy_families.seen_until END, updated_at = excluded.updated_at",
                 (family_id, seen_until, now, now))


def family_get(family_id: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(f"SELECT {', '.join(FAMILY_COLUMNS)} FROM strategy_families WHERE family_id = ?",
                           (_text(family_id, "family_id", 64),)).fetchone()
    return _row(row, FAMILY_COLUMNS) if row else None


def family_record_seen(family_id: str, end_date) -> None:
    """Raises the family's seen_until to ``end_date`` (never lowers it); creates the row when missing."""
    end = _iso_date(end_date, "end_date")
    with _connect() as conn:
        _family_raise_seen(conn, _text(family_id, "family_id", 64), end, utc_now_iso())


def family_strategy_ids(family_id: str) -> List[str]:
    with _connect() as conn:
        rows = conn.execute("SELECT id FROM strategies WHERE family_id = ? ORDER BY created_at, id",
                            (_text(family_id, "family_id", 64),)).fetchall()
    return [r[0] for r in rows]


def family_trial_count(family_id: str) -> int:
    """Number of done run cards (all kinds) of all strategies of the family; a card with a later
    ``resumed_from`` successor is not counted (v8, SPEC §10.3: a resumed chain is ONE trial)."""
    with _connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM backtest_runs r JOIN strategies s ON s.id = r.strategy_id "
                                "WHERE s.family_id = ? AND r.status = 'done' AND " + NOT_RESUMED,
                                (_text(family_id, "family_id", 64),)).fetchone()[0])


def family_done_runs(family_id: str) -> List[dict]:
    """{id, kind, trial_number, finished_at, metrics_json} of the family's done cards, oldest first."""
    with _connect() as conn:
        rows = conn.execute("SELECT r.id, r.kind, r.trial_number, r.finished_at, r.metrics_json FROM backtest_runs r "
                            "JOIN strategies s ON s.id = r.strategy_id WHERE s.family_id = ? AND r.status = 'done' "
                            "AND " + NOT_RESUMED + " ORDER BY r.finished_at, r.rowid", (_text(family_id, "family_id", 64),)).fetchall()
    return [_row(r, ("id", "kind", "trial_number", "finished_at", "metrics_json")) for r in rows]


def holdout_event_insert(event: dict, *, audit: tuple) -> int:
    """Inserts an immutable holdout event (HOLDOUT_EVENT_COLUMNS without id) and, in the same
    transaction, sets the family's seen_until to max(current, window_end) and holdout_failed_at to
    the event ts on 'fail' / NULL on 'pass' (a passed re-test lifts the lock) + the audit row."""
    ev = _check_holdout_event(event)
    with _connect() as conn:
        event_id = _write_holdout_event(conn, ev)
        _insert_audit(conn, *audit)
        return event_id


def _check_holdout_event(event: dict) -> dict:
    cols = HOLDOUT_EVENT_COLUMNS[1:]
    for k in event:
        if k not in cols:
            raise ValueError(f"unknown holdout event field {k!r}")
    if event.get("result") not in ("pass", "fail"):
        raise ValueError("result: pass or fail")
    if event.get("universe_kind") not in ("equity", "crypto", "mixed"):
        raise ValueError("universe_kind: equity, crypto or mixed")
    ev = dict(event)
    ev["ts"] = to_utc_iso(ev.get("ts"), "ts")
    ev["window_start"] = _iso_date(ev.get("window_start"), "window_start")
    ev["window_end"] = _iso_date(ev.get("window_end"), "window_end")
    if ev["window_start"] > ev["window_end"]:
        raise ValueError("window: start after end")
    for k in ("family_id", "strategy_id", "run_id"):
        _text(ev.get(k), k, 64)
    _text(ev.get("universe_key"), "universe_key", 200)
    if isinstance(ev.get("version"), bool) or not isinstance(ev.get("version"), int):
        raise ValueError("version: an integer")
    return ev


def _write_holdout_event(conn, ev: dict) -> int:
    """Inserts a checked event + family seen_until / holdout_failed_at (caller's transaction)."""
    cols = HOLDOUT_EVENT_COLUMNS[1:]
    cur = conn.execute(f"INSERT INTO holdout_events ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                       tuple(ev[c] for c in cols))
    _family_raise_seen(conn, ev["family_id"], ev["window_end"], ev["ts"])
    conn.execute("UPDATE strategy_families SET holdout_failed_at = ?, updated_at = ? WHERE family_id = ?",
                 (ev["ts"] if ev["result"] == "fail" else None, ev["ts"], ev["family_id"]))
    return int(cur.lastrowid)


def holdout_events_list(family_id: Optional[str] = None, *, universe_key: Optional[str] = None) -> List[dict]:
    """Holdout events, oldest first (filters optional)."""
    where, args = [], []
    if family_id is not None:
        where.append("family_id = ?")
        args.append(_text(family_id, "family_id", 64))
    if universe_key is not None:
        where.append("universe_key = ?")
        args.append(_text(universe_key, "universe_key", 200))
    sql = f"SELECT {', '.join(HOLDOUT_EVENT_COLUMNS)} FROM holdout_events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    with _connect() as conn:
        rows = conn.execute(sql + " ORDER BY id", args).fetchall()
    return [_row(r, HOLDOUT_EVENT_COLUMNS) for r in rows]


def validation_exception_add(run_id: str, gate: str, reason: str, *, actor: str = "user",
                             audit: Optional[tuple] = None) -> int:
    """Insert-only exception for a failed gate G1–G8 of a DONE validation run card (SPEC §9.4).
    ``reason`` stripped, 30..2000 characters. Unknown/not-done/not-validation card or a second
    exception for the same gate -> ValueError. The caller (validation layer) checks that the gate
    actually failed. -> id"""
    if gate not in VALIDATION_GATES:
        raise ValueError(f"gate: one of {', '.join(VALIDATION_GATES)} (the holdout gate H has no exception)")
    if not isinstance(reason, str) or not MIN_EXCEPTION_REASON <= len(reason.strip()) <= MAX_EXCEPTION_REASON:
        raise ValueError(f"reason: {MIN_EXCEPTION_REASON}..{MAX_EXCEPTION_REASON} characters")
    if actor not in ("user", "system"):
        raise ValueError("actor: user or system")
    with _connect() as conn:
        card = conn.execute("SELECT kind, status FROM backtest_runs WHERE id = ?",
                            (_text(run_id, "run_id", 64),)).fetchone()
        if card is None or card["kind"] != "validation" or card["status"] != "done":
            raise ValueError("run_id: a finished validation run")
        if conn.execute("SELECT 1 FROM validation_exceptions WHERE run_id = ? AND gate = ?",
                        (run_id, gate)).fetchone():
            raise ValueError(f"gate: {gate} already has an exception in this run")
        cur = conn.execute("INSERT INTO validation_exceptions (ts, run_id, gate, reason, actor) VALUES (?, ?, ?, ?, ?)",
                           (utc_now_iso(), run_id, gate, reason.strip(), actor))
        _insert_audit(conn, *(audit or (actor, "validation_exception", {"run_id": run_id, "gate": gate})))
        return int(cur.lastrowid)


def validation_exceptions_list(run_id: str) -> List[dict]:
    with _connect() as conn:
        rows = conn.execute(f"SELECT {', '.join(EXCEPTION_COLUMNS)} FROM validation_exceptions WHERE run_id = ? "
                            "ORDER BY id", (_text(run_id, "run_id", 64),)).fetchall()
    return [_row(r, EXCEPTION_COLUMNS) for r in rows]


# --- resumable runs (v8, SPEC §10.3, R2 notes) ---------------------------------------------------------

CHECKPOINT_COLUMNS = ("id", "run_id", "step_key", "payload_json", "sha256", "created_at")
MAX_CHECKPOINT_PAYLOAD = 64_000_000
MAX_CHAIN = 1000


def backtest_run_note(run_id: str, *, error_kind=None, resume_count_increment: bool = False,
                      audit: Optional[tuple] = None) -> bool:
    """running -> running (v8): sets ``error_kind`` (None clears it) and optionally raises
    ``resume_count`` by one — the auto-resume's waiting / resumed state; nothing else can change while
    a card runs (trigger). -> False when the card is not running (nothing written)."""
    if error_kind is not None and error_kind not in ERROR_KINDS:
        raise ValueError(f"error_kind: one of {', '.join(ERROR_KINDS)}")
    sets = "updated_at = ?, error_kind = ?" + (", resume_count = resume_count + 1" if resume_count_increment else "")
    with _connect() as conn:
        cur = conn.execute(f"UPDATE backtest_runs SET {sets} WHERE id = ? AND status = 'running'",
                           (utc_now_iso(), error_kind, _text(run_id, "run_id", 64)))
        if cur.rowcount != 1:
            return False
        if audit is not None:
            _insert_audit(conn, *audit)
    return True


def backtest_run_chain(run_id: str) -> List[str]:
    """[run_id, its resumed_from, that one's resumed_from, …] (oldest last; unknown id -> [run_id])."""
    out = [_text(run_id, "run_id", 64)]
    with _connect() as conn:
        while len(out) <= MAX_CHAIN:
            row = conn.execute("SELECT resumed_from FROM backtest_runs WHERE id = ?", (out[-1],)).fetchone()
            if row is None or row[0] is None or row[0] in out:
                break
            out.append(row[0])
    return out


def backtest_run_successors(run_ids: Iterable[str]) -> Dict[str, str]:
    """{failed run id: id of the card that resumed it} for the given ids that were resumed."""
    ids = [_text(r, "run_id", 64) for r in run_ids]
    out: Dict[str, str] = {}
    with _connect() as conn:
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            rows = conn.execute(f"SELECT resumed_from, id FROM backtest_runs WHERE resumed_from IN "
                                f"({', '.join('?' * len(part))})", part).fetchall()
            out.update({r[0]: r[1] for r in rows})
    return out


def checkpoint_insert(run_id: str, step_key: str, payload_json: str, sha256: str) -> int:
    """Inserts one checkpoint row (insert-only table) -> id. The caller computes ``sha256``."""
    _text(run_id, "run_id", 64)
    _text(step_key, "step_key", 512)
    _text(payload_json, "payload_json", MAX_CHECKPOINT_PAYLOAD)
    if not isinstance(sha256, str) or len(sha256) != 64:
        raise ValueError("sha256: 64 hex characters")
    with _connect() as conn:
        cur = conn.execute("INSERT INTO run_checkpoints (run_id, step_key, payload_json, sha256, created_at) "
                           "VALUES (?, ?, ?, ?, ?)", (run_id, step_key, payload_json, sha256, utc_now_iso()))
        return int(cur.lastrowid)


def checkpoints_list(run_ids: Iterable[str]) -> List[dict]:
    """Checkpoint rows (CHECKPOINT_COLUMNS) of the given runs, oldest first."""
    ids = [_text(r, "run_id", 64) for r in run_ids]
    out: List[dict] = []
    with _connect() as conn:
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            rows = conn.execute(f"SELECT {', '.join(CHECKPOINT_COLUMNS)} FROM run_checkpoints WHERE run_id IN "
                                f"({', '.join('?' * len(part))}) ORDER BY id", part).fetchall()
            out.extend(_row(r, CHECKPOINT_COLUMNS) for r in rows)
    return sorted(out, key=lambda r: r["id"])


# --- explanations (SPEC §11.2, v10) ----------------------------------------------------------

EXPLAIN_BUDGET_KEY = "explain_daily_budget_usd"
EXPLAIN_SPEND_PREFIX = "explain_spend_usd:"          # + UTC day YYYY-MM-DD
EXPLANATION_KINDS = ("backtest", "validation", "holdout")
EXPLANATION_COLUMNS = ("id", "run_id", "run_kind", "created_at", "model", "language", "facts_hash",
                       "output_json", "check_json", "usage_json", "cost_usd", "provenance_json")
_DAY_RE_TEXT = r"\d{4}-\d{2}-\d{2}"


def _day(day) -> str:
    import re as _re
    if not isinstance(day, str) or not _re.fullmatch(_DAY_RE_TEXT, day):
        raise ValueError("day: YYYY-MM-DD")
    return day


def _spend_value(text, key: str) -> float:
    """A stored spend counter; an unreadable value raises (fail closed — never read as 0)."""
    if text is None:
        return 0.0
    try:
        v = float(text)
    except (TypeError, ValueError):
        v = float("nan")
    if not math.isfinite(v) or v < 0:
        raise StorageUnavailable("a stored value is invalid", detail=f"{key}={str(text)[:40]!r}")
    return v


def explain_spend_get(day: str) -> float:
    """The explanation spend (USD) booked on the UTC ``day``; 0.0 when nothing was booked."""
    key = EXPLAIN_SPEND_PREFIX + _day(day)
    with _connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return _spend_value(row["value"] if row else None, key)


def explain_spend_add(day: str, usd) -> float:
    """Adds ``usd`` (>= 0) to the UTC ``day``'s explanation spend in one transaction -> the new total."""
    key = EXPLAIN_SPEND_PREFIX + _day(day)
    usd = _number(usd, "usd", True)
    if usd < 0:
        raise ValueError("usd: >= 0")
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")          # read-modify-write under the write lock
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        total = _spend_value(row["value"] if row else None, key) + usd
        conn.execute("INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                     (key, repr(total), utc_now_iso()))
    return total


def _explanation_out(row) -> dict:
    out = {c: row[c] for c in EXPLANATION_COLUMNS}
    for c in ("output_json", "check_json", "usage_json", "provenance_json"):
        out[c[:-5]] = _loads(out.pop(c))
    return out


def explanation_insert(row: dict, audit: tuple) -> dict:
    """Inserts one explanation + its audit row in ONE transaction -> the stored row (decoded:
    output, check, usage, provenance). Insert-only table (triggers refuse UPDATE/DELETE)."""
    allowed = set(EXPLANATION_COLUMNS) - {"id"}
    for k in row:
        if k not in allowed:
            raise ValueError(f"unknown explanation field {k!r}")
    run_id = _text(row.get("run_id"), "run_id", 64)
    if row.get("run_kind") not in EXPLANATION_KINDS:
        raise ValueError(f"run_kind: one of {', '.join(EXPLANATION_KINDS)}")
    if row.get("language") not in ("en", "de"):
        raise ValueError("language: en or de")
    model = _text(row.get("model"), "model", 200)
    facts_hash = _text(row.get("facts_hash"), "facts_hash", 64)
    for k in ("output_json", "check_json", "provenance_json"):
        _text(row.get(k), k, 1_000_000)
    cost = _number(row.get("cost_usd"), "cost_usd", False)
    created = row.get("created_at") or utc_now_iso()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO run_explanations (run_id, run_kind, created_at, model, language, facts_hash, "
            "output_json, check_json, usage_json, cost_usd, provenance_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, row["run_kind"], to_utc_iso(created, "created_at"), model, row["language"], facts_hash,
             row["output_json"], row["check_json"], row.get("usage_json"), cost, row["provenance_json"]))
        new_id = int(cur.lastrowid)
        actor, action, detail = audit
        detail = dict(detail or {})
        detail["explanation_id"] = new_id
        _insert_audit(conn, actor, action, detail)
        stored = conn.execute(f"SELECT {', '.join(EXPLANATION_COLUMNS)} FROM run_explanations WHERE id = ?",
                              (new_id,)).fetchone()
    return _explanation_out(stored)


def explanations_list(run_id: str, run_kinds: Optional[Iterable[str]] = None) -> List[dict]:
    """The explanations of a run, newest first (decoded rows); [] when none."""
    run_id = _text(run_id, "run_id", 64)
    sql, args = f"SELECT {', '.join(EXPLANATION_COLUMNS)} FROM run_explanations WHERE run_id = ?", [run_id]
    if run_kinds is not None:
        kinds = [k for k in run_kinds]
        if not kinds or any(k not in EXPLANATION_KINDS for k in kinds):
            raise ValueError(f"run_kinds: of {', '.join(EXPLANATION_KINDS)}")
        sql += f" AND run_kind IN ({', '.join('?' * len(kinds))})"
        args += kinds
    sql += " ORDER BY id DESC"
    with _connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_explanation_out(r) for r in rows]


# --- Strategy Studio + memory (SPEC §13, v11) --------------------------------------------------------

IDEATION_MONTHLY_KEY = "ideation_monthly_budget_usd"
IDEATION_DAILY_KEY = "ideation_daily_cap_usd"
IDEATION_SPEND_MONTH_PREFIX = "ideation_spend_month_usd:"     # + UTC month YYYY-MM
IDEATION_SPEND_DAY_PREFIX = "ideation_spend_day_usd:"         # + UTC day YYYY-MM-DD
STUDIO_ROLES = ("user", "assistant", "system_note")
STUDIO_BACKTEST_EVENTS = ("waiting", "started", "cancelled", "start_failed", "not_queued")
SESSION_COLUMNS = ("id", "created_at", "title", "strategy_id")
STUDIO_MESSAGE_COLUMNS = ("id", "session_id", "role", "text", "spec_version", "run_id", "operator_requests_json",
                          "provenance_json", "usage_json", "cost_usd", "created_at", "detail_json")
STUDIO_BACKTEST_COLUMNS = ("id", "session_id", "message_id", "strategy_id", "version", "event", "run_id", "note",
                           "created_at")
MEMORY_COLUMNS = ("id", "family_id", "strategy_id", "version", "run_id", "kind", "fact_json", "created_at")


def _month(month) -> str:
    import re as _re
    if not isinstance(month, str) or not _re.fullmatch(r"\d{4}-\d{2}", month):
        raise ValueError("month: YYYY-MM")
    return month


def ideation_spend_get(month: str, day: str):
    """(spent this UTC month, spent this UTC day) in USD; 0.0 when nothing was booked; an unreadable
    stored value raises StorageUnavailable (fail closed)."""
    mk, dk = IDEATION_SPEND_MONTH_PREFIX + _month(month), IDEATION_SPEND_DAY_PREFIX + _day(day)
    with _connect() as conn:
        rows = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta WHERE key IN (?, ?)",
                                                           (mk, dk)).fetchall()}
    return _spend_value(rows.get(mk), mk), _spend_value(rows.get(dk), dk)


def ideation_spend_add(month: str, day: str, usd):
    """Adds ``usd`` to the month AND the day counter in one transaction -> (month total, day total)."""
    mk, dk = IDEATION_SPEND_MONTH_PREFIX + _month(month), IDEATION_SPEND_DAY_PREFIX + _day(day)
    usd = _number(usd, "usd", True)
    if usd < 0:
        raise ValueError("usd: >= 0")
    out = []
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for key in (mk, dk):
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            total = _spend_value(row["value"] if row else None, key) + usd
            conn.execute("INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                         (key, repr(total), utc_now_iso()))
            out.append(total)
    return out[0], out[1]


def studio_session_insert(row: dict, audit: tuple) -> dict:
    sid = _text(row.get("id"), "id", 64)
    title = row.get("title")
    if title is not None:
        title = _text(title, "title", 120)
    created = to_utc_iso(row.get("created_at") or utc_now_iso(), "created_at")
    with _connect() as conn:
        conn.execute("INSERT INTO studio_sessions (id, created_at, title, strategy_id) VALUES (?, ?, ?, NULL)",
                     (sid, created, title))
        _insert_audit(conn, *audit)
    return {"id": sid, "created_at": created, "title": title, "strategy_id": None}


def studio_session_get(session_id: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(f"SELECT {', '.join(SESSION_COLUMNS)} FROM studio_sessions WHERE id = ?",
                           (_text(session_id, "session_id", 64),)).fetchone()
    return _row(row, SESSION_COLUMNS) if row else None


def studio_sessions_list(limit: int = 50) -> List[dict]:
    """Newest first, with message_count and last_message_at."""
    limit = max(1, min(int(limit), 1000))
    with _connect() as conn:
        rows = conn.execute(
            "SELECT s.id, s.created_at, s.title, s.strategy_id, COUNT(m.id) AS message_count, "
            "MAX(m.created_at) AS last_message_at FROM studio_sessions s "
            "LEFT JOIN studio_messages m ON m.session_id = s.id GROUP BY s.id "
            "ORDER BY s.created_at DESC, s.id DESC LIMIT ?", (limit,)).fetchall()
    return [_row(r, SESSION_COLUMNS + ("message_count", "last_message_at")) for r in rows]


def studio_session_set_strategy(session_id: str, strategy_id: str) -> bool:
    """Sets the session's strategy ONCE -> True; False when it already has one (or does not exist)."""
    with _connect() as conn:
        cur = conn.execute("UPDATE studio_sessions SET strategy_id = ? WHERE id = ? AND strategy_id IS NULL",
                           (_text(strategy_id, "strategy_id", 64), _text(session_id, "session_id", 64)))
        return cur.rowcount == 1


def _studio_message_tuple(r: dict) -> tuple:
    if r.get("role") not in STUDIO_ROLES:
        raise ValueError(f"role: one of {', '.join(STUDIO_ROLES)}")
    for k in r:
        if k not in STUDIO_MESSAGE_COLUMNS or k == "id":
            raise ValueError(f"unknown studio message field {k!r}")
    if not isinstance(r.get("text"), str):
        raise ValueError("text: a string")
    return (_text(r.get("session_id"), "session_id", 64), r["role"], r["text"], r.get("spec_version"),
            r.get("run_id"), r.get("operator_requests_json"), r.get("provenance_json"), r.get("usage_json"),
            _number(r.get("cost_usd"), "cost_usd", False), to_utc_iso(r.get("created_at") or utc_now_iso()),
            r.get("detail_json"))


def studio_messages_insert(rows: List[dict], audit: Optional[tuple] = None) -> List[int]:
    """Inserts messages (+ an optional audit row) in ONE transaction -> their ids."""
    values = [_studio_message_tuple(r) for r in rows]
    ids = []
    with _connect() as conn:
        for v in values:
            cur = conn.execute("INSERT INTO studio_messages (session_id, role, text, spec_version, run_id, "
                               "operator_requests_json, provenance_json, usage_json, cost_usd, created_at, "
                               "detail_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", v)
            ids.append(int(cur.lastrowid))
        if audit is not None:
            actor, action, detail = audit
            _insert_audit(conn, actor, action, dict(detail or {}, message_ids=ids))
    return ids


def studio_messages_list(session_id: Optional[str] = None, *, with_operator_requests: bool = False,
                         ids: Optional[Iterable[int]] = None) -> List[dict]:
    """Messages oldest first (decoded JSON columns: operator_requests, provenance, usage, detail)."""
    sql, args, where = f"SELECT {', '.join(STUDIO_MESSAGE_COLUMNS)} FROM studio_messages", [], []
    if session_id is not None:
        where.append("session_id = ?")
        args.append(_text(session_id, "session_id", 64))
    if with_operator_requests:
        where.append("operator_requests_json IS NOT NULL AND operator_requests_json != '[]'")
    if ids is not None:
        id_list = [int(i) for i in ids]
        if not id_list:
            return []
        where.append(f"id IN ({', '.join('?' * len(id_list))})")
        args += id_list
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id"
    with _connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    out = []
    for r in rows:
        d = _row(r, STUDIO_MESSAGE_COLUMNS)
        for c in ("operator_requests_json", "provenance_json", "usage_json", "detail_json"):
            d[c[:-5]] = _loads(d.pop(c))
        out.append(d)
    return out


def studio_backtest_event_insert(row: dict, audit: Optional[tuple] = None) -> int:
    if row.get("event") not in STUDIO_BACKTEST_EVENTS:
        raise ValueError(f"event: one of {', '.join(STUDIO_BACKTEST_EVENTS)}")
    with _connect() as conn:
        cur = conn.execute("INSERT INTO studio_backtests (session_id, message_id, strategy_id, version, event, "
                           "run_id, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (_text(row.get("session_id"), "session_id", 64), int(row["message_id"]),
                            _text(row.get("strategy_id"), "strategy_id", 64), int(row["version"]), row["event"],
                            row.get("run_id"), row.get("note"), utc_now_iso()))
        if audit is not None:
            _insert_audit(conn, *audit)
        return int(cur.lastrowid)


def studio_backtest_events(session_id: Optional[str] = None) -> List[dict]:
    """All draft-backtest events (oldest first)."""
    sql, args = f"SELECT {', '.join(STUDIO_BACKTEST_COLUMNS)} FROM studio_backtests", []
    if session_id is not None:
        sql += " WHERE session_id = ?"
        args.append(_text(session_id, "session_id", 64))
    sql += " ORDER BY id"
    with _connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_row(r, STUDIO_BACKTEST_COLUMNS) for r in rows]


def memory_facts_insert(run_id: str, rows: List[dict]) -> int:
    """Inserts the memory facts of one finished run in ONE transaction -> the number written; 0 when the
    run already has facts (the finish hook may run twice) or ``rows`` is empty."""
    run_id = _text(run_id, "run_id", 64)
    if not rows:
        return 0
    values = []
    for r in rows:
        if r.get("run_id") != run_id:
            raise ValueError("run_id: every fact belongs to the given run")
        values.append((_text(r.get("family_id"), "family_id", 64), _text(r.get("strategy_id"), "strategy_id", 64),
                       int(r["version"]), run_id, _text(r.get("kind"), "kind", 40),
                       _text(r.get("fact_json"), "fact_json", 100_000), utc_now_iso()))
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM memory_facts WHERE run_id = ? LIMIT 1", (run_id,)).fetchone():
            return 0
        conn.executemany("INSERT INTO memory_facts (family_id, strategy_id, version, run_id, kind, fact_json, "
                         "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", values)
    return len(values)


def memory_facts_list(family_id: Optional[str] = None, strategy_id: Optional[str] = None,
                      limit: int = 200) -> List[dict]:
    """Newest first (decoded ``fact``)."""
    limit = max(1, min(int(limit), 5000))
    sql, args, where = f"SELECT {', '.join(MEMORY_COLUMNS)} FROM memory_facts", [], []
    if family_id is not None:
        where.append("family_id = ?")
        args.append(_text(family_id, "family_id", 64))
    if strategy_id is not None:
        where.append("strategy_id = ?")
        args.append(_text(strategy_id, "strategy_id", 64))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with _connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    out = []
    for r in rows:
        d = _row(r, MEMORY_COLUMNS)
        d["fact"] = _loads(d.pop("fact_json"))
        out.append(d)
    return out


def studio_draft_save_insert(row: dict, audit: tuple) -> dict:
    """Records the confirmed save of a draft (one per message) + audit in ONE transaction."""
    vals = (_text(row.get("session_id"), "session_id", 64), int(row["message_id"]),
            _text(row.get("strategy_id"), "strategy_id", 64), int(row["version"]), utc_now_iso())
    with _connect() as conn:
        conn.execute("INSERT INTO studio_draft_saves (session_id, message_id, strategy_id, version, created_at) "
                     "VALUES (?, ?, ?, ?, ?)", vals)
        _insert_audit(conn, *audit)
    return dict(zip(("session_id", "message_id", "strategy_id", "version", "created_at"), vals))


def studio_draft_saves(session_id: Optional[str] = None) -> Dict[int, dict]:
    """message_id -> confirmed save row."""
    sql, args = "SELECT session_id, message_id, strategy_id, version, created_at FROM studio_draft_saves", []
    if session_id is not None:
        sql += " WHERE session_id = ?"
        args.append(_text(session_id, "session_id", 64))
    with _connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return {int(r["message_id"]): {k: r[k] for k in r.keys()} for r in rows}


# --- stage 5b: papers + committee (SPEC §14, v13) — generic insert-only helpers -------------------------------

STAGE5B_COLUMNS = {
    "papers": ("id", "source_kind", "url", "filename", "sha256", "title", "authors", "year", "text_chars", "pages",
               "created_at"),
    "paper_extractions": ("id", "paper_id", "created_at", "model", "model_tier", "relevant", "result_json",
                          "provenance_json", "usage_json", "cost_usd"),
    "paper_confirmations": ("id", "paper_id", "extraction_id", "created_at", "confirmed_json", "strategy_id",
                            "version", "draft_json"),
    "paper_replications": ("id", "paper_id", "strategy_id", "version", "event", "run_id", "request_json", "note",
                           "created_at"),
    "committee_runs": ("id", "strategy_id", "version", "validation_run_id", "model_tier", "model", "trigger",
                       "holdout_included", "created_at"),
    "committee_messages": ("id", "run_id", "role", "seq", "output_json", "check_json", "provenance_json",
                           "usage_json", "cost_usd", "created_at"),
    "committee_events": ("id", "run_id", "event", "role", "note", "created_at"),
    # stage 6 (SPEC §16, v14) — same generic insert-only helpers
    "loop_runs": ("id", "created_at", "seed_kind", "seed_json", "config_json"),
    "loop_variants": ("id", "loop_id", "generation", "slot", "origin", "parent_variant_id", "created_at"),
    "loop_events": ("id", "loop_id", "event", "generation", "variant_id", "data_json", "created_at"),
    "loop_ai_messages": ("id", "loop_id", "variant_id", "role", "output_json", "check_json", "provenance_json",
                         "usage_json", "cost_usd", "created_at"),
}
COMMITTEE_SETTINGS_AUTO_KEY = "committee_auto_after_validation"
COMMITTEE_SETTINGS_MODEL_KEY = "committee_model"


def _decode_json_cols(d: dict) -> dict:
    for c in [c for c in d if c.endswith("_json")]:
        d[c[:-5]] = _loads(d.pop(c))
    return d


def s5b_insert(table: str, row: dict, audit: Optional[tuple] = None):
    """Inserts one row into a stage-5b table (+ optional audit row) in ONE transaction -> its id (rowid for
    INTEGER keys, the given id for TEXT keys). Unknown columns -> ValueError."""
    cols = STAGE5B_COLUMNS[table]
    row = dict(row)
    row.setdefault("created_at", utc_now_iso())
    for k in row:
        if k not in cols:
            raise ValueError(f"unknown {table} field {k!r}")
    keys = [k for k in cols if k in row]
    with _connect() as conn:
        cur = conn.execute(f"INSERT INTO {table} ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
                           [row[k] for k in keys])
        if audit is not None:
            _insert_audit(conn, *audit)
        return row["id"] if "id" in row else int(cur.lastrowid)


def s5b_insert_many(rows: List[tuple], audit: Optional[tuple] = None) -> List:
    """Inserts [(table, row), …] of the generic insert-only tables (+ optional audit row) in ONE transaction
    (SPEC §16.12 L8: a generation is planned atomically) -> their ids in order."""
    out = []
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for table, row in rows:
            cols = STAGE5B_COLUMNS[table]
            row = dict(row)
            row.setdefault("created_at", utc_now_iso())
            for k in row:
                if k not in cols:
                    raise ValueError(f"unknown {table} field {k!r}")
            keys = [k for k in cols if k in row]
            cur = conn.execute(f"INSERT INTO {table} ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
                               [row[k] for k in keys])
            out.append(row["id"] if "id" in row else int(cur.lastrowid))
        if audit is not None:
            _insert_audit(conn, *audit)
    return out


def s5b_select(table: str, where: Optional[dict] = None, *, order: str = "id", desc: bool = False,
               limit: Optional[int] = None) -> List[dict]:
    """Rows of a stage-5b table (decoded *_json columns)."""
    cols = STAGE5B_COLUMNS[table]
    if order not in cols:
        raise ValueError("order")
    sql, args, conds = f"SELECT {', '.join(cols)} FROM {table}", [], []
    for k, v in (where or {}).items():
        if k not in cols:
            raise ValueError(f"unknown {table} field {k!r}")
        conds.append(f"{k} = ?")
        args.append(v)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += f" ORDER BY {order} {'DESC' if desc else 'ASC'}, rowid {'DESC' if desc else 'ASC'}"
    if limit is not None:
        sql += " LIMIT ?"
        args.append(max(1, min(int(limit), 5000)))
    with _connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_decode_json_cols(_row(r, cols)) for r in rows]


# --- stage 6: discovery loop budget (SPEC §16.6, v14 meta keys) -------------------------------------------------

LOOP_MONTHLY_KEY = "loop_monthly_budget_usd"
LOOP_DAILY_KEY = "loop_daily_budget_usd"
LOOP_RUN_CAP_KEY = "loop_run_cap_usd"
LOOP_AUTONOMOUS_KEY = "loop_autonomous"
LOOP_SPEND_MONTH_PREFIX = "loop_spend_month_usd:"     # + UTC month YYYY-MM
LOOP_SPEND_DAY_PREFIX = "loop_spend_day_usd:"         # + UTC day YYYY-MM-DD
LOOP_SPEND_RUN_PREFIX = "loop_spend_run_usd:"         # + loop id


def _loop_spend_keys(month: str, day: str, loop_id: Optional[str]) -> List[str]:
    keys = [LOOP_SPEND_MONTH_PREFIX + _month(month), LOOP_SPEND_DAY_PREFIX + _day(day)]
    if loop_id is not None:
        keys.append(LOOP_SPEND_RUN_PREFIX + _text(loop_id, "loop_id", 64))
    return keys


def loop_spend_get(month: str, day: str, loop_id: Optional[str] = None):
    """(month, day, run) loop AI spend in USD (run = 0.0 without ``loop_id``); an unreadable stored value
    raises StorageUnavailable (fail closed)."""
    keys = _loop_spend_keys(month, day, loop_id)
    with _connect() as conn:
        rows = {r["key"]: r["value"] for r in conn.execute(
            f"SELECT key, value FROM meta WHERE key IN ({', '.join('?' * len(keys))})", keys).fetchall()}
    vals = [_spend_value(rows.get(k), k) for k in keys]
    return vals[0], vals[1], (vals[2] if loop_id is not None else 0.0)


def loop_spend_add(month: str, day: str, loop_id: Optional[str], usd):
    """Adds ``usd`` to the month, day and run counters in ONE transaction -> (month, day, run) totals."""
    keys = _loop_spend_keys(month, day, loop_id)
    usd = _number(usd, "usd", True)
    if usd < 0:
        raise ValueError("usd: >= 0")
    out = []
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for key in keys:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            total = _spend_value(row["value"] if row else None, key) + usd
            conn.execute("INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                         (key, repr(total), utc_now_iso()))
            out.append(total)
    return out[0], out[1], (out[2] if loop_id is not None else 0.0)
