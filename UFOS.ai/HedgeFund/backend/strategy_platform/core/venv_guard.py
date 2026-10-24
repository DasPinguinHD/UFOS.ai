"""Backend-venv guard for the command-line tools in ``backend/tools/`` (stage 10, S10-H).

Every tool must run with the backend's own interpreter, ``HedgeFund\\backend\\.venv\\Scripts\\python.exe``. The system
Python lacks the backend's packages, and the failures are confusing (e.g. tzdata missing → "No time zone found with
key America/New_York"). A tool calls ``require(<file name>, BACKEND_DIR, <exit code>)`` at the top of its
``if __name__ == "__main__"`` path, BEFORE importing anything beyond the standard library. Importing a tool as a module
(the tests) never triggers the guard.

Standard library only. Part of UFOS.ai, a proof-of-concept portfolio project.
"""
from __future__ import annotations

import os
import sys
from typing import Optional


def hint(tool: str) -> str:
    return f"run with .venv\\Scripts\\python.exe tools\\{tool} …  (from HedgeFund\\backend)"


def problem(tool: str, backend_dir: str, prefix: Optional[str] = None) -> Optional[str]:
    """None when this interpreter is the backend venv (sys.prefix == <backend_dir>/.venv), else the message."""
    want = os.path.normcase(os.path.realpath(os.path.join(backend_dir, ".venv")))
    have = os.path.normcase(os.path.realpath(prefix if prefix is not None else sys.prefix))
    if have == want:
        return None
    return f"This is not the backend's Python ({have}) — {hint(tool)}"


def require(tool: str, backend_dir: str, exit_code: int) -> None:
    """Stops the process with ``exit_code`` (and the message on stderr) outside the backend venv."""
    msg = problem(tool, backend_dir)
    if msg:
        print(f"REFUSED: {msg}", file=sys.stderr)
        sys.exit(exit_code)
