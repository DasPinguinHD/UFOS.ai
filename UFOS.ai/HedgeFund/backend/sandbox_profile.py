"""Sandbox profile (strategy_platform/SPEC.md "Sandbox profile", user decision 2026-10-23).

A click-test profile for the incubation / monitoring flow that keeps the user's real databases clean. With the
marker ``data/SANDBOX_ACTIVE`` present when the backend starts:

- EVERY database path points to ``data/sandbox/`` (consistent copies made by ``tools/sandbox.py on`` with the
  SQLite backup API): hedgefund.db, newsroom.db, market_research.db (+ the paper texts next to hedgefund.db);
- broker accounts other than the internal paper broker are hard-blocked (no request to Alpaca trading or
  Binance; the state says "blocked in sandbox"); market DATA reads stay allowed;
- the old engine's autopilot and scheduled AI cycles are off; the kill switch only acts inside the sandbox;
- AI cost-log lines are tagged ``"mode": "sandbox"``;
- /health and the main state payloads carry ``"mode": "sandbox"`` (otherwise ``"mode": "normal"``).

The mode is decided ONCE per process (``state()``): a marker created or renamed while the backend runs has no
effect before the next start (tools/sandbox.py refuses to switch while the backend answers /health anyway).
A marker that is unreadable, or names a sandbox copy that is missing, stops the start (``SandboxError``) — the
backend never falls back to the real databases.

Part of UFOS.ai, a proof-of-concept portfolio project; PAPER / DEMO TRADING ONLY, not investment advice.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from typing import Optional

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
MARKER_NAME = "SANDBOX_ACTIVE"
SANDBOX_DIRNAME = "sandbox"
DB_FILES = ("hedgefund.db", "newsroom.db", "market_research.db")
PAPERS_DIRNAME = "papers"
MODE_NORMAL = "normal"
MODE_SANDBOX = "sandbox"

BLOCKED_TEXT = "blocked in sandbox"
BROKER_BLOCKED = ("blocked in sandbox — the sandbox profile never sends a request to a broker (Alpaca trading, "
                  "Binance); only the internal paper broker trades here")
ENGINE_OFF = "Sandbox mode: the old engine's autopilot and scheduled AI cycles are off."
PRICES_NOTE = "prices for copied positions may be unavailable while brokers are blocked"    # fix1023 B6
BANNER = ("SANDBOX — a click-test copy of the databases (data/sandbox). Broker accounts are blocked; nothing here is "
          "real evidence.")
from strategy_platform.core import fixture as _fixture  # noqa: E402 — the labels live with the API code
FIXTURE_TAG, FIXTURE_NAME, FIXTURE_NOTE = _fixture.TAG, _fixture.STRATEGY_NAME, _fixture.NOTE

logger = logging.getLogger(__name__)


class SandboxError(RuntimeError):
    """The sandbox profile cannot be used as asked (message for the user)."""

    def __init__(self, message: str):
        super().__init__(message)
        self.user_message = message


_lock = threading.Lock()
_state: Optional[dict] = None
_root_override: Optional[str] = None          # tests only (``use_root``)


# --- paths --------------------------------------------------------------------------------------------------------

def real_data_dir() -> str:
    """HedgeFund/backend/data (the user's real data; tests may point it elsewhere with ``use_root``)."""
    return _root_override or os.path.join(BACKEND_DIR, "data")


def marker_path(root: Optional[str] = None) -> str:
    return os.path.join(root or real_data_dir(), MARKER_NAME)


def sandbox_dir(root: Optional[str] = None) -> str:
    return os.path.join(root or real_data_dir(), SANDBOX_DIRNAME)


def read_marker(root: Optional[str] = None) -> Optional[dict]:
    """The marker's JSON, None without a marker. Unreadable / not a sandbox marker -> ``SandboxError``."""
    path = marker_path(root)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as ex:
        raise SandboxError(f"The sandbox marker {path} is unreadable ({type(ex).__name__}) — the backend does not "
                           "start on it. Run tools/sandbox.py off (renames it) or fix it.") from None
    if not isinstance(data, dict) or data.get("mode") != MODE_SANDBOX or not isinstance(data.get("files"), dict):
        raise SandboxError(f"The sandbox marker {path} is not a sandbox marker written by tools/sandbox.py on — the "
                           "backend does not start on it. Run tools/sandbox.py off (renames it).")
    return data


def _detect(root: str) -> dict:
    marker = read_marker(root)
    if marker is None:
        return {"mode": MODE_NORMAL, "root": root, "dir": root, "marker": None}
    sdir = sandbox_dir(root)
    missing = [name for name in marker["files"] if not os.path.isfile(os.path.join(sdir, name))]
    if missing:
        raise SandboxError(f"Sandbox mode is on (marker {marker_path(root)}) but the sandbox copy is missing: "
                           f"{', '.join(missing)} in {sdir}. The backend does not start (it never falls back to the "
                           "real databases). Run tools/sandbox.py off, then tools/sandbox.py on again.")
    return {"mode": MODE_SANDBOX, "root": root, "dir": sdir, "marker": marker}


def state() -> dict:
    """{mode, root, dir, marker} — decided once per process."""
    global _state
    with _lock:
        if _state is None:
            _state = _detect(real_data_dir())
        return _state


def mode() -> str:
    return state()["mode"]


def is_sandbox() -> bool:
    return state()["mode"] == MODE_SANDBOX


def data_dir() -> str:
    """Where the databases live in this process: data/sandbox in sandbox mode, else data/."""
    return state()["dir"]


def db_file(name: str) -> str:
    return os.path.join(data_dir(), name)


def mode_fields() -> dict:
    """The keys the main state payloads carry (/health, /version, /fund/state, /v2/portfolio/state, …)."""
    st = state()
    if st["mode"] != MODE_SANDBOX:
        return {"mode": MODE_NORMAL}
    return {"mode": MODE_SANDBOX,
            "sandbox": {"since": (st["marker"] or {}).get("created_at"), "banner": BANNER,
                        "brokers": BLOCKED_TEXT, "data_dir": "data/" + SANDBOX_DIRNAME, "prices_note": PRICES_NOTE}}


def stamp(payload: dict) -> dict:
    """A copy of ``payload`` with ``mode_fields()`` (the route wrappers of the main state payloads)."""
    return {**payload, **mode_fields()}


def require_sandbox(what: str) -> None:
    """Refuses ``what`` (seeding, force-decay) unless this process runs in sandbox mode."""
    if not is_sandbox():
        raise SandboxError(f"{what} runs only in sandbox mode (marker data/{MARKER_NAME}, tools/sandbox.py on) — "
                           "refused in normal mode.")
    assert is_sandbox(), "sandbox-only code reached in normal mode"     # second line of defence (python without -O)


# --- process start ------------------------------------------------------------------------------------------------

def startup(log: Optional[logging.Logger] = None) -> dict:
    """main.py, before any database is opened: decides the mode. Sandbox -> a loud log line and every later log
    line of this process carries "[SANDBOX]". A broken marker raises ``SandboxError`` (the backend does not start)."""
    log = log or logger
    st = state()
    if st["mode"] == MODE_SANDBOX:
        fmt = logging.Formatter("%(asctime)s %(levelname)s [SANDBOX] %(name)s: %(message)s")
        for h in logging.getLogger().handlers:
            h.setFormatter(fmt)
        line = "#" * 100
        log.warning(line)
        log.warning("SANDBOX MODE — marker %s (created %s). ALL databases are the copies in %s. Broker accounts are "
                    "blocked (no Alpaca trading / Binance requests), the old engine's autopilot and scheduled AI "
                    "cycles are off, AI costs are tagged 'sandbox'. Run tools/sandbox.py off to leave.",
                    marker_path(st["root"]), (st["marker"] or {}).get("created_at"), st["dir"])
        log.warning(line)
    else:
        log.info("Profile: normal (no sandbox marker) — databases in %s", st["dir"])
    return st


# --- helpers for tools/sandbox.py and tests -----------------------------------------------------------------------

def sha256_file(path: str) -> Optional[str]:
    """sha256 hex of a file, None when it does not exist."""
    if not os.path.isfile(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def use_root(root: Optional[str]) -> None:
    """Tests: point the real data dir at ``root`` (None = the real HedgeFund/backend/data) and forget the mode."""
    global _root_override, _state
    with _lock:
        _root_override = root
        _state = None


def reset() -> None:
    """Forget the decided mode (tests; tools/sandbox.py after writing the marker)."""
    global _state
    with _lock:
        _state = None
