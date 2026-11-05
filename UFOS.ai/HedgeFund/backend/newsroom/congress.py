"""Congress trades (STOCK Act) for the Disclosures section — collector + mapping (CONGRESS_API.md, accepted 2026-10-24).

Source (user choice): the kadoa-org/congress-trading-monitor repository's static JSON, refreshed daily by its bot,
fetched at RUNTIME only (``raw.githubusercontent.com``) — nothing of it is ever committed; tests use synthetic rows.
The filings are public records under the Ethics in Government Act (use restrictions: no commercial use, no credit
rating, no solicitation) — personal research only, no redistribution, no public signal feed.

- Scope (user): House + Senate (``source_id`` house_clerk / senate_efd); executive-branch rows (oge_executive) are
  dropped at import. kadoa's own return fields are ignored — returns come from the shared Disclosures engine.
- Pull (decision §10.4): a conditional GET (If-None-Match / ETag) of ``trades.json`` every 6 h and at start — nearly
  free when unchanged. Rows are upserted into ``disclosure_trades`` (source "congress", id ``congress:<row id>``).
- Staleness (decision §10.3): "source not updated since <date>" when the newest ``ingested_at`` of the data is more
  than 3 CALENDAR days old (kadoa skips single days, so one missing day is normal).
- Errors (HTTP, invalid JSON, unexpected shape) fail the pull visibly ("congress source: <reason>"), keep the stored
  rows and retry at the next pull; a row without id / filing_date / filer is skipped and counted with its reason.
- Measurable (decision §10.2): a ticker + an equity/ETF asset type (``MEASURABLE_ASSET_TYPES``); the status lists
  every asset type seen with its count so the Senate codes can be confirmed (pilot) and added to the constant.

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from . import disclosure_db as ddb
from . import disclosure_trades as dtr

logger = logging.getLogger(__name__)

BASE_URL = "https://raw.githubusercontent.com/kadoa-org/congress-trading-monitor/main/public/data/"
TRADES_FILE = "trades.json"
SCOPE = ("house_clerk", "senate_efd")
CHAMBERS = {"house_clerk": "house", "senate_efd": "senate"}
MEASURABLE_ASSET_TYPES = ("ST", "EF")          # House codes stock / ETF; Senate codes added after the pilot
OWNER_TYPES = {None: "self", "": "self", "SELF": "self", "SP": "spouse", "JT": "joint", "DC": "child"}
STALE_DAYS = 3
PULL_INTERVAL = timedelta(hours=6)
TIMEOUT_SECONDS = 60
JOB_PULL = "congress:pull"
TERMS = ("Congress trades: public records under the Ethics in Government Act — personal research only, "
         "no redistribution (source: kadoa-org/congress-trading-monitor, refreshed daily by its bot).")


class SourceError(RuntimeError):
    """The congress source could not be read (the message is shown in the status)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def _day(value) -> Optional[str]:
    try:
        return date.fromisoformat(str(value or "")[:10]).isoformat()
    except ValueError:
        return None


def _num(value) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _code(transaction_type) -> str:
    t = str(transaction_type or "").strip().lower()
    if t == "purchase":
        return "P"
    if t.startswith("sale"):
        return "S"
    return "X"


def _http_url(value) -> Optional[str]:
    text = str(value or "").strip()
    return text if text.startswith(("https://", "http://")) else None


def map_row(row: dict, filer: Optional[dict] = None, now: Optional[str] = None) -> Tuple[Optional[dict], Optional[str]]:
    """(trade row, None) | (None, skip reason). ``filer`` supplies chamber/party/state/office for the per-filer files
    (trades.json rows carry them themselves)."""
    now = now or _iso(_now())
    filer = filer or {}
    source_id = str(row.get("source_id") or "")
    if source_id not in SCOPE:
        return None, f"out of scope ({source_id or 'no source_id'})"
    rid, filed = str(row.get("id") or "").strip(), _day(row.get("filing_date"))
    filer_id = str(row.get("filer_id") or filer.get("id") or "").strip()
    if not rid or not filed or not filer_id:
        return None, "missing id / filing_date / filer_id"
    code = _code(row.get("transaction_type"))
    ticker = dtr.clean_ticker(row.get("ticker"))
    asset_type = str(row.get("asset_type") or "").strip() or None
    if code == "X":
        reason = f"not measurable (transaction type {row.get('transaction_type') or 'unknown'})"
    elif ticker is None:
        reason = "no ticker"
    elif asset_type not in MEASURABLE_ASSET_TYPES:
        reason = f"not measurable (asset type {asset_type or 'unknown'})"
    else:
        reason = None
    owner = str(row.get("owner") or "").strip().upper() or None
    traded = _day(row.get("transaction_date"))
    late = row.get("is_late")
    return {
        "id": f"congress:{rid}", "source": "congress", "accession": str(row.get("filing_id") or "") or None,
        "form": str(row.get("filing_type") or "") or None, "accepted_at": None, "acceptance_state": None,
        "filing_date": filed, "issuer_cik": None, "ticker": ticker, "issuer_name": row.get("asset_name") or None,
        "owner_cik": filer_id, "owner_name": row.get("filer_name") or filer.get("full_name") or None,
        "role_class": None, "roles_raw": None, "co_reporters": 0, "code": code, "shares": None, "value_usd": None,
        "avg_price": None, "trade_date_first": traded, "trade_date_last": traded, "plan_10b5_1": None,
        "plan_source": None, "cluster": 0, "measurable": 0 if reason else 1, "not_measurable_reason": reason,
        "official_url": _http_url(row.get("doc_url")), "origin": "live", "first_seen": now, "updated_at": now,
        "chamber": row.get("chamber") or filer.get("chamber") or CHAMBERS.get(source_id),
        "party": row.get("party") or filer.get("party"), "state": row.get("state") or filer.get("state"),
        "office": row.get("office") or filer.get("office"), "owner_type": OWNER_TYPES.get(owner, "other"),
        "amount_low": _num(row.get("amount_range_low")), "amount_high": _num(row.get("amount_range_high")),
        "amount_label": row.get("amount_range_label") or None,
        "days_to_file": int(row["days_to_file"]) if str(row.get("days_to_file") or "").lstrip("-").isdigit() else None,
        "is_late": None if late in (None, "") else (1 if str(late).strip().lower() in ("1", "true") else 0),
        "asset_type": asset_type, "filing_type": row.get("filing_type") or None,
        "source_ingested_at": str(row.get("ingested_at") or "") or None,
        "comment": row.get("comment") or None,
    }, None


def map_rows(rows: Iterable[dict], filer: Optional[dict] = None, origin: str = "live") -> Tuple[List[dict], Counter]:
    out, skipped = [], Counter()
    for r in rows:
        if not isinstance(r, dict):
            skipped["not an object"] += 1
            continue
        mapped, reason = map_row(r, filer)
        if mapped is None:
            skipped[reason.split(" (")[0] if reason.startswith("out of scope") else reason] += 1
            continue
        mapped["origin"] = origin
        out.append(mapped)
    return out, skipped


# --- fetching -----------------------------------------------------------------------------------------------------

def http_get(url: str, etag: Optional[str] = None, timeout: float = TIMEOUT_SECONDS) -> Tuple[int, Optional[str], bytes]:
    """(status, etag, body) — 304 when the ETag still matches. Raises SourceError."""
    from strategy_platform.core import tls
    headers = {"User-Agent": "UFOS.ai (personal research)", "Accept-Encoding": "identity"}
    if etag:
        headers["If-None-Match"] = etag
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=tls.client_context()) as resp:
            return resp.status, resp.headers.get("ETag"), resp.read()
    except urllib.error.HTTPError as ex:
        if ex.code == 304:
            return 304, etag, b""
        raise SourceError(f"{url}: HTTP {ex.code}") from ex
    except (urllib.error.URLError, OSError, TimeoutError) as ex:
        raise SourceError(f"{url}: {type(ex).__name__}: {getattr(ex, 'reason', ex)}") from ex


def parse_json(body: bytes, what: str):
    try:
        return json.loads(body)
    except ValueError as ex:
        raise SourceError(f"{what}: invalid JSON ({ex})") from ex


def pull(fetch: Callable = http_get, now: Optional[datetime] = None) -> dict:
    """One conditional pull of trades.json; returns the job progress (also stored). Raises SourceError."""
    now = now or _now()
    job = ddb.job_get(JOB_PULL) or {"progress": {}}
    prev = job.get("progress") or {}
    status, etag, body = fetch(BASE_URL + TRADES_FILE, prev.get("etag"))
    progress = dict(prev, last_attempt=_iso(now))
    if status == 304:
        progress.update(last_success=_iso(now), unchanged=True)
        ddb.job_put(JOB_PULL, "done", progress, None)
        return progress
    rows = parse_json(body, TRADES_FILE)
    if isinstance(rows, dict) and isinstance(rows.get("trades"), list):
        rows = rows["trades"]
    if not isinstance(rows, list):
        raise SourceError(f"{TRADES_FILE}: expected a list of trades, got {type(rows).__name__}")
    mapped, skipped = map_rows(rows)
    known = ddb.existing_ids(m["id"] for m in mapped)
    ddb.upsert_trades(mapped)
    new = [m for m in mapped if m["id"] not in known]
    if prev.get("last_success"):                      # never alert on the very first pull (that is an initial load)
        from . import disclosure_alerts
        progress["alerts"] = disclosure_alerts.on_new_trades(new)
    newest = max((r["source_ingested_at"] for r in mapped if r.get("source_ingested_at")), default=None)
    progress.update(etag=etag, last_success=_iso(now), unchanged=False, rows=len(rows), upserted=len(mapped),
                    new=len(new),
                    skipped=dict(skipped), updated_through=max(filter(None, [newest, prev.get("updated_through")]),
                                                              default=None))
    ddb.job_put(JOB_PULL, "done", progress, None)
    return progress


def stale(updated_through: Optional[str], now: Optional[datetime] = None) -> bool:
    """True when the source's newest data is more than STALE_DAYS calendar days old (or unknown)."""
    d = _day(updated_through)
    if d is None:
        return True
    now = now or _now()
    return (now.astimezone(timezone.utc).date() - date.fromisoformat(d)).days > STALE_DAYS


def status(now: Optional[datetime] = None) -> dict:
    now = now or _now()
    job = ddb.job_get(JOB_PULL) or {}
    p = job.get("progress") or {}
    asset_types = ddb.column_counts("congress", "asset_type")
    return {"last_pull": p.get("last_attempt"), "last_success": p.get("last_success"), "state": job.get("state"),
            "last_error": job.get("last_error"), "updated_through": p.get("updated_through"),
            "stale": stale(p.get("updated_through"), now), "rows": ddb.counts("congress")["trades"],
            "skipped": p.get("skipped") or {}, "asset_types": asset_types,
            "measurable_asset_types": list(MEASURABLE_ASSET_TYPES), "terms": TERMS}


async def run_forever(fetch: Callable = http_get, clock: Callable[[], datetime] = _now) -> None:
    """The 6-hour conditional pull (main.py). Never raises; failures are stored and retried at the next interval."""
    while True:
        try:
            await asyncio.to_thread(pull, fetch, clock())
        except ddb.StoreUnavailable as ex:
            logger.warning("Congress pull paused: %s", ex)
        except Exception as ex:  # noqa: BLE001 — shown in the status, retried in 6 h
            msg = f"congress source: {getattr(ex, 'user_message', None) or ex}"
            logger.warning("%s", msg)
            try:
                job = await asyncio.to_thread(ddb.job_get, JOB_PULL)
                prog = dict((job or {}).get("progress") or {}, last_attempt=_iso(clock()))
                await asyncio.to_thread(ddb.job_put, JOB_PULL, "failed", prog, msg)
            except Exception:  # noqa: BLE001
                pass
        await asyncio.sleep(PULL_INTERVAL.total_seconds())
