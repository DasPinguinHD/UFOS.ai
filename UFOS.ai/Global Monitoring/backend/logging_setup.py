"""Runtime (operational) logging setup — console + a real log file on disk.

This is deliberately separate from logger.py's log_event(), which only
writes structured JSONL *data* events (an emitted marker, a source_status
update) to data/logs/<date>.jsonl. Everything else — every logger.info(),
logger.warning(), logger.exception() call across main.py, every collector,
gdelt_client.py, assess.py — is "why did X fail" operational diagnostics,
and until now that only ever went to the console via logging.basicConfig().

That was effectively invisible in normal use: MainWindow.xaml.cs starts this
backend as a hidden subprocess with its stdout/stderr redirected, and pipes
what it captures into Debug.WriteLine — which only shows up in an attached
debugger's Output window. Running the built app normally (not under a
debugger), or manually via `python main.py` in a closed terminal, meant
none of these diagnostics were ever kept anywhere a person could go back and
read (2026-09-22, reported as "the logging system is currently empty" while
trying to diagnose the Assess button).

configure_logging() fixes that by giving the root logger a rotating file
handler pointed at data/logs/backend.log, in addition to the console. Since
every module's `logging.getLogger(__name__)` propagates up to the root
logger by default, this one call is what makes ALL of the app's operational
log lines durable — including aiohttp's own per-request access log (method,
path, status, timing), which starts appearing automatically too once the
root logger's level allows INFO through, and is the most direct way to
confirm whether a given frontend request (e.g. POST /assess, or its OPTIONS
preflight) ever actually reached this server at all.
"""
import logging
import logging.handlers
import os

from config import LOG_DIR

RUNTIME_LOG_PATH = os.path.join(LOG_DIR, "backend.log")

_configured = False


def configure_logging(level: int = logging.INFO) -> None:
    global _configured
    if _configured:
        return
    _configured = True

    os.makedirs(LOG_DIR, exist_ok=True)

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    # 2MB x 3 backups is plenty for a proof-of-concept run's diagnostics
    # without growing unbounded across many long-running sessions.
    file_handler = logging.handlers.RotatingFileHandler(
        RUNTIME_LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(console_handler)
    root.addHandler(file_handler)

    logging.getLogger(__name__).info(
        "Runtime logging configured — console + %s", RUNTIME_LOG_PATH
    )
