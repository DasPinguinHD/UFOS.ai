"""SIC -> sector ETF benchmark for the Disclosures returns (INSIDER_RET_API.md §4, accepted 2026-10-24).

The table lives in ONE editable file next to this module, ``sector_etf_map.json``, with its source and date. It is an
approximation: SEC SIC codes predate the GICS sectors the SPDR Select Sector ETFs follow. The first matching rule
wins. A rule with ``"etf": null`` (blank checks, non-classifiable) and any SIC without a rule give no benchmark —
the caller shows "no sector ETF for SIC nnnn". There is never a SPY substitute (no unrequested fallback).

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Optional, Tuple

MAP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sector_etf_map.json")
CAVEAT = "Sector benchmark: approximation (SIC → GICS-based sector ETFs)."


class SectorMapError(ValueError):
    """sector_etf_map.json is missing or invalid (the message names the problem)."""


def validate(doc: dict) -> list:
    """The rules of a map document, checked: schema 1, a source text, ETFs from the declared list, sic ranges of
    integers 0-9999 with from <= to. Raises SectorMapError with the first problem."""
    if not isinstance(doc, dict) or doc.get("schema") != 1:
        raise SectorMapError("sector_etf_map.json: schema must be 1")
    if not str(doc.get("source") or "").strip():
        raise SectorMapError("sector_etf_map.json: 'source' (origin and date of the mapping) is required")
    etfs = doc.get("etfs")
    if not isinstance(etfs, list) or not etfs or not all(isinstance(e, str) and e.isupper() for e in etfs):
        raise SectorMapError("sector_etf_map.json: 'etfs' must list the ETF symbols")
    rules = doc.get("rules")
    if not isinstance(rules, list) or not rules:
        raise SectorMapError("sector_etf_map.json: 'rules' must be a non-empty list")
    out = []
    for i, r in enumerate(rules):
        where = f"sector_etf_map.json: rule {i + 1}"
        if not isinstance(r, dict):
            raise SectorMapError(f"{where}: must be an object")
        lo, hi, etf = r.get("sic_from"), r.get("sic_to"), r.get("etf")
        if not all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 9999 for v in (lo, hi)) or lo > hi:
            raise SectorMapError(f"{where}: sic_from/sic_to must be integers 0-9999 with sic_from <= sic_to")
        if etf is not None and etf not in etfs:
            raise SectorMapError(f"{where}: etf {etf!r} is not in 'etfs'")
        if not str(r.get("label") or "").strip():
            raise SectorMapError(f"{where}: 'label' is required")
        out.append((lo, hi, etf, str(r["label"])))
    return out


@lru_cache(maxsize=4)
def _load(path: str, mtime: float) -> Tuple[tuple, str, tuple]:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError as ex:
        raise SectorMapError(f"sector_etf_map.json not found ({path})") from ex
    except ValueError as ex:
        raise SectorMapError(f"sector_etf_map.json is not valid JSON: {ex}") from ex
    return tuple(validate(doc)), str(doc["source"]), tuple(doc["etfs"])


def table(path: str = MAP_FILE):
    """(rules, source, etfs) — re-read when the file changes (it is user-editable)."""
    try:
        mtime = os.path.getmtime(path)
    except OSError as ex:
        raise SectorMapError(f"sector_etf_map.json not found ({path})") from ex
    return _load(path, mtime)


def etf_for(sic, path: str = MAP_FILE) -> Tuple[Optional[str], Optional[str]]:
    """(etf, reason): the benchmark ETF for a SIC code, or (None, "no sector ETF for SIC nnnn") /
    (None, "SIC code unknown")."""
    try:
        code = int(str(sic).strip()) if sic not in (None, "") else None
    except ValueError:
        code = None
    if code is None:
        return None, "SIC code unknown"
    rules, _source, _etfs = table(path)
    for lo, hi, etf, _label in rules:
        if lo <= code <= hi:
            return (etf, None) if etf else (None, f"no sector ETF for SIC {code:04d}")
    return None, f"no sector ETF for SIC {code:04d}"
