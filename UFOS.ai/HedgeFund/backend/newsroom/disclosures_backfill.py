"""2-year insider backfill of the Disclosures section (INSIDER_RET_API.md §5 + decisions §11, 2026-10-24 21:05).

Started ONLY by the user ("Start backfill" -> POST /newsroom/disclosures/backfill {"action": "start"}), with progress
and pause/resume. Steps, each resumable (state in ``disclosure_jobs``), gaps recorded with their reason:

1. ``datasets``  SEC "Insider Transactions Data Sets" (quarterly ``YYYYqN_form345.zip``; the download links are read
   from https://www.sec.gov/dera/data/form-345 because their paths differ per quarter) for every quarter that
   overlaps the 2-year window: Form 4 / 4/A rows with open-market P/S codes become trades (origin "backfill",
   acceptance time pending). Live rows are never overwritten.
2. ``pilot``     50 issuers: does the issuer submissions JSON list the Form 4s with their acceptanceDateTime? More
   than ``PILOT_MAX_MISSING`` accessions missing -> the backfill STOPS with state "needs_decision" (lead decision
   §11.2: stop and ask).
3. ``acceptance`` the submissions JSON of every issuer with pending trades (recent list + the older ``files[]``
   pages back to the window start): acceptance times ('found'), accessions not listed -> 'missing' (their
   from-filing returns are "acceptance time unknown"); the issuer SIC is stored on the way.
4. ``gap``       the days after the newest data set up to the first live day: EDGAR daily index ``form.YYYYMMDD.idx``
   -> one complete-submission ``.txt`` per Form 4 (acceptance time from its header, XML parsed like the live
   collector).
5. ``prices``    returns of the backfilled trades through the Alpaca gate — only outside 09:00–16:30 New York on
   trading days, inside ``research_priority`` (trading and the council always go first), grouped by ticker so each
   symbol's daily bars are fetched once (shared bar cache); minute bars only for in-session acceptances.

The EDGAR steps run with the shared SEC limiter lowered to 0.2 s (5 req/s, decision §11.4) and restore the
configured interval afterwards. Never an AI call.

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import threading
import time as _time
import zipfile
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from strategy_platform.core import calendars

from . import disclosure_db as ddb
from . import disclosure_returns as eng
from . import disclosure_trades as dtr
from . import disclosures_service as svc
from . import sector_map
from .sec_limiter import BACKFILL_INTERVAL_SECONDS, LIMITER

logger = logging.getLogger(__name__)

STATE_KEY = "backfill:state"
STEPS = ("datasets", "pilot", "acceptance", "gap", "prices")
DATASETS_PAGE = "https://www.sec.gov/dera/data/form-345"
DATASET_LINK_RE = re.compile(r'href="([^"]*?/(\d{4})q([1-4])_form345\.zip)"', re.IGNORECASE)
DAILY_INDEX_URL = "https://www.sec.gov/Archives/edgar/daily-index/{y}/QTR{q}/form.{ymd}.idx"
ARCHIVE_URL = "https://www.sec.gov/Archives/{path}"
PILOT_ISSUERS = 50
PILOT_MAX_MISSING = 2
PRICE_BATCH = 500
MARKET_BLOCK = (time(9, 0), time(16, 30))       # New York, trading days: the Alpaca part waits
_TRUE = {"1", "true", "y", "yes"}
_MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV",
                                       "DEC"), start=1)}
# the live collector's 10b5-1 footnote rule (sec_edgar_collector._10B5_1_RE / _10B5_1_NEGATED_RE; a test keeps them equal)
_10B5_1_RE = re.compile(r"10b5\s*-\s*1", re.IGNORECASE)
_10B5_1_NEGATED_RE = re.compile(r"\bnot\b[^.;]{0,60}10b5\s*-\s*1", re.IGNORECASE)


class Paused(Exception):
    """The user paused the backfill (raised between two items; progress is kept)."""


class NeedsDecision(Exception):
    """The pilot found missing acceptance times — the backfill stops and asks (decision §11.2)."""


# --- data sets ------------------------------------------------------------------------------------------------------

def quarters_for(start: date, end: date) -> List[Tuple[int, int]]:
    out, y, q = [], start.year, (start.month - 1) // 3 + 1
    while (y, q) <= (end.year, (end.month - 1) // 3 + 1):
        out.append((y, q))
        y, q = (y + 1, 1) if q == 4 else (y, q + 1)
    return out


def dataset_links(html: str) -> Dict[Tuple[int, int], str]:
    out = {}
    for href, y, q in DATASET_LINK_RE.findall(html or ""):
        url = href if href.startswith("http") else "https://www.sec.gov" + (href if href.startswith("/") else "/" + href)
        out[(int(y), int(q))] = url
    return out


def _dset_date(text: str) -> Optional[str]:
    """DD-MON-YYYY (data sets) -> YYYY-MM-DD."""
    m = re.fullmatch(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})", (text or "").strip())
    if not m or m.group(2).upper() not in _MONTHS:
        return None
    try:
        return date(int(m.group(3)), _MONTHS[m.group(2).upper()], int(m.group(1))).isoformat()
    except ValueError:
        return None


def _num(text) -> Optional[float]:
    try:
        return float(str(text).strip()) if str(text or "").strip() else None
    except ValueError:
        return None


def _tsv(zf: zipfile.ZipFile, name: str) -> Iterable[dict]:
    member = next((n for n in zf.namelist() if n.rsplit("/", 1)[-1].upper() == name), None)
    if member is None:
        raise ValueError(f"data set zip has no {name}")
    with zf.open(member) as fh:
        text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace", newline="")
        yield from csv.DictReader(text, delimiter="\t", quoting=csv.QUOTE_NONE)


def _roles(rel: str, title: str, other: str) -> List[str]:
    parts = {p.strip().upper() for p in (rel or "").replace(";", ",").split(",") if p.strip()}
    roles = []
    if "DIRECTOR" in parts:
        roles.append("Director")
    if "OFFICER" in parts:
        roles.append((title or "").strip() or "Officer")
    if "TENPERCENTOWNER" in parts or "TEN PERCENT OWNER" in parts:
        roles.append("10% owner")
    if "OTHER" in parts:
        roles.append((other or "").strip() or "Other")
    return roles


def parse_dataset(raw: bytes, since: str, until: Optional[str] = None) -> List[dict]:
    """Trades of one quarterly data set zip: Form 4 / 4/A with P or S non-derivative rows, filed since..until."""
    from .sec_edgar_collector import pretty_company_name
    zf = zipfile.ZipFile(io.BytesIO(raw))
    subs = {}
    for r in _tsv(zf, "SUBMISSION.TSV"):
        form = (r.get("DOCUMENT_TYPE") or "").strip()
        filed = _dset_date(r.get("FILING_DATE"))
        if form not in ("4", "4/A") or not filed or filed < since or (until and filed > until):
            continue
        subs[r["ACCESSION_NUMBER"]] = {"form": form, "filed": filed, "cik": (r.get("ISSUERCIK") or "").strip(),
                                       "name": (r.get("ISSUERNAME") or "").strip(),
                                       "ticker": (r.get("ISSUERTRADINGSYMBOL") or "").strip(),
                                       "aff": (r.get("AFF10B5ONE") or "").strip().lower() in _TRUE}
    txs: Dict[str, list] = {}
    for r in _tsv(zf, "NONDERIV_TRANS.TSV"):
        acc = r.get("ACCESSION_NUMBER")
        code = (r.get("TRANS_CODE") or "").strip().upper()
        if acc in subs and code in ("P", "S"):
            txs.setdefault(acc, []).append({"code": code, "derivative": False, "date": _dset_date(r.get("TRANS_DATE")),
                                            "shares": _num(r.get("TRANS_SHARES")),
                                            "price": _num(r.get("TRANS_PRICEPERSHARE"))})
    owners: Dict[str, list] = {}
    for r in _tsv(zf, "REPORTINGOWNER.TSV"):
        if r.get("ACCESSION_NUMBER") in txs:
            owners.setdefault(r["ACCESSION_NUMBER"], []).append(r)
    footnote_plan = set()
    for r in _tsv(zf, "FOOTNOTES.TSV"):
        acc = r.get("ACCESSION_NUMBER")
        text = r.get("FOOTNOTE_TXT") or ""
        if acc in txs and _10B5_1_RE.search(text) and not _10B5_1_NEGATED_RE.search(text):
            footnote_plan.add(acc)
    out = []
    for acc, rows in txs.items():
        s = subs[acc]
        own = owners.get(acc) or [{}]
        first = own[0]
        plan_source = "checkbox" if s["aff"] else ("footnote" if acc in footnote_plan else None)
        details = {"issuer_name": pretty_company_name(s["name"], s["ticker"]), "issuer_ticker": s["ticker"],
                   "issuer_cik": s["cik"], "owner_name": (first.get("RPTOWNERNAME") or "").strip(),
                   "owner_cik": (first.get("RPTOWNERCIK") or "").strip(),
                   "owner_roles": _roles(first.get("RPTOWNER_RELATIONSHIP"), first.get("RPTOWNER_TITLE"),
                                         first.get("RPTOWNER_TXT")),
                   "co_reporters": max(0, len(own) - 1), "transactions": rows,
                   "plan_10b5_1": plan_source is not None, "plan_10b5_1_source": plan_source}
        out += dtr.insider_trades(acc, details, form=s["form"], accepted_at=None, filing_date=s["filed"],
                                  origin="backfill")
    return out


# --- acceptance times -----------------------------------------------------------------------------------------------

def _acceptance_map(block: dict) -> Dict[str, str]:
    accs, times = block.get("accessionNumber") or [], block.get("acceptanceDateTime") or []
    out = {}
    for acc, when in zip(accs, times):
        dt = svc._parse(when)
        if dt is not None:
            out[acc] = dt.isoformat(timespec="seconds")
    return out


def issuer_acceptance(cik, wanted: Iterable[str], since: str, fetch_json: Callable[[str], dict]) -> Tuple[dict, dict]:
    """({accession: acceptance UTC ISO}, submissions doc) for the wanted accessions of one issuer: the recent list,
    then older pages (``files[]``) while accessions are missing and the page reaches the window."""
    number = int(str(cik).strip())
    doc = fetch_json(svc.SUBMISSIONS_URL.format(cik=number))
    want = set(wanted)
    found = {a: t for a, t in _acceptance_map((doc.get("filings") or {}).get("recent") or {}).items() if a in want}
    for page in (doc.get("filings") or {}).get("files") or []:
        if want <= set(found):
            break
        if str(page.get("filingTo") or "9999") < since:
            continue
        older = fetch_json("https://data.sec.gov/submissions/" + str(page["name"]))
        found.update({a: t for a, t in _acceptance_map(older).items() if a in want})
    return found, doc


# --- gap days -------------------------------------------------------------------------------------------------------

_HDR_ACCEPT = re.compile(rb"<ACCEPTANCE-DATETIME>\s*(\d{14})")
_HDR_TYPE = re.compile(rb"CONFORMED SUBMISSION TYPE:\s*(\S+)")
_XML = re.compile(rb"<XML>\s*(.*?)\s*</XML>", re.S)


def daily_index_paths(raw: bytes) -> List[Tuple[str, str]]:
    """[(form, archive path)] of Form 4 / 4/A lines of a ``form.YYYYMMDD.idx`` (one per accession)."""
    out, seen = [], set()
    for line in raw.decode("latin-1").splitlines():
        m = re.match(r"^(4|4/A)\s{2,}.*?\s(edgar/data/\S+\.txt)\s*$", line)
        if m and m.group(2) not in seen:
            seen.add(m.group(2))
            out.append((m.group(1), m.group(2)))
    return out


def parse_submission_txt(raw: bytes) -> Tuple[Optional[str], Optional[str], Optional[bytes]]:
    """(form, acceptance UTC ISO, ownership XML) of a complete-submission .txt (acceptance is New York time)."""
    m_type, m_acc, m_xml = _HDR_TYPE.search(raw), _HDR_ACCEPT.search(raw), _XML.search(raw)
    accepted = None
    if m_acc:
        local = datetime.strptime(m_acc.group(1).decode(), "%Y%m%d%H%M%S").replace(tzinfo=calendars.NY)
        accepted = local.astimezone(timezone.utc).isoformat(timespec="seconds")
    return (m_type.group(1).decode() if m_type else None), accepted, (m_xml.group(1) if m_xml else None)


# --- the controller -------------------------------------------------------------------------------------------------

class Backfill:
    """One backfill per process; the work runs in a daemon thread, pause is checked between items."""

    def __init__(self, fetch: Callable[[str], bytes] = svc.sec_get, prices_factory=eng.AlpacaPrices,
                 clock: Callable[[], datetime] = svc._utc_now, sleep: Callable[[float], None] = _time.sleep):
        self._fetch, self._prices_factory, self._clock, self._sleep = fetch, prices_factory, clock, sleep
        self._lock = threading.Lock()
        self._pause = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # state ---------------------------------------------------------------------------------------------------------
    def state(self) -> dict:
        job = ddb.job_get(STATE_KEY) or {"state": "not_started", "progress": {}, "last_error": None}
        return job

    def _set(self, state: str, last_error: Optional[str] = None, **progress) -> None:
        current = (ddb.job_get(STATE_KEY) or {}).get("progress") or {}
        current.update(progress)
        ddb.job_put(STATE_KEY, state, current, last_error)

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def recover_after_restart(self) -> None:
        """A backfill that was running when the backend stopped is shown as paused (the user resumes it)."""
        if self.state().get("state") == "running" and not self.running():
            self._set("paused", "backend restarted — press Resume to continue")

    # control -------------------------------------------------------------------------------------------------------
    def control(self, action: str) -> Tuple[bool, str]:
        """(ok, message). start: not_started|failed|complete -> running; pause: running -> paused;
        resume: paused|needs_decision(after a decision) -> running."""
        with self._lock:
            state = self.state().get("state")
            if action == "start":
                if state not in ("not_started", "failed", "complete") or self.running():
                    return False, f"the backfill is {state} — use resume or pause"
            elif action == "resume":
                if state not in ("paused",) or self.running():
                    return False, f"the backfill is {state} — nothing to resume"
            elif action == "pause":
                if state != "running" or not self.running():
                    return False, f"the backfill is {state} — nothing to pause"
                self._pause.set()
                return True, "pausing after the current item"
            else:
                return False, "action must be start, pause or resume"
            self._pause.clear()
            if action == "start":
                # a new run repeats every step cheaply: finished data sets / gap days are skipped by their 'done'
                # lists, only pending acceptance times, gaps and trades without returns are worked on again
                for step in STEPS:
                    j = ddb.job_get(f"backfill:{step}")
                    if j and j["state"] == "done":
                        ddb.job_put(f"backfill:{step}", "rerun", j.get("progress"), j.get("last_error"))
            started = svc._iso(self._clock()) if action == "start" else (self.state().get("progress") or {}).get("started")
            self._set("running", None, started=started)
            self._thread = threading.Thread(target=self._run, name="disclosures-backfill", daemon=True)
            self._thread.start()
            return True, "running"

    def _check_pause(self) -> None:
        if self._pause.is_set():
            raise Paused()

    def _run(self) -> None:
        try:
            for step in STEPS:
                job = ddb.job_get(f"backfill:{step}")
                if job and job["state"] == "done":
                    continue
                self._set("running", None, step=step)
                getattr(self, "_step_" + step)()
                ddb.job_put(f"backfill:{step}", "done", dict((ddb.job_get(f"backfill:{step}") or {}).get("progress")
                                                            or {}, finished=svc._iso(self._clock())))
            self._set("complete", None, step=None, finished=svc._iso(self._clock()))
        except Paused:
            self._set("paused", None)
        except NeedsDecision as ex:
            self._set("needs_decision", str(ex))
        except Exception as ex:  # noqa: BLE001 — shown in the status; start again to retry
            logger.exception("Disclosures backfill failed: %s", ex)
            self._set("failed", f"{type(ex).__name__}: {getattr(ex, 'user_message', None) or ex}")

    # helpers -------------------------------------------------------------------------------------------------------
    def _window(self) -> Tuple[date, date]:
        today = calendars.ny_date(self._clock())
        return today - timedelta(days=svc.RETENTION_DAYS), today

    def _edgar(self, url: str) -> bytes:
        with LIMITER.override(BACKFILL_INTERVAL_SECONDS):
            return self._fetch(url)

    def _json(self, url: str) -> dict:
        return json.loads(self._edgar(url))

    def _progress(self, step: str, **kw) -> dict:
        job = ddb.job_get(f"backfill:{step}") or {"progress": {}}
        progress = job.get("progress") or {}
        progress.update(kw)
        ddb.job_put(f"backfill:{step}", "running", progress, job.get("last_error"))
        return progress

    def _gap(self, step: str, key: str, reason: str) -> None:
        progress = (ddb.job_get(f"backfill:{step}") or {}).get("progress") or {}
        gaps = dict(progress.get("gaps") or {})
        gaps[key] = reason
        self._progress(step, gaps=gaps)

    # steps ---------------------------------------------------------------------------------------------------------
    def _step_datasets(self) -> None:
        start, end = self._window()
        links = dataset_links(self._edgar(DATASETS_PAGE).decode("utf-8", "replace"))
        wanted = [q for q in quarters_for(start, end) if q in links]
        done = set(((ddb.job_get("backfill:datasets") or {}).get("progress") or {}).get("done") or [])
        newest = max(wanted) if wanted else None
        self._progress("datasets", total=len(wanted), newest=f"{newest[0]}q{newest[1]}" if newest else None,
                       done=sorted(done))
        for y, q in wanted:
            key = f"{y}q{q}"
            if key in done:
                continue
            self._check_pause()
            try:
                rows = parse_dataset(self._edgar(links[(y, q)]), since=start.isoformat())
            except (svc.SecUnavailable, ValueError, zipfile.BadZipFile) as ex:
                self._gap("datasets", key, str(ex))
                continue
            ddb.upsert_trades(rows, keep_existing=True)
            done.add(key)
            self._progress("datasets", done=sorted(done), trades=(
                ((ddb.job_get("backfill:datasets") or {}).get("progress") or {}).get("trades", 0) + len(rows)))
        gaps = ((ddb.job_get("backfill:datasets") or {}).get("progress") or {}).get("gaps") or {}
        if gaps and not done:
            raise RuntimeError(f"no data set could be loaded: {gaps}")

    def _step_pilot(self) -> None:
        start, _end = self._window()
        pending = ddb.pending_acceptance()
        ciks = [c for c in sorted(pending) if c][:PILOT_ISSUERS]
        missing, checked = [], 0
        for cik in ciks:
            self._check_pause()
            found, doc = issuer_acceptance(cik, pending[cik], start.isoformat(), self._json)
            sic = str(doc.get("sic") or "").strip()
            ddb.sic_put(cik, int(sic) if sic.isdigit() else None, doc.get("sicDescription"), doc.get("tickers") or [])
            ddb.set_acceptance(found, [a for a in pending[cik] if a not in found])
            checked += len(pending[cik])
            missing += [a for a in pending[cik] if a not in found]
        self._progress("pilot", issuers=len(ciks), accessions=checked, missing=len(missing), samples=missing[:10])
        if len(missing) > PILOT_MAX_MISSING:
            ddb.job_put("backfill:pilot", "needs_decision", (ddb.job_get("backfill:pilot") or {}).get("progress"),
                        f"{len(missing)} of {checked} accessions not listed in the issuer submissions")
            raise NeedsDecision(f"Pilot: {len(missing)} of {checked} Form 4 accessions have no acceptance time in the "
                                f"issuer submissions — decide: per-filing index headers (~200,000 requests) or keep "
                                f"them 'acceptance time unknown'.")

    def _step_acceptance(self) -> None:
        start, _end = self._window()
        pending = ddb.pending_acceptance()
        total = len(pending)
        self._progress("acceptance", total=total, remaining=total)
        for i, (cik, accs) in enumerate(sorted(pending.items())):
            self._check_pause()
            if not cik:
                ddb.set_acceptance({}, accs)
                continue
            try:
                found, doc = issuer_acceptance(cik, accs, start.isoformat(), self._json)
            except (svc.SecUnavailable, ValueError) as ex:
                self._gap("acceptance", f"CIK {cik}", str(ex))
                continue                                      # stays pending -> retried when the step runs again
            sic = str(doc.get("sic") or "").strip()
            ddb.sic_put(cik, int(sic) if sic.isdigit() else None, doc.get("sicDescription"), doc.get("tickers") or [])
            ddb.set_acceptance(found, [a for a in accs if a not in found])
            if i % 25 == 0:
                self._progress("acceptance", remaining=total - i - 1)
        self._progress("acceptance", remaining=len(ddb.pending_acceptance()))

    def _gap_days(self) -> List[date]:
        newest = ((ddb.job_get("backfill:datasets") or {}).get("progress") or {}).get("newest")
        start, today = self._window()
        if newest:
            y, q = int(newest[:4]), int(newest[-1])
            first = date(y, 3 * q, 1) + timedelta(days=31)
            first = date(first.year, first.month, 1) - timedelta(days=1)     # last day of the quarter (5:30 pm cutoff)
        else:
            first = start
        live = ddb.query_trades("insider", [("origin = ?", "live")], limit=1, order="filing_date ASC")[0]
        yesterday = today - timedelta(days=1)                 # today's daily index is published at night
        # the first live day too (the live collector may have started mid-day; known rows are kept as they are)
        last = min(date.fromisoformat(live[0]["filing_date"]), yesterday) if live else yesterday
        return [d for d in calendars.trading_days(first, last)] if first <= last else []

    def _step_gap(self) -> None:
        from .sec_edgar_collector import _parse_form4
        days = self._gap_days()
        done = set(((ddb.job_get("backfill:gap") or {}).get("progress") or {}).get("done") or [])
        self._progress("gap", total=len(days), done=sorted(done))
        for d in days:
            key = d.isoformat()
            if key in done:
                continue
            self._check_pause()
            url = DAILY_INDEX_URL.format(y=d.year, q=(d.month - 1) // 3 + 1, ymd=d.strftime("%Y%m%d"))
            try:
                paths = daily_index_paths(self._edgar(url))
            except svc.SecUnavailable as ex:
                self._gap("gap", key, str(ex))
                continue
            rows, failed = [], 0
            for form, path in paths:
                self._check_pause()
                acc = path.rsplit("/", 1)[-1][:-4]
                try:
                    kind, accepted, xml = parse_submission_txt(self._edgar(ARCHIVE_URL.format(path=path)))
                    if xml is None:
                        raise ValueError("no ownership XML")
                    details = _parse_form4(xml)
                except Exception:  # noqa: BLE001 — counted per day, the day is still marked done with the count
                    failed += 1
                    continue
                rows += dtr.insider_trades(acc, details, form="4/A" if (kind or form) == "4/A" else "4",
                                           accepted_at=accepted, filing_date=key, origin="backfill")
            ddb.upsert_trades(rows, keep_existing=True)
            done.add(key)
            if failed:
                self._gap("gap", key, f"{failed} of {len(paths)} filings could not be read")
            self._progress("gap", done=sorted(done))

    def _market_blocked(self) -> bool:
        now = self._clock()
        d = calendars.ny_date(now)
        if not calendars.is_trading_day(d):
            return False
        local = now.astimezone(calendars.NY).time()
        return MARKET_BLOCK[0] <= local < MARKET_BLOCK[1]

    def _wait_for_market_close(self) -> None:
        self._progress("prices", waiting="US market hours — continues after 16:30 New York")
        for _ in range(60):                                   # 5 min in 5-s steps, so pause stays responsive
            self._check_pause()
            self._sleep(5)

    def _step_prices(self) -> None:
        from strategy_platform.data import http_gate
        start, _end = self._window()
        failed_tickers: set = set()                           # price source failed this run -> retried next start
        while True:
            self._check_pause()
            if self._market_blocked():
                self._wait_for_market_close()
                continue
            batch = [t for t in ddb.trades_to_compute("insider", start.isoformat(), new_backfill=True,
                                                      limit=PRICE_BATCH + len(failed_tickers) * 50,
                                                      order="t.ticker, t.filing_date, t.id")
                     if (t["ticker"] or "") not in failed_tickers][:PRICE_BATCH]
            if not batch:
                break
            # issuers without a SIC yet (e.g. gap-day filings never went through the acceptance step)
            for cik in sorted({t["issuer_cik"] for t in batch if t["measurable"] and t.get("issuer_cik")
                               and ddb.sic_get(t["issuer_cik"]) is None}):
                self._check_pause()
                try:
                    svc.refresh_sic(cik, lambda c: self._json(svc.SUBMISSIONS_URL.format(cik=int(str(c)))))
                except Exception as ex:  # noqa: BLE001 — recorded; these trades wait for the next start
                    self._gap("prices", f"SIC {cik}", getattr(ex, "user_message", None) or str(ex))
                    failed_tickers.update(t["ticker"] or "" for t in batch if t.get("issuer_cik") == cik)
            batch = [t for t in batch if (t["ticker"] or "") not in failed_tickers]
            if not batch:
                continue
            prices = self._prices_factory(self._clock())
            ok = errors = 0
            with http_gate.research_priority():
                for t in batch:
                    self._check_pause()
                    if (t["ticker"] or "") in failed_tickers:
                        continue
                    etf = reason = None
                    if t["measurable"]:
                        row = ddb.sic_get(t["issuer_cik"]) if t.get("issuer_cik") else None
                        etf, reason = sector_map.etf_for(row["sic"]) if row else (None, "SIC code unknown")
                    try:
                        ddb.replace_returns(t["id"], eng.compute(t, etf, reason, prices, self._clock()))
                        ok += 1
                    except eng.PriceUnavailable as ex:
                        errors += 1
                        failed_tickers.add(t["ticker"] or "")
                        self._gap("prices", t["ticker"] or t["id"], str(ex))
            if errors and not ok:
                raise RuntimeError(f"price source unavailable for a whole batch ({errors} tickers) — "
                                   f"start the backfill again later")
            progress = (ddb.job_get("backfill:prices") or {}).get("progress") or {}
            self._progress("prices", done=progress.get("done", 0) + ok, errors=progress.get("errors", 0) + errors,
                           waiting=None)


BACKFILL = Backfill()


def status() -> dict:
    """The backfill part of GET /newsroom/disclosures/status."""
    job = BACKFILL.state()
    steps = []
    for step in STEPS:
        j = ddb.job_get(f"backfill:{step}") or {}
        p = j.get("progress") or {}
        steps.append({"key": step, "state": j.get("state") or "pending", "progress": p,
                      "gaps": [{"key": k, "reason": v} for k, v in sorted((p.get("gaps") or {}).items())]})
    return {"state": job.get("state"), "last_error": job.get("last_error"), "progress": job.get("progress") or {},
            "steps": steps, "running": BACKFILL.running(),
            "estimate": {"requests_sec": 6000 + 14500 + 8, "requests_alpaca": 5000 + 90000,
                         "hours": "EDGAR ~1.2 h at 0.2 s; Alpaca daily ~1–1.5 h, minute entries ~10–25 h outside "
                                  "US market hours"}}
