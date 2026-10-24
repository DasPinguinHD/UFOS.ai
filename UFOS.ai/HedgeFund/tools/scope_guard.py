"""Scope guard for the Strategy Platform rebuild of the HedgeFund module.

The rebuild may change ONLY files inside UFOS.ai/HedgeFund/ (plus the
HedgeFund sections of a few documentation files the user approved:
CLAUDE.md, .github/copilot-instructions.md, docs/todo.md, UFOS.ai/docs/todo.md,
docs/market-research-plan.md).
This tool takes a SHA-256 snapshot of every file outside the module and later
compares against it, so an accidental change or deletion elsewhere is caught.

Usage (from the repo root or anywhere):
    python scope_guard.py snapshot --label stage0
    python scope_guard.py check    --label stage0

Approved allow-lists (2026-10-17, task B "central AI budget window", contract
UFOS.ai/docs/ai-budgets-spec.md §2): for a task that the user explicitly allowed
to change a FEW named files outside the module, the approved list lives in a
versioned JSON file next to this tool, ``scope-allow-<label>.json``
({"label", "approved", "files": [...]}). Entries are posix paths relative to the
repo root: an exact path, or a path whose LAST segment contains ``*`` with at least
3 literal characters before the first ``*`` (``UFOS.ai/Shared/AiBudget*.cs``);
``*`` never crosses ``/``; no wildcards in directory parts, no ``?``/``[``, no ``..``.
The list is embedded into the manifest when the snapshot is taken:
    python scope_guard.py snapshot --label budget0 --allow-list HedgeFund/tools/scope-allow-budget0.json
    python scope_guard.py check    --label budget0          # uses the embedded (approved) copy
    python scope_guard.py amend    --label budget0 --allow-list HedgeFund/tools/scope-allow-budget0.json
                                   # re-embeds an extended list (additions only, recorded)
    python scope_guard.py close    --label budget0          # end of the task: freeze the exception
Older snapshots (stage0, mr0, ...) are checked "except the allow-listed files"
automatically: the approved copies embedded in the other snapshots of OUT_DIR are
applied (``--strict`` turns that off). After ``close`` (review M1) an allow-listed file
is skipped ONLY while its sha256 equals the one recorded by ``close`` (or it is still
absent) — any later change is reported like any other change and needs a new
approved exception. ``--allow-list FILE`` on check adds a list (patterns, for review);
``--allow-file PATH`` one exact path (ad-hoc, printed for review). Allow-listed
changes are listed in their own section and do not fail the check.

Snapshots go to HedgeFund/backend/data/scope-guard/ (git-ignored).
Exit code of `check`: 0 = nothing outside changed (doc exceptions are listed
separately), 1 = unexpected changes, 2 = usage/IO error. Stdlib only.

Part of UFOS.ai, a proof-of-concept portfolio project.
"""
import argparse
import fnmatch
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]          # UFOS.ai/HedgeFund
REPO_ROOT = MODULE_DIR.parents[1]                          # repo root (contains .git)
OUT_DIR = MODULE_DIR / "backend" / "data" / "scope-guard"

# Directories never compared (build output, IDE state, caches, runtime data).
SKIP_DIR_NAMES = {".git", ".vs", "bin", "obj", ".venv", "__pycache__", "node_modules", ".pytest_cache"}
# Runtime files that other modules legitimately rewrite while the app runs.
SKIP_SUFFIXES = (".db", ".db-wal", ".db-shm", ".log", ".jsonl", ".pyc")
SKIP_PATH_PARTS = ("/data/logs/", "/data/api-token")

# Files outside the module that the rebuild may touch, HedgeFund sections only.
DOC_EXCEPTIONS = {
    "CLAUDE.md",
    ".github/copilot-instructions.md",
    "docs/todo.md",
    "UFOS.ai/docs/todo.md",
    "docs/market-research-plan.md",   # Market Research plan (user, 2026-10-15)
    "UFOS.ai/docs/ai-budgets-spec.md",  # central AI budget window contract (user, 2026-10-17, task B)
}


_WILDCARD_CHARS = set("*?[]")


def _entry_problem(f: str):
    """Why an allow-list entry is not acceptable, or None (rules in the module docstring)."""
    if not f or f.startswith("/") or "\\" in f or ":" in f.split("/")[0]:
        return "must be a path relative to the repo root"
    parts = f.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return "must not contain empty, '.' or '..' segments"
    if any(c in _WILDCARD_CHARS - {"*"} for c in f):
        return "only '*' is allowed as a wildcard"
    if any("*" in p for p in parts[:-1]):
        return "no wildcards in directory parts"
    last = parts[-1]
    if "*" in last:
        if len(parts) < 2:
            return "a wildcard entry needs a directory"
        if len(last.split("*", 1)[0]) < 3:
            return "a wildcard needs at least 3 literal characters before the first '*'"
    return None


def _valid_entries(files, where: str, strict: bool = True) -> list:
    out = []
    for f in files:
        f = str(f).strip()
        problem = _entry_problem(f)
        if problem:
            if strict:
                raise ValueError(f"{where}: entry {f!r} {problem}")
            print(f"warning: {where}: entry {f!r} ignored ({problem})", file=sys.stderr)
            continue
        out.append(f)
    return out


def _load_allow_list(path) -> dict:
    """{"label", "approved", "files", "source", "sha256"} from an allow-list JSON file."""
    p = Path(path)
    if not p.is_absolute():
        p = REPO_ROOT / p if (REPO_ROOT / p).exists() else Path.cwd() / p
    raw = p.read_bytes()
    data = json.loads(raw.decode("utf-8"))
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, list) or not all(isinstance(f, str) and f.strip() for f in files):
        raise ValueError(f"{p}: expected {{\"files\": [\"path\", ...]}}")
    return {"label": data.get("label"), "approved": data.get("approved"),
            "files": _valid_entries(files, str(p)), "source": p.as_posix(),
            "sha256": hashlib.sha256(raw).hexdigest()}


def _match(rel: str, pattern: str) -> bool:
    """Exact path, or ``*`` within the LAST segment only (never across '/')."""
    if "*" not in pattern:
        return rel == pattern
    pd, _, pl = pattern.rpartition("/")
    rd, _, rl = rel.rpartition("/")
    return pd == rd and fnmatch.fnmatchcase(rl, pl)


def _allowed(rel: str, patterns) -> bool:
    return any(_match(rel, pat) for pat in patterns)


class _Rule:
    """One allow-list: patterns (open exception) or recorded hashes (closed exception)."""

    def __init__(self, name: str, patterns, closed=None):
        self.name, self.patterns, self.closed = name, list(patterns), closed

    def allows(self, rel: str, current_sha) -> bool:
        if self.closed is None:
            return _allowed(rel, self.patterns)
        if rel not in self.closed:
            return False
        return self.closed[rel] == current_sha       # None == None: still absent


def _rule_from_manifest(label: str, manifest: dict):
    allow = manifest.get("allow_list") if isinstance(manifest, dict) else None
    if not isinstance(allow, dict) or not isinstance(allow.get("files"), list):
        return None
    patterns = _valid_entries(allow["files"], f"manifest '{label}'", strict=False)
    closed = manifest.get("closed")
    files = closed.get("files") if isinstance(closed, dict) and isinstance(closed.get("files"), dict) else None
    return _Rule(label, patterns, files)


def _skipped(rel: str) -> bool:
    if rel.endswith(SKIP_SUFFIXES):
        return True
    probe = "/" + rel
    return any(part in probe for part in SKIP_PATH_PARTS)


def _iter_outside_files():
    module_rel = MODULE_DIR.relative_to(REPO_ROOT).as_posix() + "/"
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES]
        for name in files:
            path = Path(root) / name
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel.startswith(module_rel) or _skipped(rel):
                continue
            yield rel, path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _manifest_path(label: str) -> Path:
    return OUT_DIR / f"manifest-{label}.json"


def _read_manifest(label: str):
    source = _manifest_path(label)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
        data["files"]
        return data
    except (OSError, ValueError, KeyError, TypeError) as ex:
        print(f"cannot read snapshot {source}: {ex}", file=sys.stderr)
        return None


def _write_manifest(label: str, payload: dict) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    target = _manifest_path(label)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, target)
    return target


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def snapshot(label: str, allow_list=None) -> int:
    allow = None
    if allow_list:
        try:
            allow = _load_allow_list(allow_list)
        except (OSError, ValueError) as ex:
            print(f"cannot read allow-list {allow_list}: {ex}", file=sys.stderr)
            return 2
    entries = {}
    for rel, path in _iter_outside_files():
        try:
            entries[rel] = {"sha256": _sha256(path), "size": path.stat().st_size}
        except OSError as ex:
            entries[rel] = {"error": str(ex)}
    payload = {"label": label, "created_at": _now(),
               "repo_root": str(REPO_ROOT), "module": MODULE_DIR.relative_to(REPO_ROOT).as_posix(),
               "file_count": len(entries), "files": entries}
    if allow is not None:
        payload["allow_list"] = allow
    target = _write_manifest(label, payload)
    print(f"snapshot '{label}': {len(entries)} files outside the module -> {target}")
    if allow is not None:
        print(f"approved allow-list embedded ({len(allow['files'])} entries, sha256 {allow['sha256'][:12]}…)")
    return 0


def amend(label: str, allow_list) -> int:
    """Re-embeds an extended approved list (additions only; the file hashes of the snapshot stay)."""
    manifest = _read_manifest(label)
    if manifest is None:
        return 2
    if not allow_list:
        print("amend needs --allow-list FILE", file=sys.stderr)
        return 2
    old = manifest.get("allow_list")
    if not isinstance(old, dict):
        print(f"snapshot '{label}' has no embedded allow-list to amend", file=sys.stderr)
        return 2
    if manifest.get("closed"):
        print(f"snapshot '{label}' is closed ({manifest['closed'].get('closed_at')}) — a new exception "
              "needs a new snapshot label", file=sys.stderr)
        return 2
    try:
        new = _load_allow_list(allow_list)
    except (OSError, ValueError) as ex:
        print(f"cannot read allow-list {allow_list}: {ex}", file=sys.stderr)
        return 2
    removed = [f for f in old.get("files", []) if f not in new["files"]]
    if removed:
        print(f"amend refused: entries would be removed: {removed}", file=sys.stderr)
        return 2
    added = [f for f in new["files"] if f not in old.get("files", [])]
    history = list(manifest.get("amendments") or [])
    history.append({"at": _now(), "added": added, "sha256": new["sha256"]})
    manifest["allow_list"], manifest["amendments"] = new, history
    _write_manifest(label, manifest)
    print(f"allow-list of '{label}' amended: +{len(added)} entries: {added}")
    return 0


def close(label: str) -> int:
    """Freezes the exception: records the sha256 (or absence) of every allow-listed file."""
    manifest = _read_manifest(label)
    if manifest is None:
        return 2
    rule = _rule_from_manifest(label, manifest)
    if rule is None:
        print(f"snapshot '{label}' has no embedded allow-list", file=sys.stderr)
        return 2
    if rule.closed is not None:
        print(f"snapshot '{label}' is already closed ({manifest['closed'].get('closed_at')})", file=sys.stderr)
        return 2
    files = {}
    for rel, path in _iter_outside_files():
        if _allowed(rel, rule.patterns):
            files[rel] = _sha256(path)
    for pat in rule.patterns:          # exact entries that do not exist: recorded as absent
        if "*" not in pat and pat not in files:
            files[pat] = None
    manifest["closed"] = {"closed_at": _now(), "files": files}
    _write_manifest(label, manifest)
    print(f"snapshot '{label}' closed: {sum(1 for v in files.values() if v)} allow-listed files frozen "
          f"({sum(1 for v in files.values() if v is None)} absent); later changes are reported")
    return 0


def check(label: str, allow_list=None, allow_files=(), strict: bool = False) -> int:
    manifest = _read_manifest(label)
    if manifest is None:
        return 2
    old = manifest["files"]
    rules = []
    own = _rule_from_manifest(label, manifest)
    if own is not None:
        rules.append(own)
        state = "closed — frozen hashes" if own.closed is not None else "open"
        print(f"approved allow-list of '{label}' (embedded at snapshot time, {state}): {len(own.patterns)} entries")
    unapproved = []
    if not strict:
        for path in sorted(OUT_DIR.glob("manifest-*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            other = str(data.get("label")) if isinstance(data, dict) else ""
            if other == label:
                continue
            rule = _rule_from_manifest(other, data)
            if rule is not None:
                rules.append(rule)
                state = "closed — frozen hashes" if rule.closed is not None else "open"
                print(f"approved allow-list of snapshot '{other}' applied ({state}): {len(rule.patterns)} entries "
                      "(--strict to disable)")
    if allow_list:
        try:
            passed = _load_allow_list(allow_list)
        except (OSError, ValueError) as ex:
            print(f"cannot read allow-list {allow_list}: {ex}", file=sys.stderr)
            return 2
        print(f"allow-list passed: {passed['source']} ({len(passed['files'])} entries, label "
              f"{passed.get('label')!r}, sha256 {passed['sha256'][:12]}…)")
        if own is not None:
            unapproved = [f for f in passed["files"] if f not in own.patterns]
        rules.append(_Rule("--allow-list", passed["files"]))
    extra = []
    for f in allow_files or ():
        f = str(f).strip()
        if "*" in f or _entry_problem(f):
            print(f"cannot use --allow-file {f!r}: an exact path relative to the repo root is required",
                  file=sys.stderr)
            return 2
        extra.append(f)
        unapproved.append(f + "  (ad-hoc --allow-file)")
    if extra:
        rules.append(_Rule("--allow-file", extra))

    new = {rel: path for rel, path in _iter_outside_files()}
    changed, added, removed, docs, allowed = [], [], [], [], []
    shas = {}

    def _cur(rel):
        if rel not in shas:
            shas[rel] = _sha256(new[rel]) if rel in new else None
        return shas[rel]

    def _sort(rel, bucket, kind):
        if rel in DOC_EXCEPTIONS:
            docs.append(rel)
            return
        cur = _cur(rel)
        hit = next((r.name for r in rules if r.allows(rel, cur)), None)
        if hit:
            allowed.append(f"{rel}  ({kind}; {hit})")
        else:
            bucket.append(rel)

    for rel, info in old.items():
        if rel not in new:
            _sort(rel, removed, "removed")
        elif _cur(rel) != info.get("sha256"):
            _sort(rel, changed, "changed")
    for rel in new:
        if rel not in old:
            _sort(rel, added, "added")
    for title, items in (("CHANGED", changed), ("ADDED", added), ("REMOVED", removed),
                         ("allow-listed changes (approved list — review)", allowed),
                         ("allow-list entries NOT in the snapshot's approved copy — review", unapproved),
                         ("doc exceptions changed (HedgeFund sections only — review)", docs)):
        if items:
            print(f"{title}:")
            for rel in sorted(items):
                print(f"  {rel}")
    bad = changed or added or removed
    print("RESULT:", "UNEXPECTED CHANGES OUTSIDE THE MODULE" if bad else
          f"ok — nothing outside the module changed since '{label}'")
    return 1 if bad else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=("snapshot", "check", "amend", "close"))
    ap.add_argument("--label", default="stage0")
    ap.add_argument("--allow-list", default=None,
                    help="approved allow-list JSON (snapshot/amend: embedded; check: allowed in addition)")
    ap.add_argument("--allow-file", action="append", default=[],
                    help="check only: one more allowed exact path (repeatable, printed for review)")
    ap.add_argument("--strict", action="store_true",
                    help="check only: do not apply the approved allow-lists embedded in other snapshots")
    args = ap.parse_args(argv)
    if args.command == "snapshot":
        return snapshot(args.label, args.allow_list)
    if args.command == "amend":
        return amend(args.label, args.allow_list)
    if args.command == "close":
        return close(args.label)
    return check(args.label, args.allow_list, args.allow_file, args.strict)


if __name__ == "__main__":
    sys.exit(main())
