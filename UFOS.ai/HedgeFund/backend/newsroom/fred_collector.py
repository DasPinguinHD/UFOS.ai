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
"""
import asyncio
import logging

import aiohttp

from config import FRED_API_KEY, FRED_POLL_SECONDS
from .store import make_event
from . import macro_derived
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


async def _fetch_series(session: aiohttp.ClientSession, series_id: str, units,
                        points_wanted: int = HISTORY_POINTS, frequency: str = None) -> list:
    """Returns up to points_wanted observations, oldest first, as
    [{"date": "YYYY-MM-DD", "value": float}]. FRED marks missing values '.'.
    frequency="m" lets FRED aggregate a daily series to monthly averages."""
    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": str(points_wanted + 10),  # a few spare for '.' gaps (holidays)
    }
    if units:
        params["units"] = units
    if frequency:
        params["frequency"] = frequency
        params["aggregation_method"] = "avg"
    async with session.get(OBSERVATIONS_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)
    points = []
    for obs in data.get("observations") or []:
        try:
            points.append({"date": obs["date"], "value": float(obs["value"])})
        except (KeyError, TypeError, ValueError):
            continue  # '.' = no value for that date
        if len(points) >= points_wanted:
            break
    points.reverse()
    return points


async def _fetch_long(session: aiohttp.ClientSession, series_id: str, units) -> list:
    """Monthly averages since LONG_HISTORY_START, oldest first (FRED aggregates
    daily/weekly series itself with frequency=m)."""
    params = {
        "series_id": series_id, "api_key": FRED_API_KEY, "file_type": "json",
        "sort_order": "asc", "observation_start": LONG_HISTORY_START,
        "frequency": "m", "aggregation_method": "avg",
    }
    if units:
        params["units"] = units
    async with session.get(OBSERVATIONS_URL, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)
    points = []
    for obs in data.get("observations") or []:
        try:
            points.append({"date": obs["date"], "value": float(obs["value"])})
        except (KeyError, TypeError, ValueError):
            continue
    return points


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


async def _poll_once(session: aiohttp.ClientSession, emit, report_status) -> None:
    placed = 0
    errors = []
    full = {}
    cards = {}
    # Series are fetched concurrently (at most FRED_CONCURRENCY requests at a time)
    # and each card is emitted as soon as its series arrives. Sequentially, the
    # 14 requests took long enough that the view sat on "loading" for a while.
    sem = asyncio.Semaphore(FRED_CONCURRENCY)

    async def _one(series_id, label, unit, units, note):
        nonlocal placed
        try:
            wanted = MONTHLY_FETCH_POINTS if series_id in MONTHLY_SERIES else HISTORY_POINTS
            async with sem:
                fetched = await _fetch_series(session, series_id, units, points_wanted=wanted)
            if not fetched:
                return
            if series_id in SCALE:
                fetched = [{"date": p["date"], "value": round(p["value"] * SCALE[series_id], 4)} for p in fetched]
            full[series_id] = fetched
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
            event["macro"] = {
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
            emit(event)          # show the card right away …
            cards[series_id] = event  # … and re-emit it below with long history / context / next release
            placed += 1
        except Exception as ex:
            msg = str(ex) or type(ex).__name__
            errors.append(f"{series_id}: {msg}")
            logger.warning("FRED series %s failed: %s", series_id, msg)

    # Inputs only used for derived indicators / the regime map (no cards).
    async def _extra(series_id, units, points, frequency=None):
        try:
            async with sem:
                return await _fetch_series(session, series_id, units, points_wanted=points, frequency=frequency)
        except Exception as ex:
            errors.append(f"{series_id} (extra): {str(ex) or type(ex).__name__}")
            logger.warning("FRED extra series %s failed: %s", series_id, ex)
            return []

    started = asyncio.get_running_loop().time()
    # Card series first in the queue (they show up first), extras behind them.
    _, extras = await asyncio.gather(
        asyncio.gather(*(_one(*spec) for spec in SERIES)),
        asyncio.gather(_extra("T10Y2Y", None, CURVE_MONTHLY_POINTS, frequency="m"),
                       _extra(SAHM_SERIES, None, CURVE_MONTHLY_POINTS),
                       *(_extra(sid, "pc1", MONTHLY_FETCH_POINTS) for sid in REGIME_AUX_SERIES)))
    curve_monthly, sahm_recent, *aux_lists = extras
    aux = dict(zip(REGIME_AUX_SERIES, aux_lists))

    # --- Second stage: long monthly history since 2000 + next release dates ---
    async def _long(series_id, units):
        try:
            async with sem:
                pts = await _fetch_long(session, series_id, units)
            if series_id in SCALE:
                pts = [{"date": p["date"], "value": round(p["value"] * SCALE[series_id], 4)} for p in pts]
            return series_id, pts
        except Exception as ex:
            logger.warning("FRED long history %s failed: %s", series_id, ex)
            return series_id, []

    long_specs = [(sid, units) for sid, _l, _u, units, _n in SERIES] + [(SAHM_SERIES, None)]
    long_results, next_release = await asyncio.gather(
        asyncio.gather(*(_long(sid, units) for sid, units in long_specs)),
        _next_releases(session, [sid for sid, *_ in SERIES if sid not in DAILY_SERIES], sem))
    long = {sid: pts for sid, pts in long_results if pts}
    for series_id, event in cards.items():
        m = event["macro"]
        if long.get(series_id):
            m["long_history"] = long[series_id]
            m["context"] = history_context(long[series_id], m["value"])
        if series_id in next_release:
            m["next_release"] = next_release[series_id]
        emit(event)

    # --- Derived indicators (macro_derived.py) ---
    derived_placed = 0
    try:
        long_derived = macro_derived.compute(long, long.get("T10Y2Y") or [], long.get(SAHM_SERIES))
        for key, series in macro_derived.compute(full, curve_monthly, sahm_recent).items():
            d = macro_derived.to_macro(key, series, long_derived.get(key))
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
            emit(event)
            derived_placed += 1
    except Exception as ex:
        errors.append(f"derived: {str(ex) or type(ex).__name__}")
        logger.exception("Derived macro indicators failed")

    # --- Macro regime map (growth × inflation momentum) ---
    try:
        reg = macro_derived.regime(full, aux)
        if reg:
            event = make_event(
                event_id="macro-regime",
                category="macro",
                title="Macro Regime",
                summary=f"{reg['label']} as of {reg['date']}",
                source="FRED (derived)",
                url=None,
            )
            event["macro"] = {"series_id": "REGIME", "group": "regime", "regime": reg}
            emit(event)
    except Exception as ex:
        errors.append(f"regime: {str(ex) or type(ex).__name__}")
        logger.exception("Macro regime map failed")

    logger.info("FRED poll done in %.1fs: %d series, %d derived, %d error(s)",
                asyncio.get_running_loop().time() - started, placed, derived_placed, len(errors))
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
