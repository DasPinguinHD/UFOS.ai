#!/usr/bin/env python3
"""Pinned, hash-checked Python dependencies for this backend (stage 10, user decision 2026-10-24).

Writes ``requirements.lock`` next to ``requirements.txt`` (and ``requirements-broker.lock`` next to an optional
``requirements-broker.txt``): every package of the full dependency closure as ``name==version --hash=sha256:...``,
for THIS machine's Python / platform. The installers (run_*.ps1, the app's environment preparation) then run
``python -m pip install --require-hashes -r requirements.lock`` - pip refuses any file whose hash differs.

Run it with the backend's OWN interpreter (the lock is made for that Python and platform), from the backend folder:

    .venv\\Scripts\\python.exe make_lockfile.py            # first time: pin exactly what is installed and tested
    .venv\\Scripts\\python.exe make_lockfile.py --update   # deliberate update: newest versions requirements.txt allows

Default mode ("installed") constrains the resolution to the versions currently installed in the venv
(``pip freeze``); ``--update`` resolves fresh (this is what "Update Python packages" runs before installing).

Optional broker set (HedgeFund: ccxt, which pins many shared packages such as certifi EXACTLY): main and broker
share ONE venv, so both sets are resolved TOGETHER (``-r requirements.txt -r requirements-broker.txt``) and each
lock is that joint resolution's closure of its own requirements file - every shared package has the same version
in both locks. If the joint resolution fails (e.g. ccxt pins a version requirements.txt forbids), the main lock
is written ALONE (the main environment never fails because of the broker set) and requirements-broker.lock is
written as a "not installable" lock that names the conflict (no packages; the installer reports it on the
binance-demo broker) - exit code 5, never silent. In --update mode a main package held below its newest allowed
version by the broker set is listed in the main lock header (``# held-by-broker: ...``).

``--check`` only reports whether each lock exists and still matches its requirements file (exit 1 if not).

Needs network access to PyPI (pip resolves and reads the hashes; nothing is installed: ``pip install --dry-run
--ignore-installed --report``, pip >= 23). Exit codes: 0 ok - 1 --check found a missing / stale / not installable
lock - 2 usage (no requirements.txt, not the backend venv) - 3 pip failed - 4 a package without a sha256 hash -
5 requirements.lock written, but the broker set cannot be resolved together with it (requirements-broker.lock
names the reason).

The same file is used by every backend (HedgeFund/backend/tools, Global Monitoring/backend, LiveStreamAgent/backend;
a test keeps the copies byte-identical). Stdlib only. Part of UFOS.ai, a proof-of-concept portfolio project.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple, Union

LOCK_SUFFIX = ".lock"
MAIN_REQ, MAIN_LOCK = "requirements.txt", "requirements.lock"
BROKER_REQ, BROKER_LOCK = "requirements-broker.txt", "requirements-broker.lock"
SETS = ((MAIN_REQ, MAIN_LOCK), (BROKER_REQ, BROKER_LOCK))
EXIT_BROKER_NOT_INSTALLABLE = 5
HEADER_SOURCE_RE = re.compile(r"^# source: (\S+) sha256=([0-9a-f]{64})$", re.M)
HEADER_WITH_RE = re.compile(r"^# with: (\S+) sha256=([0-9a-f]{64})$", re.M)
HEADER_CONFLICT_RE = re.compile(r"^# conflict: (.+)$", re.M)
LINE_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+) --hash=sha256:([0-9a-f]{64})$")

Entries = List[Tuple[str, str, str]]


class PipFailed(RuntimeError):
    """pip exited non-zero; ``reason`` is the one-line cause (the "conflict is caused by" lines when present)."""

    def __init__(self, code: int, text: str):
        self.code, self.reason = code, pip_reason(text)
        super().__init__(f"pip exited with code {code}: {self.reason}")


def one_line(text: str, limit: int = 400) -> str:
    text = re.sub(r"[^\x20-\x7E]+", " ", text).strip()
    text = re.sub(r"\s{2,}", " ", text)
    return text if len(text) <= limit else text[:limit - 3] + "..."


def pip_reason(text: str) -> str:
    """The cause of a failed pip run in one line: the lines under "The conflict is caused by:" (ResolutionImpossible),
    else the last ERROR line, else the last line."""
    lines = (text or "").splitlines()
    for i, line in enumerate(lines):
        if line.strip().lower().startswith("the conflict is caused by"):
            parts = []
            for nxt in lines[i + 1:]:
                if not nxt.strip():
                    break
                parts.append(nxt.strip())
            if parts:
                return one_line("conflicting dependencies: " + "; ".join(parts))
    errors = [x.strip() for x in lines if x.strip().startswith("ERROR")]
    if errors:
        return one_line(errors[-1])
    rest = [x.strip() for x in lines if x.strip()]
    return one_line(rest[-1]) if rest else "no output"


def backend_dir(script_path: str = __file__) -> str:
    """The folder holding requirements.txt: this script's folder, or its parent (HedgeFund keeps it in tools/)."""
    here = os.path.dirname(os.path.abspath(script_path))
    for d in (here, os.path.dirname(here)):
        if os.path.isfile(os.path.join(d, MAIN_REQ)):
            return d
    raise SystemExit(2)


def sha256_file(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def venv_problem(base: str, prefix: Optional[str] = None) -> Optional[str]:
    want = os.path.normcase(os.path.realpath(os.path.join(base, ".venv")))
    have = os.path.normcase(os.path.realpath(prefix if prefix is not None else sys.prefix))
    if have == want:
        return None
    return (f"This is not the backend's Python ({have}) - run it with .venv\\Scripts\\python.exe from the backend "
            f"folder (the lock is made for that interpreter and platform).")


def entries_from_report(report: dict) -> Entries:
    """[(name, version, sha256)] of a ``pip install --report`` (sorted by name); ValueError for an entry without a
    sha256 (never written unhashed)."""
    out, missing = [], []
    for item in report.get("install") or []:
        meta = item.get("metadata") or {}
        name, version = meta.get("name"), meta.get("version")
        info = (item.get("download_info") or {}).get("archive_info") or {}
        digest = (info.get("hashes") or {}).get("sha256")
        if not digest and str(info.get("hash", "")).startswith("sha256="):
            digest = info["hash"][len("sha256="):]
        if not name or not version:
            raise ValueError("report entry without name/version")
        if not digest or not re.fullmatch(r"[0-9a-f]{64}", digest):
            missing.append(f"{name}=={version}")
            continue
        out.append((canonical(name), version, digest))
    if missing:
        raise ValueError("no sha256 hash for: " + ", ".join(missing))
    return sorted(out)


def pins_of(entries: Entries) -> List[str]:
    return [f"{n}=={v}" for n, v, _h in entries]


def render_lock(entries, source_name: str, source_sha: str, mode: str, now: Optional[str] = None,
                extra: Sequence[str] = ()) -> str:
    now = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    head = [
        "# Generated by make_lockfile.py - DO NOT EDIT (regenerate: make_lockfile.py [--update]).",
        f"# source: {source_name} sha256={source_sha}",
        f"# mode: {mode} | python {platform.python_version()} ({sys.platform}, {platform.machine()}) | {now}",
        "# install: python -m pip install --require-hashes -r " + source_name.replace(".txt", LOCK_SUFFIX),
    ]
    return "\n".join(head + list(extra) + [f"{n}=={v} --hash=sha256:{h}" for n, v, h in entries]) + "\n"


def parse_lock(text: str) -> Tuple[Optional[Tuple[str, str]], Dict[str, str]]:
    """((source name, source sha256) or None, {name: version})."""
    m = HEADER_SOURCE_RE.search(text)
    pins = {}
    for line in text.splitlines():
        lm = LINE_RE.match(line.strip())
        if lm:
            pins[lm.group(1)] = lm.group(2)
    return ((m.group(1), m.group(2)) if m else None), pins


def lock_state(base: str, req_name: str, lock_name: str) -> str:
    """"ok" | "missing" | "stale (...)" | "not installable (...)" | "no-requirements". The broker lock must also
    have been made together with the CURRENT main lock (its "# with:" line)."""
    req, lock = os.path.join(base, req_name), os.path.join(base, lock_name)
    if not os.path.isfile(req):
        return "no-requirements"
    if not os.path.isfile(lock):
        return "missing"
    with open(lock, encoding="utf-8") as fh:
        text = fh.read()
    src, pins = parse_lock(text)
    if src is None or src[0] != req_name or src[1] != sha256_file(req):
        return f"stale ({req_name} changed since the lock was made)"
    if lock_name != MAIN_LOCK:
        w = HEADER_WITH_RE.search(text)
        if w is None:
            return "stale (no '# with:' line - made before the joint resolution; regenerate)"
        main = os.path.join(base, w.group(1))
        if not os.path.isfile(main) or sha256_file(main) != w.group(2):
            return f"stale (made for another {w.group(1)})"
        c = HEADER_CONFLICT_RE.search(text)
        if c:
            return f"not installable ({c.group(1).strip()})"
    if not pins:
        return "stale (no pinned packages)"
    return "ok"


def _pip(args: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "pip", *args, "--disable-pip-version-check", "--no-input"],
                          capture_output=True, text=True)


def resolve(req_paths: Union[str, Sequence[str]], constraints: Optional[List[str]], pip=_pip) -> Entries:
    """The closure of one or more requirements files (resolved together) under ``constraints``. PipFailed /
    ValueError (no sha256)."""
    if isinstance(req_paths, str):
        req_paths = [req_paths]
    with tempfile.TemporaryDirectory(prefix="ufos-lock-") as tmp:
        report = os.path.join(tmp, "report.json")
        # no --quiet: pip prints "The conflict is caused by:" (named in the broker lock) only at normal verbosity
        args = ["install", "--dry-run", "--ignore-installed", "--report", report]
        for path in req_paths:
            args += ["-r", path]
        if constraints:
            cpath = os.path.join(tmp, "constraints.txt")
            with open(cpath, "w", encoding="utf-8") as fh:
                fh.write("\n".join(constraints) + "\n")
            args += ["-c", cpath]
        p = pip(args)
        if p.returncode != 0:
            raise PipFailed(p.returncode, "\n".join(x for x in (p.stderr, p.stdout) if x))
        with open(report, encoding="utf-8") as fh:
            return entries_from_report(json.load(fh))


def installed_pins(pip=_pip) -> List[str]:
    p = pip(["freeze", "--exclude-editable"])
    if p.returncode != 0:
        raise PipFailed(p.returncode, "pip freeze failed: " + (p.stderr or p.stdout or ""))
    return [x.strip() for x in p.stdout.splitlines() if re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*==", x.strip())]


def write_atomic(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _subset_problem(part: Entries, joint: Entries, what: str) -> Optional[str]:
    j = {n: (v, h) for n, v, h in joint}
    bad = [f"{n}=={v}" for n, v, h in part if j.get(n) != (v, h)]
    return f"{what} differs from the joint resolution: {', '.join(bad)}" if bad else None


def _broker_hint(update: bool) -> str:
    if update:
        return ("the broker packages pin a version requirements.txt does not allow - change requirements-broker.txt "
                "(another ccxt version) or requirements.txt, then run make_lockfile.py --update again")
    return ("run make_lockfile.py --update to resolve both sets together (shared packages such as certifi then "
            "move to the versions the broker packages pin)")


def make(base: str, update: bool, out=print, pip=_pip) -> int:
    main_req, broker_req = os.path.join(base, MAIN_REQ), os.path.join(base, BROKER_REQ)
    if not os.path.isfile(main_req):
        out("REFUSED: no requirements.txt in " + base)
        return 2
    has_broker = os.path.isfile(broker_req)
    mode = "update" if update else "installed"
    try:
        constraints = None if update else installed_pins(pip)
        alone = resolve(main_req, constraints, pip)                 # the main set on its own (never blocked by ccxt)
    except ValueError as ex:
        out(f"{MAIN_REQ}: {ex} - nothing written")
        return 4
    except PipFailed as ex:
        out(f"{MAIN_REQ}: {ex} - nothing written (network to PyPI needed)")
        return 3
    main_entries, broker_entries, broker_problem, extra = alone, None, None, []
    if has_broker:
        try:
            joint = resolve([main_req, broker_req], constraints, pip)
            jpins = pins_of(joint)
            main_entries = resolve(main_req, jpins, pip)
            broker_entries = resolve(broker_req, jpins, pip)
            broker_problem = (_subset_problem(main_entries, joint, MAIN_LOCK)
                              or _subset_problem(broker_entries, joint, BROKER_LOCK))
        except (PipFailed, ValueError) as ex:
            broker_problem = getattr(ex, "reason", None) or one_line(str(ex))
        if broker_problem:
            main_entries, broker_entries = alone, None
            extra = [f"# resolved: alone - {BROKER_REQ} cannot be resolved together with it (see {BROKER_LOCK})"]
        else:
            extra = [f"# resolved: together with {BROKER_REQ} (one venv: shared packages agree)"]
            newest = {n: v for n, v, _h in alone}
            held = [f"{n} {v} (alone: {newest[n]})" for n, v, _h in main_entries if n in newest and newest[n] != v]
            if update and held:
                extra.append("# held-by-broker: " + ", ".join(held))
    main_text = render_lock(main_entries, MAIN_REQ, sha256_file(main_req), mode, extra=extra)
    write_atomic(os.path.join(base, MAIN_LOCK), main_text)
    out(f"{MAIN_LOCK}: {len(main_entries)} packages pinned with sha256 ({mode}) - lock sha256 "
        f"{sha256_text(main_text)[:16]}" + (f" - {extra[0][2:]}" if extra else ""))
    if len(extra) > 1:
        out(extra[1][2:])
    if not has_broker:
        return 0
    with_line = f"# with: {MAIN_LOCK} sha256={sha256_text(main_text)}"
    if broker_problem is None:
        text = render_lock(broker_entries, BROKER_REQ, sha256_file(broker_req), mode, extra=[with_line])
        write_atomic(os.path.join(base, BROKER_LOCK), text)
        out(f"{BROKER_LOCK}: {len(broker_entries)} packages pinned with sha256 ({mode}, together with "
            f"{MAIN_LOCK}) - lock sha256 {sha256_text(text)[:16]}")
        return 0
    text = render_lock([], BROKER_REQ, sha256_file(broker_req), mode, extra=[
        with_line, f"# conflict: {broker_problem}",
        f"# NOT INSTALLABLE together with {MAIN_LOCK} - {_broker_hint(update)}."])
    write_atomic(os.path.join(base, BROKER_LOCK), text)
    out(f"{BROKER_LOCK}: NOT INSTALLABLE - {BROKER_REQ} cannot be resolved together with {MAIN_REQ}: "
        f"{broker_problem}. {MAIN_LOCK} was written alone; the broker packages stay unavailable - "
        f"{_broker_hint(update)}.")
    return EXIT_BROKER_NOT_INSTALLABLE


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--update", action="store_true", help="resolve the newest allowed versions (deliberate update)")
    ap.add_argument("--check", action="store_true", help="only report whether the locks exist and are current")
    ap.add_argument("--any-python", action="store_true", help=argparse.SUPPRESS)       # tests
    a = ap.parse_args(argv)
    base = backend_dir()
    if a.check:
        bad = 0
        for req_name, lock_name in SETS:
            st = lock_state(base, req_name, lock_name)
            if st != "no-requirements":
                print(f"{lock_name}: {st}")
                bad += st != "ok"
        return 1 if bad else 0
    problem = None if a.any_python else venv_problem(base)
    if problem:
        print("REFUSED: " + problem, file=sys.stderr)
        return 2
    return make(base, a.update)


if __name__ == "__main__":
    sys.exit(main())
