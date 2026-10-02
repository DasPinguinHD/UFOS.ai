"""Local SQLite store for the Global Monitoring backend (2026-09-25).

One database file per module (compartmentalization — the LiveStreamAgent
backend has its own, independent copy of this pattern):

    %LOCALAPPDATA%\\UFOS.ai\\data\\globalmonitoring.sqlite
    (~/.local/share/UFOS.ai/data/ when LOCALAPPDATA is not set;
     UFOS_GLOBALMONITORING_DB = full file path overrides it, e.g. for tests)

What it holds:
- ``events``          a mirror of the in-memory EventStore (events.py). It is
                      only a cache for each event's display window, so a
                      restart shows the globe's markers immediately instead
                      of waiting for every collector's first poll. Retention
                      per category: see ``EventRetention``.
- ``localnews_cache`` successful /localnews lookups (localnews.py), so a
                      restart doesn't re-query Google News for a city that
                      was looked up minutes ago (LOCALNEWS_CACHE_SECONDS).
- ``ai_results``      every AI output this backend produced (Assess/
                      Sentiment, local-news summaries) with its provenance
                      and usage; kept for AI_RESULTS_RETENTION_DAYS.

Rules: WAL mode, schema versioned with ``PRAGMA user_version`` (see
MIGRATIONS), and it never crashes the backend — if the file can't be opened
or migrated the store logs a warning and falls back to an in-memory
database; any later SQLite error is logged and the call returns a neutral
default. Pure stdlib on purpose (testable without the backend's venv).
"""
import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)

DB_ENV = "UFOS_GLOBALMONITORING_DB"
DB_FILENAME = "globalmonitoring.sqlite"
AI_RESULTS_RETENTION_DAYS = 365

# Each entry: (version, SQL script). Append only — never edit a shipped step.
MIGRATIONS = [
    (1, """
        CREATE TABLE IF NOT EXISTS events (
            id          TEXT PRIMARY KEY,
            category    TEXT NOT NULL,
            event_json  TEXT NOT NULL,
            updated_at  REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_events_category ON events(category);

        CREATE TABLE IF NOT EXISTS localnews_cache (
            cache_key     TEXT PRIMARY KEY,
            fetched_at    REAL NOT NULL,
            payload_json  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ai_results (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at       TEXT NOT NULL,
            feature          TEXT NOT NULL,
            model            TEXT,
            input_summary    TEXT,
            output_text      TEXT,
            provenance_json  TEXT,
            usage_json       TEXT,
            cache_key        TEXT,
            extra_json       TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_ai_results_created ON ai_results(created_at);
        CREATE INDEX IF NOT EXISTS ix_ai_results_cache_key ON ai_results(cache_key, created_at);
    """),
]


def default_path() -> str:
    override = os.environ.get(DB_ENV)
    if override:
        return override
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "UFOS.ai", "data", DB_FILENAME)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(value) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class EventRetention:
    """How long a cached event stays valid (its display window).

    - geopolitics (news): ``published`` (else ``timestamp``) younger than
      news_max_age_hours (config.NEWS_MAX_AGE_HOURS);
    - disaster: same rule as gdacs_collector.is_past() — ``ends_at`` not more
      than disaster_max_hours_since_end in the past (without ``ends_at``:
      last seen within that window);
    - flight: last seen (``timestamp``) within flight_max_age_hours;
    - markets: never expires by age — only the latest row per id is kept
      (the id is stable per symbol, so an upsert replaces it);
    - anything else: last seen within news_max_age_hours.
    """
    news_max_age_hours: float = 48.0
    disaster_max_hours_since_end: float = 48.0
    flight_max_age_hours: float = 2.0

    def is_expired(self, event: dict, now: Optional[datetime] = None) -> bool:
        now = now or _utc_now()
        category = event.get("category")
        if category == "markets":
            return False
        if category == "disaster":
            ref = _parse_iso(event.get("ends_at")) or _parse_iso(event.get("timestamp"))
            hours = self.disaster_max_hours_since_end
        elif category == "flight":
            ref = _parse_iso(event.get("timestamp"))
            hours = self.flight_max_age_hours
        elif category == "geopolitics":
            ref = _parse_iso(event.get("published")) or _parse_iso(event.get("timestamp"))
            hours = self.news_max_age_hours
        else:
            ref = _parse_iso(event.get("timestamp"))
            hours = self.news_max_age_hours
        if ref is None:
            return True  # no usable time -> can't tell if it's still current
        return ref < now - timedelta(hours=hours)


class CacheDB:
    """Thin, never-raising wrapper around this module's SQLite file."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or default_path()
        self.in_memory = False
        self._lock = threading.Lock()
        self._conn = self._open()

    # --- connection / migrations ---

    def _open(self) -> sqlite3.Connection:
        conn = None
        try:
            folder = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(folder, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=5, isolation_level=None, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._migrate(conn)
            logger.info("Local store: %s (schema v%d)", self.path, _user_version(conn))
            return conn
        except Exception as ex:
            logger.warning("Local store %s unavailable (%s: %s) — using an in-memory database; "
                           "nothing will survive a restart.", self.path, type(ex).__name__, ex)
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
        self.in_memory = True
        conn = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
        self._migrate(conn)
        return conn

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        current = _user_version(conn)
        latest = MIGRATIONS[-1][0]
        if current > latest:
            logger.warning("Local store schema v%d is newer than this code (v%d); using it as is.", current, latest)
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
            logger.warning("Local store: %s failed (%s: %s)", what, type(ex).__name__, ex)
            try:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
            except Exception:
                pass
            return default

    def schema_version(self) -> int:
        return self._run("read schema version", _user_version, 0)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    # --- events (mirror of events.EventStore) ---

    def upsert_event(self, event: dict) -> bool:
        def op(c):
            c.execute(
                "INSERT INTO events(id, category, event_json, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET category=excluded.category, "
                "event_json=excluded.event_json, updated_at=excluded.updated_at",
                (str(event["id"]), str(event.get("category") or ""),
                 json.dumps(event, ensure_ascii=False, default=str), time.time()))
            return True
        return self._run("upsert event", op, False)

    def delete_events(self, ids: Iterable[str]) -> int:
        ids = [str(i) for i in ids]
        if not ids:
            return 0

        def op(c):
            c.execute("BEGIN")
            n = 0
            for i in ids:
                n += c.execute("DELETE FROM events WHERE id = ?", (i,)).rowcount
            c.execute("COMMIT")
            return n
        return self._run("delete events", op, 0)

    def load_events(self, retention: EventRetention, now: Optional[datetime] = None) -> list:
        """All cached events still inside their display window, oldest first
        (expired and unreadable rows are deleted on the way)."""
        rows = self._run("load events", lambda c: c.execute("SELECT id, event_json FROM events").fetchall(), [])
        keep, drop = [], []
        for event_id, raw in rows:
            try:
                event = json.loads(raw)
                if not isinstance(event, dict) or "id" not in event or "category" not in event:
                    raise ValueError("not an event object")
            except Exception:
                drop.append(event_id)
                continue
            if retention.is_expired(event, now):
                drop.append(event_id)
            else:
                keep.append(event)
        if drop:
            self.delete_events(drop)
        keep.sort(key=lambda e: e.get("timestamp") or "")
        return keep

    def prune_events(self, retention: EventRetention, now: Optional[datetime] = None) -> int:
        before = self._run("count events", lambda c: c.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
        remaining = len(self.load_events(retention, now))
        return max(0, before - remaining)

    # --- local news lookups (localnews.py) ---

    def save_localnews(self, cache_key: str, fetched_at: float, payload: dict) -> bool:
        def op(c):
            c.execute(
                "INSERT INTO localnews_cache(cache_key, fetched_at, payload_json) VALUES (?, ?, ?) "
                "ON CONFLICT(cache_key) DO UPDATE SET fetched_at=excluded.fetched_at, "
                "payload_json=excluded.payload_json",
                (cache_key, float(fetched_at), json.dumps(payload, ensure_ascii=False, default=str)))
            return True
        return self._run("save local news", op, False)

    def load_localnews(self, max_age_seconds: float, now: Optional[float] = None) -> dict:
        """{cache_key: (fetched_at, payload)} for lookups younger than max_age_seconds."""
        cutoff = (now if now is not None else time.time()) - max_age_seconds
        rows = self._run("load local news", lambda c: c.execute(
            "SELECT cache_key, fetched_at, payload_json FROM localnews_cache WHERE fetched_at >= ?",
            (cutoff,)).fetchall(), [])
        out = {}
        for key, fetched_at, raw in rows:
            try:
                payload = json.loads(raw)
            except Exception:
                continue
            if isinstance(payload, dict):
                out[key] = (fetched_at, payload)
        return out

    def prune_localnews(self, max_age_seconds: float, now: Optional[float] = None) -> int:
        cutoff = (now if now is not None else time.time()) - max_age_seconds
        return self._run("prune local news", lambda c: c.execute(
            "DELETE FROM localnews_cache WHERE fetched_at < ?", (cutoff,)).rowcount, 0)

    # --- AI results (assess.py outputs) ---

    def add_ai_result(self, feature: str, model: Optional[str], input_summary: str, output_text: str,
                      provenance: Optional[dict] = None, usage: Optional[dict] = None,
                      cache_key: Optional[str] = None, extra: Optional[dict] = None,
                      created_at: Optional[datetime] = None) -> bool:
        def op(c):
            c.execute(
                "INSERT INTO ai_results(created_at, feature, model, input_summary, output_text, "
                "provenance_json, usage_json, cache_key, extra_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (_iso(created_at or _utc_now()), feature, model, (input_summary or "")[:2000], output_text,
                 json.dumps(provenance, ensure_ascii=False) if provenance is not None else None,
                 json.dumps(usage, ensure_ascii=False) if usage is not None else None,
                 cache_key,
                 json.dumps(extra, ensure_ascii=False) if extra is not None else None))
            return True
        return self._run("save AI result", op, False)

    def find_recent_ai_result(self, cache_key: str, max_age_seconds: float,
                              now: Optional[datetime] = None) -> Optional[dict]:
        """Newest AI result stored under cache_key within max_age_seconds, or None."""
        if not cache_key:
            return None
        cutoff = _iso((now or _utc_now()) - timedelta(seconds=max_age_seconds))
        row = self._run("find AI result", lambda c: c.execute(
            "SELECT created_at, feature, model, output_text, provenance_json, usage_json, extra_json "
            "FROM ai_results WHERE cache_key = ? AND created_at >= ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (cache_key, cutoff)).fetchone(), None)
        if not row:
            return None
        try:
            return {
                "created_at": row[0], "feature": row[1], "model": row[2], "output_text": row[3],
                "provenance": json.loads(row[4]) if row[4] else None,
                "usage": json.loads(row[5]) if row[5] else None,
                "extra": json.loads(row[6]) if row[6] else None,
            }
        except Exception:
            return None

    def prune_ai_results(self, retention_days: float = AI_RESULTS_RETENTION_DAYS,
                         now: Optional[datetime] = None) -> int:
        cutoff = _iso((now or _utc_now()) - timedelta(days=retention_days))
        return self._run("prune AI results", lambda c: c.execute(
            "DELETE FROM ai_results WHERE created_at < ?", (cutoff,)).rowcount, 0)

    # --- housekeeping ---

    def prune_all(self, retention: EventRetention, localnews_max_age_seconds: float) -> dict:
        """Startup + daily cleanup. Returns how many rows each table lost."""
        result = {
            "events": self.prune_events(retention),
            "localnews_cache": self.prune_localnews(localnews_max_age_seconds),
            "ai_results": self.prune_ai_results(),
        }
        self._run("checkpoint", lambda c: c.execute("PRAGMA wal_checkpoint(PASSIVE)"))
        return result


def _user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])
