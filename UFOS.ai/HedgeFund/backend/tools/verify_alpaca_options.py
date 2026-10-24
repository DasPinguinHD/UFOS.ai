"""OS0 read-only data check: Alpaca options + FRED VIX series (Options Structure plan, SPEC §21 "Options Structure
layer — OS0 contract").

Run on Windows (PowerShell, from HedgeFund\\backend):
    .\\.venv\\Scripts\\python.exe tools\\verify_alpaca_options.py
Run it once during US market hours and once after the close — the tool reads the Alpaca market clock itself; outside
regular trading hours the snapshot checks say what they could not measure (stale quotes) — nothing is filled in.

READ-ONLY: every request is a GET on an allow-listed host + path (Alpaca paper trading API: option contracts, market
clock; Alpaca data API: option chain snapshots, option bars, latest stock trade; FRED: series metadata and
observations). Any other method, host or path is refused BEFORE sending (no order / position / account endpoint is
reachable from this tool). Redirects are not followed. Keys: the Alpaca PAPER key pair (``alpaca-paper-key-id`` /
``alpaca-paper-secret-key``) and ``fred-api-key`` from the Windows Credential Manager, exactly like the backend; never
printed, logged or written (the report is checked for them before it is saved).

Checks (each PASS / FAIL / PARTIAL with the measured facts):
  a  contracts endpoint (SPY, QQQ): the window today … today+60 d AND the full list (both with explicit expiry bounds —
     Alpaca's default is "up to the next weekend"): contracts, expiries, open_interest + open_interest_date, pages
  b  chain snapshot, feed=indicative (explicit — the endpoint's default is opra): greeks + implied_volatility share,
     quote ages vs the snapshot time (signed), latency, pages, filter grid counts
  c  option bars: current ATM contract (1Day + 1Min) and history probes (Jan / Feb 2024 SPY contracts → earliest bar)
  d  request budget: one filtered EOD snapshot, one intraday snapshot (both symbols), N snapshots/day — the archive's
     own 30 req/min next to the backend's 150 req/min (fixed split, total ≤ 190 < Alpaca's 200)
  e  storage estimate per day / month / year for the proposed filter (rows × bytes)
  f  FRED: VIXCLS, VXVCLS (3-month), VXNCLS (Nasdaq-100) and short-term candidates (VXSTCLS, VIX9DCLS) — found or not,
     with FRED's own error message
Output: a human summary and ``data/os0/verify_alpaca_options_<UTC>.json`` (no secrets).
Exit codes: 0 no FAIL · 1 at least one FAIL · 2 Alpaca keys missing / unreadable / rejected · 3 stopped (network,
clock unreadable, oversized answer) · 4 stopped on HTTP 429. Rate-limit friendly: one request at a time, a pause
between pages, a hard page cap, stop at the first 429.

Part of UFOS.ai, a proof-of-concept portfolio project; data collection check only — no trading, no AI.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)
if __name__ == "__main__":              # S10-H: refuse the system Python before any non-stdlib import
    from strategy_platform.core import venv_guard
    venv_guard.require("verify_alpaca_options.py", BACKEND_DIR, 3)

# transport + allow-list shared with the OS1 archive (SP §21.4): GET-only, allow-listed (method, host, path), https,
# no redirects, body cap, ONE bounded retry after a timeout / reset, sanitized network reasons
from strategy_platform.options_archive import transport as _transport  # noqa: E402
from strategy_platform.options_archive.transport import (  # noqa: E402,F401 — re-exported for the tests / callers
    ALLOWED, DATA, FRED, TRADING, NoRedirect, RateLimited, Refused, Stop, allowed, build_opener, sanitized_reason)

SYMBOLS = ("SPY", "QQQ")
TIMEOUT_SECONDS = 20.0                    # clock, latest trade, FRED
SLOW_TIMEOUT_SECONDS = 60.0               # contracts / snapshots / bars (large pages; user run 2026-10-23: 20 s timed out)
SLOW_PATHS = ("/v2/options/contracts", "/v1beta1/options/snapshots", "/v1beta1/options/bars")
RETRY_PAUSE_SECONDS = 2.0                 # ONE bounded retry after a timeout / connection reset (counted, reported)
REASON_MAX = _transport.REASON_MAX
MAX_BODY_BYTES = 20_000_000
PAGE_SLEEP_SECONDS = 0.5
MAX_PAGES = 40
CONTRACT_LIMIT = 1000                     # ≤ 1000 per page (was 10000; the large page timed out server-side)
SNAPSHOT_LIMIT = 1000
BAR_LIMIT = 10000
FULL_LIST_DAYS = 3 * 366                  # "all" active contracts: an explicit far bound
# filter grid measured by the check; the proposal (SPEC §21.2) is the middle cell
STRIKE_BANDS_PCT = (5.0, 10.0, 15.0)
EXPIRY_DAYS = (30, 45, 60)
PROPOSED = (10.0, 45)
PROPOSED_CADENCE = 28                     # every 15 min 09:45…15:45 + the 09:55 and 15:25 slots (SPEC §21.2)
INTRADAY_PER_DAY = (1, 13, PROPOSED_CADENCE, 78)
ALPACA_LIMIT_PER_MIN = 200                # Alpaca's documented per-account limit
BACKEND_SHARE_PER_MIN = 150               # backend gate (http_gate "alpaca"), + burst 10 → ≤ 160 in any minute
ARCHIVE_SHARE_PER_MIN = 30                # the OS1 archive's own gate — fixed split, no shared limiter (SPEC §21.1)
RTH_MINUTES = 390
# SQLite estimate per row incl. record header + primary-key index; run-constant columns live in os_runs, times are
# integer epoch ms (SPEC §21.5): snapshot ≈ 190 B payload + 25 B header/rowid + 45 B index; OI ≈ 60 + 20 + 45
ROW_BYTES = {"snapshot": 260, "oi": 125}
SESSIONS = {"day": 1, "month": 21, "year": 252}
FRED_SERIES = (("VIXCLS", "VIX (30-day, S&P 500)"), ("VXVCLS", "VIX 3-month"),
               ("VXNCLS", "VXN (30-day, Nasdaq-100 — QQQ replication)"),
               ("VXSTCLS", "VIX short-term (9-day), candidate"), ("VIX9DCLS", "VIX 9-day, candidate"))
FRED_REQUIRED = ("VIXCLS", "VXVCLS")
HISTORY_PROBES = ("SPY240119C00480000", "SPY240216C00500000")   # Jan 2024 (before 2024-02) and Feb 2024 monthlies
KEY_ID, SECRET_KEY, FRED_KEY = "alpaca-paper-key-id", "alpaca-paper-secret-key", "fred-api-key"
CHECK_NAMES = ("a_contracts", "b_snapshots", "c_bars", "d_budget", "e_storage", "f_fred")
DATA_ERRORS = (ValueError, TypeError, KeyError, AttributeError)


def fetch_with(opener, method: str, url: str, headers: Dict[str, str], timeout: float):
    """(status, headers, body) — a body larger than this module's MAX_BODY_BYTES is refused loudly (never truncated)."""
    return _transport.fetch_with(opener, method, url, headers, timeout, max_bytes=MAX_BODY_BYTES)


def _urllib_transport(method: str, url: str, headers: Dict[str, str], timeout: float):
    """GET over urllib with the backend's verifying TLS context (core/tls.py); redirects are NOT followed."""
    from strategy_platform.core import tls
    return fetch_with(build_opener(tls.client_context()), method, url, headers, timeout)


def timeout_for(path: str) -> float:
    return SLOW_TIMEOUT_SECONDS if path.startswith(SLOW_PATHS) else TIMEOUT_SECONDS


class Http(_transport.Http):
    """The shared ``Http`` with this tool's settings: per-path timeouts (``timeout_for``), ONE retry after a timeout /
    connection reset (no 5xx retry, no own rate gate — one request at a time), page pause + cap, progress lines."""

    def __init__(self, transport=None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, progress: Optional[Callable[[str], None]] = None):
        super().__init__(transport or _urllib_transport, sleep, clock, retries=1, retry_sleep=RETRY_PAUSE_SECONDS,
                         retry_on_5xx=False, timeouts=timeout_for, page_sleep=PAGE_SLEEP_SECONDS,
                         max_pages=MAX_PAGES, progress=progress)


# --- pure arithmetic (tested) --------------------------------------------------------------------------------------

def budget(contract_pages: Dict[str, int], snapshot_pages: Dict[str, int], per_day=INTRADAY_PER_DAY,
           share_per_min: int = ARCHIVE_SHARE_PER_MIN, backend_per_min: int = BACKEND_SHARE_PER_MIN,
           alpaca_per_min: int = ALPACA_LIMIT_PER_MIN) -> dict:
    """Requests for one EOD snapshot (OI pages + chain pages, both symbols) and one intraday snapshot (chain pages),
    per session for N intraday snapshots, and the minutes they occupy at the archive's own share."""
    snap = sum(snapshot_pages.values())
    eod = sum(contract_pages.values()) + snap
    out = {"eod_snapshot_requests": eod, "intraday_snapshot_requests": snap,
           "alpaca_limit_per_min": alpaca_per_min, "backend_share_per_min": backend_per_min,
           "archive_share_per_min": share_per_min,
           "split_total_per_min": backend_per_min + 10 + share_per_min,      # backend burst 10 included
           "eod_minutes_at_share": round(eod / share_per_min, 2) if share_per_min else None,
           "intraday_minutes_at_share": round(snap / share_per_min, 2) if share_per_min else None,
           "one_snapshot_within_one_minute": snap <= share_per_min, "per_day": []}
    for n in per_day:
        minutes = (n * snap) / share_per_min if share_per_min else None
        out["per_day"].append({"intraday_snapshots": n, "requests_per_day": eod + n * snap,
                               "archive_minutes_in_rth": round(minutes, 1) if minutes is not None else None,
                               "rth_share_pct": round(100.0 * minutes / RTH_MINUTES, 1) if minutes is not None else None})
    return out


def storage(contracts_in_filter: int, per_day=INTRADAY_PER_DAY, row_bytes=ROW_BYTES) -> dict:
    """Rows × bytes for the filter: per session one EOD snapshot + N intraday snapshots of every contract + one OI row."""
    out = []
    for n in per_day:
        rows = contracts_in_filter * (1 + n) + contracts_in_filter
        b = contracts_in_filter * (1 + n) * row_bytes["snapshot"] + contracts_in_filter * row_bytes["oi"]
        out.append({"intraday_snapshots": n, "rows_per_day": rows,
                    **{f"mb_per_{k}": round(b * v / 1e6, 1) for k, v in SESSIONS.items()}})
    return {"contracts_in_filter": contracts_in_filter, "row_bytes": dict(row_bytes), "per_day": out}


def in_filter(contract: dict, spot: Optional[float], band_pct: float, days: int, today: date) -> bool:
    try:
        k = float(contract["strike_price"])
        exp = date.fromisoformat(str(contract["expiration_date"])[:10])
    except (KeyError, TypeError, ValueError):
        return False
    if spot is None or spot <= 0:
        return False
    return abs(k / spot - 1.0) * 100.0 <= band_pct + 1e-9 and 0 <= (exp - today).days <= days


def occ_parts(symbol: str):
    """'SPY240216C00500000' → ('SPY', date(2024, 2, 16), 'C', 500.0) or None."""
    m = re.fullmatch(r"([A-Z]{1,6})(\d{6})([CP])(\d{8})", symbol or "")
    if not m:
        return None
    try:
        exp = datetime.strptime(m.group(2), "%y%m%d").date()
    except ValueError:
        return None
    return m.group(1), exp, m.group(3), int(m.group(4)) / 1000.0


def _iso(v) -> Optional[datetime]:
    """RFC 3339 incl. nanoseconds ("…T19:59:59.123456789Z") → aware datetime (fraction cut to µs); None if unparsable."""
    m = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?", str(v or "").strip())
    if not m:
        return None
    try:
        return datetime.fromisoformat(m.group(1) + ("." + m.group(2)[:6].ljust(6, "0") if m.group(2) else "")
                                      + ("+00:00" if m.group(3) in (None, "Z") else m.group(3)))
    except ValueError:
        return None


def _date(v) -> Optional[date]:
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _median(xs):
    xs = sorted(xs)
    return None if not xs else (xs[len(xs) // 2] if len(xs) % 2 else (xs[len(xs) // 2 - 1] + xs[len(xs) // 2]) / 2)


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


# --- the checks ----------------------------------------------------------------------------------------------------

def _contracts(http: Http, h: dict, sym: str, today: date, days: int):
    return http.pages(TRADING, "/v2/options/contracts",
                      {"underlying_symbols": sym, "status": "active", "expiration_date_gte": today.isoformat(),
                       "expiration_date_lte": (today + timedelta(days=days)).isoformat(), "limit": CONTRACT_LIMIT},
                      h, "option_contracts", "contracts")


def check_contracts(http: Http, h: dict, today: date) -> dict:
    """Both queries pass explicit expiry bounds (Alpaca defaults expiration_date_lte to the next weekend)."""
    facts, worst = {}, "PASS"
    window = max(EXPIRY_DAYS)
    for sym in SYMBOLS:
        f = {}
        for name, days in (("window", window), ("full", FULL_LIST_DAYS)):
            items, pages, status, capped = _contracts(http, h, sym, today, days)
            rows = [c for c in items if isinstance(c, dict)]
            bad = [c for c in rows if _date(c.get("expiration_date")) is None]
            ok = [c for c in rows if _date(c.get("expiration_date")) is not None]
            expiries = sorted({str(c["expiration_date"])[:10] for c in ok})
            with_oi = [c for c in rows if c.get("open_interest") not in (None, "")]
            with_date = [c for c in rows if c.get("open_interest_date")]
            g = {"query_days": days, "http_status": status, "pages": pages, "capped_at_max_pages": capped,
                 "contracts": len(rows), "malformed_expiration_date": len(bad), "expiries": len(expiries),
                 "first_expiry": expiries[0] if expiries else None, "last_expiry": expiries[-1] if expiries else None,
                 "open_interest_present_pct": _pct(len(with_oi), len(rows)),
                 "open_interest_date_present_pct": _pct(len(with_date), len(rows)),
                 "open_interest_dates": sorted({str(c["open_interest_date"])[:10] for c in with_date})[-3:]}
            f[name] = g
            if name == "window":
                f["_rows"] = ok
            if status != 200 or not rows:
                worst = "FAIL"
            elif capped or bad or g["open_interest_present_pct"] < 95 or g["open_interest_date_present_pct"] < 95:
                worst = "PARTIAL" if worst == "PASS" else worst
        facts[sym] = f
    return {"status": worst, "facts": facts}


def spot_of(http: Http, h: dict, sym: str) -> Tuple[Optional[float], Optional[str]]:
    status, data = http.get(DATA, f"/v2/stocks/{sym}/trades/latest", {"feed": "iex"}, h, "stock_trade")
    t = (data or {}).get("trade") if isinstance(data, dict) else None
    try:
        return float(t["p"]), t.get("t")
    except (TypeError, KeyError, ValueError):
        return None, None


def check_snapshots(http: Http, h: dict, today: date, spots: Dict[str, Optional[float]],
                    wall: Callable[[], datetime], market_open: Optional[bool]) -> dict:
    facts, worst = {}, "PASS"
    band, days = max(STRIKE_BANDS_PCT), max(EXPIRY_DAYS)
    for sym in SYMBOLS:
        spot = spots.get(sym)
        # feed MUST be explicit: the endpoint's default is opra (paid) — SPEC §21.1
        params = {"feed": "indicative", "limit": SNAPSHOT_LIMIT, "expiration_date_gte": today.isoformat(),
                  "expiration_date_lte": (today + timedelta(days=days)).isoformat()}
        if spot:
            params.update(strike_price_gte=round(spot * (1 - band / 100), 2),
                          strike_price_lte=round(spot * (1 + band / 100), 2))
        t0 = len(http.latency_ms.get("snapshots", []))
        slot = wall()                    # the snapshot's time: quote ages are measured against it (signed)
        items, pages, status, capped = http.pages(DATA, f"/v1beta1/options/snapshots/{sym}", params, h, "snapshots",
                                                  "snapshots")
        snaps = {k: v for k, v in items.items() if isinstance(v, dict)} if isinstance(items, dict) else {}
        n = len(snaps)
        greeks = [v for v in snaps.values() if isinstance(v.get("greeks"), dict)
                  and all(v["greeks"].get(g) is not None for g in ("delta", "gamma", "theta", "vega"))]
        iv = [v for v in snaps.values() if v.get("impliedVolatility") is not None]
        ages = []
        for v in snaps.values():
            q = _iso((v.get("latestQuote") or {}).get("t"))
            if q is not None:
                ages.append((slot - q).total_seconds())
        lat = http.latency_ms.get("snapshots", [])[t0:]
        f = {"http_status": status, "pages": pages, "capped_at_max_pages": capped, "contracts": n,
             "query": {"strike_band_pct": band if spot else None, "expiry_days": days, "feed": "indicative"},
             "snapshot_time": slot.isoformat(timespec="seconds"),
             "greeks_present_pct": _pct(len(greeks), n), "implied_volatility_present_pct": _pct(len(iv), n),
             "quote_age_seconds": {"median": _median(ages), "max": max(ages) if ages else None,
                                   "min": min(ages) if ages else None,
                                   "after_snapshot_time": sum(1 for a in ages if a < 0),
                                   "older_than_5_min_pct": _pct(sum(1 for a in ages if a > 300), len(ages)),
                                   "with_timestamp_pct": _pct(len(ages), n)},
             "latency_ms": {"median": _median(lat), "max": max(lat) if lat else None},
             "filter_counts": {}}
        for b in STRIKE_BANDS_PCT:
            for d in EXPIRY_DAYS:
                cnt = 0
                for k in snaps:
                    p = occ_parts(k)
                    if p and spot and abs(p[3] / spot - 1) * 100 <= b + 1e-9 and 0 <= (p[1] - today).days <= d:
                        cnt += 1
                f["filter_counts"][f"±{b:g}%/≤{d}d"] = cnt
        if spot is None:
            f["note"] = "no spot price (latest IEX trade) — unfiltered by strike; filter counts not measured"
        if market_open is False:
            f["note_market_closed"] = ("market closed: quotes are from the last session — quote ages are not "
                                       "representative of RTH; run again during US market hours")
        facts[sym] = f
        if status != 200 or n == 0:
            worst = "FAIL"
        elif capped or f["greeks_present_pct"] < 90 or f["implied_volatility_present_pct"] < 90 or spot is None \
                or market_open is not True:
            worst = "PARTIAL" if worst == "PASS" else worst
    return {"status": worst, "facts": facts}


def _bars(http: Http, h: dict, sym: str, timeframe: str, start: str, end: Optional[str] = None):
    items, pages, status, capped = http.pages(DATA, "/v1beta1/options/bars",
                                              {"symbols": sym, "timeframe": timeframe, "start": start, "end": end,
                                               "limit": BAR_LIMIT}, h, "bars", "bars")
    bars = (items or {}).get(sym) if isinstance(items, dict) else None
    bars = [b for b in (bars or []) if isinstance(b, dict) and b.get("t")]
    return bars, pages, status


def atm_contract(rows: List[dict], spot: Optional[float], today: date) -> Optional[str]:
    """The call with the strike nearest to spot of the nearest expiry ≥ 7 days out (malformed rows skipped)."""
    if not spot:
        return None
    calls = []
    for c in rows:
        exp = _date(c.get("expiration_date"))
        try:
            k = float(c.get("strike_price"))
        except (TypeError, ValueError):
            continue
        if c.get("type") == "call" and exp is not None and (exp - today).days >= 7:
            calls.append((exp, k, c.get("symbol")))
    if not calls:
        return None
    first = min(e for e, _k, _s in calls)
    return min((x for x in calls if x[0] == first), key=lambda x: abs(x[1] - spot))[2]


def check_bars(http: Http, h: dict, atm: Optional[str], today: date) -> dict:
    facts, worst = {}, "PASS"
    if atm:
        d1, p1, s1 = _bars(http, h, atm, "1Day", (today - timedelta(days=10)).isoformat())
        m1, pm, sm = _bars(http, h, atm, "1Min", (today - timedelta(days=3)).isoformat())
        facts["atm_contract"] = {"symbol": atm, "daily_bars_last_10d": len(d1), "minute_bars_last_3d": len(m1),
                                 "http_status": [s1, sm], "pages": [p1, pm]}
        if s1 != 200 or sm != 200:
            worst = "FAIL"
        elif not d1 or not m1:
            worst = "PARTIAL"
    else:
        facts["atm_contract"] = {"symbol": None, "note": "no ATM contract ≥ 7 days out (contracts or spot missing) — "
                                                         "1Day / 1Min not measured"}
        worst = "FAIL"
    probes = []
    for sym in HISTORY_PROBES:
        p = occ_parts(sym)
        d, pages, st = _bars(http, h, sym, "1Day", "2023-12-01", (p[1] + timedelta(days=1)).isoformat())
        first = min((str(b["t"])[:10] for b in d), default=None)
        probes.append({"symbol": sym, "expiry": p[1].isoformat(), "http_status": st, "daily_bars": len(d),
                       "earliest_bar": first})
        if st != 200:
            worst = "FAIL"
    facts["history_probes"] = probes
    feb = next((x for x in probes if x["symbol"] == "SPY240216C00500000"), {})
    facts["history_since_2024_02"] = bool(feb.get("earliest_bar")) and feb["earliest_bar"] <= "2024-02-16"
    if not facts["history_since_2024_02"] and worst == "PASS":
        worst = "PARTIAL"
    return {"status": worst, "facts": facts}


def check_fred(http: Http, fred_key: Optional[str], key_error: Optional[str] = None) -> dict:
    if key_error:
        return {"status": "FAIL", "facts": {"note": f"the FRED key could not be read from the Credential Manager "
                                                    f"({key_error}) — FRED not checked"}}
    if not fred_key:
        return {"status": "FAIL", "facts": {"note": "no FRED key in the Credential Manager (fred-api-key) — the FRED "
                                                    "series were not checked (no other source used)"}}
    facts, worst = {}, "PASS"
    for sid, label in FRED_SERIES:
        status, data = http.get(FRED, "/fred/series", {"series_id": sid, "api_key": fred_key, "file_type": "json"},
                                None, "fred")
        meta = ((data or {}).get("seriess") or [None])[0] if isinstance(data, dict) else None
        err = str((data or {}).get("error_message") or "")[:200] if isinstance(data, dict) else ""
        f = {"label": label, "http_status": status, "exists": bool(meta) and status == 200}
        if f["exists"]:
            f.update(title=meta.get("title"), frequency=meta.get("frequency"),
                     observation_start=meta.get("observation_start"), observation_end=meta.get("observation_end"))
            st2, obs = http.get(FRED, "/fred/series/observations", {"series_id": sid, "api_key": fred_key,
                                                                    "file_type": "json", "sort_order": "desc",
                                                                    "limit": 1}, None, "fred")
            o = ((obs or {}).get("observations") or [None])[0] if isinstance(obs, dict) else None
            f["latest"] = {"date": (o or {}).get("date"), "value": (o or {}).get("value")} if o else None
        elif status == 400 and "does not exist" in err.lower():
            f["result"] = "not on FRED"
            f["fred_error"] = err
        else:
            f["result"] = "error"                         # auth (key rejected) or another FRED error — not "missing"
            f["fred_error"] = err or f"HTTP {status}"
            worst = "FAIL"
        facts[sid] = f
        if sid in FRED_REQUIRED and not f["exists"]:
            worst = "FAIL"
    short = [s for s in ("VXSTCLS", "VIX9DCLS") if facts.get(s, {}).get("exists")]
    facts["short_term_series_on_fred"] = short or None
    if not short and worst == "PASS":
        worst = "PARTIAL"
        facts["short_term_note"] = "no short-term VIX series found on FRED — VIX term structure = 30-day vs 3-month only"
    return {"status": worst, "facts": facts}


# --- report --------------------------------------------------------------------------------------------------------

def _scrub(obj, secrets_: List[str]):
    text = json.dumps(obj, default=str)
    for s in secrets_:
        if s and len(s) >= 4:
            text = text.replace(s, "***")
    return json.loads(text)


def _data_error(ex: BaseException) -> dict:
    return {"status": "FAIL", "facts": {"data_error": f"{type(ex).__name__}: {str(ex)[:160]}"}}


def run(argv: Optional[List[str]] = None, transport=None, out: Callable[[str], None] = print, secrets_module=None,
        now: Optional[datetime] = None, sleep: Callable[[float], None] = time.sleep, report_dir: Optional[str] = None,
        clock: Callable[[], float] = time.monotonic, wall: Optional[Callable[[], datetime]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv:
        out(f"usage: verify_alpaca_options.py   (no options; unknown: {' '.join(argv)[:80]})")
        return 1
    sec = secrets_module
    if sec is None:
        from strategy_platform import secrets as sec
    try:
        kid, skey = sec.get_secret(KEY_ID), sec.get_secret(SECRET_KEY)
    except Exception as ex:          # noqa: BLE001 — Credential Manager unreadable (or not Windows)
        out(f"FAIL keys: the Credential Manager could not be read ({type(ex).__name__}). Enter the Alpaca paper keys "
            "in Hedge Fund → ⚙ Keys & data → Keys.")
        return 2
    if not kid or not skey:
        out("FAIL keys: Alpaca paper key ID / secret key not set — enter them in Hedge Fund → ⚙ Keys & data → Keys.")
        return 2
    fred_key, fred_key_error = None, None
    try:
        fred_key = sec.get_secret(FRED_KEY)
    except Exception as ex:          # noqa: BLE001 — reported as such, never as "no key"
        fred_key_error = type(ex).__name__
    hide = [kid, skey, fred_key or ""]
    now = now or datetime.now(timezone.utc)
    wall = wall or (lambda: datetime.now(timezone.utc))
    http = Http(transport, sleep, clock)
    h = {"APCA-API-KEY-ID": kid, "APCA-API-SECRET-KEY": skey, "Accept": "application/json"}
    report = {"tool": "verify_alpaca_options", "version": 2, "run_at": now.isoformat(timespec="seconds"),
              "read_only": True, "checks": {}, "stopped": None, "notes": []}
    exit_code = None

    def say(text: str):
        for s in hide:
            if s and len(s) >= 4:
                text = text.replace(s, "***")
        out(text)

    http.progress = say                  # one progress line per page / retry (the user sees it is working)

    def step(name: str, fn):
        try:
            report["checks"][name] = fn()
        except (Stop, RateLimited, Refused):
            raise
        except DATA_ERRORS as ex:    # an unexpected value in the data: this check FAILS with the field — not a stop
            report["checks"][name] = _data_error(ex)

    try:
        st, clk = http.get(TRADING, "/v2/clock", None, h, "clock")
        if st in (401, 403):
            raise Stop(f"Alpaca rejected the paper keys (HTTP {st}) — check them in ⚙ Keys & data → Keys; nothing "
                       "else was requested", code=2)
        ts = _iso(clk.get("timestamp")) if isinstance(clk, dict) and st == 200 else None
        if ts is None:
            raise Stop(f"the Alpaca market clock could not be read (HTTP {st}) — stopped (no date is assumed)")
        today = ts.date()                       # New York date (the clock's own offset)
        market_open = clk.get("is_open") if isinstance(clk.get("is_open"), bool) else None
        report["market"] = {"http_status": st, "is_open": market_open, "next_open": clk.get("next_open"),
                            "date_new_york": today.isoformat()}
        say(f"INFO market {'OPEN' if market_open else 'CLOSED' if market_open is False else 'unknown'} "
            f"(Alpaca clock, New York date {today.isoformat()})"
            + ("" if market_open else " — snapshot freshness and intraday checks are limited"))
        rows: Dict[str, List[dict]] = {s: [] for s in SYMBOLS}

        def a():
            res = check_contracts(http, h, today)
            for s in SYMBOLS:
                rows[s] = res["facts"][s].pop("_rows", [])
            return res
        step("a_contracts", a)
        spots = {s: spot_of(http, h, s)[0] for s in SYMBOLS}
        report["spots_iex"] = spots
        step("b_snapshots", lambda: check_snapshots(http, h, today, spots, wall, market_open))
        step("c_bars", lambda: check_bars(http, h, atm_contract(rows["SPY"], spots.get("SPY"), today), today))

        def d():
            a_f = report["checks"]["a_contracts"]["facts"]
            b_f = report["checks"]["b_snapshots"]["facts"]
            band, days = PROPOSED
            counts = {s: b_f[s]["filter_counts"].get(f"±{band:g}%/≤{days}d") for s in SYMBOLS}
            pages = {s: max(1, math.ceil((counts[s] or 0) / SNAPSHOT_LIMIT)) for s in SYMBOLS}
            return {"status": "PASS" if all(counts.values()) else "PARTIAL",
                    "facts": dict(budget({s: a_f[s]["window"]["pages"] for s in SYMBOLS}, pages),
                                  proposed_filter={"strike_band_pct": band, "expiry_days": days},
                                  proposed_cadence=PROPOSED_CADENCE, contracts_in_proposed_filter=counts)}
        step("d_budget", d)

        def e():
            total = sum(v or 0 for v in report["checks"]["d_budget"]["facts"]["contracts_in_proposed_filter"].values())
            return {"status": "PASS" if total else "PARTIAL", "facts": storage(total)}
        step("e_storage", e)
        step("f_fred", lambda: check_fred(http, fred_key, fred_key_error))
    except RateLimited as ex:
        report["stopped"] = str(ex)
        exit_code = 4
    except Stop as ex:
        report["stopped"] = str(ex)
        exit_code = ex.code
    except Refused as ex:            # a bug in this tool, never a request
        report["stopped"] = str(ex)
        exit_code = 3
    for name in CHECK_NAMES:         # a check that did not run is a FAIL "not run", never silently missing
        report["checks"].setdefault(name, {"status": "FAIL", "facts": {"note": "not run — the run stopped before it"}})
    report["requests"] = {"by_endpoint": dict(http.count), "total": sum(http.count.values()),
                          "retried": list(http.retries),
                          "timeouts_s": {"default": TIMEOUT_SECONDS, "contracts_snapshots_bars": SLOW_TIMEOUT_SECONDS},
                          "contract_page_limit": CONTRACT_LIMIT}
    if report["stopped"]:
        say(f"STOP {report['stopped']}")
    for name in CHECK_NAMES:
        chk = report["checks"][name]
        say(f"{chk['status']:<7} {name}: " + _summary(name, chk))
    report = _scrub(report, hide)
    path = _write(report, report_dir, now)
    say(f"Report: {path}")
    if exit_code is not None:
        say("RESULT: STOPPED" + (" (rate limit)" if exit_code == 4 else " (keys)" if exit_code == 2 else ""))
        return exit_code
    fails = [n for n in CHECK_NAMES if report["checks"][n]["status"] == "FAIL"]
    say("RESULT: OK (no FAIL)" if not fails else f"RESULT: FAIL ({', '.join(fails)})")
    return 0 if not fails else 1


def _summary(name: str, chk: dict) -> str:
    f = chk.get("facts") or {}
    if "note" in f and len(f) == 1:
        return f["note"]
    if "data_error" in f:
        return "data error — " + f["data_error"]
    try:
        if name == "a_contracts":
            return "; ".join(f"{s} {f[s]['window']['contracts']} contracts ≤ {f[s]['window']['query_days']} d "
                             f"({f[s]['full']['contracts']} in all, {f[s]['full']['expiries']} expiries), OI "
                             f"{f[s]['window']['open_interest_present_pct']}% / OI date "
                             f"{f[s]['window']['open_interest_date_present_pct']}%, {f[s]['window']['pages']} page(s)"
                             for s in SYMBOLS if s in f)
        if name == "b_snapshots":
            return "; ".join(f"{s} {f[s]['contracts']} snapshots, greeks {f[s]['greeks_present_pct']}%, IV "
                             f"{f[s]['implied_volatility_present_pct']}%, median quote age "
                             f"{f[s]['quote_age_seconds']['median']} s, {f[s]['pages']} page(s)" for s in SYMBOLS if s in f)
        if name == "c_bars":
            a = f.get("atm_contract") or {}
            return (f"ATM {a.get('symbol')}: {a.get('daily_bars_last_10d')} daily / {a.get('minute_bars_last_3d')} "
                    f"1-min bars; history since 2024-02: {f.get('history_since_2024_02')} "
                    + ", ".join(f"{p['symbol']} earliest {p['earliest_bar']}" for p in f.get("history_probes", [])))
        if name == "d_budget":
            return (f"EOD {f['eod_snapshot_requests']} req, intraday {f['intraday_snapshot_requests']} req/snapshot "
                    f"({f['intraday_minutes_at_share']} min at the archive's {f['archive_share_per_min']} req/min; "
                    f"backend {f['backend_share_per_min']} + burst 10; Alpaca {f['alpaca_limit_per_min']}); filter "
                    f"±{f['proposed_filter']['strike_band_pct']:g}% / ≤{f['proposed_filter']['expiry_days']} d → "
                    f"{f['contracts_in_proposed_filter']}")
        if name == "e_storage":
            return ", ".join(f"{r['intraday_snapshots']}/day: {r['mb_per_year']} MB/year" for r in f["per_day"])
        if name == "f_fred":
            if "note" in f:
                return f["note"]
            return ", ".join(f"{k} " + ("yes" if v.get("exists") else
                                        ("NOT on FRED" if v.get("result") == "not on FRED" else
                                         f"ERROR ({v.get('fred_error')})"))
                             for k, v in f.items() if isinstance(v, dict) and "exists" in v)
    except (KeyError, TypeError):
        pass
    return json.dumps(f, default=str)[:200]


def _write(report: dict, report_dir: Optional[str], now: datetime) -> str:
    folder = report_dir or os.path.join(BACKEND_DIR, "data", "os0")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"verify_alpaca_options_{now.strftime('%Y%m%dT%H%M%SZ')}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    return path


if __name__ == "__main__":
    sys.exit(run())
