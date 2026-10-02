"""Local SQLite storage for the Global Monitoring backend (2026-09-25).

One database file per module (project convention "compartmentalization"):
this module's copy lives at ``backend/data/gms.db``; the environment
variable ``UFOS_GM_DB`` (full file path) overrides it, e.g. for tests. The
other backends (LiveStreamAgent, HedgeFund) keep their own copy of this
pattern — never share a database between modules.

Tables (schema versioned with ``PRAGMA user_version``, see MIGRATIONS;
retention is applied by persistence.apply_retention(), see config.py):

- ``items(source, item_id, item_ts, fetched_ts, payload)``, PK (source, item_id):
  events and lookups as JSON — ``news`` (geopolitics events), ``gdacs``
  (disaster alerts), ``yahoo`` (latest market quote event per symbol),
  ``localnews`` (/localnews lookups). ``item_ts`` is the item's own time
  (published / last seen / fetched), ``fetched_ts`` when it was last written;
  both ISO-8601 UTC strings (``+00:00``, so text comparison == time order).
- ``flight_positions(icao24, ts, lat, lon, altitude_m, heading_deg,
  velocity_ms, callsign, payload)``, PK (icao24, ts): every polled position
  of a notable aircraft (basis for flight tracks); payload = the marker event.
- ``prices(symbol, ts, price, source)``, PK (symbol, ts, source): market
  price points (ts = the quote's own market time, so an unchanged delayed
  quote polled again is not stored twice).
- ``ai_outputs(id, ts, feature, model, tier, input_json, output_text,
  provenance_json, usage_json)``: every AI output with its provenance.
- ``source_state(source, last_success_ts, last_attempt_ts, status_json)``:
  when each collector last succeeded / was last tried, plus its last status.
- ``meta(key, value)``: small key/value facts (e.g. last retention run).

Rules: WAL, synchronous=NORMAL, one connection (``check_same_thread=False``)
guarded by a threading.Lock. Fail soft: every call catches its own errors,
logs a warning and returns a neutral default — storage never breaks a
collector. A corrupt file is renamed to ``<name>.corrupt-<UTC timestamp>``
(with its -wal/-shm) and a fresh database is created; if even that fails,
an in-memory database is used for the rest of the session. Pure stdlib.
"""
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)

DB_ENV = "UFOS_GM_DB"
DB_FILENAME = "gms.db"

# Each entry: (version, SQL script). Append only — never edit a shipped step.
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

        CREATE TABLE IF NOT EXISTS flight_positions (
            icao24       TEXT NOT NULL,
            ts           TEXT NOT NULL,
            lat          REAL,
            lon          REAL,
            altitude_m   REAL,
            heading_deg  REAL,
            velocity_ms  REAL,
            callsign     TEXT,
            payload      TEXT,
            PRIMARY KEY (icao24, ts)
        );
        CREATE INDEX IF NOT EXISTS ix_flight_positions_ts ON flight_positions(ts);

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
        CREATE INDEX IF NOT EXISTS ix_ai_outputs_feature_ts ON ai_outputs(feature, ts);
        CREATE INDEX IF NOT EXISTS ix_ai_outputs_ts ON ai_outputs(ts);

        CREATE TABLE IF NOT EXISTS source_state (
            source           TEXT PRIMARY KEY,
            last_success_ts  TEXT,
            last_attempt_ts  TEXT,
            status_json      TEXT
        );

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
    """ISO-8601 UTC with an explicit +00:00 offset (sorts correctly as text)."""
    dt = dt or utc_now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def norm_ts(value) -> Optional[str]:
    """Normalizes a datetime / ISO string / unix seconds to iso() form (None
    if unusable), so text comparisons in SQL (ts < cutoff) are correct."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return iso(datetime.fromtimestamp(float(value), timezone.utc))
        except (OverflowError, OSError, ValueError):
            return None
    dt = parse_iso(value)
    return iso(dt) if dt else None


def _dumps(value) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(raw):
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _float(value) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


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
                # OperationalError (locked, can't open, read-only …) is not corruption.
                if isinstance(ex, sqlite3.OperationalError) or not os.path.isfile(self.path):
                    raise
                self.recovered_from = self._move_corrupt(ex)
                conn = self._connect_file()
            logger.info("Storage: %s (schema v%d)", self.path, _user_version(conn))
            return conn
        except Exception as ex:
            logger.warning("Storage %s unavailable (%s: %s) — using an in-memory database; "
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
        logger.warning("Storage %s is corrupt (%s) — moved to %s, starting with a fresh database.",
                       self.path, reason, target)
        return target

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        current = _user_version(conn)
        latest = MIGRATIONS[-1][0]
        if current > latest:
            logger.warning("Storage schema v%d is newer than this code (v%d); using it as is.", current, latest)
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
            logger.warning("Storage: %s failed (%s: %s)", what, type(ex).__name__, ex)
            try:
                with self._lock:
                    if self._conn.in_transaction:
                        self._conn.execute("ROLLBACK")
            except Exception:
                pass
            return default

    def _write(self, what: str, fn: Callable, default=None):
        """Runs fn(conn) inside one transaction (rolled back on error)."""
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

    def checkpoint(self) -> None:
        self._run("checkpoint", lambda c: c.execute("PRAGMA wal_checkpoint(PASSIVE)"))

    # --- meta ---

    def set_meta(self, key: str, value) -> bool:
        return bool(self._write("set meta", lambda c: c.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(key), _dumps(value))).rowcount, 0))

    def get_meta(self, key: str, default=None):
        row = self._run("get meta", lambda c: c.execute(
            "SELECT value FROM meta WHERE key = ?", (str(key),)).fetchone(), None)
        if not row:
            return default
        value = _loads(row[0])
        return default if value is None else value

    # --- items ---

    def upsert_items(self, source: str, items: Iterable, fetched_ts=None) -> int:
        """items: iterable of (item_id, item_ts, payload) — item_ts may be a
        datetime, an ISO string or None. Same (source, item_id) = one row
        (dedupe); the newer write wins. Returns how many rows were written."""
        fetched = norm_ts(fetched_ts) or iso()
        try:
            rows = [(source, str(item_id), norm_ts(item_ts), fetched, _dumps(payload) or "null")
                    for item_id, item_ts, payload in items]
        except (TypeError, ValueError) as ex:
            logger.warning("Storage: bad %s items (%s)", source, ex)
            return 0
        if not rows:
            return 0
        return self._write(f"upsert {source} items", lambda c: (c.executemany(
            "INSERT INTO items(source, item_id, item_ts, fetched_ts, payload) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(source, item_id) DO UPDATE SET item_ts=excluded.item_ts, "
            "fetched_ts=excluded.fetched_ts, payload=excluded.payload", rows), len(rows))[1], 0)

    def load_items(self, source: str, since=None, id_prefix: Optional[str] = None) -> list:
        """[{item_id, item_ts, fetched_ts, payload}] of one source, oldest
        item_ts first. since: only items with item_ts >= since. id_prefix:
        only item_ids starting with it. Unreadable payloads are skipped."""
        sql = "SELECT item_id, item_ts, fetched_ts, payload FROM items WHERE source = ?"
        args: list = [source]
        since_ts = norm_ts(since)
        if since_ts:
            sql += " AND item_ts >= ?"
            args.append(since_ts)
        if id_prefix:
            sql += " AND substr(item_id, 1, ?) = ?"
            args += [len(id_prefix), id_prefix]
        sql += " ORDER BY item_ts, item_id"
        rows = self._run(f"load {source} items", lambda c: c.execute(sql, args).fetchall(), [])
        out = []
        for item_id, item_ts, fetched_ts, raw in rows:
            payload = _loads(raw)
            if payload is None:
                continue
            out.append({"item_id": item_id, "item_ts": item_ts, "fetched_ts": fetched_ts, "payload": payload})
        return out

    def has_item(self, source: str, item_id: str) -> bool:
        return bool(self._run("look up item", lambda c: c.execute(
            "SELECT 1 FROM items WHERE source = ? AND item_id = ?", (source, str(item_id))).fetchone(), None))

    def delete_items(self, source: str, item_ids: Iterable) -> int:
        ids = [(source, str(i)) for i in item_ids]
        if not ids:
            return 0
        return self._write(f"delete {source} items", lambda c: sum(
            c.execute("DELETE FROM items WHERE source = ? AND item_id = ?", row).rowcount for row in ids), 0)

    def prune_items(self, source: str, older_than) -> int:
        """Deletes items whose item_ts is older than older_than (datetime/ISO)
        and items without an item_ts. Returns how many were deleted."""
        cutoff = norm_ts(older_than)
        if not cutoff:
            return 0
        return self._write(f"prune {source} items", lambda c: c.execute(
            "DELETE FROM items WHERE source = ? AND (item_ts IS NULL OR item_ts < ?)",
            (source, cutoff)).rowcount, 0)

    def newest_item_ts(self, source: str) -> Optional[str]:
        row = self._run("newest item", lambda c: c.execute(
            "SELECT MAX(COALESCE(item_ts, fetched_ts)) FROM items WHERE source = ?", (source,)).fetchone(), None)
        return row[0] if row else None

    # --- flight positions ---

    def add_flight_positions(self, positions: Iterable[dict]) -> int:
        """positions: dicts with icao24, ts (datetime/ISO/unix) and optional
        lat, lon, altitude_m, heading_deg, velocity_ms, callsign, payload.
        The same (icao24, ts) is stored once (a re-poll updates it)."""
        rows = []
        for p in positions or []:
            if not isinstance(p, dict) or not p.get("icao24"):
                continue
            ts = norm_ts(p.get("ts"))
            if not ts:
                continue
            rows.append((str(p["icao24"]).lower(), ts, _float(p.get("lat")), _float(p.get("lon")),
                         _float(p.get("altitude_m")), _float(p.get("heading_deg")), _float(p.get("velocity_ms")),
                         p.get("callsign"), _dumps(p.get("payload"))))
        if not rows:
            return 0
        return self._write("add flight positions", lambda c: (c.executemany(
            "INSERT INTO flight_positions(icao24, ts, lat, lon, altitude_m, heading_deg, velocity_ms, callsign, "
            "payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(icao24, ts) DO UPDATE SET "
            "lat=excluded.lat, lon=excluded.lon, altitude_m=excluded.altitude_m, "
            "heading_deg=excluded.heading_deg, velocity_ms=excluded.velocity_ms, "
            "callsign=excluded.callsign, payload=excluded.payload", rows), len(rows))[1], 0)

    def load_flight_positions(self, icao24: Optional[str] = None, since=None, latest_only: bool = False) -> list:
        """Oldest first. latest_only: just the newest row per aircraft."""
        where, args = ["1=1"], []
        if icao24:
            where.append("icao24 = ?")
            args.append(str(icao24).lower())
        since_ts = norm_ts(since)
        if since_ts:
            where.append("ts >= ?")
            args.append(since_ts)
        cols = "icao24, ts, lat, lon, altitude_m, heading_deg, velocity_ms, callsign, payload"
        if latest_only:
            sql = (f"SELECT {cols} FROM flight_positions f WHERE {' AND '.join(where)} AND ts = "
                   "(SELECT MAX(ts) FROM flight_positions g WHERE g.icao24 = f.icao24) ORDER BY ts")
        else:
            sql = f"SELECT {cols} FROM flight_positions WHERE {' AND '.join(where)} ORDER BY ts, icao24"
        rows = self._run("load flight positions", lambda c: c.execute(sql, args).fetchall(), [])
        return [{"icao24": r[0], "ts": r[1], "lat": r[2], "lon": r[3], "altitude_m": r[4], "heading_deg": r[5],
                 "velocity_ms": r[6], "callsign": r[7], "payload": _loads(r[8])} for r in rows]

    def prune_flight_positions(self, older_than) -> int:
        cutoff = norm_ts(older_than)
        if not cutoff:
            return 0
        return self._write("prune flight positions", lambda c: c.execute(
            "DELETE FROM flight_positions WHERE ts < ?", (cutoff,)).rowcount, 0)

    # --- prices ---

    def add_prices(self, points: Iterable) -> int:
        """points: iterable of (symbol, ts, price, source). One row per
        (symbol, ts, source); a repeated point overwrites the price."""
        rows = []
        for point in points or []:
            try:
                symbol, ts, price, source = point
            except (TypeError, ValueError):
                continue
            stamp, value = norm_ts(ts), _float(price)
            if not symbol or not stamp or value is None:
                continue
            rows.append((str(symbol), stamp, value, str(source or "")))
        if not rows:
            return 0
        return self._write("add prices", lambda c: (c.executemany(
            "INSERT INTO prices(symbol, ts, price, source) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(symbol, ts, source) DO UPDATE SET price=excluded.price", rows), len(rows))[1], 0)

    def load_prices(self, symbol: str, since=None, source: Optional[str] = None) -> list:
        """[(ts, price, source)] oldest first."""
        sql, args = "SELECT ts, price, source FROM prices WHERE symbol = ?", [symbol]
        since_ts = norm_ts(since)
        if since_ts:
            sql += " AND ts >= ?"
            args.append(since_ts)
        if source is not None:
            sql += " AND source = ?"
            args.append(source)
        sql += " ORDER BY ts"
        return self._run(f"load prices {symbol}", lambda c: [tuple(r) for r in c.execute(sql, args).fetchall()], [])

    def prune_prices(self, older_than) -> int:
        cutoff = norm_ts(older_than)
        if not cutoff:
            return 0
        return self._write("prune prices", lambda c: c.execute(
            "DELETE FROM prices WHERE ts < ?", (cutoff,)).rowcount, 0)

    # --- source state ---

    def mark_attempt(self, source: str, ts=None, status: Optional[dict] = None) -> bool:
        return bool(self._write("mark attempt", lambda c: c.execute(
            "INSERT INTO source_state(source, last_attempt_ts, status_json) VALUES (?, ?, ?) "
            "ON CONFLICT(source) DO UPDATE SET last_attempt_ts=excluded.last_attempt_ts, "
            "status_json=COALESCE(excluded.status_json, source_state.status_json)",
            (source, norm_ts(ts) or iso(), _dumps(status))).rowcount, 0))

    def mark_success(self, source: str, ts=None, status: Optional[dict] = None) -> bool:
        """A success is also an attempt."""
        stamp = norm_ts(ts) or iso()
        return bool(self._write("mark success", lambda c: c.execute(
            "INSERT INTO source_state(source, last_success_ts, last_attempt_ts, status_json) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(source) DO UPDATE SET "
            "last_success_ts=excluded.last_success_ts, last_attempt_ts=excluded.last_attempt_ts, "
            "status_json=COALESCE(excluded.status_json, source_state.status_json)",
            (source, stamp, stamp, _dumps(status))).rowcount, 0))

    def get_state(self, source: str) -> Optional[dict]:
        row = self._run("read source state", lambda c: c.execute(
            "SELECT last_success_ts, last_attempt_ts, status_json FROM source_state WHERE source = ?",
            (source,)).fetchone(), None)
        if not row:
            return None
        return {"source": source, "last_success_ts": row[0], "last_attempt_ts": row[1], "status": _loads(row[2])}

    # --- AI outputs ---

    def save_ai_output(self, feature: str, model: Optional[str], output_text: str,
                       input_data: Optional[dict] = None, provenance: Optional[dict] = None,
                       usage: Optional[dict] = None, tier: Optional[str] = None, ts=None) -> Optional[int]:
        """Stores one AI output; returns its row id (None on failure). ts
        defaults to the provenance's generated_at, else now."""
        stamp = norm_ts(ts) or (norm_ts(provenance.get("generated_at")) if isinstance(provenance, dict) else None) \
            or iso()
        return self._write("save AI output", lambda c: c.execute(
            "INSERT INTO ai_outputs(ts, feature, model, tier, input_json, output_text, "
            "provenance_json, usage_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (stamp, feature, model, tier, _dumps(input_data), output_text,
             _dumps(provenance), _dumps(usage))).lastrowid, None)

    def list_ai_outputs(self, feature: Optional[str] = None, limit: int = 50, since=None) -> list:
        """Newest first."""
        sql = ("SELECT id, ts, feature, model, tier, input_json, output_text, provenance_json, usage_json "
               "FROM ai_outputs WHERE 1=1")
        args: list = []
        if feature:
            sql += " AND feature = ?"
            args.append(feature)
        since_ts = norm_ts(since)
        if since_ts:
            sql += " AND ts >= ?"
            args.append(since_ts)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(max(1, int(limit)))
        rows = self._run("list AI outputs", lambda c: c.execute(sql, args).fetchall(), [])
        return [{"id": r[0], "ts": r[1], "feature": r[2], "model": r[3], "tier": r[4],
                 "input": _loads(r[5]), "output_text": r[6], "provenance": _loads(r[7]),
                 "usage": _loads(r[8])} for r in rows]

    def prune_ai_outputs(self, days: float, now: Optional[datetime] = None) -> int:
        cutoff = iso((now or utc_now()) - timedelta(days=days))
        return self._write("prune AI outputs", lambda c: c.execute(
            "DELETE FROM ai_outputs WHERE ts < ?", (cutoff,)).rowcount, 0)


# Short alias (the name used in the storage design notes).
Store = Storage
