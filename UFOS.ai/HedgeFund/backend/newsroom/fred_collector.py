"""FRED (Federal Reserve Economic Data) — the Financial Newsroom's
"Macro Indicators" section.

Free, but requires a personal API key (register at
https://fred.stlouisfed.org/docs/api/api_key.html) — set FRED_API_KEY in
.env or this collector stays disabled.

Moved from Global Monitoring on 2026-09-24. There it only showed the latest
value of four series as a text ticker next to the globe (FRED is a national
aggregate, not a point on a map). In the newsroom each series carries what a
trading desk actually reads it for: latest value, previous value, change,
the last HISTORY_POINTS observations (for a sparkline) and a one-line note
on how to read it. Two rate series were added (2-year yield, 10Y-2Y spread)
and CPI is requested as year-over-year % change (FRED's units=pc1) instead
of the raw index level, which nobody reads directly.

Each poll emits one event per series with the stable id "macro-<SERIES>"
(main.py keeps only the latest per id).

2026-09-25: observations are kept in the local SQLite store (newsroom/db.py)
and fetched incrementally (see "Incremental fetching" below); the regime event
also carries the extended view (US trail since 2000, recessions, components,
euro-area point from Eurostat, phase behaviour).
"""
import asyncio
import json
import logging
import time
from datetime import date, timedelta

import aiohttp

from config import FRED_API_KEY, FRED_POLL_SECONDS
from .store import make_event
from . import macro_derived
from . import db as store_db
from .macro_rules import stability_signal, describe_rules

logger = logging.getLogger(__name__)

OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"
HISTORY_POINTS = 24
FRED_CONCURRENCY = 4  # FRED allows 120 requests/minute; 4 in parallel is gentle

# (series_id, label, unit, FRED "units" transformation or None, how to read it)
SERIES = [
    ("FEDFUNDS", "Effective Federal Funds Rate", "%", None,
     "The Fed's policy rate in practice; sets the floor for short-term borrowing costs. Monthly."),
    ("DGS2", "2-Year Treasury Yield", "%", None,
     "Most sensitive to expected Fed policy over the next two years. Daily."),
    ("DGS10", "10-Year Treasury Yield", "%", None,
     "Benchmark for mortgages and equity valuations; rising yields pressure growth stocks. Daily."),
    ("T10Y2Y", "10Y – 2Y Treasury Spread", "pp", None,
     "Yield-curve slope. Below 0 = inverted curve, historically a recession warning. Daily."),
    ("T10Y3M", "10Y – 3M Treasury Spread", "pp", None,
     "The curve measure behind the New York Fed's recession-probability model; often more reliable "
     "than 10Y–2Y. Below 0 = inverted. Daily."),
    ("CPIAUCSL", "CPI Inflation (YoY)", "%", "pc1",
     "Consumer prices vs. a year earlier (Fed target: 2% on its preferred PCE measure). Monthly."),
    ("PCEPILFE", "Core PCE Inflation (YoY)", "%", "pc1",
     "The Fed's preferred inflation gauge (excl. food and energy) — the 2% target refers to PCE. "
     "Published with a ~1-month lag. Monthly."),
    ("UNRATE", "Unemployment Rate", "%", None,
     "Share of the labour force without a job; the Fed's other mandate. Monthly."),
    ("ICSA", "Initial Jobless Claims", "K", None,
     "New unemployment-benefit claims per week, in thousands. Early warning: turns months before the "
     "unemployment rate does. Weekly."),
    ("BAMLH0A0HYM2", "High-Yield Credit Spread", "%", None,
     "Extra yield investors demand on junk bonds over Treasuries (ICE BofA OAS). Widening = credit "
     "stress, usually ahead of equity trouble. Daily."),
    ("VIXCLS", "VIX (Equity Volatility)", "pts", None,
     "Expected 30-day S&P 500 volatility — the market's 'fear gauge'. ~12–20 calm, 30+ stressed. Daily."),
    # --- Euro area (2026-09-25) ---
    ("ECBDFR", "ECB Deposit Facility Rate", "%", None,
     "The ECB's key policy rate since 2019 — what euro-area banks earn overnight. Daily."),
    ("IRLTLT01DEM156N", "10-Year German Bund Yield", "%", None,
     "Benchmark long rate for the euro area (OECD monthly average). Monthly."),
    ("CP0000EZ19M086NEST", "Euro Area HICP Inflation (YoY)", "%", "pc1",
     "Harmonised consumer prices vs. a year earlier — the ECB's target measure (2%). Monthly."),
]

# Card groups in the view: "us" (default) and "europe".
GROUP = {"ECBDFR": "europe", "IRLTLT01DEM156N": "europe", "CP0000EZ19M086NEST": "europe"}
# Daily series have no meaningful "next release" date (they update every business day).
DAILY_SERIES = {"DGS2", "DGS10", "T10Y2Y", "T10Y3M", "BAMLH0A0HYM2", "VIXCLS", "ECBDFR"}

# Display scaling (FRED unit -> card unit): ICSA is reported in persons.
SCALE = {"ICSA": 0.001}


# Monthly series are fetched with a longer history: the derived indicators
# (macro_derived.py — Sahm rule needs 12+3 months of look-back) use it; the
# cards still show the last HISTORY_POINTS.
MONTHLY_SERIES = {"FEDFUNDS", "CPIAUCSL", "PCEPILFE", "UNRATE", "IRLTLT01DEM156N", "CP0000EZ19M086NEST"}
MONTHLY_FETCH_POINTS = 48
CURVE_MONTHLY_POINTS = 36
# Growth inputs for the regime map only (not shown as cards), fetched as YoY %.
REGIME_AUX_SERIES = ("PAYEMS", "INDPRO")
# Official real-time Sahm indicator (preferred over our own reconstruction).
SAHM_SERIES = "SAHMREALTIME"
# Long monthly history (range switcher "5Y" / "Since 2000", percentile context).
LONG_HISTORY_START = "2000-01-01"


# --- Incremental fetching + local SQLite store (2026-09-25) ---
# Every series is fetched as published ("native" frequency) since
# LONG_HISTORY_START ONCE, kept in _OBS and mirrored to the SQLite store
# (newsroom/db.py, table macro_observations). Later polls only ask FRED for
# observations from (latest stored date − REVISION_WINDOW_DAYS) onward —
# FRED revises CPI, payrolls, industrial production etc. — and replace that
# tail. A full refetch happens when nothing is stored or every
# FULL_REFRESH_SECONDS (7 days, per series, meta key "obs_full:<id>:<units>").
# The card history (last 24/48 points), the monthly long history (monthly
# series as they are; daily/weekly series as local monthly averages, the same
# thing FRED's frequency=m&aggregation_method=avg returned before) and every
# derived indicator are built from this cache — so the cards also show
# instantly at startup (load_state() + build_events()).
# Before: ~45 requests per poll (recent + long history per series). Now: one
# small request per series (18) + Eurostat (1) + releases/dates (1).
FREQ_NATIVE = "native"
FULL_REFRESH_SECONDS = 7 * 86400
REVISION_WINDOW_DAYS = 400
RECESSION_SERIES = "USREC"  # NBER-based recession indicator, monthly 0/1 (extended regime view)
# Euro-area industrial production (extended regime view): Eurostat JSON-stat API, no key.
EURO_IP_SERIES = "EUROSTAT_IP_EA20"
EURO_IP_UNITS = "PCH_SM"   # % change vs. same month a year earlier (= YoY)
EURO_HICP_SERIES = "CP0000EZ19M086NEST"
EUROSTAT_URL = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/sts_inpr_m"
EUROSTAT_PARAMS = {"geo": "EA20", "nace_r2": "B-D", "s_adj": "CA", "unit": EURO_IP_UNITS,
                   "format": "JSON", "lang": "EN"}

_OBS: dict = {}          # (series_id, units key) -> {date: raw value (unscaled)}
_LAST_FULL: dict = {}    # (series_id, units key) -> epoch seconds of the last full refetch
_NEXT_RELEASE: dict = {} # series_id -> {"date", "release"} (last known, also stored in meta)
_requests = 0            # HTTP requests of the current poll (logged)


def _ukey(units) -> str:
    return units or "lin"  # FRED's own code for "levels"


def fetch_specs() -> list:
    """Every (series_id, FRED units) one poll fetches."""
    specs = [(sid, units) for sid, _l, _u, units, _n in SERIES]
    specs += [(SAHM_SERIES, None)] + [(sid, "pc1") for sid in REGIME_AUX_SERIES] + [(RECESSION_SERIES, None)]
    return specs


def _native_monthly(series_id: str) -> bool:
    return series_id in MONTHLY_SERIES or series_id in (SAHM_SERIES, RECESSION_SERIES, *REGIME_AUX_SERIES)


def plan_fetch(key: tuple, now: float = None) -> tuple:
    """(observation_start "YYYY-MM-DD", full refetch?) for one cached series."""
    now = time.time() if now is None else now
    obs = _OBS.get(key)
    if not obs or now - _LAST_FULL.get(key, 0) >= FULL_REFRESH_SECONDS:
        return LONG_HISTORY_START, True
    start = date.fromisoformat(max(obs)[:10]) - timedelta(days=REVISION_WINDOW_DAYS)
    return max(start.isoformat(), LONG_HISTORY_START), False


def apply_fetch(key: tuple, points: list, start: str, full: bool, now: float = None) -> None:
    """Merges fetched points into the cache: full = replace the series,
    otherwise replace everything on/after start (revisions, withdrawn values).
    An empty answer never wipes cached data."""
    if not points:
        return
    cur = _OBS.setdefault(key, {})
    if full:
        cur.clear()
        _LAST_FULL[key] = time.time() if now is None else now
    else:
        for d in [d for d in cur if d >= start]:
            del cur[d]
    for p in points:
        cur[p["date"]] = p["value"]


async def _persist(key: tuple, points: list, start: str, full: bool) -> None:
    if not points or not store_db.is_enabled():
        return
    sid, units = key
    await asyncio.to_thread(store_db.replace_observations, sid, units, FREQ_NATIVE, points,
                            None if full else start, full)
    if full:
        store_db.meta_set(f"obs_full:{sid}:{units}", _LAST_FULL.get(key, time.time()))


def load_state() -> int:
    """Startup: fills the observation cache, the full-refetch times, the
    release-id cache and the last known next-release dates from the SQLite
    store. Returns the number of series loaded (0 when the store is off)."""
    stored = store_db.load_observations(FREQ_NATIVE)
    for (sid, units, _freq), pts in stored.items():
        _OBS[(sid, units)] = {p["date"]: p["value"] for p in pts}
    for k, v in store_db.meta_prefix("obs_full:").items():
        try:
            _, sid, units = k.split(":", 2)
            _LAST_FULL[(sid, units)] = float(v)
        except ValueError:
            continue
    try:
        for sid, (rid, name) in json.loads(store_db.meta_get("fred_release_ids") or "{}").items():
            _RELEASE_IDS[sid] = (rid, name)
        _NEXT_RELEASE.update(json.loads(store_db.meta_get("fred_next_release") or "{}"))
    except Exception as ex:
        logger.warning("Stored FRED release info unreadable: %s", ex)
    return len(stored)


async def _fetch_observations(session: aiohttp.ClientSession, series_id: str, units,
                              observation_start: str) -> list:
    """Native observations from observation_start on, oldest first, as
    [{"date": "YYYY-MM-DD", "value": float}]. FRED marks missing values '.'."""
    global _requests
    params = {
        "series_id": series_id, "api_key": FRED_API_KEY, "file_type": "json",
        "sort_order": "asc", "observation_start": observation_start,
    }
    if units:
        params["units"] = units
    _requests += 1
    async with session.get(OBSERVATIONS_URL, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)
    points = []
    for obs in data.get("observations") or []:
        try:
            points.append({"date": obs["date"], "value": float(obs["value"])})
        except (KeyError, TypeError, ValueError):
            continue  # '.' = no value for that date
    return points


def parse_jsonstat(payload: dict) -> list:
    """Eurostat JSON-stat 2.0 -> [{"date": "YYYY-MM-01", "value"}] oldest first.
    `value` is a sparse object {"<flat index>": number} (or a list); the time
    labels live in dimension.time.category.index ({"2025-01": 0, ...}). Every
    other dimension is expected to have one category (index 0)."""
    time_index = payload["dimension"]["time"]["category"]["index"]
    if isinstance(time_index, list):
        time_index = {label: i for i, label in enumerate(time_index)}
    ids, sizes = payload.get("id") or ["time"], payload.get("size") or [len(time_index)]
    stride = 1
    for dim, size in zip(reversed(ids), reversed(sizes)):
        if dim == "time":
            break
        stride *= size
    values = payload.get("value") or {}
    out = []
    for label, i in time_index.items():
        flat = int(i) * stride
        v = values.get(str(flat)) if isinstance(values, dict) else (values[flat] if flat < len(values) else None)
        if v is None:
            continue
        month = str(label).replace("M", "-") if "M" in str(label) else str(label)  # old "2025M01" style
        try:
            out.append({"date": f"{month[:7]}-01", "value": float(v)})
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda p: p["date"])
    return out


async def _fetch_eurostat_ip(session: aiohttp.ClientSession, since_month: str) -> list:
    global _requests
    _requests += 1
    params = {**EUROSTAT_PARAMS, "sinceTimePeriod": since_month}
    async with session.get(EUROSTAT_URL, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)
    return parse_jsonstat(data)


def monthly_average(points: list) -> list:
    """Daily/weekly points -> monthly averages dated YYYY-MM-01 (the current,
    partial month included — as FRED's frequency=m aggregation does)."""
    buckets: dict = {}
    for p in points:
        buckets.setdefault(p["date"][:7], []).append(p["value"])
    return [{"date": f"{m}-01", "value": round(sum(v) / len(v), 4)} for m, v in sorted(buckets.items())]


def _points(series_id: str, units) -> list:
    obs = _OBS.get((series_id, _ukey(units))) or {}
    pts = [{"date": d, "value": v} for d, v in sorted(obs.items())]
    if series_id in SCALE:
        pts = [{"date": p["date"], "value": round(p["value"] * SCALE[series_id], 4)} for p in pts]
    return pts


def _long_of(series_id: str, pts: list) -> list:
    return pts if _native_monthly(series_id) else monthly_average(pts)


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _month_label(iso: str) -> str:
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    try:
        return f"{months[int(iso[5:7]) - 1]} {iso[:4]}"
    except (ValueError, IndexError):
        return iso


def history_context(long_history: list, value: float) -> dict:
    """Where the current value sits in the monthly history since 2000:
    percentile + "highest/lowest since <month>" when that is notable."""
    if len(long_history) < 24:
        return {}
    values = [p["value"] for p in long_history]
    pct = round(100 * sum(1 for x in values if x <= value) / len(values))
    since = long_history[0]["date"][:4]
    text = f"{_ordinal(max(1, min(100, pct)))} percentile since {since}"
    earlier = long_history[:-1]  # exclude the current month
    extreme = ""
    if pct >= 90:
        prior = [p for p in earlier if p["value"] >= value]
        extreme = (f"highest since {_month_label(prior[-1]['date'])}" if prior
                   else f"highest since at least {since}")
    elif pct <= 10:
        prior = [p for p in earlier if p["value"] <= value]
        extreme = (f"lowest since {_month_label(prior[-1]['date'])}" if prior
                   else f"lowest since at least {since}")
    # Only mention it when the previous comparable month is more than a year back.
    if extreme and prior and prior[-1]["date"] >= long_history[-13]["date"]:
        extreme = ""
    return {"percentile": pct, "since": since, "text": text + (f" · {extreme}" if extreme else "")}


# --- Next release dates (FRED release calendar) ---
_RELEASE_IDS: dict = {}  # series_id -> (release_id, release_name); cached for the process lifetime


async def _get_json(session, endpoint: str, params: dict) -> dict:
    global _requests
    _requests += 1
    params = {**params, "api_key": FRED_API_KEY, "file_type": "json"}
    async with session.get(f"https://api.stlouisfed.org/fred/{endpoint}", params=params,
                           timeout=aiohttp.ClientTimeout(total=15)) as resp:
        resp.raise_for_status()
        return await resp.json(content_type=None)


async def _next_releases(session, series_ids: list, sem) -> dict:
    """series_id -> {"date", "release"} for the next scheduled release (today or later)."""
    from datetime import date, timedelta
    today = date.today()

    async def _rid(sid):
        if sid in _RELEASE_IDS:
            return
        async with sem:
            data = await _get_json(session, "series/release", {"series_id": sid})
        rel = (data.get("releases") or [{}])[0]
        if rel.get("id") is not None:
            _RELEASE_IDS[sid] = (rel["id"], rel.get("name", ""))

    await asyncio.gather(*(_rid(s) for s in series_ids), return_exceptions=True)
    wanted = {_RELEASE_IDS[s][0] for s in series_ids if s in _RELEASE_IDS}
    first: dict = {}
    try:
        async with sem:
            data = await _get_json(session, "releases/dates", {
                "realtime_start": today.isoformat(), "realtime_end": (today + timedelta(days=75)).isoformat(),
                "include_release_dates_with_no_data": "true", "sort_order": "asc",
                "order_by": "release_date", "limit": "1000"})
        for r in data.get("release_dates") or []:
            rid, d = r.get("release_id"), r.get("date", "")
            if rid in wanted and d >= today.isoformat() and rid not in first:
                first[rid] = d
    except Exception as ex:
        logger.warning("FRED releases/dates failed: %s", ex)

    async def _fallback(rid):
        async with sem:
            data = await _get_json(session, "release/dates", {
                "release_id": rid, "include_release_dates_with_no_data": "true", "sort_order": "asc",
                "realtime_start": today.isoformat(), "realtime_end": (today + timedelta(days=75)).isoformat()})
        dates = [r.get("date", "") for r in data.get("release_dates") or [] if r.get("date", "") >= today.isoformat()]
        if dates:
            first[rid] = dates[0]

    await asyncio.gather(*(_fallback(rid) for rid in wanted if rid not in first), return_exceptions=True)
    out = {}
    for sid in series_ids:
        if sid in _RELEASE_IDS and _RELEASE_IDS[sid][0] in first:
            out[sid] = {"date": first[_RELEASE_IDS[sid][0]], "release": _RELEASE_IDS[sid][1]}
    return out


# Stability signal + popup rule texts: see macro_rules.py (single source).


def _fmt(value: float, unit: str) -> str:
    return f"{value:.2f}{'%' if unit == '%' else ' ' + unit}"


def _upcoming_releases(next_release: dict = None) -> dict:
    today = date.today().isoformat()
    source = _NEXT_RELEASE if next_release is None else next_release
    return {sid: r for sid, r in source.items() if isinstance(r, dict) and (r.get("date") or "") >= today}


def _card_event(spec: tuple, pts: list, long_pts: list, upcoming: dict) -> tuple:
    """(card event, recent points used for the derived indicators)."""
    series_id, label, unit, _units, note = spec
    fetched = pts[-(MONTHLY_FETCH_POINTS if series_id in MONTHLY_SERIES else HISTORY_POINTS):]
    history = fetched[-HISTORY_POINTS:]
    latest = history[-1]
    previous = history[-2] if len(history) > 1 else None
    change = round(latest["value"] - previous["value"], 4) if previous else None

    summary = f"{_fmt(latest['value'], unit)} as of {latest['date']}"
    if previous:
        summary += f" ({change:+.2f} {'pp' if unit in ('%', 'pp') else unit} vs. {previous['date']})"

    event = make_event(
        event_id=f"macro-{series_id}",
        category="macro",
        title=label,
        summary=summary,
        source="FRED",
        url=f"https://fred.stlouisfed.org/series/{series_id}",
    )
    m = {
        "series_id": series_id,
        "group": GROUP.get(series_id, "us"),
        "unit": unit,
        "value": latest["value"],
        "date": latest["date"],
        "previous": previous["value"] if previous else None,
        "previous_date": previous["date"] if previous else None,
        "change": change,
        "history": history,
        "note": note,
        "signal": {**stability_signal(series_id, history), "rules": describe_rules(series_id)},
    }
    if long_pts:
        m["long_history"] = long_pts
        m["context"] = history_context(long_pts, m["value"])
    if series_id in upcoming:
        m["next_release"] = upcoming[series_id]
    event["macro"] = m
    return event, fetched


def build_events(next_release: dict = None) -> tuple:
    """Every macro event (cards, derived indicators, regime map incl. the
    extended view) from the local observation cache — no I/O. Used after each
    poll and at startup (cards from the SQLite store before the first poll).
    Returns (events, card count, derived count, errors)."""
    upcoming = _upcoming_releases(next_release)
    events, errors = [], []
    full, long = {}, {}
    for spec in SERIES:
        sid = spec[0]
        pts = _points(sid, spec[3])
        if not pts:
            continue
        try:
            long[sid] = _long_of(sid, pts)
            event, full[sid] = _card_event(spec, pts, long[sid], upcoming)
            events.append(event)
        except Exception as ex:
            errors.append(f"{sid}: {str(ex) or type(ex).__name__}")
            logger.exception("FRED card %s failed", sid)
    placed = len(events)
    sahm = _points(SAHM_SERIES, None)
    if sahm:
        long[SAHM_SERIES] = sahm
    curve_monthly = (long.get("T10Y2Y") or [])[-CURVE_MONTHLY_POINTS:]
    aux_long = {sid: _points(sid, "pc1") for sid in REGIME_AUX_SERIES}
    aux = {sid: pts[-MONTHLY_FETCH_POINTS:] for sid, pts in aux_long.items()}

    # --- Derived indicators (macro_derived.py) ---
    derived_placed = 0
    try:
        long_derived = macro_derived.compute(long, long.get("T10Y2Y") or [], long.get(SAHM_SERIES))
        recent = macro_derived.compute(full, curve_monthly, sahm[-CURVE_MONTHLY_POINTS:])
        for key, series in recent.items():
            d = macro_derived.to_macro(key, series, long_derived.get(key), recent.formulas.get(key))
            if long_derived.get(key):
                d["macro"]["context"] = history_context(long_derived[key], d["macro"]["value"])
            m = d["macro"]
            event = make_event(
                event_id=f"macro-derived-{key}",
                category="macro",
                title=d["label"],
                summary=f"{_fmt(m['value'], m['unit'])} as of {m['date']} ({m['formula']})",
                source="FRED (derived)",
                url=None,
            )
            event["macro"] = m
            events.append(event)
            derived_placed += 1
    except Exception as ex:
        errors.append(f"derived: {str(ex) or type(ex).__name__}")
        logger.exception("Derived macro indicators failed")

    # --- Macro regime map (growth × inflation momentum) + extended view ---
    try:
        reg = macro_derived.regime(full, aux)
        if reg:
            try:
                reg["extended"] = macro_derived.regime_extended(
                    {**aux_long, "PCEPILFE": long.get("PCEPILFE"), "CPIAUCSL": long.get("CPIAUCSL")},
                    usrec=_points(RECESSION_SERIES, None),
                    euro_ip=_points(EURO_IP_SERIES, EURO_IP_UNITS),
                    euro_hicp=long.get(EURO_HICP_SERIES))
            except Exception as ex:
                errors.append(f"regime extended: {str(ex) or type(ex).__name__}")
                logger.exception("Extended regime view failed")
            event = make_event(
                event_id="macro-regime",
                category="macro",
                title="Macro Regime",
                summary=f"{reg['label']} as of {reg['date']}",
                source="FRED (derived)",
                url=None,
            )
            event["macro"] = {"series_id": "REGIME", "group": "regime", "regime": reg}
            events.append(event)
    except Exception as ex:
        errors.append(f"regime: {str(ex) or type(ex).__name__}")
        logger.exception("Macro regime map failed")
    return events, placed, derived_placed, errors


async def _refresh_next_releases(session, sem) -> None:
    try:
        found = await _next_releases(session, [sid for sid, *_ in SERIES if sid not in DAILY_SERIES], sem)
    except Exception as ex:
        logger.warning("FRED next release dates failed: %s", ex)
        return
    if found:
        _NEXT_RELEASE.clear()
        _NEXT_RELEASE.update(found)
        store_db.meta_set("fred_next_release", json.dumps(found))
    if _RELEASE_IDS:
        store_db.meta_set("fred_release_ids", json.dumps({sid: list(v) for sid, v in _RELEASE_IDS.items()}))


async def _poll_once(session: aiohttp.ClientSession, emit, report_status) -> None:
    global _requests
    _requests = 0
    errors = []
    full_refetches = 0
    # At most FRED_CONCURRENCY requests at a time; each card is emitted as soon
    # as its series arrives (matters on the very first start, when nothing is stored).
    sem = asyncio.Semaphore(FRED_CONCURRENCY)
    cards = {spec[0]: spec for spec in SERIES}
    started = asyncio.get_running_loop().time()

    async def _one(series_id, units):
        nonlocal full_refetches
        key = (series_id, _ukey(units))
        start, full = plan_fetch(key)
        try:
            async with sem:
                pts = await _fetch_observations(session, series_id, units, start)
        except Exception as ex:
            msg = str(ex) or type(ex).__name__
            errors.append(f"{series_id}: {msg}")
            logger.warning("FRED series %s failed (keeping stored data): %s", series_id, msg)
            return
        full_refetches += 1 if full else 0
        apply_fetch(key, pts, start, full)
        await _persist(key, pts, start, full)
        if series_id in cards and _OBS.get(key):
            try:
                all_pts = _points(series_id, units)
                emit(_card_event(cards[series_id], all_pts, _long_of(series_id, all_pts), _upcoming_releases())[0])
            except Exception:
                logger.exception("FRED card %s failed", series_id)

    async def _euro():
        nonlocal full_refetches
        key = (EURO_IP_SERIES, EURO_IP_UNITS)
        start, full = plan_fetch(key)
        try:
            async with sem:
                pts = await _fetch_eurostat_ip(session, start[:7])
        except Exception as ex:
            logger.warning("Eurostat industrial production failed (euro-area regime point keeps stored data "
                           "or stays empty): %s", str(ex) or type(ex).__name__)
            return
        if not pts:
            logger.warning("Eurostat industrial production: no observations since %s", start[:7])
            return
        full_refetches += 1 if full else 0
        apply_fetch(key, pts, start, full)
        await _persist(key, pts, start, full)

    await asyncio.gather(asyncio.gather(*(_one(sid, units) for sid, units in fetch_specs())),
                         _euro(), _refresh_next_releases(session, sem))

    events, placed, derived_placed, build_errors = build_events()
    for event in events:
        emit(event)
    errors += build_errors

    logger.info("FRED poll done in %.1fs: %d HTTP requests (%d full history refetches), %d series, %d derived, "
                "%d error(s)", asyncio.get_running_loop().time() - started, _requests, full_refetches,
                placed, derived_placed, len(errors))
    if errors and placed == 0:
        await report_status(False, "; ".join(errors))
    else:
        await report_status(True, f"{placed}/{len(SERIES)} series + {derived_placed} derived updated" + (f" ({len(errors)} failed)" if errors else ""))


async def run(emit, report_status) -> None:
    if not FRED_API_KEY:
        msg = "FRED_API_KEY is not set in HedgeFund/backend/.env (free key: https://fred.stlouisfed.org/docs/api/api_key.html). Collector disabled."
        logger.error(msg)
        await report_status(False, msg)
        return
    async with aiohttp.ClientSession() as session:
        while True:
            await _poll_once(session, emit, report_status)
            await asyncio.sleep(FRED_POLL_SECONDS)
