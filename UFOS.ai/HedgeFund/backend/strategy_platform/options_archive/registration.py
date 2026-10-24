"""The scheduled-task registration DATA (SP §21.11; lead decision 2026-10-24, review M1): the backend supplies paths +
the human summary only — NO script text, NO commands. The WPF builds the task XML from its FIXED template (task
``UFOS.ai OptionsArchive`` in the ROOT task folder: logon trigger of the current user, InteractiveToken,
LeastPrivilege, IgnoreNew, restart on failure 3× / 5 min) and registers it with ``%SystemRoot%\\System32\\schtasks.exe`` as the current user (no admin, no
stored password). Nothing here registers or starts anything."""
from __future__ import annotations

import getpass
import os
import sys
from typing import Optional

from .config import Config, paths

TASK_NAME = "UFOS.ai OptionsArchive"     # ROOT task folder (lead 2026-10-24: no subfolder for a standard user)
SCRIPT = os.path.join("tools", "options_archive.py")
SANDBOX_NOTE = ("The backend runs in SANDBOX mode (click-test profile) — registering the options archive and manual "
                "runs are disabled until sandbox mode is switched off (tools/sandbox.py off). Unregister still works.")
PYTHONW_NOTE = ("pythonw.exe was not found next to the backend's Python — the background task cannot be registered "
                "(it would open a console window). Check the backend's .venv.")


def pythons(executable: Optional[str] = None):
    """(python.exe, pythonw.exe | None) of the backend's venv — next to ``sys.executable``."""
    exe = os.path.abspath(executable or sys.executable)
    folder = os.path.dirname(exe)
    pw = os.path.join(folder, "pythonw.exe")
    return exe, (pw if os.path.exists(pw) else None)


def in_sandbox() -> bool:
    try:
        import sandbox_profile
        return bool(sandbox_profile.is_sandbox())
    except Exception:                # noqa: BLE001 — an unreadable marker stops the backend itself; here: not sandbox
        return False


def availability(executable: Optional[str] = None, sandbox: Optional[bool] = None) -> dict:
    """Whether Register / Run now may be offered: not in sandbox mode, and pythonw.exe exists (no console fallback)."""
    sb = in_sandbox() if sandbox is None else bool(sandbox)
    _exe, pyw = pythons(executable)
    reason = SANDBOX_NOTE if sb else (None if pyw else PYTHONW_NOTE)
    return {"available": reason is None, "unavailable_reason": reason, "sandbox": sb}


def payload(cfg: Config, data_dir: Optional[str] = None, executable: Optional[str] = None,
            backend_dir: Optional[str] = None, user: Optional[str] = None, sandbox: Optional[bool] = None) -> dict:
    from .config import BACKEND_DIR
    workdir = os.path.abspath(backend_dir or BACKEND_DIR)
    p = paths(data_dir)
    exe, pyw = pythons(executable)
    who = user or getpass.getuser()
    n = len(cfg.symbols)
    fixed = ", ".join(cfg.fixed_slots)
    av = availability(executable, sandbox)
    return {
        "task_name": TASK_NAME, **av,
        "backend_dir": workdir, "workdir": workdir, "pythonw": pyw, "python": exe,
        "tool": os.path.join(workdir, SCRIPT), "service_args": ["service"], "run_once_args": ["run-once"],
        "summary": {
            "what": (f"Collects {' / '.join(cfg.symbols)} option chains (Alpaca, indicative feed), open interest and the "
                     f"FRED series {', '.join(cfg.fred_series)} into the local archive — data collection only: no "
                     "trading, no orders, no AI."),
            "when": (f"Starts right after you confirm, and again at every Windows logon of {who}; it keeps running in "
                     f"the background. On NYSE trading days (New York time): open interest {cfg.oi_slot}, FRED "
                     f"{cfg.fred_slot}, chain snapshots every {cfg.cadence_minutes} min {cfg.intraday_first}–"
                     f"{cfg.intraday_last} plus {fixed}, end of day {cfg.eod_after_close_minutes} min after the close. "
                     "The PC must be on and you logged on — missed slots are recorded as gaps, never filled in."),
            "account": (f"Runs as your Windows user ({who}) with normal (limited) rights — no administrator rights, no "
                        "stored password."),
            "keys": (f"Reads the Alpaca PAPER keys and the FRED key from the Windows Credential Manager at each run and "
                     f"never writes them anywhere. At most {cfg.rate_per_min} Alpaca requests per minute (the app keeps "
                     f"its own 150 + 10; {n} symbols)."),
            "data": (f"Archive {p['db']}, status {p['status']}, log {p['log']} — on this PC only; it opens no port."),
            "stop": ("Unregister… ends the running archive now and removes the task (the archive data stay). Task "
                     "Scheduler → End stops it until the next logon. The file "
                     f"{p['stop']} stops it (and keeps it from collecting) until you delete that file."),
        },
    }
