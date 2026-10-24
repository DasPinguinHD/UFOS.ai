"""Runtime logging: console + rotating file data/logs/backend.log.

Copied from Global Monitoring's logging_setup.py (2026-09-24) so the
HedgeFund backend's diagnostics survive too — the app starts this backend
as a hidden subprocess, so console output alone would be invisible.
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
