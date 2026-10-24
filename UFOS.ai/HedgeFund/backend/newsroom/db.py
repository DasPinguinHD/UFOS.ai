"""Local SQLite store for the HedgeFund backend (2026-09-25).

One database per module, in the module's backend data folder:
HedgeFund/backend/data/newsroom.db (git-ignored; the env variable
UFOS_HEDGEFUND_DB overrides the full path, e.g. for tests). Until 2026-09-25
evening the file lived in %LOCALAPPDATA%\\UFOS.ai\\data\\hedgefund.sqlite —
init() copies that legacy file once when the new one doesn't exist yet.

What it keeps (see README "Local SQLite store"):
  insider_filings     every SEC Form 4 filing seen (kept INSIDER_RETENTION_DAYS,
                      default 5 = the Insider Trades display window)
  macro_observations  raw FRED observations (+ Eurostat euro-area IP) per
                      (series_id, units, frequency) — lets the collector fetch
                      incrementally and the cards show instantly at startup
  calendar_snapshot   latest Economic Calendar poll (one row)
  ai_results          every AI output (insider summary, Analysis & Reasoning)
                      with model, provenance and usage
  meta                small key/value state (last prune, last full refetch,
                      last successful poll per source "last_ok:<source>", ...)
  prices, signals, orders, positions, equity_curve
                      schema v2: trading tables (prices pruned after 2 years);
                      v3 extends orders/positions/equity_curve for the paper
                      broker of the HedgeFund loop (fund/SPEC.md §2)
  fund_*              schema v3: the HedgeFund autonomous loop — params
                      versions, runs, agent scores, recommendations, events,
                      audit, cash ledger, backtests (fund/store.py is the
                      only reader/writer). Paper trading only.

Design rules:
- stdlib only (sqlite3), no config import — the module can be loaded by tests
  on its own. A connection per call (cheap for SQLite), WAL journal.
- Nothing here ever raises into the caller: every public function logs and
  returns a neutral default, so the backend falls back to in-memory
  behaviour when the file is locked, corrupt or unwritable.
- Until init() succeeded every function is a no-op (tests that don't call
  init() never touch the user's real database).
- Schema versioning via PRAGMA user_version; MIGRATIONS are idempotent
  (CREATE ... IF NOT EXISTS) and applied in order.
- A corrupt file (not a database / failed quick_check) is renamed to
  "<name>.corrupt-<UTC timestamp>" (with its -wal/-shm) and a fresh database
  is created. A locked or unopenable file is NOT renamed — the backend then
  runs in-memory only (another process may legitimately hold it).
"""
import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

ENV_PATH = "UFOS_HEDGEFUND_DB"
INSIDER_RETENTION_DAYS = 5        # Form 4 filings, by filing time (user decision 2026-09-25)
AI_RESULTS_RETENTION_DAYS = 90    # AI summaries / analyses
CALENDAR_GRACE_DAYS = 1           # calendar events are kept until event date + 1 day
PRICES_RETENTION_DAYS = 730       # trading table "prices" (market data: 2 years)
FUND_RUNS_RETENTION_DAYS = 90     # fund_runs + their fund_agent_scores (by started_at), except runs
                                  # that produced a recommendation or an order (kept: decision inputs)
FUND_EVENTS_RETENTION_DAYS = 30   # fund_events (UI notifications)
DB_FILENAME = "newsroom.db"
BUSY_TIMEOUT_SECONDS = 5


def _add_columns(table: str, columns: list):
    """Migration step: ALTER TABLE ... ADD COLUMN for every column that is not
    there yet — makes ADD COLUMN idempotent (a re-run after a partial
    migration, or with user_version forced back, must not fail)."""
    def step(conn):
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, decl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    step.__name__ = f"add_columns_{table}"
    return step


# version -> statements (SQL strings or callables taking the connection).
# Append new versions; never edit an applied one.
MIGRATIONS = [
    (1, [
        """CREATE TABLE IF NOT EXISTS insider_filings (
               accession     TEXT PRIMARY KEY,
               filed_at      TEXT NOT NULL,      -- UTC ISO-8601 (normalised feed <updated>)
               issuer_ticker TEXT,
               issuer_name   TEXT,
               owner_name    TEXT,
               payload_json  TEXT NOT NULL,      -- the full newsroom event
               first_seen    TEXT NOT NULL,
               last_seen     TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS ix_insider_filed_at ON insider_filings(filed_at)",
        """CREATE TABLE IF NOT EXISTS macro_observations (
               series_id  TEXT NOT NULL,
               units      TEXT NOT NULL,        -- FRED units code ('lin', 'pc1', ...) or source unit
               frequency  TEXT NOT NULL,        -- 'native' = as published
               date       TEXT NOT NULL,        -- YYYY-MM-DD
               value      REAL NOT NULL,
               fetched_at TEXT NOT NULL,
               PRIMARY KEY (series_id, units, frequency, date))""",
        """CREATE TABLE IF NOT EXISTS calendar_snapshot (
               id           INTEGER PRIMARY KEY CHECK (id = 1),
               payload_json TEXT NOT NULL,
               fetched_at   TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS ai_results (
               id              INTEGER PRIMARY KEY AUTOINCREMENT,
               created_at      TEXT NOT NULL,   -- UTC ISO-8601
               feature         TEXT NOT NULL,   -- 'insider-summary' | 'analysis'
               model           TEXT,
               tier            TEXT,
               input_summary   TEXT,            -- JSON
               output_text     TEXT,
               provenance_json TEXT,
               usage_json      TEXT)""",
        "CREATE INDEX IF NOT EXISTS ix_ai_results_created ON ai_results(created_at)",
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
    ]),
    # v2 (2026-09-25): data model for the upcoming Hedge Fund trading work —
    # tables only, nothing reads or writes them yet. prices are pruned after
    # PRICES_RETENTION_DAYS; signals/orders/positions/equity_curve are kept.
    (2, [
        """CREATE TABLE IF NOT EXISTS prices (
               symbol TEXT NOT NULL,
               ts     TEXT NOT NULL,            -- UTC ISO-8601 bar time
               open REAL, high REAL, low REAL, close REAL, volume REAL,
               source TEXT,
               PRIMARY KEY (symbol, ts))""",
        """CREATE TABLE IF NOT EXISTS signals (
               id           INTEGER PRIMARY KEY,
               ts           TEXT NOT NULL,
               source       TEXT NOT NULL,      -- e.g. 'macro-regime', 'insider-cluster', an agent name
               symbol       TEXT,               -- NULL = market-wide
               kind         TEXT NOT NULL,
               value        REAL,
               level        TEXT,
               payload_json TEXT)""",
        "CREATE INDEX IF NOT EXISTS ix_signals_ts ON signals(ts)",
        """CREATE TABLE IF NOT EXISTS orders (
               id              INTEGER PRIMARY KEY,
               ts              TEXT NOT NULL,
               symbol          TEXT NOT NULL,
               side            TEXT NOT NULL,   -- 'buy' | 'sell'
               qty             REAL NOT NULL,
               order_type      TEXT NOT NULL,   -- 'market' | 'limit' | ...
               limit_price     REAL,
               status          TEXT NOT NULL,
               broker          TEXT,
               broker_order_id TEXT,
               mode            TEXT NOT NULL DEFAULT 'paper' CHECK (mode IN ('paper', 'live')),
               rationale       TEXT,
               provenance_json TEXT)""",
        "CREATE INDEX IF NOT EXISTS ix_orders_ts ON orders(ts)",
        """CREATE TABLE IF NOT EXISTS positions (
               symbol     TEXT PRIMARY KEY,
               qty        REAL NOT NULL,
               avg_price  REAL,
               updated_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS equity_curve (
               ts     TEXT PRIMARY KEY,
               equity REAL NOT NULL,
               cash   REAL,
               mode   TEXT NOT NULL DEFAULT 'paper')""",
    ]),
    # v3 (2026-09-26): HedgeFund autonomous loop (fund/SPEC.md §2) — paper trading only.
    (3, [
        """CREATE TABLE IF NOT EXISTS fund_params (
               version    INTEGER PRIMARY KEY,
               created_at TEXT NOT NULL,
               params_json TEXT NOT NULL,
               actor      TEXT NOT NULL,
               note       TEXT)""",
        """CREATE TABLE IF NOT EXISTS fund_runs (
               id             INTEGER PRIMARY KEY,
               started_at     TEXT NOT NULL,
               finished_at    TEXT,
               params_version INTEGER NOT NULL,
               trigger        TEXT NOT NULL,     -- schedule|manual|startup
               status         TEXT NOT NULL,     -- running|ok|partial|error|skipped
               market_open    INTEGER NOT NULL,
               judge_mode     TEXT,              -- llm|rules|mixed|none
               models         TEXT,
               cost_usd       REAL NOT NULL DEFAULT 0,
               error          TEXT,
               summary_json   TEXT)""",
        "CREATE INDEX IF NOT EXISTS ix_fund_runs_started ON fund_runs(started_at)",
        """CREATE TABLE IF NOT EXISTS fund_agent_scores (
               run_id          INTEGER NOT NULL,
               symbol          TEXT NOT NULL,
               agent           TEXT NOT NULL,
               score           REAL NOT NULL,
               confidence      REAL NOT NULL,
               summary         TEXT,
               details_json    TEXT,
               source          TEXT NOT NULL,    -- rules|llm|cached|unavailable
               provenance_json TEXT,
               PRIMARY KEY (run_id, symbol, agent))""",
        """CREATE TABLE IF NOT EXISTS fund_recommendations (
               id              INTEGER PRIMARY KEY,
               run_id          INTEGER NOT NULL,
               ts              TEXT NOT NULL,
               symbol          TEXT NOT NULL,
               action          TEXT NOT NULL,    -- LONG|SHORT|FLAT
               confidence      REAL NOT NULL,
               target_weight   REAL NOT NULL,    -- signed fraction of equity
               rule_score      REAL,
               rationale       TEXT,
               source          TEXT NOT NULL,    -- llm|rules|carried
               model           TEXT,
               provenance_json TEXT,
               usage_json      TEXT,
               status          TEXT NOT NULL DEFAULT 'open',  -- open|executed|skipped|superseded
               skip_reason     TEXT,
               order_id        INTEGER)""",
        "CREATE INDEX IF NOT EXISTS ix_fund_recs_symbol ON fund_recommendations(symbol, id)",
        "CREATE INDEX IF NOT EXISTS ix_fund_recs_run ON fund_recommendations(run_id)",
        """CREATE TABLE IF NOT EXISTS fund_events (
               id     INTEGER PRIMARY KEY,
               ts     TEXT NOT NULL,
               kind   TEXT NOT NULL,             -- flip|order|halt|margin_call|resume|error|autopilot
               symbol TEXT,
               title  TEXT NOT NULL,
               body   TEXT)""",
        "CREATE INDEX IF NOT EXISTS ix_fund_events_ts ON fund_events(ts)",
        """CREATE TABLE IF NOT EXISTS fund_audit (
               id          INTEGER PRIMARY KEY,
               ts          TEXT NOT NULL,
               actor       TEXT NOT NULL,        -- user|engine|system
               action      TEXT NOT NULL,
               detail_json TEXT)""",
        """CREATE TABLE IF NOT EXISTS fund_cash_ledger (
               id     INTEGER PRIMARY KEY,
               ts     TEXT NOT NULL,
               kind   TEXT NOT NULL,             -- deposit|reset|fill|fee|borrow_fee|dividend|dividend_short
               symbol TEXT,
               amount REAL NOT NULL,
               note   TEXT)""",
        """CREATE TABLE IF NOT EXISTS fund_backtests (
               id           INTEGER PRIMARY KEY,
               created_at   TEXT NOT NULL,
               request_json TEXT NOT NULL,
               result_json  TEXT NOT NULL)""",
        _add_columns("orders", [("run_id", "INTEGER"), ("recommendation_id", "INTEGER"), ("origin", "TEXT"),
                                ("fill_price", "REAL"), ("fees", "REAL"), ("filled_at", "TEXT"),
                                ("reject_reason", "TEXT"), ("notional", "REAL")]),
        _add_columns("positions", [("opened_at", "TEXT"), ("realized_pnl", "REAL DEFAULT 0"),
                                   ("borrow_rate_pct", "REAL"), ("last_price", "REAL")]),
        _add_columns("equity_curve", [("long_mv", "REAL"), ("short_mv", "REAL"), ("benchmark", "REAL"),
                                      ("drawdown_pct", "REAL")]),
        "CREATE INDEX IF NOT EXISTS ix_orders_status ON orders(status)",
    ]),
]
TABLES = ("insider_filings", "macro_observations", "calendar_snapshot", "ai_results", "meta",
          "prices", "signals", "orders", "positions", "equity_curve",
          "fund_params", "fund_runs", "fund_agent_scores", "fund_recommendations", "fund_events",
          "fund_audit", "fund_cash_ledger", "fund_backtests")
SCHEMA_VERSION = MIGRATIONS[-1][0]

_path = None
_init_lock = threading.Lock()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_path() -> str:
    """HedgeFund/backend/data/newsroom.db (env UFOS_HEDGEFUND_DB overrides) — in sandbox mode ALWAYS
    data/sandbox/newsroom.db (sandbox_profile.py; the env override is ignored there, it could name the real file)."""
    import sandbox_profile
    override = os.environ.get(ENV_PATH)
    if sandbox_profile.is_sandbox():
        if override:
            logger.warning("%s is set but ignored in sandbox mode — the sandbox copy is used", ENV_PATH)
        return sandbox_profile.db_file(DB_FILENAME)
    if override:
        return override
    return sandbox_profile.db_file(DB_FILENAME)


def legacy_path():
    """Where the store lived before (LOCALAPPDATA); None without LOCALAPPDATA."""
    base = os.environ.get("LOCALAPPDATA")
    return os.path.join(base, "UFOS.ai", "data", "hedgefund.sqlite") if base else None


def last_ok_key(source: str) -> str:
    """meta key holding the UTC ISO time of a source's last successful poll."""
    return f"last_ok:{source}"


def db_path():
    """Path of the open database, or None when the store is disabled."""
    return _path


def is_enabled() -> bool:
    return _path is not None


def _open(path: str) -> sqlite3.Connection:
    # A connection per call and closed right after, so check_same_thread=False
    # only matters if a caller ever keeps one (asyncio.to_thread workers).
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL + NORMAL: no fsync per small commit (the insider poll writes one row per
    # filing from the event loop), still crash-safe for the database file.
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def _connect():
    conn = _open(_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def migrate(conn: sqlite3.Connection) -> int:
    """Brings the schema to SCHEMA_VERSION; safe to call any number of times."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, statements in MIGRATIONS:
        if version <= current:
            continue
        for sql in statements:
            if callable(sql):
                sql(conn)
            else:
                conn.execute(sql)
        conn.execute(f"PRAGMA user_version = {int(version)}")
        conn.commit()
        current = version
    return current


class CorruptDatabase(Exception):
    pass


def _open_and_migrate(target: str) -> int:
    conn = None
    try:
        # Only open + pragmas + quick_check are classified: an error from
        # migrate() below must never get a good file quarantined.
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
        version = migrate(conn)
        conn.commit()
        return version
    finally:
        conn.close()


def _quarantine(target: str, now: datetime = None) -> str:
    """Renames a corrupt database (and its -wal/-shm) out of the way."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    moved = f"{target}.corrupt-{stamp}"
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(target + suffix):
            os.replace(target + suffix, moved + suffix)
    return moved


def _copy_legacy(legacy: str, target: str) -> bool:
    """One-time copy of the pre-2026-09-25-evening store (sqlite backup API,
    consistent even with a -wal next to it). The legacy file is left in place."""
    try:
        src = sqlite3.connect(f"file:{legacy}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_SECONDS)
        try:
            dst = sqlite3.connect(target)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        logger.info("SQLite store: copied the legacy database %s -> %s (the old file can be deleted)", legacy, target)
        return True
    except Exception as ex:
        logger.warning("SQLite store: copying the legacy database %s failed (%s) — starting fresh", legacy, ex)
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.remove(target + suffix)
            except OSError:
                pass
        return False


def init(path: str = None, legacy: str = None) -> bool:
    """Opens (creates) the database, sets WAL and migrates. A corrupt file is
    quarantined (renamed) and replaced by a fresh one. Returns False — and
    leaves the store disabled — on any other error (locked, no permission).
    legacy: file to copy once when target doesn't exist yet (default: the
    old LOCALAPPDATA store, only when neither path nor UFOS_HEDGEFUND_DB is set)."""
    global _path
    with _init_lock:
        target = path or default_path()
        if legacy is None and path is None and not os.environ.get(ENV_PATH):
            legacy = legacy_path()
        try:
            folder = os.path.dirname(os.path.abspath(target))
            os.makedirs(folder, exist_ok=True)
            if legacy and not os.path.exists(target) and os.path.isfile(legacy):
                _copy_legacy(legacy, target)
            try:
                version = _open_and_migrate(target)
            except CorruptDatabase as ex:
                moved = _quarantine(target)
                logger.error("SQLite store %s is corrupt (%s) — renamed to %s, starting with a fresh database",
                             target, ex, moved)
                version = _open_and_migrate(target)
            _path = target
            logger.info("SQLite store ready: %s (schema v%d)", target, version)
            return True
        except Exception as ex:
            _path = None
            logger.error("SQLite store unavailable (%s): %s — running in-memory only", target, ex)
            return False


def close() -> None:
    """Disables the store (tests)."""
    global _path
    _path = None


def _guard(default):
    """Decorator: no-op while disabled, never raise."""
    def wrap(fn):
        def inner(*args, **kwargs):
            if _path is None:
                return default() if callable(default) else default
            try:
                return fn(*args, **kwargs)
            except Exception as ex:
                logger.warning("SQLite %s failed: %s", fn.__name__, ex)
                return default() if callable(default) else default
        inner.__name__ = fn.__name__
        inner.__doc__ = fn.__doc__
        return inner
    return wrap


def normalise_timestamp(text, fallback: str = None) -> str:
    """ISO-8601 with any offset -> UTC ISO-8601 (seconds). Unparsable -> fallback/now."""
    try:
        dt = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return fallback or utc_now_iso()


# --- meta ---

@_guard(None)
def meta_get(key: str):
    with _connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None


@_guard(False)
def meta_set(key: str, value) -> bool:
    with _connect() as conn:
        conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))
    return True


@_guard(dict)
def meta_prefix(prefix: str) -> dict:
    with _connect() as conn:
        rows = conn.execute("SELECT key, value FROM meta WHERE key LIKE ?", (prefix.replace("%", "") + "%",)).fetchall()
        return {r[0]: r[1] for r in rows}


# --- insider filings ---

def _insider_row(event: dict, now: str):
    accession = event["id"][len("insider-"):] if str(event.get("id", "")).startswith("insider-") else str(event["id"])
    d = event.get("details") or {}
    filed = normalise_timestamp(event.get("filed_at"), fallback=now)
    return {"accession": accession, "filed_at": filed, "ticker": d.get("issuer_ticker") or None,
            "issuer": d.get("issuer_name") or None, "owner": d.get("owner_name") or None,
            "payload": json.dumps(event, ensure_ascii=False, default=str), "now": now,
            "has_details": 1 if event.get("details") else 0}


@_guard(0)
def upsert_insider_many(events: list, now: str = None) -> int:
    """New filings are inserted; known ones get last_seen (and payload, in case
    the details were parsed only now) updated. first_seen never changes."""
    now = now or utc_now_iso()
    rows = [_insider_row(e, now) for e in events if e.get("id")]
    if not rows:
        return 0
    with _connect() as conn:
        conn.executemany(
            """INSERT INTO insider_filings(accession, filed_at, issuer_ticker, issuer_name, owner_name,
                                           payload_json, first_seen, last_seen)
               VALUES (:accession, :filed_at, :ticker, :issuer, :owner, :payload, :now, :now)
               ON CONFLICT(accession) DO UPDATE SET
                   last_seen = excluded.last_seen,
                   -- keep a parsed payload; replace a detail-less one once details exist
                   payload_json = CASE WHEN :has_details = 1 THEN excluded.payload_json
                                       ELSE insider_filings.payload_json END,
                   issuer_ticker = COALESCE(excluded.issuer_ticker, insider_filings.issuer_ticker),
                   issuer_name = COALESCE(excluded.issuer_name, insider_filings.issuer_name),
                   owner_name = COALESCE(excluded.owner_name, insider_filings.owner_name)""",
            rows)
    return len(rows)


def upsert_insider(event: dict, now: str = None) -> int:
    return upsert_insider_many([event], now)


@_guard(list)
def load_insider(since_iso: str) -> list:
    """Stored events (payload dicts) filed at or after since_iso (UTC ISO), newest first."""
    with _connect() as conn:
        rows = conn.execute("SELECT payload_json FROM insider_filings WHERE filed_at >= ? ORDER BY filed_at DESC",
                            (normalise_timestamp(since_iso),)).fetchall()
    out = []
    for r in rows:
        try:
            out.append(json.loads(r[0]))
        except Exception:
            continue
    return out


@_guard(0)
def count_rows(table: str) -> int:
    if table not in TABLES:
        raise ValueError(table)
    with _connect() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


@_guard(None)
def storage_info():
    """{"path", "size_bytes" (db + -wal), "schema_version", "rows": {table: n}}
    for GET /newsroom/status; None while the store is disabled."""
    size = 0
    for suffix in ("", "-wal"):
        try:
            size += os.path.getsize(_path + suffix)
        except OSError:
            pass
    with _connect() as conn:
        rows = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    return {"path": _path, "size_bytes": size, "schema_version": version, "rows": rows}


# --- macro observations ---

@_guard(0)
def replace_observations(series_id: str, units: str, frequency: str, points: list,
                         from_date: str = None, full: bool = False, fetched_at: str = None) -> int:
    """Upserts points ([{"date", "value"}]). full=True replaces the whole
    series; from_date replaces everything on/after that date (FRED revisions,
    withdrawn values). One transaction."""
    fetched_at = fetched_at or utc_now_iso()
    with _connect() as conn:
        if full:
            conn.execute("DELETE FROM macro_observations WHERE series_id=? AND units=? AND frequency=?",
                         (series_id, units, frequency))
        elif from_date and points:
            conn.execute("DELETE FROM macro_observations WHERE series_id=? AND units=? AND frequency=? AND date >= ?",
                         (series_id, units, frequency, from_date))
        conn.executemany(
            "INSERT OR REPLACE INTO macro_observations(series_id, units, frequency, date, value, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(series_id, units, frequency, p["date"], float(p["value"]), fetched_at) for p in points])
    return len(points)


@_guard(dict)
def load_observations(frequency: str = None) -> dict:
    """(series_id, units, frequency) -> [{"date", "value"}] oldest first."""
    sql = "SELECT series_id, units, frequency, date, value FROM macro_observations"
    params = ()
    if frequency:
        sql += " WHERE frequency = ?"
        params = (frequency,)
    sql += " ORDER BY series_id, units, frequency, date"
    out: dict = {}
    with _connect() as conn:
        for r in conn.execute(sql, params):
            out.setdefault((r[0], r[1], r[2]), []).append({"date": r[3], "value": r[4]})
    return out


@_guard(None)
def latest_observation_date(series_id: str, units: str, frequency: str):
    with _connect() as conn:
        row = conn.execute("SELECT MAX(date) FROM macro_observations WHERE series_id=? AND units=? AND frequency=?",
                           (series_id, units, frequency)).fetchone()
        return row[0] if row else None


# --- calendar snapshot ---

@_guard(False)
def save_calendar(payload: dict) -> bool:
    with _connect() as conn:
        conn.execute("INSERT INTO calendar_snapshot(id, payload_json, fetched_at) VALUES (1, ?, ?) "
                     "ON CONFLICT(id) DO UPDATE SET payload_json = excluded.payload_json, "
                     "fetched_at = excluded.fetched_at",
                     (json.dumps(payload, ensure_ascii=False, default=str), utc_now_iso()))
    return True


@_guard(None)
def load_calendar():
    with _connect() as conn:
        row = conn.execute("SELECT payload_json FROM calendar_snapshot WHERE id = 1").fetchone()
    return json.loads(row[0]) if row else None


# --- AI results ---

def _dumps(value):
    if value is None:
        return None
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


@_guard(None)
def insert_ai_result(feature: str, model: str, tier: str, input_summary, output_text: str,
                     provenance=None, usage=None, created_at: str = None):
    """Stores one AI output; returns the new row id."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO ai_results(created_at, feature, model, tier, input_summary, output_text, "
            "provenance_json, usage_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (created_at or utc_now_iso(), feature, model, tier, _dumps(input_summary), output_text,
             _dumps(provenance), _dumps(usage)))
        return cur.lastrowid


def _loads(text):
    if text is None:
        return None
    try:
        return json.loads(text)
    except Exception:
        return text


@_guard(list)
def ai_history(feature: str = None, limit: int = 20) -> list:
    """Newest first. JSON columns are decoded."""
    limit = max(1, min(int(limit), 200))
    sql = "SELECT * FROM ai_results"
    params: tuple = ()
    if feature:
        sql += " WHERE feature = ?"
        params = (feature,)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    with _connect() as conn:
        rows = conn.execute(sql, params + (limit,)).fetchall()
    return [{"id": r["id"], "created_at": r["created_at"], "feature": r["feature"], "model": r["model"],
             "tier": r["tier"], "input_summary": _loads(r["input_summary"]), "output_text": r["output_text"],
             "provenance": _loads(r["provenance_json"]), "usage": _loads(r["usage_json"])} for r in rows]


# --- retention ---

@_guard(dict)
def prune(now: datetime = None, insider_days: int = INSIDER_RETENTION_DAYS,
          ai_days: int = AI_RESULTS_RETENTION_DAYS, calendar_grace_days: int = CALENDAR_GRACE_DAYS,
          prices_days: int = PRICES_RETENTION_DAYS, fund_runs_days: int = FUND_RUNS_RETENTION_DAYS,
          fund_events_days: int = FUND_EVENTS_RETENTION_DAYS) -> dict:
    """Retention run (startup + daily):
      insider filings older than insider_days (by filed_at, which falls back
        to first_seen at insert time), AI results older than ai_days,
      calendar snapshot events whose date is more than calendar_grace_days
        in the past, prices older than prices_days.
      HedgeFund loop: fund_runs older than fund_runs_days (by started_at)
        together with their fund_agent_scores — but only runs that produced
        no recommendation and no order: the runs and agent scores behind
        kept recommendations/orders are their decision inputs (audit trail,
        decisions export) and stay; fund_events older than fund_events_days.
    Kept: macro_observations (FRED's own history since 2000 = reference data),
    signals / orders / positions / equity_curve, fund recommendations, cash
    ledger, audit, params versions and backtests.
    Returns {"insider", "ai_results", "calendar_events", "prices"}; the keys
    "fund_runs", "fund_agent_scores", "fund_events" are added only when rows
    of that kind were removed (keeps the v2 result shape stable)."""
    now = now or datetime.now(timezone.utc)
    insider_cut = (now - timedelta(days=insider_days)).astimezone(timezone.utc).isoformat(timespec="seconds")
    ai_cut = (now - timedelta(days=ai_days)).astimezone(timezone.utc).isoformat(timespec="seconds")
    prices_cut = (now - timedelta(days=prices_days)).astimezone(timezone.utc).isoformat(timespec="seconds")
    calendar_cut = (now - timedelta(days=calendar_grace_days)).astimezone(timezone.utc).date().isoformat()
    runs_cut = (now - timedelta(days=fund_runs_days)).astimezone(timezone.utc).isoformat(timespec="seconds")
    events_cut = (now - timedelta(days=fund_events_days)).astimezone(timezone.utc).isoformat(timespec="seconds")
    fund = {}
    with _connect() as conn:
        n1 = conn.execute("DELETE FROM insider_filings WHERE filed_at < ?", (insider_cut,)).rowcount
        n2 = conn.execute("DELETE FROM ai_results WHERE created_at < ?", (ai_cut,)).rowcount
        n4 = conn.execute("DELETE FROM prices WHERE ts < ?", (prices_cut,)).rowcount
        # runs without any recommendation/order (e.g. skipped or failed cycles) are prunable
        prunable = ("SELECT id FROM fund_runs r WHERE r.started_at < ? "
                    "AND NOT EXISTS (SELECT 1 FROM fund_recommendations x WHERE x.run_id = r.id) "
                    "AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.run_id = r.id)")
        fund["fund_agent_scores"] = conn.execute(
            f"DELETE FROM fund_agent_scores WHERE run_id IN ({prunable})", (runs_cut,)).rowcount
        fund["fund_runs"] = conn.execute(f"DELETE FROM fund_runs WHERE id IN ({prunable})", (runs_cut,)).rowcount
        fund["fund_events"] = conn.execute("DELETE FROM fund_events WHERE ts < ?", (events_cut,)).rowcount
        n3 = 0
        row = conn.execute("SELECT payload_json FROM calendar_snapshot WHERE id = 1").fetchone()
        if row:
            try:
                payload = json.loads(row[0])
                events = payload.get("events") or []
                keep = [e for e in events if not (e.get("date") or "") or str(e.get("date"))[:10] >= calendar_cut]
                n3 = len(events) - len(keep)
                if n3:
                    payload["events"] = keep
                    conn.execute("UPDATE calendar_snapshot SET payload_json = ? WHERE id = 1",
                                 (json.dumps(payload, ensure_ascii=False, default=str),))
            except (ValueError, AttributeError, TypeError) as ex:
                logger.warning("SQLite prune: calendar snapshot unreadable (%s) — left as is", ex)
        conn.execute("INSERT INTO meta(key, value) VALUES ('last_prune', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (now.astimezone(timezone.utc).isoformat(timespec="seconds"),))
    if n1 or n2 or n3 or n4:
        logger.info("SQLite prune: %d insider filing(s), %d AI result(s), %d past calendar event(s), "
                    "%d price row(s) removed", n1, n2, n3, n4)
    fund = {k: v for k, v in fund.items() if v}
    if fund:
        logger.info("SQLite prune (fund): %s", ", ".join(f"{v} {k}" for k, v in sorted(fund.items())))
    result = {"insider": n1, "ai_results": n2, "calendar_events": n3, "prices": n4}
    result.update(fund)
    return result
