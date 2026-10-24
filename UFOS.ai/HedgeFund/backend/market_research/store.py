"""market_research.db — the Research section's own SQLite store (SPEC §6).

File: ``HedgeFund/backend/data/market_research.db`` (git-ignored ``*.db``);
tests pass their own path to ``init(path)``. Same robustness rules as
``strategy_platform/storage/db.py``:

- WAL journal, a connection per call, ``PRAGMA user_version`` migrations
  (``MIGRATIONS``: append-only; each version runs in ONE transaction).
- A corrupt file (not a database / failed quick_check) is renamed to
  ``market_research.db.corrupt-<UTC timestamp>`` (with -wal/-shm) and
  recreated. A locked/unopenable file is NOT renamed; ``init`` raises and
  later calls retry opening at most every ``REOPEN_INTERVAL_SECONDS``.
  A schema newer than this code is refused (file untouched).
- "Not found" → ``None`` / ``[]``; any failure → ``StoreUnavailable``
  (a ``UserFacingError``); invalid arguments → ``ValueError``. Nothing is
  swallowed into a default value. ``status()`` never raises (the
  supervisor and GET /research/feeds/status read it).
- Times: UTC ISO-8601 strings with seconds.

Stdlib only; part of UFOS.ai, a proof-of-concept portfolio project.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time as _time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from strategy_platform.core.errors import UserFacingError

from . import limits

logger = logging.getLogger(__name__)

DB_FILENAME = limits.DB_FILENAME
BUSY_TIMEOUT_SECONDS = 5
REOPEN_INTERVAL_SECONDS = 30
AUDIT_ACTORS = ("user", "system")


class StoreUnavailable(UserFacingError):
    """market_research.db could not be opened, read or written."""

    def __init__(self, reason: str, *, detail: str = ""):
        super().__init__(f"The Research database ({DB_FILENAME}) is unavailable: {reason}. "
                         "Research is paused — trading is not affected.", detail=detail)
        self.reason = reason


class CorruptDatabase(Exception):
    pass


class NewerSchema(Exception):
    pass


def _begin(conn: sqlite3.Connection) -> None:
    if not conn.in_transaction:
        conn.execute("BEGIN")


def _add_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """Idempotent ALTER TABLE … ADD COLUMN (SQLite has no IF NOT EXISTS for columns)."""
    cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
    if column not in cols:
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {decl}')


# Append-only. Stage plan for later versions: SPEC §6 table.
MIGRATIONS = [
    (1, [
        _begin,
        """CREATE TABLE IF NOT EXISTS meta (
               key        TEXT PRIMARY KEY,
               value      TEXT NOT NULL,
               updated_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS audit (
               id          INTEGER PRIMARY KEY AUTOINCREMENT,
               ts          TEXT NOT NULL,
               actor       TEXT NOT NULL,
               action      TEXT NOT NULL,
               detail_json TEXT)""",
        "CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts)",
    ]),
    (2, [   # MR2 (SPEC §6): asset list + Research's own 1m bars (30-day retention) and their coverage per day
        _begin,
        """CREATE TABLE IF NOT EXISTS assets (
               symbol      TEXT PRIMARY KEY,
               name        TEXT NOT NULL,
               asset_class TEXT NOT NULL CHECK (asset_class IN ('equity', 'crypto')),
               kind        TEXT NOT NULL CHECK (kind IN ('stock', 'etf', 'crypto')),
               exchange    TEXT,
               tradable    INTEGER NOT NULL,
               status      TEXT NOT NULL,
               fetched_at  TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS bars_1m (
               symbol      TEXT NOT NULL,
               ts          TEXT NOT NULL,
               open        REAL NOT NULL,
               high        REAL NOT NULL,
               low         REAL NOT NULL,
               close       REAL NOT NULL,
               volume      REAL,
               vwap        REAL,
               trade_count INTEGER,
               feed        TEXT NOT NULL CHECK (feed IN ('sip', 'iex', 'crypto')),
               fetched_at  TEXT NOT NULL,
               PRIMARY KEY (symbol, ts, feed))""",
        "CREATE INDEX IF NOT EXISTS bars_1m_ts ON bars_1m(ts)",
        """CREATE TABLE IF NOT EXISTS bars_1m_days (
               symbol      TEXT NOT NULL,
               day         TEXT NOT NULL,
               feed        TEXT NOT NULL,
               through     TEXT NOT NULL,
               fetched_at  TEXT NOT NULL,
               PRIMARY KEY (symbol, day, feed))""",
    ]),
    (3, [   # MR4 (SPEC §6, §13 MR4 backend notes): watchlists (= panel groups), their ordered items, the
            # restorable session (one row) and the Ctrl+K palette's recent symbols. UI preferences: no audit.
        _begin,
        """CREATE TABLE IF NOT EXISTS watchlists (
               id          TEXT PRIMARY KEY,
               name        TEXT NOT NULL,
               position    INTEGER NOT NULL,
               collapsed   INTEGER NOT NULL DEFAULT 0,
               created_at  TEXT NOT NULL,
               updated_at  TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS watchlist_items (
               list_id     TEXT NOT NULL REFERENCES watchlists(id) ON DELETE CASCADE,
               symbol      TEXT NOT NULL,
               position    INTEGER NOT NULL,
               added_at    TEXT NOT NULL,
               PRIMARY KEY (list_id, symbol))""",
        "CREATE INDEX IF NOT EXISTS watchlist_items_symbol ON watchlist_items(symbol)",
        """CREATE TABLE IF NOT EXISTS session (
               id          INTEGER PRIMARY KEY CHECK (id = 1),
               state_json  TEXT NOT NULL,
               updated_at  TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS recent_symbols (
               symbol      TEXT PRIMARY KEY,
               seq         INTEGER NOT NULL,
               used_at     TEXT NOT NULL)""",
    ]),
    (4, [   # MR5 (SPEC §6, §13 MR5 backend notes): drawings + their split rescales (log) + chart settings
        _begin,
        """CREATE TABLE IF NOT EXISTS drawings (
               id              TEXT PRIMARY KEY,
               symbol          TEXT NOT NULL,
               tool            TEXT NOT NULL,
               points_json     TEXT NOT NULL,
               style_json      TEXT NOT NULL,
               name            TEXT,
               text            TEXT,
               locked          INTEGER NOT NULL,
               visible         INTEGER NOT NULL,
               timeframes_json TEXT NOT NULL,
               split_basis     REAL NOT NULL DEFAULT 1.0,
               basis_as_of     TEXT,
               rescales        INTEGER NOT NULL DEFAULT 0,
               alert_id        TEXT,
               created_at      TEXT NOT NULL,
               updated_at      TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS drawings_symbol ON drawings(symbol)",
        """CREATE TABLE IF NOT EXISTS drawing_rescales (
               id          INTEGER PRIMARY KEY AUTOINCREMENT,
               drawing_id  TEXT NOT NULL,
               action_key  TEXT NOT NULL,
               ts          TEXT NOT NULL,
               factor      REAL NOT NULL,
               ex_date     TEXT NOT NULL,
               detail_json TEXT,
               UNIQUE (drawing_id, action_key))""",
        """CREATE TABLE IF NOT EXISTS chart_settings (
               symbol        TEXT PRIMARY KEY,
               settings_json TEXT NOT NULL,
               updated_at    TEXT NOT NULL)""",
    ]),
    (5, [   # MR6 (SPEC §6, §13 MR6 backend notes): news (7 days, de-dup by Alpaca id), symbol index, thumbnail
            # cache (follows the news retention), logos (30 days; 'none' = Alpaca has no logo, 7 days)
        _begin,
        """CREATE TABLE IF NOT EXISTS news (
               id           INTEGER PRIMARY KEY,
               symbols_json TEXT NOT NULL,
               headline     TEXT NOT NULL,
               summary      TEXT,
               publisher    TEXT,
               url          TEXT,
               images_json  TEXT NOT NULL,
               created_at   TEXT NOT NULL,
               updated_at   TEXT,
               fetched_at   TEXT NOT NULL,
               origin       TEXT NOT NULL CHECK (origin IN ('rest', 'stream')))""",
        "CREATE INDEX IF NOT EXISTS news_created ON news(created_at)",
        """CREATE TABLE IF NOT EXISTS news_symbols (
               id         INTEGER NOT NULL,
               symbol     TEXT NOT NULL,
               created_at TEXT NOT NULL,
               PRIMARY KEY (id, symbol))""",
        "CREATE INDEX IF NOT EXISTS news_symbols_symbol ON news_symbols(symbol, created_at)",
        """CREATE TABLE IF NOT EXISTS news_images (
               key          TEXT PRIMARY KEY,
               url          TEXT NOT NULL,
               created_at   TEXT NOT NULL,
               status       TEXT NOT NULL CHECK (status IN ('pending', 'ok', 'failed', 'rejected')),
               content_type TEXT,
               data         BLOB,
               reason       TEXT,
               fetched_at   TEXT)""",
        """CREATE TABLE IF NOT EXISTS logos (
               symbol     TEXT PRIMARY KEY,
               png        BLOB,
               status     TEXT NOT NULL CHECK (status IN ('ok', 'none')),
               fetched_at TEXT NOT NULL)""",
    ]),
    (6, [   # MR5 review M3 (SPEC §6, §13 MR5 notes): idempotency key of POST /research/drawings and imports
        _begin,
        lambda conn: _add_column(conn, "drawings", "client_id", "TEXT"),
        "CREATE UNIQUE INDEX IF NOT EXISTS drawings_client_id ON drawings(client_id)",
    ]),
    (7, [   # MR7 (SPEC §6, §13 MR7 backend notes): alert rules (until deleted) + alert log (90 days)
        _begin,
        """CREATE TABLE IF NOT EXISTS alerts (
               id                TEXT PRIMARY KEY,
               symbol            TEXT NOT NULL,
               kind              TEXT NOT NULL CHECK (kind IN ('price_cross', 'line_cross', 'day_move', 'news')),
               params_json       TEXT NOT NULL,
               cooldown_seconds  INTEGER NOT NULL,
               enabled           INTEGER NOT NULL,
               drawing_id        TEXT,
               last_triggered_at TEXT,
               state_json        TEXT NOT NULL DEFAULT '{}',
               disabled_reason   TEXT,
               created_at        TEXT NOT NULL,
               updated_at        TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS alerts_symbol ON alerts(symbol)",
        "CREATE UNIQUE INDEX IF NOT EXISTS alerts_drawing ON alerts(drawing_id) WHERE drawing_id IS NOT NULL",
        """CREATE TABLE IF NOT EXISTS alert_log (
               id          INTEGER PRIMARY KEY AUTOINCREMENT,
               alert_id    TEXT NOT NULL,
               symbol      TEXT NOT NULL,
               ts          TEXT NOT NULL,
               kind        TEXT NOT NULL,
               message     TEXT NOT NULL,
               price       REAL,
               detail_json TEXT)""",
        "CREATE INDEX IF NOT EXISTS alert_log_ts ON alert_log(ts)",
        "CREATE INDEX IF NOT EXISTS alert_log_alert ON alert_log(alert_id, ts)",
    ]),
]
SCHEMA_VERSION = max(v for v, _ in MIGRATIONS)   # MR5: stages may land out of order

# (table, time column) pruned by RETENTION_DAYS[table]; tables that don't exist yet are skipped.
PRUNE_COLUMNS = (("bars_1m", "ts"), ("bars_1m_days", "day"), ("news", "created_at"), ("news_symbols", "created_at"),
                 ("news_images", "created_at"), ("logos", "fetched_at"), ("alert_log", "ts"), ("audit", "ts"))

_lock = threading.Lock()
_path: Optional[str] = None
_wanted: Optional[str] = None
_last_attempt = 0.0
_last_error = ""             # user-facing reason of the last failed open
_opening = False             # S10-H: an init() is running (the startup race: other tasks / status during the open)
_op_error: Optional[str] = None   # reason of the last failed operation (cleared by the next success)
_version: Optional[int] = None


def default_path() -> str:
    """HedgeFund/backend/data/market_research.db — data/sandbox/market_research.db in sandbox mode
    (sandbox_profile.py)."""
    import sandbox_profile
    return sandbox_profile.db_file(DB_FILENAME)


def db_path() -> Optional[str]:
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
        raise NewerSchema(f"schema v{current} is newer than this code (v{SCHEMA_VERSION})")
    for version, statements in sorted(MIGRATIONS, key=lambda m: m[0]):
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
        conn = _open(target)
        # Review L5: read the schema version BEFORE anything that writes (journal_mode=WAL changes the file),
        # so a file written by a newer version stays byte-for-byte untouched.
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        if current > SCHEMA_VERSION:
            conn.close()
            conn = None
            raise NewerSchema(f"schema v{current} is newer than this code (v{SCHEMA_VERSION})")
        conn.execute("PRAGMA journal_mode=WAL")
        check = conn.execute("PRAGMA quick_check").fetchone()
    except NewerSchema:
        raise
    except sqlite3.OperationalError:
        if conn is not None:
            conn.close()
        raise                      # locked / busy / cannot open: not corruption
    except sqlite3.DatabaseError as ex:
        if conn is not None:
            conn.close()
        raise CorruptDatabase(str(ex)) from ex
    try:
        if not check or check[0] != "ok":
            raise CorruptDatabase(f"quick_check: {check[0] if check else 'no result'}")
        return migrate(conn)
    finally:
        conn.close()


QUARANTINE_ORDER = ("-wal", "-shm", "")     # side files first: the main file is moved last


def _quarantine(target: str, now: Optional[datetime] = None) -> str:
    """Renames a corrupt database and its -wal/-shm out of the way — all or nothing (review L6): on
    Windows a file held open by another process cannot be renamed; then every file already moved is
    moved back and the error is raised (init → StoreUnavailable, the file is NOT recreated)."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    moved = f"{target}.corrupt-{stamp}"
    done = []
    try:
        for suffix in QUARANTINE_ORDER:
            if os.path.exists(target + suffix):
                os.replace(target + suffix, moved + suffix)
                done.append(suffix)
    except OSError:
        for suffix in reversed(done):
            try:
                os.replace(moved + suffix, target + suffix)
            except OSError:
                logger.exception("Could not move %s back after a failed quarantine", moved + suffix)
        raise
    return moved


def init(path: Optional[str] = None) -> int:
    """Opens (creates) the database, sets WAL, migrates; returns the schema version.
    Corrupt → quarantined + recreated; any other failure → StoreUnavailable."""
    global _path, _wanted, _last_attempt, _last_error, _op_error, _version, _opening
    with _lock:
        target = os.path.abspath(path or default_path())
        _wanted = target
        _last_attempt = _time.monotonic()
        _opening = True              # S10-H: status() says "not_opened" (starting) while the first open runs
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            try:
                version = _open_and_migrate(target)
            except CorruptDatabase as ex:
                moved = _quarantine(target)
                logger.error("%s is corrupt (%s) — renamed to %s, starting with a fresh database",
                             target, ex, moved)
                version = _open_and_migrate(target)
            _path, _version, _last_error, _op_error = target, version, "", None
            logger.info("%s ready: %s (schema v%d)", DB_FILENAME, target, version)
            return version
        except NewerSchema as ex:
            _path, _version = None, None
            _last_error = "it was written by a newer version of UFOS.ai"
            logger.error("%s unavailable (%s): %s", DB_FILENAME, target, ex)
            raise StoreUnavailable(_last_error, detail=str(ex)) from ex
        except Exception as ex:
            _path, _version = None, None
            _last_error = "it could not be opened"
            logger.error("%s unavailable (%s): %s", DB_FILENAME, target, str(ex) or type(ex).__name__)
            raise StoreUnavailable(_last_error, detail=str(ex) or type(ex).__name__) from ex
        finally:
            _opening = False


def close() -> None:
    """Forgets the open database (tests; no reopen afterwards)."""
    global _path, _wanted, _last_error, _op_error, _version
    with _lock:
        _path = _wanted = _version = None
        _last_error, _op_error = "", None


def _require_path() -> str:
    if _path is not None:
        return _path
    if _opening:
        with _lock:                  # S10-H startup race: an open is running (init holds the lock) — wait for it
            if _path is not None:
                return _path
    if _wanted is None:
        raise StoreUnavailable("it has not been opened")
    if _time.monotonic() - _last_attempt < REOPEN_INTERVAL_SECONDS:
        raise StoreUnavailable(_last_error or "it could not be opened")
    init(_wanted)
    return _path


@contextmanager
def _connect():
    global _op_error
    path = _require_path()
    try:
        conn = _open(path)
    except sqlite3.Error as ex:
        _op_error = "it could not be opened"
        raise StoreUnavailable(_op_error, detail=str(ex)) from ex
    try:
        yield conn
        conn.commit()
        _op_error = None
    except sqlite3.Error as ex:
        conn.rollback()
        logger.warning("%s operation failed: %s", DB_FILENAME, ex)
        _op_error = "a database operation failed"
        raise StoreUnavailable(_op_error, detail=str(ex)) from ex
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def status() -> dict:
    """{state: ok|unavailable|not_opened, reason, file, schema_version} — never raises, no I/O."""
    if _path is not None and _op_error is None:
        return {"state": "ok", "reason": None, "file": DB_FILENAME, "schema_version": _version}
    if _path is None and (_wanted is None or (_opening and not _last_error)):
        # S10-H startup race: the FIRST open is still running — "starting", not "unavailable"
        return {"state": "not_opened", "reason": None, "file": DB_FILENAME, "schema_version": None}
    reason = _op_error if _path is not None else (_last_error or "it could not be opened")
    return {"state": "unavailable", "reason": StoreUnavailable(reason).user_message, "file": DB_FILENAME,
            "schema_version": None}


def _tables(conn) -> List[str]:
    return [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                       "AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def storage_info() -> dict:
    """{file, size_bytes, schema_version, rows: {table: n}} — raises StoreUnavailable."""
    with _connect() as conn:
        rows = {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in _tables(conn)}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    try:
        size = os.path.getsize(_path)
    except (OSError, TypeError) as ex:
        raise StoreUnavailable("its file could not be read", detail=str(ex)) from ex
    return {"file": DB_FILENAME, "size_bytes": size, "schema_version": version, "rows": rows}


def _key(key) -> str:
    if not isinstance(key, str) or not key or len(key) > 200:
        raise ValueError("key must be a non-empty string of at most 200 characters")
    return key


def meta_get(key: str) -> Optional[str]:
    key = _key(key)
    with _connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def meta_set(key: str, value) -> None:
    """Stores ``str(value)``; ``None`` deletes the key."""
    key = _key(key)
    with _connect() as conn:
        if value is None:
            conn.execute("DELETE FROM meta WHERE key = ?", (key,))
        else:
            conn.execute("INSERT INTO meta(key, value, updated_at) VALUES(?, ?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                         (key, str(value), utc_now_iso()))


def add_audit(actor: str, action: str, detail=None) -> int:
    if actor not in AUDIT_ACTORS:
        raise ValueError(f"actor must be one of {', '.join(AUDIT_ACTORS)}")
    if not isinstance(action, str) or not action or len(action) > 64:
        raise ValueError("action must be a non-empty string of at most 64 characters")
    try:
        detail_json = json.dumps(detail, ensure_ascii=False, allow_nan=False) if detail is not None else None
    except (TypeError, ValueError) as ex:
        raise ValueError(f"detail is not JSON-serialisable: {ex}") from ex
    with _connect() as conn:
        cur = conn.execute("INSERT INTO audit(ts, actor, action, detail_json) VALUES(?, ?, ?, ?)",
                           (utc_now_iso(), actor, action, detail_json))
        return int(cur.lastrowid)


def list_audit(limit: int = 50) -> List[dict]:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer 1..1000")
    with _connect() as conn:
        rows = conn.execute("SELECT id, ts, actor, action, detail_json FROM audit ORDER BY id DESC LIMIT ?",
                            (limit,)).fetchall()
    return [{"id": r["id"], "ts": r["ts"], "actor": r["actor"], "action": r["action"],
             "detail": json.loads(r["detail_json"]) if r["detail_json"] else None} for r in rows]


def prune(now: Optional[datetime] = None) -> Dict[str, int]:
    """Retention (SPEC §6): deletes rows older than RETENTION_DAYS per table; {table: deleted}."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    out: Dict[str, int] = {}
    with _connect() as conn:
        existing = set(_tables(conn))
        for table, column in PRUNE_COLUMNS:
            if table not in existing:
                continue
            days = limits.RETENTION_DAYS[limits.RETENTION_ALIASES.get(table, table)]
            cutoff = (now - timedelta(days=days)).astimezone(timezone.utc)
            cur = conn.execute(f'DELETE FROM "{table}" WHERE "{column}" < ?', (cutoff.isoformat(timespec="seconds"),))
            out[table] = cur.rowcount
    return out


# --- MR2: assets (SPEC §4.2 /research/assets) -------------------------------------------------------

ASSET_COLUMNS = ("symbol", "name", "asset_class", "kind", "exchange", "tradable", "status", "fetched_at")


def _asset_dict(r) -> dict:
    return {"symbol": r["symbol"], "name": r["name"], "asset_class": r["asset_class"], "kind": r["kind"],
            "exchange": r["exchange"], "tradable": bool(r["tradable"]), "status": r["status"],
            "fetched_at": r["fetched_at"]}


def assets_replace(rows: List[dict]) -> int:
    """Replaces the whole asset list in ONE transaction (a failed refresh keeps the old list)."""
    if not isinstance(rows, list):
        raise ValueError("rows must be a list")
    with _connect() as conn:
        _begin(conn)
        conn.execute("DELETE FROM assets")
        conn.executemany("INSERT OR REPLACE INTO assets(symbol, name, asset_class, kind, exchange, tradable, status, "
                         "fetched_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                         [(r["symbol"], r["name"], r["asset_class"], r["kind"], r.get("exchange"),
                           1 if r.get("tradable") else 0, r.get("status") or "active", r["fetched_at"])
                          for r in rows])
    return len(rows)


def asset_get(symbol: str) -> Optional[dict]:
    with _connect() as conn:
        r = conn.execute("SELECT * FROM assets WHERE symbol = ?", (str(symbol),)).fetchone()
    return _asset_dict(r) if r else None


def assets_count() -> int:
    with _connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0])


def assets_candidates(q: str, max_rows: int = 500) -> List[dict]:
    """Candidates for the search ranking of assets.py (case-insensitive): symbol prefix, name word prefix,
    name substring — each group ordered by symbol length, symbol and capped at ``max_rows`` (so a short
    query can never push the exact symbol out of the candidates)."""
    q = str(q)
    p = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    order = " ORDER BY length(symbol), symbol LIMIT ?"
    queries = (("symbol LIKE ? ESCAPE '\\'", (p + "%",)),
               ("(name LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\')", (p + "%", "% " + p + "%")),
               ("name LIKE ? ESCAPE '\\'", ("%" + p + "%",)))
    seen, out = set(), []
    with _connect() as conn:
        for where, args in queries:
            for r in conn.execute("SELECT * FROM assets WHERE " + where + order, args + (int(max_rows),)):
                if r["symbol"] not in seen:
                    seen.add(r["symbol"])
                    out.append(_asset_dict(r))
    return out


# --- MR2: Research 1m bars (SPEC §6, §7.1; 30-day retention) --------------------------------------------

BAR_FEEDS = ("sip", "iex", "crypto")


def _feed(feed: str) -> str:
    if feed not in BAR_FEEDS:
        raise ValueError(f"feed must be one of {', '.join(BAR_FEEDS)}")
    return feed


def bars_1m_upsert(symbol: str, feed: str, rows: List[dict], fetched_at: str) -> int:
    """rows: provider bar dicts (ts, open, high, low, close, volume, vwap, trade_count)."""
    feed = _feed(feed)
    with _connect() as conn:
        _begin(conn)
        conn.executemany(
            "INSERT INTO bars_1m(symbol, ts, open, high, low, close, volume, vwap, trade_count, feed, fetched_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(symbol, ts, feed) DO UPDATE SET "
            "open = excluded.open, high = excluded.high, low = excluded.low, close = excluded.close, "
            "volume = excluded.volume, vwap = excluded.vwap, trade_count = excluded.trade_count, "
            "fetched_at = excluded.fetched_at",
            [(symbol, r["ts"], float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
              r.get("volume"), r.get("vwap"), r.get("trade_count"), feed, fetched_at) for r in rows])
    return len(rows)


def bars_1m_query(symbol: str, feed: str, start: str, end: str) -> List[dict]:
    """Bars with start <= ts < end (UTC ISO strings), oldest first."""
    feed = _feed(feed)
    with _connect() as conn:
        rows = conn.execute("SELECT ts, open, high, low, close, volume, vwap, trade_count FROM bars_1m "
                            "WHERE symbol = ? AND feed = ? AND ts >= ? AND ts < ? ORDER BY ts",
                            (symbol, feed, start, end)).fetchall()
    return [{"ts": r["ts"], "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"],
             "volume": r["volume"], "vwap": r["vwap"], "trade_count": r["trade_count"]} for r in rows]


def bars_1m_days_get(symbol: str, feed: str, days: List[str]) -> Dict[str, str]:
    """{day: through} for the given days that have coverage rows."""
    feed = _feed(feed)
    if not days:
        return {}
    out: Dict[str, str] = {}
    with _connect() as conn:
        for i in range(0, len(days), 500):
            part = list(days[i:i + 500])
            marks = ",".join("?" * len(part))
            for r in conn.execute(f"SELECT day, through FROM bars_1m_days WHERE symbol = ? AND feed = ? "
                                  f"AND day IN ({marks})", [symbol, feed] + part):
                out[r["day"]] = r["through"]
    return out


def bars_1m_day_set(symbol: str, feed: str, day: str, through: str, fetched_at: str) -> None:
    feed = _feed(feed)
    with _connect() as conn:
        conn.execute("INSERT INTO bars_1m_days(symbol, day, feed, through, fetched_at) VALUES(?, ?, ?, ?, ?) "
                     "ON CONFLICT(symbol, day, feed) DO UPDATE SET through = excluded.through, "
                     "fetched_at = excluded.fetched_at", (symbol, day, feed, through, fetched_at))


# --- MR5: drawings, split rescales, chart settings (SPEC §6 v4, §13 MR5 backend notes) --------------------

DRAWING_COLUMNS = ("id", "symbol", "tool", "points_json", "style_json", "name", "text", "locked", "visible",
                   "timeframes_json", "split_basis", "basis_as_of", "rescales", "alert_id", "created_at",
                   "updated_at", "client_id")


def _immediate(conn) -> None:
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


def _drawing_dict(r) -> dict:
    return {"id": r["id"], "symbol": r["symbol"], "tool": r["tool"], "points": json.loads(r["points_json"]),
            "style": json.loads(r["style_json"]), "name": r["name"], "text": r["text"], "locked": bool(r["locked"]),
            "visible": bool(r["visible"]), "timeframes": json.loads(r["timeframes_json"]),
            "alert_id": r["alert_id"],
            "price_basis": {"as_of": r["basis_as_of"], "factor": float(r["split_basis"]),
                            "rescales": int(r["rescales"])},
            "created_at": r["created_at"], "updated_at": r["updated_at"], "client_id": r["client_id"]}


def _drawing_row(d: dict) -> tuple:
    j = lambda v: json.dumps(v, ensure_ascii=False, allow_nan=False, separators=(",", ":"))  # noqa: E731
    b = d["price_basis"]
    return (d["id"], d["symbol"], d["tool"], j(d["points"]), j(d["style"]), d.get("name"), d.get("text"),
            1 if d["locked"] else 0, 1 if d["visible"] else 0, j(d["timeframes"]), float(b.get("factor", 1.0)),
            b.get("as_of"), int(b.get("rescales", 0)), d.get("alert_id"), d["created_at"], d["updated_at"],
            d.get("client_id"))


_DRAWING_INSERT = ("INSERT INTO drawings(" + ", ".join(DRAWING_COLUMNS) + ") VALUES("
                   + ", ".join("?" * len(DRAWING_COLUMNS)) + ")")


def drawings_list(symbol: str) -> List[dict]:
    """The symbol's drawings in creation order."""
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM drawings WHERE symbol = ? ORDER BY created_at, rowid",
                            (str(symbol),)).fetchall()
    return [_drawing_dict(r) for r in rows]


def drawing_get(drawing_id: str) -> Optional[dict]:
    with _connect() as conn:
        r = conn.execute("SELECT * FROM drawings WHERE id = ?", (str(drawing_id),)).fetchone()
    return _drawing_dict(r) if r else None


def drawing_insert(d: dict, max_per_symbol: int):
    """Inserts a validated drawing in ONE transaction → ``("created", id)``; ``("full", None)`` (nothing
    written) when the symbol already has max_per_symbol; ``("exists", existing id)`` when ``d["client_id"]``
    is already stored (idempotent retry — nothing written)."""
    with _connect() as conn:
        _immediate(conn)
        if d.get("client_id"):
            r = conn.execute("SELECT id FROM drawings WHERE client_id = ?", (d["client_id"],)).fetchone()
            if r is not None:
                conn.rollback()
                return "exists", r["id"]
        n = conn.execute("SELECT COUNT(*) FROM drawings WHERE symbol = ?", (d["symbol"],)).fetchone()[0]
        if n >= max_per_symbol:
            conn.rollback()
            return "full", None
        conn.execute(_DRAWING_INSERT, _drawing_row(d))
    return "created", d["id"]


def drawings_by_client_ids(client_ids: List[str]) -> List[dict]:
    """The drawings carrying these client ids, in the given order (missing ones skipped)."""
    out = []
    with _connect() as conn:
        for cid in client_ids:
            r = conn.execute("SELECT * FROM drawings WHERE client_id = ?", (cid,)).fetchone()
            if r is not None:
                out.append(_drawing_dict(r))
    return out


_PATCH_COLUMNS = {"points": "points_json", "style": "style_json", "name": "name", "text": "text",
                  "locked": "locked", "visible": "visible", "timeframes": "timeframes_json", "alert_id": "alert_id"}


def drawing_update(drawing_id: str, changes: dict, updated_at: str, *, basis_as_of: Optional[str] = None,
                   expected_updated_at: Optional[str] = None) -> bool:
    """Applies validated changes; ``basis_as_of`` (new points) also sets the price basis date. False when
    the row is gone or ``expected_updated_at`` no longer matches (changed meanwhile)."""
    sets, args = [], []
    for k, v in changes.items():
        col = _PATCH_COLUMNS.get(k)
        if col is None:
            raise ValueError(f"unknown drawing field {k!r}")
        if col.endswith("_json"):
            v = json.dumps(v, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        elif col in ("locked", "visible"):
            v = 1 if v else 0
        sets.append(f"{col} = ?")
        args.append(v)
    if basis_as_of is not None:
        sets.append("basis_as_of = ?")
        args.append(basis_as_of)
    sets.append("updated_at = ?")
    args.append(updated_at)
    where = "id = ?" + (" AND updated_at = ?" if expected_updated_at is not None else "")
    args.append(drawing_id)
    if expected_updated_at is not None:
        args.append(expected_updated_at)
    with _connect() as conn:
        cur = conn.execute("UPDATE drawings SET " + ", ".join(sets) + " WHERE " + where, args)
        return cur.rowcount == 1


def drawing_delete(drawing_id: str) -> bool:
    with _connect() as conn:
        return conn.execute("DELETE FROM drawings WHERE id = ?", (str(drawing_id),)).rowcount == 1


def drawings_delete_symbol(symbol: str) -> int:
    with _connect() as conn:
        return int(conn.execute("DELETE FROM drawings WHERE symbol = ?", (str(symbol),)).rowcount)


def drawings_import(symbol: str, items: List[dict], *, replace: bool, max_per_symbol: int):
    """(state, replaced) — ONE transaction: optional delete of the symbol's drawings, then the inserts.
    state ``ok``; ``full`` (nothing written) when the total would exceed max_per_symbol; ``exists`` (nothing
    written) when any item's ``client_id`` is already stored — a retried import (review M3)."""
    with _connect() as conn:
        _immediate(conn)
        for d in items:
            if d.get("client_id") and conn.execute("SELECT 1 FROM drawings WHERE client_id = ?",
                                                   (d["client_id"],)).fetchone():
                conn.rollback()
                return "exists", 0
        n = conn.execute("SELECT COUNT(*) FROM drawings WHERE symbol = ?", (symbol,)).fetchone()[0]
        replaced = int(n) if replace else 0
        if (0 if replace else n) + len(items) > max_per_symbol:
            conn.rollback()
            return "full", 0
        if replace:
            conn.execute("DELETE FROM drawings WHERE symbol = ?", (symbol,))
        conn.executemany(_DRAWING_INSERT, [_drawing_row(d) for d in items])
    return "ok", replaced


def drawings_oldest_basis() -> Dict[str, str]:
    """{symbol: oldest price-basis date} of drawings that have one (equities)."""
    with _connect() as conn:
        rows = conn.execute("SELECT symbol, MIN(basis_as_of) AS a FROM drawings WHERE basis_as_of IS NOT NULL "
                            "GROUP BY symbol").fetchall()
    return {r["symbol"]: r["a"] for r in rows}


def drawing_apply_rescales(drawing_id: str, expected_as_of: str, steps: List[dict], points: List[dict],
                           new_as_of: str, factor: float, ts: str) -> bool:
    """Split rule, ONE transaction: a drawing_rescales row per step (UNIQUE drawing_id + action_key), the
    rescaled points, the new basis and an audit row. False (nothing written) when the drawing is gone, its
    basis changed meanwhile, or a step was already applied (idempotent)."""
    j = lambda v: json.dumps(v, ensure_ascii=False, allow_nan=False, separators=(",", ":"))  # noqa: E731
    with _connect() as conn:
        _immediate(conn)
        r = conn.execute("SELECT symbol, basis_as_of FROM drawings WHERE id = ?", (drawing_id,)).fetchone()
        if r is None or r["basis_as_of"] != expected_as_of:
            conn.rollback()
            return False
        for s in steps:
            cur = conn.execute("INSERT OR IGNORE INTO drawing_rescales(drawing_id, action_key, ts, factor, ex_date, "
                               "detail_json) VALUES(?, ?, ?, ?, ?, ?)",
                               (drawing_id, s["action_key"], ts, float(s["factor"]), s["ex_date"], j(s["detail"])))
            if cur.rowcount != 1:
                conn.rollback()
                return False
        conn.execute("UPDATE drawings SET points_json = ?, basis_as_of = ?, split_basis = ?, "
                     "rescales = rescales + ?, updated_at = ? WHERE id = ?",
                     (j(points), new_as_of, float(factor), len(steps), ts, drawing_id))
        conn.execute("INSERT INTO audit(ts, actor, action, detail_json) VALUES(?, 'system', 'drawing_rescaled', ?)",
                     (ts, j({"id": drawing_id, "symbol": r["symbol"],
                             "splits": [{"action": s["action_key"], "factor": s["factor"]} for s in steps]})))
    return True


def drawing_rescales_list(drawing_id: str) -> List[dict]:
    with _connect() as conn:
        rows = conn.execute("SELECT action_key, ts, factor, ex_date, detail_json FROM drawing_rescales "
                            "WHERE drawing_id = ? ORDER BY id", (str(drawing_id),)).fetchall()
    return [{"action_key": r["action_key"], "ts": r["ts"], "factor": r["factor"], "ex_date": r["ex_date"],
             "detail": json.loads(r["detail_json"]) if r["detail_json"] else None} for r in rows]


def chart_settings_get(symbol: str) -> Optional[dict]:
    """{settings, updated_at} or None."""
    with _connect() as conn:
        r = conn.execute("SELECT settings_json, updated_at FROM chart_settings WHERE symbol = ?",
                         (str(symbol),)).fetchone()
    return {"settings": json.loads(r["settings_json"]), "updated_at": r["updated_at"]} if r else None


def chart_settings_put(symbol: str, settings: dict, updated_at: str) -> None:
    with _connect() as conn:
        conn.execute("INSERT INTO chart_settings(symbol, settings_json, updated_at) VALUES(?, ?, ?) "
                     "ON CONFLICT(symbol) DO UPDATE SET settings_json = excluded.settings_json, "
                     "updated_at = excluded.updated_at",
                     (str(symbol), json.dumps(settings, ensure_ascii=False, allow_nan=False), updated_at))


# --- MR6: news, thumbnail cache, logos (SPEC §13 "MR6 backend notes") ---------------------------------

def _j(v) -> str:
    return json.dumps(v, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _news_dict(r) -> dict:
    return {"id": int(r["id"]), "headline": r["headline"], "summary": r["summary"], "publisher": r["publisher"],
            "url": r["url"], "created_at": r["created_at"], "updated_at": r["updated_at"],
            "symbols": json.loads(r["symbols_json"]), "images": json.loads(r["images_json"]),
            "fetched_at": r["fetched_at"], "origin": r["origin"]}


def news_upsert(items: List[dict], fetched_at: str, origin: str, image_key) -> Dict[int, str]:
    """De-dup by Alpaca id, ONE transaction. Returns {id: "new" | "updated" | "same"}; an existing row is
    replaced only when the incoming ``updated_at`` is newer (or the row had none). Every image URL gets a
    ``news_images`` row (``image_key(url)``; status pending until fetched; ``created_at`` = the newest news
    item using it, so the shared cache follows the news retention)."""
    if origin not in ("rest", "stream"):
        raise ValueError("origin must be rest or stream")
    out: Dict[int, str] = {}
    with _connect() as conn:
        _immediate(conn)
        for it in items:
            nid = int(it["id"])
            row = conn.execute("SELECT updated_at, images_json FROM news WHERE id = ?", (nid,)).fetchone()
            if row is not None:
                old, new = row["updated_at"], it.get("updated_at")
                if not new or (old and new <= old):
                    out[nid] = "same"
                    if it["images"] and row["images_json"] == "[]":     # stream copy first, REST has the images
                        conn.execute("UPDATE news SET images_json = ? WHERE id = ?", (_j(it["images"]), nid))
                        _image_rows(conn, it, image_key)
                    continue
                out[nid] = "updated"
            else:
                out[nid] = "new"
            conn.execute("INSERT INTO news(id, symbols_json, headline, summary, publisher, url, images_json, "
                         "created_at, updated_at, fetched_at, origin) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                         "ON CONFLICT(id) DO UPDATE SET symbols_json = excluded.symbols_json, "
                         "headline = excluded.headline, summary = excluded.summary, publisher = excluded.publisher, "
                         "url = excluded.url, images_json = excluded.images_json, created_at = excluded.created_at, "
                         "updated_at = excluded.updated_at, fetched_at = excluded.fetched_at, origin = excluded.origin",
                         (nid, _j(it["symbols"]), it["headline"], it.get("summary"), it.get("publisher"), it.get("url"),
                          _j(it["images"]), it["created_at"], it.get("updated_at"), fetched_at, origin))
            conn.execute("DELETE FROM news_symbols WHERE id = ?", (nid,))
            conn.executemany("INSERT OR IGNORE INTO news_symbols(id, symbol, created_at) VALUES(?, ?, ?)",
                             [(nid, sym, it["created_at"]) for sym in it["symbols"]])
            _image_rows(conn, it, image_key)
    return out


def _image_rows(conn, it: dict, image_key) -> None:
    for im in it["images"]:
        conn.execute("INSERT INTO news_images(key, url, created_at, status) VALUES(?, ?, ?, 'pending') "
                     "ON CONFLICT(key) DO UPDATE SET created_at = MAX(created_at, excluded.created_at)",
                     (image_key(im["url"]), im["url"], it["created_at"]))


def news_for_symbol(tag: str, limit: int, before: Optional[str] = None) -> List[dict]:
    """Newest first (created_at, then id); ``before`` exclusive."""
    sql = ("SELECT n.* FROM news_symbols s JOIN news n ON n.id = s.id WHERE s.symbol = ?"
           + (" AND n.created_at < ?" if before else "") + " ORDER BY n.created_at DESC, n.id DESC LIMIT ?")
    args = (tag, before, int(limit)) if before else (tag, int(limit))
    with _connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_news_dict(r) for r in rows]


def news_get(news_id: int) -> Optional[dict]:
    with _connect() as conn:
        r = conn.execute("SELECT * FROM news WHERE id = ?", (int(news_id),)).fetchone()
    return _news_dict(r) if r else None


def news_image_get(key: str) -> Optional[dict]:
    with _connect() as conn:
        r = conn.execute("SELECT key, url, created_at, status, content_type, data, reason, fetched_at "
                         "FROM news_images WHERE key = ?", (str(key),)).fetchone()
    if r is None:
        return None
    return {"key": r["key"], "url": r["url"], "created_at": r["created_at"], "status": r["status"],
            "content_type": r["content_type"], "data": bytes(r["data"]) if r["data"] is not None else None,
            "reason": r["reason"], "fetched_at": r["fetched_at"]}


def news_image_set(key: str, status: str, fetched_at: str, *, content_type: Optional[str] = None,
                   data: Optional[bytes] = None, reason: Optional[str] = None) -> bool:
    if status not in ("ok", "failed", "rejected"):
        raise ValueError("status must be ok, failed or rejected")
    with _connect() as conn:
        cur = conn.execute("UPDATE news_images SET status = ?, content_type = ?, data = ?, reason = ?, fetched_at = ? "
                           "WHERE key = ?", (status, content_type, sqlite3.Binary(data) if data is not None else None,
                                             reason, fetched_at, str(key)))
        return cur.rowcount == 1


def logo_get(symbol: str) -> Optional[dict]:
    with _connect() as conn:
        r = conn.execute("SELECT symbol, png, status, fetched_at FROM logos WHERE symbol = ?", (str(symbol),)).fetchone()
    if r is None:
        return None
    return {"symbol": r["symbol"], "png": bytes(r["png"]) if r["png"] is not None else None, "status": r["status"],
            "fetched_at": r["fetched_at"]}


def logo_set(symbol: str, status: str, fetched_at: str, png: Optional[bytes] = None) -> None:
    if status not in ("ok", "none") or (status == "ok") != (png is not None):
        raise ValueError("status ok needs png bytes, status none none")
    with _connect() as conn:
        conn.execute("INSERT INTO logos(symbol, png, status, fetched_at) VALUES(?, ?, ?, ?) ON CONFLICT(symbol) DO UPDATE "
                     "SET png = excluded.png, status = excluded.status, fetched_at = excluded.fetched_at",
                     (str(symbol), sqlite3.Binary(png) if png is not None else None, status, fetched_at))


# --- MR7: alerts + alert log (SPEC §13 "MR7 backend notes") --------------------------------------------

ALERT_COLUMNS = ("id", "symbol", "kind", "params_json", "cooldown_seconds", "enabled", "drawing_id",
                 "last_triggered_at", "state_json", "disabled_reason", "created_at", "updated_at")


def _alert_dict(r) -> dict:
    return {"id": r["id"], "symbol": r["symbol"], "kind": r["kind"], "params": json.loads(r["params_json"]),
            "cooldown_seconds": int(r["cooldown_seconds"]), "enabled": bool(r["enabled"]), "drawing_id": r["drawing_id"],
            "last_triggered_at": r["last_triggered_at"], "state": json.loads(r["state_json"] or "{}"),
            "disabled_reason": r["disabled_reason"], "created_at": r["created_at"], "updated_at": r["updated_at"]}


def alerts_list() -> List[dict]:
    """All rules, oldest first (= stream priority among alerts)."""
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM alerts ORDER BY created_at, rowid").fetchall()
    return [_alert_dict(r) for r in rows]


def alert_get(alert_id: str) -> Optional[dict]:
    with _connect() as conn:
        r = conn.execute("SELECT * FROM alerts WHERE id = ?", (str(alert_id),)).fetchone()
    return _alert_dict(r) if r else None


def _link_line(conn, alert_id: str, drawing_id: str, symbol: str, ts: str) -> str:
    """Inside the caller's transaction: checks the drawing (exists, alertLine, same symbol, not linked to
    ANOTHER existing alert) and sets ``drawings.alert_id``. "ok" | "no_drawing" | "not_line" | "wrong_symbol"
    | "linked"."""
    d = conn.execute("SELECT symbol, tool, alert_id FROM drawings WHERE id = ?", (drawing_id,)).fetchone()
    if d is None:
        return "no_drawing"
    if d["tool"] != "alertLine":
        return "not_line"
    if d["symbol"] != symbol:
        return "wrong_symbol"
    other = d["alert_id"]
    if other and other != alert_id and conn.execute("SELECT 1 FROM alerts WHERE id = ?", (other,)).fetchone():
        return "linked"
    if other != alert_id:
        conn.execute("UPDATE drawings SET alert_id = ?, updated_at = ? WHERE id = ?", (alert_id, ts, drawing_id))
    return "ok"


def _unlink_line(conn, alert_id: str, ts: str) -> None:
    """Inside the caller's transaction: clears ``drawings.alert_id`` wherever it points to ``alert_id``."""
    conn.execute("UPDATE drawings SET alert_id = NULL, updated_at = ? WHERE alert_id = ?", (ts, alert_id))


def alert_insert(a: dict, max_alerts: int) -> str:
    """"ok" | "full" (max_alerts reached) | "duplicate" (same symbol + kind + params) | "drawing" (the line
    already has an alert) | a ``_link_line`` code — ONE transaction: an ENABLED line alert also sets the
    drawing's ``alert_id``; nothing is written unless "ok"."""
    with _connect() as conn:
        _immediate(conn)
        if conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] >= max_alerts:
            conn.rollback()
            return "full"
        pj = _j(a["params"])
        if conn.execute("SELECT 1 FROM alerts WHERE symbol = ? AND kind = ? AND params_json = ?",
                        (a["symbol"], a["kind"], pj)).fetchone():
            conn.rollback()
            return "duplicate"
        if a.get("drawing_id"):
            if conn.execute("SELECT 1 FROM alerts WHERE drawing_id = ?", (a["drawing_id"],)).fetchone():
                conn.rollback()
                return "drawing"
            res = _link_line(conn, a["id"], a["drawing_id"], a["symbol"], a["updated_at"]) if a["enabled"] else \
                _check_line(conn, a["drawing_id"], a["symbol"])
            if res != "ok":
                conn.rollback()
                return res
        conn.execute("INSERT INTO alerts(" + ", ".join(ALERT_COLUMNS) + ") VALUES(" + ", ".join("?" * len(ALERT_COLUMNS))
                     + ")", (a["id"], a["symbol"], a["kind"], pj, int(a["cooldown_seconds"]), 1 if a["enabled"] else 0,
                             a.get("drawing_id"), None, "{}", a.get("disabled_reason"), a["created_at"], a["updated_at"]))
    return "ok"


def _check_line(conn, drawing_id: str, symbol: str) -> str:
    d = conn.execute("SELECT symbol, tool FROM drawings WHERE id = ?", (drawing_id,)).fetchone()
    if d is None:
        return "no_drawing"
    if d["tool"] != "alertLine":
        return "not_line"
    return "ok" if d["symbol"] == symbol else "wrong_symbol"


def alert_update(alert_id: str, changes: dict, updated_at: str) -> str:
    """"ok" | "missing" | "duplicate" (params collide with another rule of the same symbol + kind) | a
    ``_link_line`` code (switching a line alert on) — ONE transaction: a line alert switched on links its
    drawing (``drawings.alert_id``), switched off unlinks it."""
    cols = {"params": "params_json", "cooldown_seconds": "cooldown_seconds", "enabled": "enabled",
            "disabled_reason": "disabled_reason"}
    sets, args = [], []
    for k, v in changes.items():
        if k not in cols:
            raise ValueError(f"unknown alert field {k!r}")
        if k == "params":
            v = _j(v)
        elif k == "enabled":
            v = 1 if v else 0
        sets.append(f"{cols[k]} = ?")
        args.append(v)
    sets.append("updated_at = ?")
    args.append(updated_at)
    with _connect() as conn:
        _immediate(conn)
        r = conn.execute("SELECT symbol, kind, drawing_id, enabled FROM alerts WHERE id = ?", (str(alert_id),)).fetchone()
        if r is None:
            conn.rollback()
            return "missing"
        if "params" in changes and conn.execute(
                "SELECT 1 FROM alerts WHERE symbol = ? AND kind = ? AND params_json = ? AND id <> ?",
                (r["symbol"], r["kind"], _j(changes["params"]), str(alert_id))).fetchone():
            conn.rollback()
            return "duplicate"
        if r["drawing_id"] and "enabled" in changes:
            if changes["enabled"]:
                res = _link_line(conn, str(alert_id), r["drawing_id"], r["symbol"], updated_at)
                if res != "ok":
                    conn.rollback()
                    return res
            else:
                _unlink_line(conn, str(alert_id), updated_at)
        conn.execute("UPDATE alerts SET " + ", ".join(sets) + " WHERE id = ?", args + [str(alert_id)])
    return "ok"


def alert_delete(alert_id: str, ts: Optional[str] = None) -> bool:
    """Deletes the rule and clears its drawing link — ONE transaction."""
    with _connect() as conn:
        _immediate(conn)
        if conn.execute("DELETE FROM alerts WHERE id = ?", (str(alert_id),)).rowcount != 1:
            conn.rollback()
            return False
        _unlink_line(conn, str(alert_id), ts or utc_now_iso())
    return True


def alert_disable(alert_id: str, reason: str, ts: str) -> bool:
    """Switches a rule off with a reason + audit row and clears its drawing link (ONE transaction); False when
    gone / already off."""
    with _connect() as conn:
        _immediate(conn)
        r = conn.execute("SELECT symbol, enabled FROM alerts WHERE id = ?", (str(alert_id),)).fetchone()
        if r is None or not r["enabled"]:
            conn.rollback()
            return False
        conn.execute("UPDATE alerts SET enabled = 0, disabled_reason = ?, updated_at = ? WHERE id = ?",
                     (reason, ts, str(alert_id)))
        _unlink_line(conn, str(alert_id), ts)
        conn.execute("INSERT INTO audit(ts, actor, action, detail_json) VALUES(?, 'system', 'alert_disabled', ?)",
                     (ts, _j({"id": str(alert_id), "symbol": r["symbol"], "reason": reason})))
    return True


def alert_fire(alert_id: str, ts: str, state: dict, log: dict) -> Optional[int]:
    """A trigger, ONE transaction: last_triggered_at + de-dup state of the rule and the alert_log row.
    None (nothing written) when the rule is gone."""
    with _connect() as conn:
        _immediate(conn)
        cur = conn.execute("UPDATE alerts SET last_triggered_at = ?, state_json = ? WHERE id = ?",
                           (ts, _j(state), str(alert_id)))
        if cur.rowcount != 1:
            conn.rollback()
            return None
        cur = conn.execute("INSERT INTO alert_log(alert_id, symbol, ts, kind, message, price, detail_json) "
                           "VALUES(?, ?, ?, ?, ?, ?, ?)",
                           (str(alert_id), log["symbol"], ts, log["kind"], log["message"], log.get("price"),
                            _j(log.get("detail") or {})))
        return int(cur.lastrowid)


def alert_log_list(limit: int, alert_id: Optional[str] = None) -> List[dict]:
    sql = "SELECT * FROM alert_log" + (" WHERE alert_id = ?" if alert_id else "") + " ORDER BY id DESC LIMIT ?"
    args = (alert_id, int(limit)) if alert_id else (int(limit),)
    with _connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [{"id": int(r["id"]), "alert_id": r["alert_id"], "symbol": r["symbol"], "ts": r["ts"], "kind": r["kind"],
             "message": r["message"], "price": r["price"],
             "detail": json.loads(r["detail_json"]) if r["detail_json"] else {}} for r in rows]
