"""FRED release calendar + FOMC meetings — the Financial Newsroom's
"Economic Calendar" section (added 2026-09-25).

What it shows: the next CALENDAR_WINDOW_DAYS (45) days of the US data
releases that move markets most, plus the FOMC rate decisions, each with a
one-line "why it matters" note and the latest value of the underlying series
("last: 3.35% YoY (Aug 2026)"). Consensus forecasts are NOT included — they
are not freely available.

Sources (FRED API, same free key as the Macro Indicators section):
  1. fred/series/release       series id -> FRED release {id, name, link}
                               (cached for the process lifetime)
  2. fred/releases/dates       all scheduled release dates in the window
                               (paged, filtered to the curated releases);
                               fallback per release: fred/release/dates
  3. fred/series/observations  latest value per curated series
FOMC dates are hardcoded below (FOMC_MEETINGS) from federalreserve.gov.

Emits ONE dict per poll: {"events": [...], "updated": iso, "window_days": 45,
"errors": [...]}; main.py keeps the latest (GET /newsroom/calendar).
Event fields: date, time_hint, kind ("release"|"fomc"), name, series_id,
importance ("high"|"medium"), why, last_value, last_unit, last_date,
last_display, release_link, sep (FOMC with Summary of Economic Projections).
"""
import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

import aiohttp

from config import FRED_API_KEY, CALENDAR_POLL_SECONDS

logger = logging.getLogger(__name__)

API_BASE = "https://api.stlouisfed.org/fred/"
CALENDAR_WINDOW_DAYS = 45
CONCURRENCY = 4            # FRED allows 120 requests/minute; 4 in parallel is gentle
REQUEST_TIMEOUT_SECONDS = 15
RELEASE_DATES_PAGE = 1000  # FRED maximum for fred/releases/dates
RELEASE_DATES_MAX_PAGES = 5

# Curated series -> the release that publishes them.
# (series_id, display name, importance, FRED "units" transformation, unit label, frequency, why it matters)
# The transformation gives the headline number markets quote (CPI/PCE as YoY %,
# payrolls as monthly change, GDP as annualised q/q growth, retail sales /
# industrial production / PPI as m/m %).
CURATED = [
    ("CPIAUCSL", "CPI (Consumer Price Index)", "high", "pc1", "% YoY", "m",
     "Headline inflation; a hot print pushes yields up and rate-cut hopes out."),
    ("PAYEMS", "Employment Situation (jobs report)", "high", "chg", "K m/m", "m",
     "Nonfarm payrolls + unemployment rate — the single biggest monthly market mover."),
    ("PCEPILFE", "Personal Income and Outlays (PCE)", "high", "pc1", "% YoY", "m",
     "Core PCE is the Fed's preferred inflation gauge; its 2% target refers to PCE."),
    ("GDPC1", "GDP", "high", "pca", "% q/q ann.", "q",
     "Broadest measure of growth (real, annualised); revisions can move markets too."),
    ("ICSA", "Weekly Jobless Claims", "medium", None, "K", "w",
     "Earliest weekly read on layoffs; a sustained rise often leads the unemployment rate."),
    ("RSAFS", "Retail Sales", "medium", "pch", "% m/m", "m",
     "Consumer spending drives ~2/3 of GDP; a quick read on demand."),
    ("INDPRO", "Industrial Production", "medium", "pch", "% m/m", "m",
     "Factory, mining and utility output — cyclical side of the economy."),
    ("JTSJOL", "JOLTS (Job Openings)", "medium", None, "M", "m",
     "Labour-demand gauge the Fed watches for wage pressure; published with a ~2-month lag."),
    ("PPIFIS", "PPI (Producer Prices)", "medium", "pch", "% m/m", "m",
     "Pipeline inflation; feeds into several PCE components, so it sharpens PCE estimates."),
    ("UMCSENT", "Consumer Sentiment (U. Michigan)", "medium", None, "index", "m",
     "Household mood plus inflation expectations the Fed cites; preliminary mid-month, final at month-end."),
]

# Display scaling: ICSA is in persons (-> thousands), JTSJOL in thousands (-> millions).
SCALE = {"ICSA": 0.001, "JTSJOL": 0.001}

# TYPICAL release times (US Eastern) — documented practice of the agencies, not
# guaranteed for every release; the view labels them as typical.
#   BLS/BEA/Census/DOL releases (CPI, jobs, PCE, GDP, claims, retail sales, PPI): 08:30 ET
#   Fed G.17 industrial production: 09:15 ET; JOLTS and U. Michigan sentiment: 10:00 ET
#   FOMC statement: 14:00 ET (press conference 14:30 ET)
TIME_HINTS = {
    "CPIAUCSL": "08:30 ET", "PAYEMS": "08:30 ET", "PCEPILFE": "08:30 ET", "GDPC1": "08:30 ET",
    "ICSA": "08:30 ET", "RSAFS": "08:30 ET", "PPIFIS": "08:30 ET",
    "INDPRO": "09:15 ET", "JTSJOL": "10:00 ET", "UMCSENT": "10:00 ET",
    "FOMC": "14:00 ET",
}

# FOMC meetings, verified from https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
# (as of 2026-09-25). MUST BE UPDATED YEARLY from that URL (the Fed publishes the
# next year's schedule mid-year). (first day, second day = decision day, SEP)
# SEP = Summary of Economic Projections incl. the "dot plot".
FOMC_MEETINGS = [
    ("2026-01-27", "2026-01-28", False),
    ("2026-03-17", "2026-03-18", True),
    ("2026-04-28", "2026-04-29", False),
    ("2026-06-16", "2026-06-17", True),
    ("2026-07-28", "2026-07-29", False),
    ("2026-09-15", "2026-09-16", True),
    ("2026-10-27", "2026-10-28", False),
    ("2026-12-08", "2026-12-09", True),
    ("2027-01-26", "2027-01-27", False),
    ("2027-03-16", "2027-03-17", True),
    ("2027-04-27", "2027-04-28", False),
    ("2027-06-08", "2027-06-09", True),
    ("2027-07-27", "2027-07-28", False),
    ("2027-09-14", "2027-09-15", True),
    ("2027-10-26", "2027-10-27", False),
    ("2027-12-07", "2027-12-08", True),
]
FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
FOMC_WHY = ("Fed policy-rate decision (statement 14:00 ET, press conference 14:30 ET); "
            "moves every asset class.")
FOMC_WHY_SEP = FOMC_WHY + " Includes new economic projections and the dot plot."

# series_id -> {"id", "name", "link"}; release ids don't change, so cache for the process lifetime.
_release_cache: dict = {}


def release_page(release_id) -> str:
    """FRED's page for a release (all its series + release calendar)."""
    return f"https://fred.stlouisfed.org/release?rid={release_id}"


async def _fetch_json(session, endpoint: str, params: dict) -> dict:
    """GET API_BASE + endpoint with the API key; raises on HTTP errors.
    Tests monkeypatch this function."""
    q = dict(params)
    q["api_key"] = FRED_API_KEY
    q["file_type"] = "json"
    async with session.get(API_BASE + endpoint, params=q,
                           timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)) as resp:
        resp.raise_for_status()
        return await resp.json(content_type=None)


# ---------- pure helpers (unit-tested in tests/test_calendar.py) ----------

def _parse_date(s):
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def select_release_dates(release_dates: list, wanted_ids, start: date, end: date) -> dict:
    """fred/releases/dates rows -> {release_id: sorted unique dates in [start, end]}
    for the wanted release ids only."""
    wanted = {int(i) for i in wanted_ids}
    out: dict = {}
    for row in release_dates or []:
        try:
            rid = int(row.get("release_id"))
        except (TypeError, ValueError, AttributeError):
            continue
        if rid not in wanted:
            continue
        d = _parse_date(row.get("date"))
        if d is None or d < start or d > end:
            continue
        out.setdefault(rid, set()).add(d)
    return {rid: sorted(ds) for rid, ds in out.items()}


def latest_observation(observations: list):
    """First numeric observation of a desc-sorted list -> (date str, float) or None.
    FRED marks missing values '.'."""
    for obs in observations or []:
        try:
            return obs["date"], float(obs["value"])
        except (KeyError, TypeError, ValueError):
            continue
    return None


def period_label(iso_date: str, frequency: str) -> str:
    d = _parse_date(iso_date)
    if d is None:
        return iso_date or ""
    if frequency == "q":
        return f"Q{(d.month - 1) // 3 + 1} {d.year}"
    if frequency == "m":
        return d.strftime("%b %Y")
    return f"week of {d.strftime('%b')} {d.day}, {d.year}"


def format_last(value: float, unit: str) -> str:
    """Display string for the latest value: "3.35% YoY", "+142K m/m", "231K", "7.20M", "55.1"."""
    base, _, suffix = unit.partition(" ")
    tail = f" {suffix}" if suffix else ""
    if base == "K":
        # A change (payrolls, "K m/m") gets a sign; a level (claims) doesn't.
        return (f"{value:+,.0f}K" if suffix else f"{value:,.0f}K") + tail
    if base == "M":
        return f"{value:.2f}M" + tail
    if base == "index":
        return f"{value:.1f}" + tail
    return f"{value:.2f}%" + tail


def fomc_events(start: date, end: date) -> list:
    events = []
    for first, second, sep in FOMC_MEETINGS:
        d = date.fromisoformat(second)
        if start <= d <= end:
            events.append({
                "date": second, "time_hint": TIME_HINTS["FOMC"], "kind": "fomc",
                "name": "FOMC rate decision" + (" + projections" if sep else ""),
                "series_id": "", "importance": "high",
                "why": FOMC_WHY_SEP if sep else FOMC_WHY,
                "last_value": None, "last_unit": "", "last_date": "", "last_display": "",
                "release_link": FOMC_URL, "sep": sep, "meeting_start": first,
            })
    return events


def build_events(releases: dict, dates_by_release: dict, latest: dict, start: date, end: date) -> list:
    """releases: series_id -> {"id","name","link"}; dates_by_release: release_id -> [date];
    latest: series_id -> (date str, value) (already scaled). One event per (series, date);
    two curated series on the same release+date are listed once (first series wins)."""
    events = []
    seen = set()
    for series_id, name, importance, _units, unit, freq, why in CURATED:
        rel = releases.get(series_id)
        if not rel:
            continue
        obs = latest.get(series_id)
        for d in dates_by_release.get(int(rel["id"]), []):
            if not (start <= d <= end) or (rel["id"], d) in seen:
                continue
            seen.add((rel["id"], d))
            last_value = round(obs[1], 4) if obs else None
            last_date = obs[0] if obs else ""
            events.append({
                "date": d.isoformat(), "time_hint": TIME_HINTS.get(series_id, ""), "kind": "release",
                "name": name, "series_id": series_id, "importance": importance, "why": why,
                "release_name": rel.get("name", ""),
                "last_value": last_value, "last_unit": unit, "last_date": last_date,
                "last_display": (f"{format_last(last_value, unit)} ({period_label(last_date, freq)})"
                                 if obs else ""),
                "release_link": release_page(rel["id"]), "sep": False,
            })
    return events


def sort_events(events: list) -> list:
    """By date, then time hint ("" last), high importance first, then name."""
    return sorted(events, key=lambda e: (e["date"], e["time_hint"] or "99", 0 if e["importance"] == "high" else 1,
                                         e["name"]))


# ---------- poll ----------

async def _poll_once(session, emit, report_status) -> None:
    today = date.today()
    end = today + timedelta(days=CALENDAR_WINDOW_DAYS)
    errors: list = []
    sem = asyncio.Semaphore(CONCURRENCY)

    async def call(endpoint, params):
        async with sem:
            return await _fetch_json(session, endpoint, params)

    # 1. series -> release (cached)
    async def resolve(series_id):
        if series_id in _release_cache:
            return
        try:
            data = await call("series/release", {"series_id": series_id})
            rel = (data.get("releases") or [None])[0]
            if not rel or rel.get("id") is None:
                raise ValueError("no release returned")
            _release_cache[series_id] = {"id": int(rel["id"]), "name": rel.get("name", ""),
                                         "link": rel.get("link", "")}
        except Exception as ex:
            errors.append(f"{series_id} release: {str(ex) or type(ex).__name__}")

    # 3. latest observation per series
    latest: dict = {}

    async def observe(series_id, units):
        params = {"series_id": series_id, "sort_order": "desc", "limit": "5"}
        if units:
            params["units"] = units
        try:
            obs = latest_observation((await call("series/observations", params)).get("observations"))
            if obs:
                latest[series_id] = (obs[0], obs[1] * SCALE.get(series_id, 1.0))
        except Exception as ex:
            errors.append(f"{series_id} value: {str(ex) or type(ex).__name__}")

    started = asyncio.get_running_loop().time()
    await asyncio.gather(*(resolve(s[0]) for s in CURATED), *(observe(s[0], s[3]) for s in CURATED))
    releases = {sid: _release_cache[sid] for sid, *_ in CURATED if sid in _release_cache}
    wanted_ids = {r["id"] for r in releases.values()}

    # 2. upcoming dates: all releases in the window (paged), then per-release fallback.
    rows: list = []
    window = {"realtime_start": today.isoformat(), "realtime_end": end.isoformat(),
              "include_release_dates_with_no_data": "true", "sort_order": "asc"}
    try:
        for page in range(RELEASE_DATES_MAX_PAGES):
            data = await call("releases/dates", {**window, "order_by": "release_date",
                                                 "limit": str(RELEASE_DATES_PAGE),
                                                 "offset": str(page * RELEASE_DATES_PAGE)})
            batch = data.get("release_dates") or []
            rows.extend(batch)
            if len(batch) < RELEASE_DATES_PAGE:
                break
    except Exception as ex:
        errors.append(f"releases/dates: {str(ex) or type(ex).__name__}")
    dates_by_release = select_release_dates(rows, wanted_ids, today, end)

    async def fallback(rid):
        try:
            data = await call("release/dates", {**window, "release_id": str(rid)})
            found = select_release_dates(data.get("release_dates") or [], {rid}, today, end)
            if found.get(rid):
                dates_by_release[rid] = found[rid]
        except Exception as ex:
            errors.append(f"release {rid} dates: {str(ex) or type(ex).__name__}")

    missing = sorted(rid for rid in wanted_ids if not dates_by_release.get(rid))
    if missing:
        await asyncio.gather(*(fallback(rid) for rid in missing))

    release_events = build_events(releases, dates_by_release, latest, today, end)
    events = sort_events(release_events + fomc_events(today, end))
    emit({"events": events, "updated": datetime.now(timezone.utc).isoformat(),
          "window_days": CALENDAR_WINDOW_DAYS, "errors": errors})

    fomc_count = len(events) - len(release_events)
    logger.info("Calendar poll done in %.1fs: %d releases, %d FOMC, %d error(s)",
                asyncio.get_running_loop().time() - started, len(release_events), fomc_count, len(errors))
    summary = (f"{len(release_events)} release dates from {len(releases)}/{len(CURATED)} releases + "
               f"{fomc_count} FOMC in the next {CALENDAR_WINDOW_DAYS} days")
    if errors and not release_events:
        await report_status(False, "; ".join(errors[:6]) + (f" (+{len(errors) - 6} more)" if len(errors) > 6 else ""))
    else:
        await report_status(True, summary + (f" ({len(errors)} failed)" if errors else ""))


async def run(emit, report_status) -> None:
    if not FRED_API_KEY:
        # FOMC dates need no key: still show them, and say why the releases are missing.
        msg = ("FRED_API_KEY is not set in HedgeFund/backend/.env (free key: "
               "https://fred.stlouisfed.org/docs/api/api_key.html) — showing FOMC dates only.")
        logger.error(msg)
        while True:
            today = date.today()
            emit({"events": sort_events(fomc_events(today, today + timedelta(days=CALENDAR_WINDOW_DAYS))),
                  "updated": datetime.now(timezone.utc).isoformat(),
                  "window_days": CALENDAR_WINDOW_DAYS, "errors": [msg]})
            await report_status(False, msg)
            await asyncio.sleep(CALENDAR_POLL_SECONDS)
    async with aiohttp.ClientSession() as session:
        while True:
            await _poll_once(session, emit, report_status)
            await asyncio.sleep(CALENDAR_POLL_SECONDS)
