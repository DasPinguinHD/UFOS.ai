#!/usr/bin/env python3
"""Re-export the user's LOCAL real-data fixture of the Council parity gate (licence clean-up 2026-10-25).

Writes ``backend/data/local_fixtures/council_parity_real_bars.json`` (git-ignored — the user's own Alpaca bars, never
committed) from a COPY of ``backend/data/hedgefund.db``: the database (and its -wal / -shm files) is copied into a
temporary folder, its sha256 is taken before and after the copy and must be unchanged (otherwise the backend was
writing — stop it and run again), and only the copy is opened. The real file is never opened by SQLite.

Layout = the committed synthetic fixture (tests/council_parity_synth_bars.py): {"_about", "exported_at",
"source_sha256", "symbols": {sym: {"source": [...], "bars": [[date, open, high, low, close, volume], ...]}}}, the last
``--days`` daily bars up to ``--until``; one source per day (alpaca_sip > alpaca > tiingo); equities dated by their New
York session date, crypto by the UTC date.

Run from the repository root:  python HedgeFund/tools/export_council_parity_bars.py [--db PATH] [--until 2026-10-23]
(stdlib only, so it lives next to scope_guard.py / check_repo_data.py — no backend venv needed)
Exit codes: 0 ok · 2 usage / database missing · 3 the database changed during the copy · 4 not enough bars.
Stdlib only. Part of UFOS.ai, a proof-of-concept portfolio project.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
DEFAULT_DB = os.path.join(BACKEND, "data", "hedgefund.db")
DEFAULT_OUT = os.path.join(BACKEND, "data", "local_fixtures", "council_parity_real_bars.json")
SYMBOLS = ("SPY", "QQQ", "TLT", "GLD", "BTC-USD")
CRYPTO = ("BTC-USD",)
SOURCE_RANK = {"alpaca_sip": 0, "alpaca": 1, "tiingo": 2}
NY = ZoneInfo("America/New_York")


class DatabaseChanged(RuntimeError):
    pass


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def copy_database(src: str, folder: str, copy=shutil.copy2) -> tuple:
    """(copy path, sha256) — the db and its -wal/-shm siblings; raises DatabaseChanged when any of them changed while
    copying (sha256 before != after)."""
    files = [p for p in (src, src + "-wal", src + "-shm") if os.path.exists(p)]
    before = {p: sha256_file(p) for p in files}
    target = os.path.join(folder, os.path.basename(src))
    for p in files:
        copy(p, target + p[len(src):])
    after = {p: sha256_file(p) for p in files}
    if before != after:
        raise DatabaseChanged("the database changed while it was copied — stop the backend and run again")
    copied = {p: sha256_file(target + p[len(src):]) for p in files}
    if copied != before:
        raise DatabaseChanged("the copy differs from the source — run again")
    return target, before[src]


def _day(symbol: str, ts: str) -> date:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    return dt.astimezone(timezone.utc).date() if symbol in CRYPTO else dt.astimezone(NY).date()


def read_bars(db_path: str, until: date, days: int) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bars)")}
        sql = "SELECT symbol, ts, open, high, low, close, volume, source FROM bars WHERE timeframe = '1Day' AND symbol = ?"
        if "adjustment" in cols:
            sql += " AND COALESCE(adjustment, 'all') = 'all'"
        out = {}
        for sym in SYMBOLS:
            best = {}
            for _s, ts, o, h, l, c, v, src in conn.execute(sql, (sym,)):
                d = _day(sym, ts)
                if d > until or src not in SOURCE_RANK:
                    continue
                if d not in best or SOURCE_RANK[src] < SOURCE_RANK[best[d][6]]:
                    best[d] = (d.isoformat(), o, h, l, c, v if v is not None else 0.0, src)
            rows = [best[d] for d in sorted(best)][-days:]
            out[sym] = {"source": sorted({r[6] for r in rows}), "bars": [list(r[:6]) for r in rows]}
        return out
    finally:
        conn.close()


def export(db: str = DEFAULT_DB, out: str = DEFAULT_OUT, until: date = date(2026, 10, 23), days: int = 260,
           copy=shutil.copy2) -> dict:
    if not os.path.isfile(db):
        raise FileNotFoundError(f"database not found: {db}")
    with tempfile.TemporaryDirectory(prefix="ufos-parity-export-") as tmp:
        copy_path, digest = copy_database(db, tmp, copy)
        symbols = read_bars(copy_path, until, days)
    short = {s: len(v["bars"]) for s, v in symbols.items() if len(v["bars"]) < days}
    if short:
        raise ValueError(f"not enough daily bars up to {until} (need {days}): {short}")
    doc = {"_about": (f"LOCAL real-data fixture of the Council parity gate (SP §22.10 (c)) — the user's own Alpaca bars, "
                      f"git-ignored, never committed: the last {days} daily bars up to {until} of "
                      f"{', '.join(SYMBOLS)}, exported from a COPY of hedgefund.db; one source per day "
                      f"(alpaca_sip > alpaca > tiingo). Columns: date, open, high, low, close, volume."),
           "exported_at": datetime.now(timezone.utc).date().isoformat(), "source_sha256": digest, "symbols": symbols}
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp_out = out + ".tmp"
    with open(tmp_out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    os.replace(tmp_out, out)
    if sha256_file(db) != digest:
        raise DatabaseChanged("the database changed during the export — the written file may mix two states; run again")
    return doc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--until", default="2026-10-23")
    ap.add_argument("--days", type=int, default=260)
    a = ap.parse_args(argv)
    try:
        doc = export(a.db, a.out, date.fromisoformat(a.until), a.days)
    except FileNotFoundError as ex:
        print(f"REFUSED: {ex}", file=sys.stderr)
        return 2
    except DatabaseChanged as ex:
        print(f"REFUSED: {ex}", file=sys.stderr)
        return 3
    except ValueError as ex:
        print(f"REFUSED: {ex}", file=sys.stderr)
        return 4
    print(f"wrote {a.out}: " + ", ".join(f"{s} {len(v['bars'])} bars" for s, v in doc["symbols"].items())
          + f" (source sha256 {doc['source_sha256'][:12]}, unchanged)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
