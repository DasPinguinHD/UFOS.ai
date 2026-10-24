"""Every number of the options archive in ONE place (SP §21.2 / §21.3). Filter (±10 % strikes, ≤ 45 days), cadence
(28 snapshots per session), the OI floor and the FRED series are CONFIRMED 2026-10-24 (the user's OS0 run
20261024T114530Z + the user's decision 13:49); the quote-staleness threshold and the coverage minimum stay PROVISIONAL
until the RTH run on Monday 2026-10-26 (``CONFIRMED`` / ``PROVISIONAL_KEYS`` below — kept OUT of the hashed config, so
the running archive's ``assumptions_hash`` did not change). Defaults below; ``data/options_archive_config.json`` may override known keys (validated: unknown key, wrong
type, NaN / Infinity, a value outside ``RANGES`` or an inconsistent combination is REFUSED with the reason — never
ignored, never clamped). The effective config's sha256 is the runs' ``assumptions_hash``.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import time
from typing import Dict, Optional, Tuple

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(BACKEND_DIR, "data")
VERSION = "os1-1"
OVERRIDE_FILE = os.path.join(DATA_DIR, "options_archive_config.json")


CONFIRMED_BY = "confirmed 2026-10-24 (OS0 run 20261024T114530Z)"
# key → the confirmed default (a key counts as confirmed only while the effective value equals it)
CONFIRMED = {
    "strike_band_pct": 10.0, "expiry_days": 45,                                      # filter (user decision 13:49)
    "cadence_minutes": 15, "intraday_first": "09:45", "intraday_last": "15:45",       # cadence: 25 intraday
    "fixed_slots": ("09:55", "15:25"), "eod_after_close_minutes": 15,                 # + 2 fixed + EOD = 28
    "oi_floor": "08:00",                     # OI of T published the morning of the next trading day (OS0 run)
    "fred_series": ("VIXCLS", "VXVCLS", "VXNCLS"),   # on FRED; VXSTCLS / VIX9DCLS are not (OS0 run)
}
# still PROVISIONAL: measured only during regular trading hours (RTH run Monday 2026-10-26)
PROVISIONAL_KEYS = ("quote_max_age_s", "coverage_min_pct")


@dataclass(frozen=True)
class Config:
    provisional: bool = True               # True while ANY key is provisional (see PROVISIONAL_KEYS)
    symbols: Tuple[str, ...] = ("SPY", "QQQ")
    strike_band_pct: float = 10.0          # ± around the spot at snapshot time — confirmed 2026-10-24 (OS0 run)
    expiry_days: int = 45                  # expiries today … today + N calendar days (0DTE archived) — confirmed
    cadence_minutes: int = 15              # intraday slots 09:45 … 15:45 New York — confirmed (28 per session)
    intraday_first: str = "09:45"
    intraday_last: str = "15:45"
    fixed_slots: Tuple[str, ...] = ("09:55", "15:25")    # H2 (10:00) / H1 (15:25) — must FINISH by the slot
    eod_after_close_minutes: int = 15      # EOD snapshot = close + 15 min (16:15; 13:15 on early-close days)
    oi_slot: str = "08:30"                 # contracts + OI once per morning
    fred_slot: str = "08:45"
    slot_lead_seconds: int = 90            # a chain snapshot starts at slot − lead and must finish by the slot
    quote_max_age_s: int = 300             # a quote counts only if ≤ 5 min old at the slot — PROVISIONAL (RTH run)
    coverage_min_pct: float = 80.0         # below → a "coverage" gap (GEX unavailable) — PROVISIONAL (RTH run)
    oi_floor: str = "08:00"                # OI of T never available before T+1 08:00 New York — confirmed
    rate_per_min: int = 30                 # the archive's OWN Alpaca gate (backend: 150 + burst 10)
    page_limit: int = 1000                 # page size for contracts and snapshots
    page_sleep_s: float = 0.5
    max_pages: int = 40
    retries: int = 1                       # ONE bounded retry after a timeout / reset / HTTP 5xx (recorded); 429 never
    retry_sleep_s: float = 2.0
    timeouts_s: Dict[str, float] = field(default_factory=lambda: {"contracts": 30.0, "snapshots": 20.0,
                                                                  "stock": 10.0, "fred": 20.0, "bars": 20.0})
    fred_series: Tuple[str, ...] = ("VIXCLS", "VXVCLS", "VXNCLS")
    fred_observations: int = 10            # newest N observations per run (revisions are new rows)
    eod_grace_minutes: int = 45            # a missed EOD / OI / FRED slot is still run up to this late (else missing)
    heartbeat_s: int = 60

    def as_json(self) -> str:
        d = asdict(self)
        return json.dumps(d, sort_keys=True, separators=(",", ":"))

    @property
    def assumptions_hash(self) -> str:
        return hashlib.sha256(self.as_json().encode("utf-8")).hexdigest()


def key_status(cfg: Config) -> dict:
    """Which keys are confirmed defaults (effective value == the confirmed one) and which are still provisional."""
    confirmed = [k for k, v in CONFIRMED.items() if getattr(cfg, k) == v]
    changed = [k for k in CONFIRMED if k not in confirmed]
    return {"confirmed_keys": confirmed, "confirmed_by": CONFIRMED_BY,
            "provisional_keys": list(PROVISIONAL_KEYS) + changed}


def hm(text: str) -> time:
    h, m = text.split(":")
    return time(int(h), int(m))


# Allowed range per numeric key (inclusive). Finite numbers only (NaN / Infinity refused). An override outside a
# range is REFUSED with the reason — never clamped (review M2).
RANGES: Dict[str, Tuple[float, float]] = {
    "strike_band_pct": (0.5, 50.0), "expiry_days": (0, 366), "cadence_minutes": (5, 390),
    "eod_after_close_minutes": (0, 120), "slot_lead_seconds": (10, 900), "quote_max_age_s": (1, 3600),
    "coverage_min_pct": (0.0, 100.0), "rate_per_min": (1, 30), "page_limit": (1, 1000), "page_sleep_s": (0.0, 10.0),
    "max_pages": (1, 100), "retries": (0, 1), "retry_sleep_s": (0.0, 60.0), "fred_observations": (1, 100),
    "eod_grace_minutes": (0, 120), "heartbeat_s": (10, 120)}
TIMEOUT_KEYS = ("contracts", "snapshots", "stock", "fred", "bars")
TIMEOUT_RANGE = (1.0, 120.0)
SYMBOLS_ALLOWED = ("SPY", "QQQ")                  # the transport allow-list knows only these two (SP §21.1)
_HHMM = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
_SERIES = re.compile(r"[A-Z0-9]{1,20}")


def _no_constant(name):
    raise ValueError(f"{name} is not a number (NaN / Infinity are refused)")


def validate(cfg: Config, where: str = "config") -> Config:
    """Every key in range, finite, consistent — raises ValueError naming the key (used for defaults AND overrides)."""
    def bad(msg):
        raise ValueError(f"{where}: {msg}")
    for k, (lo, hi) in RANGES.items():
        v = getattr(cfg, k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
            bad(f"{k} must be a finite number in {lo:g} … {hi:g} (got {v!r})")
    if set(cfg.timeouts_s) != set(TIMEOUT_KEYS):
        bad(f"timeouts_s must have exactly the keys {', '.join(TIMEOUT_KEYS)}")
    for k, v in cfg.timeouts_s.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or \
                not TIMEOUT_RANGE[0] <= v <= TIMEOUT_RANGE[1]:
            bad(f"timeouts_s.{k} must be {TIMEOUT_RANGE[0]:g} … {TIMEOUT_RANGE[1]:g} seconds (got {v!r})")
    if not cfg.symbols or len(set(cfg.symbols)) != len(cfg.symbols) or not set(cfg.symbols) <= set(SYMBOLS_ALLOWED):
        bad(f"symbols must be a non-empty list of distinct values from {', '.join(SYMBOLS_ALLOWED)}")
    if not cfg.fred_series or len(cfg.fred_series) > 10 or not all(_SERIES.fullmatch(x) for x in cfg.fred_series):
        bad("fred_series must be 1 … 10 FRED series ids (A-Z, 0-9)")
    if len(cfg.fixed_slots) > 4:
        bad("fixed_slots: at most 4")
    for name in ("intraday_first", "intraday_last", "oi_slot", "fred_slot", "oi_floor"):
        if not isinstance(getattr(cfg, name), str) or not _HHMM.fullmatch(getattr(cfg, name)):
            bad(f"{name} must be HH:MM (00:00 … 23:59)")
    for i, f in enumerate(cfg.fixed_slots):
        if not _HHMM.fullmatch(f):
            bad(f"fixed_slots[{i}] must be HH:MM")
    if hm(cfg.oi_slot) < hm(cfg.oi_floor):
        bad("oi_slot must not be before oi_floor (OI of the previous session is not available before the floor)")
    if hm(cfg.intraday_first) > hm(cfg.intraday_last):
        bad("intraday_first must not be after intraday_last")
    if not hm(cfg.oi_slot) < hm(cfg.intraday_first) or not hm(cfg.fred_slot) < hm(cfg.intraday_first):
        bad("oi_slot and fred_slot must be before intraday_first")
    return cfg


def load(path: Optional[str] = None) -> Config:
    """Defaults + the validated override file (absent → defaults). Raises ValueError naming the bad key — the archive
    then refuses to start (exit 3) and the backend routes answer 409 ``config_invalid``."""
    cfg = Config()
    p = path or OVERRIDE_FILE
    if not os.path.exists(p):
        return validate(cfg)
    name = os.path.basename(p)
    with open(p, encoding="utf-8") as fh:
        try:
            data = json.load(fh, parse_constant=_no_constant)
        except ValueError as ex:
            raise ValueError(f"{name}: not valid JSON ({str(ex)[:120]})") from None
    if not isinstance(data, dict):
        raise ValueError(f"{name}: expected a JSON object")
    known = {f.name: f for f in fields(Config)}
    changes = {}
    for k, v in data.items():
        if k not in known:
            raise ValueError(f"{name}: unknown key {k!r}")
        cur = getattr(cfg, k)
        if isinstance(cur, tuple):
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise ValueError(f"{name}: {k} must be a list of strings")
            v = tuple(v)
        elif isinstance(cur, dict):
            if not isinstance(v, dict) or not all(isinstance(x, (int, float)) and not isinstance(x, bool)
                                                  for x in v.values()):
                raise ValueError(f"{name}: {k} must be an object of numbers")
            unknown = set(v) - set(cur)
            if unknown:
                raise ValueError(f"{name}: {k} has unknown key(s) {', '.join(sorted(unknown))}")
            if not all(math.isfinite(float(x)) for x in v.values()):
                raise ValueError(f"{name}: {k} must hold finite numbers (NaN / Infinity are refused)")
            v = dict(cur, **{a: float(b) for a, b in v.items()})
        elif isinstance(cur, bool):
            if not isinstance(v, bool):
                raise ValueError(f"{name}: {k} must be true/false")
        elif isinstance(cur, (int, float)):
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"{name}: {k} must be a number")
            if not math.isfinite(float(v)):
                raise ValueError(f"{name}: {k} must be a finite number (NaN / Infinity are refused)")
            if isinstance(cur, int) and not isinstance(cur, bool) and float(v) != int(v):
                raise ValueError(f"{name}: {k} must be a whole number")
            v = type(cur)(v)
        elif isinstance(cur, str):
            if not isinstance(v, str):
                raise ValueError(f"{name}: {k} must be a string")
        changes[k] = v
    return validate(replace(cfg, **changes), name)


def paths(data_dir: Optional[str] = None) -> Dict[str, str]:
    d = data_dir or DATA_DIR
    return {"db": os.path.join(d, "options_archive.db"), "status": os.path.join(d, "os_archive_status.json"),
            "log": os.path.join(d, "logs", "options_archive.log"), "lock": os.path.join(d, "options_archive.lock"),
            "stop": os.path.join(d, "options_archive.stop")}
