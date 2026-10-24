"""Local SQLite store for the LiveStreamAgent (Fed Agent) backend (2026-09-25).

One database file per module (compartmentalization — the Global Monitoring
backend has its own, independent copy of this pattern):

    %LOCALAPPDATA%\\UFOS.ai\\data\\livestream.sqlite
    (~/.local/share/UFOS.ai/data/ when LOCALAPPDATA is not set;
     UFOS_LIVESTREAM_DB = full file path overrides it, e.g. for tests)

Tables:
- ``verdicts``    every verdict that was broadcast (full payload incl.
                  provenance/usage), kept VERDICT_RETENTION_DAYS (90).
- ``analyses``    every per-segment analysis result, same retention.
- ``ai_results``  one compact row per AI output (verdict or analysis):
                  created_at, feature, model, input_summary, output_text,
                  provenance_json, usage_json; kept AI_RESULTS_RETENTION_DAYS
                  (1 year).

Rules: WAL mode, schema versioned with ``PRAGMA user_version`` (MIGRATIONS),
and it never crashes the backend — if the file can't be opened or migrated,
a warning is logged and an in-memory database is used; any later SQLite
error is logged and the call returns a neutral default. Pure stdlib.
"""
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

logger = logging.getLogger(__name__)

DB_ENV = "UFOS_LIVESTREAM_DB"
DB_FILENAME = "livestream.sqlite"
VERDICT_RETENTION_DAYS = 90
AI_RESULTS_RETENTION_DAYS = 365

# Each entry: (version, SQL script). Append only — never edit a shipped step.
MIGRATIONS = [
    (1, """
        CREATE TABLE IF NOT EXISTS verdicts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at    TEXT NOT NULL,
            verdict       TEXT,
            ticker        TEXT,
            confidence    INTEGER,
            reason        TEXT,
            model         TEXT,
            payload_json  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_verdicts_created ON verdicts(created_at);

        CREATE TABLE IF NOT EXISTS analyses (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at    TEXT NOT NULL,
            summary       TEXT,
            model         TEXT,
            payload_json  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_analyses_created ON analyses(created_at);

        CREATE TABLE IF NOT EXISTS ai_results (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at       TEXT NOT NULL,
            feature          TEXT NOT NULL,
            model            TEXT,
            input_summary    TEXT,
            output_text      TEXT,
            provenance_json  TEXT,
            usage_json       TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_ai_results_created ON ai_results(created_at);
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


def _created_at(payload: dict, fallback: Optional[datetime]) -> str:
    """The payload's own provenance time (what the WPF shows) if parseable."""
    prov = payload.get("provenance") if isinstance(payload, dict) else None
    value = prov.get("generated_at") if isinstance(prov, dict) else None
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return _iso(dt)
        except ValueError:
            pass
    return _iso(fallback or _utc_now())


def _model(payload: dict) -> Optional[str]:
    prov = payload.get("provenance") if isinstance(payload, dict) else None
    return prov.get("model") if isinstance(prov, dict) else None


def _user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


class LocalStore:
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
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=5, isolation_level=None, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._migrate(conn)
            logger.info("Local store: %s (schema v%d)", self.path, _user_version(conn))
            return conn
        except Exception as ex:
            logger.warning("Local store %s unavailable (%s: %s) - using an in-memory database; "
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

    # --- verdicts / analyses ---

    def add_verdict(self, verdict: dict, created_at: Optional[datetime] = None) -> bool:
        def op(c):
            conf = verdict.get("confidence")
            c.execute(
                "INSERT INTO verdicts(created_at, verdict, ticker, confidence, reason, model, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_created_at(verdict, created_at), verdict.get("verdict"), verdict.get("ticker"),
                 int(conf) if isinstance(conf, (int, float)) else None, verdict.get("reason"),
                 _model(verdict), json.dumps(verdict, ensure_ascii=False, default=str)))
            return True
        return self._run("save verdict", op, False)

    def recent_verdicts(self, days: float = VERDICT_RETENTION_DAYS, limit: int = 500,
                        now: Optional[datetime] = None) -> list:
        """Stored verdict payloads of the last `days`, newest first."""
        cutoff = _iso((now or _utc_now()) - timedelta(days=days))
        rows = self._run("load verdicts", lambda c: c.execute(
            "SELECT payload_json FROM verdicts WHERE created_at >= ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (cutoff, int(limit))).fetchall(), [])
        out = []
        for (raw,) in rows:
            try:
                payload = json.loads(raw)
            except Exception:
                continue
            if isinstance(payload, dict):
                out.append(payload)
        return out

    def add_analysis(self, analysis: dict, created_at: Optional[datetime] = None) -> bool:
        def op(c):
            c.execute(
                "INSERT INTO analyses(created_at, summary, model, payload_json) VALUES (?, ?, ?, ?)",
                (_created_at(analysis, created_at), analysis.get("summary"), _model(analysis),
                 json.dumps(analysis, ensure_ascii=False, default=str)))
            return True
        return self._run("save analysis", op, False)

    # --- AI results ---

    def add_ai_result(self, feature: str, model: Optional[str], input_summary: str, output_text: str,
                      provenance: Optional[dict] = None, usage: Optional[dict] = None,
                      created_at: Optional[datetime] = None) -> bool:
        def op(c):
            c.execute(
                "INSERT INTO ai_results(created_at, feature, model, input_summary, output_text, "
                "provenance_json, usage_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_iso(created_at or _utc_now()), feature, model, (input_summary or "")[:2000], output_text,
                 json.dumps(provenance, ensure_ascii=False) if provenance is not None else None,
                 json.dumps(usage, ensure_ascii=False) if usage is not None else None))
            return True
        return self._run("save AI result", op, False)

    # --- housekeeping ---

    def prune_all(self, now: Optional[datetime] = None) -> dict:
        """Startup + daily cleanup. Returns how many rows each table lost."""
        now = now or _utc_now()
        verdict_cutoff = _iso(now - timedelta(days=VERDICT_RETENTION_DAYS))
        ai_cutoff = _iso(now - timedelta(days=AI_RESULTS_RETENTION_DAYS))

        def delete(table, cutoff):
            return lambda c: c.execute(f"DELETE FROM {table} WHERE created_at < ?", (cutoff,)).rowcount

        result = {
            "verdicts": self._run("prune verdicts", delete("verdicts", verdict_cutoff), 0),
            "analyses": self._run("prune analyses", delete("analyses", verdict_cutoff), 0),
            "ai_results": self._run("prune AI results", delete("ai_results", ai_cutoff), 0),
        }
        self._run("checkpoint", lambda c: c.execute("PRAGMA wal_checkpoint(PASSIVE)"))
        return result
