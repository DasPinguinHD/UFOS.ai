"""Options archive CLI (SP §21.4, OS1) — the entry point of the background service and of manual runs.

    .\\.venv\\Scripts\\python.exe tools\\options_archive.py service            (what the scheduled task runs)
    .\\.venv\\Scripts\\python.exe tools\\options_archive.py run-once           (one snapshot now + today's missed OI / FRED)
    .\\.venv\\Scripts\\python.exe tools\\options_archive.py run-slot 09:55     (a slot of today: HH:MM | oi | fred | eod)
    .\\.venv\\Scripts\\python.exe tools\\options_archive.py status             (JSON: DB + status file)
    .\\.venv\\Scripts\\python.exe tools\\options_archive.py gaps [--date YYYY-MM-DD]   (JSON gap report of a NY day)

Data collection only (GET allow-list, no trading, no AI, no listening port); keys only from the Windows Credential
Manager. Exit codes: 0 ok · 1 partial / failed run · 2 keys missing / unreadable / rejected · 3 stopped (network /
clock / bad config / calendar / not the backend venv) · 4 HTTP 429 · 5 another archive process holds the lock (e.g. run-once while the
service runs) · 6 refused in sandbox mode (run-once / run-slot) · 7 internal error (logged). Stop the service:
Ctrl+C, the scheduled task's "End", or create ``data/options_archive.stop``. ``run-once`` / ``run-slot`` write only
the ``manual`` block of the status file (never the service's state / heartbeat); ``run-slot`` outside the slot's
window is recorded as ``manual:<slot>``.

Part of UFOS.ai, a proof-of-concept portfolio project; data collection only.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date, datetime, timezone
from typing import List, Optional

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)
if __name__ == "__main__":              # S10-H: refuse the system Python before any non-stdlib import
    from strategy_platform.core import venv_guard
    venv_guard.require("options_archive.py", BACKEND_DIR, 3)

USAGE = ("usage: options_archive.py service | run-once | run-slot <HH:MM|oi|fred|eod> | status | "
         "gaps [--date YYYY-MM-DD]")


EXIT_SANDBOX, EXIT_CRASH = 6, 7


def main(argv: Optional[List[str]] = None, **kw) -> int:
    out = kw.get("out", print)
    try:
        return _main(argv, **kw)
    except Exception as ex:          # noqa: BLE001 — never a raw traceback on the console; the log has it (redacted)
        out(f"FAIL internal error ({type(ex).__name__}) — see data/logs/options_archive.log")
        return EXIT_CRASH


def _main(argv: Optional[List[str]] = None, *, data_dir: Optional[str] = None, transport=None, secrets_module=None,
          now=None, sleep=None, out=print, max_slots: Optional[int] = None) -> int:
    from strategy_platform.core import calendars
    from strategy_platform.options_archive import config as oconfig
    from strategy_platform.options_archive import db as odb
    from strategy_platform.options_archive import report, slots
    from strategy_platform.options_archive import registration
    from strategy_platform.options_archive.service import Archive, InstanceLock, setup_logging
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("service", "run-once", "run-slot", "status", "gaps"):
        out(USAGE)
        return 1
    cmd = argv[0]
    try:
        cfg = oconfig.load(os.path.join(data_dir, "options_archive_config.json") if data_dir else None)
    except (ValueError, OSError) as ex:
        out(f"FAIL config: {ex}")
        return 3
    p = oconfig.paths(data_dir)
    nowf = now or (lambda: datetime.now(timezone.utc))
    if cmd == "status":
        out(json.dumps(report.status_payload(cfg, nowf(), data_dir, registration.availability()), indent=1,
                       default=str))
        return 0
    if cmd == "gaps":
        d = nowf().astimezone(slots.NY).date()
        if len(argv) == 3 and argv[1] == "--date":
            try:
                d = date.fromisoformat(argv[2])
            except ValueError:
                out(USAGE)
                return 1
        elif len(argv) != 1:
            out(USAGE)
            return 1
        conn = odb.open_ro(p["db"])
        try:
            out(json.dumps(report.day_report(conn, d, cfg, nowf()), indent=1, default=str))
        finally:
            if conn is not None:
                conn.close()
        return 0
    if cmd in ("run-once", "run-slot") and os.path.exists(os.path.join(data_dir or oconfig.DATA_DIR,
                                                                         "SANDBOX_ACTIVE")):
        out("REFUSED the backend is in sandbox mode (data/SANDBOX_ACTIVE) — manual archive runs are disabled until "
            "sandbox mode is switched off (tools/sandbox.py off)")
        return EXIT_SANDBOX
    lock = InstanceLock(p["lock"])
    if not lock.acquire():
        out("STOP another options-archive process is running (lock held) — nothing done")
        return 5
    kw = {"transport": transport, "secrets_module": secrets_module, "now": nowf}
    if sleep is not None:
        kw["sleep"] = sleep
    try:
        arch = Archive(cfg, data_dir, mode="service" if cmd == "service" else "manual", **kw)
    except BaseException:
        lock.release()
        raise
    handler = setup_logging(p["log"], arch.hidden)
    try:
        if cmd == "service":
            if len(argv) != 1:
                out(USAGE)
                return 1
            return arch.serve(max_slots=max_slots)
        if cmd == "run-once":
            results = arch.run_once()
        else:
            if len(argv) != 2:
                out(USAGE)
                return 1
            d = nowf().astimezone(slots.NY).date()
            try:
                s = slots.find(d, argv[1], cfg)
            except calendars.CalendarUnavailable as ex:
                out(f"STOP {ex.user_message}")
                return 3
            if s is None:
                out(f"FAIL no slot {argv[1]!r} today ({d}, NY) — not a trading day or not in the plan")
                return 1
            results = [arch.run_slot(s)]
        for r in results:
            out(f"{r['status'].upper():<8} run {r['id']} {r['kind']} {r['slot_label']}: {r['requests']} request(s), "
                f"{r['gaps']} gap(s)" + (f" — {r['error']}" if r["error"] else ""))
        arch.finish_manual(results)
        codes = [r["exit_code"] for r in results]
        return max(codes) if codes else 0
    except calendars.CalendarUnavailable as ex:
        out(f"STOP {ex.user_message}")
        return 3
    except Exception:
        logging.getLogger("options_archive").exception("options archive %s crashed", cmd)
        raise
    finally:
        logging.getLogger("options_archive").removeHandler(handler)
        handler.close()
        arch.close()
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
