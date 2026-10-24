"""Session-spanning local storage (SQLite) for the LiveStreamAgent backend (2026-09-25).

One database file per module (project convention "compartmentalization" - the
other UFOS.ai backends each keep their own copy of this pattern):

    backend/data/livestream.db   (override: UFOS_LSA_DB = full file path)

Tables (retention applied at backend start and once a day, see run_retention()):
- ``ai_outputs``   every AI verdict / analysis: ts, feature ("verdict" |
                   "analysis"), model, tier, input_json (transcript excerpt +
                   market snapshot), output_text, provenance_json, usage_json.
                   Kept AI_OUTPUT_RETENTION_DAYS (90).
- ``items``        (source, item_id) -> item_ts, fetched_ts, payload JSON.
                   Transcript segments (source "transcript", kept
                   TRANSCRIPT_RETENTION_DAYS = 7) and the latest quote per
                   symbol (source "quote", kept QUOTE_SNAPSHOT_RETENTION_DAYS = 2).
- ``prices``       (symbol, ts, price, source): market price points (at most
                   one per QUOTE_STORE_INTERVAL_SECONDS in main.py), kept
                   PRICE_RETENTION_DAYS (730 = 2 years).
- ``source_state`` last success / attempt / error per data source.
- ``meta``         small key/value facts (e.g. last retention run).

Rules: WAL, synchronous=NORMAL, one connection (check_same_thread=False)
guarded by a threading.Lock, schema versioned with ``PRAGMA user_version``
(MIGRATIONS, append-only). Fail soft: every call logs its own error and
returns a neutral default - never raises into the backend. A corrupt file is
renamed to ``<name>.corrupt-<UTC timestamp>`` (with its -wal/-shm) and a fresh
database is created; if even that fails, an in-memory database is used for
the session. Pure stdlib.
"""
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

DB_ENV = "UFOS_LSA_DB"
DB_FILENAME = "livestream.db"

# Retention (days) applied at backend start and once a day.
AI_OUTPUT_RETENTION_DAYS = 90
TRANSCRIPT_RETENTION_DAYS = 7
QUOTE_SNAPSHOT_RETENTION_DAYS = 2
PRICE_RETENTION_DAYS = 730

SOURCE_TRANSCRIPT = "transcript"
SOURCE_QUOTE = "quote"
ITEM_RETENTION_DAYS = {
    SOURCE_TRANSCRIPT: TRANSCRIPT_RETENTION_DAYS,
    SOURCE_QUOTE: QUOTE_SNAPSHOT_RETENTION_DAYS,
}

# Each entry: (version, SQL script). Append only - never edit a shipped step.
MIGRATIONS = [
    (1, """
        CREATE TABLE IF NOT EXISTS items (
            source      TEXT NOT NULL,
            item_id     TEXT NOT NULL,
            item_ts     TEXT,
            fetched_ts  TEXT NOT NULL,
            payload     TEXT NOT NULL,
            PRIMARY KEY (source, item_id)
        );
        CREATE INDEX IF NOT EXISTS ix_items_source_ts ON items(source, item_ts);
        CREATE INDEX IF NOT EXISTS ix_items_source_fetched ON items(source, fetched_ts);

        CREATE TABLE IF NOT EXISTS source_state (
            source           TEXT PRIMARY KEY,
            last_success_ts  TEXT,
            last_attempt_ts  TEXT,
            last_error       TEXT,
            info_json        TEXT
        );

        CREATE TABLE IF NOT EXISTS prices (
            symbol  TEXT NOT NULL,
            ts      TEXT NOT NULL,
            price   REAL NOT NULL,
            source  TEXT NOT NULL,
            PRIMARY KEY (symbol, ts, source)
        );
        CREATE INDEX IF NOT EXISTS ix_prices_ts ON prices(ts);

        CREATE TABLE IF NOT EXISTS ai_outputs (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            ts               TEXT NOT NULL,
            feature          TEXT NOT NULL,
            model            TEXT,
            tier             TEXT,
            input_json       TEXT,
            output_text      TEXT,
            provenance_json  TEXT,
            usage_json       TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_ai_outputs_ts ON ai_outputs(ts);
        CREATE INDEX IF NOT EXISTS ix_ai_outputs_feature_ts ON ai_outputs(feature, ts);

        CREATE TABLE IF NOT EXISTS meta (
            key    TEXT PRIMARY KEY,
            value  TEXT
        );
    """),
]


def default_path() -> str:
    override = os.environ.get(DB_ENV)
    if override:
        return override
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", DB_FILENAME)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime] = None) -> str:
    dt = dt or utc_now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _dumps(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(raw: Optional[str]) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _norm_ts(value: Any) -> Optional[str]:
    """ISO-8601 UTC (seconds) for datetimes / parseable strings; other values as str."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return iso(value)
    text = str(value)
    try:
        return iso(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return text


class _Corrupt(Exception):
    pass


class Storage:
    """Thin, never-raising wrapper around this module's SQLite file."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or default_path()
        self.in_memory = False
        # Path the corrupt file was moved to at open (None if it wasn't).
        self.recovered_from: Optional[str] = None
        self._lock = threading.Lock()
        self._conn = self._open()

    # --- connection / migrations ---

    def _connect_file(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None, check_same_thread=False)
        try:
            check = conn.execute("PRAGMA quick_check(1)").fetchone()
            if not check or str(check[0]).lower() != "ok":
                raise _Corrupt(check[0] if check else "quick_check returned nothing")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._migrate(conn)
            return conn
        except BaseException:
            conn.close()
            raise

    def _open(self) -> sqlite3.Connection:
        try:
            try:
                conn = self._connect_file()
            except (_Corrupt, sqlite3.DatabaseError) as ex:
                # OperationalError (locked, can't open, read-only ...) is not corruption.
                if isinstance(ex, sqlite3.OperationalError) or not os.path.isfile(self.path):
                    raise
                self.recovered_from = self._move_corrupt(ex)
                conn = self._connect_file()
            logger.info("Local storage: %s (schema v%d)", self.path, _user_version(conn))
            return conn
        except Exception as ex:
            logger.warning("Local storage %s unavailable (%s: %s) - using an in-memory database; "
                           "nothing will survive a restart.", self.path, type(ex).__name__, ex)
        self.in_memory = True
        conn = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
        self._migrate(conn)
        return conn

    def _move_corrupt(self, reason) -> str:
        target = f"{self.path}.corrupt-{utc_now().strftime('%Y%m%dT%H%M%SZ')}"
        os.replace(self.path, target)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                try:
                    os.replace(self.path + suffix, target + suffix)
                except OSError:
                    pass
        logger.warning("Local storage %s is corrupt (%s) - moved to %s, starting with a fresh database.",
                       self.path, reason, target)
        return target

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        current = _user_version(conn)
        latest = MIGRATIONS[-1][0]
        if current > latest:
            logger.warning("Local storage schema v%d is newer than this code (v%d); using it as is.",
                           current, latest)
            return
        for version, script in MIGRATIONS:
            if version <= current:
                continue
            conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {int(version)};\nCOMMIT;")

    def _run(self, what: str, fn: Callable, default=None):
        try:
            with self._lock:
                return fn(self._conn)
        except Exception as ex:
            logger.warning("Local storage: %s failed (%s: %s)", what, type(ex).__name__, ex)
            try:
                with self._lock:
                    if self._conn.in_transaction:
                        self._conn.execute("ROLLBACK")
            except Exception:
                pass
            return default

    def _write(self, what: str, fn: Callable, default=None):
        """Run fn inside one transaction (commit on success, rollback on error)."""
        def op(c):
            c.execute("BEGIN")
            result = fn(c)
            c.execute("COMMIT")
            return result
        return self._run(what, op, default)

    def schema_version(self) -> int:
        return self._run("read schema version", _user_version, 0)

    def close(self) -> None:
        try:
            with self._lock:
                self._conn.close()
        except Exception:
            pass

    # --- meta ---

    def set_meta(self, key: str, value: Any) -> bool:
        return bool(self._write("set meta", lambda c: c.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(key), _dumps(value))).rowcount, 0))

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self._run("get meta", lambda c: c.execute("SELECT value FROM meta WHERE key = ?", (str(key),)).fetchone())
        if not row or row[0] is None:
            return default
        return _loads(row[0])

    # --- items ---

    def upsert_items(self, source: str, items: Iterable[dict], fetched_ts: Optional[datetime] = None) -> int:
        """Insert or replace items (same (source, item_id) = one row, newest write wins).
        Each item: {"item_id", "item_ts" (optional), "payload"}."""
        fetched = iso(fetched_ts)
        rows = []
        for it in items or []:
            if not isinstance(it, dict) or it.get("item_id") in (None, ""):
                continue
            rows.append((source, str(it["item_id"]), _norm_ts(it.get("item_ts")), fetched,
                         _dumps(it.get("payload")) or "null"))
        if not rows:
            return 0

        def op(c):
            c.executemany(
                "INSERT INTO items(source, item_id, item_ts, fetched_ts, payload) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(source, item_id) DO UPDATE SET item_ts=excluded.item_ts, "
                "fetched_ts=excluded.fetched_ts, payload=excluded.payload", rows)
            return len(rows)
        return self._write(f"upsert {source} items", op, 0)

    def load_items(self, source: str, limit: int = 500, since: Optional[datetime] = None) -> list:
        """Items of `source`, newest first (by item_ts, then fetched_ts)."""
        sql = "SELECT item_id, item_ts, fetched_ts, payload FROM items WHERE source = ?"
        args: list = [source]
        if since is not None:
            sql += " AND COALESCE(item_ts, fetched_ts) >= ?"
            args.append(iso(since))
        sql += " ORDER BY COALESCE(item_ts, fetched_ts) DESC, fetched_ts DESC LIMIT ?"
        args.append(int(limit))
        rows = self._run(f"load {source} items", lambda c: c.execute(sql, args).fetchall(), [])
        return [{"item_id": r[0], "item_ts": r[1], "fetched_ts": r[2], "payload": _loads(r[3])} for r in rows]

    def prune_items(self, source: str, older_than_days: float, now: Optional[datetime] = None) -> int:
        """Delete items of `source` whose item time (or fetch time) is older than the cutoff."""
        cutoff = iso((now or utc_now()) - timedelta(days=older_than_days))
        return self._write(f"prune {source} items", lambda c: c.execute(
            "DELETE FROM items WHERE source = ? AND COALESCE(item_ts, fetched_ts) < ?",
            (source, cutoff)).rowcount, 0)

    # --- prices ---

    def add_prices(self, points: Iterable) -> int:
        """points: iterable of (symbol, ts, price, source); one row per (symbol, ts, source)."""
        rows = []
        for point in points or []:
            try:
                symbol, ts, price, source = point
                stamp = _norm_ts(ts)
                if not symbol or not stamp or price is None or isinstance(price, bool):
                    continue
                rows.append((str(symbol), stamp, float(price), str(source or "")))
            except (TypeError, ValueError):
                continue
        if not rows:
            return 0

        def op(c):
            c.executemany(
                "INSERT INTO prices(symbol, ts, price, source) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(symbol, ts, source) DO UPDATE SET price=excluded.price", rows)
            return len(rows)
        return self._write("add prices", op, 0)

    def load_prices(self, symbol: str, since: Optional[datetime] = None) -> list:
        """[(ts, price, source)] oldest first."""
        sql = "SELECT ts, price, source FROM prices WHERE symbol = ?"
        args: list = [symbol]
        if since is not None:
            sql += " AND ts >= ?"
            args.append(iso(since))
        sql += " ORDER BY ts"
        return self._run(f"load prices {symbol}", lambda c: [tuple(r) for r in c.execute(sql, args).fetchall()], [])

    def prune_prices(self, days: float = PRICE_RETENTION_DAYS, now: Optional[datetime] = None) -> int:
        cutoff = iso((now or utc_now()) - timedelta(days=days))
        return self._write("prune prices", lambda c: c.execute(
            "DELETE FROM prices WHERE ts < ?", (cutoff,)).rowcount, 0)

    # --- source state ---

    def mark_success(self, source: str, info: Optional[dict] = None, ts: Optional[datetime] = None) -> bool:
        now = iso(ts)

        def op(c):
            c.execute(
                "INSERT INTO source_state(source, last_success_ts, last_attempt_ts, last_error, info_json) "
                "VALUES (?, ?, ?, NULL, ?) ON CONFLICT(source) DO UPDATE SET "
                "last_success_ts=excluded.last_success_ts, last_attempt_ts=excluded.last_attempt_ts, "
                "last_error=NULL, info_json=COALESCE(excluded.info_json, source_state.info_json)",
                (source, now, now, _dumps(info)))
            return True
        return self._write(f"mark {source} success", op, False)

    def mark_failure(self, source: str, error: str, ts: Optional[datetime] = None) -> bool:
        now = iso(ts)

        def op(c):
            c.execute(
                "INSERT INTO source_state(source, last_attempt_ts, last_error) VALUES (?, ?, ?) "
                "ON CONFLICT(source) DO UPDATE SET last_attempt_ts=excluded.last_attempt_ts, "
                "last_error=excluded.last_error",
                (source, now, str(error)[:1000]))
            return True
        return self._write(f"mark {source} failure", op, False)

    def get_state(self, source: str) -> Optional[dict]:
        row = self._run(f"get {source} state", lambda c: c.execute(
            "SELECT last_success_ts, last_attempt_ts, last_error, info_json FROM source_state WHERE source = ?",
            (source,)).fetchone())
        if not row:
            return None
        return {"source": source, "last_success_ts": row[0], "last_attempt_ts": row[1],
                "last_error": row[2], "info": _loads(row[3])}

    # --- AI outputs ---

    def save_ai_output(self, feature: str, model: Optional[str], output_text: Any, input_data: Any = None,
                       provenance: Optional[dict] = None, usage: Optional[dict] = None,
                       tier: Optional[str] = None, ts: Optional[datetime] = None) -> Optional[int]:
        """Store one AI output; returns its row id (None on failure). `ts` defaults to the
        provenance's generated_at (the time the UI shows), else now."""
        if ts is None and isinstance(provenance, dict) and provenance.get("generated_at"):
            stamp = _norm_ts(provenance.get("generated_at"))
        else:
            stamp = iso(ts)
        text = output_text if isinstance(output_text, str) or output_text is None else _dumps(output_text)

        def op(c):
            cur = c.execute(
                "INSERT INTO ai_outputs(ts, feature, model, tier, input_json, output_text, provenance_json, "
                "usage_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (stamp, feature, model, tier, _dumps(input_data), text, _dumps(provenance), _dumps(usage)))
            return cur.lastrowid
        return self._write(f"save AI output ({feature})", op, None)

    def list_ai_outputs(self, limit: int = 50, feature: Optional[str] = None,
                        since: Optional[datetime] = None, include_input: bool = True) -> list:
        """Stored AI outputs, newest first, JSON columns decoded."""
        cols = "id, ts, feature, model, tier, output_text, provenance_json, usage_json" + (
            ", input_json" if include_input else "")
        sql = f"SELECT {cols} FROM ai_outputs WHERE 1=1"
        args: list = []
        if feature:
            sql += " AND feature = ?"
            args.append(feature)
        if since is not None:
            sql += " AND ts >= ?"
            args.append(iso(since))
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(max(0, int(limit)))
        rows = self._run("list AI outputs", lambda c: c.execute(sql, args).fetchall(), [])
        out = []
        for r in rows:
            entry = {"id": r[0], "ts": r[1], "feature": r[2], "model": r[3], "tier": r[4],
                     "output_text": r[5], "provenance": _loads(r[6]), "usage": _loads(r[7])}
            if include_input:
                entry["input"] = _loads(r[8])
            out.append(entry)
        return out

    def prune_ai_outputs(self, days: float = AI_OUTPUT_RETENTION_DAYS, now: Optional[datetime] = None) -> int:
        cutoff = iso((now or utc_now()) - timedelta(days=days))
        return self._write("prune AI outputs", lambda c: c.execute(
            "DELETE FROM ai_outputs WHERE ts < ?", (cutoff,)).rowcount, 0)

    # --- housekeeping ---

    def run_retention(self, now: Optional[datetime] = None) -> dict:
        """Backend start + daily: apply all retention rules. Returns deleted row counts. Never raises."""
        try:
            result = {"ai_outputs": self.prune_ai_outputs(AI_OUTPUT_RETENTION_DAYS, now=now),
                      "prices": self.prune_prices(PRICE_RETENTION_DAYS, now=now)}
            for source, days in ITEM_RETENTION_DAYS.items():
                result[f"items:{source}"] = self.prune_items(source, days, now=now)
            self.set_meta("last_retention_ts", iso(now))
            self._run("checkpoint", lambda c: c.execute("PRAGMA wal_checkpoint(PASSIVE)"))
            return result
        except Exception as ex:
            logger.warning("Local storage retention failed: %s", ex)
            return {}


# Short alias (the name used in the storage design notes).
Store = Storage
