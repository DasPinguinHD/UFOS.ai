"""Sandbox profile tool (strategy_platform/SPEC.md "Sandbox profile", user decision 2026-10-23).

Run from HedgeFund\\backend with the backend's venv (Windows PowerShell):

    .venv\\Scripts\\python.exe tools\\sandbox.py on                     # copy the databases + marker, then seed the fixture
    .venv\\Scripts\\python.exe tools\\sandbox.py status
    .venv\\Scripts\\python.exe tools\\sandbox.py seed-incubation-test   # sandbox only (re-seed; ``on`` seeds already)
    .venv\\Scripts\\python.exe tools\\sandbox.py force-decay "<strategy>"   # sandbox only
    .venv\\Scripts\\python.exe tools\\sandbox.py off                    # marker -> *.stale; compares the real DB hashes

``on`` and ``off`` refuse while the backend answers GET /health (close UFOS.ai first). Nothing is ever deleted:
replaced sandbox copies and the marker are renamed to ``*.stale``. Never prints a secret or .env value.
Refuses to run outside the backend venv (sys.prefix must be HedgeFund/backend/.venv — the system Python lacks e.g.
tzdata). ``on`` seeds the incubation fixture right after the copy (exit 4 if that fails — the sandbox stays ON).

Part of UFOS.ai, a proof-of-concept portfolio project; PAPER / DEMO TRADING ONLY, not investment advice.
"""
from __future__ import annotations

import errno
import json
import os
import pathlib
import shutil
import socket
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Optional

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import sandbox_profile as sp  # noqa: E402
from sandbox_profile import SandboxError  # noqa: E402

EXIT_OK, EXIT_ERROR, EXIT_CHANGED, EXIT_SEED_FAILED = 0, 2, 3, 4
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"     # sha256 of b"" 
VENV_HINT = r"run with .venv\Scripts\python.exe tools\sandbox.py …  (from HedgeFund\backend)"


def venv_problem(prefix: Optional[str] = None) -> Optional[str]:
    """None when this interpreter is the backend's venv (sys.prefix == HedgeFund/backend/.venv), else the message
    (the shared S10-H guard, ``strategy_platform/core/venv_guard.py``)."""
    from strategy_platform.core import venv_guard
    return venv_guard.problem("sandbox.py", BACKEND_DIR, prefix)
CLOSE_FIRST = "close UFOS.ai first (and wait until its HedgeFund backend has stopped), then run this again"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp() -> str:
    return _now().strftime("%Y%m%dT%H%M%SZ")


# --- is the backend running? (the existing health check: GET /health on 127.0.0.1:HEALTH_PORT) ---------------------

def health_port() -> int:
    from config import HEALTH_PORT        # fixed 8867 (stage 10: no .env)
    return int(HEALTH_PORT)


def backend_running(port: Optional[int] = None, timeout: float = 8.0) -> bool:
    # 8 s, not 2 s: on Windows a connect to a CLOSED loopback port is not refused at once — the TCP stack retries
    # the SYN for ~2 s before "connection refused" (live test 2026-10-23: every `on` timed out with nothing running).
    """True when something answers GET /health on 127.0.0.1:<port>; False on "connection refused". Anything else
    (timeout, odd network error) -> SandboxError: the state is unknown, so the switch is refused."""
    port = health_port() if port is None else port
    url = f"http://127.0.0.1:{port}/health"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))      # loopback: never via a proxy
    try:
        with opener.open(url, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True                       # it answered (any status): a process owns the port
    except urllib.error.URLError as ex:
        reason = ex.reason
        if isinstance(reason, ConnectionRefusedError) or getattr(reason, "errno", None) in (errno.ECONNREFUSED, 10061):
            return False
        raise SandboxError(f"Could not tell whether the backend is running ({type(reason).__name__}: {reason}) — "
                           f"{CLOSE_FIRST}.") from None
    except ConnectionRefusedError:
        return False
    except (socket.timeout, TimeoutError):
        raise SandboxError(f"The backend port {port} did not answer within {timeout:g} s — {CLOSE_FIRST}.") from None


def _refuse_if_running(running_fn: Callable[[], bool]) -> None:
    if running_fn():
        raise SandboxError(f"The HedgeFund backend is running (it answers /health) — {CLOSE_FIRST}.")


# --- copies -------------------------------------------------------------------------------------------------------

def _set_aside(path: str, stamp: str) -> list:
    """Renames ``path`` (and its -wal / -shm) to ``<name>.<stamp>.stale`` — never deletes. -> renamed paths."""
    out = []
    for p in (path, path + "-wal", path + "-shm", path + ".partial"):
        if os.path.exists(p):
            target = f"{p}.{stamp}.stale"
            os.replace(p, target)
            out.append(target)
    return out


def _backup(src: str, dst: str) -> None:
    """A consistent copy of the SQLite database ``src`` at ``dst`` (SQLite online backup API; the source is opened
    read-only). The copy is checked (PRAGMA quick_check) before it takes the final name."""
    tmp = dst + ".partial"
    s = sqlite3.connect(pathlib.Path(src).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    try:
        d = sqlite3.connect(tmp)
        try:
            s.backup(d)
        finally:
            d.close()
    finally:
        s.close()
    c = sqlite3.connect(tmp)
    try:
        check = c.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        c.close()
    if check != "ok":
        raise SandboxError(f"The sandbox copy of {os.path.basename(src)} failed its integrity check ({check}) — "
                           "sandbox mode was NOT switched on.")
    os.replace(tmp, dst)


def _real_hashes(root: str, name: str) -> dict:
    src = os.path.join(root, name)
    wal = src + "-wal"
    # an EMPTY -wal holds no data (SQLite creates / removes it with the connections) — the same as no -wal file
    wal_sha = sp.sha256_file(wal) if os.path.isfile(wal) and os.path.getsize(wal) > 0 else None
    return {"sha256": sp.sha256_file(src), "wal_sha256": wal_sha,
            "bytes": os.path.getsize(src) if os.path.isfile(src) else None}


def cmd_on(root: Optional[str] = None, running_fn: Optional[Callable[[], bool]] = None,
           seed_fn: Optional[Callable[[], dict]] = None) -> dict:
    """Copies, writes the marker, then seeds the incubation fixture inside the sandbox (user 2026-10-24; same code
    path as ``seed-incubation-test``). ``marker["seed"]`` = {ok, result | error} — a failed seeding leaves the
    sandbox ON and says so (never skipped silently)."""
    root = root or sp.real_data_dir()
    running_fn = running_fn or backend_running
    if os.path.exists(sp.marker_path(root)):
        raise SandboxError(f"Sandbox mode is already on (marker {sp.marker_path(root)}). Use status, or off first.")
    _refuse_if_running(running_fn)
    names = [n for n in sp.DB_FILES if os.path.isfile(os.path.join(root, n))]
    if "hedgefund.db" not in names:
        raise SandboxError(f"{os.path.join(root, 'hedgefund.db')} does not exist — nothing to copy.")
    sdir = sp.sandbox_dir(root)
    os.makedirs(sdir, exist_ok=True)
    stamp = _stamp()
    files, set_aside = {}, []
    for name in names:
        src, dst = os.path.join(root, name), os.path.join(sdir, name)
        before = _real_hashes(root, name)
        set_aside += _set_aside(dst, stamp)
        _backup(src, dst)
        after = _real_hashes(root, name)
        if after["sha256"] != before["sha256"]:
            raise SandboxError(f"The real {name} changed while it was copied (is the backend running?) — sandbox mode "
                               f"was NOT switched on; {CLOSE_FIRST}.")
        files[name] = {"source_sha256": after["sha256"], "source_wal_sha256": after["wal_sha256"],
                       "source_bytes": after["bytes"], "copy_sha256": sp.sha256_file(dst)}
    papers_src = os.path.join(root, sp.PAPERS_DIRNAME)
    papers = None
    if os.path.isdir(papers_src):
        papers_dst = os.path.join(sdir, sp.PAPERS_DIRNAME)
        if os.path.exists(papers_dst):
            os.replace(papers_dst, f"{papers_dst}.{stamp}.stale")
            set_aside.append(f"{papers_dst}.{stamp}.stale")
        shutil.copytree(papers_src, papers_dst)
        papers = len(os.listdir(papers_dst))
    marker = {"mode": sp.MODE_SANDBOX, "created_at": _now().isoformat(timespec="seconds"),
              "created_by": "tools/sandbox.py on", "sandbox_dir": "data/" + sp.SANDBOX_DIRNAME, "files": files,
              "papers_copied": papers, "set_aside": [os.path.basename(p) for p in set_aside],
              "note": "Click-test sandbox: the backend uses the copies in data/sandbox while this marker exists. "
                      "The source hashes are compared by tools/sandbox.py off."}
    tmp = sp.marker_path(root) + ".partial"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(marker, f, indent=2, ensure_ascii=False)
    os.replace(tmp, sp.marker_path(root))
    marker["seed"] = _seed_after_on(seed_fn or cmd_seed)
    return marker


def _seed_after_on(seed_fn: Callable[[], dict]) -> dict:
    try:
        return {"ok": True, "result": seed_fn()}
    except Exception as ex:      # noqa: BLE001 — reported by ``on`` (the sandbox stays ON)
        return {"ok": False, "error": getattr(ex, "user_message", None) or getattr(ex, "message", None)
                or f"{type(ex).__name__}: {ex}"}


def cmd_off(root: Optional[str] = None, running_fn: Optional[Callable[[], bool]] = None) -> dict:
    root = root or sp.real_data_dir()
    running_fn = running_fn or backend_running
    path = sp.marker_path(root)
    if not os.path.exists(path):
        raise SandboxError("Sandbox mode is not on (no marker) — nothing to switch off.")
    _refuse_if_running(running_fn)
    try:
        marker = sp.read_marker(root)
        problem = None
    except SandboxError as ex:
        marker, problem = None, ex.user_message
    comparison, identical = {}, None
    if marker is not None:
        identical = True
        for name, info in sorted(marker["files"].items()):
            now = _real_hashes(root, name)
            wal_on = info.get("source_wal_sha256")
            wal_on = None if wal_on == EMPTY_SHA256 else wal_on    # markers written before 2026-10-24 hashed empty -wal files
            same = now["sha256"] == info.get("source_sha256") and now["wal_sha256"] == wal_on
            comparison[name] = {"identical": same, "sha256_at_on": info.get("source_sha256"),
                                "sha256_now": now["sha256"], "wal_at_on": info.get("source_wal_sha256"),
                                "wal_now": now["wal_sha256"]}
            wal_file = os.path.join(root, name + "-wal")
            if info.get("source_wal_sha256") == EMPTY_SHA256 and not os.path.exists(wal_file):
                comparison[name]["note"] = "wal: empty → absent (no data change)"
            elif info.get("source_wal_sha256") is None and os.path.isfile(wal_file) and not os.path.getsize(wal_file):
                comparison[name]["note"] = "wal: absent → empty (no data change)"
            identical = identical and same
    stale = f"{path}.{_stamp()}.stale"
    os.replace(path, stale)                 # never deleted; the sandbox folder stays as it is
    out = {"marker_renamed_to": os.path.basename(stale), "sandbox_dir_kept": sp.sandbox_dir(root),
           "identical": identical, "comparison": comparison, "marker_problem": problem,
           "off_at": _now().isoformat(timespec="seconds")}
    if marker is not None:
        marker["off"] = out
        with open(stale, "w", encoding="utf-8") as f:       # the renamed marker records the result
            json.dump(marker, f, indent=2, ensure_ascii=False)
    return out


def cmd_status(root: Optional[str] = None, running_fn: Optional[Callable[[], bool]] = None) -> dict:
    root = root or sp.real_data_dir()
    running_fn = running_fn or backend_running
    try:
        running = running_fn()
    except SandboxError as ex:
        running = f"unknown ({ex.user_message})"
    out = {"mode": "normal", "marker": None, "backend_running": running, "sandbox_dir": sp.sandbox_dir(root),
           "sandbox_files": sorted(os.listdir(sp.sandbox_dir(root))) if os.path.isdir(sp.sandbox_dir(root)) else []}
    try:
        marker = sp.read_marker(root)
    except SandboxError as ex:
        out.update(mode="broken marker", marker_problem=ex.user_message)
        return out
    if marker is None:
        return out
    out["mode"] = "sandbox"
    out["marker"] = {"created_at": marker.get("created_at"), "files": sorted(marker["files"])}
    out["real_db_unchanged_so_far"] = {
        name: _real_hashes(root, name)["sha256"] == info.get("source_sha256")
        for name, info in sorted(marker["files"].items())}
    return out


# --- sandbox-only commands (run against the sandbox copies) --------------------------------------------------------

def _sandbox_process(what: str) -> None:
    """This process works on the sandbox databases — refused unless the marker is active."""
    sp.reset()
    sp.require_sandbox(what)
    from strategy_platform.storage import db
    db.init()                              # default path = data/sandbox/hedgefund.db (sandbox_profile.data_dir)
    if os.path.dirname(os.path.abspath(db.db_path())) != os.path.abspath(sp.sandbox_dir(sp.state()["root"])):
        raise SandboxError(f"hedgefund.db opened at {db.db_path()}, not in the sandbox folder — refused.")


def cmd_seed() -> dict:
    _sandbox_process("seed-incubation-test")
    import sandbox_fixtures
    return sandbox_fixtures.seed_incubation_test()


def cmd_force_decay(strategy: str) -> dict:
    _sandbox_process("force-decay")
    import sandbox_fixtures
    return sandbox_fixtures.force_decay(strategy)


# --- CLI ----------------------------------------------------------------------------------------------------------

USAGE = ('usage: tools\\sandbox.py on | off | status | seed-incubation-test | force-decay "<strategy name or id>"')


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(USAGE)
        return EXIT_ERROR
    cmd = args[0]
    try:
        if cmd == "on" and len(args) == 1:
            m = cmd_on()
            _print({"sandbox": "ON", "marker": sp.marker_path(), "created_at": m["created_at"],
                    "copied": sorted(m["files"]), "papers_copied": m["papers_copied"],
                    "set_aside": m["set_aside"]})
            print("Sandbox mode is ON. Start UFOS.ai: the HedgeFund backend now uses data\\sandbox (payloads carry "
                  '"mode": "sandbox"). Leave with: tools\\sandbox.py off (after closing UFOS.ai).')
            seed = m["seed"]
            if seed["ok"]:
                r = seed["result"]
                print(f"Incubation fixture: {r.get('name')} ({r.get('strategy_id')}) — {r.get('state')}"
                      + (" (seeded now)" if r.get("created") else " (already there)") + ".")
                return EXIT_OK
            print(f"Sandbox mode is ON, but seeding the incubation fixture FAILED: {seed['error']}", file=sys.stderr)
            print("Fix the cause, then run: .venv\\Scripts\\python.exe tools\\sandbox.py seed-incubation-test",
                  file=sys.stderr)
            return EXIT_SEED_FAILED
        if cmd == "off" and len(args) == 1:
            out = cmd_off()
            _print(out)
            if out["identical"] is True:
                print("Sandbox mode is OFF. The real databases are IDENTICAL to their state at 'on' — the real "
                      "backend did not run in between. data\\sandbox was kept.")
                return EXIT_OK
            if out["identical"] is False:
                print("Sandbox mode is OFF. WARNING: a real database CHANGED since 'on' (see comparison) — the real "
                      "backend (or another program) wrote it in between. data\\sandbox was kept.")
                return EXIT_CHANGED
            print("Sandbox mode is OFF (the marker was unreadable — no hash comparison possible). data\\sandbox was kept.")
            return EXIT_CHANGED
        if cmd == "status" and len(args) == 1:
            _print(cmd_status())
            return EXIT_OK
        if cmd == "seed-incubation-test" and len(args) == 1:
            _print(cmd_seed())
            return EXIT_OK
        if cmd == "force-decay" and len(args) == 2:
            _print(cmd_force_decay(args[1]))
            return EXIT_OK
    except SandboxError as ex:
        print(f"REFUSED: {ex.user_message}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as ex:      # noqa: BLE001 — e.g. a registry refusal: its message, no traceback noise
        print(f"FAILED: {getattr(ex, 'user_message', None) or getattr(ex, 'message', None) or ex}", file=sys.stderr)
        return EXIT_ERROR
    print(USAGE)
    return EXIT_ERROR


if __name__ == "__main__":
    _problem = venv_problem()
    if _problem:
        print(f"REFUSED: {_problem}", file=sys.stderr)
        sys.exit(EXIT_ERROR)
    sys.exit(main())
