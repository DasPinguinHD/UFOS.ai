"""SEC EDGAR — insider trades (Form 4 filings).

Free, public domain (US government work product), no API key. The only real
requirement is SEC's Fair Access policy: every automated requester must send
a descriptive User-Agent (name + contact email) — set SEC_EDGAR_USER_AGENT in
.env or this collector refuses to run rather than risk being throttled/blocked
under a generic identity.

"Real-time" caveat: insiders must file Form 4 within 2 business days of a
trade. That's a legal ceiling, not a data-source limitation — polling more
often than the feed itself updates buys nothing.

2026-09-24: parses the actual Form 4 document for each filing instead of
only the Atom feed's one-line title. The feed alone says "4 - Caras Matthew
L (0001609077) (Reporting)" and a timestamp — no issuer, no role, no
buy/sell, no size — which left the Insider Trades popup's AI summary
nothing to rank or compare. For each NEW accession number this now fetches
the filing directory's index.json, finds the Form 4 XML, and extracts:
issuer + ticker, reporting owner + CIK, relationship (director/officer
title/10% owner), and every non-derivative transaction (code, shares,
price, acquired/disposed, holdings afterwards). Parsed details are cached
per accession, so each filing costs its two extra requests exactly once.

Two cleanups came with it: the Atom feed lists every filing twice (once
for the reporting person, once for the issuer) and occasionally mixes in
other form types (e.g. 424B2) — both are now filtered, one event per Form 4.

2026-09-25: each filing also carries plan_10b5_1 (+ plan_10b5_1_source
"checkbox"/"footnote") — trades under a Rule 10b5-1 plan were scheduled in
advance and are marked "pre-planned" on the card and in the AI table.

Moved from Global Monitoring to the HedgeFund module's Financial Newsroom
on 2026-09-24 — insider trades have no meaningful map position (EDGAR gives
no issuer location), so they never belonged on the globe. Events carry no
lat/lon here.
"""
import asyncio
import logging
import re
import xml.etree.ElementTree as ET

import aiohttp

from config import SEC_EDGAR_USER_AGENT, SEC_EDGAR_POLL_SECONDS, SEC_EDGAR_MIN_REQUEST_INTERVAL_SECONDS
from .store import make_event

logger = logging.getLogger(__name__)

FEED_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

# Plain-English meaning of Form 4 transaction codes (SEC Form 4 General
# Instructions, section 8). Only the common ones; anything else is shown as
# its raw code.
TRANSACTION_CODES = {
    "P": "open-market purchase",
    "S": "open-market sale",
    "A": "grant/award",
    "M": "option exercise",
    "F": "shares withheld for tax",
    "G": "gift",
    "D": "disposition to issuer",
    "C": "conversion",
    "X": "option exercise (in the money)",
    "J": "other",
}

_ACCESSION_RE = re.compile(r"accession-number=([0-9-]+)")

# Rule 10b5-1 trading plans (2026-09-25). A trade executed under a 10b5-1
# plan was scheduled months in advance, so it says little about what the
# insider thinks NOW. Since April 2023 (schema X0508) Form 4 has a root-level
# checkbox <aff10b5One> ("true"/"false", sometimes "1"/"0"); older filings
# and many current ones also (or only) say so in a footnote ("...effected
# pursuant to a Rule 10b5-1 trading plan adopted on ...").
_10B5_1_RE = re.compile(r"10b5\s*-\s*1", re.IGNORECASE)
# "...were NOT made pursuant to a Rule 10b5-1 plan" must not count.
_10B5_1_NEGATED_RE = re.compile(r"\bnot\b[^.;]{0,60}10b5\s*-\s*1", re.IGNORECASE)

# accession -> parsed details dict (or None if parsing failed — don't retry
# forever). Bounded so a long-running process doesn't grow without limit.
_details_cache: dict = {}
_DETAILS_CACHE_MAX = 500


# EDGAR usually spells issuer names in ALL CAPS ("BAR HARBOR BANKSHARES").
# pretty_company_name() turns that into "Bar Harbor Bankshares" for the
# popup cards and the AI summary, keeping real acronyms (IBM, AT&T, LLC,
# N.V.) and lower-casing small connector words.
_KEEP_UPPER = {"LLC", "LP", "LLP", "PLC", "NV", "N.V.", "SA", "S.A.", "AG", "SE", "ETF",
               "REIT", "USA", "US", "UK", "II", "III", "IV", "AT&T", "N.A.", "BDC"}
_SMALL_WORDS = {"OF", "AND", "THE", "FOR", "IN", "ON", "AT", "&"}
_KNOWN_CASE = {"INC": "Inc", "INC.": "Inc.", "CORP": "Corp", "CORP.": "Corp.", "CO": "Co",
               "CO.": "Co.", "LTD": "Ltd", "LTD.": "Ltd.", "HOLDINGS": "Holdings", "GROUP": "Group"}


def pretty_company_name(name: str, ticker: str = "") -> str:
    # EDGAR appends the state of incorporation to some names ("BANK OF
    # AMERICA CORP /DE/") — noise for a reader, drop it.
    name = re.sub(r"\s*/[A-Za-z]{2,3}/\s*$", "", name or "").strip()
    if not name or name != name.upper():
        return name  # already mixed case (or empty): leave it alone
    ticker = (ticker or "").upper()
    out = []
    for i, word in enumerate(name.split()):
        bare = word.strip(",")
        if bare in _KNOWN_CASE:
            new = _KNOWN_CASE[bare]
        elif (bare in _KEEP_UPPER or (ticker and bare == ticker)
              or (len(bare) <= 2 and bare.isalpha() and bare not in _SMALL_WORDS)
              or (len(bare) <= 4 and bare.isalpha() and not any(v in bare for v in "AEIOUY"))):
            new = bare                     # acronyms: the ticker itself (AIAI, IBM), vowel-less codes (BHB)
        elif bare in _SMALL_WORDS and i > 0:
            new = bare.lower() if bare != "&" else "&"
        else:
            new = "-".join(part.capitalize() for part in bare.split("-"))
        out.append(new + ("," if word.endswith(",") else ""))
    return " ".join(out)


def _txt(el, path):
    found = el.find(path) if el is not None else None
    return (found.text or "").strip() if found is not None and found.text else ""


def _num(el, path):
    try:
        return float(_txt(el, path))
    except ValueError:
        return None


def _parse_form4(xml_text) -> dict:
    root = ET.fromstring(xml_text)
    if root.tag != "ownershipDocument":
        raise ValueError(f"not a Form 4 ownershipDocument (root <{root.tag}>)")

    issuer = root.find("issuer")
    owner = root.find("reportingOwner")
    rel = owner.find("reportingOwnerRelationship") if owner is not None else None

    roles = []
    if _txt(rel, "isDirector") in ("1", "true"):
        roles.append("Director")
    if _txt(rel, "isOfficer") in ("1", "true"):
        roles.append(_txt(rel, "officerTitle") or "Officer")
    if _txt(rel, "isTenPercentOwner") in ("1", "true"):
        roles.append("10% owner")
    if _txt(rel, "isOther") in ("1", "true"):
        roles.append(_txt(rel, "otherText") or "Other")

    # Both tables: plain shares (nonDerivative) and options/RSUs/warrants
    # (derivative). securityTitle is WHAT was traded ("Common Stock",
    # "Class A Common Stock", "Stock Option (Right to Buy)", ...) — without
    # it a summary can only say "sold $48K" without saying of what.
    transactions = []
    for table, derivative in (("./nonDerivativeTable/nonDerivativeTransaction", False),
                              ("./derivativeTable/derivativeTransaction", True)):
        for tx in root.findall(table):
            code = _txt(tx, "transactionCoding/transactionCode")
            shares = _num(tx, "transactionAmounts/transactionShares/value")
            price = _num(tx, "transactionAmounts/transactionPricePerShare/value")
            transactions.append({
                "code": code,
                "type": TRANSACTION_CODES.get(code, code or "unknown"),
                "security": _txt(tx, "securityTitle/value") or ("derivative security" if derivative else "shares"),
                "derivative": derivative,
                "date": _txt(tx, "transactionDate/value"),
                "shares": shares,
                "price": price,
                "value_usd": round(shares * price, 2) if shares and price else None,
                "acquired_disposed": _txt(tx, "transactionAmounts/transactionAcquiredDisposedCode/value"),
                "shares_after": _num(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction/value"),
            })

    # Rule 10b5-1 flag: explicit checkbox first, footnote text as fallback.
    # Note: a footnote mention is filing-wide — in rare mixed filings it may
    # only apply to some of the transactions.
    plan_source = None
    if _txt(root, "aff10b5One").lower() in ("1", "true"):
        plan_source = "checkbox"
    else:
        for fn in root.findall("./footnotes/footnote"):
            text = "".join(fn.itertext())
            if _10B5_1_RE.search(text) and not _10B5_1_NEGATED_RE.search(text):
                plan_source = "footnote"
                break

    return {
        "issuer_name": pretty_company_name(_txt(issuer, "issuerName"), _txt(issuer, "issuerTradingSymbol")),
        "issuer_ticker": _txt(issuer, "issuerTradingSymbol"),
        "issuer_cik": _txt(issuer, "issuerCik"),
        "owner_name": _txt(owner, "reportingOwnerId/rptOwnerName"),
        "owner_cik": _txt(owner, "reportingOwnerId/rptOwnerCik"),
        "owner_roles": roles,
        "transactions": transactions,
        "has_derivative_transactions": root.find("./derivativeTable/derivativeTransaction") is not None,
        "plan_10b5_1": plan_source is not None,
        "plan_10b5_1_source": plan_source,
    }


async def _fetch_details(session: aiohttp.ClientSession, filing_index_url: str, headers: dict) -> dict:
    """filing_index_url is the Atom <link>, e.g.
    https://www.sec.gov/Archives/edgar/data/<cik>/<acc-no-dashes>/<acc>-index.htm"""
    directory = filing_index_url.rsplit("/", 1)[0]
    async with session.get(directory + "/index.json", headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        resp.raise_for_status()
        listing = await resp.json(content_type=None)
    await asyncio.sleep(SEC_EDGAR_MIN_REQUEST_INTERVAL_SECONDS)

    names = [item.get("name", "") for item in listing.get("directory", {}).get("item", [])]
    xml_names = [n for n in names if n.lower().endswith(".xml")]
    # Prefer anything that looks like the Form 4 itself; fall back to any XML.
    xml_names.sort(key=lambda n: 0 if "form4" in n.lower() or "f4" in n.lower() else 1)
    if not xml_names:
        raise ValueError("no XML document in filing directory")

    last_error = None
    for name in xml_names[:2]:
        async with session.get(f"{directory}/{name}", headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            resp.raise_for_status()
            body = (await resp.read()).strip()  # bytes: ElementTree honors the XML encoding declaration
        await asyncio.sleep(SEC_EDGAR_MIN_REQUEST_INTERVAL_SECONDS)
        try:
            return _parse_form4(body)
        except Exception as ex:
            last_error = ex
    raise ValueError(f"could not parse Form 4 XML: {last_error}")


def _fmt_money(v):
    if v is None:
        return None
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v / 1_000:.0f}K"
    return f"${v:,.0f}"


def _describe(d: dict) -> tuple[str, str]:
    """(card title, card summary) for the Insider Trades popup — also the
    text the AI summary sees as a fallback if structured details are missing."""
    issuer = d["issuer_name"] or "Unknown issuer"
    ticker = f" ({d['issuer_ticker']})" if d["issuer_ticker"] else ""
    roles = ", ".join(d["owner_roles"]) or "insider"
    title = f"{issuer}{ticker} — {d['owner_name'] or 'unknown filer'} ({roles})"

    parts = []
    for tx in d["transactions"]:
        bits = [tx["type"]]
        if tx["shares"] is not None:
            bits.append(f"{tx['shares']:,.0f} × {tx.get('security') or 'shares'}")
        if tx["price"]:
            bits.append(f"@ ${tx['price']:,.2f}")
        money = _fmt_money(tx["value_usd"])
        if money:
            bits.append(f"≈ {money}")
        if tx["shares_after"] is not None:
            bits.append(f"→ holds {tx['shares_after']:,.0f}")
        parts.append(" ".join(bits))
    if not parts:
        parts.append("derivative-only filing (options/RSUs)" if d["has_derivative_transactions"] else "no transactions listed")
    summary = "; ".join(parts)
    if d.get("plan_10b5_1"):
        codes = {tx.get("code") for tx in d["transactions"]}
        kind = ("sale" if codes & {"S"} and not codes & {"P"}
                else "purchase" if codes & {"P"} and not codes & {"S"}
                else "sale/purchase")
        summary += f" · pre-planned {kind} under a Rule 10b5-1 trading plan (less informative)"
    return title, summary


# EDGAR's getcurrent `type=4` is a PREFIX match: it also returns 424B2/424B3
# prospectuses (filed in bulk by big banks), 425, 40-F... — on 2026-09-24 a
# 100-entry page held only 3 actual Form 4 filings. So the collector pages
# through the feed (100 entries per page) until it has TARGET_FORM4_FILINGS
# real Form 4s or MAX_FEED_PAGES pages, whichever comes first.
TARGET_FORM4_FILINGS = 40
MAX_FEED_PAGES = 6


def _is_form4_title(title: str) -> bool:
    return title.startswith("4 - ") or title.startswith("4/A - ")


FEED_PAGE_TIMEOUT_SECONDS = 45
RETRY_AFTER_FAILED_POLL_SECONDS = 30


def _describe_error(ex: Exception) -> str:
    # asyncio/aiohttp timeouts have an EMPTY str() — which is why backend.log
    # showed "SEC EDGAR poll failed: " with nothing after it. Always include
    # the exception type.
    text = str(ex)
    return f"{type(ex).__name__}: {text}" if text else type(ex).__name__


async def _fetch_feed_page(session: aiohttp.ClientSession, headers: dict, start: int) -> list:
    """One 100-entry feed page, with a single retry. EDGAR's getcurrent
    endpoint is occasionally slow (2026-09-24: a page exceeded the old 20s
    timeout right after startup)."""
    params = {
        "action": "getcurrent", "type": "4", "company": "", "dateb": "",
        "owner": "include", "count": "100", "start": str(start), "output": "atom",
    }
    last_error = None
    for attempt in range(2):
        try:
            async with session.get(FEED_URL, params=params, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=FEED_PAGE_TIMEOUT_SECONDS)) as resp:
                resp.raise_for_status()
                body = await resp.text()
            await asyncio.sleep(SEC_EDGAR_MIN_REQUEST_INTERVAL_SECONDS)  # stay well under the 10 req/s limit
            return ET.fromstring(body).findall("atom:entry", ATOM_NS)
        except Exception as ex:
            last_error = ex
            logger.warning("SEC EDGAR feed page start=%d attempt %d failed: %s", start, attempt + 1, _describe_error(ex))
            await asyncio.sleep(3)
    raise last_error


async def _fetch_feed_entries(session: aiohttp.ClientSession, headers: dict) -> list:
    entries = []
    form4_accessions = set()
    for page in range(MAX_FEED_PAGES):
        try:
            page_entries = await _fetch_feed_page(session, headers, page * 100)
        except Exception:
            if page == 0:
                raise  # nothing at all — the poll really failed
            # Later page failed: keep what the earlier pages gave us rather
            # than throwing the whole poll away.
            logger.warning("SEC EDGAR: continuing with %d entries from %d page(s)", len(entries), page)
            break
        if not page_entries:
            break
        entries.extend(page_entries)
        for e in page_entries:
            t = e.find("atom:title", ATOM_NS)
            i = e.find("atom:id", ATOM_NS)
            if t is not None and t.text and _is_form4_title(t.text) and i is not None and i.text:
                form4_accessions.add(i.text)
        if len(form4_accessions) >= TARGET_FORM4_FILINGS:
            break
    return entries


async def _poll_once(session: aiohttp.ClientSession, emit, report_status) -> None:
    headers = {"User-Agent": SEC_EDGAR_USER_AGENT}
    try:
        entries = await _fetch_feed_entries(session, headers)
    except Exception as ex:
        logger.warning("SEC EDGAR poll failed: %s", _describe_error(ex))
        await report_status(False, _describe_error(ex))
        return False

    seen_accessions: set = set()
    placed = detailed = 0
    for entry in entries:
        try:
            id_el = entry.find("atom:id", ATOM_NS)
            title_el = entry.find("atom:title", ATOM_NS)
            link_el = entry.find("atom:link", ATOM_NS)
            updated_el = entry.find("atom:updated", ATOM_NS)

            feed_title = title_el.text if title_el is not None and title_el.text else ""
            # Only Form 4 (and amendments) — the feed occasionally mixes in
            # other form types like 424B2.
            if not _is_form4_title(feed_title):
                continue

            raw_id = id_el.text if id_el is not None and id_el.text else feed_title
            m = _ACCESSION_RE.search(raw_id)
            accession = m.group(1) if m else raw_id
            # Every filing appears twice (Reporting + Issuer entry): keep one.
            if accession in seen_accessions:
                continue
            seen_accessions.add(accession)

            link = link_el.get("href") if link_el is not None else None
            filed_at = updated_el.text if updated_el is not None else "unknown time"

            if accession not in _details_cache and link:
                try:
                    _details_cache[accession] = await _fetch_details(session, link, headers)
                except Exception as ex:
                    logger.warning("Form 4 details for %s unavailable: %s", accession, _describe_error(ex))
                    _details_cache[accession] = None
                if len(_details_cache) > _DETAILS_CACHE_MAX:
                    for old_key in list(_details_cache)[: len(_details_cache) - _DETAILS_CACHE_MAX]:
                        _details_cache.pop(old_key, None)

            details = _details_cache.get(accession)
            if details:
                title, summary = _describe(details)
                detailed += 1
            else:
                title = feed_title or "Form 4 filing"
                summary = f"Form 4 (insider transaction) filed at {filed_at}; details could not be parsed."

            event = make_event(
                event_id="insider-" + accession,
                category="insider",
                title=title,
                summary=summary,
                source="SEC EDGAR",
                url=link,
            )
            # Structured payload for /insider-summary (main.py). Extra keys
            # are ignored by the globe frontend and the popup's DTO.
            event["details"] = details
            event["filed_at"] = filed_at
            emit(event)
            placed += 1
        except Exception:
            logger.exception("Failed to parse a SEC EDGAR entry, skipping it")

    await report_status(True, f"{placed} Form 4 filings ({detailed} with parsed transaction details)")
    return True


async def run(emit, report_status) -> None:
    if not SEC_EDGAR_USER_AGENT:
        msg = "SEC_EDGAR_USER_AGENT is not set in .env — SEC requires a descriptive User-Agent (name + contact email). Collector disabled."
        logger.error(msg)
        await report_status(False, msg)
        return
    async with aiohttp.ClientSession() as session:
        while True:
            ok = await _poll_once(session, emit, report_status)
            # A failed poll (e.g. a slow EDGAR response) retries after 30s
            # instead of leaving the Insider Trades list empty for a full
            # SEC_EDGAR_POLL_SECONDS cycle.
            await asyncio.sleep(SEC_EDGAR_POLL_SECONDS if ok else RETRY_AFTER_FAILED_POLL_SECONDS)
