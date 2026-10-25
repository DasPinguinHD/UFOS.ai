"""Disclosures — live collection and the daily return job (INSIDER_RET_API.md §6, accepted 2026-10-24).

- ``on_insider_event(event)``: every Form 4 the existing SEC EDGAR collector emits with parsed details and P/S rows
  is also written to ``disclosure_trades`` (origin "live", acceptance time = the feed's <updated>). Idempotent.
- ``run_daily()``: once per trading day at 17:00 New York (16:45 + 15 min for final IEX bars) and at start when the
  last run was missed — never per UI refresh:
    1. issuer SIC codes for new issuers (SEC submissions JSON, through the shared SEC limiter, refreshed after 90 days);
    2. returns of trades that have none yet or still have running horizons (Alpaca bars, research priority so trading
       always goes first); a trade whose issuer SIC is not loaded yet waits for the next run (it is not finalised
       without a benchmark);
    3. point-in-time cluster flags.
  Progress and errors go to ``disclosure_jobs`` ("returns:daily") and show in GET /newsroom/disclosures/status.
  A failed price or SIC fetch is retried at the next run, never skipped silently. No AI calls.

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, time, timedelta, timezone
from typing import Callable, Optional

from strategy_platform.core import calendars

from . import disclosure_db as ddb
from . import disclosure_returns as eng
from . import disclosure_trades as dtr
from . import sector_map
from .sec_limiter import LIMITER

logger = logging.getLogger(__name__)

JOB_DAILY = "returns:daily"
RUN_AT = time(17, 0)                    # New York, trading days
RETENTION_DAYS = 730
SEC_SECRET = "sec-edgar-user-agent"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SEC_TIMEOUT_SECONDS = 30
ERROR_SAMPLES = 5

LIVE_STATE = {"rows_written": 0, "last_at": None, "last_error": None}


class SecUnavailable(RuntimeError):
    """A request to sec.gov failed (the message names the URL and the reason)."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def _parse(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


# --- live ---------------------------------------------------------------------------------------------------------

def on_insider_event(event: dict) -> int:
    """Writes the P/S trades of one collector event; returns how many. Errors are logged and kept in LIVE_STATE
    (shown in the status endpoint) — the collector itself is never disturbed."""
    try:
        details = event.get("details")
        eid = str(event.get("id") or "")
        if not details or not eid.startswith("insider-"):
            return 0
        accepted = _parse(event.get("filed_at"))
        filing_date = calendars.ny_date(accepted).isoformat() if accepted else calendars.ny_date(_utc_now()).isoformat()
        rows = dtr.insider_trades(eid[len("insider-"):], details, form=event.get("form") or "4",
                                  accepted_at=accepted, filing_date=filing_date, origin="live", url=event.get("url"))
        if rows:
            ddb.upsert_trades(rows)
            LIVE_STATE["rows_written"] += len(rows)
            LIVE_STATE["last_at"] = _iso(_utc_now())
        return len(rows)
    except Exception as ex:  # noqa: BLE001 — recorded for the status endpoint
        LIVE_STATE["last_error"] = f"{_iso(_utc_now())}: {type(ex).__name__}: {ex}"
        logger.warning("Disclosures: Form 4 %s not written: %s", event.get("id"), ex)
        return 0


# --- SEC ----------------------------------------------------------------------------------------------------------

def sec_get(url: str, timeout: float = SEC_TIMEOUT_SECONDS) -> bytes:
    """GET with the declared User-Agent (Credential Manager) through the shared limiter. Raises the
    UserFacingError of a missing secret, or SecUnavailable."""
    from strategy_platform import secrets
    from strategy_platform.core import tls
    agent = secrets.require_secret(SEC_SECRET)
    LIMITER.wait()
    req = urllib.request.Request(url, headers={"User-Agent": agent, "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=tls.client_context()) as resp:
            body = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            return body
    except urllib.error.HTTPError as ex:
        raise SecUnavailable(f"{url}: HTTP {ex.code}") from ex
    except (urllib.error.URLError, OSError, TimeoutError) as ex:
        raise SecUnavailable(f"{url}: {type(ex).__name__}: {getattr(ex, 'reason', ex)}") from ex


def fetch_submissions(cik) -> dict:
    """The issuer's submissions JSON (sic, sicDescription, tickers, filings)."""
    try:
        number = int(str(cik).strip())
    except ValueError as ex:
        raise SecUnavailable(f"invalid issuer CIK {cik!r}") from ex
    body = sec_get(SUBMISSIONS_URL.format(cik=number))
    try:
        return json.loads(body)
    except ValueError as ex:
        raise SecUnavailable(f"submissions of CIK {number}: invalid JSON") from ex


def refresh_sic(cik, fetch: Callable[[object], dict] = fetch_submissions) -> dict:
    doc = fetch(cik)
    sic = str(doc.get("sic") or "").strip()
    ddb.sic_put(cik, int(sic) if sic.isdigit() else None, doc.get("sicDescription") or None, doc.get("tickers") or [])
    return ddb.sic_get(cik)


# --- daily job ----------------------------------------------------------------------------------------------------

def run_daily(now: Optional[datetime] = None, prices=None, fetch: Callable[[object], dict] = fetch_submissions,
              source: str = "insider") -> dict:
    from strategy_platform.core.errors import UserFacingError
    from strategy_platform.data import http_gate
    now = now or _utc_now()
    since = (now - timedelta(days=RETENTION_DAYS)).date().isoformat()
    started = _iso(_utc_now())
    ddb.job_put(JOB_DAILY, "running", {"started": started})
    todo = ddb.trades_to_compute(source, since)
    ciks = sorted({t["issuer_cik"] for t in todo if t["measurable"] and t.get("issuer_cik")
                   and ddb.sic_stale(t["issuer_cik"], now)})
    sic_failed = {}
    for cik in ciks:
        try:
            refresh_sic(cik, fetch)
        except (SecUnavailable, UserFacingError) as ex:
            sic_failed[cik] = getattr(ex, "user_message", None) or str(ex)
    prices = prices or eng.AlpacaPrices(now)
    computed, waiting, errors = 0, 0, []
    with http_gate.research_priority():                      # trading and the council always go first
        for t in todo:
            etf = reason = None
            if t["measurable"]:
                if not t.get("issuer_cik"):
                    reason = "issuer CIK unknown"
                else:
                    row = ddb.sic_get(t["issuer_cik"])
                    if row is None:
                        waiting += 1                         # SIC not loaded yet -> next run
                        continue
                    etf, reason = sector_map.etf_for(row["sic"])
            try:
                ddb.replace_returns(t["id"], eng.compute(t, etf, reason, prices, now))
                computed += 1
            except eng.PriceUnavailable as ex:
                errors.append(str(ex))
    ids = dtr.cluster_ids(ddb.trades(source, since_date=since))
    clusters = ddb.set_clusters(source, ids)
    problems = [f"price: {e}" for e in errors[:ERROR_SAMPLES]] + \
               [f"SIC {c}: {m}" for c, m in list(sic_failed.items())[:ERROR_SAMPLES]]
    progress = {"started": started, "finished": _iso(_utc_now()), "trades_checked": len(todo),
                "computed": computed, "waiting_for_sic": waiting, "price_errors": len(errors),
                "sic_fetched": len(ciks) - len(sic_failed), "sic_failed": len(sic_failed), "clusters": clusters,
                "for_close": _iso(calendars.last_close_before(now))}
    state = "done" if not errors and not sic_failed else "partial"
    ddb.job_put(JOB_DAILY, state, progress, "; ".join(problems) or None)
    if problems:
        logger.warning("Disclosures daily job %s: %s", state, "; ".join(problems))
    return dict(progress, state=state)


def latest_slot(now: datetime) -> Optional[datetime]:
    """The most recent scheduled run time (trading day 17:00 New York) at or before ``now``."""
    d = calendars.ny_date(now)
    for _ in range(15):
        if calendars.is_trading_day(d):
            slot = datetime.combine(d, RUN_AT, tzinfo=calendars.NY).astimezone(timezone.utc)
            if slot <= now:
                return slot
        d -= timedelta(days=1)
    return None


def next_slot(now: datetime) -> datetime:
    d = calendars.ny_date(now)
    while True:
        if calendars.is_trading_day(d):
            slot = datetime.combine(d, RUN_AT, tzinfo=calendars.NY).astimezone(timezone.utc)
            if slot > now:
                return slot
        d += timedelta(days=1)


def due(now: datetime, job: Optional[dict]) -> bool:
    """True when the last finished run is older than the latest slot (missed or not run yet)."""
    slot = latest_slot(now)
    if slot is None:
        return False
    finished = _parse(((job or {}).get("progress") or {}).get("finished"))
    return finished is None or finished < slot


async def run_forever(clock: Callable[[], datetime] = _utc_now) -> None:
    """The scheduler task (main.py). Never raises; problems go to the job row and the log."""
    while True:
        now = clock()
        try:
            job = await asyncio.to_thread(ddb.job_get, JOB_DAILY)
            if due(now, job):
                await asyncio.to_thread(run_daily)
                continue
            wait = (next_slot(now) - now).total_seconds()
        except ddb.StoreUnavailable as ex:
            logger.warning("Disclosures daily job paused: %s", ex)
            wait = 3600
        except Exception as ex:  # noqa: BLE001 — recorded, retried in an hour
            logger.exception("Disclosures daily job failed: %s", ex)
            try:
                await asyncio.to_thread(ddb.job_put, JOB_DAILY, "failed", {"failed_at": _iso(clock())},
                                        f"{type(ex).__name__}: {ex}")
            except Exception:  # noqa: BLE001
                pass
            wait = 3600
        await asyncio.sleep(max(30.0, min(wait, 3600.0)))
