"""Every number and assumption of the OS2 computations in ONE place (SP §21.12). Defaults below;
``data/options_structure_config.json`` may override known keys — validated like the archive's override (an unknown
key, a wrong type, NaN / Infinity or a value outside ``RANGES`` is REFUSED with the reason, never clamped).

**Confirmation (like the archive's ``CONFIRMED``):** the same file may hold ``"confirmed": {key: value, …}`` (+ an
optional ``"confirmed_note"``, ≤ 200 characters) for keys in ``PROVISIONAL_KEYS``. A key counts as confirmed only while
its EFFECTIVE value equals the confirmed one (a later change makes it provisional again). Validated: an unknown key, a
key that is not provisional or a value of the wrong type is REFUSED. The confirmation is kept OUT of the hashed
assumptions (confirming does not change ``assumptions_hash``, so the history continues).

The whole OS2 output is ``provisional`` while the ARCHIVE config is provisional (quote staleness / coverage, SP §21.10)
OR any key in ``PROVISIONAL_KEYS`` is unconfirmed. ``assumptions_hash`` = sha256 of the canonical JSON of this config
plus the archive numbers OS2 applies (quote max age, coverage minimum, rate series, OI floor) — stored with every
output; a changed value → a new hash → the OS2 history restarts under it (old rows kept).

Assumptions (also in every output's ``assumptions``):
* the risk-free rate is the FRED series in the archive's ``rate_series`` (DTB3, 3-month T-bill, discount basis;
  confirmed by the user 2026-11-06) converted to a continuously compounded ACT/365 rate
  (``bs.rate_from_discount_yield``), one flat rate for every expiry ≤ 45 days;
* the dividend yield ``q`` is IMPLIED PER EXPIRY from put-call parity (user decision 2026-11-06): the median of
  ``q_K = −ln((C − P + K e^{−rT}) / S) / T`` over the strikes within ±``parity_band_pct`` of the spot where both the call
  and the put have a usable mid (≥ ``parity_min_pairs`` pairs; fewer → that expiry is unavailable, no fallback).
  SPY / QQQ options are AMERICAN: early exercise (puts; calls before an ex-dividend date) makes European parity only
  approximate, so the implied q is a market-consistent carry that absorbs the early-exercise premium and any rate
  mismatch — not a dividend forecast. The fixed per-symbol ``dividend_yield`` is only a cross-check in the quality
  view;
* option expiry at 16:00 New York on the expiration date (SPY / QQQ options are PM-settled); time to expiry in
  calendar years (ACT/365) from the slot;
* an implied q with |q| > ``q_flag_abs_pct`` or a pair MAD > ``q_flag_mad_pct`` is FLAGGED and shown (``q_flags``),
  the expiry stays in use (PROVISIONAL thresholds; flagged + shown, never silently dropped);
* open interest is used only when its ``valid_for`` is D−1 … D−``oi_max_age_sessions`` (= 2, the archive's fresh
  window ``OI_FRESH_SESSIONS``; user decision 2026-11-06 b); older OI counts as no fresh OI —
  a contract without (fresh) OI contributes 0 (no OI reported);
* evaluability (user decision 2026-11-06 b): a decision time is not evaluable for the regime when the expiries
  excluded from the total GEX carry more than ``max_excluded_oi_pct`` (= 15) % of the total OI;
* dealer positioning: three variants in parallel (``gex.VARIANTS``), the baseline A is the pre-registration's.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass, field, fields, replace
from typing import Dict, Optional, Tuple

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(BACKEND_DIR, "data")
OVERRIDE_NAME = "options_structure_config.json"
MODEL_VERSION = "os2-1"

# keys whose values are assumptions until the user confirms them (``"confirmed"`` in the override file)
PROVISIONAL_KEYS = ("spread_max_pct", "parity_band_pct", "parity_min_pairs", "state_max_age_minutes", "rr_history_min",
                    "q_flag_abs_pct", "q_flag_mad_pct")
CONFIRMED_NOTE_MAX = 200


@dataclass(frozen=True)
class Config:
    # --- quote quality (SP §21.12 / task §1) -----------------------------------------------------------------
    spread_max_pct: float = 50.0          # a mid is used only when (ask − bid) / mid ≤ this (indicative feed: wide)
    # --- model inputs ----------------------------------------------------------------------------------------
    dividend_yield: Dict[str, float] = field(default_factory=lambda: {"SPY": 0.012, "QQQ": 0.006})  # cross-check only
    parity_band_pct: float = 3.0          # implied q: put-call pairs with strikes within ± this of the spot
    parity_min_pairs: int = 3             # … at least this many pairs per expiry, else the expiry is unavailable
    q_flag_abs_pct: float = 5.0           # implied q: |q| above this (% p.a.) → the expiry is FLAGGED (shown, still used)
    q_flag_mad_pct: float = 2.0           # … or the MAD of the pair values above this (% p.a.) → flagged likewise
    rate_tenor_days: int = 91             # DTB3: a 13-week bill (discount basis, ACT/360) → continuous ACT/365
    rate_max_age_days: int = 7            # the newest archived rate observation must be ≤ this old at the slot
    iv_min: float = 0.001                 # implied-vol root bracket (decimal); outside → unavailable, never clipped
    iv_max: float = 5.0
    # --- open interest (user decision 2026-11-06 b) ---------------------------------------------------------------
    oi_max_age_sessions: int = 2          # OI used only when valid_for is D−1 … D−this (the archive's fresh window)
    # --- GEX -------------------------------------------------------------------------------------------------
    flip_range_pct: float = 5.0           # the flip curve: hypothetical spots ± this
    flip_points: int = 101                # grid points of the flip curve (odd: the spot itself is one)
    buckets: Tuple[Tuple[str, int, int], ...] = (("0-7", 0, 7), ("8-30", 8, 30), ("31-45", 31, 45))
    # --- surface ---------------------------------------------------------------------------------------------
    rr_delta: float = 0.25                # 25-delta risk reversal / butterfly
    vrp_target_days: int = 30             # IV_ATM at ≈ 30 days (total-variance interpolation between expiries)
    rv_sessions: int = 21                 # realised vol: close-to-close log returns, 21 sessions, √252
    rr_history_min: int = 20              # a percentile of today's RR needs ≥ this many earlier slots
    # --- regime / state --------------------------------------------------------------------------------------
    state_max_age_minutes: int = 30       # GET …/state: the newest slot older than this → "not available"
    job_batch: int = 20                   # archive runs per job tick (the job catches up over several ticks)
    max_excluded_oi_pct: float = 15.0     # regime evaluable only when the excluded expiries carry ≤ this % of the OI

    def model_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


RANGES: Dict[str, Tuple[float, float]] = {
    "spread_max_pct": (1.0, 200.0), "parity_band_pct": (0.5, 20.0), "parity_min_pairs": (1, 50), "q_flag_abs_pct": (0.1, 100.0), "q_flag_mad_pct": (0.01, 100.0), "oi_max_age_sessions": (1, 2), "max_excluded_oi_pct": (0.0, 100.0), "rate_tenor_days": (1, 366), "rate_max_age_days": (1, 30), "iv_min": (1e-6, 0.5), "iv_max": (0.5, 20.0),
    "flip_range_pct": (0.5, 25.0), "flip_points": (11, 1001), "rr_delta": (0.05, 0.45), "vrp_target_days": (1, 45),
    "rv_sessions": (5, 252), "rr_history_min": (1, 10000), "state_max_age_minutes": (1, 1440),
    "job_batch": (1, 500)}
SYMBOLS = ("SPY", "QQQ")


def validate(cfg: Config, where: str = "config") -> Config:
    def bad(msg):
        raise ValueError(f"{where}: {msg}")
    for k, (lo, hi) in RANGES.items():
        v = getattr(cfg, k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
            bad(f"{k} must be a finite number in {lo:g} … {hi:g} (got {v!r})")
    from strategy_platform.options_archive.config import OI_FRESH_SESSIONS
    if cfg.oi_max_age_sessions > OI_FRESH_SESSIONS:
        bad(f"oi_max_age_sessions must be ≤ the archive's fresh OI window ({OI_FRESH_SESSIONS} sessions)")
    if cfg.iv_min >= cfg.iv_max:
        bad("iv_min must be below iv_max")
    if cfg.flip_points % 2 == 0:
        bad("flip_points must be odd (the spot itself is a grid point)")
    if set(cfg.dividend_yield) != set(SYMBOLS):
        bad(f"dividend_yield must give exactly {', '.join(SYMBOLS)}")
    for s, q in cfg.dividend_yield.items():
        if isinstance(q, bool) or not isinstance(q, (int, float)) or not math.isfinite(q) or not 0.0 <= q <= 0.2:
            bad(f"dividend_yield.{s} must be a decimal in 0 … 0.2 (got {q!r})")
    last = -1
    for i, b in enumerate(cfg.buckets):
        if len(b) != 3 or not isinstance(b[0], str) or not all(isinstance(x, int) for x in b[1:]) or \
                not last < b[1] <= b[2] <= 366:
            bad(f"buckets[{i}] must be [label, from_day, to_day] with ascending, non-overlapping day ranges")
        last = b[2]
    return cfg


def _no_constant(name):
    raise ValueError(f"{name} is not a number (NaN / Infinity are refused)")


def load(data_dir: Optional[str] = None) -> Config:
    """Defaults + the validated override (absent → defaults). Raises ValueError naming the key."""
    cfg = Config()
    p = os.path.join(data_dir or DATA_DIR, OVERRIDE_NAME)
    if not os.path.exists(p):
        return validate(cfg)
    with open(p, encoding="utf-8") as fh:
        try:
            data = json.load(fh, parse_constant=_no_constant)
        except ValueError as ex:
            raise ValueError(f"{OVERRIDE_NAME}: not valid JSON ({str(ex)[:120]})") from None
    if not isinstance(data, dict):
        raise ValueError(f"{OVERRIDE_NAME}: expected a JSON object")
    known = {f.name for f in fields(Config)}
    changes = {}
    for k, v in data.items():
        if k in ("confirmed", "confirmed_note"):
            continue                     # validated by load_confirmed (kept out of the hashed config)
        if k not in known:
            raise ValueError(f"{OVERRIDE_NAME}: unknown key {k!r}")
        cur = getattr(cfg, k)
        if k == "buckets":
            if not isinstance(v, list) or not all(isinstance(b, list) for b in v):
                raise ValueError(f"{OVERRIDE_NAME}: buckets must be a list of [label, from_day, to_day]")
            v = tuple(tuple(b) for b in v)
        elif isinstance(cur, dict):
            if not isinstance(v, dict) or set(v) - set(cur):
                raise ValueError(f"{OVERRIDE_NAME}: {k} must be an object with keys from {', '.join(cur)}")
            v = dict(cur, **v)
        elif isinstance(cur, (int, float)) and not isinstance(cur, bool):
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"{OVERRIDE_NAME}: {k} must be a number")
            if isinstance(cur, int) and float(v) != int(v):
                raise ValueError(f"{OVERRIDE_NAME}: {k} must be a whole number")
            v = type(cur)(v)
        changes[k] = v
    return validate(replace(cfg, **changes), OVERRIDE_NAME)


def load_confirmed(data_dir: Optional[str] = None, cfg: Optional[Config] = None) -> Dict[str, object]:
    """``{"values": {key: confirmed value}, "note": str|None}`` from the override file (absent → nothing confirmed).
    Raises ValueError (refused) for an unknown / non-provisional key, a wrong type or a too long note."""
    p = os.path.join(data_dir or DATA_DIR, OVERRIDE_NAME)
    out: Dict[str, object] = {"values": {}, "note": None}
    if not os.path.exists(p):
        return out
    with open(p, encoding="utf-8") as fh:
        try:
            data = json.load(fh, parse_constant=_no_constant)
        except ValueError as ex:
            raise ValueError(f"{OVERRIDE_NAME}: not valid JSON ({str(ex)[:120]})") from None
    if not isinstance(data, dict):
        raise ValueError(f"{OVERRIDE_NAME}: expected a JSON object")
    conf = data.get("confirmed", {})
    if not isinstance(conf, dict):
        raise ValueError(f"{OVERRIDE_NAME}: confirmed must be an object {{key: value}}")
    base = cfg or Config()
    for k, v in conf.items():
        if k not in PROVISIONAL_KEYS:
            raise ValueError(f"{OVERRIDE_NAME}: confirmed.{k}: only {', '.join(PROVISIONAL_KEYS)} can be confirmed")
        cur = getattr(base, k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)) or \
                (isinstance(cur, int) and float(v) != int(v)):
            raise ValueError(f"{OVERRIDE_NAME}: confirmed.{k} must be a {'whole ' if isinstance(cur, int) else ''}"
                             "number")
        out["values"][k] = type(cur)(v)
    note = data.get("confirmed_note")
    if note is not None and (not isinstance(note, str) or len(note) > CONFIRMED_NOTE_MAX):
        raise ValueError(f"{OVERRIDE_NAME}: confirmed_note must be a string of ≤ {CONFIRMED_NOTE_MAX} characters")
    out["note"] = note
    return out


def archive_numbers(acfg) -> dict:
    """The archive config values OS2 applies (they enter OS2's assumptions hash)."""
    return {"quote_max_age_s": acfg.quote_max_age_s, "coverage_min_pct": acfg.coverage_min_pct,
            "strike_band_pct": acfg.strike_band_pct, "expiry_days": acfg.expiry_days,
            "rate_series": list(acfg.rate_series), "oi_floor": acfg.oi_floor}


def assumptions(cfg: Config, acfg) -> dict:
    """The full, canonical assumption set of an output (hashed)."""
    return {"model_version": MODEL_VERSION, "os2": json.loads(cfg.model_json()), "archive": archive_numbers(acfg),
            "rate": f"FRED {'/'.join(acfg.rate_series) or '(none)'} (discount basis, {cfg.rate_tenor_days}-day bill) → "
                    "continuous ACT/365; one flat rate for all expiries (confirmed 2026-11-06)",
            "dividend_yield": f"implied per expiry from put-call parity: median over strikes within ±"
                              f"{cfg.parity_band_pct:g} % of the spot with both mids usable (≥ {cfg.parity_min_pairs} "
                              "pairs, else the expiry is unavailable); American exercise makes parity approximate — "
                              "the implied q is a market-consistent carry; the fixed yields are a cross-check only",
            "open_interest": "per contract the newest OI with valid_for ≤ the previous session and available_at ≤ the "
                             f"slot (in practice D−2; user decision 2026-11-06), used only when valid_for is D−1 … "
                             f"D−{cfg.oi_max_age_sessions} (older = no fresh OI); a contract without (fresh) OI "
                             "contributes 0 (no OI reported) — OI coverage and the per-contract OI age shown",
            "implied_q_flags": f"an expiry whose implied |q| > {cfg.q_flag_abs_pct:g} % or whose pair MAD > "
                               f"{cfg.q_flag_mad_pct:g} % is flagged and shown, still used (not excluded)",
            "evaluability": f"the regime is not evaluable when the excluded expiries carry > {cfg.max_excluded_oi_pct:g} "
                            "% of the total (fresh) OI (user decision 2026-11-06 b)",
            "expiry_time": "16:00 New York on the expiration date (PM settlement); ACT/365 calendar time",
            "gex_convention": "USD per 1 % move of the underlying: OI × gamma × multiplier × spot² × 0.01",
            "dealer_variants": "A calls + / puts − (pre-registration baseline); B all −; C OTM − / ITM + "
                               "(moneyness at the snapshot spot)"}


def assumptions_hash(cfg: Config, acfg) -> str:
    text = json.dumps(assumptions(cfg, acfg), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def provisional(cfg: Config, acfg, confirmed: Optional[dict] = None) -> Tuple[bool, list]:
    """(provisional?, the keys that make it so) — archive keys first; an OS2 key is confirmed only while its effective
    value equals the confirmed one."""
    from strategy_platform.options_archive import config as oconfig
    keys = [f"archive.{k}" for k in oconfig.key_status(acfg)["provisional_keys"]]
    vals = (confirmed or {}).get("values", {})
    keys += [k for k in PROVISIONAL_KEYS if k not in vals or vals[k] != getattr(cfg, k)]
    return (bool(acfg.provisional) or bool(keys)), keys
