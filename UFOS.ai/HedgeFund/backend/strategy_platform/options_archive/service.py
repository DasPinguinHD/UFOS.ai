"""The archive process (SP §21.4 / §21.11): single instance (lock file), sleeps until the next NY slot, runs it,
writes the status file + log; exits cleanly on Ctrl+C / the stop file ``data/options_archive.stop`` / the scheduled
task's End. No listening port.

Slot windows (``in_window``) — a slot is collected AS THAT SLOT only inside its window, otherwise it is missing:
chain slots ``slot − lead … slot``; ``eod`` ``slot − lead … slot + eod_grace_minutes``; ``oi`` / ``fred`` from the
slot time until the close of THAT session (daily data, honest ``observed`` stamps). Missed slots (PC off / asleep)
are never run later under their old slot. ``run-slot`` outside the window → a ``manual:<slot>`` run.

Resilience (review M3): an unexpected error inside one run is logged (redacted, with traceback) and recorded as a
``failed`` run + ``crash`` gap; the service continues with the next slot. A fatal error in the loop itself → state
``error`` with a sanitized ``last_error`` and exit 7. Status-file writes never raise (retried on Windows locks).
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import re
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional

from strategy_platform.core import calendars

from . import db as odb
from .collector import Collector
from .config import Config, paths
from .report import merge_status, read_status, service_status, write_status
from .slots import NY, Slot, day_slots, next_slot
from .transport import Http, RateGate

logger = logging.getLogger("options_archive")
EXIT_CRASH = 7                    # an internal error (a bug) — distinct from 1 (partial / failed run)


class RedactFilter(logging.Filter):
    """Removes the secrets the collector read (and any ``api_key=`` / APCA header value) from every log record —
    including a formatted traceback (``logger.exception``), which is folded into the message first."""

    def __init__(self, hide: Callable[[], List[str]]):
        super().__init__()
        self.hide = hide

    def redact(self, msg: str) -> str:
        for s in self.hide():
            if s and len(s) >= 4:
                msg = msg.replace(s, "***")
        return re.sub(r"(?i)(api_key=|APCA-API-(?:KEY-ID|SECRET-KEY)['\"]?\s*[:=]\s*['\"]?)[^&\s'\"]+", r"\1***", msg)

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + "".join(traceback.format_exception(*record.exc_info)).rstrip()
            record.exc_info, record.exc_text = None, None
        record.msg, record.args = self.redact(msg), None
        return True


def setup_logging(log_path: str, hide: Callable[[], List[str]]) -> logging.Handler:
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    h = logging.handlers.RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    h.addFilter(RedactFilter(hide))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)
    logger.propagate = False         # only the redacting handler — never an unfiltered root handler
    return h


def sanitized_error(ex: BaseException, clean: Callable[[Optional[str]], Optional[str]]) -> str:
    """``Type: message`` with the read secrets removed, ≤ 300 characters (status file / run row)."""
    return clean(f"{type(ex).__name__}: {ex}") or type(ex).__name__


class InstanceLock:
    """An OS file lock (msvcrt on Windows, flock elsewhere) — released by the OS when the process dies."""

    def __init__(self, path: str):
        self.path, self.fh = path, None

    def acquire(self) -> bool:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self.fh = fh
        return True

    def release(self) -> None:
        if self.fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        finally:
            self.fh.close()
            self.fh = None


class Archive:
    """``mode="service"``: owns the status file's service fields + heartbeat. ``mode="manual"`` (run-once / run-slot):
    never touches them — it writes only the ``manual`` block when it finishes (review m11)."""

    def __init__(self, cfg: Config, data_dir: Optional[str] = None, *, transport=None, secrets_module=None,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic,
                 mode: str = "service"):
        self.cfg, self.paths, self.mode = cfg, paths(data_dir), mode
        self.now, self.sleep = now, sleep
        now_ms = lambda: int(self.now().timestamp() * 1000)   # noqa: E731
        self.conn = odb.open_rw(self.paths["db"], now_ms)
        self.http = Http(transport, sleep, monotonic, gate=RateGate(cfg.rate_per_min, monotonic, sleep),
                         retries=cfg.retries, retry_sleep=cfg.retry_sleep_s, retry_on_5xx=True,
                         timeouts=cfg.timeouts_s, page_sleep=cfg.page_sleep_s, max_pages=cfg.max_pages,
                         progress=self._progress, wall=lambda: self.now().timestamp())
        self.collector = Collector(self.conn, cfg, self.http, secrets_module=secrets_module, now_ms=now_ms)
        self.state, self.last_error = "idle", None
        self._last_beat = None

    def hidden(self) -> List[str]:
        return list(self.collector._hide)

    def clean(self, text: Optional[str]) -> Optional[str]:
        return self.collector.clean(text)

    def close(self) -> None:
        self.conn.close()

    # --- status file ----------------------------------------------------------------------------------------
    def write_status(self, state: Optional[str] = None) -> bool:
        """Service mode only (manual mode: no-op). Keeps the ``manual`` block. Never raises."""
        if state:
            self.state = state
        if self.mode != "service":
            return False
        try:
            payload = service_status(self.conn, self.cfg, self.now(), self.state, os.getpid(), self.last_error)
            prev = read_status(self.paths["status"]) or {}
            if prev.get("manual") is not None:
                payload["manual"] = prev["manual"]
            ok = write_status(self.paths["status"], payload, sleep=self.sleep)
        except Exception as ex:      # noqa: BLE001 — a status problem never stops the collection
            logger.warning("status file not written: %s", type(ex).__name__)
            return False
        if not ok:
            logger.warning("status file not written (locked?) — retried at the next heartbeat")
        self._last_beat = self.now()
        return ok

    def _progress(self, _line: str) -> None:
        """Called per page / retry: keeps the heartbeat fresh during long runs (service mode)."""
        if self.mode == "service" and (self._last_beat is None or
                                       (self.now() - self._last_beat).total_seconds() >= self.cfg.heartbeat_s):
            self.write_status()

    def finish_manual(self, outs: List[dict]) -> bool:
        """``run-once`` / ``run-slot``: the ``manual`` block of the status file (service fields untouched)."""
        if not outs:
            return False
        rank = {"ok": 0, "partial": 1, "late": 2, "failed": 3}
        worst = max(outs, key=lambda o: rank.get(o["status"], 3))
        block = {"finished_at": self.now().isoformat(timespec="seconds").replace("+00:00", "Z"),
                 "status": worst["status"], "run_ids": [o["id"] for o in outs if o.get("id") is not None],
                 "exit_code": max(o["exit_code"] for o in outs), "error": worst.get("error")}
        return merge_status(self.paths["status"], "manual", block, sleep=self.sleep)

    # --- runs -----------------------------------------------------------------------------------------------
    def run(self, slot: Slot, manual: bool = False) -> dict:
        self.write_status("collecting")
        try:
            out = self.collector.run(slot, manual=manual)
        except Exception as ex:      # noqa: BLE001 — one broken run never stops the archive (review M3)
            err = sanitized_error(ex, self.clean)
            logger.exception("run %s %s crashed — recorded as failed, continuing", slot.kind, slot.label)
            rid = self.collector.record_crash(slot, manual, err)
            out = {"id": rid, "kind": "manual" if manual else slot.kind, "slot_label": slot.label,
                   "slot_ms": slot.ms, "status": "failed", "requests": 0, "pages": 0, "retries": 0, "gaps": 1,
                   "rows": 0, "error": err, "stopped": "crash", "exit_code": EXIT_CRASH}
        self.last_error = out["error"] if out["status"] == "failed" else None
        self.write_status("error" if out["stopped"] == "stop" and out["exit_code"] == 2 else "idle")
        return out

    def in_window(self, slot: Slot, now: datetime) -> bool:
        """May ``slot`` be collected AS THAT SLOT at ``now``? (see the module docstring)"""
        if slot.kind == "intraday":
            return slot.start_utc(self.cfg) <= now <= slot.utc
        if slot.kind == "eod":
            return slot.start_utc(self.cfg) <= now <= slot.utc + timedelta(minutes=self.cfg.eod_grace_minutes)
        d = slot.ny.date()
        if now.astimezone(NY).date() != d or now < slot.utc:
            return False
        bounds = calendars.session_bounds(d)
        return bool(bounds) and now < bounds[1]

    def _ran(self, slot: Slot) -> bool:
        return self.conn.execute("SELECT 1 FROM os_runs WHERE slot_ms = ? AND kind = ? LIMIT 1",
                                 (slot.ms, slot.kind)).fetchone() is not None

    def catch_up(self) -> List[dict]:
        """Today's missed ``oi`` / ``fred`` while the session is open and a missed ``eod`` within the grace — only
        slots still inside their window (never chain slots after their time)."""
        now = self.now()
        out = []
        for s in day_slots(now.astimezone(NY).date(), self.cfg):
            if s.kind in ("oi", "fred", "eod") and s.utc <= now and not self._ran(s) and self.in_window(s, now):
                out.append(self.run(s))
        return out

    def run_slot(self, slot: Slot) -> dict:
        """``run-slot``: as the slot inside its window, else a ``manual:<slot>`` run (never ranked as the slot's)."""
        if self.in_window(slot, self.now()):
            return self.run(slot)
        logger.info("run-slot %s outside its window — recorded as manual:%s", slot.label, slot.label)
        return self.run(slot, manual=True)

    def run_once(self) -> List[dict]:
        """``run-once``: today's missed oi / fred / eod (catch-up) + ONE manual chain snapshot now."""
        out = self.catch_up()
        now = self.now()
        out.append(self.run(Slot("intraday", now.astimezone(NY), "manual"), manual=True))
        return out

    def stop_requested(self) -> bool:
        return os.path.exists(self.paths["stop"])

    # --- the service loop -----------------------------------------------------------------------------------
    def serve(self, max_slots: Optional[int] = None) -> int:
        """The service loop (see the module docstring). Returns the exit code: 0 clean stop, 7 fatal error."""
        logger.info("options archive service started (pid %s, config %s)", os.getpid(), self.cfg.assumptions_hash[:12])
        self.write_status("idle")
        done, final = 0, "stopped"
        try:
            try:
                self.catch_up()
            except calendars.CalendarUnavailable:
                pass                                  # handled (state error, hourly retry) by the loop below
            cursor = self.now()
            while max_slots is None or done < max_slots:
                try:
                    nxt = next_slot(cursor, self.cfg)
                except calendars.CalendarUnavailable as ex:
                    self.last_error = ex.user_message
                    self.write_status("error")
                    logger.error("%s", ex.user_message)
                    if self._sleep_until(self.now() + timedelta(hours=1)):
                        return 0
                    continue
                if nxt is None:
                    if self._sleep_until(self.now() + timedelta(hours=1)):
                        return 0
                    continue
                if self._sleep_until(nxt.start_utc(self.cfg)):
                    return 0
                now = self.now()
                if self.in_window(nxt, now):
                    if not self._ran(nxt):
                        self.run(nxt)
                else:
                    logger.warning("slot %s %s skipped — the archive was not running in its window (recorded as "
                                   "missing)", nxt.ny.date(), nxt.label)
                cursor = nxt.start_utc(self.cfg) + timedelta(milliseconds=1)
                done += 1
            return 0
        except Exception as ex:      # noqa: BLE001 — fatal: shown as error, sanitized; RestartOnFailure restarts us
            self.last_error = f"the archive stopped on an internal error: {sanitized_error(ex, self.clean)}"
            logger.exception("options archive service crashed")
            final = "error"
            return EXIT_CRASH
        finally:
            self.write_status(final)
            logger.info("options archive service stopped (%s)", final)

    def _sleep_until(self, when: datetime) -> bool:
        """Sleeps in ≤ 30 s steps, heartbeat every ``heartbeat_s``; True = stop requested."""
        last_beat = self.now()
        while True:
            if self.stop_requested():
                return True
            now = self.now()
            if now >= when:
                return False
            self.sleep(min(30.0, (when - now).total_seconds()))
            if (self.now() - last_beat).total_seconds() >= self.cfg.heartbeat_s:
                self.write_status()
                last_beat = self.now()
