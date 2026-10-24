"""The seam between the two daily equity sources (SPEC §8.1/§8.2).

User decisions 2026-10-03/-06: Tiingo serves daily equity/ETF prices and
volume BEFORE the seam, Alpaca from the seam on — SIP history (data type
``equity_bars_history``, source label ``alpaca_sip``; the free plan's IEX
history starts 2020-07 for most ETFs) — two sources, never for the same
period and never as a substitute for each other. Before Tiingo rows are used for a
symbol, the two sources must agree over an overlap window; otherwise that
symbol has NO pre-seam data and the caller gets a structured warning.

All thresholds live here (``SEAM_DATE`` … ``MIN_OVERLAP_RETURNS``):

- Seam = 2016-01-04, the first NYSE session of 2016. Overlap = the first
  ``OVERLAP_SESSIONS`` (60) NYSE sessions from the seam. Tiingo is fetched
  for the overlap ONLY for this check: its overlap closes/volumes are kept in
  the ``meta`` record of the symbol, never as bars (no Tiingo bar >= seam is
  ever stored).
- Per symbol and adjustment series (``split``, ``all``): daily returns on the
  common overlap days must agree — |r_tiingo − r_alpaca| <= RETURN_TOLERANCE
  (0.002) on >= MIN_AGREE_SHARE (95 %) of the returns and no single return
  difference > MAX_DAY_DIFF (0.02); at least MIN_OVERLAP_RETURNS (50) common
  returns. Then factor = median(alpaca_close / tiingo_close) over the common
  days, and the ratio's relative spread (max − min) / factor must be
  <= MAX_RATIO_SPREAD (0.5 %).
- Pass: every Tiingo price before the seam is multiplied by the factor and
  every Tiingo volume divided by it (both series). Rule: the factor is the
  basis difference between the sources — for ``split`` a later share split
  (Tiingo's split series is on the share basis of the overlap end, Alpaca's
  on today's), for ``all`` dividend/split adjustments made after the Tiingo
  fetch or a different adjustment method. Dividing volume by the price factor
  keeps dollar volume (price × volume) unchanged across the seam, which is
  what the participation limit uses.
- Volume (``split`` series, user decision 2026-10-06): both sources report
  consolidated volume; per overlap day r = sip_volume / (tiingo_volume /
  factor) must satisfy |r − 1| <= VOLUME_DAY_TOLERANCE (25 %) on >=
  MIN_VOLUME_AGREE_SHARE (90 %) of the days and |median r − 1| <=
  MAX_VOLUME_MEDIAN_DEVIATION (10 %) (stats ``volume``). ``volume()`` —
  data type ``equity_volume`` — uses Tiingo volume before the seam only when
  this passes (else warning ``seam_volume_mismatch``; prices are not
  affected).
- Fail -> no pre-seam rows for that symbol/series and the warning
  ``seam_mismatch`` with the numbers. Every check result is stored in
  ``seam_checks`` (a new row whenever the result changes); the check is
  re-run against the CURRENT Alpaca overlap at every call, so a later
  dividend/split re-basing Alpaca's history updates the factor.

Public API (used by the backtest engine, stage 3c):
- ``history(symbol, adjustment, start, end=None, *, progress=None) -> (rows, info)``
- ``corporate_actions(symbol, start, end=None, *, progress=None) -> (rows, info)``
- ``volume(symbol, start, end=None, *, progress=None) -> (rows, info)`` (SPEC §8.8)
- ``check_seam(tiingo, alpaca) -> {ok, factor, stats}`` (pure)
Without a Tiingo key a start before the seam raises ``SecretMissing``.
Tiingo data is fetched on demand, once per symbol (no bulk preload); the
``progress`` callback receives the rate-limit wait events of the HTTP gate.

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

import json
import logging
import statistics
import threading
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

from ..core import calendars
from ..core.errors import DataSourceUnavailable, InvalidRequest
from ..core.instruments import instrument
from .licences import licence_for
from .providers import tiingo as tiingo_mod
from .providers.base import iso, parse_ts

logger = logging.getLogger(__name__)

# --- constants (SPEC §8.2) — the ONE place for the seam thresholds -------------------------------
SEAM_DATE = date(2016, 1, 4)              # first NYSE session of 2016 = first Alpaca (SIP) day used
OVERLAP_SESSIONS = 60
RETURN_TOLERANCE = 0.002
MIN_AGREE_SHARE = 0.95
MAX_DAY_DIFF = 0.02
MAX_RATIO_SPREAD = 0.005
MIN_OVERLAP_RETURNS = 50
EARLIEST_START = date(2000, 1, 3)         # first session of 2000 (SPEC §8.5 lower bound)
ADJUSTMENTS = ("all", "split")
TIMEFRAME = "1Day"
PRE_SEAM_DATA_TYPE = tiingo_mod.DATA_TYPE
HISTORY_DATA_TYPE = "equity_bars_history"   # Alpaca SIP daily bars from the seam on (user, 2026-10-06)
HISTORY_SOURCE = "alpaca_sip"
SHORT_HISTORY_SLACK = timedelta(days=7)
# volume part of the seam check (``split`` series; user decision 2026-10-06): per overlap day
# r = alpaca_sip_volume / (tiingo_volume / factor); |r - 1| <= VOLUME_DAY_TOLERANCE on >= MIN_VOLUME_AGREE_SHARE
# of the common days and |median(r) - 1| <= MAX_VOLUME_MEDIAN_DEVIATION (both are consolidated volumes;
# vendors count some trade conditions differently, hence the wider tolerance than for prices)
VOLUME_DAY_TOLERANCE = 0.25
MIN_VOLUME_AGREE_SHARE = 0.90
MAX_VOLUME_MEDIAN_DEVIATION = 0.10
META_TIINGO = "tiingo_history/{symbol}"
META_ALPACA_ACTIONS = "alpaca_corporate_actions/{symbol}"


def overlap_dates() -> List[date]:
    """The first OVERLAP_SESSIONS NYSE sessions from the seam."""
    days = calendars.trading_days(SEAM_DATE, SEAM_DATE + timedelta(days=OVERLAP_SESSIONS * 2 + 30))
    return days[:OVERLAP_SESSIONS]


def overlap_end() -> date:
    return overlap_dates()[-1]


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return parse_ts(value).astimezone(calendars.NY).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if len(text) > 10:
        return parse_ts(text).astimezone(calendars.NY).date()
    return date.fromisoformat(text)


def _day_start(d: date) -> datetime:
    return datetime.combine(d, time(0, 0), tzinfo=timezone.utc)


def _day_end(d: date) -> datetime:
    return datetime.combine(d, time(23, 59, 59), tzinfo=timezone.utc)


def bar_date(row: dict) -> date:
    """NY trading date of a daily equity bar (ts = NY midnight in UTC)."""
    return parse_ts(row["ts"]).astimezone(calendars.NY).date()


# --- the check (pure) ----------------------------------------------------------------------------

def check_seam(tiingo: Dict[str, float], alpaca: Dict[str, float], *,
               tiingo_volume: Optional[Dict[str, float]] = None,
               alpaca_volume: Optional[Dict[str, float]] = None) -> dict:
    """``tiingo`` / ``alpaca``: {YYYY-MM-DD: close} over the overlap.
    Returns ``{ok, factor, stats}`` (factor None when no common day exists)."""
    window = [d.isoformat() for d in overlap_dates()]
    common = [d for d in window if d in tiingo and d in alpaca]
    stats: dict = {
        "overlap_start": window[0], "overlap_end": window[-1], "overlap_sessions": len(window),
        "common_days": len(common), "missing_tiingo": sum(1 for d in window if d not in tiingo),
        "missing_alpaca": sum(1 for d in window if d not in alpaca),
        "limits": {"return_tolerance": RETURN_TOLERANCE, "min_agree_share": MIN_AGREE_SHARE,
                   "max_day_diff": MAX_DAY_DIFF, "max_ratio_spread": MAX_RATIO_SPREAD,
                   "min_overlap_returns": MIN_OVERLAP_RETURNS},
        "failed": [],
    }
    factor = None
    if common:
        ratios = [alpaca[d] / tiingo[d] for d in common]
        factor = statistics.median(ratios)
        stats.update(ratio_min=min(ratios), ratio_max=max(ratios),
                     ratio_spread=(max(ratios) - min(ratios)) / factor)
    diffs = []
    for a, b in zip(common, common[1:]):
        rt = tiingo[b] / tiingo[a] - 1.0
        ra = alpaca[b] / alpaca[a] - 1.0
        diffs.append((abs(rt - ra), b))
    stats["returns"] = len(diffs)
    if diffs:
        agree = sum(1 for d, _ in diffs if d <= RETURN_TOLERANCE) / len(diffs)
        worst = max(diffs)
        stats.update(agree_share=agree, max_abs_return_diff=worst[0], worst_day=worst[1])
    if len(diffs) < MIN_OVERLAP_RETURNS:
        stats["failed"].append(f"only {len(diffs)} common daily returns in the overlap "
                               f"(at least {MIN_OVERLAP_RETURNS} needed)")
    else:
        if stats["agree_share"] < MIN_AGREE_SHARE:
            stats["failed"].append(f"returns agree within {RETURN_TOLERANCE:g} on {stats['agree_share']:.1%} of days "
                                   f"(at least {MIN_AGREE_SHARE:.0%} needed)")
        if stats["max_abs_return_diff"] > MAX_DAY_DIFF:
            stats["failed"].append(f"return difference of {stats['max_abs_return_diff']:.4f} on {stats['worst_day']} "
                                   f"(at most {MAX_DAY_DIFF:g} allowed)")
        if stats["ratio_spread"] > MAX_RATIO_SPREAD:
            stats["failed"].append(f"price ratio spread {stats['ratio_spread']:.3%} "
                                   f"(at most {MAX_RATIO_SPREAD:.1%} allowed)")
    vstats: dict = {"ok": False, "failed": [], "limits": {
        "day_tolerance": VOLUME_DAY_TOLERANCE, "min_agree_share": MIN_VOLUME_AGREE_SHARE,
        "max_median_deviation": MAX_VOLUME_MEDIAN_DEVIATION, "min_days": MIN_OVERLAP_RETURNS}}
    vr = []
    if tiingo_volume and alpaca_volume and factor:
        vr = [alpaca_volume[d] / (tiingo_volume[d] / factor) for d in common
              if tiingo_volume.get(d) and alpaca_volume.get(d) is not None]
    vstats["days"] = len(vr)
    if len(vr) < MIN_OVERLAP_RETURNS:
        vstats["failed"].append(f"only {len(vr)} overlap days with volume in both sources "
                                f"(at least {MIN_OVERLAP_RETURNS} needed)")
    else:
        med = statistics.median(vr)
        share = sum(1 for r in vr if abs(r - 1.0) <= VOLUME_DAY_TOLERANCE) / len(vr)
        vstats.update(ratio_median=med, agree_share=share, ratio_min=min(vr), ratio_max=max(vr))
        if abs(med - 1.0) > MAX_VOLUME_MEDIAN_DEVIATION:
            vstats["failed"].append(f"median volume ratio Alpaca SIP / Tiingo {med:.3f} "
                                    f"(must lie within ±{MAX_VOLUME_MEDIAN_DEVIATION:.0%} of 1)")
        if share < MIN_VOLUME_AGREE_SHARE:
            vstats["failed"].append(f"daily volumes agree within ±{VOLUME_DAY_TOLERANCE:.0%} on {share:.1%} of days "
                                    f"(at least {MIN_VOLUME_AGREE_SHARE:.0%} needed)")
    vstats["ok"] = not vstats["failed"]
    stats["volume"] = vstats
    return {"ok": not stats["failed"], "factor": factor, "stats": stats}


def _fingerprint(result: dict) -> str:
    return json.dumps({"ok": result["ok"], "factor": result["factor"], "stats": result["stats"]},
                      sort_keys=True, default=str)


def scale_rows(rows: List[dict], factor: float) -> List[dict]:
    """Prices × factor, volume ÷ factor (rule in the module docstring)."""
    out = []
    for r in rows:
        r = dict(r)
        for f in ("open", "high", "low", "close"):
            r[f] = float(r[f]) * factor
        if r.get("volume") is not None:
            r["volume"] = float(r["volume"]) / factor
        r["vwap"] = None
        out.append(r)
    return out


# --- service -------------------------------------------------------------------------------------

def _now_fn(clock):
    if clock is None:
        return lambda: datetime.now(timezone.utc)
    return clock.now_utc if hasattr(clock, "now_utc") else clock


class Seam:
    def __init__(self, store=None, cache=None, fetch=None, secrets_module=None, clock=None):
        self._store = store
        self._cache = cache
        self._fetch_fn = fetch
        self._secrets = secrets_module
        self._now = _now_fn(clock)
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()

    # dependencies
    def _db(self):
        if self._store is not None:
            return self._store
        from ..storage import db
        return db

    def _bars(self):
        if self._cache is not None:
            return self._cache
        from . import cache
        return cache.default_cache()

    def _fetch(self, data_type, op, *args, **kw):
        if self._fetch_fn is not None:
            return self._fetch_fn(data_type, op, *args, **kw)
        from . import registry
        return registry.fetch(data_type, op, *args, **kw)

    def _lock(self, key: str) -> threading.Lock:
        with self._locks_lock:
            return self._locks.setdefault(key, threading.Lock())

    def _today(self) -> date:
        return parse_ts(self._now()).astimezone(calendars.NY).date()

    # --- Tiingo history (on demand, once per symbol) ---------------------------------------------

    def _tiingo_lock(self, symbol: str) -> threading.Lock:
        return self._lock("tiingo/" + symbol)

    def tiingo_record(self, symbol: str, progress=None) -> dict:
        """The stored pre-seam Tiingo record of ``symbol``; fetched + stored first if absent
        (ONE request EARLIEST_START … overlap end — Tiingo is only used before the seam)."""
        key = META_TIINGO.format(symbol=symbol)
        with self._tiingo_lock(symbol):
            text = self._db().meta_get(key)
            if text is None:
                self._fetch_tiingo(symbol, progress)
                text = self._db().meta_get(key)
            return json.loads(text)

    def _fetch_tiingo(self, symbol: str, progress) -> None:
        end = overlap_end()
        if progress is not None:
            progress({"source": "tiingo", "waiting": False,
                      "message": f"fetching Tiingo daily history of {symbol} ({EARLIEST_START} – {end})"})
        fetched_at = iso(self._now())
        try:
            parsed = self._fetch(PRE_SEAM_DATA_TYPE, "daily_prices", symbol, EARLIEST_START, end, progress=progress)
        except tiingo_mod.UnknownSymbol:
            self._db().meta_set(META_TIINGO.format(symbol=symbol), json.dumps({
                "symbol": symbol, "fetched_at": fetched_at, "status": "not_found", "pre_seam_rows": 0,
                "first_date": None, "last_pre_seam_date": None, "overlap": {"split": [], "all": []},
                "overlap_splits": []}, sort_keys=True))
            return
        self._ingest_pre_seam(symbol, [r for r in parsed if r["date"] <= end], fetched_at)

    def _ingest_pre_seam(self, symbol: str, parsed: List[dict], fetched_at: str) -> None:
        series = tiingo_mod.build_series(symbol, parsed, fetched_at)
        pre = {adj: [b for b in series[adj] if bar_date(b) < SEAM_DATE] for adj in ADJUSTMENTS}
        actions = [a for a in series["dividends"] + series["splits"] if date.fromisoformat(a["ex_date"]) < SEAM_DATE]
        self._store_pre_seam(symbol, pre["split"] + pre["all"], actions)
        overlap = {adj: [[bar_date(b).isoformat(), b["close"], b["volume"]] for b in series[adj]
                         if bar_date(b) >= SEAM_DATE] for adj in ADJUSTMENTS}
        record = {"symbol": symbol, "fetched_at": fetched_at, "status": "ok", "pre_seam_rows": len(pre["all"]),
                  "first_date": bar_date(pre["all"][0]).isoformat() if pre["all"] else None,
                  "last_pre_seam_date": bar_date(pre["all"][-1]).isoformat() if pre["all"] else None,
                  "overlap": overlap,
                  "overlap_splits": [[a["ex_date"], a["split_ratio"]] for a in series["splits"]
                                     if date.fromisoformat(a["ex_date"]) >= SEAM_DATE]}
        self._db().meta_set(META_TIINGO.format(symbol=symbol), json.dumps(record, sort_keys=True))  # last = complete

    def volume(self, symbol: str, start, end=None, *,
               progress: Optional[Callable[[dict], None]] = None) -> Tuple[List[dict], dict]:
        """Consolidated daily volume of an equity/ETF (data type ``equity_volume``,
        user decision 2026-10-06): Alpaca SIP (``split`` series of
        ``history``) from the seam on, Tiingo before it — only when the seam
        check of the ``split`` series passes for prices AND volume (stats
        ``volume``; else warning ``seam_volume_mismatch`` and no pre-seam
        volume). Rows ``{date, volume, close_split, dollar_volume_split,
        known_at, source}`` (``volume`` in shares on the split basis of the
        ``split`` price series, ``dollar_volume_split`` = volume × split close
        = raw dollar volume). Returns (rows oldest first, info = {symbol,
        start, end, source, sources, ranges, seam, warnings, licences}).
        Crypto -> ValueError (its volume stays in crypto_bars)."""
        inst = instrument(symbol)
        if inst.asset_class != "equity":
            raise ValueError(f"{inst.symbol}: equity_volume covers equities/ETFs; crypto volume comes from crypto_bars")
        bars, hinfo = self.history(inst.symbol, "split", start, end, progress=progress)
        warnings = [w for w in hinfo["warnings"]]
        seam = hinfo.get("seam")
        pre_ok = True
        if any(b["source"] == tiingo_mod.SOURCE for b in bars):
            vstats = (seam or {}).get("stats", {}).get("volume") or {}
            if not vstats.get("ok"):
                pre_ok = False
                warnings.append({"code": "seam_volume_mismatch", "symbol": inst.symbol, "stats": vstats,
                                 "message": (f"{inst.symbol}: Tiingo and Alpaca SIP volumes disagree over the "
                                             f"overlap: " + "; ".join(vstats.get("failed") or ["no volume check"])
                                             + f" — no volume before {SEAM_DATE.isoformat()} is used (orders "
                                             "before it are refused for lack of volume).")})
        rows = []
        for b in bars:
            if b.get("volume") is None or (b["source"] == tiingo_mod.SOURCE and not pre_ok):
                continue
            rows.append({"date": bar_date(b).isoformat(), "volume": float(b["volume"]),
                         "close_split": float(b["close"]), "dollar_volume_split": float(b["volume"]) * float(b["close"]),
                         "known_at": b["known_at"], "source": b["source"]})
        sources = []
        for r in rows:
            if r["source"] not in sources:
                sources.append(r["source"])
        info = {"symbol": inst.symbol, "start": hinfo["start"], "end": hinfo["end"],
                "source": "+".join(sources) or HISTORY_SOURCE, "sources": sources,
                "ranges": hinfo["ranges"], "seam": seam, "warnings": warnings,
                "licences": {s: licence_for(s)["licence"] for s in sources},
                "first": rows[0]["date"] if rows else None, "last": rows[-1]["date"] if rows else None,
                "rows": len(rows)}
        return rows, info

    def _store_pre_seam(self, symbol: str, bars: List[dict], actions: List[dict]) -> None:
        """The ONLY write path for Tiingo data: refuses anything dated on/after the seam."""
        late = [b["ts"] for b in bars if bar_date(b) >= SEAM_DATE] + \
            [a["ex_date"] for a in actions if date.fromisoformat(a["ex_date"]) >= SEAM_DATE]
        if late or any(b["source"] != tiingo_mod.SOURCE for b in bars):
            raise ValueError(f"refusing to store Tiingo rows on/after the seam {SEAM_DATE}: {late[:3]}")
        db = self._db()
        for i in range(0, len(bars), 10000):
            db.upsert_bars(bars[i:i + 10000])
        db.replace_corporate_actions(symbol, tiingo_mod.SOURCE, None, SEAM_DATE - timedelta(days=1), actions)

    # --- seam check ------------------------------------------------------------------------------

    def check(self, symbol: str, adjustment: str, record: dict) -> dict:
        """Runs the check against the current Alpaca overlap; stores it when the result changed."""
        alpaca_rows = self._bars().get_bars(symbol, TIMEFRAME, _day_start(SEAM_DATE), _day_end(overlap_end()),
                                            adjustment=adjustment, data_type=HISTORY_DATA_TYPE)
        a_close = {bar_date(r).isoformat(): float(r["close"]) for r in alpaca_rows}
        a_vol = {bar_date(r).isoformat(): r["volume"] for r in alpaca_rows if r.get("volume") is not None}
        t = record.get("overlap", {}).get(adjustment) or []
        result = check_seam({d: float(c) for d, c, _v in t}, a_close,
                            tiingo_volume={d: v for d, _c, v in t if v is not None}, alpaca_volume=a_vol)
        db = self._db()
        latest = db.seam_check_latest(symbol, adjustment)
        if latest is None or _fingerprint(latest) != _fingerprint(result):
            latest = db.seam_check_add(symbol, adjustment, result["ok"], result["factor"], result["stats"],
                                       checked_at=iso(self._now()))
        return {"ok": latest["ok"], "factor": latest["factor"], "stats": latest["stats"],
                "checked_at": latest["checked_at"]}

    # --- public ------------------------------------------------------------------------------------

    def _pre_seam_state(self, symbol: str, adjustment: str, start: date, progress, warnings: list):
        """(record, seam result or None, usable: bool) for a range starting before the seam."""
        tiingo_mod.require_key(self._secrets)
        if start < EARLIEST_START:
            raise InvalidRequest(f"Daily history before {EARLIEST_START.isoformat()} is not offered — "
                                 "choose a later start.")
        record = self.tiingo_record(symbol, progress)
        if record["status"] == "not_found" or not record.get("pre_seam_rows"):
            why = "Tiingo does not know the ticker" if record["status"] == "not_found" else \
                f"Tiingo has no prices before {SEAM_DATE.isoformat()}"
            warnings.append({"code": "no_pre_seam_data", "symbol": symbol,
                             "message": f"{symbol}: no data before {SEAM_DATE.isoformat()} ({why})."})
            return record, None, False
        result = self.check(symbol, adjustment, record)
        if not result["ok"]:
            st = result["stats"]
            warnings.append({"code": "seam_mismatch", "symbol": symbol, "adjustment": adjustment,
                             "factor": result["factor"], "stats": st,
                             "message": (f"{symbol} ({adjustment}): Tiingo and Alpaca disagree over the overlap "
                                         f"{st['overlap_start']} – {st['overlap_end']}: " + "; ".join(st["failed"])
                                         + f" — no data before {SEAM_DATE.isoformat()} is used.")})
            return record, result, False
        return record, result, True

    def history(self, symbol: str, adjustment: str, start, end=None, *,
                progress: Optional[Callable[[dict], None]] = None) -> Tuple[List[dict], dict]:
        """Daily bars of ``symbol`` (``adjustment`` = "all" | "split") for
        start <= date <= end (NY trading dates; end None = today): Alpaca rows
        from the seam on and, when start < seam and the seam check passes,
        factor-scaled Tiingo rows before it (``source`` tells which). Returns
        (rows oldest first, info = {symbol, adjustment, start, end, sources,
        ranges: {source: {first, last, rows}}, seam: {ok, factor, stats,
        checked_at} | None, warnings: [{code, symbol, message, …}],
        licences: {source: terms}})."""
        if adjustment not in ADJUSTMENTS:
            raise ValueError(f"adjustment must be one of {', '.join(ADJUSTMENTS)}")
        inst = instrument(symbol)
        sym = inst.symbol
        lo = _as_date(start)
        hi = _as_date(end) if end is not None else self._today()
        if lo > hi:
            raise ValueError("start must not lie after end")
        warnings: List[dict] = []
        info = {"symbol": sym, "adjustment": adjustment, "start": lo.isoformat(), "end": hi.isoformat(),
                "sources": [], "ranges": {}, "seam": None, "warnings": warnings, "licences": {}}
        if inst.asset_class != "equity":
            # crypto has no splits/dividends: its one series serves both adjustments; no seam
            rows = self._bars().get_bars(sym, TIMEFRAME, _day_start(lo), _day_end(hi))
            self._describe(info, "alpaca", rows)
            return rows, info
        tiingo_rows: List[dict] = []
        if lo < SEAM_DATE:
            record, result, usable = self._pre_seam_state(sym, adjustment, lo, progress, warnings)
            if result is not None:
                info["seam"] = result
            if usable:
                stored = self._db().query_bars(sym, TIMEFRAME, _day_start(lo),
                                               _day_start(SEAM_DATE) - timedelta(seconds=1),
                                               tiingo_mod.SOURCE, adjustment=adjustment)
                tiingo_rows = scale_rows([r for r in stored if lo <= bar_date(r) <= hi], result["factor"])
                if record.get("first_date") and date.fromisoformat(record["first_date"]) - lo > SHORT_HISTORY_SLACK:
                    warnings.append({"code": "short_history", "symbol": sym,
                                     "message": f"{sym}: data starts {record['first_date']} (requested "
                                                f"{lo.isoformat()})."})
        alpaca_rows: List[dict] = []
        if hi >= SEAM_DATE:
            fetched = self._bars().get_bars(sym, TIMEFRAME, _day_start(max(lo, SEAM_DATE)), _day_end(hi),
                                            adjustment=adjustment, data_type=HISTORY_DATA_TYPE)
            alpaca_rows = [r for r in fetched if SEAM_DATE <= bar_date(r) <= hi]
            want = max(lo, SEAM_DATE)
            if not tiingo_rows and alpaca_rows and bar_date(alpaca_rows[0]) - want > SHORT_HISTORY_SLACK \
                    and not any(w["code"] == "short_history" for w in warnings):
                warnings.append({"code": "short_history", "symbol": sym, "first": bar_date(alpaca_rows[0]).isoformat(),
                                 "message": f"{sym}: data starts {bar_date(alpaca_rows[0]).isoformat()} (requested "
                                            f"{lo.isoformat()})."})
        self._describe(info, tiingo_mod.SOURCE, tiingo_rows)
        self._describe(info, HISTORY_SOURCE, alpaca_rows)
        return tiingo_rows + alpaca_rows, info

    @staticmethod
    def _describe(info: dict, source: str, rows: List[dict]) -> None:
        if not rows:
            return
        info["sources"].append(source)
        info["ranges"][source] = {"first": bar_date(rows[0]).isoformat(), "last": bar_date(rows[-1]).isoformat(),
                                  "rows": len(rows)}
        info["licences"][source] = licence_for(source)["licence"]

    # --- corporate actions ---------------------------------------------------------------------

    def _alpaca_actions(self, symbol: str) -> None:
        """Stores Alpaca's dividends/splits from the seam through today (refreshed once per NY day)."""
        key = META_ALPACA_ACTIONS.format(symbol=symbol)
        today = self._today()
        with self._lock(key):
            text = self._db().meta_get(key)
            if text is not None and json.loads(text).get("through", "") >= today.isoformat():
                return
            raw = self._fetch("corporate_actions", "corporate_actions", [symbol], SEAM_DATE, today)
            fetched_at = iso(self._now())
            rows = []
            for a in raw or []:
                if a.get("symbol") != symbol:
                    continue
                ex = date.fromisoformat(str(a["ex_date"])[:10])
                if ex < SEAM_DATE or ex > today:
                    continue
                rows.append(alpaca_action_row(a, fetched_at))
            self._db().replace_corporate_actions(symbol, "alpaca", SEAM_DATE, today, rows)
            self._db().meta_set(key, json.dumps({"through": today.isoformat(), "fetched_at": fetched_at}))

    def corporate_actions(self, symbol: str, start, end=None, *,
                          progress: Optional[Callable[[dict], None]] = None) -> Tuple[List[dict], dict]:
        """Dividends and splits with start <= ex_date <= end: Tiingo before the
        seam (only when the ``split`` seam check passes), Alpaca from the seam
        on — never both for one period. Rows = storage rows ({symbol, kind,
        ex_date, cash_amount, split_ratio, pay_date, source, known_at,
        fetched_at}) + ``cash_amount_split_adj``: the dividend per share on the
        share basis of the ``split`` price series (raw cash ÷ the later splits,
        Tiingo also × the seam factor) — what a position held in ``split``
        series shares receives; None for splits. Prices of that series are
        already split-adjusted: splits must NOT be applied to positions again.
        Returns (rows oldest first, info = {symbol, start, end, sources, seam,
        warnings, licences})."""
        inst = instrument(symbol)
        sym = inst.symbol
        lo = _as_date(start)
        hi = _as_date(end) if end is not None else self._today()
        if lo > hi:
            raise ValueError("start must not lie after end")
        warnings: List[dict] = []
        info = {"symbol": sym, "start": lo.isoformat(), "end": hi.isoformat(), "sources": [], "seam": None,
                "warnings": warnings, "licences": {}}
        if inst.asset_class != "equity":
            return [], info
        out: List[dict] = []
        db = self._db()
        if lo < SEAM_DATE:
            record, result, usable = self._pre_seam_state(sym, "split", lo, progress, warnings)
            info["seam"] = result
            if usable:
                stored = db.query_corporate_actions(sym, tiingo_mod.SOURCE, lo, min(hi, SEAM_DATE - timedelta(days=1)))
                splits = [(date.fromisoformat(a["ex_date"]), a["split_ratio"])
                          for a in db.query_corporate_actions(sym, tiingo_mod.SOURCE, None, None)
                          if a["kind"] != "cash_dividend"]
                splits += [(date.fromisoformat(d), r) for d, r in record.get("overlap_splits") or []]
                for a in stored:
                    out.append(_with_adj(a, splits, result["factor"]))
                if stored:
                    info["sources"].append(tiingo_mod.SOURCE)
                    info["licences"][tiingo_mod.SOURCE] = licence_for(tiingo_mod.SOURCE)["licence"]
        if hi >= SEAM_DATE:
            self._alpaca_actions(sym)
            stored = db.query_corporate_actions(sym, "alpaca", max(lo, SEAM_DATE), hi)
            splits = [(date.fromisoformat(a["ex_date"]), a["split_ratio"])
                      for a in db.query_corporate_actions(sym, "alpaca", SEAM_DATE, None)
                      if a["kind"] != "cash_dividend"]
            out.extend(_with_adj(a, splits, 1.0) for a in stored)
            info["sources"].append("alpaca")
            info["licences"]["alpaca"] = licence_for("alpaca")["licence"]
        out.sort(key=lambda a: (a["ex_date"], a["kind"]))
        return out, info


def _with_adj(action: dict, splits: List[Tuple[date, float]], factor: float) -> dict:
    a = dict(action)
    if a["kind"] == "cash_dividend":
        ex = date.fromisoformat(a["ex_date"])
        later = 1.0
        for d, r in splits:
            if d > ex:
                later *= float(r)
        a["cash_amount_split_adj"] = float(a["cash_amount"]) / later * factor
    else:
        a["cash_amount_split_adj"] = None
    return a


def alpaca_action_row(a: dict, fetched_at: str) -> dict:
    """Alpaca provider corporate action -> corporate_actions row (validated, no defaults)."""
    from . import pit
    kind = a["kind"]
    ex = str(a["ex_date"])[:10]
    if kind == "cash_dividend":
        if a.get("rate") is None:
            raise DataSourceUnavailable("alpaca", "corporate_actions", f"dividend of {a.get('symbol')} on {ex} "
                                        "has no cash rate")
        cash, ratio = float(a["rate"]), None
    else:
        old, new = a.get("old_rate"), a.get("new_rate")
        if not old or not new or old <= 0 or new <= 0:
            raise DataSourceUnavailable("alpaca", "corporate_actions", f"split of {a.get('symbol')} on {ex} "
                                        "has no valid old/new rate")
        cash, ratio = None, float(new) / float(old)
    return {"symbol": a["symbol"], "kind": kind, "ex_date": ex, "cash_amount": cash, "split_ratio": ratio,
            "pay_date": a.get("payable_date"), "source": "alpaca", "known_at": pit.action_known_at_iso(ex),
            "fetched_at": fetched_at}


_default: Optional[Seam] = None
_default_lock = threading.Lock()


def default() -> Seam:
    global _default
    with _default_lock:
        if _default is None:
            _default = Seam()
        return _default


def set_default(seam: Optional[Seam]) -> None:
    global _default
    with _default_lock:
        _default = seam


def history(symbol: str, adjustment: str, start, end=None, *, progress=None) -> Tuple[List[dict], dict]:
    return default().history(symbol, adjustment, start, end, progress=progress)


def corporate_actions(symbol: str, start, end=None, *, progress=None) -> Tuple[List[dict], dict]:
    return default().corporate_actions(symbol, start, end, progress=progress)


def volume(symbol: str, start, end=None, *, progress=None) -> Tuple[List[dict], dict]:
    return default().volume(symbol, start, end, progress=progress)
