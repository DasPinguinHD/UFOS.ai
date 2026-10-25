#!/usr/bin/env python3
"""Scan what would be committed for licensed market data and personal data (licence clean-up, lead 2026-10-25).

The file set is exactly what git would commit: ``git ls-files`` (tracked) plus ``git ls-files --others
--exclude-standard`` (untracked, not ignored) — read-only git with GIT_OPTIONAL_LOCKS=0, nothing is written.

Rules (each finding: ``<path>: <rule>: <detail>``):
- ``data.file``      a data file outside ``vendor/``: *.db, *.sqlite(3), *.db-wal/-shm, *.csv, *.tsv, *.parquet,
                     *.feather, *.pkl/*.pickle, *.zip, *.gz/*.tgz.
- ``price.series``   a JSON / .py / .txt file that looks like a price series: a list of >= 20 records with >= 3
                     OHLC/volume keys, a numeric array of >= 50 numbers, >= 20 numeric rows of >= 4 values, or >= 20
                     ``close``/``open``/... value literals in source text.
- ``provider.alpaca`` Alpaca bar records (an object with the keys t, o, h, l, c, v).
- ``personal.iban``  an IBAN with a valid mod-97 checksum.
- ``personal.tr_import`` a file from the Trade Republic import folder (``tr-import``).
- ``personal.account`` an Alpaca account number (PA + 10 characters) or ``account number = <6+ digits>``.
- ``vendor.manifest`` a ``vendor/`` file whose sha256 differs from its MANIFEST line, a MANIFEST line without its file,
                     or a vendor file that is not listed (README*/MANIFEST themselves excepted).

False positives go into ``HedgeFund/tools/repo-data-allow.json`` — exact path + rule + the reviewed reason; an entry
that no longer matches anything is reported as stale (exit stays 0 for stale entries).

Usage (from anywhere in the repository):  python HedgeFund/tools/check_repo_data.py [--root PATH]
Exit codes: 0 clean · 1 findings · 2 usage (no git / not a repository / invalid allow-list). Stdlib only.
Part of UFOS.ai, a proof-of-concept portfolio project.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

ALLOW_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "repo-data-allow.json")
DATA_EXT = (".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm", ".csv", ".tsv", ".parquet", ".feather", ".pkl",
            ".pickle", ".zip", ".gz", ".tgz")
TEXT_EXT = (".json", ".py", ".txt")
PRICE_KEYS = {"open", "high", "low", "close", "volume", "adj_close", "adjclose", "vwap", "o", "h", "l", "c", "v"}
OHLC_CORE = {"open", "high", "low", "close", "o", "h", "l", "c"}
ALPACA_KEYS = {"t", "o", "h", "l", "c", "v"}
MIN_RECORDS, MIN_NUMBERS, MIN_ROWS, MIN_LITERALS = 20, 50, 20, 20
MAX_SCAN_BYTES = 8 * 1024 * 1024
RULES = ("data.file", "price.series", "provider.alpaca", "personal.iban", "personal.tr_import", "personal.account",
         "vendor.manifest")

_NUM_ROW = re.compile(r"(?:-?\d+(?:\.\d+)?\s*,\s*){3,}-?\d+\.\d+")
_PRICE_LITERAL = re.compile(r"""["'](?:open|high|low|close|adj_close|volume)["']\s*:\s*-?\d""", re.IGNORECASE)
_ALPACA_BLOCK = re.compile(r"\{[^{}]{0,400}\}")
_IBAN = re.compile(r"\b([A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?)\b")
_ALPACA_ACCOUNT = re.compile(r"\bPA[0-9A-Z]{10}\b")
_ACCOUNT_NO = re.compile(r"account[ _-]?(?:no|number|nr|num)\b\W{0,4}\d{6,}", re.IGNORECASE)


class UsageError(RuntimeError):
    pass


# --- file set --------------------------------------------------------------------------------------------------------

def _git(root: str, *args: str) -> List[str]:
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    try:
        p = subprocess.run(["git", "-C", root, *args], capture_output=True, env=env)
    except FileNotFoundError as ex:
        raise UsageError("git is not installed") from ex
    if p.returncode != 0:
        raise UsageError(f"git {' '.join(args)} failed: {p.stderr.decode(errors='replace').strip()[:200]}")
    return [x for x in p.stdout.decode("utf-8", "replace").split("\0") if x]


def repo_root(start: str) -> str:
    return _git(start, "rev-parse", "--show-toplevel")[0].strip() if start else start


def commit_set(root: str) -> List[str]:
    """Posix paths of the files git would commit (tracked + untracked-not-ignored), sorted."""
    tracked = _git(root, "ls-files", "-z")
    untracked = _git(root, "ls-files", "-z", "--others", "--exclude-standard")
    return sorted({p for p in tracked + untracked if os.path.isfile(os.path.join(root, p))})


# --- helpers ---------------------------------------------------------------------------------------------------------

def _in_vendor(path: str) -> bool:
    return "vendor" in path.split("/")[:-1]


def iban_valid(text: str) -> bool:
    s = text.replace(" ", "").upper()
    if not 15 <= len(s) <= 34 or not s[:2].isalpha() or not s[2:4].isdigit():
        return False
    moved = s[4:] + s[:4]
    digits = "".join(str(int(ch, 36)) for ch in moved)
    return int(digits) % 97 == 1


def _walk(obj, visit):
    stack = [obj]
    while stack:
        cur = stack.pop()
        visit(cur)
        if isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def json_findings(doc) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    seen = set()

    def visit(node):
        if isinstance(node, dict):
            if ALPACA_KEYS <= set(map(str, node.keys())) and "provider.alpaca" not in seen:
                seen.add("provider.alpaca")
                out.append(("provider.alpaca", "object with the Alpaca bar keys t, o, h, l, c, v"))
        elif isinstance(node, list) and "price.series" not in seen:
            recs = [x for x in node if isinstance(x, dict)]
            if len(recs) >= MIN_RECORDS:
                ohlc = sum(1 for r in recs if len({str(k).lower() for k in r} & PRICE_KEYS) >= 3
                           and {str(k).lower() for k in r} & OHLC_CORE)
                if ohlc >= MIN_RECORDS:
                    seen.add("price.series")
                    out.append(("price.series", f"{ohlc} records with OHLC/volume keys"))
                    return
            nums = sum(1 for x in node if _is_num(x))
            if nums >= MIN_NUMBERS:
                seen.add("price.series")
                out.append(("price.series", f"numeric array of {nums} values"))
                return
            rows = sum(1 for x in node if isinstance(x, list) and sum(1 for y in x if _is_num(y)) >= 4)
            if rows >= MIN_ROWS:
                seen.add("price.series")
                out.append(("price.series", f"{rows} numeric rows of >= 4 values"))
    _walk(doc, visit)
    return out


def text_findings(text: str) -> List[Tuple[str, str]]:
    out = []
    rows = len(_NUM_ROW.findall(text))
    if rows >= MIN_ROWS:
        out.append(("price.series", f"{rows} numeric rows of >= 4 values"))
    lits = len(_PRICE_LITERAL.findall(text))
    if lits >= MIN_LITERALS:
        out.append(("price.series", f"{lits} open/high/low/close/volume value literals"))
    for block in _ALPACA_BLOCK.findall(text):
        # a RECORD: o/h/l/c/v carry number literals (code that builds such dicts from variables is not data)
        keys = set(re.findall(r"""["']([ohlcv])["']\s*:\s*-?\d""", block))
        if {"o", "h", "l", "c", "v"} <= keys and re.search(r"""["']t["']\s*:""", block):
            out.append(("provider.alpaca", "record literal with the Alpaca bar keys t, o, h, l, c, v"))
            break
    return out


def personal_findings(text: str) -> List[Tuple[str, str]]:
    out = []
    ibans = sorted({m for m in _IBAN.findall(text) if iban_valid(m)})
    if ibans:
        out.append(("personal.iban", f"{len(ibans)} IBAN(s) with a valid checksum, e.g. {ibans[0][:4]}…"))
    if _ALPACA_ACCOUNT.search(text):
        out.append(("personal.account", "Alpaca-style account number (PA + 10 characters)"))
    if _ACCOUNT_NO.search(text):
        out.append(("personal.account", "'account number' followed by 6+ digits"))
    return out


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def vendor_findings(root: str, paths: Sequence[str]) -> List[Tuple[str, str, str]]:
    out = []
    by_dir: Dict[str, List[str]] = {}
    for p in paths:
        if _in_vendor(p):
            parts = p.split("/")
            i = len(parts) - 2 - parts[:-1][::-1].index("vendor")
            by_dir.setdefault("/".join(parts[:i + 1]), []).append(p)
    for vdir, files in sorted(by_dir.items()):
        manifest = f"{vdir}/MANIFEST"
        listed = {}
        if manifest not in files:
            out += [(f, "vendor.manifest", f"no MANIFEST in {vdir}") for f in files]
            continue
        with open(os.path.join(root, manifest), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) < 4 or not re.fullmatch(r"[0-9a-f]{64}", parts[3]):
                    out.append((manifest, "vendor.manifest", f"malformed line: {line[:60]}"))
                    continue
                listed[f"{vdir}/{parts[0]}"] = parts[3]
        for f, digest in sorted(listed.items()):
            full = os.path.join(root, f)
            if not os.path.isfile(full):
                out.append((f, "vendor.manifest", "listed in MANIFEST but missing"))
            elif sha256_file(full) != digest:
                out.append((f, "vendor.manifest", "sha256 differs from MANIFEST (line endings converted?)"))
        for f in files:
            name = f.rsplit("/", 1)[-1]
            if f not in listed and name != "MANIFEST" and not name.upper().startswith("README"):
                out.append((f, "vendor.manifest", "vendor file not listed in MANIFEST"))
    return out


# --- scan ------------------------------------------------------------------------------------------------------------

def scan(root: str, paths: Iterable[str]) -> List[Tuple[str, str, str]]:
    """[(path, rule, detail)] for the given repository-relative posix paths."""
    paths = sorted(paths)
    out: List[Tuple[str, str, str]] = []
    for p in paths:
        low = p.lower()
        if "tr-import" in low.split("/") or "/tr-import/" in "/" + low:
            out.append((p, "personal.tr_import", "file from the Trade Republic import folder"))
        if _in_vendor(p):
            continue
        if low.endswith(DATA_EXT):
            out.append((p, "data.file", "data file outside vendor/"))
            continue
        full = os.path.join(root, p)
        try:
            if os.path.getsize(full) > MAX_SCAN_BYTES:
                out.append((p, "data.file", f"file larger than {MAX_SCAN_BYTES // (1024 * 1024)} MB — review"))
                continue
            with open(full, "rb") as fh:
                raw = fh.read()
        except OSError as ex:
            out.append((p, "data.file", f"unreadable: {ex}"))
            continue
        if b"\0" in raw[:8192]:
            continue                                     # binary (images, icons): only the extension rule applies
        text = raw.decode("utf-8", "replace")
        found = personal_findings(text)
        if low.endswith(".json"):
            try:
                found += json_findings(json.loads(text))
            except ValueError:
                found += text_findings(text)
        elif low.endswith(TEXT_EXT):
            found += text_findings(text)
        out += [(p, rule, detail) for rule, detail in found]
    out += vendor_findings(root, paths)
    return sorted(set(out))


def load_allow(path: str = ALLOW_FILE) -> List[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return []
    except ValueError as ex:
        raise UsageError(f"{path}: invalid JSON ({ex})") from ex
    entries = doc.get("entries") if isinstance(doc, dict) else None
    if doc.get("schema") != 1 or not isinstance(entries, list):
        raise UsageError(f"{path}: expected {{\"schema\": 1, \"entries\": [...]}}")
    for i, e in enumerate(entries):
        if not isinstance(e, dict) or e.get("rule") not in RULES or not e.get("path") or not str(e.get("reason") or "").strip():
            raise UsageError(f"{path}: entry {i + 1} needs path, a known rule and a reviewed reason")
    return entries


def apply_allow(findings, entries) -> Tuple[list, list]:
    """(remaining findings, stale allow-list entries)."""
    allowed = {(e["path"], e["rule"]) for e in entries}
    used = {(p, r) for p, r, _d in findings if (p, r) in allowed}
    return [f for f in findings if (f[0], f[1]) not in allowed], [e for e in entries if (e["path"], e["rule"]) not in used]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=None, help="repository root (default: the repository of this script)")
    ap.add_argument("--allow", default=ALLOW_FILE)
    a = ap.parse_args(argv)
    try:
        root = repo_root(a.root or os.path.dirname(os.path.abspath(__file__)))
        paths = commit_set(root)
        entries = load_allow(a.allow)
    except UsageError as ex:
        print(f"REFUSED: {ex}", file=sys.stderr)
        return 2
    findings, stale = apply_allow(scan(root, paths), entries)
    for p, rule, detail in findings:
        print(f"{p}: {rule}: {detail}")
    for e in stale:
        print(f"note: stale allow-list entry (matches nothing any more): {e['path']} [{e['rule']}]")
    print(f"{'FAILED' if findings else 'OK'} — {len(paths)} files scanned, {len(findings)} finding(s), "
          f"{len(entries) - len(stale)} allow-listed")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
